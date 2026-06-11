from pathlib import Path
import sys
import json
import shutil
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from zapret_zen.ui.theme import _normalize_theme


def main():
    themes_dir = Path("themes")
    if not themes_dir.exists():
        print("themes/ not found")
        return
    updated = []
    for p in sorted(themes_dir.glob("*.json")):
        try:
            text = p.read_text(encoding="utf-8")
            data = json.loads(text)
            css = str(data.get("stylesheet", "") or "")
            is_light = bool(data.get("is_light", False))
            norm = _normalize_theme(css, is_light)
            if norm.strip() != css.strip():
                # backup
                bak = p.with_suffix(p.suffix + f".bak.{int(time.time())}")
                shutil.copy2(p, bak)
                data["stylesheet"] = norm
                p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
                updated.append(p.name)
                print("Updated:", p.name)
        except Exception as e:
            print("Error processing", p.name, e)
    if not updated:
        print("No files needed normalization.")

if __name__ == '__main__':
    main()
