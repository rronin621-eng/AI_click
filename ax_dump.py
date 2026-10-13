#!/usr/bin/env python3
"""
ax_dump.py —— 诊断工具：查看目标应用的辅助功能 (AX) 界面树。

自动确认能不能工作，完全取决于目标应用的按钮有没有出现在 AX 树里、文字叫什么。
调规则之前先用这个看清楚。

用法：
  python3 ax_dump.py --watch 120         # 最常用：监控模式，实时显示新按钮和判定结果
  python3 ax_dump.py --pressable-only    # 一次性列出所有可点击元素
  python3 ax_dump.py --grep accept       # 只看文字含 accept 的元素
  python3 ax_dump.py --target cursor     # 指定配置里的某个目标
  python3 ax_dump.py --list-targets      # 看配置里有哪些目标

注意：这个工具不看暂停开关，所以自动确认处于暂停状态时也能用来排查。
"""

from __future__ import annotations

import argparse
import sys
import time

from auto_confirm import (
    DEFAULT_CONFIG_PATH,
    Config,
    Logger,
    TargetConfig,
    as_text,
    ax_actions,
    ax_attr,
    ax_children,
    classify,
    element_label,
    enable_electron_accessibility,
    find_target_app,
    scan_windows,
)

try:
    from ApplicationServices import AXIsProcessTrusted
except ImportError:  # pragma: no cover
    sys.stderr.write("缺少 pyobjc，请先 pip3 install -r requirements.txt\n")
    raise SystemExit(1)


def pick_target(cfg: Config, wanted: str | None) -> TargetConfig | None:
    if wanted:
        return cfg.target(wanted)
    for t in cfg.targets:
        if t.enabled:
            return t
    return cfg.targets[0] if cfg.targets else None


def walk(element, depth: int, max_depth: int, args, stats: dict) -> None:
    if depth > max_depth:
        return

    stats["nodes"] += 1

    role = as_text(ax_attr(element, "AXRole")) or "?"
    subrole = as_text(ax_attr(element, "AXSubrole"))
    title = as_text(ax_attr(element, "AXTitle"))
    desc = as_text(ax_attr(element, "AXDescription"))
    value = as_text(ax_attr(element, "AXValue"))
    ident = as_text(ax_attr(element, "AXIdentifier"))
    pressable = "AXPress" in ax_actions(element)

    if pressable:
        stats["pressable"] += 1

    label = element_label(element) if pressable else (title or desc or value)

    show = True
    if args.pressable_only and not pressable:
        show = False
    if args.grep:
        haystack = " ".join([role, subrole, title, desc, value, ident, label]).lower()
        if args.grep.lower() not in haystack:
            show = False

    if show:
        bits = [f"{'  ' * depth}{role}"]
        if subrole:
            bits.append(f"subrole={subrole}")
        if title:
            bits.append(f"title={title!r}")
        if desc:
            bits.append(f"desc={desc!r}")
        if value and value != title:
            bits.append(f"value={value!r}")
        if ident:
            bits.append(f"id={ident!r}")
        if pressable:
            bits.append(f">>> PRESSABLE label={label!r}")
        print(" ".join(bits))

    for child in ax_children(element):
        walk(child, depth + 1, max_depth, args, stats)


def watch(app_element, target: TargetConfig, seconds: float, interval: float) -> None:
    """
    监控模式：只在界面出现新按钮时打印，并标注按当前规则会不会被点。
    跑起来之后去触发确认弹窗，就能看到弹窗里按钮的真实文字。
    """
    print(f"监控中（{seconds}s，每 {interval}s 扫一次）。现在去触发确认弹窗…\n")
    known: set[str] = set()
    deadline = time.time() + seconds

    while time.time() < deadline:
        for scan in scan_windows(app_element, target):
            flat = scan.flat_text()
            markers = [p.pattern for p in target.dialog_markers if p.search(flat)]

            fresh = []
            for cand in scan.candidates:
                key = f"{scan.window}||{cand.label}"
                if key in known:
                    continue
                known.add(key)
                fresh.append(cand)

            if not fresh and not markers:
                continue

            if markers:
                marker_key = f"__markers__{scan.window}||{sorted(markers)}"
                if marker_key not in known:
                    known.add(marker_key)
                    print(f"[弹窗特征命中] 窗口 {scan.window!r} -> {markers}")

            for cand in fresh:
                verdict = classify(cand.label, target)
                flag = {
                    "allow": "会点击 ✅",
                    "deny": "黑名单 ⛔",
                    "ignore": "忽略 ·",
                }[verdict]
                gate = "" if markers else "（但窗口没命中弹窗特征，实际不会点）"
                print(
                    f"  {flag} {cand.label!r} role={cand.role} "
                    f"窗口={scan.window!r} {gate}"
                )

        time.sleep(interval)

    print("\n监控结束。")


def main() -> int:
    parser = argparse.ArgumentParser(description="查看目标应用的 AX 界面树")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--target", help="配置里目标的 id，默认第一个启用的")
    parser.add_argument("--list-targets", action="store_true", help="列出配置里的目标")
    parser.add_argument("--depth", type=int, default=25, help="最大遍历深度")
    parser.add_argument("--pressable-only", action="store_true", help="只显示可点击元素")
    parser.add_argument("--grep", help="只显示文字包含该关键字的元素")
    parser.add_argument(
        "--wait", type=float, default=0.0,
        help="启动后先等待 N 秒，方便手动把弹窗弄出来",
    )
    parser.add_argument(
        "--watch", type=float, metavar="SECONDS", default=None,
        help="监控模式：持续 N 秒，实时打印新出现的按钮及判定结果",
    )
    args = parser.parse_args()

    cfg = Config.load(args.config)

    if args.list_targets:
        print(f"{'id':<12} {'名称':<14} {'启用':<6} bundle_ids")
        print("-" * 70)
        for t in cfg.targets:
            print(
                f"{t.id:<12} {t.label:<14} "
                f"{('是' if t.enabled else '否'):<6} {t.bundle_ids}"
            )
        return 0

    if not AXIsProcessTrusted():
        sys.stderr.write(
            "未获得辅助功能权限。系统设置 → 隐私与安全性 → 辅助功能，"
            "勾选运行本脚本的终端程序后重启它。\n"
        )
        return 2

    target = pick_target(cfg, args.target)
    if target is None:
        sys.stderr.write("配置里没有可用的目标\n")
        return 2

    log = Logger(None, verbose=True)

    app = find_target_app(target)
    if app is None:
        sys.stderr.write(
            f"没找到目标应用 {target.label}（bundle_ids={target.bundle_ids}）。\n"
            "先确认它在运行，然后用 `python3 auto_confirm.py --list-apps` "
            "查到真实 bundle id，填进配置。\n"
        )
        return 1

    print(f"目标: {target.label} -> {app.name} (pid={app.pid}, bundle={app.bundle_id})\n")
    enable_electron_accessibility(app.element, log)
    print(f"等待 {target.warmup_seconds}s 让应用构建辅助功能树…")
    time.sleep(target.warmup_seconds)

    if args.watch is not None:
        watch(app.element, target, args.watch, max(cfg.poll_interval, 0.3))
        return 0

    if args.wait > 0:
        print(f"等待 {args.wait}s，请把确认弹窗弄出来...")
        time.sleep(args.wait)

    windows = ax_attr(app.element, "AXWindows") or []
    if not windows:
        print("AX 树里没有窗口，说明辅助功能树没成功打开。")
        print("可以试试备选方案：python3 cdp_confirm.py --help")
        return 1

    stats = {"nodes": 0, "pressable": 0}
    for idx, win in enumerate(windows):
        print(f"===== 窗口 {idx}: {as_text(ax_attr(win, 'AXTitle'))!r} =====")
        walk(win, 0, args.depth, args, stats)
        print()

    print(f"合计遍历 {stats['nodes']} 个节点，其中可点击 {stats['pressable']} 个。")
    if stats["pressable"] == 0:
        print("一个可点击元素都没有，基本可以判定 AX 方案走不通，改用 cdp_confirm.py。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
