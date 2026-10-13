#!/usr/bin/env python3
"""
cdp_confirm.py —— 备选方案：通过 Chrome DevTools Protocol 直接点 DOM 按钮。

什么时候用它：
  Kiro 是 Electron 应用，确认弹窗其实是渲染在 webview 里的 HTML。
  如果 ax_dump.py 找不到可点击元素（说明 AX 树没打开），就用这个。
  它直接在页面里执行 JS 找到按钮并 .click()，完全不依赖鼠标和窗口焦点，
  后台、最小化、甚至窗口被遮挡都能工作，比 AX 更可靠。

代价：
  需要带远程调试端口启动 Kiro，也就是要重启一次 Kiro：

      open -a Kiro --args --remote-debugging-port=9222

  （如果 App 名不是 Kiro，用 `ls /Applications | grep -i kiro` 确认）
  验证端口通了：curl http://127.0.0.1:9222/json/version

安全提示：
  远程调试端口只监听 127.0.0.1，但本机任何进程都能连上它并完全控制 Kiro
  （读取你打开的所有文件内容）。不用的时候正常重启 Kiro 关掉这个端口。

用法：
  python3 cdp_confirm.py --targets          # 看有哪些可连的页面
  python3 cdp_confirm.py --dry-run          # 只报告匹配到的按钮
  python3 cdp_confirm.py                    # 正式跑
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
import urllib.error
import urllib.request
from typing import Any

from auto_confirm import Config, DEFAULT_CONFIG_PATH, Logger, TargetConfig

try:
    import websocket  # websocket-client
except ImportError:  # pragma: no cover
    sys.stderr.write(
        "缺少 websocket-client。请运行：\n    pip3 install websocket-client\n"
    )
    raise SystemExit(1)


# --------------------------------------------------------------------------- #
# 注入页面执行的 JS
# --------------------------------------------------------------------------- #

CLICK_SCRIPT = r"""
(function(allowSrc, denySrc, markerSrc, requireMarker, skipLabels, dryRun) {
  var allow  = allowSrc.map(function (s) { return new RegExp(s, 'i'); });
  var deny   = denySrc.map(function (s) { return new RegExp(s, 'i'); });
  var marker = markerSrc.map(function (s) { return new RegExp(s, 'i'); });
  var skip   = {};
  skipLabels.forEach(function (l) { skip[l] = true; });

  var hitMarkers = [];

  // 上下文门禁：从按钮往上找有限层父节点，某一层的文字里必须出现弹窗特征。
  // 不用整页文字判断，否则聊天记录里出现 "review changes" 就会让门禁失效。
  function nearMarker(el, upLevels) {
    var cur = el;
    for (var lv = 0; lv < upLevels; lv++) {
      cur = cur.parentElement;
      if (!cur) return null;
      var t = (cur.innerText || '').replace(/\s+/g, ' ');
      if (t.length > 4000) t = t.slice(0, 4000);
      for (var i = 0; i < marker.length; i++) {
        if (marker[i].test(t)) return marker[i].source;
      }
    }
    return null;
  }

  var SELECTOR = [
    'button',
    'a[role="button"]',
    '[role="button"]',
    '.monaco-button',
    '.monaco-text-button',
    '.action-label',
    '[class*="button"]'
  ].join(',');

  function labelOf(el) {
    var t = (el.innerText || el.textContent || '').trim();
    if (!t) t = (el.getAttribute('aria-label') || '').trim();
    if (!t) t = (el.getAttribute('title') || '').trim();
    return t.replace(/\s+/g, ' ');
  }

  function visible(el) {
    if (!el.offsetParent && getComputedStyle(el).position !== 'fixed') return false;
    var r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return false;
    var st = getComputedStyle(el);
    if (st.visibility === 'hidden' || st.display === 'none') return false;
    if (parseFloat(st.opacity || '1') < 0.1) return false;
    return true;
  }

  function enabled(el) {
    if (el.disabled) return false;
    if (el.getAttribute('aria-disabled') === 'true') return false;
    if ((el.className || '').toString().indexOf('disabled') !== -1) return false;
    return true;
  }

  var clicked = [], seen = [], blocked = [];
  var nodes = document.querySelectorAll(SELECTOR);

  for (var i = 0; i < nodes.length; i++) {
    var el = nodes[i];
    var label = labelOf(el);
    if (!label || label.length > 60) continue;
    if (!visible(el) || !enabled(el)) continue;

    // 只收集有文字的候选，方便 --dry-run 观察
    seen.push(label);

    var denied = deny.some(function (re) { return re.test(label); });
    if (denied) { blocked.push(label); continue; }

    var ok = allow.some(function (re) { return re.test(label); });
    if (!ok) continue;
    if (skip[label]) continue;

    if (requireMarker) {
      var hit = nearMarker(el, 6);
      if (!hit) continue;
      if (hitMarkers.indexOf(hit) === -1) hitMarkers.push(hit);
    }

    if (!dryRun) {
      try { el.click(); } catch (e) { continue; }
    }
    clicked.push(label);
  }

  return JSON.stringify({
    clicked: clicked,
    blocked: blocked,
    seen: seen.slice(0, 200),
    markers: hitMarkers
  });
})
"""


# --------------------------------------------------------------------------- #
# CDP 客户端
# --------------------------------------------------------------------------- #

class CDPError(Exception):
    pass


def http_json(port: int, path: str, timeout: float = 3.0) -> Any:
    url = f"http://127.0.0.1:{port}{path}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError) as exc:
        raise CDPError(
            f"连不上 {url}：{exc}\n"
            f"Kiro 需要带调试端口启动：open -a Kiro --args --remote-debugging-port={port}"
        ) from exc


def list_page_targets(port: int) -> list[dict]:
    targets = http_json(port, "/json/list")
    return [
        t for t in targets
        if t.get("type") == "page" and t.get("webSocketDebuggerUrl")
    ]


class CDPSession:
    """一个极简的 CDP 连接，只用到 Runtime.callFunctionOn 相关能力。"""

    def __init__(self, ws_url: str, timeout: float = 5.0):
        self.ws_url = ws_url
        self.timeout = timeout
        self._ws = None
        self._next_id = 1

    def __enter__(self) -> "CDPSession":
        self._ws = websocket.create_connection(
            self.ws_url, timeout=self.timeout, suppress_origin=True
        )
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._ws is not None:
            try:
                self._ws.close()
            finally:
                self._ws = None

    def send(self, method: str, params: dict | None = None) -> dict:
        if self._ws is None:
            raise CDPError("连接未建立")

        msg_id = self._next_id
        self._next_id += 1
        self._ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))

        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            raw = self._ws.recv()
            if not raw:
                continue
            msg = json.loads(raw)
            if msg.get("id") != msg_id:
                continue  # 忽略事件和其他回复
            if "error" in msg:
                raise CDPError(f"{method} 失败: {msg['error']}")
            return msg.get("result", {})

        raise CDPError(f"{method} 超时")

    def eval_click(
        self,
        allow: list[str],
        deny: list[str],
        markers: list[str],
        require_marker: bool,
        skip_labels: list[str],
        dry_run: bool,
    ) -> dict:
        result = self.send(
            "Runtime.evaluate",
            {
                "expression": CLICK_SCRIPT,
                "returnByValue": False,
                "awaitPromise": False,
            },
        )
        obj = result.get("result", {})
        object_id = obj.get("objectId")
        if not object_id:
            raise CDPError(f"注入脚本没返回函数对象: {result}")

        try:
            call = self.send(
                "Runtime.callFunctionOn",
                {
                    "objectId": object_id,
                    "functionDeclaration":
                        "function(a, d, m, rm, s, dry) { return this(a, d, m, rm, s, dry); }",
                    "arguments": [
                        {"value": allow},
                        {"value": deny},
                        {"value": markers},
                        {"value": require_marker},
                        {"value": skip_labels},
                        {"value": dry_run},
                    ],
                    "returnByValue": True,
                },
            )
        finally:
            try:
                self.send("Runtime.releaseObject", {"objectId": object_id})
            except CDPError:
                pass

        if call.get("exceptionDetails"):
            raise CDPError(f"页面内 JS 抛异常: {call['exceptionDetails']}")

        payload = call.get("result", {}).get("value")
        if not payload:
            return {"clicked": [], "blocked": [], "seen": [], "markers": []}
        return json.loads(payload)


# --------------------------------------------------------------------------- #
# 主循环
# --------------------------------------------------------------------------- #

class CDPAutoConfirm:
    def __init__(
        self,
        cfg: Config,
        target: TargetConfig,
        log: Logger,
        port: int,
        dry_run: bool,
    ):
        self.cfg = cfg
        self.target = target
        self.log = log
        self.port = port
        self.dry_run = dry_run
        self.allow = [p.pattern for p in target.allow]
        self.deny = [p.pattern for p in target.deny]
        self.markers = [p.pattern for p in target.dialog_markers]
        self._last_click: dict[str, float] = {}
        self._running = True
        self._reported_seen = False

    def stop(self, *_: object) -> None:
        self._running = False

    def _skip_labels(self) -> list[str]:
        now = time.monotonic()
        return [
            label for label, ts in self._last_click.items()
            if now - ts < self.target.click_cooldown
        ]

    def tick(self) -> int:
        # 总开关或这个目标自己的开关，任一按下就停
        if os.path.exists(self.cfg.master_switch) or os.path.exists(
            self.target.off_switch
        ):
            return 0

        try:
            targets = list_page_targets(self.port)
        except CDPError as exc:
            self.log.warn(str(exc))
            time.sleep(2.0)
            return 0

        if not targets:
            self.log.debug("没有可连接的 page target")
            return 0

        skip = self._skip_labels()
        total = 0

        for target in targets:
            title = target.get("title", "?")
            try:
                with CDPSession(target["webSocketDebuggerUrl"]) as session:
                    res = session.eval_click(
                        self.allow,
                        self.deny,
                        self.markers,
                        self.target.require_dialog_marker,
                        skip,
                        self.dry_run,
                    )
            except (CDPError, OSError, websocket.WebSocketException) as exc:
                self.log.debug(f"target {title!r} 处理失败: {exc}")
                continue

            if not self._reported_seen and res.get("seen"):
                uniq = sorted(set(res["seen"]))
                self.log.info(f"target {title!r} 当前可见按钮文字样本: {uniq}")
                self._reported_seen = True

            for label in res.get("blocked", []):
                self.log.debug(f"跳过(黑名单) {label!r} @ {title!r}")

            for label in res.get("clicked", []):
                self._last_click[label] = time.monotonic()
                total += 1
                prefix = "[dry-run] 将点击" if self.dry_run else "已点击"
                self.log.info(f"{prefix} {label!r} @ {title!r}")

        return total

    def run(self) -> None:
        self.log.info(
            f"CDP 模式启动{'（dry-run）' if self.dry_run else ''}，"
            f"端口 {self.port}，轮询 {self.cfg.poll_interval}s"
        )
        while self._running:
            try:
                self.tick()
            except Exception as exc:
                self.log.warn(f"本轮异常: {exc!r}")
            time.sleep(self.cfg.poll_interval)
        self.log.info("已停止")


def main() -> int:
    parser = argparse.ArgumentParser(description="通过 CDP 自动点击 Kiro 确认按钮")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--port", type=int, default=None, help="远程调试端口，默认读配置")
    parser.add_argument("--dry-run", action="store_true", help="只报告不点击")
    parser.add_argument("--once", action="store_true", help="只跑一轮")
    parser.add_argument("--pages", action="store_true", help="列出可连接的页面后退出")
    parser.add_argument("--target", help="配置里目标的 id，默认第一个启用的")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    try:
        cfg = Config.load(args.config)
    except FileNotFoundError:
        sys.stderr.write(f"找不到配置文件: {args.config}\n")
        return 2

    target = None
    if args.target:
        target = cfg.target(args.target)
    else:
        target = next((t for t in cfg.targets if t.enabled), None)
    if target is None:
        sys.stderr.write("配置里没有可用的目标\n")
        return 2

    port = args.port or cfg.cdp_port
    log = Logger(cfg.log_file, verbose=args.verbose)

    if args.pages:
        try:
            for t in list_page_targets(port):
                print(f"- {t.get('title')!r}\n    url={t.get('url')}\n    ws={t.get('webSocketDebuggerUrl')}")
        except CDPError as exc:
            sys.stderr.write(str(exc) + "\n")
            return 1
        return 0

    worker = CDPAutoConfirm(cfg, target, log, port, dry_run=args.dry_run)
    signal.signal(signal.SIGINT, worker.stop)
    signal.signal(signal.SIGTERM, worker.stop)

    try:
        if args.once:
            n = worker.tick()
            log.info(f"单轮结束，点击 {n} 次")
        else:
            worker.run()
    finally:
        log.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
