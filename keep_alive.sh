#!/bin/bash
# サーバーが落ちたら自動で再起動するスクリプト
cd /Users/sho/stock_analyzer
while true; do
  echo "[$(date)] Starting Flask server..."
  python3 app.py
  echo "[$(date)] Server crashed! Restarting in 3 seconds..."
  sleep 3
done
