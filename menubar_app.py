#!/usr/bin/env python3
"""
menubar_app.py —— 菜单栏控制面板。

屏幕右上角常驻一个图标，点开是一组状态卡片，每个被监控的应用一张：
    ▶  绿点  = 该目标正在自动确认
    ⏸  灰点  = 该目标已暂停
    ⚠  橙点  = 缺权限 / 扫描异常
卡片上能看到目标是否在运行、累计点击次数、最近点了哪个按钮。
每张卡片下面有独立开关，底部还有「全部启用 / 全部暂停」。

启动：
    open -a AutoConfirm          （打包后）
    ./run_gui.sh                 （开发时直接跑）

设计说明：
  - 每个目标一个独立的 AutoConfirm 工作器 + 一个后台线程，计数和限流互不干扰。
  - 开关状态落在文件上，命令行版和图形版共用，终端里改了菜单栏会跟着变。
  - 启动默认全部暂停，需要手动点开。自动确认不该一启动就偷偷跑起来。
"""

from __future__ import annotations

import fcntl
import os
import subprocess
import sys
import threading
import time

try:
    import objc
    from AppKit import (
        NSApplication,
        NSApplicationActivationPolicyAccessory,
        NSApplicationActivationPolicyRegular,
        NSAttributedString,
        NSBezelStyleRounded,
        NSBezierPath,
        NSButton,
        NSColor,
        NSControlSizeSmall,
        NSFont,
        NSFontAttributeName,
        NSForegroundColorAttributeName,
        NSGraphicsContext,
        NSMenu,
        NSMenuItem,
        NSObject,
        NSStatusBar,
        NSTimer,
        NSVariableStatusItemLength,
        NSView,
    )
    from Foundation import NSAutoreleasePool, NSBundle, NSMakePoint, NSMakeRect
    from ApplicationServices import (
        AXIsProcessTrusted,
        AXIsProcessTrustedWithOptions,
        kAXTrustedCheckOptionPrompt,
    )
except ImportError:  # pragma: no cover
    sys.stderr.write(
        "缺少 pyobjc。请先运行：\n"
        "    python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt\n"
    )
    raise SystemExit(1)

from auto_confirm import (
    DEFAULT_CONFIG_PATH,
    STATE_DIR,
    AutoConfirm,
    Config,
    Logger,
    build_workers,
)
from permissions import PermissionsWindowController, accessibility_granted

# 顶部栏要用醒目的 ASCII 徽标：之前的 ▶ / ⏸ 太像普通系统按钮，
# 在有微信、输入法等图标时难以辨认；AC 则明确表示 AutoConfirm。
ICON_ON = "AC"
ICON_OFF = "AC"
ICON_WARN = "AC!"

ACCESSIBILITY_PANE = (
    "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
)

LOCK_PATH = os.path.join(STATE_DIR, ".instance.lock")

# 开机自启：用户级 LaunchAgent，登录时执行一次 `open -a AutoConfirm.app`。
# 不用 SMAppService：真正跑菜单的是 Python 子进程，用它注册会把 Python 当成登录项。
LAUNCH_AGENT_LABEL = "local.autoconfirm.launcher"
LAUNCH_AGENT_PATH = os.path.expanduser(
    f"~/Library/LaunchAgents/{LAUNCH_AGENT_LABEL}.plist"
)


def bootstrap_config() -> str:
    """
    准备用户配置。安装包里只带一份模板：首次运行时复制到
    ~/Library/Application Support/AutoConfirm/config.json，之后用户改的就是这份；
    升级后再补上新版本引入的规则（只增不删，原文件留 .bak）。
    """
    import shutil

    import auto_confirm
    import migrate_config

    user_path = auto_confirm.USER_CONFIG_PATH
    if not os.path.exists(user_path):
        os.makedirs(os.path.dirname(user_path), exist_ok=True)
        shutil.copy2(auto_confirm.BUNDLED_CONFIG_PATH, user_path)
    else:
        try:
            migrate_config.migrate(user_path)
        except Exception as exc:  # 配置升级失败不应该挡住启动
            sys.stderr.write(f"配置升级失败，继续用现有配置: {exc!r}\n")

    # 菜单里「打开配置文件」等处引用的是这个全局变量
    global DEFAULT_CONFIG_PATH
    DEFAULT_CONFIG_PATH = user_path
    return user_path


# Chromium（Kiro/VS Code/Cursor 都基于它）在窗口被完全挡住时会停止渲染界面，
# 新弹出的确认框不会进入辅助功能树，自动点击就看不到它。带上这两个启动参数
# 可以关掉这个行为。Kiro 的 argv.json 不接受它们，只能在启动时传入。
OCCLUSION_FLAGS = [
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
]


def process_has_occlusion_flag(pid: int | None) -> bool | None:
    """目标进程是否带着「被遮挡也继续渲染」的参数启动。查不到返回 None。"""
    if not pid:
        return None
    try:
        out = subprocess.run(
            ["/bin/ps", "-ww", "-o", "command=", "-p", str(pid)],
            capture_output=True, text=True, timeout=3,
        ).stdout
    except Exception:
        return None
    if not out:
        return None
    return OCCLUSION_FLAGS[0] in out


def app_path_for_pid(pid: int) -> str | None:
    from AppKit import NSRunningApplication

    app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
    url = app.bundleURL() if app is not None else None
    return url.path() if url is not None else None


def relaunch_with_occlusion_flags(pid: int, log: Logger) -> tuple[bool, str]:
    """正常退出目标应用（会走它自己的保存流程），再带上参数重新打开。"""
    from AppKit import NSRunningApplication

    path = app_path_for_pid(pid)
    if not path:
        return False, "找不到应用路径"
    app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
    app.terminate()

    # 不能用 app.isTerminated() 判断：它靠主线程的 KVO 更新，在后台线程里
    # 读到的一直是旧值，Kiro 明明退出了还会等满 30 秒然后放弃重启。
    # 直接按可执行文件路径查进程，主进程和残留的子进程都退干净才算退出。
    exe = os.path.join(path, "Contents", "MacOS")

    def still_running() -> bool:
        out = subprocess.run(
            ["/usr/bin/pgrep", "-f", exe], capture_output=True, text=True
        ).stdout
        return bool(out.strip())

    for _ in range(150):  # 最多等 30 秒
        if not still_running():
            break
        time.sleep(0.2)
    else:
        return False, "应用没有在 30 秒内退出（可能弹出了保存提示），已放弃重启"
    time.sleep(1.0)

    # -n：强制新开实例。否则 LaunchServices 若还记着旧实例，只会激活它、
    # 把 --args 丢掉，Kiro 就又以普通模式起来了。
    subprocess.Popen(["/usr/bin/open", "-n", "-a", path, "--args", *OCCLUSION_FLAGS])

    # 确认新进程真的带上了参数
    for _ in range(50):  # 最多等 10 秒
        time.sleep(0.2)
        out = subprocess.run(
            ["/bin/ps", "-axww", "-o", "command="], capture_output=True, text=True
        ).stdout
        mains = [l for l in out.splitlines() if l.startswith(exe + "/")]
        if mains:
            if any(OCCLUSION_FLAGS[0] in l for l in mains):
                log.info(f"已以兼容模式重新打开 {path}")
                return True, "已重启，兼容模式已生效"
            return False, "应用重新打开了，但没有带上兼容参数"
    return False, "应用没有重新打开，请手动打开"


def installed_app_path() -> str | None:
    # 安装包版：菜单组件在 <宿主>.app/Contents/Helpers/ 里，开机自启要拉起的就是
    # 当前这份宿主，不管用户把它放在「应用程序」还是别处
    exe = os.path.realpath(sys.executable)
    marker = ".app/Contents/Helpers/"
    if marker in exe:
        host = exe.split(marker, 1)[0] + ".app"
        if os.path.isdir(host):
            return host
    # 开发版：build_app.sh 装到的位置
    for path in ("/Applications/AutoConfirm.app",
                 os.path.expanduser("~/Applications/AutoConfirm.app")):
        if os.path.isdir(path):
            return path
    return None


# 记录用户是否手动设置过开机自启。没设置过就按默认（开启）处理；
# 用户手动关掉后就尊重这个选择，不会在下次启动时又偷偷打开。
LOGIN_PREF_PATH = os.path.join(STATE_DIR, ".launch_at_login_pref")


def launch_at_login_enabled() -> bool:
    return os.path.exists(LAUNCH_AGENT_PATH)


def remember_login_pref(enabled: bool) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(LOGIN_PREF_PATH, "w", encoding="utf-8") as fh:
        fh.write("on\n" if enabled else "off\n")


def apply_default_launch_at_login(log: Logger) -> None:
    """首次运行时默认开启开机自启；之后以用户的选择为准。"""
    if os.path.exists(LOGIN_PREF_PATH):
        return
    ok, message = set_launch_at_login(True)
    (log.info if ok else log.warn)(f"首次运行，默认{message}")
    if ok:
        remember_login_pref(True)


def set_launch_at_login(enabled: bool) -> tuple[bool, str]:
    """开关开机自启，返回 (是否成功, 说明)。"""
    if not enabled:
        # 先从 launchd 卸载（没加载过会报错，忽略），再删文件
        subprocess.run(
            ["/bin/launchctl", "bootout", f"gui/{os.getuid()}/{LAUNCH_AGENT_LABEL}"],
            capture_output=True,
        )
        try:
            os.remove(LAUNCH_AGENT_PATH)
        except FileNotFoundError:
            pass
        return True, "已关闭开机自启"

    app_path = installed_app_path()
    if app_path is None:
        return False, "没找到已安装的 AutoConfirm.app，请先执行 ./build_app.sh"

    import plistlib

    os.makedirs(os.path.dirname(LAUNCH_AGENT_PATH), exist_ok=True)
    with open(LAUNCH_AGENT_PATH, "wb") as fh:
        plistlib.dump(
            {
                "Label": LAUNCH_AGENT_LABEL,
                # 通过 open 启动，走 LaunchServices，和双击 app 完全一样
                "ProgramArguments": ["/usr/bin/open", "-a", app_path],
                "RunAtLoad": True,
                # 只在登录时拉起一次；用户主动退出后不会被重新拉起
                "KeepAlive": False,
            },
            fh,
        )
    return True, f"已开启开机自启（{app_path}）"

# 模块级变量，持有文件锁。一旦被回收锁就释放了，所以必须留着引用。
_lock_handle = None


def acquire_single_instance_lock() -> bool:
    """
    保证同时只有一个实例在跑，否则菜单栏会堆出一排图标、重复点击。

    用文件锁而不是查进程列表：进程退出时锁由内核自动释放，
    程序被强杀也不会留下需要手工清理的残留状态。
    """
    global _lock_handle
    try:
        os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
        handle = open(LOCK_PATH, "w", encoding="utf-8")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False

    handle.write(f"{os.getpid()}\n")
    handle.flush()
    _lock_handle = handle
    return True


# --------------------------------------------------------------------------- #
# 状态卡片
# --------------------------------------------------------------------------- #

CARD_WIDTH = 330.0
CARD_HEIGHT = 74.0
OVERVIEW_HEIGHT = 56.0

STATE_COLORS = {
    "on": lambda: NSColor.systemGreenColor(),
    "off": lambda: NSColor.systemGrayColor(),
    "warn": lambda: NSColor.systemOrangeColor(),
    "idle": lambda: NSColor.systemBlueColor(),
}


def _attr(text: str, size: float, color, bold: bool = False):
    font = (
        NSFont.boldSystemFontOfSize_(size) if bold else NSFont.systemFontOfSize_(size)
    )
    return NSAttributedString.alloc().initWithString_attributes_(
        text,
        {NSFontAttributeName: font, NSForegroundColorAttributeName: color},
    )


class StatusCardView(NSView):
    """
    一张卡片。用 drawRect_ 直接画，而不是拼一堆子视图——
    内容每秒刷新一次，自己画更好控制，也不用管子视图的布局。
    """

    def initWithFrame_(self, frame):
        self = objc.super(StatusCardView, self).initWithFrame_(frame)
        if self is None:
            return None
        self._payload = {
            "title": "",
            "state": "off",
            "state_text": "",
            "line2": "",
            "line3": "",
            "compact": False,
        }
        return self

    # 用左上角原点，从上往下排文字更直观
    def isFlipped(self) -> bool:
        return True

    def setPayload_(self, payload) -> None:
        self._payload = dict(payload)
        toggle = getattr(self, "_toggle", None)
        if toggle is not None:
            toggle.setState_(1 if self._payload.get("toggle_on") else 0)
            toggle.setEnabled_(bool(self._payload.get("toggle_enabled", True)))
        switch = getattr(self, "_switch", None)
        if switch is not None:
            switch.setState_(1 if self._payload.get("switch_on") else 0)
            switch.setEnabled_(bool(self._payload.get("switch_enabled", True)))
        self.setNeedsDisplay_(True)

    @objc.python_method
    def attach_switch(self, target, action: str) -> None:
        """总览卡片右侧的总开关：开 = 全部启用，关 = 全部暂停。"""
        switch = make_switch(target, action)
        size = switch.frame().size
        bounds = self.bounds()
        switch.setFrameOrigin_(NSMakePoint(
            bounds.size.width - 23.0 - size.width,
            (bounds.size.height - size.height) / 2.0,
        ))
        self.addSubview_(switch)
        self._switch = switch
        # 状态标签要让出开关的位置
        self._right_reserve = size.width + 10.0

    @objc.python_method
    def attach_toggle(self, target, action: str, target_id: str) -> None:
        """
        在卡片右下角放一个启用/暂停开关，和总览卡片的总开关同一种样式。
        开关在菜单项的自定义视图里，点它菜单不会收起，状态当场就能看到变化。
        """
        switch = make_switch(target, action)
        size = switch.frame().size
        bounds = self.bounds()
        switch.setFrameOrigin_(NSMakePoint(
            bounds.size.width - 23.0 - size.width,
            bounds.size.height - 13.0 - size.height,
        ))
        switch.setIdentifier_(target_id)
        self.addSubview_(switch)
        self._toggle = switch

    def drawRect_(self, _rect) -> None:
        p = self._payload
        compact = bool(p.get("compact"))
        bounds = self.bounds()
        color = STATE_COLORS.get(p.get("state", "off"), STATE_COLORS["off"])()

        card = NSMakeRect(
            11.0, 3.0, bounds.size.width - 22.0, bounds.size.height - 6.0
        )
        radius = 9.0
        path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            card, radius, radius
        )

        # 卡片底色 + 边框。菜单背景是半透明材质，只靠淡淡的填充根本看不出边界，
        # 所以这里用 controlBackgroundColor 打底再描一圈边，卡片感才立得住。
        NSColor.controlBackgroundColor().setFill()
        path.fill()
        NSColor.separatorColor().setStroke()
        path.setLineWidth_(1.0)
        path.stroke()

        text_x = card.origin.x + 16.0
        right = card.origin.x + card.size.width - 10.0 - getattr(self, "_right_reserve", 0.0)
        y = card.origin.y + (9.0 if not compact else 7.0)

        title = _attr(
            p.get("title", ""),
            13.5 if not compact else 13.0,
            NSColor.labelColor(),
            bold=True,
        )
        title.drawAtPoint_(NSMakePoint(text_x, y))

        # 状态做成药丸标签：带底色的圆角块，比一行小字醒目得多
        state_text = p.get("state_text", "")
        if state_text:
            st = _attr(state_text, 10.5, color, bold=True)
            tw = st.size().width
            pill_h = 17.0
            pill = NSMakeRect(right - tw - 14.0, y + 1.0, tw + 14.0, pill_h)
            pill_path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                pill, pill_h / 2.0, pill_h / 2.0
            )
            color.colorWithAlphaComponent_(0.16).setFill()
            pill_path.fill()
            st.drawAtPoint_(NSMakePoint(pill.origin.x + 7.0, y + 3.5))

        y += 21.0 if not compact else 20.0
        for key in ("line2", "line3"):
            text = p.get(key, "")
            if not text:
                continue
            line = _attr(text, 11.0, NSColor.secondaryLabelColor())
            line.drawAtPoint_(NSMakePoint(text_x, y))
            y += 15.0


class ToggleSwitch(NSView):
    """
    自己画的开关：开 = 绿色，关 = 灰色，不可用 = 半透明。

    不用系统 NSSwitch：它放进菜单后会按「非活动窗口」样式绘制，
    打开状态也是灰色，开和关几乎分不出来。
    """

    WIDTH, HEIGHT = 36.0, 20.0

    def initWithFrame_(self, frame):
        self = objc.super(ToggleSwitch, self).initWithFrame_(frame)
        if self is None:
            return None
        self._on = False
        self._enabled = True
        self._target = None
        self._action = None
        return self

    # -- 和 NSControl 一致的接口，控制器那边不用区分 ------------------ #

    def state(self) -> int:
        return 1 if self._on else 0

    def setState_(self, value) -> None:
        on = bool(value)
        if on != self._on:
            self._on = on
            self.setNeedsDisplay_(True)

    def isEnabled(self) -> bool:
        return self._enabled

    def setEnabled_(self, value) -> None:
        enabled = bool(value)
        if enabled != self._enabled:
            self._enabled = enabled
            self.setNeedsDisplay_(True)

    @objc.python_method
    def set_target_action(self, target, action: str) -> None:
        self._target = target
        self._action = action

    # -- 交互 ------------------------------------------------------------- #

    def acceptsFirstMouse_(self, _event) -> bool:
        return True

    def mouseDown_(self, _event) -> None:
        self.performClick_(None)

    def performClick_(self, _sender) -> None:
        if not self._enabled:
            return
        self._on = not self._on
        self.setNeedsDisplay_(True)
        if self._target is not None and self._action:
            NSApplication.sharedApplication().sendAction_to_from_(
                self._action, self._target, self
            )

    # -- 绘制 ------------------------------------------------------------- #

    def drawRect_(self, _rect) -> None:
        b = self.bounds()
        h = b.size.height
        track = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            b, h / 2.0, h / 2.0
        )
        alpha = 1.0 if self._enabled else 0.4
        if self._on:
            NSColor.systemGreenColor().colorWithAlphaComponent_(alpha).setFill()
        else:
            NSColor.colorWithSRGBRed_green_blue_alpha_(0.78, 0.78, 0.80, alpha).setFill()
        track.fill()

        inset = 2.0
        d = h - inset * 2.0
        x = b.size.width - inset - d if self._on else inset
        knob = NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(x, inset, d, d))
        NSColor.whiteColor().colorWithAlphaComponent_(1.0 if self._enabled else 0.8).setFill()
        knob.fill()
        NSColor.colorWithSRGBRed_green_blue_alpha_(0, 0, 0, 0.12 * alpha).setStroke()
        knob.setLineWidth_(0.5)
        knob.stroke()


def make_switch(target, action: str) -> ToggleSwitch:
    switch = ToggleSwitch.alloc().initWithFrame_(
        NSMakeRect(0.0, 0.0, ToggleSwitch.WIDTH, ToggleSwitch.HEIGHT)
    )
    switch.set_target_action(target, action)
    return switch


def make_card(height: float) -> StatusCardView:
    view = StatusCardView.alloc().initWithFrame_(
        NSMakeRect(0.0, 0.0, CARD_WIDTH, height)
    )
    return view


# --------------------------------------------------------------------------- #
# 后台扫描线程（每个目标一个）
# --------------------------------------------------------------------------- #

class WorkerThread(threading.Thread):
    def __init__(self, worker: AutoConfirm, poll_interval: float):
        super().__init__(daemon=True, name=f"scan-{worker.target.id}")
        self.worker = worker
        self.poll_interval = poll_interval
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.is_set():
            pool = NSAutoreleasePool.alloc().init()
            try:
                self.worker.tick_guarded()
            finally:
                del pool
            self._stop.wait(self.poll_interval)


# --------------------------------------------------------------------------- #
# 菜单栏控制器
# --------------------------------------------------------------------------- #

class MenubarController(NSObject):

    def initWithConfig_logger_(self, cfg, log):
        self = objc.super(MenubarController, self).init()
        if self is None:
            return None

        self.cfg = cfg
        self.log = log
        self.workers = build_workers(cfg, log, dry_run=False, on_click=self._on_click)
        self.last_click_text = {w.target.id: "还没点过" for w in self.workers}
        self.cards = {}

        # 启动默认全部暂停：逐个目标写自己的开关，而不是按总开关。
        # 这样之后单独启用某个目标时，不会顺带把别的目标也放出来。
        for w in self.workers:
            w.set_paused(True)
        self._clear_master()

        self._build_status_item()

        self.threads = [WorkerThread(w, cfg.poll_interval) for w in self.workers]
        for t in self.threads:
            t.start()

        self.timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            1.0, self, "refresh:", None, True
        )
        self.permissions = PermissionsWindowController.alloc().initWithLaunchAgentPath_toggleLogin_onReady_(
            LAUNCH_AGENT_PATH, self._toggle_login, self._on_permissions_ready
        )
        self.refresh_(None)
        # 必需权限没到位：启动就把权限设置窗口摆出来，权限都在这一处申请
        if not accessibility_granted():
            self.permissions.show()
        return self

    @objc.python_method
    def _on_permissions_ready(self) -> None:
        self.log.info("必需权限已就绪，自动确认现在可以启用")
        self.refresh_(None)

    @objc.python_method
    def _require_permissions(self) -> bool:
        """所有「启用」操作的门槛：缺必需权限时不执行，并把权限窗口调出来。"""
        if accessibility_granted():
            return True
        self.log.warn("缺少辅助功能权限，已打开权限设置窗口")
        self.permissions.show()
        return False

    # -- 构建界面 --------------------------------------------------------- #
    #
    # 下面这些内部方法都标了 @objc.python_method。pyobjc 默认会把类里的方法
    # 当成 Objective-C selector 暴露出去，名字和参数个数必须对得上，
    # 像 _worker(self, target_id) 这种就会直接报 BadPrototypeError。
    # 加上这个装饰器表示「这是普通 Python 方法，别当 selector」。

    @objc.python_method
    def _build_status_item(self) -> None:
        self.status_item = NSStatusBar.systemStatusBar().statusItemWithLength_(
            NSVariableStatusItemLength
        )
        self.status_item.button().setToolTip_("AutoConfirm 自动确认")

        menu = NSMenu.alloc().init()
        menu.setAutoenablesItems_(False)

        # 总览卡片
        self.overview_card = make_card(OVERVIEW_HEIGHT)
        self.overview_card.attach_switch(self, "toggleAll:")
        overview_item = NSMenuItem.alloc().init()
        overview_item.setView_(self.overview_card)
        menu.addItem_(overview_item)

        # 每个目标一张卡片，启用/暂停按钮直接放在卡片右下角
        for w in self.workers:
            menu.addItem_(NSMenuItem.separatorItem())

            card = make_card(CARD_HEIGHT)
            card.attach_toggle(self, "toggleTarget:", w.target.id)
            item = NSMenuItem.alloc().init()
            item.setView_(card)
            menu.addItem_(item)
            self.cards[w.target.id] = card

        menu.addItem_(NSMenuItem.separatorItem())
        self.item_occlusion = self._add_item(
            menu, "兼容模式重启 Kiro（窗口被挡住也能点击）…", "relaunchCompat:"
        )
        self.item_login = self._add_item(menu, "开机自启", "toggleLaunchAtLogin:")
        self.item_dry = self._add_item(menu, "试运行模式（只记录不点击）", "toggleDry:")
        self._add_item(menu, "打开日志", "openLog:")
        self._add_item(menu, "打开风险命令记录", "openRiskyLog:")
        self._add_item(menu, "打开配置文件", "openConfig:")
        self.item_perm = self._add_item(menu, "权限设置…", "showPermissions:")

        menu.addItem_(NSMenuItem.separatorItem())
        self._add_item(menu, "退出", "quit:")

        self.status_item.setMenu_(menu)

    @objc.python_method
    def _add_item(self, menu, title: str, action: str | None) -> NSMenuItem:
        item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, action, "")
        if action:
            item.setTarget_(self)
            item.setEnabled_(True)
        else:
            item.setEnabled_(False)
        menu.addItem_(item)
        return item

    # -- 状态 ------------------------------------------------------------- #

    @objc.python_method
    def _clear_master(self) -> None:
        try:
            os.remove(self.cfg.master_switch)
        except FileNotFoundError:
            pass

    @objc.python_method
    def _worker(self, target_id: str) -> "AutoConfirm | None":
        for w in self.workers:
            if w.target.id == target_id:
                return w
        return None

    @objc.python_method
    def _on_click(self, target_id: str, label: str, _window: str) -> None:
        prefix = "[试运行] " if any(w.dry_run for w in self.workers) else ""
        self.last_click_text[target_id] = (
            f"{prefix}{label} @ {time.strftime('%H:%M:%S')}"
        )

    # -- 菜单动作 --------------------------------------------------------- #

    def toggleTarget_(self, sender) -> None:
        # 卡片上的开关用 identifier 标识目标
        worker = self._worker(str(sender.identifier()))
        if worker is None:
            return
        # 暂停永远允许；启用要先过权限门槛，被拦下时让开关立刻弹回去
        if not worker.active and not self._require_permissions():
            self.refresh_(None)
            return
        now_active = worker.active
        worker.set_paused(now_active)
        self.log.info(
            f"[{worker.label}] 已{'暂停' if now_active else '启用'}（菜单操作）"
        )
        self.refresh_(None)

    def toggleAll_(self, sender) -> None:
        if sender.state() == 1:
            self.enableAll_(sender)
        else:
            self.pauseAll_(sender)
        # 被权限门槛拦下时，开关要弹回真实状态
        self.refresh_(None)

    def enableAll_(self, _sender) -> None:
        if not self._require_permissions():
            return
        for w in self.workers:
            w.set_paused(False)
        self.log.info("全部启用（菜单操作）")
        self.refresh_(None)

    def pauseAll_(self, _sender) -> None:
        for w in self.workers:
            w.set_paused(True)
        self._clear_master()
        self.log.info("全部暂停（菜单操作）")
        self.refresh_(None)

    def toggleDry_(self, _sender) -> None:
        new_value = not any(w.dry_run for w in self.workers)
        for w in self.workers:
            w.dry_run = new_value
        self.log.info("试运行模式：" + ("开" if new_value else "关"))
        self.refresh_(None)

    def openLog_(self, _sender) -> None:
        path = os.path.expanduser(self.cfg.log_file) if self.cfg.log_file else ""
        if path and os.path.exists(path):
            subprocess.Popen(["/usr/bin/open", "-a", "Console", path])
        else:
            self.log.warn(f"日志文件还不存在: {path or '(未配置)'}")

    @objc.python_method
    def _needs_compat(self) -> list:
        """正在运行、且没带兼容参数启动的目标。"""
        return [
            w for w in self.workers
            if w.target.enabled and w.target_pid
            and process_has_occlusion_flag(w.target_pid) is False
        ]

    def relaunchCompat_(self, _sender) -> None:
        from AppKit import NSAlert

        targets = self._needs_compat()
        if not targets:
            self.log.info("目标应用已经是兼容模式，无需重启")
            return
        names = "、".join(w.target_name or w.label for w in targets)
        alert = NSAlert.alloc().init()
        alert.setMessageText_(f"要以兼容模式重启 {names} 吗？")
        alert.setInformativeText_(
            "窗口被其他窗口完全挡住时，Kiro 会停止刷新界面，新弹出的确认框读不到，"
            "所以无法自动点击。兼容模式会关掉这个行为。\n\n"
            "重启会中断正在进行的对话和任务，建议在空闲时操作。"
            "重启后打开的窗口会恢复，但从程序坞直接打开的 Kiro 不带兼容模式，"
            "需要再点一次这里。"
        )
        alert.addButtonWithTitle_("重启")
        alert.addButtonWithTitle_("取消")
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        if alert.runModal() != 1000:  # NSAlertFirstButtonReturn
            return

        pids = [(w.label, w.target_pid) for w in targets]

        def job() -> None:
            for label, pid in pids:
                ok, msg = relaunch_with_occlusion_flags(pid, self.log)
                (self.log.info if ok else self.log.warn)(f"[{label}] 兼容模式重启：{msg}")

        threading.Thread(target=job, daemon=True).start()

    def toggleLaunchAtLogin_(self, _sender) -> None:
        self._toggle_login()

    @objc.python_method
    def _toggle_login(self) -> None:
        wanted = not launch_at_login_enabled()
        ok, message = set_launch_at_login(wanted)
        (self.log.info if ok else self.log.warn)(message)
        if ok:
            remember_login_pref(wanted)
        self.refresh_(None)

    def showPermissions_(self, _sender) -> None:
        self.permissions.show()

    def openRiskyLog_(self, _sender) -> None:
        from auto_confirm import RISKY_LOG_PATH

        if not os.path.exists(RISKY_LOG_PATH):
            # 还没有记录时建一个带说明的空文件，免得点了没反应
            with open(RISKY_LOG_PATH, "w", encoding="utf-8") as fh:
                fh.write("# 自动放行的风险命令（时间 / 目标 / 窗口 / 命中规则 / 命令片段）\n")
        subprocess.Popen(["/usr/bin/open", "-t", RISKY_LOG_PATH])

    def openConfig_(self, _sender) -> None:
        subprocess.Popen(["/usr/bin/open", "-t", DEFAULT_CONFIG_PATH])

    def quit_(self, _sender) -> None:
        for t in self.threads:
            t.stop()
        for w in self.workers:
            w.stop()
        self.log.info("菜单栏程序退出")
        NSApplication.sharedApplication().terminate_(None)

    # -- 定时刷新 --------------------------------------------------------- #

    def refresh_(self, _timer) -> None:
        trusted = AXIsProcessTrusted()
        dry = any(w.dry_run for w in self.workers)

        active_count = 0
        warn_count = 0
        total_clicks = 0

        for w in self.workers:
            payload = self._payload_for(w, trusted)
            card = self.cards.get(w.target.id)
            if card is not None:
                card.setPayload_(payload)

            if payload["state"] == "on":
                active_count += 1
            elif payload["state"] == "warn":
                warn_count += 1
            total_clicks += w.total_clicks

        # 总览
        if not trusted:
            ov_state, ov_text = "warn", "缺少辅助功能权限"
        elif warn_count:
            ov_state, ov_text = "warn", f"{warn_count} 个目标异常"
        elif active_count:
            ov_state, ov_text = "on", f"{active_count} 个运行中"
        else:
            ov_state, ov_text = "off", "全部暂停"

        self.overview_card.setPayload_({
            "title": "AutoConfirm" + ("（试运行）" if dry else ""),
            "state": ov_state,
            "state_text": ov_text,
            "line2": f"共 {len(self.workers)} 个目标 · 累计点击 {total_clicks} 次",
            "line3": "",
            "compact": True,
            # 有任意目标在运行就算「开」；没权限时只允许关、不允许开
            "switch_on": any(w.active for w in self.workers),
            "switch_enabled": trusted or any(w.active for w in self.workers),
        })

        # 顶部栏始终显示可辨认的 AC 徽标；运行中追加目标数，异常追加 !。
        # 例如：AC（暂停）、AC1（一个目标运行）、AC!（需处理）。
        if not trusted or warn_count:
            icon = ICON_WARN
        elif active_count:
            icon = ICON_ON + str(active_count)
        else:
            icon = ICON_OFF
        self.status_item.button().setTitle_(icon)
        self.status_item.button().setToolTip_(
            f"AutoConfirm：{ov_text}（点此打开控制面板）"
        )

        self.item_dry.setState_(1 if dry else 0)
        # 查进程参数要起子进程，每 10 秒刷新一次就够了
        now = time.monotonic()
        if now - getattr(self, "_compat_checked", -1e9) > 10.0:
            self._compat_checked = now
            self._compat_missing = {w.target.id for w in self._needs_compat()}
        missing = getattr(self, "_compat_missing", set())
        self.item_occlusion.setHidden_(not missing)
        self.item_login.setState_(1 if launch_at_login_enabled() else 0)
        # 权限设置入口始终可用；缺权限时标题直接提示
        self.item_perm.setTitle_("权限设置…" if trusted else "⚠ 权限设置（缺少必需权限）…")

    @objc.python_method
    def _recent_line(self, w: AutoConfirm) -> str:
        """
        卡片最后一行。最近一次是「拦截危险命令」时优先显示它，
        提醒用户有个弹窗在等手动处理，而不是让人以为程序没反应。
        """
        blocked_at = w.last_blocked_at
        clicked_at = w.last_click_at
        if blocked_at and (clicked_at is None or blocked_at > clicked_at):
            return (
                f"⚠ 已拦截危险命令，请手动确认 @ "
                f"{time.strftime('%H:%M:%S', time.localtime(blocked_at))}"
            )
        return "最近　" + self.last_click_text.get(w.target.id, "还没点过")

    @objc.python_method
    def _payload_for(self, w: AutoConfirm, trusted: bool) -> dict:
        running = w.target_name is not None

        if not w.target.enabled:
            state, state_text = "off", "配置已禁用"
        elif not trusted:
            state, state_text = "warn", "缺权限"
        elif w.last_error:
            state, state_text = "warn", "异常"
        elif w.active:
            state, state_text = "on", "运行中"
        else:
            state, state_text = "off", "已暂停"

        if w.last_error:
            line2 = w.last_error[:46]
        elif w.target.id in getattr(self, "_compat_missing", set()):
            line2 = "窗口被挡住时无法点击 · 见菜单「兼容模式重启」"
        else:
            line2 = (
                f"目标{'已锁定' if running else '未运行'}"
                f"　累计 {w.total_clicks} 次"
            )
            if running and w.target_pid:
                line2 += f"　pid {w.target_pid}"

        return {
            "title": w.label,
            "state": state,
            "state_text": state_text,
            "line2": line2,
            "line3": self._recent_line(w),
            "toggle_on": w.active,
            # 没有必需权限时「启用」不可点（已在运行的仍可以暂停）
            "toggle_enabled": w.target.enabled and (trusted or w.active),
            "compact": False,
        }


# --------------------------------------------------------------------------- #

def main() -> int:
    if not acquire_single_instance_lock():
        sys.stderr.write("已经有一个实例在运行，本次启动退出。\n")
        return 0

    # 不在这里弹系统授权框：所有权限申请都集中在「权限设置」窗口里，
    # 缺必需权限时控制器启动后会自动打开那个窗口。

    config_path = bootstrap_config()
    try:
        cfg = Config.load(config_path)
    except FileNotFoundError:
        sys.stderr.write(f"找不到配置文件: {config_path}\n")
        return 2

    log = Logger(cfg.log_file, verbose=False)
    log.info(
        f"菜单栏程序启动，目标 {[t.label for t in cfg.targets]}，默认全部暂停"
    )
    apply_default_launch_at_login(log)

    bundle = NSBundle.mainBundle()
    log.info(
        "运行环境: interpreter=%s bundle=%s id=%s 辅助功能权限=%s"
        % (
            sys.executable,
            bundle.bundlePath(),
            bundle.bundleIdentifier(),
            AXIsProcessTrusted(),
        )
    )

    app = NSApplication.sharedApplication()
    # 打包版由外层 .app 保持在 Dock；这个子进程只负责菜单栏控制器。
    # 直接运行脚本时默认仍是普通应用，便于开发和调试。
    agent_mode = os.environ.get("AUTOCONFIRM_MENU_AGENT") == "1"
    requested_policy = (
        NSApplicationActivationPolicyAccessory
        if agent_mode
        else NSApplicationActivationPolicyRegular
    )
    policy_changed = app.setActivationPolicy_(requested_policy)
    log.info(
        f"GUI 模式: {'菜单栏代理' if agent_mode else '普通应用'} "
        f"| 策略切换={'成功' if policy_changed else '失败'} "
        f"| 当前策略={app.activationPolicy()}"
    )

    controller = MenubarController.alloc().initWithConfig_logger_(cfg, log)
    if controller is None:
        sys.stderr.write("菜单栏控件初始化失败\n")
        return 1

    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
