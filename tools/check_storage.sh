#!/data/data/com.termux/files/usr/bin/bash
# 端末の事前確認（ライブラリには触れない）。SDカード直下に clo_check、Download に clo_check を作り、最後に消す。
#   bash tools/check_storage.sh          … 書き込み・移動・上書き防止・中断の確認
#   bash tools/check_storage.sh --sleep  … 上記に加え、画面を消しても処理が止まらないかを確認
# 画面の内容は /sdcard/Download/librarian/check_日時.log にも記録する（引き渡し用）
OUT=/sdcard/Download/librarian; mkdir -p "$OUT"
LOG="$OUT/check_$(date +%Y%m%d-%H%M%S).log"
exec > >(tee -a "$LOG") 2>&1
echo "== 環境"
echo "Android $(getprop ro.build.version.release) / $(getprop ro.product.model)"
termux-info 2>/dev/null | grep -E 'TERMUX_APK_RELEASE|TERMUX_VERSION'
SD=$(readlink ~/storage/external-1 2>/dev/null | grep -o '^/storage/[0-9A-Fa-f]\{4\}-[0-9A-Fa-f]\{4\}')
[ -n "$SD" ] || SD=$(grep -o '/storage/[0-9A-Fa-f]\{4\}-[0-9A-Fa-f]\{4\}' /proc/mounts | head -1)
echo "SD=$SD"
[ -n "$SD" ] || { echo "★SDカードの場所が見つからない（termux-setup-storage を実行したか確認）"; exit 1; }
T="$SD/clo_check"; D=/sdcard/Download/clo_check
rm -rf "$D"; mkdir -p "$D"
mkdir -p "$T/a" "$T/b" 2>&1 || { echo "★SDカード直下に書き込めない（「すべてのファイルの管理」の許可を確認）"; exit 1; }
echo "SD書込OK"
echo "== (1) SDカード内の移動（300MB）"
dd if=/dev/zero of="$T/a/big.bin" bs=1M count=300 status=none
time mv "$T/a/big.bin" "$T/b/"
echo "== (2) 本体 → SDカードの移動（300MB, rsync）"
dd if=/dev/zero of="$D/r.bin" bs=1M count=300 status=none
time rsync -r --ignore-existing --remove-source-files "$D/r.bin" "$T/b/"
ls -l "$T/b/r.bin"; [ -e "$D/r.bin" ] && echo "★元が残った" || echo "元の削除OK"
echo "== (3) 移動先に同名があるとき"
echo new > "$D/dup.zip"; echo old > "$T/b/dup.zip"
rsync -r --ignore-existing --remove-source-files "$D/dup.zip" "$T/b/"
echo "移動先の中身: $(cat "$T/b/dup.zip")（oldなら上書きなし）"
[ -e "$D/dup.zip" ] && echo "元は残った（正しい）" || echo "★元が消えた"
echo "== (4) 日本語・記号入りの名前"
touch "$D/[作者 名] テスト 第01巻w.zip"
rsync -r --ignore-existing --remove-source-files "$D/[作者 名] テスト 第01巻w.zip" "$T/b/" && ls "$T/b"
echo "== (5) 中断（3秒後に自動で中断）"
dd if=/dev/zero of="$D/big2.bin" bs=1M count=1500 status=none
timeout -s INT 3 rsync -r --ignore-existing --remove-source-files "$D/big2.bin" "$T/b/"
echo "rsync終了コード: $?"
ls -la "$T/b"; ls -l "$D/big2.bin" 2>&1
if [ "$1" = "--sleep" ]; then
  echo "== (6) 画面を消しても止まらないか（librarian.py と同じくウェイクロックを取る）"
  termux-wake-lock
  read -r -p "Enter を押したらすぐ電源ボタンで画面を消し、90秒以上たってから画面を点けてください > " _
  START=$(date +%s); PREV=$START; MAXGAP=0
  for i in $(seq 1 90); do
    sleep 1; NOW=$(date +%s); GAP=$((NOW - PREV)); PREV=$NOW
    [ $GAP -gt $MAXGAP ] && MAXGAP=$GAP
  done
  termux-wake-unlock
  echo "90回の待ちにかかった時間: $((PREV - START))秒、最大の間隔: ${MAXGAP}秒（1〜2秒なら止まっていない）"
fi
echo "== 後片付け"
rm -rf "$T" "$D" && echo "完了"
echo "（画面の記録: $LOG）"
