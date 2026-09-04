#!/bin/bash
# ⚡5分足bot。米国市場が開いている時だけ走らせる（JST 22:30〜翌5:00）
cd /Users/sho/stock_analyzer || exit 1
export TZ=Asia/Tokyo
H=$(date +%H); M=$(date +%M); DOW=$(date +%u)
# 土日はスキップ（月曜早朝=米金曜引けまでは動かす）
if [ "$DOW" -eq 6 ]; then exit 0; fi
if [ "$DOW" -eq 7 ]; then exit 0; fi
HM=$((10#$H*60+10#$M))
# 22:30〜23:59 または 00:00〜05:00 のみ
if [ $HM -lt 1350 ] && [ $HM -gt 300 ]; then exit 0; fi
python3 -c "import scalp_bot; print(scalp_bot.run_once())" >> /tmp/scalp.log 2>&1
git add scalp_bot_state.json 2>/dev/null
if ! git diff --cached --quiet 2>/dev/null; then
  git commit -q -m "scalp: state $(date '+%m-%d %H:%M')"
  git push -q origin main 2>>/tmp/scalp.log || { git pull -q --rebase origin main 2>>/tmp/scalp.log && git push -q origin main 2>>/tmp/scalp.log; }
fi
exit 0
