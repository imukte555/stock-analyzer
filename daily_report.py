#!/usr/bin/env python3
"""実運用（フォワードテスト）の日次レポート。
過去のバックテストではなく「新ルール適用後、実際に何が起きたか」だけを追う。
S&P500と比較しないと「上がった」が実力か相場かを区別できないので、必ず並べて出す。
"""
import json, os, sys, urllib.request, datetime
from datetime import timezone, timedelta

JST = timezone(timedelta(hours=9))   # botはJSTで記録するので、環境のTZに依存させない
def today_jst(): return datetime.datetime.now(JST).date()
BASE = os.path.dirname(os.path.abspath(__file__))
TOPIC = "imukte"

def push(title, body, priority=3):
    try:
        p = json.dumps({"topic": TOPIC, "title": title, "message": body,
                        "tags": ["chart_with_upwards_trend"], "priority": priority},
                       ensure_ascii=False).encode()
        r = urllib.request.Request("https://ntfy.sh", data=p,
                                   headers={"Content-Type": "application/json"})
        urllib.request.urlopen(r, timeout=20); return True
    except Exception as e:
        print("push失敗:", e, file=sys.stderr); return False

def spy_return(since):
    """開始日からのS&P500の騰落率。取れなければNone（数字をでっち上げない）"""
    try:
        import yfinance as yf, warnings; warnings.filterwarnings('ignore')
        # 開始日当日はまだ値が無いことがあるので数日前から取り、開始日以降で最初の値を基準にする
        st = (datetime.date.fromisoformat(since) - datetime.timedelta(days=7)).isoformat()
        h = yf.Ticker("SPY").history(start=st)
        if h is None or len(h) < 2: return None
        h = h[h.index.tz_localize(None) >= datetime.datetime.fromisoformat(since)] if h.index.tz is not None else h[h.index >= datetime.datetime.fromisoformat(since)]
        if len(h) < 2: return 'NODATA'
        return (float(h['Close'].iloc[-1]) / float(h['Close'].iloc[0]) - 1) * 100
    except Exception:
        return None

def report(acct_file, label):
    d = json.load(open(os.path.join(BASE, acct_file)))
    ft = d.get('forward_test')
    if not ft: return None, None
    eq = d['cash'] + sum(p['cost'] for p in d['positions'].values())
    start = ft['start_equity']
    ret = (eq / start - 1) * 100
    days = (today_jst() - datetime.date.fromisoformat(ft['start_date'])).days
    # 新ルール下で完結した取引だけを数える（開始前に建てた玉の決済は実力に含めない）
    legacy = set(ft.get('legacy_positions', []))
    new_tr = [h for h in d['history']
              if h.get('opened','') >= ft['start_date'] or h.get('sym') not in legacy]
    closed_new = [h for h in d['history'] if h.get('opened','') >= ft['start_date']]
    lines = [f"【{label}】{days}日経過",
             f"資産 {eq:,.0f}円 ({ret:+.2f}%)",
             f"建玉 {len(d['positions'])}件 / 新ルールでの決済 {len(closed_new)}件"]
    if closed_new:
        wins = sum(1 for h in closed_new if h['pnl_yen'] > 0)
        tot = sum(h['pnl_yen'] for h in closed_new)
        lines.append(f"新ルール成績 {wins}/{len(closed_new)}勝 合計{tot:+,.0f}円")
    return "\n".join(lines), (ft['start_date'], ret)

if __name__ == "__main__":
    out, meta = [], None
    for f, lab in [("swing_bot_state.json", "株"), ("swing_bot_fx_state.json", "FX")]:
        r, m = report(f, lab)
        if r: out.append(r)
        if lab == "株": meta = m
    if meta:
        sp = spy_return(meta[0])
        if sp == 'NODATA':
            out.append("S&P500: 開始直後で比較データなし（次の取引日から比較開始）")
        elif sp is None:
            out.append("S&P500: 取得失敗（比較なし）")
        else:
            diff = meta[1] - sp
            out.append(f"同期間のS&P500 {sp:+.2f}%\n→ 市場との差 {diff:+.2f}%")
    body = "\n\n".join(out)
    print(body)
    # スマホ通知は停止（2026-08-29 shoさんの指示）。
    # 成績は /swing のダッシュボードで見る。手動で送りたい時だけ --push を付ける。
    if "--push" in sys.argv:
        push("📊 スイングbot 実運用レポート", body)
