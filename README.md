# comic-librarian

個人用のコミック書庫（ZIP/RAR）管理ツール。新刊フォルダの書庫を、ライブラリの作品フォルダへ振り分ける。
Android タブレット上の Termux で動かす（Python 標準ライブラリ＋rsync）。

## 使い方（Termux）

```bash
pkg install -y python rsync git gh
gh auth login
gh repo clone bax-naka/comic-librarian ~/comic-librarian && cd ~/comic-librarian
python librarian.py plan --library <ライブラリ> --new <新刊フォルダ>   # 計画を表示するだけ
python librarian.py run  --library <ライブラリ> --new <新刊フォルダ>   # 要確認を選択 → y で移動 → 自動照合
python librarian.py list                                                # ライブラリを選んで一覧TSVを書き出す（中身は読まない）
python librarian.py apply                                               # ライブラリ整理の計画を確認 → y で実行 → 自動照合
```

- `run` は要確認の新刊を作品（作者・作品名が同じもの）ごとにまとめて1回だけ選ばせ、その作品の全巻に当てはめる（候補番号／`n`＝新規作品として直下へ／Enter＝見送り）。
  候補へ追加を選んでも、その作品に同じ巻がある巻・同名がある巻は追加せず新刊フォルダに残す（「同じ巻が既にある」の組では同名の巻だけ残す）
- 移動中は件数・サイズ・全体の%と、rsync の進捗（転送量・%・速度・残り時間）を表示
- 出力はすべて `/sdcard/Download/librarian/` に置く（ファイルアプリから見え、そのまま引き渡せる）
  - `plan_日時.txt` / `run_日時.txt` / `list_日時.txt`：画面の記録（入力した回答も含む）
  - `run_日時.tsv`：移動ログ（日時・結果・判定・元・移動先・サイズ）
  - `list_日時.tsv`：ライブラリの一覧
  - `apply_日時.txt` / `apply_日時.tsv`：整理の画面の記録と実行ログ
  - `check_日時.log`：`tools/check_storage.sh` の記録

`apply` は `tools/make_cleanup_plan.py` が作った計画（`cleanup_plan*.tsv`）を実行する。計画ファイルを省略すると
`/sdcard/Download`（と1階層下）の最も新しいものを使う。実行前に全件を確認し、計画時の一覧と1件でも食い違えば何もしない。
「削除」はゴミ箱（ライブラリと同じ階層の `_librarian_trash/日時/`）へ移すだけで、消すのは利用者がファイル管理アプリで行う。

端末の事前確認（ライブラリには触れない）：`bash tools/check_storage.sh`（`--sleep` で画面を消しても止まらないかも確認）

## 判定

| 判定 | 動作 |
|---|---|
| 追加 | 既存の作品フォルダへ移動 |
| 新規 | ライブラリ直下に作品フォルダを作って移動 |
| 単巻 | 巻番号なし・該当作品なし → `_single` へ移動 |
| 要確認 | 実行時に選択（見送りなら新刊フォルダに残す） |
| 移動不要 | 同じ巻・同じサイズが既にある → 新刊フォルダに残す（確認後に手で削除） |

## 安全基準

1. 上書きしない（計画時と実行直前に移動先の同名を確認、あればその項目は実行しない）
2. 移動は rsync（一時名で書き込み→付け替え→成功後に元を削除）。確認は一般的なファイル管理アプリ並み
3. 1件ごとにログを残す
4. これ以上の仕組み（SHA照合・ジャーナル・自動再開など）は作らない
5. ライブラリ内の整理（`apply`）は同じSDカード内の名前の付け替え（中身をコピーしない）。削除はせずゴミ箱フォルダへ移す。
   実行前に全件の有無・サイズ・移動先の同名を確認し、実行中は NG が出た時点で止める

## 試験

```bash
python -m unittest
```

計画と進め方は [docs/plan.md](docs/plan.md)。
