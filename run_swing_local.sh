#!/bin/bash
# 🤖 スイングbot: Macで頭脳を実行 → stateをGitHubへpush（Renderは表示専用）
cd /Users/sho/stock_analyzer
export TZ=Asia/Tokyo
python3 -c "
import swing_bot
r1=swing_bot.run_once('stock'); r2=swing_bot.run_once('fx')
print('stock:',r1); print('fx:',r2)
" >> /tmp/swing_local.log 2>&1
# stateをpush（botリモート=トークン付き）
git add swing_bot_state.json swing_bot_fx_state.json 2>/dev/null
if ! git diff --cached --quiet 2>/dev/null; then
  git commit -q -m "bot: state $(date '+%m-%d %H:%M')"
  git push -q bot main 2>>/tmp/swing_local.log || { git pull -q --rebase bot main 2>>/tmp/swing_local.log && git push -q bot main 2>>/tmp/swing_local.log; }
fi
