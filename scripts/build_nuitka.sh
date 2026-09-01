#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON="${PYTHON:-$ROOT/.venv/bin/python}"
OUTPUT_DIR="${OUTPUT_DIR:-dist_nuitka}"
VERSION="${VERSION:-}"

if [[ ! -x "$PYTHON" ]]; then
  PYTHON="${PYTHON:-python3}"
fi

if [[ -z "$VERSION" ]]; then
  VERSION="$("$PYTHON" -c "import sys; sys.path.insert(0,'src'); from zapret_zen import __version__; print(__version__)")"
else
  INIT_PY="$ROOT/src/zapret_zen/__init__.py"
  sed -i -E "s/(__version__\s*=\s*\")[^\"]*(.*)/\1${VERSION}\2/" "$INIT_PY"
  echo "Injected version $VERSION into $INIT_PY"
fi

NUITKA_VERSION="$("$PYTHON" -c "
import re, sys
v = sys.argv[1].strip()
m = re.search(r'^(\d+(?:\.\d+)*)', v)
parts = tuple(int(x) for x in (m.group(1) if m else '0').split('.')[:4])
print('.'.join(str(p) for p in parts))
" "$VERSION")"

"$PYTHON" scripts/sync_app_icon.py

STAGING_ROOT="$ROOT/.nuitka_staging"
RUNTIME_STAGE="$STAGING_ROOT/runtime"
rm -rf "$STAGING_ROOT"
mkdir -p "$RUNTIME_STAGE"

EXCLUDE_DIRS=(".git" ".github" "__pycache__" ".mypy_cache" ".pytest_cache")
EXCLUDE_PATTERNS=("*.pyc" "*.pyo")

for item in "$ROOT"/runtime/* "$ROOT"/runtime/.[^.]*; do
  [[ -e "$item" ]] || continue
  name="$(basename "$item")"
  for excl in "${EXCLUDE_DIRS[@]}"; do
    [[ "$name" == "$excl" ]] && continue 2
  done
  cp -r "$item" "$RUNTIME_STAGE/"
done

find "$RUNTIME_STAGE" -type d \( -name .git -o -name .github -o -name __pycache__ -o -name .mypy_cache -o -name .pytest_cache \) -prune -exec rm -rf {} + 2>/dev/null || true
find "$RUNTIME_STAGE" -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete 2>/dev/null || true

NUITKA_ARGS=(
  -m nuitka
  --standalone
  --assume-yes-for-downloads
  --no-deployment-flag=self-execution
  --enable-plugin=pyside6
  --file-version="$NUITKA_VERSION"
  --output-dir="$OUTPUT_DIR"
  --output-filename=zapret_zen
  --include-data-dir=sample_data=sample_data
  --include-data-dir=ui_assets=ui_assets
  --include-data-dir=src/zapret_zen/translations=_internal/zapret_zen/translations
  --include-package=cryptography
  --include-package=certifi
  --include-package-data=certifi
  --nofollow-import-to=tkinter
  --remove-output
  src/zapret_zen/main.py
)

set -x
"$PYTHON" "${NUITKA_ARGS[@]}"
set +x

DIST_DIR="$(find "$OUTPUT_DIR" -maxdepth 2 -type d -name '*.dist' | head -n1)"
if [[ -z "$DIST_DIR" ]]; then
  echo "ERROR: Nuitka output directory (*.dist) not found in $OUTPUT_DIR" >&2
  exit 1
fi

RUNTIME_TARGET="$DIST_DIR/runtime"
rm -rf "$RUNTIME_TARGET"
cp -r "$RUNTIME_STAGE" "$RUNTIME_TARGET"
chmod -R u+rwX "$RUNTIME_TARGET"

rm -rf "$STAGING_ROOT"

echo "Built Linux standalone distribution: $DIST_DIR"