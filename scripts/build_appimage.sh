#!/usr/bin/env bash
# Build a portable AppImage from the Nuitka standalone Linux build.
#
# The Nuitka --standalone output already bundles Python, PySide6 and all Qt
# shared libraries, so an AppImage is produced by assembling a standard AppDir
# (usr/ layout) and running `appimagetool`.  When launched, the app stores all
# user data/config in ~/.zapzen (see bootstrap._is_appimage_runtime), keeping
# the image itself read-only.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON="${PYTHON:-python3}"
DIST_DIR="${DIST_DIR:-dist_nuitka/main.dist}"
OUTPUT_DIR="${OUTPUT_DIR:-dist_linux_packages}"
VERSION="${VERSION:-}"

if [[ -z "$VERSION" ]]; then
  VERSION="$("$PYTHON" -c "import sys; sys.path.insert(0,'src'); from zapret_zen import __version__; print(__version__)")"
fi
VERSION="${VERSION#v}"

if [[ ! -f "$DIST_DIR/zapret_zen" ]]; then
  echo "ERROR: Nuitka binary not found at $DIST_DIR/zapret_zen. Run scripts/build_nuitka.sh first." >&2
  exit 1
fi

FILE_OUTPUT="$(file -b "$DIST_DIR/zapret_zen")"
if [[ "$FILE_OUTPUT" == *"aarch64"* ]]; then
  ARCH="aarch64"
elif [[ "$FILE_OUTPUT" == *"x86-64"* ]]; then
  ARCH="x86_64"
else
  echo "ERROR: unsupported binary architecture: $FILE_OUTPUT" >&2
  exit 1
fi

TOOLS_DIR="$ROOT/.appimage_tools"
APPIMAGETOOL="$TOOLS_DIR/appimagetool"
rm -rf "$TOOLS_DIR"
mkdir -p "$TOOLS_DIR"

# Preferred toolchain: linuxdeploy (assemble from existing files) + appimagetool.
if ! command -v appimagetool >/dev/null 2>&1; then
  echo "Downloading appimagetool..."
  if [[ "$ARCH" == "aarch64" ]]; then
    curl -L -o "$APPIMAGETOOL" "https://github.com/AppImage/AppImageKit/releases/download/continuous/appimagetool-aarch64.AppImage"
  else
    curl -L -o "$APPIMAGETOOL" "https://github.com/AppImage/AppImageKit/releases/download/continuous/appimagetool-x86_64.AppImage"
  fi
  chmod +x "$APPIMAGETOOL"
fi
if [[ -x "$APPIMAGETOOL" ]]; then
  APPIMAGETOOL_BIN="$APPIMAGETOOL"
else
  APPIMAGETOOL_BIN="$(command -v appimagetool)"
fi

STAGE="$OUTPUT_DIR/.appimage_stage"
rm -rf "$STAGE"
mkdir -p "$STAGE/usr/lib/zapret-zen"
mkdir -p "$STAGE/usr/share/applications"
mkdir -p "$STAGE/usr/share/icons/hicolor/256x256/apps"
mkdir -p "$STAGE/usr/bin"

# Copy the whole standalone dist (binary + bundled python + Qt) as the payload.
cp -r "$DIST_DIR"/. "$STAGE/usr/lib/zapret-zen/"
chmod +x "$STAGE/usr/lib/zapret-zen/zapret_zen"

cat > "$STAGE/usr/bin/zapret-zen" <<'EOS'
#!/usr/bin/env bash
exec "/usr/lib/zapret-zen/zapret_zen" "$@"
EOS
chmod +x "$STAGE/usr/bin/zapret-zen"

cat > "$STAGE/AppRun" <<EOF
#!/usr/bin/env bash
# Locate the payload relative to this AppImage mount (APPDIR).
HERE="\$(dirname "\$(readlink -f "\$0")")"
export LD_LIBRARY_PATH="\$HERE/usr/lib/zapret-zen:\$HERE/usr/lib:\${LD_LIBRARY_PATH:-}"
exec "\$HERE/usr/lib/zapret-zen/zapret_zen" "\$@"
EOF
chmod +x "$STAGE/AppRun"

cat > "$STAGE/zapret-zen.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=Zapret-Zen
Name[ru]=Запрет-Зен
GenericName=DPI bypass manager
GenericName[ru]=Менеджер обхода DPI
Comment=Desktop hub for managing DPI-bypass utilities
Comment[ru]=Центр управления утилитами обхода блокировок
Exec=zapret-zen
Icon=zapret-zen
Terminal=false
Categories=Network;Utility;
StartupNotify=true
EOF

cp "$STAGE/zapret-zen.desktop" "$STAGE/usr/share/applications/zapret-zen.desktop"
cp "$ROOT/ui_assets/icons/app.png" "$STAGE/usr/share/icons/hicolor/256x256/apps/zapret-zen.png"
# Icon also at AppDir root for the AppImage runtime.
cp "$ROOT/ui_assets/icons/app.png" "$STAGE/.DirIcon"
ln -sf "usr/share/icons/hicolor/256x256/apps/zapret-zen.png" "$STAGE/zapret-zen.png"

ARCH_ENV="ARCH=$ARCH"
APP_NAME="Zapret-Zen"
OUT="$OUTPUT_DIR/zapret_zen_${VERSION}_${ARCH}.AppImage"
echo "Bundling AppImage with $APPIMAGETOOL_BIN ..."
ARCH="$ARCH" "$APPIMAGETOOL_BIN" --appimage-extract-and-run "$STAGE" "$OUT" 2>/dev/null \
  || ARCH="$ARCH" "$APPIMAGETOOL_BIN" "$STAGE" "$OUT"
chmod +x "$OUT" 2>/dev/null || true

rm -rf "$TOOLS_DIR" "$STAGE"
if [[ -f "$OUT" ]]; then
  echo "Created $OUT"
else
  echo "ERROR: AppImage was not produced." >&2
  exit 1
fi
