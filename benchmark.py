#!/usr/bin/env python3
"""対戦相手: S&P500(SPY)を買って何もしない口座。
botが「手間をかける価値があるか」を判定するための基準線。
円建てで比較するため、SPYの値動きに為替(USDJPY)も掛ける（実際に日本から買った場合に合わせる）。
"""
import os, json
from datetime import datetime, timedelta, timezone
import warnings; warnings.filterwarnings('ignore')
import yfinance as yf

BASE = os.path.dirname(os.path.abspath(__file__))
FILE = os.path.join(BASE, 'benchmark_state.json')
RAW  = 'https://raw.githubusercontent.com/imukte555/stock-analyzer/main/benchmark_state.json'
IS_RENDER = bool(os.environ.get('RENDER'))
JST  = timezone(timedelta(hours=9))
def _now():   return datetime.now(JST).replace(tzinfo=None)
def _today(): return datetime.now(JST).date()

CAPITAL = 1_000_000

def _px(sym):
    """終値を返す。取れなければNone（数字を捏造しない）"""
    try:
        h = yf.Ticker(sym).history(period='5d')
        if h is None or len(h) == 0: return None
        return float(h['Close'].iloc[-1])
    except Exception:
        return None

def load():
    # Renderは表示専用。計算はMac側で行い、結果をGitHub経由で読む
    if IS_RENDER:
        try:
            import urllib.request
            url = RAW + '?t=' + _now().strftime('%Y%m%d%H%M')
            with urllib.request.urlopen(url, timeout=10) as r:
                return json.loads(r.read().decode())
        except Exception:
            pass
    if os.path.exists(FILE):
        return json.load(open(FILE))
    return {'label': 'S&P500を買って放置', 'initial_capital': CAPITAL,
            'start_date': None, 'entry_spy': None, 'entry_fx': None,
            'units': None, 'daily': [], 'last_run_at': None}

def save(d):
    tmp = FILE + '.tmp'
    json.dump(d, open(tmp, 'w'), ensure_ascii=False, indent=1)
    os.replace(tmp, FILE)

def run_once():
    d = load()
    spy, fx = _px('SPY'), _px('USDJPY=X')
    if spy is None or fx is None:
        d['last_error'] = f'価格取得失敗 (SPY={spy}, USDJPY={fx})'
        save(d); return {'ok': False, 'error': d['last_error']}
    # 初回: 全額でSPYを買う
    if d['units'] is None:
        d.update(start_date=str(_today()), entry_spy=spy, entry_fx=fx,
                 units=CAPITAL / (spy * fx))
    eq = d['units'] * spy * fx
    ret = (eq / CAPITAL - 1) * 100
    today = str(_today())
    d['daily'] = [x for x in d['daily'] if x['date'] != today]
    d['daily'].append({'date': today, 'equity': round(eq), 'spy': round(spy, 2),
                       'usdjpy': round(fx, 2), 'ret_pct': round(ret, 3)})
    d['daily'] = d['daily'][-400:]
    d['last_run_at'] = _now().strftime('%Y-%m-%d %H:%M')
    d.pop('last_error', None)
    save(d)
    return {'ok': True, 'equity': round(eq), 'ret_pct': round(ret, 2),
            'spy': round(spy, 2), 'usdjpy': round(fx, 2)}

def status():
    d = load()
    if d['units'] is None:
        return {'label': d['label'], 'started': False}
    last = d['daily'][-1] if d['daily'] else None
    return {'label': d['label'], 'started': True, 'start_date': d['start_date'],
            'initial_capital': CAPITAL,
            'equity': last['equity'] if last else CAPITAL,
            'ret_pct': last['ret_pct'] if last else 0.0,
            'entry_spy': round(d['entry_spy'], 2), 'entry_fx': round(d['entry_fx'], 2),
            'spy': last['spy'] if last else None, 'usdjpy': last['usdjpy'] if last else None,
            'last_run_at': d['last_run_at'], 'daily': d['daily'][-120:],
            'error': d.get('last_error')}

if __name__ == '__main__':
    print(run_once())
