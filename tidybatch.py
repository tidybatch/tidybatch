#!/usr/bin/env python3
"""
TidyBatch Core - bulk rename files safely, with preview and one-command undo.

Safety model:
  * Every command is a DRY RUN unless you pass --apply.
  * Every applied run writes a journal to <folder>/.tidybatch/,
    so `tidybatch undo <folder>` puts every file back where it was.
  * Nothing is ever deleted or overwritten.
  * Name collisions are resolved with a numeric suffix, never by overwriting.

Standard library only (Python 3.9+). MIT licensed.

`organize` and `dedupe` are part of TidyBatch Pro: https://whop.com/tidybatch/bulk-file-renamer/
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import struct
import sys
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Iterator

__version__ = "1.1.0"

JOURNAL_DIR = ".tidybatch"
TMP_PREFIX = ".tidybatch-tmp-"
SKIP_DIRS = {JOURNAL_DIR, "_duplicates", ".git", "__pycache__"}

class TidyError(Exception):
    """A user-facing error: printed without a traceback."""


@dataclass(frozen=True)
class Move:
    src: Path
    dst: Path


# --------------------------------------------------------------------------- #
# File discovery
# --------------------------------------------------------------------------- #

def iter_files(root: Path, recursive: bool, glob: str | None, include_hidden: bool) -> Iterator[Path]:
    """Yield regular files under root, skipping TidyBatch's own folders."""
    if recursive:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and (include_hidden or not d.startswith("."))]
            for name in filenames:
                p = Path(dirpath) / name
                if _wanted(p, glob, include_hidden):
                    yield p
    else:
        for p in root.iterdir():
            if p.is_file() and _wanted(p, glob, include_hidden):
                yield p


def _wanted(p: Path, glob: str | None, include_hidden: bool) -> bool:
    if p.name.startswith(TMP_PREFIX) or p.is_symlink():
        return False
    if not include_hidden and p.name.startswith("."):
        return False
    return glob is None or p.match(glob)


def natural_key(p: Path) -> list:
    """Sort 'img2' before 'img10', the way humans expect."""
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", p.name.casefold())]


def sort_files(files: Iterable[Path], by: str) -> list[Path]:
    if by == "taken":
        return sorted(files, key=lambda p: (taken_or_mtime(p), natural_key(p)))
    if by == "date":
        return sorted(files, key=lambda p: (p.stat().st_mtime, natural_key(p)))
    if by == "size":
        return sorted(files, key=lambda p: (p.stat().st_size, natural_key(p)))
    return sorted(files, key=natural_key)


# --------------------------------------------------------------------------- #
# Photo dates (EXIF "date taken"), standard library only.
# JPEG, TIFF, TIFF-based RAW (DNG, CR2, NEF, ARW, ORF, RW2), Fuji RAF, Canon CR3
# and HEIC/HEIF/AVIF. Checked against ExifTool's sample images; see dev/.
# --------------------------------------------------------------------------- #

_TIFF_MAGIC = {42, 0x4F52, 0x5352, 0x55}  # TIFF, Olympus ORF (x2), Panasonic RW2
_DATE_TAGS = (0x9003, 0x9004, 0x0132)     # DateTimeOriginal, DateTimeDigitized, DateTime


def taken_date(path: Path) -> datetime | None:
    """EXIF DateTimeOriginal, else DateTimeDigitized, else IFD0 DateTime; None if absent."""
    try:
        with path.open("rb") as f:
            head = f.read(16)
            if head[:2] == b"\xff\xd8":
                base = _jpeg_tiff_offset(f, 0)
            elif head[:4] in (b"II*\x00", b"MM\x00*", b"IIRO", b"IIRS", b"IIU\x00"):
                base = 0
            elif head.startswith(b"FUJIFILMCCD-RAW"):
                f.seek(84)
                jpeg_at = struct.unpack(">I", f.read(4))[0]
                base = _jpeg_tiff_offset(f, jpeg_at)
            elif head[4:12] == b"ftypcrx ":
                return _cr3_date(f)
            elif head[4:8] == b"ftyp":
                base = _isobmff_tiff_offset(f)
            else:
                return None
            return _tiff_date(f, base) if base is not None else None
    except Exception:  # metadata is best-effort: a broken file must never stop a rename
        return None


def _cr3_date(f) -> datetime | None:
    """Canon CR3: EXIF lives in 'CMT1' (IFD0) and 'CMT2' (Exif IFD) boxes, each a TIFF."""
    f.seek(0)
    head = f.read(1 << 18)
    for box in (b"CMT2", b"CMT1"):
        i = head.find(box)
        if i >= 4:
            d = _tiff_date(f, i + 4, exif_ifd_at_root=(box == b"CMT2"))
            if d:
                return d
    return None


def _jpeg_tiff_offset(f, start: int) -> int | None:
    """Offset of the TIFF header inside a JPEG's 'Exif' APP1 segment."""
    pos = start + 2
    for _ in range(1000):
        f.seek(pos)
        marker = f.read(2)
        if len(marker) < 2 or marker[0] != 0xFF:
            return None
        m = marker[1]
        if m == 0xFF:              # fill byte
            pos += 1
            continue
        if m in (0xD9, 0xDA):      # end of image / start of scan: no EXIF before pixels
            return None
        if 0xD0 <= m <= 0xD7 or m == 0x01:
            pos += 2
            continue
        seglen = struct.unpack(">H", f.read(2))[0]
        if m == 0xE1 and f.read(6) == b"Exif\x00\x00":
            return pos + 10
        pos += 2 + seglen
    return None


def _boxes(f, start: int, end: int):
    pos = start
    while pos + 8 <= end:
        f.seek(pos)
        size, btype = struct.unpack(">I4s", f.read(8))
        hdr = 8
        if size == 1:
            size = struct.unpack(">Q", f.read(8))[0]
            hdr = 16
        elif size == 0:
            size = end - pos
        if size < hdr:
            return
        yield btype, pos, size, hdr
        pos += size


def _isobmff_tiff_offset(f) -> int | None:
    """HEIC/HEIF/AVIF: locate the 'Exif' item via meta/iinf + meta/iloc."""
    f.seek(0, 2)
    file_end = f.tell()
    meta = next(((p + h + 4, p + s) for t, p, s, h in _boxes(f, 0, file_end) if t == b"meta"), None)
    if not meta:
        return None
    exif_id, iloc = None, None
    for btype, p, s, h in _boxes(f, *meta):
        if btype == b"iinf":
            exif_id = _iinf_exif_id(f, p + h, p + s)
        elif btype == b"iloc":
            iloc = (p + h, p + s)
    if exif_id is None or iloc is None:
        return None
    loc = _iloc_offset(f, iloc[0], exif_id)
    if loc is None:
        return None
    f.seek(loc)
    tiff = loc + 4 + struct.unpack(">I", f.read(4))[0]
    f.seek(tiff)
    if f.read(2) not in (b"II", b"MM"):           # some writers point at "Exif\0\0"
        f.seek(loc)
        blob = f.read(32)
        i = blob.find(b"Exif\x00\x00")
        tiff = loc + i + 6 if i >= 0 else None
    return tiff


def _iinf_exif_id(f, start: int, end: int) -> int | None:
    f.seek(start)
    version = f.read(4)[0]
    f.read(2 if version == 0 else 4)               # entry_count
    for btype, p, s, h in _boxes(f, f.tell(), end):
        if btype != b"infe":
            continue
        f.seek(p + h)
        v = f.read(4)[0]
        if v < 2:
            continue
        item_id = struct.unpack(">H" if v == 2 else ">I", f.read(2 if v == 2 else 4))[0]
        f.read(2)                                   # protection index
        if f.read(4) == b"Exif":
            return item_id
    return None


def _uint(f, size: int) -> int:
    return int.from_bytes(f.read(size), "big") if size else 0


def _iloc_offset(f, start: int, wanted: int) -> int | None:
    f.seek(start)
    version = f.read(4)[0]
    b1, b2 = f.read(2)
    off_size, len_size, base_size = b1 >> 4, b1 & 15, b2 >> 4
    idx_size = b2 & 15 if version in (1, 2) else 0
    count = _uint(f, 2 if version < 2 else 4)
    if count > 4096:
        return None
    for _ in range(count):
        item_id = _uint(f, 2 if version < 2 else 4)
        method = _uint(f, 2) & 15 if version in (1, 2) else 0
        f.read(2)                                   # data reference index
        base = _uint(f, base_size)
        extents = _uint(f, 2)
        if extents > 4096:
            return None
        first = None
        for _ in range(extents):
            _uint(f, idx_size)
            off = _uint(f, off_size)
            _uint(f, len_size)
            if first is None:
                first = off
        if item_id == wanted:
            return base + (first or 0) if method == 0 else None
    return None


def _tiff_date(f, base: int, exif_ifd_at_root: bool = False) -> datetime | None:
    f.seek(base)
    hdr = f.read(8)
    if hdr[:2] == b"II":
        e = "<"
    elif hdr[:2] == b"MM":
        e = ">"
    else:
        return None
    magic, ifd0 = struct.unpack(e + "HI", hdr[2:8])
    if magic not in _TIFF_MAGIC:
        return None
    found: dict[int, tuple] = {}
    exif_ifd = None
    for tag, typ, count, raw in _ifd(f, base, ifd0, e):
        if exif_ifd_at_root and tag in (0x9003, 0x9004):
            found[tag] = (typ, count, raw)
        elif tag == 0x8769:
            exif_ifd = struct.unpack(e + "I", raw)[0]
        elif tag == 0x0132:
            found[tag] = (typ, count, raw)
    if exif_ifd:
        for tag, typ, count, raw in _ifd(f, base, exif_ifd, e):
            if tag in (0x9003, 0x9004):
                found[tag] = (typ, count, raw)
    for tag in _DATE_TAGS:
        if tag in found:
            d = _ascii_date(f, base, e, *found[tag])
            if d:
                return d
    return None


def _ifd(f, base: int, offset: int, e: str):
    f.seek(base + offset)
    n = struct.unpack(e + "H", f.read(2))[0]
    if n > 1000:
        return
    data = f.read(12 * n)
    for i in range(n):
        entry = data[12 * i:12 * i + 12]
        if len(entry) < 12:
            return
        tag, typ, count = struct.unpack(e + "HHI", entry[:8])
        yield tag, typ, count, entry[8:12]


def _ascii_date(f, base: int, e: str, typ: int, count: int, raw: bytes) -> datetime | None:
    if typ != 2 or count < 19:
        return None
    f.seek(base + struct.unpack(e + "I", raw)[0])
    text = f.read(min(count, 32)).split(b"\x00")[0].decode("ascii", "ignore").strip()
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d %H:%M"):
        try:
            return datetime.strptime(text[:19] if "S" in fmt else text[:16], fmt)
        except ValueError:
            continue
    return None


def taken_or_mtime(path: Path) -> datetime:
    """Photo 'date taken' when the file has one, otherwise its modification time."""
    return taken_date(path) or datetime.fromtimestamp(path.stat().st_mtime)


# --------------------------------------------------------------------------- #
# Naming helpers
# --------------------------------------------------------------------------- #

_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def slugify(text: str, lower: bool = True, sep: str = "_") -> str:
    """'Relatório Final (v2)' -> 'relatorio_final_v2'. Safe on every OS."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^\w.-]+", sep, text)
    text = re.sub(rf"{re.escape(sep)}{{2,}}", sep, text).strip(sep + ".")
    return text.lower() if lower else text


def render_name(template: str, path: Path, index: int, stem: str | None = None) -> str:
    stat = path.stat()
    fields = {
        "stem": path.stem if stem is None else stem,
        "ext": path.suffix,
        "n": index,
        "date": datetime.fromtimestamp(stat.st_mtime),
        # Reading EXIF costs a file read, so only do it when the template asks.
        "taken": taken_or_mtime(path) if "{taken" in template else None,
        "today": datetime.now(),
        "parent": path.parent.name,
        "size_kb": stat.st_size // 1024,
    }
    try:
        name = template.format(**fields)
    except KeyError as e:
        raise TidyError(f"Unknown template field {e}. Available: {', '.join('{' + k + '}' for k in fields)}")
    except (ValueError, IndexError) as e:
        raise TidyError(f"Invalid template '{template}': {e}")
    if "/" in name or "\\" in name:
        raise TidyError("Template must produce a file name, not a path (no '/' or '\\').")
    return name


def _key(p: Path) -> str:
    # Case- and normalization-insensitive on purpose: Windows and macOS file
    # systems treat 'A.txt'/'a.txt' (and composed/decomposed 'é') as the same
    # file, so we must too.
    return unicodedata.normalize("NFC", str(p)).casefold()


def resolve_collisions(moves: list[Move]) -> list[Move]:
    """
    Make every destination unique. A destination is free if nobody else
    already claimed it in this plan and either nothing is there on disk, or
    whatever is there is itself being moved away by this plan.
    """
    active = [m for m in moves if m.src != m.dst]
    leaving = {_key(m.src) for m in active}
    # Files that keep their name still occupy it.
    claimed = {_key(m.src) for m in moves if m.src == m.dst}
    result = []
    for m in active:
        dst, i = m.dst, 1
        # A case-only rename (Photo.JPG -> photo.jpg) passes: its own src is "leaving".
        while _key(dst) in claimed or (dst.exists() and _key(dst) not in leaving):
            dst = m.dst.with_name(f"{m.dst.stem}_{i}{m.dst.suffix}")
            i += 1
        claimed.add(_key(dst))
        result.append(Move(m.src, dst))
    return result


# --------------------------------------------------------------------------- #
# Planners: they only compute moves, never touch the disk
# --------------------------------------------------------------------------- #

def plan_rename(files: list[Path], template: str, find: str | None, replace: str,
                slug: bool, lower: bool, start: int) -> list[Move]:
    pattern = None
    if find:
        try:
            pattern = re.compile(find)
        except re.error as e:
            raise TidyError(f"Invalid --find regex: {e}")

    moves = []
    for index, path in enumerate(files, start=start):
        stem = pattern.sub(replace, path.stem) if pattern else path.stem
        name = render_name(template, path, index, stem)
        if slug or lower:
            p = Path(name)
            stem = slugify(p.stem, lower=lower) if slug else p.stem.lower()
            name = (stem or "file") + (p.suffix.lower() if lower else p.suffix)
        name = _INVALID_CHARS.sub("_", name).strip() or f"file{path.suffix}"
        moves.append(Move(path, path.with_name(name)))
    return resolve_collisions(moves)


# --------------------------------------------------------------------------- #
# Execution + journal
# --------------------------------------------------------------------------- #

def apply_moves(moves: list[Move], root: Path) -> Path:
    """
    Two-phase move: every source first goes to a temporary name, then to its
    final name. This makes chains and swaps safe (a->b while b->c, or a<->b).
    The journal is written even if something fails halfway.
    """
    journal_dir = root / JOURNAL_DIR
    journal_dir.mkdir(exist_ok=True)
    journal = journal_dir / f"journal-{datetime.now():%Y%m%d-%H%M%S-%f}.json"

    staged: list[tuple[Move, Path]] = []
    done: list[Move] = []
    try:
        for i, m in enumerate(moves):
            tmp = m.src.with_name(f"{TMP_PREFIX}{os.getpid()}-{i}{m.src.suffix}")
            os.rename(m.src, tmp)
            staged.append((m, tmp))
        for m, tmp in staged:
            if m.dst.exists():
                raise TidyError(f"Refusing to overwrite '{m.dst}' (it appeared during the run).")
            m.dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(tmp), str(m.dst))
            done.append(m)
    except BaseException:
        finished = {id(m) for m in done}
        for m, tmp in staged:
            if id(m) not in finished and tmp.exists():
                os.rename(tmp, m.src)
        raise
    finally:
        if done:
            payload = {
                "version": __version__,
                "root": str(root.resolve()),
                "created": datetime.now().isoformat(timespec="seconds"),
                "moves": [{"src": str(m.src.resolve()), "dst": str(m.dst.resolve())} for m in done],
            }
            journal.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return journal


def undo(root: Path, journal_path: Path | None) -> int:
    journal_dir = root / JOURNAL_DIR
    if journal_path is None:
        journals = sorted(journal_dir.glob("journal-*.json")) if journal_dir.is_dir() else []
        if not journals:
            raise TidyError(f"No journal found in '{journal_dir}'. Nothing to undo.")
        journal_path = journals[-1]

    data = json.loads(journal_path.read_text(encoding="utf-8"))
    journal_root = Path(data["root"])
    restored = skipped = 0
    for entry in reversed(data["moves"]):
        src, dst = Path(entry["src"]), Path(entry["dst"])
        if not dst.exists():
            print(f"  skip (missing now): {dst}")
            skipped += 1
            continue
        if src.exists() and _key(src) != _key(dst):
            print(f"  skip (original name taken): {src}")
            skipped += 1
            continue
        src.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(dst), str(src))
        _remove_empty_parents(dst.parent, stop_at=journal_root)
        restored += 1

    journal_path.rename(journal_path.with_suffix(".undone"))
    print(f"Restored {restored} file(s), skipped {skipped}. Journal: {journal_path.name}")
    return 0 if skipped == 0 else 1


def _remove_empty_parents(folder: Path, stop_at: Path) -> None:
    folder, stop_at = folder.resolve(), stop_at.resolve()
    while folder != stop_at and stop_at in folder.parents:
        try:
            folder.rmdir()  # only succeeds when empty
        except OSError:
            return
        folder = folder.parent


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def show_plan(moves: list[Move], root: Path, applying: bool = False) -> None:
    def rel(p: Path) -> str:
        try:
            return str(p.relative_to(root))
        except ValueError:
            return str(p)

    if not moves:
        print("Nothing to do: every file already matches.")
        return
    width = min(max(len(rel(m.src)) for m in moves), 60)
    for m in moves:
        print(f"  {rel(m.src):<{width}}  ->  {rel(m.dst)}")
    print(f"\n{len(moves)} file(s) {'will change' if applying else 'would change'}.")


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("folder", type=Path, help="folder to work on")
    common.add_argument("-r", "--recursive", action="store_true", help="include sub-folders")
    common.add_argument("-g", "--glob", help="only files matching this pattern, e.g. '*.jpg'")
    common.add_argument("--hidden", action="store_true", help="include hidden files")
    common.add_argument("--apply", action="store_true", help="actually do it (default is a dry run)")

    parser = argparse.ArgumentParser(
        prog="tidybatch",
        description="Bulk rename files safely. Dry run by default; every change can be undone.",
        epilog="Need to sort folders or remove duplicates? TidyBatch Pro adds `organize` and `dedupe`: https://whop.com/tidybatch/bulk-file-renamer/",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    r = sub.add_parser("rename", parents=[common], help="rename files from a template",
                       description="Template fields: {stem} {ext} {n} {date} {taken} {today} {parent} {size_kb}. "
                                   "{date} is the modification time; {taken} is the photo's EXIF date taken "
                                   "(falls back to {date}). Formats work: {n:03}, {taken:%Y-%m-%d}.")
    r.add_argument("-t", "--template", default="{stem}{ext}", help="new name template (default: {stem}{ext})")
    r.add_argument("--find", help="regex to search in the original name (without extension)")
    r.add_argument("--replace", default="", help="replacement for --find (supports \\1 groups)")
    r.add_argument("--slug", action="store_true", help="remove accents, spaces and symbols")
    r.add_argument("--lower", action="store_true", help="lowercase the whole name")
    r.add_argument("--sort", choices=["name", "date", "taken", "size"], default="name", help="order used for {n}")
    r.add_argument("--start", type=int, default=1, help="first value of {n} (default: 1)")

    u = sub.add_parser("undo", help="revert the last applied run in a folder")
    u.add_argument("folder", type=Path)
    u.add_argument("--journal", type=Path, help="a specific journal file to revert")

    for name, what in (("organize", "sort files into folders"), ("dedupe", "move exact duplicates aside")):
        pro = sub.add_parser(name, help=f"[Pro] {what}")
        pro.add_argument("rest", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    # Windows consoles/redirects may use a legacy code page: never crash on a file name.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    args = build_parser().parse_args(argv)
    if args.command in ("organize", "dedupe"):
        # Non-zero so a script calling it never mistakes "not available" for success.
        print(f"`{args.command}` is part of TidyBatch Pro (one-time purchase, includes ready-to-run apps):\n"
              f"  https://whop.com/tidybatch/bulk-file-renamer/\n"
              "TidyBatch Core (this version) does `rename` and `undo`.", file=sys.stderr)
        return 2
    root: Path = args.folder.expanduser()
    if not root.is_dir():
        print(f"error: '{root}' is not a folder", file=sys.stderr)
        return 2

    try:
        if args.command == "undo":
            return undo(root, args.journal)

        files = sort_files(iter_files(root, args.recursive, args.glob, args.hidden),
                           getattr(args, "sort", "name"))
        moves = plan_rename(files, args.template, args.find,
                            args.replace, args.slug, args.lower, args.start)

        show_plan(moves, root, applying=args.apply)
        if not moves:
            return 0
        if not args.apply:
            print("Dry run: nothing was changed. Add --apply to do it.")
            return 0
        journal = apply_moves(moves, root)
        print(f"Done. To revert: tidybatch undo \"{root}\"   (journal: {journal.name})")
        return 0
    except TidyError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted. Files already moved are listed in the journal; run `undo` to revert.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
