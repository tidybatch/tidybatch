"""Feed taken_date truncated and byte-flipped copies of real photos.

It must never raise and never hang: a damaged file simply has no date, and
TidyBatch falls back to the modification time. POSIX only (uses SIGALRM).
Runs in CI; to run it locally (see verify_exif.py for the ExifTool checkout):

    python3 dev/fuzz_exif.py /tmp/exiftool 3000
"""
from __future__ import annotations

import random
import signal
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tidybatch import taken_date  # noqa: E402

FORMATS = {".jpg", ".tif", ".heic", ".cr2", ".cr3", ".nef", ".dng", ".rw2", ".raf"}
TIME_LIMIT = 2  # seconds per file; real files take a few milliseconds


class Hang(BaseException):
    """Not an Exception subclass, so taken_date's own error handling can't swallow it."""


def on_alarm(*_):
    raise Hang


def main() -> int:
    exiftool = Path(sys.argv[1] if len(sys.argv) > 1 else "exiftool")
    iterations = int(sys.argv[2]) if len(sys.argv) > 2 else 3000
    seeds = sorted(p for p in (exiftool / "t" / "images").iterdir() if p.suffix.lower() in FORMATS)
    rng = random.Random(1)  # fixed seed: a failure can be replayed exactly
    signal.signal(signal.SIGALRM, on_alarm)

    crashes = hangs = 0
    worst = 0.0
    with tempfile.TemporaryDirectory() as tmp:
        sample = Path(tmp) / "sample"
        for i in range(iterations):
            seed = rng.choice(seeds)
            data = bytearray(seed.read_bytes())
            if rng.random() < 0.4:
                data = data[:rng.randint(0, len(data))]
            else:
                for _ in range(rng.randint(1, 40)):
                    data[rng.randrange(len(data))] = rng.randrange(256)
            sample.write_bytes(data)

            signal.alarm(TIME_LIMIT)
            start = time.perf_counter()
            try:
                taken_date(sample)
            except Hang:
                hangs += 1
                print(f"HANG #{i} (mutated {seed.name})")
            except Exception as e:  # anything else is a bug: taken_date must swallow it
                crashes += 1
                print(f"CRASH #{i} (mutated {seed.name}): {type(e).__name__}: {e}")
            finally:
                signal.alarm(0)
            worst = max(worst, time.perf_counter() - start)

    print(f"{iterations} corrupted files: {crashes} exceptions, {hangs} hangs, "
          f"slowest {worst * 1000:.1f} ms")
    return 1 if crashes or hangs else 0


if __name__ == "__main__":
    sys.exit(main())
