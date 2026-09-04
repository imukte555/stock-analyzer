#!/bin/bash
# ⚡5分足bot。開場判定はscalp_bot._market_open_now()で行う（東京09:00-15:00 / 米国22:30-05:00）
cd /Users/sho/stock_analyzer || exit 1
export TZ=Asia/Tokyo
# 曜日の除外はここではやらない。米国市場の金曜セッションは
# 日本時間では「金22:30〜土05:00」にまたがるため、土曜を一律に止めると
# 金曜の米国市場を丸ごと取りこぼす（2026-09-04にこれで1セッション欠測した）。
# 開場判定は scalp_bot._market_open_now() に集約する。
python3 -c "import scalp_bot; print(scalp_bot.run_once())" >> /tmp/scalp.log 2>&1
git add scalp_bot_state.json 2>/dev/null
if ! git diff --cached --quiet 2>/dev/null; then
  git commit -q -m "scalp: state $(date '+%m-%d %H:%M')"
  git push -q origin main 2>>/tmp/scalp.log || { git pull -q --rebase origin main 2>>/tmp/scalp.log && git push -q origin main 2>>/tmp/scalp.log; }
fi
exit 0
