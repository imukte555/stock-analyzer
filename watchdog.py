#!/usr/bin/env python3
"""スイングbotの死活監視。
botのlaunchdジョブ自体が死ぬと誰も気づけないので、監視は別ジョブとして独立して動かす。
(2026-08-27にDNS障害でジョブが異常終了し、2時間無言で停止していた事故を受けて追加)
"""
import json, os, sys, urllib.request
from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))   # botはJSTで記録する。環境TZ(欧州等)に依存させない

BASE = os.path.dirname(os.path.abspath(__file__))
STALE_MIN = 90          # 30分間隔なので、90分途絶えたら異常
TOPIC = "imukte"        # エルメス監視と同じntfyトピック

def push(title, body, priority=5):
    try:
        p = json.dumps({"topic": TOPIC, "title": title, "message": body,
                        "tags": ["warning"], "priority": priority},
                       ensure_ascii=False).encode()
        r = urllib.request.Request("https://ntfy.sh", data=p,
                                   headers={"Content-Type": "application/json"})
        urllib.request.urlopen(r, timeout=20)
        return True
    except Exception as e:
        print(f"push失敗: {e}", file=sys.stderr)
        return False

def check():
    problems = []
    for f, lab in [("swing_bot_state.json", "株"), ("swing_bot_fx_state.json", "FX")]:
        p = os.path.join(BASE, f)
        if not os.path.exists(p):
            problems.append(f"{lab}: stateファイルが無い"); continue
        try:
            d = json.load(open(p))
        except Exception as e:
            problems.append(f"{lab}: stateが壊れている ({e})"); continue
        lr = d.get("last_run_at")
        if not lr:
            problems.append(f"{lab}: 実行記録なし"); continue
        try:
            now = datetime.now(JST).replace(tzinfo=None)
            age = (now - datetime.strptime(lr, "%Y-%m-%d %H:%M")).total_seconds() / 60
        except Exception:
            problems.append(f"{lab}: 実行時刻が読めない ({lr})"); continue
        if age > STALE_MIN:
            problems.append(f"{lab}: {age:.0f}分間停止中（最終 {lr}）")
    return problems

if __name__ == "__main__":
    if "--test" in sys.argv:
        ok = push("🤖 スイングbot 監視テスト",
                  "死活監視を設置しました。botが90分以上止まったらこの形式で通知します。",
                  priority=3)
        print("テスト送信:", "成功" if ok else "失敗"); sys.exit(0)
    probs = check()
    if probs:
        # 2026-08-29: shoさんの指示でスマホ通知を停止。ログに残すだけにする。
        # (状態は /swing のダッシュボードでも確認できる)
        print("異常検知:", probs)
    else:
        print("正常")
