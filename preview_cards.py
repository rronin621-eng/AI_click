#!/usr/bin/env python3
"""
preview_cards.py —— 把菜单栏的状态卡片渲染成一张 PNG。

用途：不点开菜单也能看到当前各目标的状态，也方便把界面效果贴给别人看。
只读状态，不启动扫描线程、不点任何按钮，随时可以跑。

点击次数是从日志文件统计的，和 app 内部计数同源。

用法：
    python3 preview_cards.py                 # 输出到 preview_cards.png
    python3 preview_cards.py /tmp/out.png    # 指定输出路径
"""

from __future__ import annotations

import os
import re
import sys

from AppKit import (
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSBezierPath,
    NSColor,
    NSView,
)
from ApplicationServices import AXIsProcessTrusted
from Foundation import NSMakeRect

try:
    from AppKit import NSBitmapImageFileTypePNG as PNG_TYPE
except ImportError:  # 老系统的常量名
    from AppKit import NSPNGFileType as PNG_TYPE

from auto_confirm import Config, DEFAULT_CONFIG_PATH, find_target_app
from menubar_app import CARD_HEIGHT, CARD_WIDTH, OVERVIEW_HEIGHT, make_card

CLICK_LINE = re.compile(r"^(\S+ \S+) \[info\] \[([^\]]+)\] 已点击 '([^']*)'")


class Canvas(NSView):
    """卡片的画布，铺一层窗口底色，免得 PNG 是透明的不好看。"""

    def isFlipped(self) -> bool:
        return True

    def drawRect_(self, _rect) -> None:
        NSColor.windowBackgroundColor().setFill()
        NSBezierPath.fillRect_(self.bounds())


def read_click_stats(log_path: str) -> tuple[dict, dict]:
    """从日志里数每个目标点了多少次、最近一次点的什么。"""
    counts: dict[str, int] = {}
    last: dict[str, str] = {}
    if not os.path.exists(log_path):
        return counts, last

    with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = CLICK_LINE.match(line)
            if not m:
                continue
            timestamp, label, button = m.groups()
            counts[label] = counts.get(label, 0) + 1
            last[label] = f"{button} @ {timestamp.split()[1]}"
    return counts, last


def build_payloads(cfg: Config, trusted: bool) -> tuple[list[dict], int, int]:
    counts, last = read_click_stats(os.path.expanduser(cfg.log_file))
    master_paused = os.path.exists(cfg.master_switch)

    payloads: list[dict] = []
    active = 0
    total = 0

    for t in cfg.targets:
        running = find_target_app(t) is not None
        paused = master_paused or os.path.exists(t.off_switch)

        if not t.enabled:
            state, state_text = "off", "配置已禁用"
        elif not trusted:
            state, state_text = "warn", "缺权限"
        elif paused:
            state, state_text = "off", "已暂停"
        else:
            state, state_text = "on", "运行中"
            active += 1

        n = counts.get(t.label, 0)
        total += n
        payloads.append({
            "title": t.label,
            "state": state,
            "state_text": state_text,
            "line2": f"目标{'已锁定' if running else '未运行'}　累计 {n} 次",
            "line3": "最近　" + last.get(t.label, "还没点过"),
            "compact": False,
            "h": CARD_HEIGHT,
        })

    return payloads, active, total


def render(out_path: str) -> int:
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)

    cfg = Config.load(DEFAULT_CONFIG_PATH)
    trusted = bool(AXIsProcessTrusted())

    payloads, active, total = build_payloads(cfg, trusted)

    if active:
        ov_state, ov_text = "on", f"{active} 个运行中"
    elif not trusted:
        ov_state, ov_text = "warn", "缺少辅助功能权限"
    else:
        ov_state, ov_text = "off", "全部暂停"

    overview = {
        "title": "AutoConfirm",
        "state": ov_state,
        "state_text": ov_text,
        "line2": f"共 {len(cfg.targets)} 个目标 · 累计点击 {total} 次",
        "line3": "",
        "compact": True,
        "h": OVERVIEW_HEIGHT,
    }

    cards = [overview] + payloads
    height = sum(c["h"] for c in cards) + 16.0
    canvas = Canvas.alloc().initWithFrame_(NSMakeRect(0, 0, CARD_WIDTH, height))

    y = 8.0
    for spec in cards:
        card = make_card(spec["h"])
        card.setFrame_(NSMakeRect(0, y, CARD_WIDTH, spec["h"]))
        card.setPayload_({k: v for k, v in spec.items() if k != "h"})
        canvas.addSubview_(card)
        y += spec["h"]

    rep = canvas.bitmapImageRepForCachingDisplayInRect_(canvas.bounds())
    canvas.cacheDisplayInRect_toBitmapImageRep_(canvas.bounds(), rep)
    data = rep.representationUsingType_properties_(PNG_TYPE, {})

    if not data.writeToFile_atomically_(out_path, True):
        sys.stderr.write(f"写出失败: {out_path}\n")
        return 1

    print(f"已生成: {out_path}")
    print(
        f"辅助功能权限={trusted}  运行中={active}/{len(cfg.targets)}  "
        f"累计点击={total}"
    )
    return 0


if __name__ == "__main__":
    target = (
        sys.argv[1]
        if len(sys.argv) > 1
        else os.path.join(os.path.dirname(os.path.abspath(__file__)), "preview_cards.png")
    )
    raise SystemExit(render(target))
