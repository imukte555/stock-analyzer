#!/bin/bash
# 🤖 スイングbot: Macで頭脳を実行 → stateをGitHubへpush（Renderは表示専用）
#
# 設計上の前提（2026-08-27の障害を受けて）:
#   - Macはスリープする。起床直後はWi-Fiがまだ繋がっていない
#   - 一時的な通信エラーでジョブ全体を落とさない（次回巡回で取り返せるため）
#   - 動かなくなったことはログに残す（スマホ通知はshoさんの指示で廃止）
cd /Users/sho/stock_analyzer || exit 1
export TZ=Asia/Tokyo
LOG=/tmp/swing_local.log

# --- タイムアウト付き実行（macOSに timeout コマンドが無いので自前） ---
# 2026-09-17: ネット断時に git push が75秒ブロックし、リトライ込みで巡回間隔を潰して
# botが3.5時間止まった（同種の事故は3回目）。gitの低速切断もTCP接続段階には効かないため、
# シェル側でプロセスごと打ち切る。stateは次回巡回でまとめてpushすればよい。
run_with_timeout() {
  local secs="$1"; shift
  local flag; flag=$(mktemp)
  "$@" &
  local pid=$!
  ( sleep "$secs"; if kill -0 "$pid" 2>/dev/null; then echo timeout > "$flag"; kill -9 "$pid" 2>/dev/null; fi ) &
  local killer=$!
  wait "$pid" 2>/dev/null; local rc=$?
  kill -9 "$killer" 2>/dev/null; wait "$killer" 2>/dev/null
  # 打ち切った場合は失敗(124)を返す。wait が 0 を返しても成功扱いにしない
  if [ -s "$flag" ]; then rc=124; fi
  rm -f "$flag"
  return $rc
}

HEARTBEAT=/tmp/swing_last_success

# --- 起床直後を想定してネット復帰を待つ（最大60秒） ---
for i in $(seq 1 12); do
  if ping -c1 -W2 1.1.1.1 >/dev/null 2>&1; then break; fi
  sleep 5
done

# --- bot本体 ---
python3 -c "
import swing_bot
r1=swing_bot.run_once('stock'); r2=swing_bot.run_once('fx')
print('stock:',r1); print('fx:',r2)
" >> "$LOG" 2>&1
BOT_RC=$?

# --- stateをGitHubへpush（失敗しても致命傷にしない） ---
PUSH_OK=0
git add swing_bot_state.json swing_bot_fx_state.json 2>/dev/null
if git diff --cached --quiet 2>/dev/null; then
  PUSH_OK=1   # 変更なし＝pushの必要なし
else
  git commit -q -m "bot: state $(date '+%m-%d %H:%M')"
  # 🔴 git push にタイムアウトを入れる。
  # 2026-09-17: ネット断時に push 1回が263秒かかり、3回リトライで13分超を消費。
  # 巡回間隔(30分)を圧迫して bot が3.5時間止まった（同種の事故は3回目）。
  # state は次回巡回でまとめて push すればよいので、通信に粘る価値はない。
  for attempt in 1 2; do
    if run_with_timeout 30 git push -q origin main 2>>"$LOG"; then PUSH_OK=1; break; fi
    run_with_timeout 30 git pull -q --rebase origin main 2>>"$LOG"
  done
fi

# --- 成否の記録と通知 ---
NOW=$(date +%s)
if [ "$BOT_RC" -eq 0 ] && [ "$PUSH_OK" -eq 1 ]; then
  echo "$NOW" > "$HEARTBEAT"
else
  echo "[$(date '+%m-%d %H:%M')] 失敗 bot_rc=$BOT_RC push_ok=$PUSH_OK" >> "$LOG"
  # 前回成功から2時間以上あいていたら通知（一時的な失敗では鳴らさない）
  if [ -f "$HEARTBEAT" ]; then
    LAST=$(cat "$HEARTBEAT")
    GAP=$(( (NOW - LAST) / 60 ))
    if [ "$GAP" -ge 120 ]; then
      echo "[$(date '+%m-%d %H:%M')] ⚠️ ${GAP}分停止していた (bot_rc=$BOT_RC push_ok=$PUSH_OK)" >> "$LOG"
    fi
  fi
fi
exit 0   # launchdのタイマーを止めないよう常に正常終了する
