#!/usr/bin/env python3
"""重複作品シートの判断から、ライブラリ整理の計画TSV（librarian.py apply 用）を作る。ファイルは動かさない。

  python tools/make_cleanup_plan.py 判断.tsv 一覧.tsv > cleanup_plan.tsv

判断.tsv：組・判断・場所・作品フォルダ（見出し行つき。場所は「（直下）」か "_A" などの特別フォルダ）
  残す（統合先）       … 各組に1つだけ
  削除                 … フォルダごとゴミ箱へ（TRASH）
  統合先へ移して消す   … 中のファイルを統合先へ移し（MOVE）、空になったフォルダを消す（RMDIR）
一覧.tsv：librarian.py list の出力（1行目の "# library:" を計画に引き継ぐ）
"""
import collections
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import librarian as L  # noqa: E402

KEEP, TRASH, MERGE = "残す（統合先）", "削除", "統合先へ移して消す"


def read_decisions(path):
    groups = collections.OrderedDict()
    with open(path, encoding="utf-8") as f:
        rows = [ln.rstrip("\n").split("\t") for ln in f if ln.strip()][1:]
    for g, dec, place, name in rows:
        rel = name if place == "（直下）" else f"{place}/{name}"
        groups.setdefault(g, []).append((dec, rel))
    return groups


def make_ops(groups, dirs, files):
    dirset = set(dirs)
    under = collections.defaultdict(list)          # フォルダ → その下の全ファイル（相対パス, サイズ）
    for rel, size in files:
        parts = rel.split("/")
        for k in range(1, len(parts)):
            under["/".join(parts[:k])].append(("/".join(parts[k:]), size))
    ops, errors = [], []
    for g, rows in groups.items():
        keep = [rel for dec, rel in rows if dec == KEEP]
        if len(keep) != 1:
            errors.append(f"組{g}: 「{KEEP}」が{len(keep)}件")
            continue
        tgt = keep[0]
        have = {n for n, _ in under[tgt]}
        for dec, rel in rows:
            if dec == KEEP:
                continue
            memo = f"組{g} {dec}（統合先: {tgt}）"
            if rel not in dirset:
                errors.append(f"組{g}: 一覧に無いフォルダ: {rel}")
            elif dec == TRASH:
                ops.append(L.Op("TRASH", rel, "", len(under[rel]), sum(s for _, s in under[rel]), memo))
            elif dec == MERGE:
                if any(d.startswith(rel + "/") for d in dirset):
                    errors.append(f"組{g}: サブフォルダがあるため手で確認してください: {rel}")
                    continue
                for n, s in sorted(under[rel]):
                    if n in have:
                        errors.append(f"組{g}: 統合先に同名がある: {n}")
                    ops.append(L.Op("MOVE", f"{rel}/{n}", f"{tgt}/{n}", 1, s, memo))
                ops.append(L.Op("RMDIR", rel, "", 0, 0, memo))
            else:
                errors.append(f"組{g}: 不明な判断: {dec}")
    return ops, errors


def write_plan(ops, lib, source, out):
    out.write("# comic-librarian 整理計画（tools/make_cleanup_plan.py で作成。実行は librarian.py apply）\n")
    out.write(f"# library: {lib}\n# 元の一覧: {source}\n")
    out.write("# 操作\t対象\t移動先\tファイル数\tバイト数\tメモ\n")
    for o in ops:
        out.write(f"{o.op}\t{o.src}\t{o.dst}\t{o.count}\t{o.size}\t{o.memo}\n")


def main(argv):
    dec_path, list_path = argv
    with open(list_path, encoding="utf-8") as f:
        first = f.readline()
    lib = first.split(":", 1)[1].strip() if first.startswith("# library:") else ""
    dirs, files, _ = L.load_tsv(list_path)
    ops, errors = make_ops(read_decisions(dec_path), dirs, files)
    if errors:
        sys.stderr.write("計画を作れません:\n  " + "\n  ".join(errors) + "\n")
        return 2
    write_plan(ops, lib, os.path.basename(list_path), sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
