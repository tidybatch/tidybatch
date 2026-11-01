"""Check tidybatch.taken_date against ExifTool on ExifTool's own sample images.

Only EXIF dates are compared (DateTimeOriginal, then CreateDate, then IFD0
ModifyDate), because dates stored only in XMP/IPTC are out of scope by design.
Runs in CI; to run it locally:

    git clone --depth 1 https://github.com/exiftool/exiftool /tmp/exiftool
    python3 dev/verify_exif.py /tmp/exiftool
"""
from __future__ import annotations

import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tidybatch  # noqa: E402

FORMATS = {".jpg", ".tif", ".heic", ".cr2", ".cr3", ".nef", ".dng", ".rw2", ".raf"}


def main() -> int:
    exiftool = Path(sys.argv[1] if len(sys.argv) > 1 else "exiftool")
    files = sorted(str(p) for p in (exiftool / "t" / "images").iterdir() if p.suffix.lower() in FORMATS)
    out = subprocess.run(
        ["perl", str(exiftool / "exiftool"), "-j", "-q", "-d", "%Y-%m-%d %H:%M:%S",
         "-EXIF:DateTimeOriginal", "-EXIF:CreateDate", "-IFD0:ModifyDate", *files],
        capture_output=True, text=True,
    ).stdout
    tags = json.loads(out)
    if len(tags) != len(files):
        print(f"ExifTool returned {len(tags)} results for {len(files)} files")
        return 1

    diffs, dated = 0, Counter()
    for t in tags:
        path = Path(t["SourceFile"])
        want = t.get("DateTimeOriginal") or t.get("CreateDate") or t.get("ModifyDate")
        got = tidybatch.taken_date(path)
        got = got.strftime("%Y-%m-%d %H:%M:%S") if got else None
        if got != want:
            diffs += 1
            print(f"DIFF {path.name}: exiftool={want} tidybatch={got}")
        elif want:
            dated[path.suffix.lower().lstrip(".")] += 1

    print(f"{len(tags) - diffs}/{len(tags)} files match ExifTool "
          f"({sum(dated.values())} with a date: {', '.join(f'{k} {v}' for k, v in sorted(dated.items()))}; "
          f"the rest have no EXIF date for either tool)")
    return 1 if diffs else 0


if __name__ == "__main__":
    sys.exit(main())
