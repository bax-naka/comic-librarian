#!/usr/bin/env python3
"""新刊フォルダの書庫ファイルを、ライブラリの作品フォルダへ振り分ける（試作）。

  python librarian.py plan --library LIB --new NEW     計画を表示するだけ（何も動かさない）
  python librarian.py run  --library LIB --new NEW     要確認を対話で選び、確認後に移動して照合する
  python librarian.py plan --tsv LIST.tsv              一覧TSV（LIBRARY/, NEW/ で始まる相対パス）から計画だけ作る

標準ライブラリ＋rsync のみ使用（Termux 想定）。
安全の基準：上書きしない／移動は rsync（一時名→付け替え→成功後に元を削除）／1件ごとにログを残す。
"""
import argparse
import datetime as dt
import difflib
import os
import re
import subprocess
import sys
import unicodedata
from dataclasses import dataclass, field

ARCHIVE_EXT = {".zip", ".rar", ".cbz", ".cbr", ".7z"}
AUTHOR_SEP = re.compile(r"[×✕／/、，,&＆]")
HEAD_RE = re.compile(r"^\s*\[(?P<auth>[^\]]*)\]\s*(?P<rest>.*)$")
VOL_RE = re.compile(
    r"^(?P<title>.*?)\s*第\s*(?P<v1>\d+)\s*(?:(?P<sep>[-~～〜\s])\s*(?P<v2>\d*))?\s*巻?\s*(?P<sfx>[wsWS])?\s*$")
SINGLE_DIR = "_single"

# 判定のしきい値（暫定。本番ライブラリの一覧で調整する）
TITLE_SURE = 1.0      # 正規化後の完全一致
TITLE_CONTAIN = 0.9   # 片方がもう片方を含む（サブタイトル抜けなど）
TITLE_MIN = 0.6       # これ未満は候補にしない

ORDER = ["追加", "新規", "単巻", "要確認", "移動不要", "除外"]


# ---------------------------------------------------------------- 名前の解析
def norm(s: str) -> str:
    """比較用：NFKC（全角/半角・括弧の統一）→ 文字と数字だけ残す → 小文字。"""
    s = unicodedata.normalize("NFKC", s).replace("〜", "~")
    return "".join(ch for ch in s if unicodedata.category(ch)[0] in "LN").lower()


@dataclass
class Parsed:
    authors: frozenset
    title: str                 # 正規化済み
    v1: int | None = None
    v2: int | None = None      # まとめ巻の終わり（"第10-" のような開いた範囲は -1）


def parse_name(stem: str) -> Parsed:
    m = HEAD_RE.match(stem)
    auth, rest = (m.group("auth"), m.group("rest")) if m else ("", stem)
    authors = frozenset(a for a in (norm(x) for x in AUTHOR_SEP.split(auth)) if a)
    v = VOL_RE.match(rest)
    if not v:
        return Parsed(authors, norm(rest))
    v2 = None
    if v.group("sep"):
        v2 = int(v.group("v2")) if v.group("v2") else -1
    return Parsed(authors, norm(v.group("title")), int(v.group("v1")), v2)


def volumes(p: Parsed) -> set:
    if p.v1 is None:
        return set()
    end = p.v1 if p.v2 in (None, -1) else p.v2
    return set(range(p.v1, end + 1))


def work_folder_name(stem: str) -> str:
    """新規作品のフォルダ名：ファイル名から巻表記より前を取る。"""
    return re.split(r"\s*第\s*\d", stem)[0].strip().rstrip(".")


def split_ext(name):
    root, ext = os.path.splitext(name)
    return root, ext.lower()


# ---------------------------------------------------------------- ライブラリ
@dataclass
class Work:
    path: str                                     # ライブラリ内の相対パス
    aliases: list = field(default_factory=list)   # (authors, title)
    files: list = field(default_factory=list)     # (name, size, Parsed)


def build_library(dirs, files):
    """dirs: 相対パスのリスト、files: (相対パス, サイズ) のリスト（どちらもライブラリ基準）。"""
    works = {}
    for rel in dirs:
        name = rel.rsplit("/", 1)[-1]
        if name.startswith("["):                     # 作品フォルダ（入れ子の外伝も作品）
            p = parse_name(name)
            works[rel] = Work(rel, [(p.authors, p.title)])
    for rel, size in files:
        if "/" not in rel:
            continue
        parent, name = rel.rsplit("/", 1)
        stem, ext = split_ext(name)
        if parent in works and ext in ARCHIVE_EXT:   # 作品フォルダ直下のみ（temp 等は対象外）
            p = parse_name(stem)
            w = works[parent]
            w.files.append((name, size, p))
            if (p.authors, p.title) not in w.aliases:
                w.aliases.append((p.authors, p.title))
    return list(works.values())


def title_score(a, b):
    if not a or not b:
        return 0.0
    if a == b:
        return TITLE_SURE
    short, long_ = sorted((a, b), key=len)
    if len(short) >= 4 and short in long_:
        return TITLE_CONTAIN
    return difflib.SequenceMatcher(None, a, b).ratio()


def author_overlap(a, b):
    if not a or not b:
        return None
    return len(a & b) / min(len(a), len(b))


def score_work(p, w):
    best = (0.0, None)
    for authors, title in w.aliases:
        cur = (title_score(p.title, title), author_overlap(p.authors, authors))
        if (cur[0], cur[1] or 0.0) > (best[0], best[1] or 0.0):
            best = cur
    return best


# ---------------------------------------------------------------- 計画
@dataclass
class Item:
    kind: str
    name: str
    size: int
    dest: str = ""            # 移動先フォルダ（ライブラリ基準の相対パス、"" は直下）
    reason: str = ""
    cands: list = field(default_factory=list)      # 要確認の候補 Work
    existing: list = field(default_factory=list)   # 同じ巻の既存ファイル名


def make_plan(works, new_files):
    items, new_works = [], {}
    for name, size in sorted(new_files):
        stem, ext = split_ext(name)
        if ext not in ARCHIVE_EXT:
            items.append(Item("除外", name, size, reason="書庫ファイルではない"))
            continue
        p = parse_name(stem)
        scored = sorted(((score_work(p, w), w) for w in works),
                        key=lambda x: (x[0][0], x[0][1] or 0.0), reverse=True)
        cands = [(s, w) for s, w in scored if s[0] >= TITLE_MIN][:3]
        if not cands:
            if p.v1 is None:
                items.append(Item("単巻", name, size, SINGLE_DIR, "巻番号なし・該当作品なし"))
            else:
                key = (p.authors, p.title)
                folder = new_works.setdefault(key, work_folder_name(stem))
                items.append(Item("新規", name, size, folder, "該当作品なし（ライブラリ直下に作成）"))
            continue
        (ts, ao), w = cands[0]
        rivals = [c for c in cands[1:] if c[0][0] >= TITLE_CONTAIN]
        if not (ts == TITLE_SURE and (ao is None or ao > 0) and not rivals):
            why = ("候補が複数" if rivals else
                   "タイトル一致だが作者不一致" if ts == TITLE_SURE else "タイトルが類似のみ")
            items.append(Item("要確認", name, size, w.path, why, [c[1] for c in cands]))
            continue
        vols = volumes(p)
        same = [(n, s) for n, s, fp in w.files if vols & volumes(fp)]
        if same:
            if len(vols) == 1 and any(s == size for _, s in same):
                items.append(Item("移動不要", name, size, w.path, "同じ巻・同じサイズが既にある",
                                  existing=[n for n, _ in same]))
            else:
                items.append(Item("要確認", name, size, w.path, f"同じ巻が既にある（{len(same)}件）",
                                  [w], [n for n, _ in same]))
            continue
        items.append(Item("追加", name, size, w.path, "タイトル一致"))
    items.sort(key=lambda i: (ORDER.index(i.kind), i.dest, i.name))
    return items


def show_plan(items, out=sys.stdout):
    for kind in ORDER:
        group = [i for i in items if i.kind == kind]
        if not group:
            continue
        out.write(f"\n■ {kind}（{len(group)}件）\n")
        for i in group:
            dest = f" → {i.dest or '（直下）'}" if kind in ("追加", "新規", "単巻") else ""
            out.write(f"  {i.name}{dest}\n")
            if kind in ("要確認", "移動不要"):
                out.write(f"      理由: {i.reason}\n")
                for n in i.existing[:3]:
                    out.write(f"      既存: {n}\n")
                for k, w in enumerate(i.cands, 1):
                    out.write(f"      候補{k}: {w.path}\n")


# ---------------------------------------------------------------- 実行
def ask_review(items, inp=input):
    """要確認を1件ずつ選ばせる。Enter は見送り。"""
    for i in [x for x in items if x.kind == "要確認"]:
        print(f"\n[要確認] {i.name}\n  理由: {i.reason}")
        for n in i.existing[:5]:
            print(f"  既存: {n}")
        opts = {}
        dup = bool(i.existing)
        for k, w in enumerate(i.cands, 1):
            if dup and any(n == i.name for n in i.existing):
                continue                                      # 同名があるなら追加できない
            label = "この作品に追加（既存も残す）" if dup else "この作品に追加"
            opts[str(k)] = ("追加", w.path)
            print(f"  {k}: {label} … {w.path}")
        if not dup:
            opts["n"] = ("新規", work_folder_name(split_ext(i.name)[0]))
            print(f"  n: 新規作品としてライブラリ直下へ … {opts['n'][1]}")
        print("  Enter: 見送る（新刊フォルダに残す）")
        ans = inp("  選択> ").strip().lower()
        if ans in opts:
            i.kind, i.dest = opts[ans]
            i.reason = "要確認で選択"
        else:
            i.kind, i.reason = "見送り", "要確認で見送り"


def scan(root):
    dirs, files = [], []
    for dp, dns, fns in os.walk(root):
        for d in dns:
            dirs.append(os.path.relpath(os.path.join(dp, d), root).replace(os.sep, "/"))
        for f in fns:
            p = os.path.join(dp, f)
            files.append((os.path.relpath(p, root).replace(os.sep, "/"), os.path.getsize(p)))
    return dirs, files


def new_files_in(new_root):
    return [(f, os.path.getsize(os.path.join(new_root, f))) for f in os.listdir(new_root)
            if os.path.isfile(os.path.join(new_root, f))]


def execute(items, lib, new, log_path):
    todo = [i for i in items if i.kind in ("追加", "新規", "単巻")]
    ok = ng = 0
    with open(log_path, "w", encoding="utf-8") as log:
        log.write("時刻\t結果\t判定\t元\t移動先\tサイズ\t備考\n")
        for i in todo:
            src = os.path.join(new, i.name)
            ddir = os.path.join(lib, i.dest) if i.dest else lib
            dst = os.path.join(ddir, i.name)
            note = ""
            if not os.path.isfile(src) or os.path.getsize(src) != i.size:
                result, note = "NG", "新刊が見つからない／サイズが計画時と違う"
            elif os.path.exists(dst):
                result, note = "NG", "移動先に同名がある（上書きしない）"
            else:
                os.makedirs(ddir, exist_ok=True)
                r = subprocess.run(["rsync", "-r", "--ignore-existing", "--remove-source-files",
                                    src, ddir + "/"], capture_output=True, text=True)
                moved = (os.path.isfile(dst) and os.path.getsize(dst) == i.size
                         and not os.path.exists(src))
                result = "OK" if r.returncode == 0 and moved else "NG"
                note = "" if result == "OK" else (r.stderr.strip() or "移動後の確認に失敗")[:200]
            ok += result == "OK"
            ng += result == "NG"
            log.write(f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}\t{result}\t{i.kind}\t{src}\t{dst}\t{i.size}\t{note}\n")
            print(f"  {result} {i.name}" + (f"  … {note}" if note else ""))
    return ok, ng


def verify(items, lib, new):
    """実行後にもう一度スキャンし、計画どおりの場所にあるか・残るべきものが残っているかを照合。"""
    problems = []
    remaining = {n for n, _ in new_files_in(new)}
    for i in items:
        if i.kind in ("追加", "新規", "単巻"):
            dst = os.path.join(lib, i.dest, i.name) if i.dest else os.path.join(lib, i.name)
            if not (os.path.isfile(dst) and os.path.getsize(dst) == i.size):
                problems.append(f"移動先にない: {i.name}")
            if i.name in remaining:
                problems.append(f"新刊フォルダに残っている: {i.name}")
        elif i.name not in remaining:
            problems.append(f"残るはずが消えている: {i.name}")
    return problems


# ---------------------------------------------------------------- 入口
def load_tsv(path, lib="LIBRARY", new="NEW"):
    dirs, files, newf = [], [], []
    with open(path, encoding="utf-8") as fh:
        lines = [ln for ln in fh if ln.strip() and not ln.startswith("#")]
    for line in lines:
        t, size, _d, rel = line.rstrip("\n").split("\t", 3)
        if rel.startswith(lib + "/"):
            r = rel[len(lib) + 1:]
            (dirs.append(r) if t == "d" else files.append((r, int(size))))
        elif rel.startswith(new + "/") and t == "f" and "/" not in rel[len(new) + 1:]:
            newf.append((rel[len(new) + 1:], int(size)))
    return dirs, files, newf


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["plan", "run"])
    ap.add_argument("--library")
    ap.add_argument("--new")
    ap.add_argument("--tsv")
    ap.add_argument("--log-dir", default=os.path.expanduser("~/librarian_logs"))
    a = ap.parse_args(argv)
    if a.tsv:
        if a.cmd != "plan":
            ap.error("--tsv は plan 専用です")
        dirs, files, newf = load_tsv(a.tsv)
    else:
        if not (a.library and a.new):
            ap.error("--library と --new を指定してください")
        dirs, files = scan(a.library)
        newf = new_files_in(a.new)
    items = make_plan(build_library(dirs, files), newf)
    show_plan(items)
    if a.cmd == "plan":
        return 0
    ask_review(items)
    print("\n===== 実行内容 =====")
    show_plan(items)
    if input("\nこの内容で移動しますか？ [y/N] ").strip().lower() != "y":
        print("中止しました（何も動かしていません）")
        return 1
    os.makedirs(a.log_dir, exist_ok=True)
    log_path = os.path.join(a.log_dir, f"{dt.datetime.now():%Y%m%d-%H%M%S}.tsv")
    print("\n===== 移動 =====")
    ok, ng = execute(items, a.library, a.new, log_path)
    problems = verify(items, a.library, a.new)
    print(f"\n===== 結果 =====\n移動 OK {ok}件 / NG {ng}件\nログ: {log_path}")
    print("照合: 計画どおり" if not problems else "照合で不一致:\n  " + "\n  ".join(problems))
    left = sorted(n for n, _ in new_files_in(a.new))
    if left:
        print(f"新刊フォルダに残したもの（{len(left)}件）:\n  " + "\n  ".join(left))
    return 0 if ng == 0 and not problems else 2


if __name__ == "__main__":
    sys.exit(main())
