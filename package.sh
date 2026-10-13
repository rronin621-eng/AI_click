#!/bin/bash
# package.sh —— 生成可以发给别人的安装包 dist/AutoConfirm-<版本>.dmg
#
# 和 build_app.sh 的区别：
#   build_app.sh  只给本机开发用，依赖本机的 Python 和 Application Support 里的代码
#   package.sh    完全自包含：Python 运行时、依赖、代码都打进 app，别人的 Mac 上什么都不用装
#
# 结构：
#   AutoConfirm.app                       原生 Cocoa 宿主（通用二进制），负责 Dock
#     Contents/Helpers/AutoConfirm Menu.app   PyInstaller 打的菜单栏组件（LSUIElement）
#
# 签名：只做 ad-hoc 签名，没有 Apple 公证。对方第一次打开需要在
#       「系统设置 → 隐私与安全性」里点「仍要打开」，安装说明里写了步骤。
#
# 用法： ./package.sh            产物在 dist/

set -euo pipefail
cd "$(dirname "$0")"
P="$(pwd)"

VERSION="1.0.0"
APP_NAME="AutoConfirm"
BUNDLE_ID="local.autoconfirm.app"
HELPER_NAME="AutoConfirm Menu"
HELPER_ID="local.autoconfirm.menu"

BUILD="$P/build"
DIST="$P/dist"
APP="$DIST/$APP_NAME.app"
DMG="$DIST/$APP_NAME-$VERSION.dmg"
PY="$P/.venv/bin/python3"

if ! "$PY" -c "import PyInstaller" 2>/dev/null; then
  echo "缺少 PyInstaller：.venv/bin/python -m pip install pyinstaller==6.22.3"
  exit 1
fi

rm -rf "$BUILD" "$DIST"
mkdir -p "$BUILD" "$DIST"

# --------------------------------------------------------------------------- #
# 1. 菜单栏组件：PyInstaller 打成自带运行时的通用 app
# --------------------------------------------------------------------------- #
echo "==> 构建菜单栏组件（PyInstaller，universal2）"
"$P/.venv/bin/pyinstaller" --noconfirm --clean --log-level WARN \
  --windowed --name "$HELPER_NAME" \
  --target-arch universal2 \
  --osx-bundle-identifier "$HELPER_ID" \
  --add-data "$P/config.json:." \
  --hidden-import permissions --hidden-import migrate_config \
  --hidden-import ServiceManagement --hidden-import Quartz \
  --distpath "$BUILD/dist" --workpath "$BUILD/work" --specpath "$BUILD" \
  "$P/menubar_app.py"

HELPER="$BUILD/dist/$HELPER_NAME.app"
HPLIST="$HELPER/Contents/Info.plist"
# 只驻留菜单栏，不单独出现在 Dock（Dock 图标由外层宿主负责）
/usr/libexec/PlistBuddy -c "Add :LSUIElement bool true" "$HPLIST" 2>/dev/null \
  || /usr/libexec/PlistBuddy -c "Set :LSUIElement true" "$HPLIST"
/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $VERSION" "$HPLIST"

# --------------------------------------------------------------------------- #
# 2. 外层 app
# --------------------------------------------------------------------------- #
echo "==> 组装 $APP_NAME.app"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources" "$APP/Contents/Helpers"

clang -fobjc-arc -fblocks -O2 -arch arm64 -arch x86_64 -mmacosx-version-min=11.0 \
  -framework Cocoa "$P/dock_host.m" -o "$APP/Contents/MacOS/$APP_NAME"

ditto "$HELPER" "$APP/Contents/Helpers/$HELPER_NAME.app"

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
  <key>CFBundleShortVersionString</key><string>$VERSION</string>
  <key>CFBundleVersion</key><string>$VERSION</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>LSUIElement</key><false/>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
</dict>
</plist>
EOF

echo "==> 生成图标"
ICONSET="$BUILD/AppIcon.iconset"
mkdir -p "$ICONSET"
"$PY" "$P/make_icon.py" "$ICONSET/icon_1024.png" >/dev/null
for spec in "16 16x16" "32 16x16@2x" "32 32x32" "64 32x32@2x" \
            "128 128x128" "256 128x128@2x" "256 256x256" "512 256x256@2x" \
            "512 512x512" "1024 512x512@2x"; do
  px="${spec%% *}"; name="${spec##* }"
  sips -z "$px" "$px" "$ICONSET/icon_1024.png" --out "$ICONSET/icon_$name.png" >/dev/null
done
rm -f "$ICONSET/icon_1024.png"
iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/AppIcon.icns"
cp "$APP/Contents/Resources/AppIcon.icns" \
   "$APP/Contents/Helpers/$HELPER_NAME.app/Contents/Resources/"

# --------------------------------------------------------------------------- #
# 3. 签名：先内后外。Apple 芯片上未签名的程序根本跑不起来，至少要 ad-hoc
# --------------------------------------------------------------------------- #
echo "==> ad-hoc 签名"
codesign --force --deep --sign - "$APP/Contents/Helpers/$HELPER_NAME.app"
codesign --force --sign - "$APP"
codesign --verify --deep --strict "$APP"
echo "    签名校验通过"

# --------------------------------------------------------------------------- #
# 4. DMG：app + 「应用程序」快捷方式 + 安装说明
# --------------------------------------------------------------------------- #
echo "==> 生成 DMG"
STAGE="$BUILD/dmg"
mkdir -p "$STAGE"
ditto "$APP" "$STAGE/$APP_NAME.app"
ln -s /Applications "$STAGE/应用程序"
cp "$P/安装说明.txt" "$STAGE/安装说明.txt"
hdiutil create -quiet -volname "$APP_NAME $VERSION" -srcfolder "$STAGE" \
  -fs HFS+ -format UDZO -ov "$DMG"

echo ""
echo "完成："
echo "  $DMG  ($(du -h "$DMG" | cut -f1))"
echo "  架构：$(lipo -archs "$APP/Contents/MacOS/$APP_NAME")"
