#!/usr/bin/env python3
"""ライブラリ一覧（librarian.py list の出力）で照合の精度を測る。ファイルは一切動かさない。

  python tools/evaluate.py 一覧.tsv

1. 既存作品：各作品の最終巻を新刊と見なしてライブラリから外し、元の作品に戻せるか
2. 未登録作品：作品を丸ごと（同名の別置きフォルダ・親子の外伝も）外し、その最終巻を
   別作品へ自動で入れてしまわないか
"""
import collections
import copy
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import librarian as L  # noqa: E402


def last_volume(w):
    arch = [(n, s, p) for n, s, p in w.files if p.v1 is not None and s >= 0]
    return max(arch, key=lambda x: (x[2].v1, x[0])) if arch else None


def existing_works(works):
    res, ex = collections.Counter(), collections.defaultdict(list)
    for w in works:
        if len([f for f in w.files if f[2].v1 is not None and f[1] >= 0]) < 2:
            continue
        n, s, _ = last_volume(w)
        w2 = copy.copy(w)                      # その1冊を抜いた作品（変わるのはこの作品だけ）
        w2.files = [f for f in w.files if f[0] != n]
        w2.aliases = [w.aliases[0]] + list(dict.fromkeys((f[2].authors, f[2].akey, f[2].title) for f in w2.files))
        it = L.make_plan([w2 if x is w else x for x in works], [(n, s)])[0]
        if it.kind == "追加":
            k = "自動で正しい作品へ" if it.dest == w.path else "自動で別の作品へ（誤り）"
        elif it.kind == "移動不要" or it.existing:
            k = "同じ巻が既にある"
        elif it.kind == "要確認":
            k = "要確認（候補に正解あり）" if any(c.path == w.path for c in it.cands) else "要確認（候補に正解なし）"
        else:
            k = f"見落とし（{it.kind}）"
        res[k] += 1
        ex[k].append(f"{n} → {it.dest}（正解 {w.path}）{it.reason}")
    return res, ex


def unknown_works(works):
    res, ex = collections.Counter(), collections.defaultdict(list)
    for w in works:
        lv = last_volume(w)
        if not lv:
            continue
        n, s, _ = lv
        base = os.path.basename(w.path)
        lib = [x for x in works if x is not w and os.path.basename(x.path) != base
               and not x.path.startswith(w.path + "/") and not w.path.startswith(x.path + "/")]
        it = L.make_plan(lib, [(n, s)])[0]
        k = "自動で別の作品へ（要確認：同じ作品の別表記フォルダか）" if it.kind in ("追加", "移動不要") else it.kind
        res[k] += 1
        ex[k].append(f"{n} → {it.dest} {it.reason}")
    return res, ex


def report(title, res, ex, show):
    total = sum(res.values()) or 1
    print(f"\n===== {title}（{total}件）")
    for k, v in sorted(res.items(), key=lambda x: -x[1]):
        print(f"  {k}: {v}（{v * 100 / total:.1f}%）")
    for k in show:
        for line in ex.get(k, [])[:20]:
            print(f"    [{k}] {line}")


def main():
    dirs, files, _ = L.load_tsv(sys.argv[1])
    works = L.build_library(dirs, files)
    print(f"作品フォルダ {len(works)}件")
    report("1. 既存作品の最終巻を戻せるか", *existing_works(works),
           show=["自動で別の作品へ（誤り）", "要確認（候補に正解なし）"])
    report("2. 未登録作品を別作品へ入れないか", *unknown_works(works),
           show=["自動で別の作品へ（要確認：同じ作品の別表記フォルダか）"])


if __name__ == "__main__":
    main()
