#!/bin/bash
# build_app.sh —— 安装成一个 macOS 应用（菜单栏常驻）。
#
# 目录分工：
#   ~/Library/Application Support/AutoConfirm/   运行时（代码 + venv + 配置）
#   /Applications/AutoConfirm.app                启动器（图标、Info.plist）
#
# 为什么运行时不放在项目目录：打包后的 app 以自己的身份运行，macOS 不允许它
# 读取 ~/Desktop、~/Documents 这类受保护目录。如果 app 去调 Desktop 下的
# .venv，启动时会因为权限被拒直接崩溃（实测就是这样）。
# Application Support 不受保护，所以运行时放那里。
#
# 为什么不把解释器复制进 bundle：macOS 的 framework 版 Python 会把自己
# 重新 exec 到 Resources/Python.app，脱离原始目录结构就找不到，很脆。
# 让 app 去调一个放在正常位置的 venv 更可靠。
#
# 用法： ./build_app.sh
# 产物： /Applications/AutoConfirm.app

set -euo pipefail
cd "$(dirname "$0")"
PROJECT_DIR="$(pwd)"

APP_NAME="AutoConfirm"
BUNDLE_ID="local.autoconfirm.app"
LSREGISTER="/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
SUPPORT="$HOME/Library/Application Support/AutoConfirm"
CODE_FILES=(auto_confirm.py menubar_app.py permissions.py ax_dump.py cdp_confirm.py migrate_config.py)

if [ ! -x "$PROJECT_DIR/.venv/bin/python3" ]; then
  echo "找不到项目的虚拟环境，请先执行："
  echo "  python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt"
  exit 1
fi

if [ -w /Applications ]; then
  INSTALL_DIR="/Applications"
else
  INSTALL_DIR="$HOME/Applications"
  mkdir -p "$INSTALL_DIR"
  echo "注意：/Applications 不可写，改装到 $INSTALL_DIR"
fi
APP="$INSTALL_DIR/$APP_NAME.app"

# --------------------------------------------------------------------------- #
# 1. 运行时：代码
# --------------------------------------------------------------------------- #
echo "==> 同步代码到 $SUPPORT"
mkdir -p "$SUPPORT"
cp "${CODE_FILES[@]}" "$SUPPORT/"

# --------------------------------------------------------------------------- #
# 2. 运行时：虚拟环境
# --------------------------------------------------------------------------- #
if [ -x "$SUPPORT/venv/bin/python3" ] && \
   "$SUPPORT/venv/bin/python3" -c "import objc" >/dev/null 2>&1; then
  echo "==> 复用已有的运行时环境"
else
  echo "==> 创建运行时环境（不联网，直接复制项目里已装好的依赖）"
  rm -rf "$SUPPORT/venv"
  python3 -m venv "$SUPPORT/venv"

  SRC_SP="$("$PROJECT_DIR/.venv/bin/python3" -c \
      "import sysconfig; print(sysconfig.get_paths()['purelib'])")"
  DST_SP="$("$SUPPORT/venv/bin/python3" -c \
      "import sysconfig; print(sysconfig.get_paths()['purelib'])")"
  mkdir -p "$DST_SP"
  (cd "$SRC_SP" && tar cf - --exclude='__pycache__' --exclude='*.pyc' .) \
    | (cd "$DST_SP" && tar xf -)

  if ! "$SUPPORT/venv/bin/python3" -c "import objc, AppKit" >/dev/null 2>&1; then
    echo "运行时环境里的 pyobjc 不可用，构建中止"
    exit 1
  fi
  echo "    OK（$(du -sh "$SUPPORT/venv" | cut -f1)）"
fi

# --------------------------------------------------------------------------- #
# 3. 运行时：配置（已存在就不覆盖，保留你调好的规则）
# --------------------------------------------------------------------------- #
if [ -f "$SUPPORT/config.json" ]; then
  echo "==> 保留已有配置 $SUPPORT/config.json"
  # 旧的单目标格式自动升级成多目标格式，原文件留 .bak 备份
  "$PROJECT_DIR/.venv/bin/python3" migrate_config.py "$SUPPORT/config.json" \
    2>&1 | sed 's/^/    /'
else
  cp config.json "$SUPPORT/config.json"
  echo "==> 已创建配置 $SUPPORT/config.json"
fi

# --------------------------------------------------------------------------- #
# 4. app bundle
# --------------------------------------------------------------------------- #
# macOS 把辅助功能权限记在 AutoConfirm.app 的签名上（它是 Python 子进程的
# 「责任进程」）。ad-hoc 签名每重建一次就变，系统会把旧授权当作作废。
# Python 代码都在 Application Support 里，改它们根本不需要动 app 本体，
# 所以只有宿主源码 / 图标 / Info.plist 真变了才重建，授权就能一直保留。
# 需要强制重建时：./build_app.sh --force-bundle
BUNDLE_REV="3"   # Info.plist 模板等打包内容变化时手动 +1
BUNDLE_STAMP="$(cat "$PROJECT_DIR/dock_host.m" "$PROJECT_DIR/make_icon.py" \
  | shasum | cut -d' ' -f1)-$BUNDLE_REV-$BUNDLE_ID"
STAMP_FILE="$SUPPORT/.bundle_stamp"

REBUILD_BUNDLE=1
if [ "${1:-}" != "--force-bundle" ] && [ -x "$APP/Contents/MacOS/$APP_NAME" ] \
   && [ "$(cat "$STAMP_FILE" 2>/dev/null)" = "$BUNDLE_STAMP" ]; then
  REBUILD_BUNDLE=0
  echo "==> app 本体无变化，保留现有 ${APP}（辅助功能授权不受影响）"
fi

if [ "$REBUILD_BUNDLE" = 1 ]; then
echo "==> 组装 $APP"
echo "    注意：app 本体重建后，需要在辅助功能里重新授权 AutoConfirm"
# 先把旧记录从 LaunchServices 里注销。同一个 bundle id 如果残留了指向已删除
# 路径的记录，系统会解析到那个死路径，表现就是双击完全没反应。
if [ -x "$LSREGISTER" ] && [ -d "$APP" ]; then
  "$LSREGISTER" -u "$APP" >/dev/null 2>&1 || true
fi
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

# 外层必须是原生 Cocoa 可执行文件：它拥有 AutoConfirm.app 的真实 bundle 身份，
# 因此会稳定显示在 Dock；随后它会启动独立的 Python 菜单栏代理。
echo "==> 编译原生 Dock 宿主"
clang -fobjc-arc -fblocks -framework Cocoa "$PROJECT_DIR/dock_host.m" \
  -o "$APP/Contents/MacOS/$APP_NAME"
chmod +x "$APP/Contents/MacOS/$APP_NAME"

cat > "$APP/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>$APP_NAME</string>
  <key>CFBundleDisplayName</key><string>$APP_NAME</string>
  <key>CFBundleExecutable</key><string>$APP_NAME</string>
  <key>CFBundleIdentifier</key><string>$BUNDLE_ID</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>NSHighResolutionCapable</key><true/>
  <!-- 普通应用：在 Dock 显示运行中的 AutoConfirm。 -->
  <key>LSUIElement</key><false/>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
</dict>
</plist>
EOF

# --------------------------------------------------------------------------- #
# 5. 图标
# --------------------------------------------------------------------------- #
echo "==> 生成图标"
TMPD="$(mktemp -d)"
ICONSET="$TMPD/AppIcon.iconset"
mkdir -p "$ICONSET"
if "$PROJECT_DIR/.venv/bin/python3" make_icon.py "$ICONSET/icon_1024.png" >/dev/null 2>&1; then
  for spec in "16 16x16" "32 16x16@2x" "32 32x32" "64 32x32@2x" \
              "128 128x128" "256 128x128@2x" "256 256x256" "512 256x256@2x" \
              "512 512x512" "1024 512x512@2x"; do
    px="${spec%% *}"; name="${spec##* }"
    sips -z "$px" "$px" "$ICONSET/icon_1024.png" \
         --out "$ICONSET/icon_$name.png" >/dev/null 2>&1
  done
  rm -f "$ICONSET/icon_1024.png"
  if iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/AppIcon.icns" 2>/dev/null; then
    echo "    图标 OK"
  else
    echo "    图标打包失败，用系统默认图标（不影响功能）"
  fi
else
  echo "    图标绘制失败，用系统默认图标（不影响功能）"
fi
rm -rf "$TMPD"

# --------------------------------------------------------------------------- #
# 6. ad-hoc 签名，让系统能稳定认出这个 app
# --------------------------------------------------------------------------- #
echo "==> ad-hoc 签名"
codesign --force --deep --sign - "$APP" 2>&1 | sed 's/^/    /' || \
  echo "    签名失败，app 仍可运行，但系统可能反复要求重新授权"

echo "==> 重新注册到 LaunchServices"
[ -x "$LSREGISTER" ] && "$LSREGISTER" -f "$APP" >/dev/null 2>&1 || true

echo "$BUNDLE_STAMP" > "$STAMP_FILE"
fi  # REBUILD_BUNDLE

echo ""
echo "完成：$APP"
echo ""
echo "下一步："
echo "  1) open -a $APP_NAME"
echo "  2) 辅助功能权限要授给 AutoConfirm（不是 Python），授权后再启动一次"
echo "  3) 开机自启：系统设置 → 通用 → 登录项，加入 $APP_NAME"
echo ""
echo "配置文件（命令行版和 app 共用这一份）："
echo "  $SUPPORT/config.json"
