#!/usr/bin/env python3
"""
auto_confirm.py —— 基于 macOS 辅助功能 (Accessibility / AXUIElement) 的后台自动确认。

原理：
  用 AX API 遍历目标应用的界面树，找到文字命中白名单的按钮，
  直接对它发送 AXPress 动作。不移动鼠标、不需要窗口在前台，所以后台也能用。

支持同时监控多个应用（Kiro / Cursor / VS Code …），每个目标有独立的：
  规则（白名单、黑名单、弹窗特征）、开关、计数、限流。

安全设计（三层，缺一不可）：
  1. deny_patterns  命中即绝不点击，优先级最高
  2. allow_patterns 只有命中的按钮才是候选
  3. dialog_markers 候选按钮的上层容器里必须有弹窗特征文字
  另有冷却时间、每分钟点击上限、暂停开关、--dry-run。

首次使用：
  1) pip install -r requirements.txt
  2) 给运行本脚本的终端授权：系统设置 → 隐私与安全性 → 辅助功能
  3) python3 ax_dump.py --watch 120     # 看清目标应用的按钮文字
  4) python3 auto_confirm.py --dry-run
  5) python3 auto_confirm.py
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

try:
    from AppKit import NSWorkspace
    from ApplicationServices import (
        AXIsProcessTrusted,
        AXUIElementCopyActionNames,
        AXUIElementCopyAttributeValue,
        AXUIElementCreateApplication,
        AXUIElementPerformAction,
        AXUIElementSetAttributeValue,
    )
except ImportError:  # pragma: no cover
    sys.stderr.write(
        "缺少 pyobjc 依赖。请先运行：\n"
        "    pip3 install -r requirements.txt\n"
        "或：pip3 install pyobjc-framework-Cocoa pyobjc-framework-ApplicationServices\n"
    )
    raise SystemExit(1)

AX_SUCCESS = 0

# 运行时状态目录。打包成 .app 之后应用以自己的身份运行，读不到 ~/Desktop 这类
# 受 macOS TCC 保护的目录，所以配置和状态都放在 Application Support。
STATE_DIR = os.path.expanduser("~/Library/Application Support/AutoConfirm")

BUNDLED_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "config.json"
)
USER_CONFIG_PATH = os.path.join(STATE_DIR, "config.json")


def resolve_config_path() -> str:
    """优先级：环境变量 > 用户配置 > 代码目录里的模板。"""
    env = os.environ.get("AUTO_CONFIRM_CONFIG")
    if env:
        return os.path.expanduser(env)
    if os.path.exists(USER_CONFIG_PATH):
        return USER_CONFIG_PATH
    return BUNDLED_CONFIG_PATH


DEFAULT_CONFIG_PATH = resolve_config_path()


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #

def _compile(patterns: Iterable[str]) -> list[re.Pattern]:
    return [re.compile(p, re.I) for p in patterns]


@dataclass
class TargetConfig:
    """一个被监控的应用，以及只作用于它的规则。"""

    id: str
    label: str
    bundle_ids: list[str] = field(default_factory=list)
    name_contains: list[str] = field(default_factory=list)
    enabled: bool = True

    allow: list[re.Pattern] = field(default_factory=list)
    deny: list[re.Pattern] = field(default_factory=list)
    dialog_markers: list[re.Pattern] = field(default_factory=list)
    # 弹窗上下文（含要执行的命令）命中这些规则时，即使按钮在白名单里也不点
    command_deny: list[re.Pattern] = field(default_factory=list)
    # 命中这些规则的命令照常放行，但会单独记录到风险日志里，方便事后核查
    command_warn: list[re.Pattern] = field(default_factory=list)
    # 后台页签：标签匹配 pending_tab 的页签会被切过去处理，再切回来
    background_tabs: bool = True
    pending_tab: re.Pattern = field(
        default_factory=lambda: re.compile(r"^Needs your input\b", re.I)
    )
    background_tab_idle_seconds: float = 5.0
    background_tab_wait_seconds: float = 3.0
    background_tab_retry_seconds: float = 60.0
    require_dialog_marker: bool = True
    dialog_context_up_levels: int = 6
    dialog_context_down_depth: int = 5

    warmup_seconds: float = 8.0
    click_cooldown: float = 2.0
    max_clicks_per_minute: int = 30
    max_tree_depth: int = 40
    max_tree_nodes: int = 6000

    @property
    def off_switch(self) -> str:
        """这个目标单独的暂停开关文件。"""
        return os.path.join(STATE_DIR, f"off.{self.id}")


@dataclass
class Config:
    targets: list[TargetConfig] = field(default_factory=list)
    poll_interval: float = 0.6
    # 总开关。存在即全部暂停，命令行 `touch ~/.auto_confirm_off` 可以一键停掉所有目标。
    kill_switch_file: str = "~/.auto_confirm_off"
    log_file: str = "~/Library/Logs/auto_confirm.log"
    cdp_port: int = 9222

    def target(self, target_id: str) -> "TargetConfig | None":
        for t in self.targets:
            if t.id == target_id:
                return t
        return None

    @property
    def master_switch(self) -> str:
        return os.path.expanduser(self.kill_switch_file)

    @classmethod
    def load(cls, path: str) -> "Config":
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: dict) -> "Config":
        globals_ = dict(
            poll_interval=float(raw.get("poll_interval", 0.6)),
            kill_switch_file=raw.get("kill_switch_file", "~/.auto_confirm_off"),
            log_file=raw.get("log_file", "~/Library/Logs/auto_confirm.log"),
            cdp_port=int(raw.get("cdp_port", 9222)),
        )

        if "targets" in raw:
            defaults = raw.get("defaults", {})
            targets = [
                cls._build_target(entry, defaults) for entry in raw["targets"]
            ]
        else:
            # 兼容旧的单目标格式：整个文件本身就是一个目标的规则
            targets = [cls._build_target(cls._legacy_target(raw), {})]

        return cls(targets=targets, **globals_)

    @staticmethod
    def _legacy_target(raw: dict) -> dict:
        app = raw.get("target_app", {})
        names = app.get("name_contains") or []
        bundles = app.get("bundle_ids") or []

        # 名字优先用 name_contains（"kiro" -> "Kiro"），它比 bundle id 更接近人读的名字。
        # 退而用 bundle id：dev.kiro.desktop 这种取尾段会得到 "Desktop"，
        # 所以跳过 desktop/app/macos 这类无意义的后缀。
        if names:
            label = str(names[0]).title()
        elif bundles:
            parts = [
                p for p in str(bundles[0]).split(".")
                if p.lower() not in {"com", "dev", "io", "org", "net",
                                     "desktop", "app", "macos", "mac"}
            ]
            label = (parts[-1] if parts else bundles[0]).title()
        else:
            label = "Target"

        entry = dict(raw)
        entry.pop("target_app", None)
        entry.update(
            id=label.lower().replace(" ", "-"),
            label=label,
            bundle_ids=bundles,
            name_contains=names,
            enabled=True,
        )
        return entry

    @staticmethod
    def _build_target(entry: dict, defaults: dict) -> TargetConfig:
        def pick(key: str, fallback: Any) -> Any:
            if key in entry:
                return entry[key]
            if key in defaults:
                return defaults[key]
            return fallback

        target_id = str(entry.get("id") or entry.get("label") or "target").strip()
        # 开关文件名里会用到 id，过滤掉路径分隔符之类的危险字符
        target_id = re.sub(r"[^A-Za-z0-9_.-]", "_", target_id) or "target"

        return TargetConfig(
            id=target_id,
            label=str(entry.get("label") or target_id),
            bundle_ids=[b.lower() for b in pick("bundle_ids", [])],
            name_contains=[n.lower() for n in pick("name_contains", [])],
            enabled=bool(entry.get("enabled", True)),
            allow=_compile(pick("allow_patterns", [])),
            deny=_compile(pick("deny_patterns", [])),
            dialog_markers=_compile(pick("dialog_markers", [])),
            command_deny=_compile(pick("command_deny_patterns", [])),
            command_warn=_compile(pick("command_warn_patterns", [])),
            background_tabs=bool(pick("background_tabs", True)),
            pending_tab=re.compile(
                pick("pending_tab_pattern", r"^Needs your input\b"), re.I
            ),
            background_tab_idle_seconds=float(pick("background_tab_idle_seconds", 5.0)),
            background_tab_wait_seconds=float(pick("background_tab_wait_seconds", 3.0)),
            background_tab_retry_seconds=float(pick("background_tab_retry_seconds", 60.0)),
            require_dialog_marker=bool(pick("require_dialog_marker", True)),
            dialog_context_up_levels=int(pick("dialog_context_up_levels", 6)),
            dialog_context_down_depth=int(pick("dialog_context_down_depth", 5)),
            warmup_seconds=float(pick("warmup_seconds", 8.0)),
            click_cooldown=float(pick("click_cooldown", 2.0)),
            max_clicks_per_minute=int(pick("max_clicks_per_minute", 30)),
            max_tree_depth=int(pick("max_tree_depth", 40)),
            max_tree_nodes=int(pick("max_tree_nodes", 6000)),
        )


# --------------------------------------------------------------------------- #
# 日志
# --------------------------------------------------------------------------- #

class Logger:
    """
    同时写 stdout 和日志文件，任意一路坏掉都不影响另一路。

    打包成 .app 之后 stdout 可能是个已经没人读的管道，直接 print 会抛
    BrokenPipeError。以前这个异常会被后台线程捕获并一直显示成「运行异常」，
    所以这里对两路输出都做了容错，坏掉就永久跳过。
    """

    def __init__(self, path: str | None, verbose: bool = False):
        self.verbose = verbose
        self._fh = None
        self._stdout_ok = True
        if path:
            expanded = os.path.expanduser(path)
            try:
                os.makedirs(os.path.dirname(expanded), exist_ok=True)
                self._fh = open(expanded, "a", encoding="utf-8")
            except OSError as exc:
                sys.stderr.write(f"[warn] 无法写日志文件 {expanded}: {exc}\n")

    def _emit(self, level: str, msg: str) -> None:
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{level}] {msg}"

        if self._stdout_ok:
            try:
                print(line, flush=True)
            except (OSError, ValueError):
                # 管道断了或 stdout 已关闭，之后不再尝试。
                # 顺手把 sys.stdout 换成黑洞，否则解释器退出时最后那次
                # 自动 flush 还会往 stderr 吐一条 BrokenPipeError 噪音。
                self._stdout_ok = False
                try:
                    sys.stdout = open(os.devnull, "w", encoding="utf-8")
                except OSError:
                    pass

        if self._fh is not None:
            try:
                self._fh.write(line + "\n")
                self._fh.flush()
            except (OSError, ValueError):
                self._fh = None

    def info(self, msg: str) -> None:
        self._emit("info", msg)

    def warn(self, msg: str) -> None:
        self._emit("warn", msg)

    def debug(self, msg: str) -> None:
        if self.verbose:
            self._emit("debug", msg)

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None


# --------------------------------------------------------------------------- #
# AX 基础封装
# --------------------------------------------------------------------------- #

def ax_attr(element: Any, name: str) -> Any:
    """读取一个 AX 属性，失败返回 None。"""
    try:
        err, value = AXUIElementCopyAttributeValue(element, name, None)
    except Exception:
        return None
    return value if err == AX_SUCCESS else None


def ax_actions(element: Any) -> list[str]:
    try:
        err, names = AXUIElementCopyActionNames(element, None)
    except Exception:
        return []
    if err != AX_SUCCESS or not names:
        return []
    return [str(n) for n in names]


def ax_children(element: Any) -> list[Any]:
    kids = ax_attr(element, "AXChildren")
    return list(kids) if kids else []


def as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    # AXValue 可能是数字/布尔/AXValue 结构，这些对按钮文字没意义
    if isinstance(value, (int, float, bool)):
        return ""
    try:
        return str(value).strip()
    except Exception:
        return ""


def element_label(element: Any, depth: int = 2) -> str:
    """
    取按钮的可读文字。
    Electron 里按钮文字常常在子节点的 AXStaticText 上，所以做有限深度的下探拼接。
    """
    for attr in ("AXTitle", "AXDescription", "AXHelp", "AXValue"):
        text = as_text(ax_attr(element, attr))
        if text:
            return text

    if depth <= 0:
        return ""

    parts: list[str] = []
    for child in ax_children(element)[:12]:
        text = element_label(child, depth - 1)
        if text:
            parts.append(text)
    return " ".join(parts).strip()


def window_label(element: Any) -> str:
    """向上找所属窗口的标题，用于日志和去重。"""
    cur = element
    for _ in range(40):
        if cur is None:
            break
        role = as_text(ax_attr(cur, "AXRole"))
        if role == "AXWindow":
            return as_text(ax_attr(cur, "AXTitle")) or "<untitled window>"
        cur = ax_attr(cur, "AXParent")
    return "<no window>"


# --------------------------------------------------------------------------- #
# 目标应用定位
# --------------------------------------------------------------------------- #

@dataclass
class TargetApp:
    pid: int
    name: str
    bundle_id: str
    element: Any


def find_target_app(target: TargetConfig) -> TargetApp | None:
    """
    两轮匹配：先按 bundle id 精确匹配，再退回按名字模糊匹配。
    这样可以避免命中 "Open and Save Panel Service (Kiro)" 这类同名辅助进程。
    """
    running = list(NSWorkspace.sharedWorkspace().runningApplications())

    def build(app: Any) -> TargetApp:
        pid = int(app.processIdentifier())
        return TargetApp(
            pid=pid,
            name=app.localizedName() or "?",
            bundle_id=(app.bundleIdentifier() or "").lower(),
            element=AXUIElementCreateApplication(pid),
        )

    for app in running:
        if (app.bundleIdentifier() or "").lower() in target.bundle_ids:
            return build(app)

    for app in running:
        lname = (app.localizedName() or "").lower()
        # 只接受名字本身就是目标的，不接受 "xxx (Kiro)" 这种附属进程
        if any(
            lname == token or lname.startswith(token)
            for token in target.name_contains
        ):
            return build(app)

    return None


def enable_electron_accessibility(app_element: Any, log: Logger) -> None:
    """
    Electron/Chromium 默认不把渲染进程的界面暴露到 AX 树。
    设置 AXManualAccessibility 可以强制打开，这是操作 VS Code 系应用的关键一步。
    """
    for attr in ("AXManualAccessibility", "AXEnhancedUserInterface"):
        try:
            err = AXUIElementSetAttributeValue(app_element, attr, True)
            log.debug(f"设置 {attr} -> err={err}")
        except Exception as exc:
            log.debug(f"设置 {attr} 失败: {exc}")


# --------------------------------------------------------------------------- #
# 按钮扫描
# --------------------------------------------------------------------------- #

CLICKABLE_ROLES = {
    "AXButton",
    "AXMenuButton",
    "AXRadioButton",
    "AXCheckBox",
    "AXLink",
    "AXStaticText",   # Electron 里常见：可点的其实是带 AXPress 的文本/分组
    "AXGroup",
    "AXCell",
    "AXImage",
    "AXUnknown",
}


@dataclass
class Candidate:
    element: Any
    label: str
    role: str
    window: str


@dataclass
class TabInfo:
    """页签按钮。Kiro 的会话页签标签形如 "Needs your input <标题> Close <标题>"。"""
    element: Any
    label: str
    selected: bool
    group: Any  # 所在页签组（父元素），切回原页签时要在同一组里找


@dataclass
class WindowScan:
    window: str
    element: Any = None
    texts: list[str] = field(default_factory=list)
    candidates: list[Candidate] = field(default_factory=list)
    tabs: list[TabInfo] = field(default_factory=list)

    def flat_text(self) -> str:
        return " ".join(" ".join(self.texts).split())


def scan_window(window_element: Any, target: TargetConfig) -> WindowScan:
    """
    广度遍历单个窗口的 AX 子树，同时收集：
      - 所有文本（诊断用，看窗口里到底有什么）
      - 所有支持 AXPress 且有文字的元素（点击候选）
    """
    title = as_text(ax_attr(window_element, "AXTitle")) or "<untitled window>"
    scan = WindowScan(window=title, element=window_element)

    queue: deque[tuple[Any, int]] = deque([(window_element, 0)])
    visited = 0

    while queue and visited < target.max_tree_nodes:
        element, depth = queue.popleft()
        visited += 1

        if depth > target.max_tree_depth:
            continue

        role = as_text(ax_attr(element, "AXRole"))

        for attr in ("AXTitle", "AXDescription", "AXValue"):
            text = as_text(ax_attr(element, attr))
            if text:
                scan.texts.append(text)

        if role in CLICKABLE_ROLES and "AXPress" in ax_actions(element):
            label = element_label(element)
            if label and as_text(ax_attr(element, "AXSubrole")) == "AXTabButton":
                scan.tabs.append(
                    TabInfo(
                        element=element,
                        label=" ".join(label.split()),
                        selected=bool(ax_attr(element, "AXSelected")),
                        group=ax_attr(element, "AXParent"),
                    )
                )
            if label:
                scan.candidates.append(
                    Candidate(
                        element=element,
                        label=label,
                        role=role or "<no role>",
                        window=title,
                    )
                )

        for child in ax_children(element):
            queue.append((child, depth + 1))

    return scan


def scan_windows(app_element: Any, target: TargetConfig) -> list[WindowScan]:
    windows = ax_attr(app_element, "AXWindows") or []
    return [scan_window(win, target) for win in windows]


def classify(label: str, target: TargetConfig) -> str:
    """返回 'deny' / 'allow' / 'ignore'。deny 优先。"""
    flat = " ".join(label.split())
    for pattern in target.deny:
        if pattern.search(flat):
            return "deny"
    for pattern in target.allow:
        if pattern.search(flat):
            return "allow"
    return "ignore"


def is_enabled(element: Any) -> bool:
    value = ax_attr(element, "AXEnabled")
    return True if value is None else bool(value)


def collect_text(element: Any, depth: int, budget: list[int]) -> str:
    """收集某个元素子树里的文字，深度和节点数都设上限，避免性能炸掉。"""
    if depth < 0 or budget[0] <= 0:
        return ""
    budget[0] -= 1

    parts: list[str] = []
    for attr in ("AXTitle", "AXDescription", "AXValue"):
        text = as_text(ax_attr(element, attr))
        if text:
            parts.append(text)

    for child in ax_children(element):
        if budget[0] <= 0:
            break
        text = collect_text(child, depth - 1, budget)
        if text:
            parts.append(text)

    return " ".join(parts)


def user_idle_seconds() -> float | None:
    """距离用户上一次按键/移动鼠标过了多少秒。拿不到就返回 None（按「不空闲」处理）。"""
    try:
        import Quartz

        return float(
            Quartz.CGEventSourceSecondsSinceLastEventType(
                Quartz.kCGEventSourceStateCombinedSessionState,
                Quartz.kCGAnyInputEventType,
            )
        )
    except Exception:
        return None


def command_block_reason(context_text: str, target: TargetConfig) -> str | None:
    """
    命令审批弹窗的二次把关：弹窗上下文里如果出现危险命令，返回命中的规则。

    按钮文字过滤只能防「点错按钮」，防不了「按钮对、但要执行的命令很危险」。
    Allow 一点下去命令就执行了，所以要先看清命令内容再决定。
    宁可误拦（留给用户手动点），不可误放。
    """
    flat = " ".join(context_text.split())
    for pattern in target.command_deny:
        if pattern.search(flat):
            return pattern.pattern
    return None


RISKY_LOG_PATH = os.path.expanduser("~/Library/Logs/auto_confirm_risky.log")


def command_warn_hit(context_text: str, target: TargetConfig) -> tuple[str, str] | None:
    """命中「放行但记录」规则时返回 (规则, 命中处前后的命令片段)。"""
    flat = " ".join(context_text.split())
    for pattern in target.command_warn:
        m = pattern.search(flat)
        if m:
            return pattern.pattern, flat[max(0, m.start() - 60): m.end() + 60]
    return None


def has_marker_near(element: Any, target: TargetConfig) -> tuple[bool, str, str]:
    """
    从按钮往上找有限层祖先，逐层检查该祖先子树里的文字是否含弹窗特征。

    为什么不用"整个窗口里有没有特征词"：聊天记录里可能正好出现 "review changes"
    这类文字，那样门禁就形同虚设。就近判定才能真正定位到弹窗容器。
    """
    cur = element
    for level in range(1, target.dialog_context_up_levels + 1):
        parent = ax_attr(cur, "AXParent")
        if parent is None:
            break
        cur = parent

        text = " ".join(
            collect_text(cur, target.dialog_context_down_depth, [400]).split()
        )
        for pattern in target.dialog_markers:
            if pattern.search(text):
                # 把弹窗容器的文字一起返回，命令审批弹窗要靠它检查命令内容
                return True, f"第 {level} 层祖先命中 /{pattern.pattern}/", text

    return False, "上层容器没有弹窗特征", ""


# --------------------------------------------------------------------------- #
# 单个目标的工作器
# --------------------------------------------------------------------------- #

class RateLimiter:
    def __init__(self, max_per_minute: int):
        self.max_per_minute = max_per_minute
        self._events: deque[float] = deque()

    def allow(self) -> bool:
        now = time.monotonic()
        while self._events and now - self._events[0] > 60.0:
            self._events.popleft()
        return len(self._events) < self.max_per_minute

    def record(self) -> None:
        self._events.append(time.monotonic())


class AutoConfirm:
    """负责一个目标应用。多目标就创建多个实例，各自独立计数和限流。"""

    def __init__(
        self,
        target: TargetConfig,
        master_switch: str,
        log: Logger,
        dry_run: bool = False,
        on_click: "Callable[[str, str, str], None] | None" = None,
    ):
        self.target = target
        self.master_switch = master_switch
        self.log = log
        self.dry_run = dry_run
        self.on_click = on_click

        self.limiter = RateLimiter(target.max_clicks_per_minute)
        self.total_clicks = 0
        self.last_click_label: str | None = None
        self.last_click_at: float | None = None
        self.last_error: str | None = None
        # 最近一次因危险命令被拦下的记录，界面用来提示「这个需要你手动处理」
        self.last_blocked_rule: str | None = None
        self.last_blocked_at: float | None = None

        self._last_click: dict[str, float] = {}
        self._blocked_logged: dict[str, float] = {}
        self._tab_skip: dict[str, float] = {}
        self._tab_wait_logged: dict[str, float] = {}
        self._last_ax_reopen = -1e9
        self._app: TargetApp | None = None
        self._ax_enabled_for_pid: int | None = None
        self._running = True
        self._warned_empty_markers = False

    # -- 状态查询（界面用）----------------------------------------------- #

    @property
    def label(self) -> str:
        return self.target.label

    @property
    def target_name(self) -> str | None:
        return self._app.name if self._app else None

    @property
    def target_pid(self) -> int | None:
        return self._app.pid if self._app else None

    @property
    def master_paused(self) -> bool:
        return os.path.exists(self.master_switch)

    @property
    def self_paused(self) -> bool:
        return os.path.exists(self.target.off_switch)

    @property
    def active(self) -> bool:
        """真正会干活的条件：总开关没按下、自己没暂停、配置里也没禁用。"""
        return self.target.enabled and not self.master_paused and not self.self_paused

    def set_paused(self, paused: bool) -> None:
        """
        只改这个目标自己的开关。
        打开时顺手清掉总开关，否则用户点了「启用」却因为总开关还按着而没反应。
        """
        if paused:
            os.makedirs(STATE_DIR, exist_ok=True)
            with open(self.target.off_switch, "w", encoding="utf-8") as fh:
                fh.write("paused\n")
        else:
            for path in (self.target.off_switch, self.master_switch):
                try:
                    os.remove(path)
                except FileNotFoundError:
                    pass

    def stop(self, *_: object) -> None:
        self._running = False

    # -- 单轮扫描 --------------------------------------------------------- #

    def tick(self, force: bool = False) -> int:
        """
        扫一轮。force=True 会忽略暂停状态，专门给「探测一次」用：
        暂停时也要能查看当前会匹配到什么，否则没法排查规则对不对。
        """
        if not force and not self.active:
            return 0

        app = self._ensure_app()
        if app is None:
            return 0

        target = self.target
        clicks = 0
        scans = scan_windows(app.element, target)
        if self._ax_tree_collapsed(scans):
            # 树塌了：本轮读到的都是空壳，重新打开后下一轮再扫
            return 0

        for scan in scans:
            self.log.debug(
                f"[{target.label}] 窗口 {scan.window!r}: "
                f"{len(scan.candidates)} 个可点击元素"
            )
            got = self._process_candidates(scan.candidates)
            if got is None:  # 触发限流，本轮到此为止
                return clicks
            clicks += got

        if target.background_tabs:
            clicks += self._handle_background_tabs(scans)

        return clicks

    def _process_candidates(self, candidates: list[Candidate]) -> int | None:
        """
        对一批候选按钮走完整套判定并点击。返回点击数；触发限流时返回 None。
        前台扫描和「切到后台页签后再扫一次」共用这一套逻辑，规则完全一致。
        """
        target = self.target
        clicks = 0
        for cand in candidates:
            verdict = classify(cand.label, target)

            if verdict == "deny":
                self.log.debug(f"[{target.label}] 跳过(黑名单) {cand.label!r}")
                continue
            if verdict == "ignore":
                continue

            if not is_enabled(cand.element):
                continue

            # 上下文门禁放在白名单之后：只有少量候选需要做这步较贵的检查。
            # 作用是防止误点编辑器里同名的按钮（Run and Debug / Restore 之类）。
            if target.require_dialog_marker:
                if not target.dialog_markers:
                    if not self._warned_empty_markers:
                        self.log.warn(
                            f"[{target.label}] require_dialog_marker=true 但 "
                            f"dialog_markers 为空，不会点击任何东西"
                        )
                        self._warned_empty_markers = True
                    continue
                ok, reason, context_text = has_marker_near(cand.element, target)
                if not ok:
                    self.log.debug(
                        f"[{target.label}] 跳过(不在弹窗内) {cand.label!r}: {reason}"
                    )
                    continue
                self.log.debug(f"[{target.label}] 上下文通过 {cand.label!r}: {reason}")

                # 命令审批的二次把关：弹窗里要执行的命令命中危险规则就不点，
                # 留给用户手动决定。同一个弹窗每分钟只记一次日志，避免刷屏。
                hit = command_block_reason(context_text, target)
                if hit:
                    self._note_blocked(cand, hit)
                    continue
                # 「放行但记录」：照常点，点成功后单独记一笔
                warn = command_warn_hit(context_text, target)
            else:
                warn = None

            if self._in_cooldown(cand):
                continue

            if not self.limiter.allow():
                self.log.warn(
                    f"[{target.label}] 已达每分钟 "
                    f"{target.max_clicks_per_minute} 次上限，本轮停止点击"
                )
                return None

            if self._press(cand):
                clicks += 1
                if warn and not self.dry_run:
                    self._record_risky(cand, *warn)

        return clicks

    # -- 后台页签 --------------------------------------------------------- #

    def _handle_background_tabs(self, scans: list[WindowScan]) -> int:
        """
        后台页签里的审批弹窗不会渲染进辅助功能树，必须切过去才看得见。
        流程：找到标记为「等待输入」的后台页签 → 切过去 → 按同一套规则处理
        → 切回用户原来的页签。

        切页签会改变用户眼前的画面，所以只在用户一段时间没动键盘鼠标时才做；
        处理不了的页签（危险命令、没找到能点的按钮）会冷却一段时间不再反复切。
        """
        target = self.target
        if self.dry_run:
            # 试运行不能有副作用，只报告
            for scan in scans:
                for tab in scan.tabs:
                    if not tab.selected and target.pending_tab.search(tab.label):
                        self.log.info(f"[{target.label}] [dry-run] 后台页签待处理: {tab.label!r}")
            return 0

        now = time.monotonic()
        for scan in scans:
            for tab in scan.tabs:
                if tab.selected or not target.pending_tab.search(tab.label):
                    continue
                key = f"{scan.window}||{tab.label}"
                if now - self._tab_skip.get(key, -1e9) < target.background_tab_retry_seconds:
                    continue
                if self._user_busy_in(scan):
                    if now - self._tab_wait_logged.get(key, -1e9) > 30.0:
                        self._tab_wait_logged[key] = now
                        self.log.info(
                            f"[{target.label}] 后台页签在等审批，你正在这个窗口里操作，"
                            f"停手 {target.background_tab_idle_seconds:g} 秒后处理: {tab.label!r}"
                        )
                    continue
                # 每轮最多处理一个后台页签，处理完立刻把画面还给用户
                return self._visit_tab(scan, tab, key)
        return 0

    def _user_busy_in(self, scan: WindowScan) -> bool:
        """
        切页签会不会打扰用户：只有页签所在的正是用户正在用的 Kiro 窗口
        （Kiro 在前台、且这是它的主窗口）时才算打扰，这时要等用户停手。
        用户在别的应用里、或页签在另一个 Kiro 窗口里，直接处理。
        """
        try:
            front = NSWorkspace.sharedWorkspace().frontmostApplication()
            kiro_front = front is not None and int(front.processIdentifier()) == self.target_pid
        except Exception:
            kiro_front = True  # 判断不了就按「可能打扰」处理
        if not kiro_front or not ax_attr(scan.element, "AXMain"):
            return False
        idle = user_idle_seconds()
        return idle is None or idle < self.target.background_tab_idle_seconds

    def _visit_tab(self, scan: WindowScan, tab: TabInfo, key: str) -> int:
        target = self.target
        original = next(
            (t for t in scan.tabs if t.selected and t.group == tab.group), None
        )
        self.log.info(f"[{target.label}] 切到后台页签处理: {tab.label!r}")

        try:
            if AXUIElementPerformAction(tab.element, "AXPress") != AX_SUCCESS:
                self._tab_skip[key] = time.monotonic()
                return 0

            clicks = 0
            deadline = time.monotonic() + target.background_tab_wait_seconds
            while time.monotonic() < deadline:
                time.sleep(0.4)  # 等页签内容渲染
                fresh = scan_window(scan.element, target)
                got = self._process_candidates(fresh.candidates)
                if got:
                    clicks += got
                    break
                if got is None:
                    break

            if clicks == 0:
                # 危险命令被拦、或弹窗里没有能点的按钮：留给用户，冷却后再试
                self._tab_skip[key] = time.monotonic()
                self.log.info(
                    f"[{target.label}] 后台页签没有可自动处理的弹窗，留给你手动处理: {tab.label!r}"
                )
            return clicks
        finally:
            self._restore_tab(scan, original)

    def _restore_tab(self, scan: WindowScan, original: "TabInfo | None") -> None:
        if original is None:
            return
        if AXUIElementPerformAction(original.element, "AXPress") == AX_SUCCESS:
            return
        # 元素引用失效（界面重绘过）时，按标签文字重新找一次
        title = original.label.split(" Close ")[0]
        for t in scan_window(scan.element, self.target).tabs:
            if title and title in t.label:
                AXUIElementPerformAction(t.element, "AXPress")
                return
        self.log.warn(f"[{self.target.label}] 没能切回原页签 {original.label!r}")

    def tick_guarded(self, force: bool = False) -> int:
        """给后台线程用：吞掉异常并记录，单轮失败不应该让整个程序停摆。"""
        try:
            count = self.tick(force=force)
            self.last_error = None
            return count
        except Exception as exc:
            self.last_error = repr(exc)
            self.log.warn(f"[{self.target.label}] 本轮扫描异常: {exc!r}")
            return 0

    def probe(self) -> int:
        """不点击的探测：临时切到 dry-run 并忽略暂停，日志里会列出会点什么。"""
        original = self.dry_run
        self.dry_run = True
        try:
            return self.tick_guarded(force=True)
        finally:
            self.dry_run = original

    def run(self, poll_interval: float) -> None:
        while self._running:
            self.tick_guarded()
            time.sleep(poll_interval)

    # -- 内部 ------------------------------------------------------------- #

    def _ensure_app(self) -> TargetApp | None:
        if self._app is not None:
            still_alive = any(
                int(a.processIdentifier()) == self._app.pid
                for a in NSWorkspace.sharedWorkspace().runningApplications()
            )
            if still_alive:
                return self._app
            self.log.warn(
                f"[{self.target.label}] 目标进程 {self._app.pid} 已退出，重新查找"
            )
            self._app = None

        app = find_target_app(self.target)
        if app is None:
            self.log.debug(f"[{self.target.label}] 没找到目标应用（未运行？）")
            return None

        self.log.info(
            f"[{self.target.label}] 锁定目标: {app.name} "
            f"(pid={app.pid}, bundle={app.bundle_id})"
        )
        if self._ax_enabled_for_pid != app.pid:
            enable_electron_accessibility(app.element, self.log)
            self._ax_enabled_for_pid = app.pid
            # Electron/Chromium 构建完整 AX 树需要几秒，1 秒不够（实测 1s 只能看到
            # 原生窗口按钮，6s 才能看到 webview 里的内容）
            self.log.info(
                f"[{self.target.label}] 等待 {self.target.warmup_seconds}s "
                f"让 {app.name} 构建辅助功能树…"
            )
            time.sleep(self.target.warmup_seconds)

        self._app = app
        return app

    def _ax_tree_collapsed(self, scans: list[WindowScan]) -> bool:
        """
        Electron 应用（Kiro 自动更新后重启时尤其常见）会把我们打开的辅助功能树
        重置掉，之后每个窗口只剩标题栏那几个节点，弹窗全都读不到。

        AXManualAccessibility 属性读出来不可靠（打开后仍可能读到 False），
        所以看实际效果：所有窗口都只有寥寥几个文字节点，就判定树塌了并重新打开。
        最多 20 秒重开一次，避免目标确实没内容时反复折腾。
        """
        if not scans or any(len(s.texts) > 20 for s in scans):
            return False
        now = time.monotonic()
        if now - self._last_ax_reopen < 20.0:
            return True
        self._last_ax_reopen = now
        self.log.info(
            f"[{self.target.label}] 读不到界面内容（应用可能刚重启或更新过），"
            f"重新打开辅助功能树"
        )
        enable_electron_accessibility(self._app.element, self.log)
        return True

    def _record_risky(self, cand: Candidate, rule: str, snippet: str) -> None:
        """自动放行了一条风险命令：写主日志，并追加到单独的风险日志便于事后核查。"""
        self.log.warn(
            f"[{self.target.label}] 已放行风险命令（只记录不拦截）/{rule}/: …{snippet}…"
        )
        try:
            with open(RISKY_LOG_PATH, "a", encoding="utf-8") as fh:
                fh.write(
                    f"{time.strftime('%Y-%m-%d %H:%M:%S')}\t{self.target.label}\t"
                    f"{cand.window}\t/{rule}/\t{snippet}\n"
                )
        except OSError as exc:
            self.log.warn(f"写风险日志失败: {exc}")

    def _note_blocked(self, cand: Candidate, rule: str) -> None:
        self.last_blocked_rule = rule
        self.last_blocked_at = time.time()
        key = self._cooldown_key(cand)
        last = self._blocked_logged.get(key)
        if last is None or time.monotonic() - last > 60.0:
            self._blocked_logged[key] = time.monotonic()
            self.log.warn(
                f"[{self.target.label}] 已拦截 {cand.label!r}：弹窗中的命令命中危险规则 "
                f"/{rule}/，请手动确认 (window={cand.window!r})"
            )

    def _cooldown_key(self, cand: Candidate) -> str:
        return f"{cand.window}||{cand.label}"

    def _in_cooldown(self, cand: Candidate) -> bool:
        last = self._last_click.get(self._cooldown_key(cand))
        return (
            last is not None
            and (time.monotonic() - last) < self.target.click_cooldown
        )

    def _press(self, cand: Candidate) -> bool:
        desc = (
            f"{cand.label!r} (role={cand.role}, window={cand.window!r})"
        )

        if self.dry_run:
            self.log.info(f"[{self.target.label}] [dry-run] 将点击 {desc}")
            self._last_click[self._cooldown_key(cand)] = time.monotonic()
            return False

        try:
            err = AXUIElementPerformAction(cand.element, "AXPress")
        except Exception as exc:
            self.log.warn(f"[{self.target.label}] 点击异常 {desc}: {exc!r}")
            return False

        self._last_click[self._cooldown_key(cand)] = time.monotonic()

        if err == AX_SUCCESS:
            self.limiter.record()
            self.total_clicks += 1
            self.last_click_label = cand.label
            self.last_click_at = time.time()
            self.log.info(f"[{self.target.label}] 已点击 {desc}")
            if self.on_click:
                try:
                    self.on_click(self.target.id, cand.label, cand.window)
                except Exception as exc:
                    self.log.warn(f"点击回调异常: {exc!r}")
            return True

        self.log.warn(f"[{self.target.label}] 点击失败 err={err} {desc}")
        return False


def build_workers(
    cfg: Config,
    log: Logger,
    dry_run: bool = False,
    on_click: "Callable[[str, str, str], None] | None" = None,
    only: str | None = None,
) -> list[AutoConfirm]:
    workers = []
    for target in cfg.targets:
        if only and target.id != only:
            continue
        workers.append(
            AutoConfirm(target, cfg.master_switch, log, dry_run, on_click)
        )
    return workers


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def list_apps() -> None:
    rows = []
    for app in NSWorkspace.sharedWorkspace().runningApplications():
        name = app.localizedName() or "?"
        bundle = app.bundleIdentifier() or "-"
        rows.append((name, bundle, int(app.processIdentifier())))
    rows.sort(key=lambda r: r[0].lower())
    print(f"{'名称':<32} {'Bundle ID':<44} PID")
    print("-" * 86)
    for name, bundle, pid in rows:
        print(f"{name:<32} {bundle:<44} {pid}")


def print_status(cfg: Config) -> None:
    master = os.path.exists(cfg.master_switch)
    print(f"总开关: {'已按下（全部暂停）' if master else '未按下'}")
    print(f"配置:   {DEFAULT_CONFIG_PATH}")
    print()
    print(f"{'目标':<16} {'状态':<12} {'运行中':<8} 规则")
    print("-" * 72)
    for t in cfg.targets:
        if not t.enabled:
            state = "配置已禁用"
        elif master or os.path.exists(t.off_switch):
            state = "暂停"
        else:
            state = "运行中"
        app = find_target_app(t)
        print(
            f"{t.label:<16} {state:<12} {('是' if app else '否'):<8} "
            f"allow={len(t.allow)} deny={len(t.deny)} marker={len(t.dialog_markers)}"
        )


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="确认弹窗后台自动点击（支持多目标）")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH, help="配置文件路径")
    parser.add_argument("--dry-run", action="store_true", help="只打印不点击")
    parser.add_argument("--once", action="store_true", help="每个目标只扫一轮后退出")
    parser.add_argument("--target", help="只处理指定 id 的目标")
    parser.add_argument("--verbose", "-v", action="store_true", help="输出调试日志")
    parser.add_argument("--status", action="store_true", help="打印各目标状态后退出")
    parser.add_argument(
        "--list-apps", action="store_true", help="列出运行中的 App 及 bundle id"
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.list_apps:
        list_apps()
        return 0

    try:
        cfg = Config.load(args.config)
    except FileNotFoundError:
        sys.stderr.write(f"找不到配置文件: {args.config}\n")
        return 2
    except (json.JSONDecodeError, re.error, ValueError) as exc:
        sys.stderr.write(f"配置文件有问题: {exc}\n")
        return 2

    if args.status:
        print_status(cfg)
        return 0

    if not AXIsProcessTrusted():
        sys.stderr.write(
            "\n未获得辅助功能权限，无法读取其他应用的界面。\n"
            "请打开：系统设置 → 隐私与安全性 → 辅助功能，\n"
            "把运行本脚本的程序加进去并勾选，然后重启该程序再试。\n\n"
        )
        return 2

    log = Logger(cfg.log_file, verbose=args.verbose)
    workers = build_workers(cfg, log, dry_run=args.dry_run, only=args.target)

    if not workers:
        sys.stderr.write("没有匹配到任何目标，检查配置里的 targets 或 --target 参数\n")
        return 2

    def stop_all(*_: object) -> None:
        for w in workers:
            w.stop()

    signal.signal(signal.SIGINT, stop_all)
    signal.signal(signal.SIGTERM, stop_all)

    try:
        if args.once:
            for w in workers:
                count = w.tick()
                log.info(f"[{w.label}] 单轮结束，点击 {count} 次")
        else:
            log.info(
                f"启动{'（dry-run）' if args.dry_run else ''}，"
                f"目标 {[w.label for w in workers]}，"
                f"轮询 {cfg.poll_interval}s，总开关 {cfg.kill_switch_file}"
            )
            running = True
            while running:
                for w in workers:
                    w.tick_guarded()
                    if not w._running:
                        running = False
                time.sleep(cfg.poll_interval)
            log.info("已停止")
    finally:
        log.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
