#!/usr/bin/env python3
"""
dock_host.py —— 打包版 AutoConfirm 的 Dock 宿主。

为什么需要它：
  macOS 对“普通 Dock 应用”和“菜单栏代理应用”的生命周期处理不同。
  让同一个 framework Python 进程既承担 Dock 身份又承担 status item，
  在这个打包方式下会导致 status item 偶发不渲染。

结构：
  AutoConfirm.app → 本宿主（NSApplication Regular，负责 Dock）
                  → menubar_app.py 子进程（NSApplication Accessory，负责顶部 AC）

菜单栏子进程退出时，Dock 宿主随之退出；用户从 Dock 退出宿主时，finally
会终止菜单栏子进程，因此二者生命周期始终一致。
"""

from __future__ import annotations

import os
import subprocess
import sys

from AppKit import NSApplication, NSApplicationActivationPolicyRegular
from Foundation import NSTimer


def main() -> int:
    support = os.path.dirname(os.path.abspath(__file__))
    menu_main = os.path.join(support, "menubar_app.py")

    env = os.environ.copy()
    env["AUTOCONFIRM_MENU_AGENT"] = "1"

    # 启动菜单栏代理。它自身有单实例锁，重复启动时会自行无害退出。
    child = subprocess.Popen([sys.executable, menu_main], env=env)

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyRegular)

    def monitor(_timer) -> None:
        if child.poll() is not None:
            _timer.invalidate()
            app.terminate_(None)

    NSTimer.scheduledTimerWithTimeInterval_repeats_block_(
        0.5, True, monitor
    )

    try:
        app.run()
    finally:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                child.kill()

    return child.returncode or 0


if __name__ == "__main__":
    raise SystemExit(main())
