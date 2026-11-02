"""Run with:  python -m unittest discover -s tests  (from the product folder)."""
from __future__ import annotations

import io
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tidybatch  # noqa: E402


def run(*argv: str) -> int:
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        return tidybatch.main(list(argv))


def jpeg_with_exif_date(when: str) -> bytes:
    """Smallest JPEG-ish file whose APP1 segment carries EXIF DateTimeOriginal."""
    import struct
    tiff = b"II*\x00" + struct.pack("<I", 8)
    tiff += struct.pack("<H", 1) + struct.pack("<HHII", 0x8769, 4, 1, 26) + struct.pack("<I", 0)
    tiff += struct.pack("<H", 1) + struct.pack("<HHII", 0x9003, 2, 20, 44) + struct.pack("<I", 0)
    tiff += when.encode("ascii") + b"\x00"
    payload = b"Exif\x00\x00" + tiff
    return b"\xff\xd8" + b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload + b"\xff\xd9"


def case_sensitive(folder: Path) -> bool:
    probe = folder / "CaseProbe"
    probe.write_text("x")
    try:
        return not (folder / "caseprobe").exists()
    finally:
        probe.unlink()


class TidyBatchTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def make(self, rel: str, content: str = "x", age: float = 0) -> Path:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        if age:
            t = time.time() - age
            os.utime(p, (t, t))
        return p

    def names(self, folder: Path | None = None) -> set[str]:
        folder = folder or self.root
        return {p.relative_to(folder).as_posix() for p in folder.rglob("*")
                if p.is_file() and tidybatch.JOURNAL_DIR not in p.parts}

    def make_photo(self, rel: str, taken: str, age: float = 0) -> Path:
        p = self.root / rel
        p.write_bytes(jpeg_with_exif_date(taken))
        if age:
            t = time.time() - age
            os.utime(p, (t, t))
        return p

    def test_taken_date_reads_exif(self):
        p = self.make_photo("a.jpg", "2021:05:06 07:08:09")
        self.assertEqual(str(tidybatch.taken_date(p)), "2021-05-06 07:08:09")

    def test_taken_template_uses_exif_not_mtime(self):
        self.make_photo("IMG_1.jpg", "2019:12:31 23:59:59")
        run("rename", str(self.root), "-t", "{taken:%Y-%m-%d_%H%M%S}{ext}", "--apply")
        self.assertEqual(self.names(), {"2019-12-31_235959.jpg"})

    def test_taken_falls_back_to_mtime(self):
        self.make("notes.txt")
        p = self.root / "notes.txt"
        t = time.mktime((2020, 2, 3, 12, 0, 0, 0, 0, -1)); os.utime(p, (t, t))
        run("rename", str(self.root), "-t", "{taken:%Y-%m-%d}_{stem}{ext}", "--apply")
        self.assertEqual(self.names(), {"2020-02-03_notes.txt"})

    def test_garbage_file_has_no_taken_date(self):
        p = self.root / "broken.jpg"
        p.write_bytes(b"\xff\xd8\xff\xe1\x00")
        self.assertIsNone(tidybatch.taken_date(p))

    def test_sort_taken_orders_by_photo_date(self):
        self.make_photo("b.jpg", "2020:01:01 10:00:00")
        self.make_photo("a.jpg", "2021:01:01 10:00:00")
        run("rename", str(self.root), "--sort", "taken", "-t", "{n}{ext}", "--apply")
        self.assertEqual(tidybatch.taken_date(self.root / "1.jpg").year, 2020)

    def test_dry_run_changes_nothing(self):
        self.make("My File.TXT")
        self.assertEqual(run("rename", str(self.root), "--slug", "--lower"), 0)
        self.assertEqual(self.names(), {"My File.TXT"})

    def test_slug_and_undo_round_trip(self):
        self.make("Relatório Final (v2).PDF")
        self.make("Foto Férias 01.JPG")
        before = self.names()
        run("rename", str(self.root), "--slug", "--lower", "--apply")
        self.assertEqual(self.names(), {"relatorio_final_v2.pdf", "foto_ferias_01.jpg"})
        run("undo", str(self.root))
        self.assertEqual(self.names(), before)

    def test_numbering_chain_does_not_clobber(self):
        # 1->2, 2->3, 3->4: a naive renamer overwrites file 2 with file 1.
        for i in (1, 2, 3):
            self.make(f"{i}.txt", content=f"content-{i}")
        run("rename", str(self.root), "-t", "{n}{ext}", "--start", "2", "--apply")
        self.assertEqual(self.names(), {"2.txt", "3.txt", "4.txt"})
        self.assertEqual((self.root / "4.txt").read_text(), "content-3")
        self.assertEqual((self.root / "2.txt").read_text(), "content-1")

    def test_collisions_get_suffix(self):
        self.make("a b.txt", "1")
        self.make("a_b.txt", "2")  # already the slug of the first one
        run("rename", str(self.root), "--slug", "--apply")
        self.assertEqual(self.names(), {"a_b.txt", "a_b_1.txt"})
        self.assertEqual((self.root / "a_b.txt").read_text(), "2")

    def test_case_only_collision_on_linux(self):
        # Two new names that differ only in case: Linux would happily create both, but the same
        # folder on a Mac or Windows holds just one of them, so the second must get a suffix.
        a, b = self.make("a.txt"), self.make("b.txt")
        moves = tidybatch.resolve_collisions([
            tidybatch.Move(a, self.root / "Photo.jpg"),
            tidybatch.Move(b, self.root / "photo.jpg"),
        ])
        self.assertEqual(moves[1].dst.name, "photo_1.jpg")

    def test_lowering_next_to_the_lowercase_name_keeps_both(self):
        if not case_sensitive(self.root):
            self.skipTest("only a case-sensitive file system (Linux) can hold Report.TXT and report.txt together")
        self.make("Report.TXT", "upper")
        self.make("report.txt", "lower")
        run("rename", str(self.root), "--lower", "--apply")
        self.assertEqual((self.root / "report.txt").read_text(), "lower")
        self.assertEqual((self.root / "report_1.txt").read_text(), "upper")

    def test_case_only_rename_of_one_file_needs_no_suffix(self):
        self.make("Photo.JPG")
        run("rename", str(self.root), "--lower", "--apply")
        self.assertEqual(self.names(), {"photo.jpg"})

    def test_case_only_rename_is_not_dropped_as_a_no_op(self):
        # Windows paths compare equal regardless of case; the plan must still keep Photo.JPG -> photo.jpg.
        from pathlib import PureWindowsPath
        self.assertEqual(PureWindowsPath("Photo.JPG"), PureWindowsPath("photo.jpg"))
        a = self.make("Photo.JPG")
        moves = tidybatch.resolve_collisions([tidybatch.Move(a, a.with_name("photo.jpg"))])
        self.assertEqual([m.dst.name for m in moves], ["photo.jpg"])

    def interrupt_move(self, at: int):
        """Patch shutil.move so the run is interrupted (Ctrl+C) on its `at`-th move."""
        from unittest import mock
        real_move, calls = tidybatch.shutil.move, []

        def interrupted(src, dst):
            calls.append(dst)
            if len(calls) == at:
                raise KeyboardInterrupt
            return real_move(src, dst)

        return mock.patch.object(tidybatch.shutil, "move", side_effect=interrupted)

    def contents(self) -> dict[str, str]:
        return {n: (self.root / n).read_text() for n in self.names()}

    def numbered(self) -> dict[str, str]:
        for i in (1, 2, 3):
            self.make(f"{i}.txt", content=f"content-{i}")
        return self.contents()

    def test_interrupted_run_puts_every_file_back(self):
        # Ctrl+C halfway through phase 2: every file goes back to its original name, no temporary
        # names are left, and no journal is left to undo.
        for name in ("A.txt", "B.txt", "C.txt"):
            self.make(name, content=name)
        with self.interrupt_move(2):
            self.assertEqual(run("rename", str(self.root), "--lower", "--apply"), 130)
        self.assertEqual(self.contents(), {"A.txt": "A.txt", "B.txt": "B.txt", "C.txt": "C.txt"})
        self.assertEqual(list((self.root / tidybatch.JOURNAL_DIR).iterdir()), [])

    def test_interrupted_numbering_chain_loses_nothing(self):
        # 1->2, 2->3, 3->4 stopped after the first move: "2" already holds the old "1" when the old
        # "2" has to go back. Up to 1.1.3 putting it back overwrote the old "1".
        before = self.numbered()
        with self.interrupt_move(2):
            self.assertEqual(run("rename", str(self.root), "-t", "{n}{ext}", "--start", "2", "--apply"), 130)
        self.assertEqual(self.contents(), before)

    def test_undo_of_numbering_chain_restores_every_file(self):
        # Up to 1.1.3 undo put back only "1" and skipped "2" and "3" as taken.
        before = self.numbered()
        run("rename", str(self.root), "-t", "{n}{ext}", "--start", "2", "--apply")
        self.assertEqual(run("undo", str(self.root)), 0)
        self.assertEqual(self.contents(), before)

    def test_undo_of_a_swap(self):
        a, b = self.make("a.txt", "A"), self.make("b.txt", "B")
        with redirect_stdout(io.StringIO()):
            tidybatch.apply_moves([tidybatch.Move(a, b), tidybatch.Move(b, a)], self.root)
            self.assertEqual(self.contents(), {"a.txt": "B", "b.txt": "A"})
            self.assertEqual(tidybatch.undo(self.root, None), 0)
        self.assertEqual(self.contents(), {"a.txt": "A", "b.txt": "B"})

    def crash(self, target: str, at: int, *argv: str) -> None:
        """Run tidybatch in a child process that dies like kill -9 on its `at`-th call to `target`."""
        import subprocess
        module, name = target.split(".")
        code = "\n".join([
            "import os, shutil, sys",
            f"sys.path.insert(0, {str(Path(tidybatch.__file__).resolve().parent)!r})",
            "import tidybatch",
            f"owner, calls = {module}, []",
            f"real = owner.{name}",
            "def dying(*a, **k):",
            "    calls.append(1)",
            f"    if len(calls) == {at}:",
            "        os._exit(9)",
            "    return real(*a, **k)",
            f"owner.{name} = dying",
            f"tidybatch.main({list(argv)!r})",
        ])
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        self.assertEqual(r.returncode, 9, r.stdout + r.stderr)

    def test_undo_after_a_crash_while_moving_to_temporary_names(self):
        # Power cut in phase 1: one file is at its temporary name, the others never moved.
        before = self.numbered()
        self.crash("os.rename", 2, "rename", str(self.root), "-t", "{n}{ext}", "--start", "2", "--apply")
        self.assertEqual(len(self.names()), 3)
        self.assertEqual(run("undo", str(self.root)), 0)
        self.assertEqual(self.contents(), before)

    def test_undo_after_a_crash_while_moving_to_new_names(self):
        # Power cut in phase 2 of a chain: "2" holds the old "1", the old "2" and "3" are at their
        # temporary names, and the journal still lists every planned move.
        before = self.numbered()
        self.crash("shutil.move", 2, "rename", str(self.root), "-t", "{n}{ext}", "--start", "2", "--apply")
        self.assertEqual((self.root / "2.txt").read_text(), "content-1")
        self.assertEqual(run("undo", str(self.root)), 0)
        self.assertEqual(self.contents(), before)

    def test_journal_is_written_before_any_file_moves(self):
        # Full disk: if the journal cannot be written, nothing may move (there would be no undo).
        import errno
        from unittest import mock
        self.make("A.txt")
        real_write = Path.write_text

        def full_disk(path, *args, **kwargs):
            if tidybatch.JOURNAL_DIR in path.parts:
                raise OSError(errno.ENOSPC, "No space left on device")
            return real_write(path, *args, **kwargs)

        with mock.patch.object(Path, "write_text", autospec=True, side_effect=full_disk):
            self.assertEqual(run("rename", str(self.root), "--lower", "--apply"), 2)
        self.assertEqual(self.names(), {"A.txt"})
        self.assertEqual(list((self.root / tidybatch.JOURNAL_DIR).iterdir()), [])

    def test_journal_that_cannot_be_removed_does_not_hide_the_interrupt(self):
        # The run is rolled back, then the journal cannot be removed (a read-only folder, say): the
        # interrupt still comes through with a warning, and undo finds nothing to change.
        import errno
        from unittest import mock
        for name in ("A.txt", "B.txt", "C.txt"):
            self.make(name, content=name)
        real_unlink = Path.unlink

        def locked(path, *args, **kwargs):
            if tidybatch.JOURNAL_DIR in path.parts:
                raise OSError(errno.EACCES, "Permission denied")
            return real_unlink(path, *args, **kwargs)

        err = io.StringIO()
        with self.interrupt_move(2), mock.patch.object(Path, "unlink", autospec=True, side_effect=locked), \
                redirect_stdout(io.StringIO()), redirect_stderr(err):
            self.assertEqual(tidybatch.main(["rename", str(self.root), "-t", "{n}{ext}", "--apply"]), 130)
        self.assertIn("warning:", err.getvalue())
        self.assertEqual(self.contents(), {"A.txt": "A.txt", "B.txt": "B.txt", "C.txt": "C.txt"})
        self.assertEqual(run("undo", str(self.root)), 0)
        self.assertEqual(self.contents(), {"A.txt": "A.txt", "B.txt": "B.txt", "C.txt": "C.txt"})

    def forge_journal(self, payload) -> Path:
        import json
        j = self.root / tidybatch.JOURNAL_DIR / "journal-forged.json"
        j.parent.mkdir(exist_ok=True)
        j.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")
        return j

    def test_undo_ignores_journal_paths_outside_the_folder(self):
        # A folder from someone else can carry a forged journal: undo must not move files in or out of it.
        with tempfile.TemporaryDirectory() as other:
            secret = Path(other) / "secret.txt"
            secret.write_text("mine")
            inside = self.make("inside.txt", "in")
            self.forge_journal({"root": other, "moves": [
                {"src": str(self.root.resolve() / "stolen.txt"), "dst": str(secret)},
                {"src": str(Path(other) / "out.txt"), "dst": str(inside)},
            ]})
            self.assertEqual(run("undo", str(self.root)), 1)
            self.assertEqual(secret.read_text(), "mine")
            self.assertEqual(self.names(), {"inside.txt"})
            self.assertEqual({p.name for p in Path(other).iterdir()}, {"secret.txt"})

    def test_damaged_journals_are_clean_errors(self):
        self.make("a.txt")
        for payload in ('{"root": ', "{}", '{"moves": "x"}', '{"moves": [{"src": 1, "dst": 2}]}', "[]", b"\xff"):
            with self.subTest(payload=payload):
                if isinstance(payload, bytes):
                    j = self.forge_journal("")
                    j.write_bytes(payload)
                else:
                    self.forge_journal(payload)
                err = io.StringIO()
                with redirect_stdout(io.StringIO()), redirect_stderr(err):
                    self.assertEqual(tidybatch.main(["undo", str(self.root)]), 2)
                self.assertIn("error:", err.getvalue())
                self.assertEqual(self.names(), {"a.txt"})

    def test_missing_journal_file_is_a_clean_error(self):
        self.assertEqual(run("undo", str(self.root), "--journal", str(self.root / "nope.json")), 2)

    @unittest.skipIf(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0), "needs POSIX permissions")
    def test_read_only_folder_is_a_clean_error(self):
        self.make("A.txt")
        os.chmod(self.root, 0o555)
        try:
            self.assertEqual(run("rename", str(self.root), "--lower", "--apply"), 2)
            self.assertEqual(self.names(), {"A.txt"})
        finally:
            os.chmod(self.root, 0o755)

    @unittest.skipIf(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0), "needs POSIX permissions")
    def test_undo_in_read_only_folder_can_be_retried(self):
        self.make("sub/A.txt")
        self.make("sub/B.txt")
        self.assertEqual(run("rename", str(self.root), "-r", "--lower", "--apply"), 0)
        os.chmod(self.root / "sub", 0o555)
        try:
            self.assertEqual(run("undo", str(self.root)), 2)
        finally:
            os.chmod(self.root / "sub", 0o755)
        run("undo", str(self.root))
        self.assertEqual(self.names(), {"sub/A.txt", "sub/B.txt"})

    def test_find_replace_with_groups(self):
        self.make("IMG_2024_0001.jpg")
        run("rename", str(self.root), "--find", r"IMG_(\d{4})_(\d+)", "--replace", r"\1-photo-\2", "--apply")
        self.assertEqual(self.names(), {"2024-photo-0001.jpg"})

    def test_natural_sort_numbering(self):
        for name in ("img10.png", "img2.png", "img1.png"):
            self.make(name, content=name)
        run("rename", str(self.root), "-t", "{n:02}{ext}", "--apply")
        self.assertEqual((self.root / "02.png").read_text(), "img2.png")
        self.assertEqual((self.root / "03.png").read_text(), "img10.png")

    def test_summary_wording_matches_mode(self):
        self.make("A B.txt")
        out = io.StringIO()
        with redirect_stdout(out):
            tidybatch.main(["rename", str(self.root), "--slug"])
        self.assertIn("1 file(s) would change.", out.getvalue())
        out = io.StringIO()
        with redirect_stdout(out):
            tidybatch.main(["rename", str(self.root), "--slug", "--apply"])
        self.assertIn("1 file(s) will change.", out.getvalue())

    def test_unicode_normalization_counts_as_collision(self):
        # 'é' precomposed (NFC) vs 'e' + combining accent (NFD): one file on macOS.
        a, b = self.make("a.txt"), self.make("b.txt")
        moves = tidybatch.resolve_collisions([
            tidybatch.Move(a, self.root / "caf\u00e9.txt"),
            tidybatch.Move(b, self.root / "cafe\u0301.txt"),
        ])
        self.assertEqual(moves[1].dst.name, "cafe\u0301_1.txt")

    def test_pro_commands_point_to_pro_without_touching_files(self):
        self.make("a b.txt")
        for argv in (["organize", str(self.root), "--apply"], ["dedupe", str(self.root)]):
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = tidybatch.main(argv)
            # A script must not mistake "not in Core" for success.
            self.assertEqual(code, 2)
            self.assertIn("TidyBatch Pro", err.getvalue())
            self.assertEqual(out.getvalue(), "")
        self.assertEqual(self.names(), {"a b.txt"})

    def test_pro_links_carry_utm(self):
        # Every Whop link the CLI shows must be tagged, so sales from the CLI can be counted.
        tag = "?utm_source=cli&utm_campaign=tidybatch"
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            tidybatch.main(["organize", str(self.root)])
        self.assertIn(f"/bulk-file-renamer/{tag}", err.getvalue())
        # In --help the whole link must sit on one line, or the terminal cannot open it.
        self.assertIn(f"https://whop.com/tidybatch/bulk-file-renamer/{tag}\n", tidybatch.build_parser().format_help())
        self.assertIn(f"/bulk-file-renamer/{tag}", tidybatch.__doc__)

    def test_bad_template_is_a_clean_error(self):
        self.make("a.txt")
        self.assertEqual(run("rename", str(self.root), "-t", "{nope}"), 2)

    def test_template_cannot_create_paths(self):
        self.make("a.txt")
        self.assertEqual(run("rename", str(self.root), "-t", "../{stem}{ext}", "--apply"), 2)
        self.assertEqual(self.names(), {"a.txt"})


if __name__ == "__main__":
    unittest.main()
