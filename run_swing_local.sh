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
  for attempt in 1 2 3; do
    if git push -q origin main 2>>"$LOG"; then PUSH_OK=1; break; fi
    git pull -q --rebase origin main 2>>"$LOG"
    sleep 5
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
