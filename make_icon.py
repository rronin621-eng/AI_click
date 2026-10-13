#!/usr/bin/env python3
"""
make_icon.py —— 画一张 1024x1024 的应用图标 PNG。

只负责画最大尺寸，缩放和打包成 .icns 由 build_app.sh 用 sips / iconutil 完成。
用法：python3 make_icon.py 输出路径.png
"""

from __future__ import annotations

import sys

from AppKit import (
    NSAttributedString,
    NSBezierPath,
    NSBitmapImageRep,
    NSColor,
    NSDeviceRGBColorSpace,
    NSFont,
    NSFontAttributeName,
    NSForegroundColorAttributeName,
    NSGradient,
    NSGraphicsContext,
)
from Foundation import NSMakePoint, NSMakeRect

try:
    from AppKit import NSBitmapImageFileTypePNG as PNG_TYPE
except ImportError:  # 老系统的常量名
    from AppKit import NSPNGFileType as PNG_TYPE

SIZE = 1024


def draw(path: str) -> None:
    rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, SIZE, SIZE, 8, 4, True, False, NSDeviceRGBColorSpace, 0, 0
    )
    ctx = NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)

    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.setCurrentContext_(ctx)

    # macOS 图标惯例：四周留白，主体是个圆角方形
    inset = SIZE * 0.08
    radius = SIZE * 0.225
    body = NSMakeRect(inset, inset, SIZE - inset * 2, SIZE - inset * 2)
    shape = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
        body, radius, radius
    )

    # 紫色渐变，和 Kiro 的配色呼应
    top = NSColor.colorWithSRGBRed_green_blue_alpha_(0.44, 0.33, 0.85, 1.0)
    bottom = NSColor.colorWithSRGBRed_green_blue_alpha_(0.24, 0.16, 0.51, 1.0)
    NSGradient.alloc().initWithStartingColor_endingColor_(top, bottom).drawInBezierPath_angle_(
        shape, 270.0
    )

    # 中间一个对勾，表示"确认"
    glyph = "✓"
    font = NSFont.systemFontOfSize_weight_(SIZE * 0.62, 1.0)
    attrs = {
        NSFontAttributeName: font,
        NSForegroundColorAttributeName: NSColor.whiteColor(),
    }
    text = NSAttributedString.alloc().initWithString_attributes_(glyph, attrs)
    tsize = text.size()
    text.drawAtPoint_(
        NSMakePoint((SIZE - tsize.width) / 2.0, (SIZE - tsize.height) / 2.0)
    )

    NSGraphicsContext.restoreGraphicsState()

    data = rep.representationUsingType_properties_(PNG_TYPE, {})
    if not data.writeToFile_atomically_(path, True):
        raise SystemExit(f"写入失败: {path}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("用法: make_icon.py 输出路径.png")
    draw(sys.argv[1])
    print(f"图标已生成: {sys.argv[1]}")
