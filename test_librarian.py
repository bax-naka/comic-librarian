"""librarian.py の最小限の試験（python -m unittest）。"""
import io
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

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


class ListOutput(unittest.TestCase):
    def test_roundtrip(self):
        """list の出力を load_tsv で読み戻すと、実フォルダを scan したのと同じ内容になる。"""
        t = tempfile.mkdtemp()
        try:
            lib = os.path.join(t, "LIB")
            mk(lib, "_S/[A] 作品/[A] 作品 第01巻.zip", 10)
            mk(lib, "[B] 単独/[B] 単独 第01巻.rar", 20)
            out = os.path.join(t, "list.tsv")
            clo.write_list(lib, out)
            dirs, files, newf = clo.load_tsv(out)
            sd, sf = clo.scan(lib)
            self.assertEqual((sorted(dirs), sorted(files), newf), (sorted(sd), sorted(sf), []))
        finally:
            shutil.rmtree(t)


class Picker(unittest.TestCase):
    def test_detect_sd_from_mounts(self):
        t = tempfile.mkdtemp()
        try:
            mounts = os.path.join(t, "mounts")
            with open(mounts, "w", encoding="utf-8") as f:
                f.write("/dev/fuse /storage/emulated fuse rw 0 0\n/dev/fuse /storage/4FF7-51B2 fuse rw 0 0\n")
            self.assertEqual(clo.detect_sd(link=os.path.join(t, "none"), mounts=mounts), "/storage/4FF7-51B2")
        finally:
            shutil.rmtree(t)

    def test_pick_second_level_and_same_name(self):
        t = tempfile.mkdtemp()
        try:
            sd, internal = os.path.join(t, "sd"), os.path.join(t, "sdcard")
            for d in ("sd/Documents/SD_comic", "sd/Android/data", "sdcard/Download"):
                os.makedirs(os.path.join(t, d))
            cands = clo.candidate_dirs([sd, internal])
            self.assertIn(os.path.join(sd, "Documents", "SD_comic"), cands)
            self.assertNotIn(os.path.join(sd, "Android"), cands)
            answers = iter([os.path.join(internal, "Documents", "SD_comic"), "y"])
            with redirect_stdout(io.StringIO()):
                got = clo.pick_library(inp=lambda _: next(answers), roots=[sd, internal])
            self.assertEqual(got, os.path.join(sd, "Documents", "SD_comic"))
        finally:
            shutil.rmtree(t)


class RulesFromProduction(unittest.TestCase):
    """本番ライブラリの一覧での測定（tools/evaluate.py）から入れた規則。"""

    def lib(self, paths):
        dirs, files = set(), []
        for p in paths:
            parts = p.split("/")
            for i in range(1, len(parts)):
                dirs.add("/".join(parts[:i]))
            if not p.endswith("/"):
                files.append((p, 100))
        return clo.build_library(sorted(dirs), files)

    def kind(self, works, name):
        return clo.make_plan(works, [(name, 1)])[0]

    def test_latin_x_separator(self):
        w = self.lib(["_A/[采芽杏奈x三島千廣] 錬金貴族の領地経営/[采芽杏奈x三島千廣] 錬金貴族の領地経営 第06巻.zip"])
        self.assertEqual(self.kind(w, "[采芽杏奈×三島千廣] 錬金貴族の領地経営 第07巻.zip").kind, "追加")
        self.assertEqual(clo.parse_name("[Orenge30×ReiOn] t").authors, frozenset({"orenge30", "reion"}))

    def test_joined_author_key(self):
        w = self.lib(["[わんたちわ×小太郎] 無人島でエルフと共同生活/[わんたちわ×小太郎] 無人島でエルフと共同生活 第09巻.zip"])
        self.assertEqual(self.kind(w, "[わんた×ちわ小太郎] 無人島でエルフと共同生活 第10巻.zip").kind, "追加")

    def test_partial_author_overlap_goes_to_review(self):
        # 同じ原作の別作画：原作者だけが重なる → 自動で入れない
        w = self.lib(["[樋辻臥命×クニくん] 放課後の迷宮冒険者/[樋辻臥命×クニくん] 放課後の迷宮冒険者 第01巻.zip"])
        self.assertEqual(self.kind(w, "[あび×樋辻臥命×かれい] 放課後の迷宮冒険者 第03巻.zip").kind, "要確認")

    def test_rival_with_weaker_authors_is_ignored(self):
        w = self.lib(["[樋辻臥命×クニくん] 放課後の迷宮冒険者/[樋辻臥命×クニくん] 放課後の迷宮冒険者 第01巻.zip",
                      "[あび×樋辻臥命×かれい] 放課後の迷宮冒険者/[あび×樋辻臥命×かれい] 放課後の迷宮冒険者 第02巻.zip"])
        it = self.kind(w, "[あび×樋辻臥命×かれい] 放課後の迷宮冒険者 第03巻.zip")
        self.assertEqual((it.kind, it.dest), ("追加", "[あび×樋辻臥命×かれい] 放課後の迷宮冒険者"))

    def test_volume_folder_is_not_a_work(self):
        w = self.lib(["[A] 竜馬/[A] 竜馬 第01巻/001.jpg"])
        self.assertEqual([x.path for x in w], ["[A] 竜馬"])
        self.assertEqual(self.kind(w, "[A] 竜馬 第01巻.zip").kind, "要確認")   # 同じ巻（画像フォルダ）が既にある
        self.assertEqual(self.kind(w, "[A] 竜馬 第02巻.zip").kind, "追加")


class ApplyPlan(unittest.TestCase):
    """重複作品の判断 → 計画（tools/make_cleanup_plan.py）→ apply。"""

    def setUp(self):
        sys.path.insert(0, os.path.join(HERE, "tools"))
        import make_cleanup_plan
        self.mcp = make_cleanup_plan
        self.t = tempfile.mkdtemp()
        self.lib = os.path.join(self.t, "SD_comic")
        self.trash = os.path.join(self.t, "_librarian_trash", "x")
        mk(self.lib, "[A] 作品/[A] 作品 第01巻.zip", 10)
        mk(self.lib, "[A] 作品/[A] 作品 第02巻.zip", 20)
        mk(self.lib, "_unread/[A] 作品/[A] 作品 第01巻.zip", 11)
        mk(self.lib, "[B] 本編/[B] 本編 第01巻.zip", 30)
        mk(self.lib, "[B] 本編、続編/[B] 本編、続編 第01巻.zip", 40)
        mk(self.lib, "_A/[C] 物語/[C] 物語 第01巻.zip", 50)
        mk(self.lib, "[C] 物語 外伝/[C] 物語 外伝 第01巻.zip", 60)
        self.decisions = [("1", "残す（統合先）", "（直下）", "[A] 作品"),
                          ("1", "削除", "_unread", "[A] 作品"),
                          ("2", "残す（統合先）", "（直下）", "[B] 本編"),
                          ("2", "統合先へ移して消す", "（直下）", "[B] 本編、続編"),
                          ("3", "残す（統合先）", "_A", "[C] 物語"),
                          ("3", "統合先のサブフォルダへ", "（直下）", "[C] 物語 外伝")]

    def tearDown(self):
        shutil.rmtree(self.t)

    def ops(self):
        dec = os.path.join(self.t, "dec.tsv")
        with open(dec, "w", encoding="utf-8") as f:
            f.write("組\t判断\t場所\t作品フォルダ\n" + "".join("\t".join(r) + "\n" for r in self.decisions))
        lst = os.path.join(self.t, "list.tsv")
        clo.write_list(self.lib, lst)
        dirs, files, _ = clo.load_tsv(lst)
        ops, errors = self.mcp.make_ops(self.mcp.read_decisions(dec), dirs, files)
        self.assertEqual(errors, [])
        return ops

    def test_plan_and_apply(self):
        ops = self.ops()
        self.assertEqual([o.op for o in ops], ["TRASH", "MOVE", "RMDIR", "MOVEDIR"])
        self.assertEqual(clo.check_ops(ops, self.lib, self.trash), [])
        with redirect_stdout(io.StringIO()):
            ok = clo.apply_ops(ops, self.lib, self.trash, os.path.join(self.t, "log.tsv"))
        self.assertEqual(ok, 4)
        self.assertTrue(all(clo.op_done(o, self.lib, self.trash) for o in ops))
        self.assertFalse(os.path.exists(os.path.join(self.lib, "_unread", "[A] 作品")))
        self.assertTrue(os.path.isfile(os.path.join(self.trash, "_unread/[A] 作品/[A] 作品 第01巻.zip")))
        self.assertEqual(sorted(os.listdir(os.path.join(self.lib, "[B] 本編"))),
                         ["[B] 本編 第01巻.zip", "[B] 本編、続編 第01巻.zip"])
        self.assertFalse(os.path.exists(os.path.join(self.lib, "[B] 本編、続編")))
        self.assertTrue(os.path.isfile(os.path.join(self.lib, "_A/[C] 物語/[C] 物語 外伝/[C] 物語 外伝 第01巻.zip")))
        self.assertFalse(os.path.exists(os.path.join(self.lib, "[C] 物語 外伝")))
        # サブフォルダは別の作品として照合される（外伝の新刊は外伝へ）
        d, f = clo.scan(self.lib)
        it = clo.make_plan(clo.build_library(d, f), [("[C] 物語 外伝 第02巻.zip", 1)])[0]
        self.assertEqual((it.kind, it.dest), ("追加", "_A/[C] 物語/[C] 物語 外伝"))

    def test_changed_after_plan_does_nothing(self):
        ops = self.ops()
        mk(self.lib, "_unread/[A] 作品/[A] 作品 第03巻.zip", 5)          # 計画後に増えた
        mk(self.lib, "[B] 本編、続編/メモ.txt", 1)                          # 移す対象以外が入った
        mk(self.lib, "[B] 本編/[B] 本編、続編 第01巻.zip", 99)              # 移動先に同名ができた
        mk(self.lib, "_A/[C] 物語/[C] 物語 外伝/x.zip", 1)                  # サブフォルダの移動先に同名ができた
        self.assertEqual(len(clo.check_ops(ops, self.lib, self.trash)), 4)
        plan = os.path.join(self.t, "cleanup_plan.tsv")
        with open(plan, "w", encoding="utf-8") as f:
            self.mcp.write_plan(ops, self.lib, "list.tsv", f)
        with redirect_stdout(io.StringIO()), mock.patch("builtins.input", return_value="y"):
            rc = clo.main(["apply", plan, "--trash", self.trash, "--out-dir", os.path.join(self.t, "out")])
        self.assertEqual(rc, 2)
        self.assertFalse(os.path.exists(self.trash))
        self.assertEqual(os.path.getsize(os.path.join(self.lib, "[B] 本編/[B] 本編、続編 第01巻.zip")), 99)

    def test_stops_at_first_ng(self):
        ops = self.ops()
        os.remove(os.path.join(self.lib, "_unread/[A] 作品/[A] 作品 第01巻.zip"))   # 1件目が失敗する
        with redirect_stdout(io.StringIO()):
            ok = clo.apply_ops(ops, self.lib, self.trash, os.path.join(self.t, "log.tsv"))
        self.assertEqual(ok, 0)
        self.assertTrue(os.path.isdir(os.path.join(self.lib, "[B] 本編、続編")))     # 2件目以降は実行しない

    def test_merge_name_clash_is_an_error(self):
        mk(self.lib, "[B] 本編/[B] 本編、続編 第01巻.zip", 1)
        dec = os.path.join(self.t, "dec.tsv")
        with open(dec, "w", encoding="utf-8") as f:
            f.write("組\t判断\t場所\t作品フォルダ\n" + "".join("\t".join(r) + "\n" for r in self.decisions))
        dirs, files = clo.scan(self.lib)
        _, errors = self.mcp.make_ops(self.mcp.read_decisions(dec), dirs, files)
        self.assertEqual(len(errors), 1)


class Parse(unittest.TestCase):
    def test_variants(self):
        p = clo.parse_name("[蝉川夏哉×碓井ツカサ] 異世界居酒屋｢げん｣ 第01 14巻")
        self.assertEqual(clo.volumes(p), set(range(1, 15)))
        self.assertEqual(clo.parse_name("[x] 題 第02巻w").v1, 2)
        self.assertEqual(clo.parse_name("[x] 題 第21巻b").v1, 21)          # 本番ライブラリにある b 付き
        self.assertEqual(clo.norm("異世界居酒屋｢げん｣"), clo.norm("異世界居酒屋「げん」"))
        self.assertEqual(clo.parse_name("[あろえ×立山鈴] t").authors, clo.parse_name("[立山鈴×あろえ] t").authors)


if __name__ == "__main__":
    unittest.main()
