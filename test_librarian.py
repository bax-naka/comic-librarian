"""librarian.py の最小限の試験（python -m unittest）。"""
import io
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout

import librarian as clo

HERE = os.path.dirname(os.path.abspath(__file__))


def mk(root, rel, size):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(b"\0" * size)


class PlanAgainstExpected(unittest.TestCase):
    """Titanのテストサンプル一覧に対する判定が、事前に書いた期待値と一致するか。"""

    def test_titan_samples(self):
        dirs, files, newf = clo.load_tsv(os.path.join(HERE, "data/titan_samples.tsv"))
        dirs = [d for d in dirs if "[OpenHands]" not in d]
        files = [f for f in files if "[OpenHands]" not in f[0]]
        items = {i.name: i for i in clo.make_plan(clo.build_library(dirs, files), newf)}
        with open(os.path.join(HERE, "data/titan_expected.tsv"), encoding="utf-8") as f:
            rows = [ln.rstrip("\n").split("\t") for ln in f if ln.strip() and not ln.startswith("#")]
        for name, kind, _dest, _why in rows:
            with self.subTest(name=name):
                self.assertEqual(items[name].kind, kind)


class Execute(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.mkdtemp()
        self.lib, self.new = os.path.join(self.t, "LIB"), os.path.join(self.t, "NEW")
        mk(self.lib, "_S/[A×B] 作品 甲/[A×B] 作品 甲 第01巻.zip", 10)
        mk(self.new, "[A×B] 作品 甲 第02巻.zip", 20)
        mk(self.new, "[C] 別の物語 第01巻.zip", 30)
        mk(self.new, "[A] 作品 甲（誤記） 第03巻.zip", 40)

    def tearDown(self):
        shutil.rmtree(self.t)

    def plan(self):
        d, f = clo.scan(self.lib)
        return clo.make_plan(clo.build_library(d, f), clo.new_files_in(self.new))

    def test_move_and_verify(self):
        items = self.plan()
        kinds = {i.name: i.kind for i in items}
        self.assertEqual(kinds["[A×B] 作品 甲 第02巻.zip"], "追加")
        self.assertEqual(kinds["[C] 別の物語 第01巻.zip"], "新規")
        self.assertEqual(kinds["[A] 作品 甲（誤記） 第03巻.zip"], "要確認")
        with redirect_stdout(io.StringIO()):
            clo.ask_review(items, inp=lambda _: "1")          # 要確認 → 候補1へ追加
            ok, ng = clo.execute(items, self.lib, self.new, os.path.join(self.t, "log.tsv"))
        self.assertEqual((ok, ng), (3, 0))
        self.assertEqual(clo.verify(items, self.lib, self.new), [])
        self.assertTrue(os.path.isfile(os.path.join(self.lib, "[C] 別の物語", "[C] 別の物語 第01巻.zip")))
        self.assertEqual(os.listdir(self.new), [])

    def test_no_overwrite(self):
        items = self.plan()
        # 計画のあとに、移動先へ同名ファイルが置かれた場合
        mk(self.lib, "_S/[A×B] 作品 甲/[A×B] 作品 甲 第02巻.zip", 99)
        with redirect_stdout(io.StringIO()):
            clo.ask_review(items, inp=lambda _: "")
            ok, ng = clo.execute(items, self.lib, self.new, os.path.join(self.t, "log.tsv"))
        self.assertEqual(ng, 1)
        self.assertEqual(os.path.getsize(os.path.join(self.lib, "_S/[A×B] 作品 甲/[A×B] 作品 甲 第02巻.zip")), 99)
        self.assertTrue(os.path.isfile(os.path.join(self.new, "[A×B] 作品 甲 第02巻.zip")))

    def test_review_skip_and_new(self):
        items = self.plan()
        with redirect_stdout(io.StringIO()):
            clo.ask_review(items, inp=lambda _: "n")          # 要確認 → 新規作品
        i = next(x for x in items if x.name.startswith("[A] 作品 甲（誤記）"))
        self.assertEqual((i.kind, i.dest), ("新規", "[A] 作品 甲（誤記）"))


class Parse(unittest.TestCase):
    def test_variants(self):
        p = clo.parse_name("[蝉川夏哉×碓井ツカサ] 異世界居酒屋｢げん｣ 第01 14巻")
        self.assertEqual(clo.volumes(p), set(range(1, 15)))
        self.assertEqual(clo.parse_name("[x] 題 第02巻w").v1, 2)
        self.assertEqual(clo.norm("異世界居酒屋｢げん｣"), clo.norm("異世界居酒屋「げん」"))
        self.assertEqual(clo.parse_name("[あろえ×立山鈴] t").authors, clo.parse_name("[立山鈴×あろえ] t").authors)


if __name__ == "__main__":
    unittest.main()
