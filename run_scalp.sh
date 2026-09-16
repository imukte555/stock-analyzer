#!/bin/bash
# ⚡5分足bot。開場判定はscalp_bot._market_open_now()で行う（東京09:00-15:00 / 米国22:30-05:00）
cd /Users/sho/stock_analyzer || exit 1
export TZ=Asia/Tokyo

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

# 曜日の除外はここではやらない。米国市場の金曜セッションは
# 日本時間では「金22:30〜土05:00」にまたがるため、土曜を一律に止めると
# 金曜の米国市場を丸ごと取りこぼす（2026-09-04にこれで1セッション欠測した）。
# 開場判定は scalp_bot._market_open_now() に集約する。
python3 -c "import scalp_bot; print(scalp_bot.run_once())" >> /tmp/scalp.log 2>&1
git add scalp_bot_state.json 2>/dev/null
if ! git diff --cached --quiet 2>/dev/null; then
  git commit -q -m "scalp: state $(date '+%m-%d %H:%M')"
  # 5分間隔なので push は20秒で打ち切る（ネット断で巡回が丸ごと潰れるのを防ぐ）
  run_with_timeout 20 git push -q origin main 2>>/tmp/scalp.log \
    || { run_with_timeout 20 git pull -q --rebase origin main 2>>/tmp/scalp.log \
         && run_with_timeout 20 git push -q origin main 2>>/tmp/scalp.log; }
fi
exit 0
