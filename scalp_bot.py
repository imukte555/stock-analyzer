#!/usr/bin/env python3
"""⚡ 5分足の短期トレードbot（仮想資金100万円）

swing_bot とは完全に独立。日足前提のロジックを壊さないため別ファイルにしている。

⚠️ バックテスト(2026-06〜09, 1249取引)では 累計-15.52% / 年率換算-51.6% / 勝率37.2% と
   明確に負けた。取引回数が日足の3倍以上になり往復コスト0.10%に食われるのが主因。
   それでも「実際に動かして確かめる」ため稼働させる。仮想資金なので損失は発生しない。

判定は swing_bot の指標・シグナル関数をそのまま使う（別物を測る事故を防ぐ）。
"""
import os, json, threading
from datetime import datetime, timedelta, timezone
import numpy as np, yfinance as yf
import swing_bot as sb

JST = timezone(timedelta(hours=9))
def _now():  return datetime.now(JST).replace(tzinfo=None)

BASE = os.path.dirname(os.path.abspath(__file__))
FILE = os.path.join(BASE, 'scalp_bot_state.json')
RAW  = 'https://raw.githubusercontent.com/imukte555/stock-analyzer/main/scalp_bot_state.json'
IS_RENDER = bool(os.environ.get('RENDER'))
_lock = threading.Lock()

# 5分足は流動性が要る。米国大型のみ（日本株は5分足の板が薄く滑りが読めない）
UNIVERSE = [("NVDA","NVIDIA"),("AAPL","Apple"),("MSFT","Microsoft"),("AMD","AMD"),("TSLA","Tesla"),
            ("META","Meta"),("AMZN","Amazon"),("GOOGL","Alphabet"),("AVGO","Broadcom"),("NFLX","Netflix"),
            ("PLTR","Palantir"),("COIN","Coinbase"),("MU","Micron"),("ARM","Arm"),("QCOM","Qualcomm")]
NAME = dict(UNIVERSE)

DEFAULT = {
    'label': '⚡5分足 短期トレード（仮想）',
    'initial_capital': 1_000_000, 'cash': 1_000_000,
    'positions': {}, 'pending': {}, 'history': [], 'equity_curve': [], 'log': [],
    'started_at': None, 'last_run_at': None,
    'settings': {
        'interval': '5m',
        'risk_pct': 1.0, 'min_position_pct': 3, 'max_position_pct': 25,
        'max_positions': 8, 'max_per_sector': 3,
        'sl_atr': 2.5, 'tp_atr': 10.0,
        'max_hold': 30,          # 30本＝2時間30分
        'cost_pct': 0.10,
        'strategies': ['reversal','breakout'],
    },
    'backtest_warning': 'バックテストでは年率換算-51.6%。負ける想定で稼働中',
}

def _load():
    if IS_RENDER:
        try:
            import urllib.request
            with urllib.request.urlopen(RAW+'?t='+_now().strftime('%Y%m%d%H%M'),timeout=10) as r:
                d=json.loads(r.read().decode())
            for k,v in DEFAULT.items():
                if k not in d: d[k]=v
            return d
        except Exception:
            return json.loads(json.dumps(DEFAULT))
    if not os.path.exists(FILE): return json.loads(json.dumps(DEFAULT))
    try:
        d=json.load(open(FILE))
        for k,v in DEFAULT.items():
            if k not in d: d[k]=v
        for k,v in DEFAULT['settings'].items():
            if k not in d['settings']: d['settings'][k]=v
        return d
    except Exception:
        return json.loads(json.dumps(DEFAULT))

def _save(d):
    tmp=FILE+'.tmp'
    json.dump(d,open(tmp,'w'),ensure_ascii=False,indent=1); os.replace(tmp,FILE)

def _log(d,msg):
    d['log'].append({'t':_now().strftime('%m-%d %H:%M'),'msg':msg}); d['log']=d['log'][-200:]

def _fetch(sym, interval='5m'):
    try:
        h=yf.Ticker(sym).history(period='5d', interval=interval)
        if h is None or len(h)==0: return None
        h=h[['Open','High','Low','Close']].dropna()
        if h.index.tz is not None: h.index=h.index.tz_localize(None)
        return h
    except Exception:
        return None

def run_once():
    if IS_RENDER: return dict(actions=[], note='viewer mode')
    with _lock:
        st=_load(); S=st['settings']; actions=[]
        if not st['started_at']: st['started_at']=_now().strftime('%Y-%m-%d %H:%M')
        syms=set(list(st['positions'])+list(st['pending'])+[s for s,_ in UNIVERSE])
        data={}; fail=[]
        for s in syms:
            h=_fetch(s,S['interval'])
            if h is not None and len(h)>40: data[s]=h
            else: fail.append(NAME.get(s,s))
        if fail: _log(st,f"⚠️ 取得失敗 {len(fail)}/{len(syms)}銘柄")
        if not data:
            st['last_run_at']=_now().strftime('%Y-%m-%d %H:%M'); _save(st)
            return dict(actions=[], error='データなし')

        # (1) 約定待ち → 次のバーの始値で約定
        for s,p in list(st['pending'].items()):
            h=data.get(s)
            if h is None: continue
            after=h[h.index > __import__('pandas').Timestamp(p['bar'])]
            if len(after)==0: continue
            entry=float(after['Open'].iloc[0])
            if len(st['positions'])>=S['max_positions']:
                _log(st,f"⏭ {p['name']} 見送り（ポジション上限）"); del st['pending'][s]; continue
            a=p['atr']; eq=st['cash']+sum(x['cost'] for x in st['positions'].values())
            sd=(a*S['sl_atr'])/entry if entry>0 else 0
            budget=(eq*S['risk_pct']/100)/sd if sd>0 else 0
            budget=max(eq*S['min_position_pct']/100, min(eq*S['max_position_pct']/100, budget))
            budget=min(budget, st['cash'])
            if budget<=0:
                _log(st,f"⏭ {p['name']} 見送り（資金不足）"); del st['pending'][s]; continue
            st['cash']-=budget
            st['positions'][s]=dict(entry=entry, sl=entry-a*S['sl_atr'], tp=entry+a*S['tp_atr'],
                                    atr=a, cost=budget, bars=0, name=p['name'],
                                    strategy=p['strategy'], opened=str(after.index[0]))
            del st['pending'][s]
            act=f"🟢 約定 {p['name']} 買 @{entry:,.2f} 損切{entry-a*S['sl_atr']:,.2f} 利確{entry+a*S['tp_atr']:,.2f} 投入¥{budget:,.0f}"
            _log(st,act); actions.append(act)

        # (2) 保有の管理
        import pandas as pd
        for s,p in list(st['positions'].items()):
            h=data.get(s)
            if h is None: continue
            bars=h[h.index > pd.Timestamp(p['opened'])]
            for t,r in bars.iloc[p['bars']:].iterrows():
                p['bars']+=1
                hit=None
                if float(r['Low'])<=p['sl']: hit=('SL',p['sl'])
                elif float(r['High'])>=p['tp']: hit=('TP',p['tp'])
                if not hit and p['bars']>=S['max_hold']: hit=('時間',float(r['Close']))
                if hit:
                    net=(hit[1]/p['entry']-1)*100 - S['cost_pct']
                    proceeds=p['cost']*(1+net/100); st['cash']+=proceeds
                    st['history'].append(dict(sym=s,name=p['name'],entry=p['entry'],exit=hit[1],
                        pnl_pct=round(net,2), pnl_yen=round(proceeds-p['cost']), reason=hit[0],
                        bars=p['bars'], strategy=p['strategy'], opened=p['opened'], closed=str(t)))
                    emoji={'TP':'💰','SL':'🔴','時間':'⏰'}[hit[0]]
                    act=f"{emoji} 決済 {p['name']} {hit[0]} @{hit[1]:,.2f} {net:+.2f}% (¥{proceeds-p['cost']:+,.0f}) {p['bars']}本保有"
                    _log(st,act); actions.append(act)
                    del st['positions'][s]; break

        # (3) 新規シグナル
        used_sec={}
        for s in list(st['positions'])+list(st['pending']):
            sec=sb._sector_of(s); used_sec[sec]=used_sec.get(sec,0)+1
        for s,name in UNIVERSE:
            if s in st['positions'] or s in st['pending']: continue
            h=data.get(s)
            if h is None or len(h)<40: continue
            ind=sb._indicators(h); i=len(h)-2   # 最新は未確定の可能性 → 1本前を確定足とする
            a=float(ind['atr'].iloc[i])
            if np.isnan(a) or a<=0: continue
            for strat in S['strategies']:
                sig,reason=sb._detect_signal(strat, ind, i, h['Close'], False)
                if sig!='L': continue
                sec=sb._sector_of(s)
                if used_sec.get(sec,0)>=S['max_per_sector']:
                    _log(st,f"⏭ {name} 見送り（{sec}セクター上限）"); break
                if len(st['positions'])+len(st['pending'])>=S['max_positions']: break
                st['pending'][s]=dict(atr=a, name=name, strategy=strat, reason=reason,
                                      bar=str(h.index[i]), close=float(h['Close'].iloc[i]))
                used_sec[sec]=used_sec.get(sec,0)+1
                tag='逆張り' if strat=='reversal' else '順張り'
                act=f"📡 シグナル[{tag}] {name} 買候補 {reason} 終値{float(h['Close'].iloc[i]):,.2f} → 次バーで約定予定"
                _log(st,act); actions.append(act)
                break

        eq=st['cash']+sum(p['cost'] for p in st['positions'].values())
        st['equity_curve'].append({'t':_now().strftime('%m-%d %H:%M'),'equity':round(eq)})
        st['equity_curve']=st['equity_curve'][-2000:]
        st['last_run_at']=_now().strftime('%Y-%m-%d %H:%M')
        if not actions: _log(st,'👀 巡回完了・変化なし')
        _save(st)
        return dict(actions=actions, equity=round(eq), positions=len(st['positions']), pending=len(st['pending']))

def status():
    d=_load()
    eq=d['cash']+sum(p['cost'] for p in d['positions'].values())
    pos=[]
    for s,p in d['positions'].items():
        cur=p['entry']; stale=True
        h=_fetch(s,d['settings']['interval']) if not IS_RENDER else None
        if h is not None and len(h): cur=float(h['Close'].iloc[-1]); stale=False
        un=(cur/p['entry']-1)*100
        pos.append(dict(sym=s,name=p['name'],entry=p['entry'],cur=cur,sl=p['sl'],tp=p['tp'],
                        unreal_pct=round(un,2), unreal_yen=round(p['cost']*un/100),
                        bars=p['bars'], strategy=p['strategy'], stale=stale))
        eq+= p['cost']*un/100
    return dict(label=d['label'], initial_capital=d['initial_capital'], cash=d['cash'],
                equity=round(eq), positions=pos, pending=d['pending'],
                history=d['history'][::-1][:100], equity_curve=d['equity_curve'][-300:],
                log=d['log'][::-1][:60], settings=d['settings'],
                started_at=d['started_at'], last_run_at=d['last_run_at'],
                backtest_warning=d.get('backtest_warning'))

if __name__ == '__main__':
    print(run_once())
