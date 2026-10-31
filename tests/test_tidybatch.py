"""Run with:  python -m unittest discover -s tests  (from the product folder)."""
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
        return {str(p.relative_to(folder)) for p in folder.rglob("*")
                if p.is_file() and tidybatch.JOURNAL_DIR not in p.parts}

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
        self.assertEqual(run("organize", str(self.root), "--apply"), 0)
        self.assertEqual(run("dedupe", str(self.root)), 0)
        self.assertEqual(self.names(), {"a b.txt"})

    def test_bad_template_is_a_clean_error(self):
        self.make("a.txt")
        self.assertEqual(run("rename", str(self.root), "-t", "{nope}"), 2)

    def test_template_cannot_create_paths(self):
        self.make("a.txt")
        self.assertEqual(run("rename", str(self.root), "-t", "../{stem}{ext}", "--apply"), 2)
        self.assertEqual(self.names(), {"a.txt"})


if __name__ == "__main__":
    unittest.main()
