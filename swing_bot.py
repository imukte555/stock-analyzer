"""
🤖 完全自動スイングbot（仮想資金）
バックテストで実証したv2戦略をそのまま実運用:
  エントリー: 終値 < BB下限(20,2σ) かつ RSI(7) < 30 → 翌営業日寄付で買い
             （FX/米国株はショートも: 終値 > BB上限 かつ RSI(7) > 70）
  損切り: ATR(14)×1.5   利確: ATR(14)×2.5
  建値ストップ: 含み益がATR×1.0乗ったら損切りラインを建値に引き上げ
  時間切れ: 10営業日で強制決済
  資金管理: 1銘柄あたり資金の10%、同時最大8ポジション
"""
import os, json, threading, traceback
from datetime import datetime, timedelta
import numpy as np
import pandas as pd
import yfinance as yf
import ta

_lock = threading.Lock()
ACCOUNTS = {
    'stock': dict(file='swing_bot_state.json',    markets=['jp','us'], leverage=1.0, cost_pct=0.10, label='株（日本+米国）'),
    'fx':    dict(file='swing_bot_fx_state.json', markets=['fx'],      leverage=5.0, cost_pct=0.02, label='FX（レバ5倍）'),
}
def _file(acct): return os.path.join(os.path.dirname(__file__), ACCOUNTS[acct]['file'])

UNIVERSE = {
    'jp': [("8035.T","東京エレクトロン"),("6857.T","アドバンテスト"),("6920.T","レーザーテック"),("9984.T","ソフトバンクG"),
           ("5803.T","フジクラ"),("6146.T","ディスコ"),("7013.T","IHI"),("6758.T","ソニーG"),
           ("7011.T","三菱重工"),("5802.T","住友電工"),("7974.T","任天堂"),("4568.T","第一三共")],
    'us': [("AMD","AMD"),("MU","Micron"),("MSTR","MicroStrategy"),("COIN","Coinbase"),("PLTR","Palantir"),("SMCI","Super Micro"),
           ("NVDA","NVIDIA"),("TSLA","Tesla"),("ARM","Arm"),("INTC","Intel")],
    'fx': [("USDJPY=X","ドル円"),("GBPJPY=X","ポンド円"),("EURUSD=X","ユーロドル"),("AUDJPY=X","豪ドル円"),
           ("MXNJPY=X","ペソ円"),("GBPUSD=X","ポンドドル"),("NZDJPY=X","NZ円"),("EURJPY=X","ユーロ円")],
}
JP_NAME = {s:n for s,n in UNIVERSE['jp']}
US_NAME = {s:n for s,n in UNIVERSE['us']}
FX_NAME = {s:n for s,n in UNIVERSE['fx']}
ALL_NAME = {**JP_NAME, **US_NAME, **FX_NAME}

DEFAULT = {
    'initial_capital': 1_000_000,
    'cash': 1_000_000,
    'positions': {},        # sym -> {side, entry, sl, tp, atr, be, qty, opened, name, market}
    'pending': {},          # sym -> {side, atr, signal_date, name, market}  (翌寄付で約定待ち)
    'history': [],          # closed trades
    'equity_curve': [],     # [{date, equity}]
    'log': [],              # 直近200件のイベントログ
    'started_at': None,
    'last_run_at': None,
    'settings': {
        'position_pct': 10,      # 1ポジション = 資金の10%
        'max_positions': 8,
        'markets': ['jp','us'],
        'sl_atr': 1.5, 'tp_atr': 2.5, 'be_atr': 1.0, 'max_hold': 10,
        'cost_pct': 0.10,        # 往復コスト
        'leverage': 1.0,         # FX口座は5倍
        'annual_interest': 0.0,  # FXスワップは無視（概算）
    }
}

def _default_for(acct):
    d=json.loads(json.dumps(DEFAULT)); a=ACCOUNTS[acct]
    d['settings'].update(markets=a['markets'], leverage=a['leverage'], cost_pct=a['cost_pct'])
    d['account']=acct; d['label']=a['label']
    return d

RAW_BASE='https://raw.githubusercontent.com/imukte555/stock-analyzer/main/'
IS_RENDER=bool(os.environ.get('RENDER'))

def _load_remote(acct):
    """GitHub raw から最新stateを取得（Render表示用）"""
    try:
        import urllib.request
        url=RAW_BASE+ACCOUNTS[acct]['file']+'?t='+datetime.now().strftime('%Y%m%d%H%M')
        with urllib.request.urlopen(url,timeout=10) as r:
            d=json.loads(r.read().decode())
        base=_default_for(acct)
        for k,v in base.items():
            if k not in d: d[k]=v
        d['account']=acct; d['label']=ACCOUNTS[acct]['label']
        return d
    except Exception:
        return None

def _load(acct='stock'):
    if IS_RENDER:
        d=_load_remote(acct)
        if d: return d
    path=_file(acct)
    if not os.path.exists(path):
        return _default_for(acct)
    try:
        with open(path,'r',encoding='utf-8') as f: d=json.load(f)
        base=_default_for(acct)
        for k,v in base.items():
            if k not in d: d[k]=v
        for k,v in base['settings'].items():
            if k not in d['settings']: d['settings'][k]=v
        # 口座ごとの市場/レバは常に定義に従わせる（古いstateの['jp','us','fx']を矯正）
        d['settings']['markets']=ACCOUNTS[acct]['markets']
        d['settings']['leverage']=ACCOUNTS[acct]['leverage']
        d['settings']['cost_pct']=ACCOUNTS[acct]['cost_pct']
        d['account']=acct; d['label']=ACCOUNTS[acct]['label']
        return d
    except Exception:
        return _default_for(acct)

def _save(state):
    path=_file(state.get('account','stock'))
    tmp=path+'.tmp'
    with open(tmp,'w',encoding='utf-8') as f: json.dump(state,f,ensure_ascii=False,indent=1)
    os.replace(tmp,path)

def _log(state,msg):
    state['log'].append({'t':datetime.now().strftime('%m-%d %H:%M'),'msg':msg})
    state['log']=state['log'][-200:]

def _market_of(sym):
    if sym.endswith('=X'): return 'fx'
    if sym.endswith('.T'): return 'jp'
    return 'us'

def _fetch(sym, period='6mo'):
    try:
        h=yf.Ticker(sym).history(period=period)
        if h is None or len(h)<40: return None
        h=h[['Open','High','Low','Close']].dropna()
        return h
    except Exception:
        return None

def _indicators(h):
    c,hi,lo=h['Close'],h['High'],h['Low']
    bb=ta.volatility.BollingerBands(c,20,2)
    return dict(
        bbu=bb.bollinger_hband(), bbl=bb.bollinger_lband(),
        rsi=ta.momentum.RSIIndicator(c,7).rsi(),
        atr=ta.volatility.AverageTrueRange(hi,lo,c,14).average_true_range(),
    )

def _last_completed_bar(h, market):
    """当日の未確定バーを除いた最新の確定足を返す。
       yfinanceは取引時間中に当日バーを含めることがあるので、簡易的に
       '当日日付のバー' は未確定として扱う（jp: 15:30以降なら確定扱い）。"""
    last_date=h.index[-1]
    now=datetime.now(last_date.tzinfo) if last_date.tzinfo else datetime.now()
    if last_date.date()==now.date():
        # jp市場は15:30以降なら確定、それ以外は前日を確定足とみなす
        if market=='jp' and now.hour*60+now.minute>=15*60+30:
            return h, len(h)-1
        return h.iloc[:-1], len(h)-2
    return h, len(h)-1

def run_once(acct='stock'):
    if IS_RENDER:
        return dict(actions=[], note='viewer mode: bot runs on Mac', account=acct)
    """1回の実行: (1) 約定待ちを寄付で約定 (2) 保有をSL/TP/BE/時間切れ判定 (3) 新規シグナル検出→pendingへ"""
    with _lock:
        state=_load(acct)
        if not state['started_at']:
            state['started_at']=datetime.now().strftime('%Y-%m-%d %H:%M')
        S=state['settings']
        actions=[]
        # ---------- (1)&(2): 既存ポジションと約定待ち ----------
        symbols=set(list(state['positions'].keys())+list(state['pending'].keys()))
        for m in S['markets']:
            for sym,_ in UNIVERSE[m]: symbols.add(sym)
        data={}
        for sym in symbols:
            h=_fetch(sym)
            if h is not None: data[sym]=h

        # 約定待ち → 今日のバーが「シグナル日の翌日」以降なら、そのバーのOpenで約定
        for sym,p in list(state['pending'].items()):
            h=data.get(sym)
            if h is None: continue
            idx=[i for i,d in enumerate(h.index) if d.strftime('%Y-%m-%d')>p['signal_date']]
            if not idx: continue
            i=idx[0]
            entry=float(h['Open'].iloc[i])
            if len(state['positions'])>=S['max_positions']:
                _log(state,f"⏭ {p['name']} 約定見送り（ポジション上限）"); del state['pending'][sym]; continue
            budget=state['cash']*S['position_pct']/100
            if budget<entry and p['market']!='fx':
                _log(state,f"⏭ {p['name']} 約定見送り（資金不足）"); del state['pending'][sym]; continue
            qty=budget/entry
            a=p['atr']
            if p['side']=='L': sl,tp=entry-a*S['sl_atr'],entry+a*S['tp_atr']
            else: sl,tp=entry+a*S['sl_atr'],entry-a*S['tp_atr']
            state['cash']-=budget
            state['positions'][sym]=dict(side=p['side'],entry=entry,sl=sl,tp=tp,atr=a,be=False,qty=qty,cost=budget,
                                          opened=h.index[i].strftime('%Y-%m-%d'),name=p['name'],market=p['market'],bars=0)
            del state['pending'][sym]
            act=f"🟢 約定 {p['name']} {'買' if p['side']=='L' else '売'} @{entry:,.2f} 損切{sl:,.2f} 利確{tp:,.2f} 投入¥{budget:,.0f}"
            _log(state,act); actions.append(act)

        # 保有ポジションの管理（約定日以降のバーを順に判定）
        for sym,pos in list(state['positions'].items()):
            h=data.get(sym)
            if h is None: continue
            bars=[(d,r) for d,r in h.iterrows() if d.strftime('%Y-%m-%d')>pos['opened']]
            # 建値判定は「前日終値」ベース、SL/TPは当日高安ベースで順に
            closed=False
            for d,r in bars[pos.get('bars',0):]:
                pos['bars']=pos.get('bars',0)+1
                side=pos['side']; e=pos['entry']
                # 建値ストップ（前バーで含み益ATR×be_atr以上）
                if not pos['be']:
                    prev_close=float(h['Close'].loc[:d].iloc[-2]) if len(h.loc[:d])>=2 else e
                    unreal=(prev_close/e-1)*(1 if side=='L' else -1)
                    if unreal>=pos['atr']/e*S['be_atr']:
                        pos['be']=True; pos['sl']=e; _log(state,f"🛡 {pos['name']} 建値ストップ発動 SL→{e:,.2f}")
                hit=None
                if side=='L':
                    if float(r['Low'])<=pos['sl']: hit=('BE' if pos['be'] else 'SL',pos['sl'])
                    elif float(r['High'])>=pos['tp']: hit=('TP',pos['tp'])
                else:
                    if float(r['High'])>=pos['sl']: hit=('BE' if pos['be'] else 'SL',pos['sl'])
                    elif float(r['Low'])<=pos['tp']: hit=('TP',pos['tp'])
                if not hit and pos['bars']>=S['max_hold']: hit=('時間',float(r['Close']))
                if hit:
                    px=hit[1]; side_mult=1 if side=='L' else -1
                    pnl_pct=(px/e-1)*100*side_mult
                    cost_pct=S['cost_pct']
                    lev=float(S.get('leverage',1.0))
                    net_pct=pnl_pct*lev-cost_pct
                    proceeds=pos['cost']*(1+net_pct/100)
                    state['cash']+=proceeds
                    state['history'].append(dict(sym=sym,name=pos['name'],market=pos['market'],side=side,entry=e,exit=px,
                        pnl_pct=round(net_pct,2),pnl_yen=round(proceeds-pos['cost']),reason=hit[0],
                        opened=pos['opened'],closed=d.strftime('%Y-%m-%d'),days=pos['bars']))
                    emoji={'TP':'💰','SL':'🔴','BE':'⚪','時間':'⏰'}[hit[0]]
                    act=f"{emoji} 決済 {pos['name']} {hit[0]} @{px:,.2f} 損益 {net_pct:+.2f}% (¥{proceeds-pos['cost']:+,.0f})"
                    _log(state,act); actions.append(act)
                    del state['positions'][sym]; closed=True; break
            if not closed: state['positions'][sym]=pos

        # ---------- (3) 新規シグナル検出 ----------
        for m in S['markets']:
            for sym,name in UNIVERSE[m]:
                if sym in state['positions'] or sym in state['pending']: continue
                h=data.get(sym)
                if h is None: continue
                hh,i=_last_completed_bar(h,m)
                if i<25: continue
                ind=_indicators(hh)
                c=float(hh['Close'].iloc[i]); rsi=float(ind['rsi'].iloc[i]); a=float(ind['atr'].iloc[i])
                if np.isnan(rsi) or np.isnan(a) or a<=0: continue
                sig=None
                if c<float(ind['bbl'].iloc[i]) and rsi<30: sig='L'
                elif m!='jp' and c>float(ind['bbu'].iloc[i]) and rsi>70: sig='S'
                if sig:
                    sd=hh.index[i].strftime('%Y-%m-%d')
                    state['pending'][sym]=dict(side=sig,atr=a,signal_date=sd,name=name,market=m,close=c,rsi=round(rsi,1))
                    act=f"📡 シグナル {name} {'買' if sig=='L' else '売'}候補 終値{c:,.2f} RSI{rsi:.0f} → 翌寄付で約定予定"
                    _log(state,act); actions.append(act)

        # ---------- 資産推移 ----------
        equity=state['cash']
        for sym,pos in state['positions'].items():
            h=data.get(sym)
            if h is None: equity+=pos['cost']; continue
            cur=float(h['Close'].iloc[-1]); side_mult=1 if pos['side']=='L' else -1
            equity+=pos['cost']*(1+(cur/pos['entry']-1)*side_mult*float(S.get('leverage',1.0)))
        today=datetime.now().strftime('%Y-%m-%d')
        if state['equity_curve'] and state['equity_curve'][-1]['date']==today:
            state['equity_curve'][-1]['equity']=round(equity)
        else:
            state['equity_curve'].append({'date':today,'equity':round(equity)})
        state['last_run_at']=datetime.now().strftime('%Y-%m-%d %H:%M')
        if not actions: _log(state,"👀 巡回完了・変化なし")
        _save(state)
        return dict(actions=actions,equity=equity,positions=len(state['positions']),pending=len(state['pending']))

def status(acct='stock'):
    with _lock:
        state=_load(acct)
    # 現在値で評価
    pos_out=[]; equity=state['cash']
    for sym,pos in state['positions'].items():
        h=_fetch(sym,'1mo'); cur=float(h['Close'].iloc[-1]) if h is not None else pos['entry']
        side_mult=1 if pos['side']=='L' else -1
        lev=float(state['settings'].get('leverage',1.0))
        upnl=(cur/pos['entry']-1)*100*side_mult*lev
        val=pos['cost']*(1+upnl/100); equity+=val
        pos_out.append(dict(sym=sym,name=pos['name'],market=pos['market'],side=pos['side'],entry=pos['entry'],cur=cur,
                            sl=pos['sl'],tp=pos['tp'],be=pos['be'],unreal_pct=round(upnl,2),unreal_yen=round(val-pos['cost']),
                            opened=pos['opened'],days=pos.get('bars',0),cost=pos['cost']))
    hist=state['history']
    wins=[t for t in hist if t['pnl_pct']>0]
    total_pnl=equity-state['initial_capital']
    return dict(
        initial_capital=state['initial_capital'], cash=round(state['cash']), equity=round(equity),
        total_pnl=round(total_pnl), total_pnl_pct=round(total_pnl/state['initial_capital']*100,2),
        positions=pos_out, pending=state['pending'], history=hist[::-1][:100],
        trade_count=len(hist), win_rate=round(len(wins)/len(hist)*100,1) if hist else 0,
        avg_win=round(np.mean([t['pnl_pct'] for t in wins]),2) if wins else 0,
        avg_loss=round(np.mean([t['pnl_pct'] for t in hist if t['pnl_pct']<=0]),2) if len(hist)>len(wins) else 0,
        equity_curve=state['equity_curve'][-180:], log=state['log'][::-1][:60],
        started_at=state['started_at'], last_run_at=state['last_run_at'], settings=state['settings'],
        account=state.get('account','stock'), label=state.get('label',''), leverage=state['settings'].get('leverage',1.0),
        universe={m:[{'sym':s,'name':n} for s,n in v] for m,v in UNIVERSE.items()},
    )

def reset(capital=1_000_000, markets=None, acct='stock'):
    with _lock:
        d=_default_for(acct); d['initial_capital']=capital; d['cash']=capital
        _save(d)
    return d

# ---------- スケジューラ（サーバー内で自動巡回） ----------
_sched_started=False
def start_scheduler(interval_min=30):
    """30分ごとに run_once を回すバックグラウンドスレッド。日足戦略なので頻度は低くてよい。"""
    global _sched_started
    if IS_RENDER: return  # Renderは表示専用
    if _sched_started: return
    _sched_started=True
    def loop():
        import time
        time.sleep(20)  # 起動直後は待つ
        while True:
            for acct in ACCOUNTS:
                try: run_once(acct)
                except Exception: traceback.print_exc()
            time.sleep(interval_min*60)
    threading.Thread(target=loop,daemon=True).start()
