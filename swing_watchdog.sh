#!/bin/bash
# スイングbotが動いているかを外から見張る。bot本体が死んでも動くよう独立ジョブにする。
# 前回成功から2時間以上あいていたらiPhoneへ通知する（1回鳴らしたら6時間は黙る）
export TZ=Asia/Tokyo
HEARTBEAT=/tmp/swing_last_success
ALERTED=/tmp/swing_alerted
NOW=$(date +%s)

[ -f "$HEARTBEAT" ] || exit 0            # まだ一度も成功記録がない場合は何もしない
LAST=$(cat "$HEARTBEAT" 2>/dev/null) || exit 0
GAP=$(( (NOW - LAST) / 60 ))
[ "$GAP" -lt 120 ] && { rm -f "$ALERTED"; exit 0; }   # 正常。過去の通知フラグを消す

if [ -f "$ALERTED" ]; then                # 連続通知を防ぐ（6時間に1回まで）
  PREV=$(cat "$ALERTED" 2>/dev/null || echo 0)
  [ $(( (NOW - PREV) / 3600 )) -lt 6 ] && exit 0
fi

POWER=$(pmset -g batt 2>/dev/null | head -1 | grep -o "'.*'" | tr -d "'")
python3 - "$GAP" "$POWER" <<'PY' 2>/dev/null
import json,sys,urllib.request
gap,power=sys.argv[1],sys.argv[2]
try:
    payload=json.dumps({"topic":"imukte",
        "title":f"⚠️ スイングbotが{gap}分止まっています",
        "message":f"電源: {power}\nMacがスリープしていた可能性があります。電源接続を確認してください。",
        "tags":["warning"],"priority":4,
        "actions":[{"action":"view","label":"📊 botを開く",
                    "url":"https://stock-analyzer-m20q.onrender.com/swing","clear":False}]},
        ensure_ascii=False).encode()
    urllib.request.urlopen(urllib.request.Request("https://ntfy.sh",data=payload,
        headers={"Content-Type":"application/json"},method="POST"),timeout=10)
except Exception: pass
PY
echo "$NOW" > "$ALERTED"
