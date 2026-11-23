#!/usr/bin/env python3
"""新刊フォルダの書庫ファイルを、ライブラリの作品フォルダへ振り分ける（試作）。

  python librarian.py plan --library LIB --new NEW     計画を表示するだけ（何も動かさない）
  python librarian.py list                             ライブラリを選んで一覧TSVを書き出す
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
import shutil
import subprocess
import sys
import unicodedata
from dataclasses import dataclass, field

ARCHIVE_EXT = {".zip", ".rar", ".cbz", ".cbr", ".7z"}
# 作者の区切り。本番ライブラリに "[采芽杏奈x三島千廣]" のように半角 x で区切った表記があるため、
# 日本語の文字に挟まれた x/X も区切りとみなす（英字名の中の x は区切らない）
AUTHOR_SEP = re.compile(r"[×✕／/、，,&＆]|(?<=[^\x00-\x7F])[xX](?=[^\x00-\x7F])")
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
    akey: str = ""             # 作者表記を区切りなしで連結したもの（"わんた×ちわ小太郎" と "わんたちわ×小太郎" を同一視）


def parse_name(stem: str) -> Parsed:
    m = HEAD_RE.match(stem)
    auth, rest = (m.group("auth"), m.group("rest")) if m else ("", stem)
    parts = [norm(x) for x in AUTHOR_SEP.split(auth)]
    authors = frozenset(a for a in parts if a)
    akey = "".join(parts)
    v = VOL_RE.match(rest)
    if not v:
        return Parsed(authors, norm(rest), akey=akey)
    v2 = None
    if v.group("sep"):
        v2 = int(v.group("v2")) if v.group("v2") else -1
    return Parsed(authors, norm(v.group("title")), int(v.group("v1")), v2, akey)


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
    aliases: list = field(default_factory=list)   # (authors, akey, title)
    files: list = field(default_factory=list)     # (name, size, Parsed)


def build_library(dirs, files):
    """dirs: 相対パスのリスト、files: (相対パス, サイズ) のリスト（どちらもライブラリ基準）。"""
    works, vol_dirs = {}, []
    for rel in dirs:
        name = rel.rsplit("/", 1)[-1]
        if name.startswith("["):                     # 作品フォルダ（入れ子の外伝も作品）
            p = parse_name(name)
            if p.v1 is not None:                     # 「… 第01巻」のような巻フォルダ（画像を展開した巻）
                vol_dirs.append((rel, name, p))
            else:
                works[rel] = Work(rel, [(p.authors, p.akey, p.title)])
    for rel, name, p in vol_dirs:                    # 巻フォルダは親作品の巻として重複確認に使う
        parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
        if parent in works:
            works[parent].files.append((name + "/", -1, p))
    for rel, size in files:
        if "/" not in rel:
            continue
        parent, name = rel.rsplit("/", 1)
        stem, ext = split_ext(name)
        if parent in works and ext in ARCHIVE_EXT:   # 作品フォルダ直下のみ（temp 等は対象外）
            p = parse_name(stem)
            w = works[parent]
            w.files.append((name, size, p))
            if (p.authors, p.akey, p.title) not in w.aliases:
                w.aliases.append((p.authors, p.akey, p.title))
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


def author_overlap(a, b, ka="", kb=""):
    """少ない方の作者が全員含まれていれば 1.0。区切り位置だけ違う表記（連結すると同じ）も 1.0。"""
    if not a or not b:
        return None
    if ka and ka == kb:
        return 1.0
    return len(a & b) / min(len(a), len(b))


def score_work(p, w):
    best = (0.0, None)
    for authors, akey, title in w.aliases:
        cur = (title_score(p.title, title), author_overlap(p.authors, authors, p.akey, akey))
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
        # 競合：タイトルが含む関係以上で、作者の一致度が1位と同等以上の候補
        # （同じ原作の別作画のように作者が一部しか重ならない候補は競合にしない）
        rivals = [c for c in cands[1:] if c[0][0] >= TITLE_CONTAIN and (c[0][1] or 0.0) >= (ao or 0.0)]
        # 作者が一部しか重ならない（例：同じ原作の別作画）ときは自動で入れない
        if not (ts == TITLE_SURE and (ao is None or ao >= 1.0) and not rivals):
            why = ("候補が複数" if rivals else
                   "タイトル一致・作者が一部のみ一致" if ts == TITLE_SURE and ao else
                   "タイトル一致だが作者不一致" if ts == TITLE_SURE else
                   "タイトルの一方が他方を含む" if ts >= TITLE_CONTAIN else "タイトルが類似のみ")
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


def show_plan(items, out=None):
    out = out or sys.stdout          # 呼び出し時点の sys.stdout（記録用の Tee）に書く
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


def mb(n):
    return f"{n / 1024 / 1024:,.1f}MB"


def execute(items, lib, new, log_path, progress=None):
    """progress: 端末で実行中なら rsync の進捗（転送量・%・速度・残り時間）を表示する。"""
    if progress is None:
        progress = sys.stdout.isatty()
    todo = [i for i in items if i.kind in ("追加", "新規", "単巻")]
    total = sum(i.size for i in todo) or 1
    done = ok = ng = 0
    # 移動中に画面が消えても止まらないように Termux のウェイクロックを取る
    # （Galaxy の電池最適化の下で効くかは未確認。手順4.5で確認する）
    wake = progress and shutil.which("termux-wake-lock")
    if wake:
        subprocess.run(["termux-wake-lock"])
    try:
        with open(log_path, "w", encoding="utf-8") as log:
            log.write("時刻\t結果\t判定\t元\t移動先\tサイズ\t備考\n")
            for n, i in enumerate(todo, 1):
                print(f"[{n}/{len(todo)}] {i.name}（{mb(i.size)}）  全体 {done * 100 // total}%"
                      f"（{mb(done)} / {mb(total)}）", flush=True)
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
                    cmd = ["rsync", "-r", "--ignore-existing", "--remove-source-files"]
                    cmd += (["--info=progress2"] if progress else []) + [src, ddir + "/"]
                    r = subprocess.run(cmd, stdout=None if progress else subprocess.DEVNULL,
                                       stderr=subprocess.PIPE, text=True)
                    moved = (os.path.isfile(dst) and os.path.getsize(dst) == i.size
                             and not os.path.exists(src))
                    result = "OK" if r.returncode == 0 and moved else "NG"
                    note = "" if result == "OK" else (r.stderr.strip() or "移動後の確認に失敗")[:200]
                ok += result == "OK"
                ng += result == "NG"
                done += i.size
                log.write(f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}\t{result}\t{i.kind}\t{src}\t{dst}\t{i.size}\t{note}\n")
                print(f"  {result}" + (f"  … {note}" if note else ""), flush=True)
    finally:
        if wake:
            subprocess.run(["termux-wake-unlock"])
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


# ---------------------------------------------------------------- 一覧の書き出し
SD_RE = re.compile(r"/storage/[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}")
SKIP_DIRS = {"Android", "LOST.DIR"}


def detect_sd(link="~/storage/external-1", mounts="/proc/mounts"):
    """SD カードのルートを得る。termux-setup-storage のリンクが無い端末（Galaxy など）では
    /proc/mounts から探す（tools/check_storage.sh と同じ方法）。"""
    try:
        m = SD_RE.match(os.readlink(os.path.expanduser(link)))
        if m:
            return m.group(0)
    except OSError:
        pass
    try:
        with open(mounts, encoding="utf-8") as f:
            m = SD_RE.search(f.read())
        return m.group(0) if m else None
    except OSError:
        return None


def candidate_dirs(roots, depth=2):
    """各ルートの下、depth 階層までのフォルダ（隠し・Android・LOST.DIR は除く）。"""
    out = []
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        stack = [(root, 0)]
        while stack:
            d, lv = stack.pop()
            try:
                names = sorted(os.listdir(d), reverse=True)
            except OSError:
                continue
            for n in names:
                p = os.path.join(d, n)
                if os.path.isdir(p) and not n.startswith(".") and n not in SKIP_DIRS:
                    if lv + 1 <= depth:
                        out.append((lv + 1, p))
                    if lv + 1 < depth:
                        stack.append((p, lv + 1))
    return sorted(p for _, p in out)


def pick_library(inp=input, roots=None):
    roots = roots or [detect_sd(), "/sdcard"]
    cands = candidate_dirs(roots)
    for k, p in enumerate(cands, 1):
        print(f"  {k}: {p}")
    ans = inp("ライブラリのフォルダ（番号、または絶対パス）> ").strip()
    if ans.isdigit() and 1 <= int(ans) <= len(cands):
        return cands[int(ans) - 1]
    if not os.path.isdir(ans):
        # 入力したパスが無いとき、同じ名前のフォルダを候補から探して示す
        same = [p for p in candidate_dirs(roots, depth=3) if os.path.basename(p) == os.path.basename(ans.rstrip("/"))]
        if same:
            print("見つかりません。同じ名前のフォルダ:\n  " + "\n  ".join(same))
            if len(same) == 1 and inp(f"{same[0]} を使いますか？ [y/N] ").strip().lower() == "y":
                return same[0]
    return ans


def write_list(lib, out):
    """ライブラリのフォルダ・ファイル一覧を load_tsv と同じ形式で書き出す（中身は読まない）。"""
    dirs, files = scan(lib)
    with open(out, "w", encoding="utf-8") as f:
        f.write(f"# library: {lib}\n")
        for d in sorted(dirs):
            f.write(f"d\t0\t-\tLIBRARY/{d}\n")
        for rel, size in sorted(files):
            f.write(f"f\t{size}\t-\tLIBRARY/{rel}\n")
    return len(dirs), len(files), sum(s for _, s in files)


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


class Tee:
    """画面に出す内容を、同時に記録ファイルにも書く（引き渡し用）。"""

    def __init__(self, stream, path):
        self.stream, self.file = stream, open(path, "a", encoding="utf-8")

    def write(self, text):
        self.stream.write(text)
        self.file.write(text)
        return len(text)

    def flush(self):
        self.stream.flush()
        self.file.flush()

    def isatty(self):
        return self.stream.isatty()

    def note(self, text):
        """入力された回答など、画面には既に出ているものを記録ファイルにだけ書く。"""
        self.file.write(text + "\n")
        self.file.flush()


def default_out_dir():
    # Termux のホームはファイルアプリから見えないため、引き渡しやすい Download に置く
    base = "/sdcard/Download" if os.path.isdir("/sdcard/Download") else os.getcwd()
    return os.path.join(base, "librarian")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["plan", "run", "list"])
    ap.add_argument("--library")
    ap.add_argument("--new")
    ap.add_argument("--tsv")
    ap.add_argument("--out-dir", default=default_out_dir(),
                    help="一覧・画面の記録・移動ログの出力先（既定: /sdcard/Download/librarian）")
    a = ap.parse_args(argv)
    if a.tsv:
        if a.cmd != "plan":
            ap.error("--tsv は plan 専用です")
        dirs, files, newf = load_tsv(a.tsv)
        show_plan(make_plan(build_library(dirs, files), newf))
        return 0
    os.makedirs(a.out_dir, exist_ok=True)
    stamp = f"{dt.datetime.now():%Y%m%d-%H%M%S}"
    tee = Tee(sys.stdout, os.path.join(a.out_dir, f"{a.cmd}_{stamp}.txt"))
    sys.stdout = tee

    def inp(prompt):
        ans = input(prompt)
        tee.note(ans)
        return ans

    try:
        return run_command(a, ap, stamp, inp)
    finally:
        sys.stdout = tee.stream
        print(f"（画面の記録: {tee.file.name}）")
        tee.file.close()


def run_command(a, ap, stamp, inp):
    if a.cmd == "list":
        lib = a.library or pick_library(inp)
        if not os.path.isdir(lib):
            ap.error(f"フォルダが見つかりません: {lib}")
        out = os.path.join(a.out_dir, f"list_{stamp}.tsv")
        nd, nf, size = write_list(lib, out)
        print(f"ライブラリ: {lib}\nフォルダ {nd}件・ファイル {nf}件・合計 {mb(size)}\n一覧: {out}")
        return 0
    if not (a.library and a.new):
        ap.error("--library と --new を指定してください")
    print(f"ライブラリ: {a.library}\n新刊フォルダ: {a.new}")
    dirs, files = scan(a.library)
    items = make_plan(build_library(dirs, files), new_files_in(a.new))
    show_plan(items)
    if a.cmd == "plan":
        return 0
    ask_review(items, inp)
    print("\n===== 実行内容 =====")
    show_plan(items)
    if inp("\nこの内容で移動しますか？ [y/N] ").strip().lower() != "y":
        print("中止しました（何も動かしていません）")
        return 1
    log_path = os.path.join(a.out_dir, f"run_{stamp}.tsv")
    print("\n===== 移動 =====")
    ok, ng = execute(items, a.library, a.new, log_path)
    problems = verify(items, a.library, a.new)
    print(f"\n===== 結果 =====\n移動 OK {ok}件 / NG {ng}件\n移動ログ: {log_path}")
    print("照合: 計画どおり" if not problems else "照合で不一致:\n  " + "\n  ".join(problems))
    left = sorted(n for n, _ in new_files_in(a.new))
    if left:
        print(f"新刊フォルダに残したもの（{len(left)}件）:\n  " + "\n  ".join(left))
    return 0 if ng == 0 and not problems else 2


if __name__ == "__main__":
    sys.exit(main())
