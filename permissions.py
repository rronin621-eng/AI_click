#!/usr/bin/env python3
"""
permissions.py —— 权限设置窗口。

AutoConfirm 用到的所有系统权限都集中在这一个窗口里申请、查看状态：
  - 辅助功能（必需）：读取目标应用界面、点击按钮
  - 登录项 / 后台项目（可选）：开机自启

必需权限没授予之前，菜单里所有「启用」都不可用；这个窗口的「完成」按钮
也要等必需权限到位才能点。程序不会在别处再弹系统的授权对话框，
所有申请都从这里的按钮发起。
"""

from __future__ import annotations

import os
import subprocess
from typing import Callable

import objc
from AppKit import (
    NSApplication,
    NSBackingStoreBuffered,
    NSBezelStyleRounded,
    NSButton,
    NSColor,
    NSFont,
    NSTextField,
    NSTimer,
    NSWindow,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskTitled,
)
from ApplicationServices import (
    AXIsProcessTrusted,
    AXIsProcessTrustedWithOptions,
    kAXTrustedCheckOptionPrompt,
)
from Foundation import NSMakeRect, NSObject, NSURL

ACCESSIBILITY_PANE = (
    "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
)
LOGIN_ITEMS_PANE = "x-apple.systempreferences:com.apple.LoginItems-Settings.extension"


# --------------------------------------------------------------------------- #
# 权限状态
# --------------------------------------------------------------------------- #

def accessibility_granted() -> bool:
    return bool(AXIsProcessTrusted())


def request_accessibility() -> None:
    """
    把本应用登记进「辅助功能」列表（系统会弹一次标准提示），再打开设置页。
    只在用户点了窗口里的按钮时调用，程序不会主动弹。
    """
    try:
        AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: True})
    except Exception:
        pass
    subprocess.Popen(["/usr/bin/open", ACCESSIBILITY_PANE])


def login_item_status(agent_path: str) -> str:
    """
    返回 'off' / 'enabled' / 'needs_approval'。
    LaunchAgent 写好之后，macOS 13+ 还可能要求用户在「登录项」里批准一次。
    """
    if not os.path.exists(agent_path):
        return "off"
    try:
        from ServiceManagement import SMAppService

        status = SMAppService.statusForLegacyURL_(NSURL.fileURLWithPath_(agent_path))
        # 1 = enabled, 2 = requiresApproval
        if int(status) == 2:
            return "needs_approval"
    except Exception:
        # 老系统没有这个 API：文件在就当作已开启
        pass
    return "enabled"


# --------------------------------------------------------------------------- #
# 窗口
# --------------------------------------------------------------------------- #

WIN_W, WIN_H = 540.0, 330.0
ROW_H = 84.0


def _label(text: str, frame, size: float = 13.0, bold: bool = False, color=None):
    field = NSTextField.labelWithString_(text)
    field.setFrame_(frame)
    field.setFont_(NSFont.boldSystemFontOfSize_(size) if bold else NSFont.systemFontOfSize_(size))
    if color is not None:
        field.setTextColor_(color)
    field.setLineBreakMode_(0)  # NSLineBreakByWordWrapping
    field.cell().setWraps_(True)
    return field


def _button(title: str, frame, target, action: str):
    button = NSButton.alloc().initWithFrame_(frame)
    button.setTitle_(title)
    button.setBezelStyle_(NSBezelStyleRounded)
    button.setTarget_(target)
    button.setAction_(action)
    return button


class PermissionsWindowController(NSObject):
    """
    一行一项权限：名称 + 用途说明 + 当前状态 + 操作按钮。
    每秒刷新一次状态，用户在系统设置里勾选后，这里会自动变成「已授权」。
    """

    def initWithLaunchAgentPath_toggleLogin_onReady_(self, agent_path, toggle_login, on_ready):
        self = objc.super(PermissionsWindowController, self).init()
        if self is None:
            return None
        self.agent_path = agent_path
        self.toggle_login: Callable[[], None] = toggle_login
        self.on_ready: Callable[[], None] = on_ready
        self._was_ready = accessibility_granted()
        self._build()
        self.timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            1.0, self, "refresh:", None, True
        )
        self.refresh_(None)
        return self

    @objc.python_method
    def _build(self) -> None:
        style = NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
        win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, WIN_W, WIN_H), style, NSBackingStoreBuffered, False
        )
        win.setTitle_("AutoConfirm 权限设置")
        win.setReleasedWhenClosed_(False)
        win.center()
        self.window = win
        view = win.contentView()

        view.addSubview_(_label(
            "AutoConfirm 需要以下权限。必需权限授予之后，才能启用自动确认。",
            NSMakeRect(24, WIN_H - 52, WIN_W - 48, 24), 13.0,
        ))

        top = WIN_H - 70
        # 第 1 行：辅助功能（必需）
        y1 = top - ROW_H
        view.addSubview_(_label("辅助功能　必需", NSMakeRect(24, y1 + 52, 300, 20), 14.0, bold=True))
        view.addSubview_(_label(
            "读取 Kiro 等应用的界面，并点击确认弹窗里的 Accept / Allow 按钮。",
            NSMakeRect(24, y1 + 12, 330, 38), 12.0, color=NSColor.secondaryLabelColor(),
        ))
        self.ax_status = _label("", NSMakeRect(360, y1 + 52, 156, 20), 12.0, bold=True)
        view.addSubview_(self.ax_status)
        self.ax_button = _button("去授权…", NSMakeRect(392, y1 + 16, 124, 30), self, "grantAccessibility:")
        view.addSubview_(self.ax_button)

        # 第 2 行：登录项（可选）
        y2 = y1 - ROW_H
        view.addSubview_(_label("登录项（开机自启）　可选", NSMakeRect(24, y2 + 52, 300, 20), 14.0, bold=True))
        view.addSubview_(_label(
            "登录 Mac 后自动启动 AutoConfirm。启动后仍是暂停状态，不会自己开始点击。",
            NSMakeRect(24, y2 + 12, 330, 38), 12.0, color=NSColor.secondaryLabelColor(),
        ))
        self.login_status = _label("", NSMakeRect(360, y2 + 52, 156, 20), 12.0, bold=True)
        view.addSubview_(self.login_status)
        self.login_button = _button("开启", NSMakeRect(392, y2 + 16, 124, 30), self, "handleLogin:")
        view.addSubview_(self.login_button)

        # 底部
        self.footer = _label("", NSMakeRect(24, 26, 340, 34), 12.0, color=NSColor.secondaryLabelColor())
        view.addSubview_(self.footer)
        self.done_button = _button("完成", NSMakeRect(WIN_W - 24 - 124, 24, 124, 32), self, "done:")
        self.done_button.setKeyEquivalent_("\r")
        view.addSubview_(self.done_button)

    # -- 显示 ------------------------------------------------------------- #

    @objc.python_method
    def show(self) -> None:
        self.refresh_(None)
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        self.window.makeKeyAndOrderFront_(None)

    # -- 按钮 ------------------------------------------------------------- #

    def grantAccessibility_(self, _sender) -> None:
        request_accessibility()

    def handleLogin_(self, _sender) -> None:
        status = login_item_status(self.agent_path)
        if status == "needs_approval":
            subprocess.Popen(["/usr/bin/open", LOGIN_ITEMS_PANE])
        else:
            self.toggle_login()
        self.refresh_(None)

    def done_(self, _sender) -> None:
        if accessibility_granted():
            self.window.orderOut_(None)

    # -- 刷新 ------------------------------------------------------------- #

    def refresh_(self, _timer) -> None:
        green = NSColor.systemGreenColor()
        orange = NSColor.systemOrangeColor()
        gray = NSColor.secondaryLabelColor()

        ax = accessibility_granted()
        self.ax_status.setStringValue_("✓ 已授权" if ax else "✗ 未授权")
        self.ax_status.setTextColor_(green if ax else orange)
        self.ax_button.setTitle_("已完成" if ax else "去授权…")
        self.ax_button.setEnabled_(not ax)

        login = login_item_status(self.agent_path)
        text, color, button = {
            "enabled": ("✓ 已开启", green, "关闭"),
            "needs_approval": ("待批准", orange, "去批准…"),
            "off": ("未开启", gray, "开启"),
        }[login]
        self.login_status.setStringValue_(text)
        self.login_status.setTextColor_(color)
        self.login_button.setTitle_(button)

        if ax:
            self.footer.setStringValue_("必需权限已就绪，可以在菜单栏 AC 图标里启用自动确认。")
        else:
            self.footer.setStringValue_(
                "点「去授权…」后，在系统设置里打开 AutoConfirm 的开关。"
                "授权后这里会自动更新。"
            )
        self.done_button.setEnabled_(ax)

        if ax and not self._was_ready:
            self._was_ready = True
            try:
                self.on_ready()
            except Exception:
                pass
        elif not ax:
            self._was_ready = False
