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
    # max_exposure_pct = レバ適用後の建玉合計の上限（対資産%）。ギャップで損切りが機能しない時の被弾量を縛る。
    # 株はレバ1倍なので100%＝信用を使わない。FXはレバ5倍かつ分散させたいので200%（1銘柄15%×5倍=75%が最大）。
    # allow_short: 株はバックテスト(2019-2026)で空売りが損失のほぼ全額を出したため停止。
    # FXは一度もバックテストしていないので、株の結果を流用せず従来どおり両建て可のままにする
    # （通貨ペアは上下対称で、株の「空売りが不利」という性質がそのまま当てはまらない）。
    'stock': dict(file='swing_bot_state.json',    markets=['jp','us'], leverage=1.0, cost_pct=0.10, label='株（日本+米国）',
                  max_exposure_pct=100, max_position_pct=25, allow_short=False),
    'fx':    dict(file='swing_bot_fx_state.json', markets=['fx'],      leverage=5.0, cost_pct=0.02, label='FX（レバ5倍）',
                  max_exposure_pct=200, max_position_pct=15, allow_short=True),
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
# セクター定義: 同じセクターは同時に1銘柄までしか持たない（分散の実効性を担保）
SECTOR = {
    # 仮想通貨連動（ビットコイン価格に強く連動する一群）
    'MSTR':'crypto', 'COIN':'crypto',
    # 半導体・AI（同じ設備投資サイクルで動く）
    '8035.T':'semi', '6857.T':'semi', '6920.T':'semi', '6146.T':'semi',
    'AMD':'semi', 'MU':'semi', 'NVDA':'semi', 'ARM':'semi', 'INTC':'semi', 'SMCI':'semi',
    # 電線・AI電力
    '5803.T':'power', '5802.T':'power',
    # 防衛・重工
    '7013.T':'defense', '7011.T':'defense',
    # 日本ハイテク・その他
    '9984.T':'jp_tech', '6758.T':'jp_tech', '7974.T':'jp_game',
    '4568.T':'pharma',
    # 米国その他
    'TSLA':'ev', 'PLTR':'us_soft',
    # FX: 通貨ごとにグループ化（円ペア/ドルストレートで相関が高い）
    'USDJPY=X':'jpy', 'GBPJPY=X':'jpy', 'AUDJPY=X':'jpy', 'EURJPY=X':'jpy',
    'NZDJPY=X':'jpy', 'MXNJPY=X':'jpy',
    'EURUSD=X':'usd_straight', 'GBPUSD=X':'usd_straight',
}
def _sector_of(sym): return SECTOR.get(sym, sym)

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
        'position_pct': 10,      # （旧）固定配分。sizing='fixed' の時のみ使用
        'sizing': 'risk',        # 'risk'=1トレードのリスク額を一定にする / 'fixed'=旧方式
        'risk_pct': 1.0,         # 1トレードで失ってよい資産の割合(%)。損切りに当たった時の損失額
        'min_position_pct': 3,   # 1銘柄の下限（小さすぎる建玉を防ぐ）
        'max_position_pct': 25,  # 1銘柄の上限（低ボラ銘柄への集中を防ぐ）
        'max_positions': 8,
        'markets': ['jp','us'],
        'sl_atr': 1.5, 'tp_atr': 2.5, 'be_atr': 1.0, 'max_hold': 10,
        'cost_pct': 0.10,        # 往復コスト
        'leverage': 1.0,         # FX口座は5倍
        'annual_interest': 0.0,  # FXスワップは無視（概算）
        'strategies': ['reversal','breakout'],  # 逆張り + 順張り（相関-0.79で補完関係）
        'max_per_sector': 1,     # 同一セクターは1銘柄まで
        'max_per_strategy': 4,   # 1戦略あたり最大4ポジション
        'earnings_guard': True,  # 決算をまたがない（株のみ。FXは決算がないので無効）
        'earnings_buffer': 2,    # 決算の何日前に手仕舞うか（データ遅延・時差の余裕を見て2日）
        'max_exposure_pct': 100, # レバ適用後の建玉合計の上限（対資産%）
        'allow_short': False,    # 口座定義(ACCOUNTS)で上書きされる。株=False / FX=True
    }
}

def _default_for(acct):
    d=json.loads(json.dumps(DEFAULT)); a=ACCOUNTS[acct]
    d['settings'].update(markets=a['markets'], leverage=a['leverage'], cost_pct=a['cost_pct'],
                         max_exposure_pct=a['max_exposure_pct'], max_position_pct=a['max_position_pct'],
                         allow_short=a['allow_short'])
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
        if d:
            d['_remote_load_failed']=False
            return d
        # リモート取得失敗: ローカルファイルもRenderには無いはずなので、
        # 「失敗した」というフラグ付きの初期状態を返す（無言で0円スタートに見せない）
        d=_default_for(acct); d['_remote_load_failed']=True
        return d
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
        d['settings']['max_exposure_pct']=ACCOUNTS[acct]['max_exposure_pct']
        d['settings']['max_position_pct']=ACCOUNTS[acct]['max_position_pct']
        d['settings']['allow_short']=ACCOUNTS[acct]['allow_short']
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
        if h is None or len(h)==0: return None
        h=h[['Open','High','Low','Close']].dropna()
        # 巡回のシグナル計算(指標に必要な最低本数)と、単なる現在値取得を区別する
        return h
    except Exception:
        return None

def _has_enough_bars(h, min_bars=40):
    return h is not None and len(h) >= min_bars

# ---- 決算日ガード -------------------------------------------------
# 決算はザラ場外に出るので、損切り注文は素通りされる（翌朝いきなり-15%で寄る）。
# 「売られすぎだから戻る」という前提が、決算という新情報で無効になるのが本質的な問題。
# 避けられる負けなので、決算をまたぐ持ち越しはしない。
_EARN_CACHE = {}   # sym -> (取得日, date or None)

def _earnings_date(sym):
    """次回決算日を返す。取得できない/過去日付なら None（＝不明）。
    yfinanceは銘柄によって古い日付を返すことがある（実測: 9984.Tが19日前の日付を返した）。
    不明を『決算なし』と誤読すると穴になるので、呼び出し側で安全側に倒す。"""
    if _market_of(sym) == 'fx':
        return None
    today = datetime.now().date()
    hit = _EARN_CACHE.get(sym)
    if hit and hit[0] == today:
        return hit[1]
    d = None
    try:
        cal = yf.Ticker(sym).calendar
        ed = cal.get('Earnings Date') if isinstance(cal, dict) else None
        if ed:
            v = ed[0] if isinstance(ed, (list, tuple)) else ed
            v = v.date() if hasattr(v, 'date') else v
            # 過去日付は古いデータ。信用せず「不明」に倒す
            if v and v >= today:
                d = v
    except Exception:
        d = None
    _EARN_CACHE[sym] = (today, d)
    return d

def _earnings_within(sym, days, unknown_is_risky=True):
    """今日から days 営業日以内に決算があるなら (True, 決算日) を返す。
    決算日が取得できない場合は unknown_is_risky に従う（既定: 危険側＝True）。"""
    if _market_of(sym) == 'fx':
        return False, None
    d = _earnings_date(sym)
    if d is None:
        return (unknown_is_risky, None)
    # 営業日換算はしない（暦日で多めに見る＝安全側）
    return ((d - datetime.now().date()).days <= days, d)

def _indicators(h):
    c,hi,lo=h['Close'],h['High'],h['Low']
    bb=ta.volatility.BollingerBands(c,20,2)
    return dict(
        bbu=bb.bollinger_hband(), bbl=bb.bollinger_lband(),
        rsi=ta.momentum.RSIIndicator(c,7).rsi(),
        atr=ta.volatility.AverageTrueRange(hi,lo,c,14).average_true_range(),
        # 順張り(ブレイクアウト)用
        don_hi=hi.rolling(20).max(), don_lo=lo.rolling(20).min(),
        adx=ta.trend.ADXIndicator(hi,lo,c,14).adx(),
    )

def _detect_signal(strategy, ind, i, close, allow_short):
    """戦略ごとのシグナル判定。(side, reason) を返す。無ければ (None, '')"""
    c = float(close.iloc[i])
    try:
        if strategy == 'reversal':
            rsi = float(ind['rsi'].iloc[i])
            if c < float(ind['bbl'].iloc[i]) and rsi < 30:
                return 'L', f'売られすぎ(RSI{rsi:.0f}・下限割れ)'
            if allow_short and c > float(ind['bbu'].iloc[i]) and rsi > 70:
                return 'S', f'買われすぎ(RSI{rsi:.0f}・上限超え)'
        elif strategy == 'breakout':
            adx = float(ind['adx'].iloc[i])
            if adx <= 20:
                return None, ''
            prev_hi = float(ind['don_hi'].iloc[i-1]); prev_lo = float(ind['don_lo'].iloc[i-1])
            if c >= prev_hi:
                return 'L', f'20日高値ブレイク(ADX{adx:.0f})'
            if allow_short and c <= prev_lo:
                return 'S', f'20日安値ブレイク(ADX{adx:.0f})'
    except Exception:
        pass
    return None, ''

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
        fetch_fail=[]
        for sym in symbols:
            h=_fetch(sym)
            if h is not None: data[sym]=h
            else: fetch_fail.append(ALL_NAME.get(sym,sym))
        if fetch_fail:
            _log(state, f"⚠️ データ取得失敗: {', '.join(fetch_fail)}（{len(fetch_fail)}/{len(symbols)}銘柄）")
        state['last_fetch_fail_count']=len(fetch_fail)
        state['last_fetch_total']=len(symbols)

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
            a_pre=p['atr']
            lev=float(S.get('leverage',1.0))
            # --- ポジションサイズ決定 ---
            if S.get('sizing','risk')=='risk' and a_pre>0 and entry>0:
                # 損切りまでの距離（％）。ATR×sl_atr が実際の損失幅になる
                stop_dist_pct = (a_pre*S['sl_atr'])/entry
                # 建玉に対する損失率 = stop_dist_pct × レバレッジ
                loss_rate = stop_dist_pct*lev
                # equity（現金＋建玉の簿価）を基準にする
                equity_base = state['cash']+sum(pp['cost'] for pp in state['positions'].values())
                risk_amount = equity_base*S.get('risk_pct',1.0)/100
                budget = risk_amount/loss_rate if loss_rate>0 else 0
                lo_cap = equity_base*S.get('min_position_pct',3)/100
                hi_cap = equity_base*S.get('max_position_pct',25)/100
                budget = max(lo_cap, min(hi_cap, budget))
                budget = min(budget, state['cash'])   # 現金以上は使えない
                size_note = f"想定損失{risk_amount:,.0f}円/損切幅{stop_dist_pct*100*lev:.1f}%"
            else:
                budget=state['cash']*S['position_pct']/100
                size_note = f"固定{S['position_pct']}%"
            # --- レバ適用後の合計エクスポージャー上限 ---
            # FXはレバ5倍なので、1銘柄25%上限でも実効125%になりうる（検証で指摘された穴）。
            # 建玉合計×レバが資産の max_exposure_pct を超えないところまで削る
            max_exp_pct=float(S.get('max_exposure_pct',100))
            if max_exp_pct>0 and lev>0:
                eq_now=state['cash']+sum(pp['cost'] for pp in state['positions'].values())
                used_exp=sum(pp['cost'] for pp in state['positions'].values())*lev
                room=(eq_now*max_exp_pct/100)-used_exp
                allowed=room/lev
                if allowed<=0:
                    _log(state,f"⏭ {p['name']} 約定見送り（エクスポージャー上限{max_exp_pct:.0f}%に到達）")
                    del state['pending'][sym]; continue
                if budget>allowed:
                    budget=allowed
                    size_note+=f"/上限{max_exp_pct:.0f}%で縮小"
            if budget<=0 or (budget<entry and p['market']!='fx'):
                _log(state,f"⏭ {p['name']} 約定見送り（資金不足）"); del state['pending'][sym]; continue
            qty=budget/entry
            a=p['atr']
            if p['side']=='L': sl,tp=entry-a*S['sl_atr'],entry+a*S['tp_atr']
            else: sl,tp=entry+a*S['sl_atr'],entry-a*S['tp_atr']
            state['cash']-=budget
            state['positions'][sym]=dict(side=p['side'],entry=entry,sl=sl,tp=tp,atr=a,be=False,qty=qty,cost=budget,
                                          opened=h.index[i].strftime('%Y-%m-%d'),name=p['name'],market=p['market'],bars=0,
                                          strategy=p.get('strategy','reversal'),reason=p.get('reason',''))
            del state['pending'][sym]
            _tag='逆張り' if p.get('strategy','reversal')=='reversal' else '順張り'
            act=f"🟢 約定[{_tag}] {p['name']} {'買' if p['side']=='L' else '売'} @{entry:,.2f} 損切{sl:,.2f} 利確{tp:,.2f} 投入¥{budget:,.0f}（{size_note}）"
            _log(state,act); actions.append(act)

        # 保有ポジションの管理（約定日以降のバーを順に判定）
        for sym,pos in list(state['positions'].items()):
            h=data.get(sym)
            if h is None:
                _log(state, f"⚠️ {pos['name']} の当日データ取得失敗、この巡回はスキップ")
                continue
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
                # 決算が迫ったらシグナルに関係なく手仕舞う（ギャップは損切りを素通りするため）
                # 決算日が取得できない銘柄まで強制決済すると無駄な回転を生むので、
                # 決済側では「不明＝安全」に倒す（入口側では逆に「不明＝危険」で見送る）
                if not hit and S.get('earnings_guard',True) and _market_of(sym)!='fx':
                    near,ed=_earnings_within(sym,S.get('earnings_buffer',1),unknown_is_risky=False)
                    if near: hit=('決算前',float(r['Close']))
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
                        opened=pos['opened'],closed=d.strftime('%Y-%m-%d'),days=pos['bars'],
                        strategy=pos.get('strategy','reversal')))
                    emoji={'TP':'💰','SL':'🔴','BE':'⚪','時間':'⏰','決算前':'📅'}[hit[0]]
                    act=f"{emoji} 決済 {pos['name']} {hit[0]} @{px:,.2f} 損益 {net_pct:+.2f}% (¥{proceeds-pos['cost']:+,.0f})"
                    _log(state,act); actions.append(act)
                    del state['positions'][sym]; closed=True; break
            if not closed: state['positions'][sym]=pos

        # ---------- (3) 新規シグナル検出（複数戦略 + セクター分散） ----------
        # 現在使用中のセクターを集計（保有＋約定待ち）
        used_sectors={}
        for sym in list(state['positions'].keys())+list(state['pending'].keys()):
            sec=_sector_of(sym); used_sectors[sec]=used_sectors.get(sec,0)+1
        # 戦略ごとの現在の保有数
        strat_count={}
        for p in list(state['positions'].values())+list(state['pending'].values()):
            st=p.get('strategy','reversal'); strat_count[st]=strat_count.get(st,0)+1

        enabled=S.get('strategies',['reversal','breakout'])
        max_per_sector=int(S.get('max_per_sector',1))
        max_per_strategy=int(S.get('max_per_strategy',4))

        candidates=[]
        for m in S['markets']:
            for sym,name in UNIVERSE[m]:
                if sym in state['positions'] or sym in state['pending']: continue
                h=data.get(sym)
                if not _has_enough_bars(h, 40): continue
                hh,i=_last_completed_bar(h,m)
                if i<25: continue
                ind=_indicators(hh)
                a=float(ind['atr'].iloc[i])
                if np.isnan(a) or a<=0: continue
                allow_short = bool(S.get('allow_short',False)) and (m!='jp')
                for strategy in enabled:
                    sig,reason=_detect_signal(strategy, ind, i, hh['Close'], allow_short)
                    if not sig: continue
                    c=float(hh['Close'].iloc[i]); rsi=float(ind['rsi'].iloc[i])
                    candidates.append(dict(sym=sym,name=name,market=m,side=sig,atr=a,strategy=strategy,
                                           reason=reason,close=c,rsi=round(rsi,1) if not np.isnan(rsi) else 50,
                                           signal_date=hh.index[i].strftime('%Y-%m-%d')))
                    break  # 1銘柄につき最初に成立した戦略のみ

        # セクター分散・戦略枠の制約をかけながら採用
        for cand in candidates:
            sec=_sector_of(cand['sym'])
            if used_sectors.get(sec,0)>=max_per_sector:
                _log(state,f"⏭ {cand['name']} 見送り（{sec}セクターは既に保有中）")
                continue
            if strat_count.get(cand['strategy'],0)>=max_per_strategy:
                _log(state,f"⏭ {cand['name']} 見送り（{cand['strategy']}戦略の枠が上限）")
                continue
            if len(state['positions'])+len(state['pending'])>=S['max_positions']:
                _log(state,f"⏭ {cand['name']} 見送り（全体のポジション上限）")
                continue
            # 保有予定期間(max_hold)の中に決算が入るならエントリーしない。
            # 入口では「決算日が不明な銘柄」も見送る（見送りは機会損失で済むが、
            # 決算を踏み抜くと損切りが機能しないので損失が青天井になる）
            if S.get('earnings_guard',True) and cand['market']!='fx':
                near,ed=_earnings_within(cand['sym'],int(S.get('max_hold',10)),unknown_is_risky=True)
                if near:
                    when=ed.strftime('%m/%d') if ed else '不明'
                    _log(state,f"⏭ {cand['name']} 見送り（保有期間中に決算 {when}）")
                    continue
            state['pending'][cand['sym']]=dict(side=cand['side'],atr=cand['atr'],signal_date=cand['signal_date'],
                name=cand['name'],market=cand['market'],close=cand['close'],rsi=cand['rsi'],
                strategy=cand['strategy'],reason=cand['reason'])
            used_sectors[sec]=used_sectors.get(sec,0)+1
            strat_count[cand['strategy']]=strat_count.get(cand['strategy'],0)+1
            tag='逆張り' if cand['strategy']=='reversal' else '順張り'
            act=f"📡 シグナル[{tag}] {cand['name']} {'買' if cand['side']=='L' else '売'}候補 {cand['reason']} 終値{cand['close']:,.2f} → 翌寄付で約定予定"
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
    price_fail=[]
    for sym,pos in state['positions'].items():
        h=_fetch(sym,'1mo')
        stale = h is None
        cur=float(h['Close'].iloc[-1]) if h is not None else pos['entry']
        if stale: price_fail.append(pos['name'])
        side_mult=1 if pos['side']=='L' else -1
        lev=float(state['settings'].get('leverage',1.0))
        upnl=(cur/pos['entry']-1)*100*side_mult*lev
        val=pos['cost']*(1+upnl/100); equity+=val
        pos_out.append(dict(sym=sym,name=pos['name'],market=pos['market'],side=pos['side'],entry=pos['entry'],cur=cur,
                            sl=pos['sl'],tp=pos['tp'],be=pos['be'],unreal_pct=round(upnl,2),unreal_yen=round(val-pos['cost']),
                            opened=pos['opened'],days=pos.get('bars',0),cost=pos['cost'],stale=stale,
                            strategy=pos.get('strategy','reversal'),reason=pos.get('reason','')))
    hist=state['history']
    wins=[t for t in hist if t['pnl_pct']>0]
    total_pnl=equity-state['initial_capital']
    # ---- 健全性チェック（サイレント故障を可視化） ----
    warnings=[]
    if state.get('last_run_at'):
        try:
            last=datetime.strptime(state['last_run_at'],'%Y-%m-%d %H:%M')
            mins=(datetime.now()-last).total_seconds()/60
            if mins>90:
                warnings.append(f"⚠️ 最終巡回から{int(mins)}分経過（想定30分ごと）。Macが寝ているか停止している可能性")
        except Exception:
            pass
    else:
        warnings.append("⚠️ まだ一度もbotが巡回していません")
    ff=state.get('last_fetch_fail_count',0); ft=state.get('last_fetch_total',0)
    if ft and ff/ft>0.3:
        warnings.append(f"⚠️ 前回巡回でデータ取得失敗が多発（{ff}/{ft}銘柄）。Yahoo Financeの一時制限の可能性")
    if price_fail:
        warnings.append(f"⚠️ 現在値の取得に失敗中: {', '.join(price_fail)}（表示は建値で代用・不正確）")
    if IS_RENDER and state.get('_remote_load_failed'):
        warnings.append("⚠️ GitHub上の最新データを取得できず、この画面は古い/初期状態を表示している可能性があります")

    # 戦略別サマリー
    by_strategy={}
    for t in hist:
        st=t.get('strategy','reversal')
        d0=by_strategy.setdefault(st, dict(n=0,wins=0,pnl=0.0))
        d0['n']+=1; d0['pnl']+=t['pnl_yen']
        if t['pnl_pct']>0: d0['wins']+=1
    for st,v in by_strategy.items():
        v['win_rate']=round(v['wins']/v['n']*100,1) if v['n'] else 0
        v['pnl']=round(v['pnl'])

    return dict(
        by_strategy=by_strategy,
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
        warnings=warnings, healthy=len(warnings)==0,
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
