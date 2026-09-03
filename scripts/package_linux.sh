#!/usr/bin/env bash
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

for tool in file dpkg-deb; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "ERROR: required tool '$tool' not found." >&2
    exit 1
  fi
done

FILE_OUTPUT="$(file -b "$DIST_DIR/zapret_zen")"
if [[ "$FILE_OUTPUT" == *"aarch64"* ]]; then
  ARCH="aarch64"
  DEB_ARCH="arm64"
  RPM_ARCH="aarch64"
elif [[ "$FILE_OUTPUT" == *"x86-64"* ]]; then
  ARCH="x86_64"
  DEB_ARCH="amd64"
  RPM_ARCH="x86_64"
else
  echo "ERROR: unsupported binary architecture: $FILE_OUTPUT" >&2
  exit 1
fi

DEB_VERSION="${VERSION//_/.}"
DEB_VERSION="${DEB_VERSION//+/.}"
DEB_VERSION="${DEB_VERSION%%[^0-9a-zA-Z.+~-]*}"
[[ "$DEB_VERSION" =~ ^[0-9] ]] || DEB_VERSION="0$DEB_VERSION"

RPM_VERSION="${VERSION//-/_}"
RPM_VERSION="${RPM_VERSION//+/_}"
RPM_VERSION="$(echo "$RPM_VERSION" | tr -cd '[:alnum:]._')"
[[ "$RPM_VERSION" =~ ^[0-9] ]] || RPM_VERSION="0$RPM_VERSION"

NAME="zapret_zen_${VERSION}_linux_${ARCH}"
STAGE="$OUTPUT_DIR/.stage"
PORTABLE_DIR="$OUTPUT_DIR/$NAME"
rm -rf "$STAGE" "$PORTABLE_DIR"
mkdir -p "$STAGE" "$PORTABLE_DIR"

cp -r "$DIST_DIR"/* "$PORTABLE_DIR"/
chmod +x "$PORTABLE_DIR/zapret_zen"
cp "$ROOT/assets/zapret-zen.desktop" "$PORTABLE_DIR/"

cr_install_sh() {
  cat > "$PORTABLE_DIR/install.sh" <<'EOS'
#!/usr/bin/env bash
set -euo pipefail

APP_NAME="zapret-zen"
APP_SOURCE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="${LIB_DIR:-$HOME/.local/lib/$APP_NAME}"
BIN_DIR="${BIN_DIR:-$HOME/.local/bin}"
APP_DIRS="$HOME/.local/share/applications"

mkdir -p "$LIB_DIR" "$BIN_DIR" "$APP_DIRS"
cp -r "$APP_SOURCE"/. "$LIB_DIR"/
chmod +x "$LIB_DIR/zapret_zen"
ln -sf "$LIB_DIR/zapret_zen" "$BIN_DIR/zapret-zen"
cp -f "$LIB_DIR/zapret-zen.desktop" "$APP_DIRS/zapret-zen.desktop"

ICON_BASE="$HOME/.local/share/icons/hicolor"
for size in 256; do
  target="$ICON_BASE/${size}x${size}/apps/zapret-zen.png"
  mkdir -p "$(dirname "$target")"
  cp -f "$LIB_DIR/ui_assets/icons/app.png" "$target"
done

echo "Zapret-Zen installed to $LIB_DIR"
echo "Run 'zapret-zen' from your terminal or from the application menu."
echo "Note: nfqws needs root (CAP_NET_ADMIN) to bypass DPI."
EOS
  chmod +x "$PORTABLE_DIR/install.sh"
}

cr_install_sh

tar -C "$OUTPUT_DIR" -czf "$OUTPUT_DIR/zapret_zen_${VERSION}_portable_linux_${ARCH}.tar.gz" "$NAME"
echo "Created $OUTPUT_DIR/zapret_zen_${VERSION}_portable_linux_${ARCH}.tar.gz"

# Arch-friendly tarball (.tar.xz) using a standard /usr layout.
ARCH_DIR="$OUTPUT_DIR/$NAME-usr"
rm -rf "$ARCH_DIR"
mkdir -p "$ARCH_DIR/usr/bin" "$ARCH_DIR/usr/share/applications" "$ARCH_DIR/usr/share/icons/hicolor/256x256/apps"
cp -r "$DIST_DIR" "$ARCH_DIR/usr/lib/zapret-zen"
chmod +x "$ARCH_DIR/usr/lib/zapret-zen/zapret_zen"
ln -s /usr/lib/zapret-zen/zapret_zen "$ARCH_DIR/usr/bin/zapret-zen"
cp "$ROOT/assets/zapret-zen.desktop" "$ARCH_DIR/usr/share/applications/zapret-zen.desktop"
cp "$ROOT/ui_assets/icons/app.png" "$ARCH_DIR/usr/share/icons/hicolor/256x256/apps/zapret-zen.png"
tar -C "$OUTPUT_DIR" -cJf "$OUTPUT_DIR/zapret_zen_${VERSION}_linux_${ARCH}.tar.xz" "$NAME-usr"
rm -rf "$ARCH_DIR"
echo "Created $OUTPUT_DIR/zapret_zen_${VERSION}_linux_${ARCH}.tar.xz"

DEB_ROOT="$OUTPUT_DIR/$NAME.deb"
rm -rf "$DEB_ROOT"
mkdir -p "$DEB_ROOT/DEBIAN"
mkdir -p "$DEB_ROOT/opt/zapret-zen"
mkdir -p "$DEB_ROOT/usr/bin"
mkdir -p "$DEB_ROOT/usr/share/applications"
mkdir -p "$DEB_ROOT/usr/share/icons/hicolor/256x256/apps"

cp -r "$DIST_DIR"/* "$DEB_ROOT/opt/zapret-zen/"
chmod +x "$DEB_ROOT/opt/zapret-zen/zapret_zen"
cp "$ROOT/assets/zapret-zen.desktop" "$DEB_ROOT/usr/share/applications/zapret-zen.desktop"
cp "$ROOT/ui_assets/icons/app.png" "$DEB_ROOT/usr/share/icons/hicolor/256x256/apps/zapret-zen.png"
ln -s /opt/zapret-zen/zapret_zen "$DEB_ROOT/usr/bin/zapret-zen"

INSTALLED_SIZE="$(du -sk "$DEB_ROOT/opt" | awk '{print $1}')"
cat > "$DEB_ROOT/DEBIAN/control" <<EOF
Package: zapret-zen
Version: $DEB_VERSION
Section: net
Priority: optional
Architecture: $DEB_ARCH
Installed-Size: $INSTALLED_SIZE
Maintainer: peshk0v
Depends: libc6 (>= 2.31), libgl1, libegl1, libxcb-cursor0
Description: Cross-platform desktop hub for managing DPI-bypass utilities
 Zapret-Zen is a desktop hub for managing network bypass utilities
 (zapret / zapret-rust, tg-ws-proxy). On Linux it drives the
 zapret-discord-youtube-rust engine via zapret-rust / nfqws.
EOF

dpkg-deb --root-owner-group --build "$DEB_ROOT" "$OUTPUT_DIR/zapret_zen_${DEB_VERSION}_${DEB_ARCH}.deb" >/dev/null
echo "Created $OUTPUT_DIR/zapret_zen_${DEB_VERSION}_${DEB_ARCH}.deb"

if command -v rpmbuild >/dev/null 2>&1; then
  RPMROOT="$OUTPUT_DIR/rpmbuild"
  SPEC="$RPMROOT/SPECS/zapret-zen.spec"
  mkdir -p "$RPMROOT"/{SPECS,SOURCES,BUILD,RPMS}
  cat > "$SPEC" <<EOF
Name: zapret-zen
Version: $RPM_VERSION
Release: 1
Summary: Cross-platform desktop hub for managing DPI-bypass utilities
License: MIT
URL: https://github.com/peshk0v/Zapret-Zen
BuildArch: $RPM_ARCH

%description
Zapret-Zen is a desktop hub for managing network bypass utilities
(zapret / zapret-rust, tg-ws-proxy). On Linux it drives the
zapret-discord-youtube-rust engine via zapret-rust / nfqws.

%install
rm -rf %{buildroot}
mkdir -p %{buildroot}/opt/zapret-zen
mkdir -p %{buildroot}/usr/bin
mkdir -p %{buildroot}/usr/share/applications
mkdir -p %{buildroot}/usr/share/icons/hicolor/256x256/apps
cp -r "$DIST_DIR"/* %{buildroot}/opt/zapret-zen/
chmod +x %{buildroot}/opt/zapret-zen/zapret_zen
cp "$ROOT/assets/zapret-zen.desktop" %{buildroot}/usr/share/applications/zapret-zen.desktop
cp "$ROOT/ui_assets/icons/app.png" %{buildroot}/usr/share/icons/hicolor/256x256/apps/zapret-zen.png
ln -s /opt/zapret-zen/zapret_zen %{buildroot}/usr/bin/zapret-zen

%files
/opt/zapret-zen
/usr/bin/zapret-zen
/usr/share/applications/zapret-zen.desktop
/usr/share/icons/hicolor/256x256/apps/zapret-zen.png
EOF
  rpmbuild --define "_topdir $RPMROOT" -bb "$SPEC" >/dev/null
  cp "$RPMROOT/RPMS/$RPM_ARCH"/zapret-zen-*.rpm "$OUTPUT_DIR/" 2>/dev/null || true
  echo "Created $OUTPUT_DIR/zapret-zen-${RPM_VERSION}-1.${RPM_ARCH}.rpm"
else
  echo "SKIP: rpmbuild not found; skipping .rpm packaging."
fi

echo "Building AppImage..."
DIST_DIR="$ROOT/$DIST_DIR" OUTPUT_DIR="$ROOT/$OUTPUT_DIR" VERSION="$VERSION" \
  "$ROOT/scripts/build_appimage.sh"

rm -rf "$STAGE" "$DEB_ROOT"
echo "Linux packages prepared in $OUTPUT_DIR"