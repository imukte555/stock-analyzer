from flask import Flask, render_template, request, jsonify
from flask_cors import CORS
import yfinance as yf
import pandas as pd
import numpy as np
import ta
import json
import requests as req
from datetime import datetime, timedelta
import warnings
warnings.filterwarnings('ignore')

try:
    from deep_translator import GoogleTranslator
    _translator = GoogleTranslator(source='en', target='ja')
    def translate_ja(text):
        try:
            if not text or not text.strip(): return text
            return _translator.translate(text[:400]) or text
        except:
            return text
except ImportError:
    def translate_ja(text): return text

app = Flask(__name__)
CORS(app)

def get_rsi_signal(rsi):
    if rsi is None or np.isnan(rsi):
        return "neutral", "データなし"
    if rsi < 30:
        return "buy", f"RSI {rsi:.1f} — 売られすぎゾーン。反発の可能性大"
    elif rsi < 45:
        return "weak_buy", f"RSI {rsi:.1f} — やや売られ気味。押し目買いチャンス"
    elif rsi > 70:
        return "sell", f"RSI {rsi:.1f} — 買われすぎゾーン。利確・売り検討"
    elif rsi > 55:
        return "weak_sell", f"RSI {rsi:.1f} — やや買われ気味。過熱感あり"
    else:
        return "neutral", f"RSI {rsi:.1f} — 中立ゾーン"

def get_macd_signal(macd, signal, hist):
    if macd is None or signal is None:
        return "neutral", "データなし"
    diff = macd - signal
    if diff > 0 and hist > 0:
        return "buy", f"MACDがシグナル線の上。上昇モメンタム継続中"
    elif diff > 0 and hist < 0:
        return "weak_buy", f"MACDは上だが勢い鈍化。トレンド転換に注意"
    elif diff < 0 and hist < 0:
        return "sell", f"MACDがシグナル線の下。下降トレンド継続"
    else:
        return "weak_sell", f"MACDは下だが下落鈍化。底打ち確認待ち"

def get_bb_signal(close, bb_upper, bb_lower, bb_mid):
    if close is None or bb_upper is None:
        return "neutral", "データなし"
    bb_width = bb_upper - bb_lower
    pos = (close - bb_lower) / bb_width * 100 if bb_width > 0 else 50
    if close <= bb_lower:
        return "buy", f"ボリンジャー下限タッチ。統計的反発ゾーン（位置: {pos:.0f}%）"
    elif close >= bb_upper:
        return "sell", f"ボリンジャー上限タッチ。統計的天井ゾーン（位置: {pos:.0f}%）"
    elif pos < 30:
        return "weak_buy", f"バンド下半分（位置: {pos:.0f}%）。底値圏に近い"
    elif pos > 70:
        return "weak_sell", f"バンド上半分（位置: {pos:.0f}%）。高値圏に近い"
    else:
        return "neutral", f"バンド中央付近（位置: {pos:.0f}%）"

def get_ma_signal(close, ma5, ma25, ma75):
    signals = []
    score = 0
    if ma5 and ma25:
        if ma5 > ma25:
            signals.append("短期MA > 中期MA（上昇トレンド）")
            score += 1
        else:
            signals.append("短期MA < 中期MA（下降トレンド）")
            score -= 1
    if ma25 and ma75:
        if ma25 > ma75:
            signals.append("中期MA > 長期MA（ゴールデンクロス圏）")
            score += 1
        else:
            signals.append("中期MA < 長期MA（デッドクロス圏）")
            score -= 1
    if close and ma75:
        if close > ma75:
            signals.append(f"現値が長期MAの上（+{((close/ma75)-1)*100:.1f}%）")
            score += 1
        else:
            signals.append(f"現値が長期MAの下（{((close/ma75)-1)*100:.1f}%）")
            score -= 1
    desc = " / ".join(signals) if signals else "データなし"
    if score >= 2:
        return "buy", desc
    elif score == 1:
        return "weak_buy", desc
    elif score == -1:
        return "weak_sell", desc
    elif score <= -2:
        return "sell", desc
    else:
        return "neutral", desc

def get_volume_signal(vol, avg_vol, price_change):
    if vol is None or avg_vol is None or avg_vol == 0:
        return "neutral", "データなし"
    ratio = vol / avg_vol
    if ratio > 1.5 and price_change > 0:
        return "buy", f"出来高{ratio:.1f}倍 + 株価上昇 = 強い買いシグナル"
    elif ratio > 1.5 and price_change < 0:
        return "sell", f"出来高{ratio:.1f}倍 + 株価下落 = 強い売りシグナル"
    elif ratio > 1.2:
        return "weak_buy" if price_change > 0 else "weak_sell", f"出来高{ratio:.1f}倍。市場の注目度が高い"
    else:
        return "neutral", f"出来高は平均の{ratio:.1f}倍。通常レベル"

def score_to_verdict(scores):
    buy_count = scores.count("buy") + scores.count("weak_buy") * 0.5
    sell_count = scores.count("sell") + scores.count("weak_sell") * 0.5
    strong_buy = scores.count("buy")
    strong_sell = scores.count("sell")

    if strong_buy >= 3:
        return "STRONG_BUY", 95
    elif buy_count >= 3:
        return "BUY", 75
    elif buy_count >= 2:
        return "WEAK_BUY", 60
    elif strong_sell >= 3:
        return "STRONG_SELL", 5
    elif sell_count >= 3:
        return "SELL", 25
    elif sell_count >= 2:
        return "WEAK_SELL", 40
    else:
        return "NEUTRAL", 50

# ===== 為替（FX）通貨ペアリスト =====
FOREX_PAIRS = [
    # 円ベース（日本人に人気）
    {'symbol':'USDJPY=X','name':'米ドル/円','desc':'最も取引量の多い基軸ペア。米国の金利・経済指標に敏感。'},
    {'symbol':'EURJPY=X','name':'ユーロ/円','desc':'ユーロ圏経済と日本の影響を受ける。値動きはやや大きめ。'},
    {'symbol':'GBPJPY=X','name':'英ポンド/円','desc':'値動きが激しく、上級者向け。ボラティリティ高い。'},
    {'symbol':'AUDJPY=X','name':'豪ドル/円','desc':'資源国通貨。リスクオン時に買われやすい。'},
    {'symbol':'NZDJPY=X','name':'NZドル/円','desc':'高金利通貨ペア。スワップポイント狙いに人気。'},
    {'symbol':'CADJPY=X','name':'加ドル/円','desc':'原油価格と連動しやすい資源国通貨。'},
    {'symbol':'CHFJPY=X','name':'スイスフラン/円','desc':'安全通貨ペア。リスクオフで買われやすい。'},
    {'symbol':'TRYJPY=X','name':'トルコリラ/円','desc':'超高金利通貨。スワップ狙いだが為替変動リスク大。'},
    {'symbol':'ZARJPY=X','name':'南アランド/円','desc':'高金利新興国通貨。リスク高め。'},
    {'symbol':'MXNJPY=X','name':'メキシコペソ/円','desc':'高金利新興国通貨。原油との関連性あり。'},
    # ドルストレート（メジャー）
    {'symbol':'EURUSD=X','name':'ユーロ/米ドル','desc':'世界一取引量の多い通貨ペア。流動性最高。'},
    {'symbol':'GBPUSD=X','name':'英ポンド/米ドル','desc':'通称ケーブル。値動き激しい。'},
    {'symbol':'AUDUSD=X','name':'豪ドル/米ドル','desc':'資源国×米ドル。商品市況に敏感。'},
    {'symbol':'NZDUSD=X','name':'NZドル/米ドル','desc':'豪ドルと連動傾向。流動性は中程度。'},
    {'symbol':'USDCHF=X','name':'米ドル/スイスフラン','desc':'スイスフランは安全通貨。リスクオフで下落。'},
    {'symbol':'USDCAD=X','name':'米ドル/加ドル','desc':'原油価格と逆相関しやすい。'},
    # クロス
    {'symbol':'EURGBP=X','name':'ユーロ/英ポンド','desc':'欧州内クロス。値動き穏やか。'},
    {'symbol':'EURAUD=X','name':'ユーロ/豪ドル','desc':'欧州×資源国。比較的トレンド出やすい。'},
    {'symbol':'GBPAUD=X','name':'英ポンド/豪ドル','desc':'値動き大きいクロスペア。'},
    {'symbol':'AUDNZD=X','name':'豪ドル/NZドル','desc':'同じ大洋州通貨同士。穏やかな値動き。'},
]

# 主要中央銀行政策金利（2024-2026年の概算値、参考用）
# 実際のリアルタイムレートはAPIで取りに行くべきだが、概算で十分な使い方
CENTRAL_BANK_RATES = {
    'USD': {'rate': 4.50, 'bank': '米連邦準備制度（FRB）',  'stance': '利下げサイクル中（2024〜）',  'next': 'インフレ動向次第で追加利下げ'},
    'JPY': {'rate': 0.50, 'bank': '日本銀行（BOJ）',         'stance': '段階的な利上げ局面',           'next': '物価・賃金次第で追加利上げ'},
    'EUR': {'rate': 3.00, 'bank': '欧州中央銀行（ECB）',     'stance': '利下げ局面',                  'next': '景気・インフレ次第'},
    'GBP': {'rate': 4.50, 'bank': 'イングランド銀行（BOE）','stance': '慎重に利下げ中',              'next': 'インフレ次第で追加利下げ'},
    'AUD': {'rate': 4.10, 'bank': 'オーストラリア準備銀行（RBA）','stance': '高金利維持',          'next': 'コアCPIが鍵'},
    'NZD': {'rate': 4.25, 'bank': 'ニュージーランド準備銀行（RBNZ）','stance': '利下げ局面',         'next': '内需・労働市場次第'},
    'CAD': {'rate': 3.00, 'bank': 'カナダ銀行（BOC）',       'stance': '利下げ局面',                  'next': '原油・米経済次第'},
    'CHF': {'rate': 0.50, 'bank': 'スイス国立銀行（SNB）',   'stance': '低金利・介入警戒',           'next': '為替動向次第'},
    'TRY': {'rate': 45.00,'bank': 'トルコ中央銀行（TCMB）',  'stance': '超高金利政策',                'next': 'インフレ抑制最優先'},
    'ZAR': {'rate': 7.75, 'bank': '南アフリカ準備銀行（SARB）','stance': '高金利維持',                'next': 'インフレ次第'},
    'MXN': {'rate': 9.50, 'bank': 'メキシコ銀行（Banxico）', 'stance': '利下げ局面',                  'next': '米経済・関税リスク'},
}

def normalize_forex_symbol(sym):
    """USDJPY → USDJPY=X / USD/JPY → USDJPY=X"""
    s = (sym or '').strip().upper().replace('/','').replace(' ','')
    if not s: return ''
    if not s.endswith('=X'):
        s = s + '=X'
    return s

def get_forex_meta(symbol):
    """通貨ペアのメタ情報を取得"""
    for p in FOREX_PAIRS:
        if p['symbol'] == symbol:
            return p
    # 不明な通貨ペアでも自動でラベル生成
    base = symbol.replace('=X','')
    if len(base) == 6:
        c1, c2 = base[:3], base[3:]
        cmap = {'USD':'米ドル','JPY':'円','EUR':'ユーロ','GBP':'英ポンド','AUD':'豪ドル','NZD':'NZドル',
                'CAD':'加ドル','CHF':'スイスフラン','CNY':'人民元','HKD':'香港ドル','SGD':'シンガポールドル',
                'TRY':'トルコリラ','ZAR':'南アランド','MXN':'メキシコペソ','KRW':'韓国ウォン'}
        n = f'{cmap.get(c1,c1)}/{cmap.get(c2,c2)}'
        return {'symbol':symbol, 'name':n, 'desc':f'{c1}と{c2}の通貨ペア'}
    return {'symbol':symbol, 'name':symbol, 'desc':''}


WATCHLIST = [
    # 米国 メガテック
    {"symbol": "AAPL",  "name": "Apple"},
    {"symbol": "MSFT",  "name": "Microsoft"},
    {"symbol": "NVDA",  "name": "NVIDIA"},
    {"symbol": "GOOGL", "name": "Alphabet"},
    {"symbol": "META",  "name": "Meta"},
    {"symbol": "AMZN",  "name": "Amazon"},
    {"symbol": "TSLA",  "name": "Tesla"},
    {"symbol": "AMD",   "name": "AMD"},
    # 米国 金融
    {"symbol": "JPM",   "name": "JPMorgan"},
    {"symbol": "BAC",   "name": "Bank of America"},
    {"symbol": "GS",    "name": "Goldman Sachs"},
    # 米国 その他
    {"symbol": "NFLX",  "name": "Netflix"},
    {"symbol": "DIS",   "name": "Disney"},
    {"symbol": "XOM",   "name": "Exxon Mobil"},
    {"symbol": "JNJ",   "name": "Johnson & Johnson"},
    {"symbol": "PFE",   "name": "Pfizer"},
    {"symbol": "UBER",  "name": "Uber"},
    # 米国 ETF
    {"symbol": "SPY",   "name": "S&P500 ETF"},
    {"symbol": "QQQ",   "name": "NASDAQ ETF"},
    # 日本株
    {"symbol": "7203.T", "name": "トヨタ"},
    {"symbol": "9984.T", "name": "SoftBank G"},
    {"symbol": "6758.T", "name": "ソニー"},
    {"symbol": "7974.T", "name": "任天堂"},
    {"symbol": "9432.T", "name": "NTT"},
    {"symbol": "6861.T", "name": "キーエンス"},
    {"symbol": "8306.T", "name": "三菱UFJ"},
    {"symbol": "4063.T", "name": "信越化学"},
    {"symbol": "6367.T", "name": "ダイキン"},
    {"symbol": "9983.T", "name": "ファストリ"},
    {"symbol": "4519.T", "name": "中外製薬"},
]

SECTOR_JA = {
    'Technology': 'テクノロジー', 'Financial Services': '金融',
    'Healthcare': 'ヘルスケア', 'Consumer Cyclical': '消費財（景気敏感）',
    'Consumer Defensive': '消費財（ディフェンシブ）', 'Industrials': '産業・製造',
    'Energy': 'エネルギー', 'Basic Materials': '素材', 'Real Estate': '不動産',
    'Communication Services': '通信・メディア', 'Utilities': '公益',
}

INDUSTRY_JA = {
    # Technology (yfinance uses " - " not "—")
    'Semiconductors': '半導体チップ設計・製造',
    'Semiconductor Equipment & Materials': '半導体製造装置・材料',
    'Consumer Electronics': 'スマホ・PC・家電製品',
    'Software - Application': 'ビジネス向けアプリソフト',
    'Software - Infrastructure': 'OS・クラウド基盤ソフト',
    'Information Technology Services': 'ITサービス・コンサル',
    'Computer Hardware': 'サーバー・PC等のハード',
    'Electronic Components': '電子部品・基板',
    'Scientific & Technical Instruments': '計測・精密機器',
    'Communication Equipment': '通信機器・ネットワーク',
    # Communication
    'Internet Content & Information': 'ネット検索・SNS・広告',
    'Entertainment': '映画・ゲーム・エンタメ',
    'Telecom Services': '通信キャリア・回線',
    'Broadcasting': '放送・メディア',
    # Financial
    'Banks - Diversified': '総合銀行',
    'Banks - Regional': '地方銀行',
    'Asset Management': '資産運用・投資信託',
    'Insurance - Life': '生命保険',
    'Insurance - Diversified': '総合保険',
    'Credit Services': 'クレジット・決済',
    'Capital Markets': '証券・投資銀行',
    'Financial Data & Stock Exchanges': '金融データ・取引所',
    # Healthcare
    'Drug Manufacturers - General': '医薬品（大手製薬）',
    'Drug Manufacturers - Specialty & Generic': '後発・特殊医薬品',
    'Biotechnology': 'バイオ・新薬研究',
    'Medical Devices': '医療機器',
    'Health Information Services': '医療情報サービス',
    'Managed Health Care': '医療保険・HMO',
    # Consumer
    'Auto Manufacturers': '自動車メーカー',
    'Auto Parts': '自動車部品',
    'Retail - Apparel': 'アパレル小売',
    'Specialty Retail': '専門店小売',
    'Internet Retail': 'ネット通販・EC',
    'Home Improvement Retail': 'ホームセンター',
    'Restaurants': '飲食チェーン',
    'Luxury Goods': '高級ブランド・奢侈品',
    'Beverages - Non-Alcoholic': '清涼飲料・飲料',
    'Beverages - Alcoholic': 'アルコール飲料',
    'Packaged Foods': '加工食品・スナック',
    'Tobacco': 'たばこ',
    'Household & Personal Products': '日用品・化粧品',
    # Industrials
    'Aerospace & Defense': '航空宇宙・防衛',
    'Industrial Conglomerates': '複合企業（コングロマリット）',
    'Specialty Industrial Machinery': '産業機械・設備',
    'Railroads': '鉄道',
    'Airlines': '航空会社',
    'Trucking': '陸上輸送・物流',
    'Staffing & Employment Services': '人材派遣・HR',
    # Energy
    'Oil & Gas Integrated': '石油・ガス（総合）',
    'Oil & Gas E&P': '石油・ガス探鉱・開発',
    'Oil & Gas Refining & Marketing': '石油精製・販売',
    'Solar': '太陽光発電',
    # Real Estate
    'REIT - Diversified': 'REIT（複合型）',
    'REIT - Retail': 'REIT（商業施設）',
    'REIT - Office': 'REIT（オフィス）',
}

def get_industry_ja(industry_en, sector_en, company_name=''):
    """英語industryを日本語の説明文に変換"""
    if not industry_en:
        return SECTOR_JA.get(sector_en, '')
    # 辞書にあればそのまま返す
    if industry_en in INDUSTRY_JA:
        return INDUSTRY_JA[industry_en]
    # 部分マッチ
    for k, v in INDUSTRY_JA.items():
        if k.lower() in industry_en.lower() or industry_en.lower() in k.lower():
            return v
    # フォールバック: セクター名
    return SECTOR_JA.get(sector_en, industry_en)

def get_logo_url(info, symbol):
    """ティッカーシンボルから企業ロゴURLを生成（Parqetロゴサービス使用）"""
    # ティッカーで直接取得（米国株・日本株両方対応）
    return f'https://assets.parqet.com/logos/symbol/{symbol}?format=png'

def quick_analyze(symbol):
    try:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period="3mo")
        if hist.empty or len(hist) < 20:
            return None
        close = hist['Close']
        volume = hist['Volume']
        rsi_s = ta.momentum.RSIIndicator(close, window=14).rsi()
        macd_obj = ta.trend.MACD(close)
        bb = ta.volatility.BollingerBands(close, window=20)
        ma25 = close.rolling(25).mean()
        ma75 = close.rolling(75).mean()

        def sv(s):
            try:
                v = s.iloc[-1]
                return None if np.isnan(v) else float(v)
            except:
                return None

        cur = sv(close)
        prev = float(close.iloc[-2]) if len(close) >= 2 else cur
        change = ((cur - prev) / prev * 100) if prev else 0

        rsi_sig, _ = get_rsi_signal(sv(rsi_s))
        macd_sig, _ = get_macd_signal(sv(macd_obj.macd()), sv(macd_obj.macd_signal()), sv(macd_obj.macd_diff()))
        bb_sig, _ = get_bb_signal(cur, sv(bb.bollinger_hband()), sv(bb.bollinger_lband()), sv(bb.bollinger_mavg()))
        ma_sig, _ = get_ma_signal(cur, sv(close.rolling(5).mean()), sv(ma25), sv(ma75))
        avg_vol = float(volume.tail(20).mean())
        vol_sig, _ = get_volume_signal(sv(volume), avg_vol, change)

        _, score = score_to_verdict([rsi_sig, macd_sig, bb_sig, ma_sig, vol_sig])
        verdict, _ = score_to_verdict([rsi_sig, macd_sig, bb_sig, ma_sig, vol_sig])

        # Fundamentals from info
        info = {}
        try:
            info = ticker.info or {}
        except:
            pass
        sector_en  = info.get('sector') or ''
        industry_en = info.get('industry') or ''
        sector = SECTOR_JA.get(sector_en, sector_en)
        industry_desc = get_industry_ja(industry_en, sector_en)
        logo_url = get_logo_url(info, symbol)
        company_name = info.get('longName') or info.get('shortName') or ''
        company_name = get_jp_company_name(symbol, company_name)
        pe = info.get('trailingPE') or info.get('forwardPE')
        roe_raw = info.get('returnOnEquity')
        roe = round(roe_raw * 100, 1) if roe_raw else None
        market_cap = info.get('marketCap')
        market_cap_raw = market_cap if market_cap else 0  # for sorting
        week52_high = info.get('fiftyTwoWeekHigh')
        week52_low  = info.get('fiftyTwoWeekLow')
        # 配当利回り（yfinanceは既にパーセント値で返してくる: 例 0.88 = 0.88%）
        div_yield_raw = info.get('dividendYield')
        div_yield = round(float(div_yield_raw), 2) if div_yield_raw is not None else None
        # 売上成長率（前年比）
        rev_growth_raw = info.get('revenueGrowth')
        rev_growth = round(rev_growth_raw * 100, 1) if rev_growth_raw is not None else None
        # PBR
        pb_raw = info.get('priceToBook')
        pb = round(pb_raw, 2) if pb_raw else None

        # Format market cap
        mc_str = None
        if market_cap:
            if market_cap >= 1e12:
                mc_str = f'{market_cap/1e12:.1f}兆'
            elif market_cap >= 1e8:
                mc_str = f'{market_cap/1e8:.0f}億'
            else:
                mc_str = f'{market_cap/1e6:.0f}M'

        # 52w position (0-100%)
        w52_pos = None
        if week52_high and week52_low and cur and week52_high > week52_low:
            w52_pos = round((cur - week52_low) / (week52_high - week52_low) * 100)

        # One-liner comment
        def make_comment(verdict, score, rsi_val, pe_val, roe_val, chg, w52):
            parts = []
            if verdict in ('STRONG_BUY', 'BUY'):
                parts.append('複数サイン一致の買い候補')
            elif verdict == 'WEAK_BUY':
                parts.append('やや強気、様子見も可')
            elif verdict in ('STRONG_SELL', 'SELL'):
                parts.append('売りサイン点灯中')
            elif verdict == 'WEAK_SELL':
                parts.append('やや弱気、注意')
            else:
                parts.append('方向感なし')
            if rsi_val:
                if rsi_val < 35:
                    parts.append('RSI売られすぎ圏')
                elif rsi_val > 70:
                    parts.append('RSI過熱気味')
            if pe_val and pe_val < 15:
                parts.append('PER割安')
            elif pe_val and pe_val > 40:
                parts.append('PER高め')
            if roe_val and roe_val > 20:
                parts.append('ROE高収益')
            if w52 and w52 >= 90:
                parts.append('52週高値圏')
            elif w52 and w52 <= 10:
                parts.append('52週安値圏')
            return ' · '.join(parts[:3])

        comment = make_comment(verdict, score, sv(rsi_s), pe, roe, change, w52_pos)

        return {
            'symbol': symbol,
            'price': round(cur, 2) if cur else None,
            'change': round(change, 2),
            'score': score,
            'verdict': verdict,
            'rsi': round(sv(rsi_s), 1) if sv(rsi_s) else None,
            'signals': {'rsi': rsi_sig, 'macd': macd_sig, 'bb': bb_sig, 'ma': ma_sig, 'vol': vol_sig},
            'sector': sector,
            'industry_desc': industry_desc,
            'logo_url': logo_url,
            'long_name': company_name,
            'pe': round(pe, 1) if pe else None,
            'roe': roe,
            'pb': pb,
            'div_yield': div_yield,
            'rev_growth': rev_growth,
            'market_cap': mc_str,
            'market_cap_raw': market_cap_raw,
            'w52_pos': w52_pos,
            'comment': comment,
        }
    except:
        return None

@app.route('/api/scan', methods=['GET'])
def scan():
    from concurrent.futures import ThreadPoolExecutor
    mode = request.args.get('mode', 'mixed')

    MODE_INFO = {
        'mixed':     {'label':'🌟 おすすめミックス',     'desc':'値上がり・値下がり・出来高・トレンドから動的にピックアップ（毎回違う銘柄）+ 日本株コア'},
        'gainers':   {'label':'🔥 値上がり率TOP',          'desc':'今日大きく上昇している銘柄（モメンタム順張り候補）'},
        'losers':    {'label':'❄️ 値下がり率TOP',          'desc':'今日大きく下落している銘柄（押し目買い・逆張り候補）'},
        'actives':   {'label':'📊 出来高ランキング',       'desc':'今日の取引が活発な銘柄（流動性高・市場注目度高）'},
        'breakout':  {'label':'🚀 52週高値ブレイク',       'desc':'52週高値を更新中の銘柄（強いトレンド継続候補）'},
        'watchlist': {'label':'⭐ コアウォッチリスト',     'desc':'メガテック + 主要日本株の固定リスト'},
    }

    symbols_list = get_scan_symbols(mode)
    results = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(quick_analyze, w['symbol']): w for w in symbols_list}
        for future, w in futures.items():
            r = future.result()
            if r:
                # 名前がティッカーと同じ場合は info から会社名を取得
                if w.get('name') == w['symbol']:
                    try:
                        info = yf.Ticker(w['symbol']).info or {}
                        r['name'] = info.get('longName') or info.get('shortName') or w['symbol']
                    except:
                        r['name'] = w['symbol']
                else:
                    r['name'] = w['name']
                # 日本株は日本語企業名に置き換え
                r['name'] = get_jp_company_name(w['symbol'], r['name'])
                results.append(r)
    results.sort(key=lambda x: x['score'], reverse=True)
    info = MODE_INFO.get(mode, MODE_INFO['mixed'])
    return jsonify({
        'stocks': results,
        'updated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'mode': mode,
        'mode_label': info['label'],
        'mode_desc': info['desc'],
        'total_count': len(results),
    })

KW_MAP = [
    (['earnings', 'eps', 'beat', 'revenue', 'profit', 'quarterly'], '📊 決算・業績への注目'),
    (['ai', 'artificial intelligence', 'chatgpt', 'llm', 'nvidia', 'chip'], '🤖 AI・半導体関連の話題'),
    (['record', 'high', 'ath', 'breakout', 'surge', 'soar', 'rally'], '🚀 最高値・ブレイクアウト'),
    (['fda', 'approval', 'drug', 'clinical', 'trial', 'biotech'], '💊 薬事承認・臨床試験'),
    (['merger', 'acquisition', 'deal', 'buyout', 'takeover'], '🤝 買収・合併の動き'),
    (['split', 'dividend', 'buyback', 'shareholder'], '💰 配当・自社株買い'),
    (['crash', 'drop', 'fall', 'plunge', 'sell off', 'warning', 'concern'], '⚠️ 急落・下落懸念'),
    (['fed', 'interest rate', 'inflation', 'tariff', 'trade war', 'macro'], '🏦 金利・マクロ経済'),
    (['upgrade', 'price target', 'analyst', 'overweight', 'buy rating'], '⬆️ アナリスト格上げ'),
    (['downgrade', 'underweight', 'sell rating', 'lower target'], '⬇️ アナリスト格下げ'),
    (['trump', 'elon', 'musk', 'government', 'regulation', 'lawsuit'], '🏛️ 政治・規制の動向'),
    (['guidance', 'forecast', 'outlook', 'future'], '🔭 業績見通しの変化'),
]

def analyze_news_sentiment(titles):
    """ニュースタイトルからセンチメントと理由を分析"""
    all_text = ' '.join(titles).lower()

    # ポジティブ/ネガティブワードでセンチメント判定
    pos_words = ['beat', 'surge', 'soar', 'rally', 'upgrade', 'record', 'high', 'strong',
                 'growth', 'profit', 'gain', 'rise', 'jump', 'boost', 'buy', 'approval']
    neg_words = ['miss', 'drop', 'fall', 'plunge', 'downgrade', 'warning', 'concern',
                 'loss', 'decline', 'sell', 'crash', 'lower', 'cut', 'weak', 'risk']

    pos = sum(1 for w in pos_words if w in all_text)
    neg = sum(1 for w in neg_words if w in all_text)
    total = pos + neg

    if total == 0:
        mood_pct = 50
    else:
        mood_pct = int(pos / total * 100)

    if mood_pct >= 70:   mood = "強気（買いムード）"
    elif mood_pct >= 55: mood = "やや強気"
    elif mood_pct <= 30: mood = "弱気（売りムード）"
    elif mood_pct <= 45: mood = "やや弱気"
    else:                mood = "中立"

    reasons = []
    for kws, label in KW_MAP:
        if any(k in all_text for k in kws):
            reasons.append(label)
        if len(reasons) >= 3:
            break
    if not reasons:
        reasons = ['📈 トレーダーの注目度が上昇中']

    return mood, mood_pct, reasons

def build_trend_summary(name, sym, mood, mood_pct, reasons, titles_ja, stock_data):
    """なぜトレンドなのか — 短いキャッチライン + 箇条書きポイントを返す"""
    change  = (stock_data.get('change') or 0) if stock_data else 0
    verdict = (stock_data.get('verdict') or 'NEUTRAL') if stock_data else 'NEUTRAL'

    # ── キャッチライン（1行） ──────────────────────────
    if change >= 10:
        catch = f"🚀 本日 +{change:.1f}% の急騰！市場の注目が集中しています"
    elif change >= 5:
        catch = f"📈 本日 +{change:.1f}% と大きく上昇中"
    elif change <= -10:
        catch = f"🔴 本日 {change:.1f}% の急落。売り圧力が強まっています"
    elif change <= -5:
        catch = f"📉 本日 {change:.1f}% と大幅下落中"
    elif mood_pct >= 70:
        catch = f"💚 ニュースが強気一色。買いムードが広がっています"
    elif mood_pct <= 30:
        catch = f"🔴 ニュースが弱気優勢。慎重な見方が強まっています"
    else:
        catch = f"📊 トレーダーの間で話題になっています"

    # ── 箇条書きポイント（最大3つ） ─────────────────────
    points = []

    # ① きっかけ（ニュースのキーワードカテゴリ）
    if reasons:
        r = reasons[0].split(' ', 1)[-1]  # 絵文字を除いたテキスト
        points.append(f"話題のきっかけ：{r}")

    # ② 直近ニュースの一言
    if titles_ja:
        h = titles_ja[0]
        if len(h) > 40: h = h[:38] + '…'
        points.append(f"最新ニュース：「{h}」")

    # ③ テクニカル × センチメントの組み合わせ判定
    if verdict in ('STRONG_BUY', 'BUY') and mood_pct >= 55:
        points.append("チャートも買いサイン → ニュースと一致で上昇期待")
    elif verdict in ('STRONG_SELL', 'SELL') and mood_pct <= 45:
        points.append("チャートも売りサイン → ニュースと一致で下落リスクあり")
    elif verdict in ('STRONG_BUY', 'BUY'):
        points.append("チャートは買いサイン → ニュースと合わせて判断を")
    elif verdict in ('STRONG_SELL', 'SELL'):
        points.append("チャートは売りサイン → 慎重に様子を見たい局面")
    elif mood_pct >= 60:
        points.append("ポジティブなニュースが続いており、上昇期待の声が多い")
    elif mood_pct <= 40:
        points.append("懸念材料のニュースが多く、売りたい投資家も増えている")

    return {'catch': catch, 'points': points}

def get_yahoo_news(symbol):
    """yfinanceでニュースを取得してタイトルリストを返す"""
    try:
        ticker = yf.Ticker(symbol)
        news = ticker.news or []
        titles = []
        for item in news[:8]:
            t = (item.get('content') or {}).get('title') or item.get('title') or ''
            if t:
                titles.append(t)
        return titles
    except:
        return []

def get_trending_symbols():
    """Yahoo Finance トレンド銘柄を取得（スクレイピング）"""
    return scrape_yahoo_symbols('https://finance.yahoo.com/trending-tickers/', limit=15)

def scrape_yahoo_symbols(url, limit=20):
    """Yahoo Finance のスクリーナーページから銘柄ティッカーを抽出"""
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0'}
        r = req.get(url, headers=headers, timeout=10)
        import re
        # ティッカーは "symbol":"XXX" 形式で出現
        symbols = re.findall(r'"symbol":"([A-Z][A-Z0-9.\-]{0,9})"', r.text)
        seen = []
        for s in symbols:
            # 通貨ペアや先物は除外、株式ティッカーのみ
            if '=' in s or '^' in s: continue
            if s not in seen:
                seen.append(s)
            if len(seen) >= limit: break
        return seen
    except:
        return []

def get_day_gainers():
    """値上がり率TOP（米国）"""
    return scrape_yahoo_symbols('https://finance.yahoo.com/markets/stocks/gainers/', limit=30)

def get_day_losers():
    """値下がり率TOP（米国）— 押し目買い候補"""
    return scrape_yahoo_symbols('https://finance.yahoo.com/markets/stocks/losers/', limit=30)

def get_most_actives():
    """出来高ランキング（米国）"""
    return scrape_yahoo_symbols('https://finance.yahoo.com/markets/stocks/most-active/', limit=30)

def get_52w_gainers():
    """52週高値ブレイク銘柄"""
    return scrape_yahoo_symbols('https://finance.yahoo.com/markets/stocks/52-week-gainers/', limit=30)

# ウォッチリストとは別に、日本株コアリスト（補完用）
JP_CORE_SYMBOLS = ['7203.T','9984.T','6758.T','7974.T','9432.T','6861.T','8306.T','4063.T','6367.T','9983.T','4519.T','8035.T','6098.T','8001.T','9433.T']

# 日本株 銘柄コード → 日本語企業名（主要銘柄200社）
JP_COMPANY_JA = {
    # 自動車・輸送機器
    '7203.T': 'トヨタ自動車', '7267.T': 'ホンダ', '7201.T': '日産自動車',
    '7269.T': 'スズキ', '7270.T': 'SUBARU', '7261.T': 'マツダ',
    '7259.T': 'アイシン', '6902.T': 'デンソー', '7012.T': '川崎重工業',
    '7011.T': '三菱重工業', '7013.T': 'IHI',
    # IT・テクノロジー
    '6758.T': 'ソニーグループ', '6861.T': 'キーエンス', '6981.T': '村田製作所',
    '7974.T': '任天堂', '6098.T': 'リクルートホールディングス',
    '4307.T': '野村総合研究所', '4716.T': '日本オラクル',
    '6701.T': 'NEC', '6702.T': '富士通', '6501.T': '日立製作所',
    '6502.T': '東芝', '6752.T': 'パナソニック',
    '8035.T': '東京エレクトロン', '6920.T': 'レーザーテック',
    '4063.T': '信越化学工業', '4543.T': 'テルモ',
    # 通信
    '9432.T': 'NTT', '9433.T': 'KDDI', '9434.T': 'ソフトバンク',
    '9984.T': 'ソフトバンクグループ',
    # 金融
    '8306.T': '三菱UFJフィナンシャル・グループ', '8316.T': '三井住友フィナンシャルグループ',
    '8411.T': 'みずほフィナンシャルグループ', '8604.T': '野村ホールディングス',
    '8591.T': 'オリックス', '8766.T': '東京海上ホールディングス',
    '8725.T': 'MS&ADインシュアランスグループ',
    # 商社・流通
    '8001.T': '伊藤忠商事', '8031.T': '三井物産', '8053.T': '住友商事',
    '8002.T': '丸紅', '8058.T': '三菱商事',
    # 小売
    '9983.T': 'ファーストリテイリング', '3382.T': 'セブン&アイ・ホールディングス',
    '8267.T': 'イオン', '3086.T': 'J.フロント リテイリング',
    '9843.T': 'ニトリホールディングス',
    # 食品・飲料
    '2502.T': 'アサヒグループホールディングス', '2503.T': 'キリンホールディングス',
    '2914.T': 'JT', '2802.T': '味の素', '2269.T': '明治ホールディングス',
    # 製薬・ヘルスケア
    '4519.T': '中外製薬', '4502.T': '武田薬品工業', '4503.T': 'アステラス製薬',
    '4523.T': 'エーザイ', '4568.T': '第一三共', '4151.T': '協和キリン',
    '4578.T': '大塚ホールディングス',
    # 機械・電機
    '6367.T': 'ダイキン工業', '6273.T': 'SMC', '6594.T': 'ニデック',
    '6326.T': 'クボタ', '6301.T': 'コマツ', '6503.T': '三菱電機',
    '6954.T': 'ファナック', '6506.T': '安川電機',
    # エネルギー・素材
    '5020.T': 'ENEOSホールディングス', '5108.T': 'ブリヂストン',
    '5401.T': '日本製鉄', '5713.T': '住友金属鉱山',
    # 不動産・建設
    '8801.T': '三井不動産', '8802.T': '三菱地所', '8830.T': '住友不動産',
    '1925.T': '大和ハウス工業', '1928.T': '積水ハウス',
    # ゲーム・エンタメ
    '9766.T': 'コナミグループ', '9684.T': 'スクウェア・エニックス',
    '6460.T': 'セガサミーホールディングス',
    # 運輸・物流
    '9020.T': 'JR東日本', '9021.T': 'JR西日本', '9022.T': 'JR東海',
    '9202.T': 'ANAホールディングス', '9201.T': '日本航空',
    '9064.T': 'ヤマトホールディングス',
    # 電力・ガス
    '9501.T': '東京電力ホールディングス', '9503.T': '関西電力',
    '9531.T': '東京ガス',
    # その他大手
    '4661.T': 'オリエンタルランド', '4452.T': '花王', '4911.T': '資生堂',
    '7733.T': 'オリンパス', '4523.T': 'エーザイ',
}

def get_jp_company_name(symbol, fallback):
    """日本株なら日本語企業名を返す。なければfallback（英語名）"""
    if symbol.endswith('.T') and symbol in JP_COMPANY_JA:
        return JP_COMPANY_JA[symbol]
    return fallback

def get_scan_symbols(mode='mixed', limit=30):
    """
    スキャン用の銘柄リストを動的に生成。
    mode: 'mixed' / 'gainers' / 'losers' / 'actives' / 'breakout' / 'watchlist'
    """
    if mode == 'gainers':
        us = get_day_gainers()[:25]
        return [{'symbol': s, 'name': s} for s in us] + [{'symbol': s, 'name': s} for s in JP_CORE_SYMBOLS[:5]]
    elif mode == 'losers':
        us = get_day_losers()[:25]
        return [{'symbol': s, 'name': s} for s in us] + [{'symbol': s, 'name': s} for s in JP_CORE_SYMBOLS[:5]]
    elif mode == 'actives':
        us = get_most_actives()[:25]
        return [{'symbol': s, 'name': s} for s in us] + [{'symbol': s, 'name': s} for s in JP_CORE_SYMBOLS[:5]]
    elif mode == 'breakout':
        us = get_52w_gainers()[:25]
        return [{'symbol': s, 'name': s} for s in us] + [{'symbol': s, 'name': s} for s in JP_CORE_SYMBOLS[:5]]
    elif mode == 'watchlist':
        return WATCHLIST
    else:  # mixed: 値上がり/値下がり/出来高/トレンド + 日本株コアをミックス
        symbols = []
        seen = set()
        # 各ソースから取れるだけ取る（重複排除）
        for fn in [get_day_gainers, get_day_losers, get_most_actives, get_trending_symbols, get_52w_gainers]:
            for s in fn()[:12]:
                if s not in seen:
                    seen.add(s); symbols.append(s)
                if len(symbols) >= 25: break
            if len(symbols) >= 25: break
        # 日本株コアを追加
        for s in JP_CORE_SYMBOLS:
            if s not in seen:
                seen.add(s); symbols.append(s)
            if len(symbols) >= limit: break
        # それでも足りなければ固定ウォッチリストで穴埋め
        if len(symbols) < limit:
            for w in WATCHLIST:
                if w['symbol'] not in seen:
                    seen.add(w['symbol']); symbols.append(w['symbol'])
                if len(symbols) >= limit: break
        result = [{'symbol': s, 'name': s} for s in symbols[:limit]]
        return result if result else WATCHLIST

@app.route('/api/trending', methods=['GET'])
def trending():
    from concurrent.futures import ThreadPoolExecutor

    # Yahoo Finance トレンドから銘柄取得 → フォールバックはウォッチリスト上位
    symbols = get_trending_symbols()
    if not symbols:
        symbols = ['NVDA','TSLA','AAPL','AMD','META','MSFT','AMZN','GOOGL','NFLX','JPM',
                   'UBER','DIS','PFE','BAC','XOM']

    results = []

    def process(sym):
        titles = get_yahoo_news(sym)
        mood, mood_pct, reasons = analyze_news_sentiment(titles)
        stock_data = quick_analyze(sym)

        # 会社名をyfinanceから取得
        name = sym
        try:
            info = yf.Ticker(sym).info
            name = info.get('shortName') or info.get('longName') or sym
        except:
            pass

        # ニュースタイトルを日本語に翻訳
        translated = []
        for t in titles[:3]:
            translated.append(translate_ja(t))

        # なぜトレンドなのかのサマリー生成
        summary = build_trend_summary(name, sym, mood, mood_pct, reasons, translated, stock_data)

        return {
            'symbol': sym,
            'name': name,
            'news_count': len(titles),
            'news_titles': titles[:3],
            'news_titles_ja': translated,
            'summary': summary,
            'sentiment': {
                'mood': mood,
                'mood_pct': mood_pct,
                'bullish': mood_pct,
                'bearish': 100 - mood_pct,
                'reasons': reasons,
                'sample_messages': titles[:3],
            },
            'price':   stock_data['price']   if stock_data else None,
            'change':  stock_data['change']  if stock_data else None,
            'score':   stock_data['score']   if stock_data else None,
            'verdict': stock_data['verdict'] if stock_data else 'NEUTRAL',
            'signals': stock_data['signals'] if stock_data else {},
        }

    with ThreadPoolExecutor(max_workers=6) as ex:
        futures = [ex.submit(process, sym) for sym in symbols]
        for f in futures:
            try:
                r = f.result(timeout=20)
                if r: results.append(r)
            except:
                pass

    # ニュース件数 × テクニカルスコアで並び替え
    results.sort(key=lambda x: (x['news_count'] * 5) + (x['score'] or 0), reverse=True)

    return jsonify({'stocks': results, 'updated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S')})

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/analyze', methods=['POST'])
def analyze():
    data = request.get_json()
    symbol = data.get('symbol', '').strip()
    if not symbol:
        return jsonify({'error': 'ティッカーシンボルを入力してください'}), 400
    # 日本株: 4〜5桁の数字のみなら .T を自動付与
    import re
    if re.fullmatch(r'\d{4,5}', symbol):
        symbol = symbol + '.T'
    else:
        symbol = symbol.upper()

    try:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period="6mo")
        if hist.empty:
            return jsonify({'error': f'{symbol} のデータが取得できません。ティッカーを確認してください。'}), 404

        info = {}
        try:
            info = ticker.info or {}
        except:
            pass

        # Technical indicators
        close = hist['Close']
        volume = hist['Volume']

        rsi_series = ta.momentum.RSIIndicator(close, window=14).rsi()
        macd_obj = ta.trend.MACD(close)
        macd_line = macd_obj.macd()
        macd_signal = macd_obj.macd_signal()
        macd_hist = macd_obj.macd_diff()

        bb = ta.volatility.BollingerBands(close, window=20, window_dev=2)
        bb_upper = bb.bollinger_hband()
        bb_lower = bb.bollinger_lband()
        bb_mid = bb.bollinger_mavg()

        ma5 = close.rolling(5).mean()
        ma25 = close.rolling(25).mean()
        ma75 = close.rolling(75).mean()

        def safe(series):
            try:
                v = series.iloc[-1]
                return None if np.isnan(v) else float(v)
            except:
                return None

        cur_close = safe(close)
        cur_rsi = safe(rsi_series)
        cur_macd = safe(macd_line)
        cur_signal = safe(macd_signal)
        cur_hist = safe(macd_hist)
        cur_bb_upper = safe(bb_upper)
        cur_bb_lower = safe(bb_lower)
        cur_bb_mid = safe(bb_mid)
        cur_ma5 = safe(ma5)
        cur_ma25 = safe(ma25)
        cur_ma75 = safe(ma75)
        cur_vol = safe(volume)
        avg_vol = float(volume.tail(20).mean()) if len(volume) >= 20 else None
        prev_close = float(close.iloc[-2]) if len(close) >= 2 else cur_close
        price_change = ((cur_close - prev_close) / prev_close * 100) if prev_close else 0

        rsi_sig, rsi_desc = get_rsi_signal(cur_rsi)
        macd_sig, macd_desc = get_macd_signal(cur_macd, cur_signal, cur_hist)
        bb_sig, bb_desc = get_bb_signal(cur_close, cur_bb_upper, cur_bb_lower, cur_bb_mid)
        ma_sig, ma_desc = get_ma_signal(cur_close, cur_ma5, cur_ma25, cur_ma75)
        vol_sig, vol_desc = get_volume_signal(cur_vol, avg_vol, price_change)

        all_signals = [rsi_sig, macd_sig, bb_sig, ma_sig, vol_sig]
        verdict, score = score_to_verdict(all_signals)

        # Fundamentals
        def safe_info(key, fmt=None):
            v = info.get(key)
            if v is None or v == 'N/A':
                return None
            try:
                return float(v)
            except:
                return None

        roe = safe_info('returnOnEquity')
        roa = safe_info('returnOnAssets')
        pe = safe_info('trailingPE')
        pb = safe_info('priceToBook')
        eps = safe_info('trailingEps')
        div_yield = safe_info('dividendYield')
        revenue_growth = safe_info('revenueGrowth')
        profit_margin = safe_info('profitMargins')
        debt_ratio = safe_info('debtToEquity')
        market_cap = safe_info('marketCap')
        week52_high = safe_info('fiftyTwoWeekHigh')
        week52_low = safe_info('fiftyTwoWeekLow')
        company_name = info.get('longName') or info.get('shortName') or symbol
        company_name = get_jp_company_name(symbol, company_name)

        # Chart data (last 90 days)
        chart_data = []
        tail = hist.tail(90)
        bb_u_tail = bb_upper.tail(90)
        bb_l_tail = bb_lower.tail(90)
        bb_m_tail = bb_mid.tail(90)
        ma25_tail = ma25.tail(90)
        ma75_tail = ma75.tail(90)

        for i in range(len(tail)):
            date_str = tail.index[i].strftime('%Y-%m-%d')
            def sv(s, idx):
                try:
                    v = s.iloc[idx]
                    return None if np.isnan(v) else round(float(v), 2)
                except:
                    return None
            chart_data.append({
                'date': date_str,
                'open': round(float(tail['Open'].iloc[i]), 2),
                'high': round(float(tail['High'].iloc[i]), 2),
                'low': round(float(tail['Low'].iloc[i]), 2),
                'close': round(float(tail['Close'].iloc[i]), 2),
                'volume': int(tail['Volume'].iloc[i]),
                'bb_upper': sv(bb_u_tail, i),
                'bb_lower': sv(bb_l_tail, i),
                'bb_mid': sv(bb_m_tail, i),
                'ma25': sv(ma25_tail, i),
                'ma75': sv(ma75_tail, i),
            })

        # RSI chart
        rsi_tail = rsi_series.tail(90)
        rsi_chart = []
        for i in range(len(rsi_tail)):
            v = rsi_tail.iloc[i]
            rsi_chart.append({
                'date': rsi_tail.index[i].strftime('%Y-%m-%d'),
                'value': None if np.isnan(v) else round(float(v), 2)
            })

        # MACD chart
        macd_tail = macd_line.tail(90)
        msig_tail = macd_signal.tail(90)
        mhist_tail = macd_hist.tail(90)
        macd_chart = []
        for i in range(len(macd_tail)):
            mv = macd_tail.iloc[i]
            sv2 = msig_tail.iloc[i]
            hv = mhist_tail.iloc[i]
            macd_chart.append({
                'date': macd_tail.index[i].strftime('%Y-%m-%d'),
                'macd': None if np.isnan(mv) else round(float(mv), 4),
                'signal': None if np.isnan(sv2) else round(float(sv2), 4),
                'hist': None if np.isnan(hv) else round(float(hv), 4),
            })

        result = {
            'symbol': symbol,
            'company_name': company_name,
            'logo_url': get_logo_url(info, symbol),
            'price': cur_close,
            'price_change': round(price_change, 2),
            'verdict': verdict,
            'score': score,
            'indicators': {
                'rsi': {'value': round(cur_rsi, 1) if cur_rsi else None, 'signal': rsi_sig, 'desc': rsi_desc},
                'macd': {'value': round(cur_macd, 4) if cur_macd else None, 'signal': macd_sig, 'desc': macd_desc},
                'bollinger': {'signal': bb_sig, 'desc': bb_desc,
                              'upper': round(cur_bb_upper, 2) if cur_bb_upper else None,
                              'lower': round(cur_bb_lower, 2) if cur_bb_lower else None},
                'ma': {'ma5': round(cur_ma5, 2) if cur_ma5 else None,
                       'ma25': round(cur_ma25, 2) if cur_ma25 else None,
                       'ma75': round(cur_ma75, 2) if cur_ma75 else None,
                       'signal': ma_sig, 'desc': ma_desc},
                'volume': {'value': int(cur_vol) if cur_vol else None,
                           'avg': int(avg_vol) if avg_vol else None,
                           'signal': vol_sig, 'desc': vol_desc},
            },
            'fundamentals': {
                'roe': round(roe * 100, 1) if roe else None,
                'roa': round(roa * 100, 1) if roa else None,
                'pe': round(pe, 1) if pe else None,
                'pb': round(pb, 2) if pb else None,
                'eps': round(eps, 2) if eps else None,
                'div_yield': round(div_yield * 100, 2) if div_yield else None,
                'revenue_growth': round(revenue_growth * 100, 1) if revenue_growth else None,
                'profit_margin': round(profit_margin * 100, 1) if profit_margin else None,
                'debt_ratio': round(debt_ratio, 1) if debt_ratio else None,
                'market_cap': market_cap,
                'week52_high': week52_high,
                'week52_low': week52_low,
            },
            'chart': chart_data,
            'rsi_chart': rsi_chart,
            'macd_chart': macd_chart,
            'updated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        }
        return jsonify(result)

    except Exception as e:
        return jsonify({'error': f'分析中にエラーが発生しました: {str(e)}'}), 500

def generate_deep_analysis(symbol, info, indicators, fundamentals, verdict, score, news_titles):
    """テクニカル・ファンダ・経済環境を踏まえた詳細投資考察を生成"""
    sector   = info.get('sector') or ''
    industry = info.get('industry') or ''
    beta     = info.get('beta')
    rec      = info.get('recommendationKey') or ''  # buy/hold/sell
    target_price   = info.get('targetMeanPrice')
    current_price  = info.get('currentPrice') or info.get('regularMarketPrice')

    ind = indicators
    f   = fundamentals

    sections = []

    # ── 1. 企業・セクター概要 ─────────────────────────────────
    sector_ja = {
        'Technology':'テクノロジー', 'Financial Services':'金融',
        'Healthcare':'ヘルスケア', 'Consumer Cyclical':'一般消費財',
        'Industrials':'産業', 'Energy':'エネルギー',
        'Basic Materials':'素材', 'Communication Services':'通信・メディア',
        'Consumer Defensive':'生活必需品', 'Real Estate':'不動産',
        'Utilities':'公益事業',
    }.get(sector, sector) or '不明'
    industry_ja = translate_ja(industry) if industry else ''

    overview = f"**{symbol}** は{sector_ja}セクター"
    if industry_ja and industry_ja != industry:
        overview += f"（{industry_ja}）"
    overview += "に属する銘柄です。"
    if beta is not None:
        if beta > 1.5:
            overview += f" ベータ値は {beta:.2f} と高く、市場全体が動くときに株価も大きく振れやすい「ハイリスク・ハイリターン型」の銘柄です。"
        elif beta > 1.0:
            overview += f" ベータ値は {beta:.2f} で、市場平均よりやや値動きが大きい銘柄です。"
        elif beta > 0.5:
            overview += f" ベータ値は {beta:.2f} と低く、相場全体に比べて穏やかな値動きをする傾向があります。"
        else:
            overview += f" ベータ値は {beta:.2f} と非常に低く、市場の影響を受けにくい安定株です。"
    sections.append({'title': '📌 銘柄概要', 'content': overview})

    # ── 2. 現在の経済環境との関係 ────────────────────────────
    macro_lines = []
    if sector in ('Technology', 'Communication Services'):
        macro_lines.append("現在、AIブームとデータセンター投資の拡大を背景に、テクノロジーセクター全体に追い風が吹いています。一方で、米国の高金利環境が長引く場合、成長株のバリュエーションには下押し圧力がかかりやすいため注意が必要です。")
    elif sector == 'Financial Services':
        macro_lines.append("金融セクターは金利環境の影響を大きく受けます。金利が高止まりする局面では利ざやが改善しやすく銀行・保険にとってプラスですが、景気後退懸念が高まると不良債権リスクが意識されやすくなります。")
    elif sector == 'Healthcare':
        macro_lines.append("ヘルスケアセクターは景気の影響を受けにくいディフェンシブな性格を持ちます。AI創薬・GLP-1関連など新薬テーマが市場の注目を集めており、FDA承認動向が株価の重要なカタリストとなります。")
    elif sector == 'Energy':
        macro_lines.append("エネルギーセクターは原油・天然ガス価格と地政学リスクに左右されます。脱炭素化の流れは長期的な逆風ですが、中東情勢や中国の需要回復が短期の価格押し上げ要因になり得ます。")
    elif sector == 'Consumer Cyclical':
        macro_lines.append("一般消費財セクターは消費者マインドと可処分所得に敏感です。インフレ鈍化と雇用安定が続く局面ではポジティブですが、高金利による住宅・自動車ローンへの影響がマイナス要因となる場合があります。")
    elif sector == 'Industrials':
        macro_lines.append("産業セクターはインフラ投資やオンシェアリングの恩恵を受けやすい局面にあります。一方で、製造業PMIの動向や中国経済の減速リスクには注意が必要です。")
    else:
        macro_lines.append("現在の市場は、米国の金利動向・地政学リスク・AI関連需要の3つが主要テーマとなっています。セクターの特性を踏まえながら、マクロ環境の変化に敏感に対応することが重要です。")

    # アナリスト推奨
    if rec in ('buy', 'strongBuy'):
        macro_lines.append(f"アナリストの総合推奨は「**買い**」で、機関投資家からの評価も高い状態です。")
        if target_price and current_price:
            upside = (target_price - current_price) / current_price * 100
            macro_lines.append(f"目標株価の平均は **{target_price:.1f}** で、現在値から約 **{upside:+.1f}%** の上昇余地があるとみられています。")
    elif rec in ('hold', 'neutral'):
        macro_lines.append("アナリストの総合推奨は「**中立（ホールド）**」です。大きな変化がない限り現状維持が基本スタンスです。")
    elif rec in ('sell', 'underperform'):
        macro_lines.append("アナリストの総合推奨は「**売り・弱気**」です。機関投資家の見方が慎重なことに注意が必要です。")

    sections.append({'title': '🌍 経済環境・アナリスト評価', 'content': ' '.join(macro_lines)})

    # ── 3. テクニカル総評 ────────────────────────────────────
    tech_lines = []
    rsi_v = ind['rsi']['value']
    if rsi_v:
        if rsi_v < 30:
            tech_lines.append(f"RSIは **{rsi_v}** と売られすぎ水準にあり、テクニカル的なリバウンドが期待しやすいタイミングです。")
        elif rsi_v > 70:
            tech_lines.append(f"RSIは **{rsi_v}** と買われすぎ水準にあります。短期的な過熱感があり、利食い売りが出やすい局面です。")
        else:
            tech_lines.append(f"RSIは **{rsi_v}** と中立圏にあります。")

    ma = ind['ma']
    if ma['ma25'] and ma['ma75']:
        if ma['ma25'] > ma['ma75']:
            tech_lines.append("移動平均線は短期線が長期線の上に位置しており（ゴールデンクロス圏）、中長期的な上昇トレンドが継続しています。")
        else:
            tech_lines.append("移動平均線は短期線が長期線の下に位置しており（デッドクロス圏）、下降トレンドが続いています。反転シグナルが出るまでは慎重に。")

    if ind['macd']['signal'] == 'buy':
        tech_lines.append("MACDもシグナル線を上回っており、上昇モメンタムを確認できます。")
    elif ind['macd']['signal'] == 'sell':
        tech_lines.append("MACDはシグナル線を下回っており、下降モメンタムが続いています。")

    sections.append({'title': '📊 テクニカル分析の総評', 'content': ' '.join(tech_lines) if tech_lines else 'データ不足'})

    # ── 4. ファンダメンタルズ評価 ────────────────────────────
    fund_lines = []
    if f.get('roe'):
        if f['roe'] > 20:
            fund_lines.append(f"ROEは **{f['roe']}%** と極めて高く、経営効率の優れた企業と評価できます。")
        elif f['roe'] > 10:
            fund_lines.append(f"ROEは **{f['roe']}%** と標準的な水準です。")
        else:
            fund_lines.append(f"ROEは **{f['roe']}%** と低めで、資本効率の改善が課題です。")
    if f.get('pe'):
        if f['pe'] < 15:
            fund_lines.append(f"PERは **{f['pe']}倍** と割安水準にあり、バリュー投資の観点から魅力的です。")
        elif f['pe'] < 30:
            fund_lines.append(f"PERは **{f['pe']}倍** と標準的なバリュエーションです。")
        else:
            fund_lines.append(f"PERは **{f['pe']}倍** と高めで、将来の成長期待が既に株価に織り込まれています。期待に届かない場合は急落リスクがあります。")
    if f.get('revenue_growth'):
        if f['revenue_growth'] > 15:
            fund_lines.append(f"売上成長率 **{f['revenue_growth']}%** と力強い成長が続いており、今後も事業拡大が期待されます。")
        elif f['revenue_growth'] > 0:
            fund_lines.append(f"売上成長率は **{f['revenue_growth']}%** と緩やかな成長です。")
        else:
            fund_lines.append(f"売上成長率は **{f['revenue_growth']}%** とマイナスで、トップライン減収が懸念されます。")
    if f.get('debt_ratio'):
        if f['debt_ratio'] > 200:
            fund_lines.append(f"D/Eレシオ（負債比率）は **{f['debt_ratio']}%** と高水準で、金利上昇局面では財務負担が増大するリスクがあります。")
        elif f['debt_ratio'] < 50:
            fund_lines.append(f"D/Eレシオは **{f['debt_ratio']}%** と低く、財務体質は健全です。")

    sections.append({'title': '💼 ファンダメンタルズ評価', 'content': ' '.join(fund_lines) if fund_lines else 'データ不足'})

    # ── 5. リスク要因 ────────────────────────────────────────
    risks = []
    if beta and beta > 1.5:
        risks.append("ベータが高く市場急落時には大きく下落する可能性がある")
    if f.get('pe') and f['pe'] > 35:
        risks.append("PERが高く、業績が期待を下回ると株価が急落しやすい")
    if f.get('debt_ratio') and f['debt_ratio'] > 150:
        risks.append("負債が多く、金利上昇が業績を圧迫するリスクがある")
    if ind['rsi']['value'] and ind['rsi']['value'] > 65:
        risks.append("RSIが高く、短期的な調整が起きやすい水準")
    if ind['ma']['signal'] == 'sell':
        risks.append("移動平均線がデッドクロス状態で、下降トレンドが継続中")
    if sector == 'Technology' and f.get('pe') and f['pe'] > 40:
        risks.append("ハイPERテック株は金利上昇や景気後退懸念で大幅安になりやすい")
    if not risks:
        risks.append("現時点で特に大きなリスク要因は見当たりません")

    sections.append({'title': '⚠️ 注意すべきリスク', 'content': '\n'.join(f'・{r}' for r in risks)})

    # ── 6. 投資シナリオ ──────────────────────────────────────
    if verdict in ('STRONG_BUY', 'BUY'):
        bull = "テクニカル・ファンダメンタルズが揃っており、現在は積極的に買いを検討できるタイミングです。"
        bear = "ただし、マクロ環境が悪化した場合や決算が期待を下回った場合は、素早い損切りが重要です。"
    elif verdict in ('WEAK_BUY',):
        bull = "買いシグナルは出ているものの、まだ確信が持てない段階です。少額で打診買いし、上昇が確認できれば追加するのが良いでしょう。"
        bear = "下落した場合の損切りラインを事前に決めておくことが重要です。"
    elif verdict == 'NEUTRAL':
        bull = "現在は方向感がなく、どちらとも言い難い状態です。次の決算や経済指標の発表を待ってから判断するのが無難です。"
        bear = "ポジションを持っている場合は、利益確定の水準や損切りラインを見直しましょう。"
    else:
        bull = "現在は売りシグナルが優勢です。新規買いは控え、既存保有分は損切りラインを設定して保有を続けるか、早めに売却することを検討してください。"
        bear = "さらに下落が続いた場合に備えて、ポジションサイズを小さくしておくことが賢明です。"

    w52_h = f.get('week52_high')
    w52_l = f.get('week52_low')
    if w52_h and w52_l and current_price:
        pos = (current_price - w52_l) / (w52_h - w52_l) * 100
        if pos < 25:
            scenario_extra = f" 現在の株価は52週安値圏（下から{pos:.0f}%の位置）にあり、底値拾いの観点からは魅力的な水準です。"
        elif pos > 75:
            scenario_extra = f" 現在の株価は52週高値圏（下から{pos:.0f}%の位置）にあり、新規買いは高値掴みになるリスクがあります。"
        else:
            scenario_extra = f" 現在の株価は52週レンジの中間帯（下から{pos:.0f}%の位置）にあります。"
        bull += scenario_extra

    sections.append({'title': '🎯 投資シナリオと戦略', 'content': bull + '\n\n' + bear})

    # ── 7. ニュース起点の注目ポイント ────────────────────────
    if news_titles:
        top_news = news_titles[0]
        sections.append({'title': '📰 最新ニュースのポイント',
                         'content': f"直近の注目ニュース：「{top_news}」\n\nこのニュースは株価の短期的なカタリスト（きっかけ）になる可能性があります。内容が業績にポジティブなら買いの後押しに、ネガティブなら売り圧力になり得ます。"})

    return sections


@app.route('/api/chart', methods=['POST'])
def chart_data_endpoint():
    """期間指定でチャートデータのみ再取得するエンドポイント"""
    import re
    data = request.get_json()
    symbol = data.get('symbol', '').strip()
    period = data.get('period', '6mo')  # 1mo/3mo/6mo/1y/2y/5y

    if not symbol:
        return jsonify({'error': 'symbol required'}), 400
    if re.fullmatch(r'\d{4,5}', symbol):
        symbol = symbol + '.T'
    else:
        symbol = symbol.upper()

    # period → (fetch_period, interval, display_bars)
    PERIOD_MAP = {
        '1mo':  ('3mo',  '1d',  22),
        '3mo':  ('6mo',  '1d',  65),
        '6mo':  ('1y',   '1d',  130),
        '1y':   ('2y',   '1d',  252),
        '2y':   ('3y',   '1wk', 104),
        '5y':   ('5y',   '1wk', 260),
    }
    fetch_period, interval, n_bars = PERIOD_MAP.get(period, ('1y', '1d', 252))

    try:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period=fetch_period, interval=interval)
        if hist.empty:
            return jsonify({'error': 'データなし'}), 404

        close  = hist['Close']
        volume = hist['Volume']

        bb_obj    = ta.volatility.BollingerBands(close, window=20)
        bb_upper  = bb_obj.bollinger_hband()
        bb_lower  = bb_obj.bollinger_lband()
        bb_mid    = bb_obj.bollinger_mavg()
        ma25      = close.rolling(25).mean()
        ma75      = close.rolling(75).mean()
        rsi_s     = ta.momentum.RSIIndicator(close, window=14).rsi()
        macd_obj  = ta.trend.MACD(close)
        macd_line = macd_obj.macd()
        macd_sig  = macd_obj.macd_signal()
        macd_hist = macd_obj.macd_diff()

        tail = hist.tail(n_bars)

        def sv(s, idx):
            try:
                v = s.iloc[idx]
                return None if np.isnan(v) else round(float(v), 4)
            except:
                return None

        chart_data = []
        for i in range(len(tail)):
            offset = len(hist) - len(tail) + i
            chart_data.append({
                'date':     tail.index[i].strftime('%Y-%m-%d'),
                'open':     round(float(tail['Open'].iloc[i]), 2),
                'high':     round(float(tail['High'].iloc[i]), 2),
                'low':      round(float(tail['Low'].iloc[i]), 2),
                'close':    round(float(tail['Close'].iloc[i]), 2),
                'volume':   int(tail['Volume'].iloc[i]),
                'bb_upper': sv(bb_upper, offset),
                'bb_lower': sv(bb_lower, offset),
                'bb_mid':   sv(bb_mid,   offset),
                'ma25':     sv(ma25,     offset),
                'ma75':     sv(ma75,     offset),
            })

        rsi_tail = rsi_s.tail(n_bars)
        rsi_chart = [{'date': rsi_tail.index[i].strftime('%Y-%m-%d'),
                      'value': sv(rsi_tail, i)} for i in range(len(rsi_tail))]

        macd_t = macd_line.tail(n_bars)
        msig_t = macd_sig.tail(n_bars)
        mhst_t = macd_hist.tail(n_bars)
        macd_chart = [{'date':   macd_t.index[i].strftime('%Y-%m-%d'),
                       'macd':   sv(macd_t, i),
                       'signal': sv(msig_t, i),
                       'hist':   sv(mhst_t, i)} for i in range(len(macd_t))]

        return jsonify({'chart': chart_data, 'rsi_chart': rsi_chart, 'macd_chart': macd_chart})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ============================================================
# 売買シミュレーション (バックテスト)
# ============================================================

def run_backtest(symbol, strategy, period, initial_capital, direction='long'):
    """過去データに対して戦略を適用してシミュレーション

    direction:
      'long'  : 買って売る（買いシグナルで建てて、売りシグナルで仕舞う）
      'short' : 売って買い戻す（売りシグナルで建てて、買いシグナルで仕舞う）
      'both'  : 両建て切替（買い↔売りで都度ポジションを反転）
    """
    import re
    if re.fullmatch(r'\d{4,5}', symbol):
        symbol = symbol + '.T'
    else:
        symbol = symbol.upper()

    PERIOD_MAP = {'1y':'1y','2y':'2y','3y':'3y','5y':'5y'}
    yf_period = PERIOD_MAP.get(period, '2y')

    ticker = yf.Ticker(symbol)
    hist = ticker.history(period=yf_period)
    if hist.empty or len(hist) < 50:
        return {'error': 'データ不足でシミュレーションできません'}

    # 会社情報を取得
    info = {}
    try:
        info = ticker.info or {}
    except:
        pass
    company_name = info.get('longName') or info.get('shortName') or symbol
    company_name = get_jp_company_name(symbol, company_name)
    sector_en   = info.get('sector') or ''
    industry_en = info.get('industry') or ''
    industry_desc = get_industry_ja(industry_en, sector_en)
    sector_ja = SECTOR_JA.get(sector_en, sector_en)
    current_price = float(hist['Close'].iloc[-1])
    market_cap = info.get('marketCap')
    mc_str = None
    if market_cap:
        if market_cap >= 1e12: mc_str = f'{market_cap/1e12:.1f}兆'
        elif market_cap >= 1e8: mc_str = f'{market_cap/1e8:.0f}億'
        else: mc_str = f'{market_cap/1e6:.0f}M'

    close = hist['Close']
    n = len(close)

    # 各種指標を事前計算
    rsi    = ta.momentum.RSIIndicator(close, window=14).rsi()
    rsi3   = ta.momentum.RSIIndicator(close, window=3).rsi()    # day_trade 用
    macd_o = ta.trend.MACD(close)
    macd_l = macd_o.macd()
    macd_s = macd_o.macd_signal()
    bb     = ta.volatility.BollingerBands(close, window=20)
    bb_u   = bb.bollinger_hband()
    bb_l   = bb.bollinger_lband()
    ma5    = close.rolling(5).mean()
    ma25   = close.rolling(25).mean()
    ma75   = close.rolling(75).mean()
    # スイング・プロ手法用の追加指標
    try:
        adx_o = ta.trend.ADXIndicator(hist['High'], hist['Low'], close, window=14)
        adx_v = adx_o.adx()
    except Exception:
        adx_v = close * 0 + 25  # フォールバック
    try:
        atr_o = ta.volatility.AverageTrueRange(hist['High'], hist['Low'], close, window=14)
        atr_v = atr_o.average_true_range()
    except Exception:
        atr_v = close.rolling(14).std()
    try:
        stoch_o = ta.momentum.StochasticOscillator(hist['High'], hist['Low'], close, window=14, smooth_window=3)
        stoch_k = stoch_o.stoch()
        stoch_d = stoch_o.stoch_signal()
    except Exception:
        stoch_k = close * 0 + 50
        stoch_d = close * 0 + 50

    # ───────── シグナル判定（理由つき）─────────
    def signal_at(i):
        """各戦略について i 番目のバーで (signal, reason) を返す"""
        c    = close.iloc[i]
        rv   = rsi.iloc[i]
        rv_p = rsi.iloc[i-1] if i > 0 else rv
        ml   = macd_l.iloc[i]; ml_p = macd_l.iloc[i-1] if i > 0 else ml
        ms   = macd_s.iloc[i]; ms_p = macd_s.iloc[i-1] if i > 0 else ms
        m5   = ma5.iloc[i];   m5_p = ma5.iloc[i-1]   if i > 0 else m5
        m25  = ma25.iloc[i];  m25_p = ma25.iloc[i-1] if i > 0 else m25
        bbu  = bb_u.iloc[i]
        bbl  = bb_l.iloc[i]

        if strategy == 'rsi':
            if not np.isnan(rv) and not np.isnan(rv_p):
                if rv_p >= 30 and rv < 30: return 'buy', f'RSIが {rv:.1f} に低下（売られすぎゾーン突入）'
                if rv_p <= 70 and rv > 70: return 'sell', f'RSIが {rv:.1f} に上昇（買われすぎゾーン突入）'
        elif strategy == 'macd':
            if not (np.isnan(ml) or np.isnan(ms) or np.isnan(ml_p) or np.isnan(ms_p)):
                if ml_p <= ms_p and ml > ms: return 'buy', f'MACDゴールデンクロス（{ml:.2f} > {ms:.2f}）上昇トレンド転換'
                if ml_p >= ms_p and ml < ms: return 'sell', f'MACDデッドクロス（{ml:.2f} < {ms:.2f}）下落トレンド転換'
        elif strategy == 'ma_cross':
            if not (np.isnan(m5) or np.isnan(m25) or np.isnan(m5_p) or np.isnan(m25_p)):
                if m5_p <= m25_p and m5 > m25: return 'buy', f'MA5（{m5:.1f}）がMA25（{m25:.1f}）を上抜け（短期上昇）'
                if m5_p >= m25_p and m5 < m25: return 'sell', f'MA5（{m5:.1f}）がMA25（{m25:.1f}）を下抜け（短期下落）'
        elif strategy == 'bollinger':
            if not (np.isnan(bbu) or np.isnan(bbl)):
                if c < bbl: return 'buy', f'価格 {c:.2f} が下限バンド {bbl:.2f} を割れ（行き過ぎ反発狙い）'
                if c > bbu: return 'sell', f'価格 {c:.2f} が上限バンド {bbu:.2f} を超え（過熱で反落狙い）'
        elif strategy == 'combined':
            buy_votes = 0; sell_votes = 0; reasons = []
            if not np.isnan(rv):
                if rv < 35: buy_votes += 1; reasons.append(f'RSI {rv:.1f}（割安）')
                elif rv > 65: sell_votes += 1; reasons.append(f'RSI {rv:.1f}（過熱）')
            if not (np.isnan(ml) or np.isnan(ms)):
                if ml > ms: buy_votes += 1; reasons.append('MACD強気')
                else: sell_votes += 1; reasons.append('MACD弱気')
            if not (np.isnan(m5) or np.isnan(m25)):
                if m5 > m25: buy_votes += 1; reasons.append('MA上昇トレンド')
                else: sell_votes += 1; reasons.append('MA下降トレンド')
            if buy_votes >= 2 and sell_votes == 0:
                return 'buy', '買いシグナル2つ以上一致: ' + ' / '.join(reasons)
            if sell_votes >= 2 and buy_votes == 0:
                return 'sell', '売りシグナル2つ以上一致: ' + ' / '.join(reasons)
        elif strategy == 'day_trade':
            # デイトレード: 超短期RSI(3)＋ボリンジャー下限/上限の即時逆張り。1〜2バーで仕舞う前提
            r3   = rsi3.iloc[i]
            r3_p = rsi3.iloc[i-1] if i > 0 else r3
            if not (np.isnan(r3) or np.isnan(bbl) or np.isnan(bbu)):
                if r3_p >= 20 and r3 < 20 and c <= bbl * 1.005:
                    return 'buy', f'デイトレ買い: 超短期RSI3 {r3:.1f}が20割れ＆下限BB接近 → 即時リバウンド狙い'
                if r3_p <= 80 and r3 > 80 and c >= bbu * 0.995:
                    return 'sell', f'デイトレ売り: 超短期RSI3 {r3:.1f}が80超え＆上限BB接近 → 即時反落狙い'
        elif strategy == 'swing':
            # スイングトレード: MA25/MA75 のクロス＋ ADX>20 の確認。中期保有想定
            m25_now = m25; m25_p = ma25.iloc[i-1] if i > 0 else m25_now
            m75     = ma75.iloc[i]
            m75_p   = ma75.iloc[i-1] if i > 0 else m75
            adxv    = adx_v.iloc[i] if i < len(adx_v) else float('nan')
            if not (np.isnan(m25_now) or np.isnan(m75) or np.isnan(m25_p) or np.isnan(m75_p)):
                if m25_p <= m75_p and m25_now > m75 and (np.isnan(adxv) or adxv > 20):
                    return 'buy', f'スイング買い: MA25がMA75を上抜け（ADX {adxv:.1f}でトレンド確認）→ 中期上昇'
                if m25_p >= m75_p and m25_now < m75 and (np.isnan(adxv) or adxv > 20):
                    return 'sell', f'スイング売り: MA25がMA75を下抜け（ADX {adxv:.1f}でトレンド確認）→ 中期下落'
        elif strategy == 'pro_forex':
            # プロ式為替: トレンド(ADX>25) + モメンタム(MACD) + 逆張りフィルタ(RSI極値) + ATR でリスク調整
            adxv = adx_v.iloc[i] if i < len(adx_v) else float('nan')
            atrv = atr_v.iloc[i] if i < len(atr_v) else float('nan')
            if not (np.isnan(ml) or np.isnan(ms) or np.isnan(rv) or np.isnan(adxv)):
                if adxv > 25 and ml > ms and rv < 70 and rv > 40:
                    return 'buy', f'プロ式買い: ADX {adxv:.1f}（強トレンド）＋MACD強気＋RSI {rv:.1f}（過熱前）→ 順張りエントリ'
                if adxv > 25 and ml < ms and rv > 30 and rv < 60:
                    return 'sell', f'プロ式売り: ADX {adxv:.1f}（強トレンド）＋MACD弱気＋RSI {rv:.1f}（売られ過ぎ前）→ 順張り売り'
        elif strategy == 'ichimoku':
            # 一目均衡表: 価格が雲を上抜ければ買い・下抜ければ売り（簡易版: 26日高安平均＝基準線で代替）
            high26 = hist['High'].rolling(26).max().iloc[i] if i < len(hist) else float('nan')
            low26  = hist['Low'].rolling(26).min().iloc[i] if i < len(hist) else float('nan')
            high26_p = hist['High'].rolling(26).max().iloc[i-1] if i > 0 else high26
            low26_p  = hist['Low'].rolling(26).min().iloc[i-1] if i > 0 else low26
            if not (np.isnan(high26) or np.isnan(low26)):
                mid = (high26 + low26) / 2
                mid_p = (high26_p + low26_p) / 2 if not np.isnan(high26_p) else mid
                c_p = close.iloc[i-1] if i > 0 else c
                if c_p <= mid_p and c > mid:
                    return 'buy', f'一目買い: 基準線 {mid:.2f}を上抜け → 雲ブレイク上昇'
                if c_p >= mid_p and c < mid:
                    return 'sell', f'一目売り: 基準線 {mid:.2f}を下抜け → 雲ブレイク下降'
        elif strategy == 'stochastic':
            # ストキャスティクス: %K と %D のクロス × 過熱/過売ゾーン
            k_v = stoch_k.iloc[i] if i < len(stoch_k) else float('nan')
            d_v = stoch_d.iloc[i] if i < len(stoch_d) else float('nan')
            k_p = stoch_k.iloc[i-1] if i > 0 else k_v
            d_p = stoch_d.iloc[i-1] if i > 0 else d_v
            if not (np.isnan(k_v) or np.isnan(d_v) or np.isnan(k_p) or np.isnan(d_p)):
                if k_p <= d_p and k_v > d_v and k_v < 30:
                    return 'buy', f'ストキャ買い: %K {k_v:.1f}が%D {d_v:.1f}を売られ過ぎゾーンで上抜け'
                if k_p >= d_p and k_v < d_v and k_v > 70:
                    return 'sell', f'ストキャ売り: %K {k_v:.1f}が%D {d_v:.1f}を買われ過ぎゾーンで下抜け'
        return 'hold', ''

    # ───────── シミュレーション（ロング/ショート両対応）─────────
    cash = float(initial_capital)
    shares = 0           # ポジションの株数（>0 = ロング保有株数、<0 = ショート建玉株数）
    trades = []
    equity_curve = []
    cur_entry_price = None
    cur_entry_date = None
    cur_entry_reason = ''
    position = 'flat'    # 'flat' / 'long' / 'short'

    from datetime import datetime as _dt

    def _close_position(exit_price, exit_date, exit_reason):
        """現在のポジションを exit_price で仕舞う"""
        nonlocal cash, shares, position, cur_entry_price, cur_entry_date, cur_entry_reason
        if position == 'flat' or shares == 0 or cur_entry_price is None:
            return
        abs_shares = abs(shares)
        if position == 'long':
            proceeds = abs_shares * exit_price
            profit = proceeds - (abs_shares * cur_entry_price)
            profit_pct = (exit_price - cur_entry_price) / cur_entry_price * 100
            cash += proceeds
        else:  # short
            # ショート: 建玉時に得た現金（cash 増）を返却して仕舞う
            cost_to_cover = abs_shares * exit_price
            profit = (cur_entry_price - exit_price) * abs_shares
            profit_pct = (cur_entry_price - exit_price) / cur_entry_price * 100
            cash -= cost_to_cover
        try:
            holding_days = (_dt.strptime(exit_date, '%Y-%m-%d') - _dt.strptime(cur_entry_date, '%Y-%m-%d')).days
        except Exception:
            holding_days = 0
        trades.append({
            'side': position,                       # 'long' or 'short'
            'buy_date':  cur_entry_date if position == 'long' else exit_date,
            'sell_date': exit_date     if position == 'long' else cur_entry_date,
            'entry_date': cur_entry_date,
            'exit_date':  exit_date,
            'buy_price':  round(cur_entry_price if position == 'long' else exit_price, 4),
            'sell_price': round(exit_price if position == 'long' else cur_entry_price, 4),
            'entry_price': round(cur_entry_price, 4),
            'exit_price':  round(exit_price, 4),
            'shares': abs_shares,
            'profit': round(profit, 2),
            'profit_pct': round(profit_pct, 2),
            'win': profit > 0,
            'buy_reason':  cur_entry_reason if position == 'long' else exit_reason,
            'sell_reason': exit_reason if position == 'long' else cur_entry_reason,
            'entry_reason': cur_entry_reason,
            'exit_reason': exit_reason,
            'holding_days': holding_days,
        })
        shares = 0
        position = 'flat'
        cur_entry_price = None
        cur_entry_date = None
        cur_entry_reason = ''

    def _open_position(side, entry_price, entry_date, entry_reason):
        """新規ポジションを建てる"""
        nonlocal cash, shares, position, cur_entry_price, cur_entry_date, cur_entry_reason
        if cash <= entry_price:
            return
        n_shares = int(cash / entry_price)
        if n_shares <= 0:
            return
        if side == 'long':
            cost = n_shares * entry_price
            cash -= cost
            shares = n_shares
        else:  # short
            proceeds = n_shares * entry_price
            cash += proceeds
            shares = -n_shares
        position = side
        cur_entry_price = entry_price
        cur_entry_date = entry_date
        cur_entry_reason = entry_reason

    for i in range(n):
        date = hist.index[i].strftime('%Y-%m-%d')
        price = float(close.iloc[i])
        sig, reason = signal_at(i)

        if sig == 'buy':
            # ショート中なら買い戻し → 仕舞う
            if position == 'short':
                _close_position(price, date, reason)
            # ロング許可されてて flat なら新規ロング
            if position == 'flat' and direction in ('long', 'both'):
                _open_position('long', price, date, reason)
        elif sig == 'sell':
            # ロング中なら売却 → 仕舞う
            if position == 'long':
                _close_position(price, date, reason)
            # ショート許可されてて flat なら新規ショート
            if position == 'flat' and direction in ('short', 'both'):
                _open_position('short', price, date, reason)

        # 資産推移記録（評価額 = 現金 + 含み損益）
        if i % 5 == 0 or i == n - 1:
            if position == 'long':
                total = cash + abs(shares) * price
            elif position == 'short' and cur_entry_price is not None:
                total = cash - abs(shares) * price  # ショート: 価格上昇は損
            else:
                total = cash
            equity_curve.append({'date': date, 'equity': round(total, 2)})

    # 最後にポジション残ってたら最終価格で清算扱い（評価額計算）
    final_price = float(close.iloc[-1])
    if position == 'long':
        final_value = cash + abs(shares) * final_price
        unrealized = abs(shares) * (final_price - cur_entry_price) if cur_entry_price else 0
    elif position == 'short':
        final_value = cash - abs(shares) * final_price
        unrealized = abs(shares) * (cur_entry_price - final_price) if cur_entry_price else 0
    else:
        final_value = cash
        unrealized = 0

    # ───────── 指標計算 ─────────
    total_return = final_value - initial_capital
    total_return_pct = (final_value / initial_capital - 1) * 100

    # 期間（年）を実際の日付差から計算
    days = (hist.index[-1] - hist.index[0]).days
    years = max(days / 365.25, 0.01)
    annual_return = (final_value / initial_capital) ** (1 / years) - 1
    annual_return_pct = annual_return * 100

    # Buy&Hold比較
    bh_shares = int(initial_capital / float(close.iloc[0]))
    bh_cash_left = initial_capital - bh_shares * float(close.iloc[0])
    bh_final = bh_cash_left + bh_shares * final_price
    bh_return_pct = (bh_final / initial_capital - 1) * 100

    # 勝率
    win_count = sum(1 for t in trades if t['win'])
    win_rate = (win_count / len(trades) * 100) if trades else 0

    # 最大ドローダウン
    max_dd_pct = 0
    peak = initial_capital
    for pt in equity_curve:
        if pt['equity'] > peak:
            peak = pt['equity']
        dd = (peak - pt['equity']) / peak * 100
        if dd > max_dd_pct:
            max_dd_pct = dd

    # ベスト/ワースト取引
    best_trade = max(trades, key=lambda t: t['profit_pct']) if trades else None
    worst_trade = min(trades, key=lambda t: t['profit_pct']) if trades else None
    avg_holding = sum(t.get('holding_days', 0) for t in trades) / len(trades) if trades else 0
    avg_profit_pct = sum(t['profit_pct'] for t in trades) / len(trades) if trades else 0

    # ───────── 戦略説明 ─────────
    STRATEGY_DESC = {
        'rsi': {
            'name': 'RSI逆張り戦略',
            'rule_buy': 'RSI（14日）が30を下回った時 → 売られすぎと判断して買い',
            'rule_sell': 'RSI（14日）が70を上回った時 → 買われすぎと判断して売り',
            'description': 'RSI（相対力指数）は0〜100で値動きの過熱感を示す指標。30以下＝売られすぎ、70以上＝買われすぎとされ、その極端な水準で逆張りする戦略です。レンジ相場（一定範囲内で上下する相場）に強く、トレンド相場では機能しにくい傾向があります。',
        },
        'macd': {
            'name': 'MACDクロス戦略',
            'rule_buy': 'MACD線がシグナル線を下から上に突き抜けた時（ゴールデンクロス）→ 買い',
            'rule_sell': 'MACD線がシグナル線を上から下に突き抜けた時（デッドクロス）→ 売り',
            'description': 'MACDは2本の移動平均の差分でトレンドの転換点を捉える指標。ゴールデンクロスは上昇トレンドへの転換、デッドクロスは下降トレンドへの転換を示唆します。トレンド追従型の代表的戦略で、明確なトレンドが出る相場で機能しやすいです。',
        },
        'ma_cross': {
            'name': '移動平均クロス戦略',
            'rule_buy': 'MA5（5日線）がMA25（25日線）を上抜け → 買い',
            'rule_sell': 'MA5がMA25を下抜け → 売り',
            'description': '短期移動平均が長期移動平均を上抜くゴールデンクロスで買い、下抜くデッドクロスで売る、最もポピュラーなトレンド追従戦略。MACDよりも反応が遅いがダマシも少ない傾向。横ばい相場では小さな損切りが続きやすい欠点があります。',
        },
        'bollinger': {
            'name': 'ボリンジャーバンド逆張り戦略',
            'rule_buy': '株価がボリンジャーバンドの下限を割り込んだ時 → 買い',
            'rule_sell': '株価がボリンジャーバンドの上限を超えた時 → 売り',
            'description': 'ボリンジャーバンドは過去20日の標準偏差で価格の変動範囲を示します。バンドを超える＝統計的に異常な動きとして反落・反発を期待する逆張り戦略。レンジ相場では有効ですが、強いトレンドが出ると逆張りで損が膨らむリスクがあります。',
        },
        'combined': {
            'name': '総合判定戦略（RSI + MACD + 移動平均）',
            'rule_buy': '3指標のうち2つ以上が買いシグナルで、売りシグナルが0の時 → 買い',
            'rule_sell': '3指標のうち2つ以上が売りシグナルで、買いシグナルが0の時 → 売り',
            'description': 'RSIの過熱感、MACDのトレンド勢い、移動平均のトレンド方向を総合判定する複合戦略。複数指標の合意を要求するため取引頻度は少なめですが、ダマシのリスクを下げ、より確度の高いタイミングで売買します。',
        },
        'day_trade': {
            'name': 'デイトレード戦略（短期RSI3 + ボリンジャー）',
            'rule_buy': 'RSI3が20を下抜けかつ価格がBB下限に接近 → 即時リバウンド狙いで買い',
            'rule_sell': 'RSI3が80を上抜けかつ価格がBB上限に接近 → 即時反落狙いで売り',
            'description': 'プロのデイトレーダーが使う「超短期RSI(3日)」と「ボリンジャーバンド逆張り」を組み合わせた戦略。1〜2バーで決着する想定で、極端な押し目買い・吹き値売りを狙います。エントリ頻度が高く、レンジ相場で機能しますが、強いトレンドでは負けが続くことがあります。',
        },
        'swing': {
            'name': 'スイングトレード戦略（MA25/MA75 + ADX確認）',
            'rule_buy': 'MA25がMA75を上抜けかつADX>20（トレンド明確）→ 中期上昇エントリ',
            'rule_sell': 'MA25がMA75を下抜けかつADX>20（トレンド明確）→ 中期下落エントリ',
            'description': '数日〜数週間の中期保有を想定したスイング戦略。中期移動平均クロス（MA25/MA75）でトレンド転換を捉え、ADX>20でトレンドの強さを確認してからエントリします。デイトレほど忙しくなく、長期投資よりは機敏に資金を回せる、サラリーマン投資家に人気の手法です。',
        },
        'pro_forex': {
            'name': 'プロ式為替戦略（ADX + MACD + RSI + ATR）',
            'rule_buy': 'ADX>25（強トレンド）+ MACD強気 + RSI 40-70（過熱前ゾーン）→ 順張り買い',
            'rule_sell': 'ADX>25（強トレンド）+ MACD弱気 + RSI 30-60（売られ過ぎ前）→ 順張り売り',
            'description': '為替プロが使う複合判定戦略。ADXで「そもそもトレンドが出てるか」を確認し、MACDで方向、RSIで「過熱しすぎてないか」をチェックします。ATRも参照してボラティリティを見ながらの順張り。レンジ相場ではエントリせず、強いトレンドに乗ることでドローダウンを抑える狙い。',
        },
        'ichimoku': {
            'name': '一目均衡表（基準線ブレイク）',
            'rule_buy': '価格が26日基準線（高安平均）を上抜け → 雲ブレイク上昇で買い',
            'rule_sell': '価格が26日基準線を下抜け → 雲ブレイク下降で売り',
            'description': '日本発の有名指標「一目均衡表」のシンプル版。26日の高値・安値の中値を基準線とし、価格がそれを抜けるとトレンド転換と判断します。雲（先行スパン）を完全に再現する代わりに基準線で代用しており、本物より反応が早めです。',
        },
        'stochastic': {
            'name': 'ストキャスティクス（%K/%D逆張り）',
            'rule_buy': '%K(14,3)が%Dを売られ過ぎゾーン(<30)で上抜け → 反発狙いで買い',
            'rule_sell': '%K(14,3)が%Dを買われ過ぎゾーン(>70)で下抜け → 反落狙いで売り',
            'description': 'ストキャスティクスは過去14日の値幅における現在価格の位置を表す指標。%K（高速）と%D（低速）のクロスで売買タイミングを取り、極端なゾーン（<30 / >70）でのみ動作させることでダマシを減らします。レンジ相場の精度が高い逆張り戦略。',
        },
    }
    strat = STRATEGY_DESC.get(strategy, STRATEGY_DESC['combined'])

    # ───────── 総評生成 ─────────
    summary_lines = []
    if total_return_pct > 0:
        summary_lines.append(f'✅ {years:.1f}年間で **+{total_return_pct:.2f}%** のリターンを獲得しました（年率 +{annual_return_pct:.2f}%）。')
    else:
        summary_lines.append(f'❌ {years:.1f}年間で **{total_return_pct:.2f}%** のマイナスとなりました（年率 {annual_return_pct:.2f}%）。')

    if vs_bh := round(total_return_pct - bh_return_pct, 2):
        if vs_bh > 5:
            summary_lines.append(f'🏆 同期間に同じ銘柄を「買って放置」した場合（+{bh_return_pct:.2f}%）よりも **{vs_bh:+.2f}%** 上回り、戦略の有効性が確認できました。')
        elif vs_bh > -5:
            summary_lines.append(f'⚖️ 「買って放置」（+{bh_return_pct:.2f}%）とほぼ同等の成績（{vs_bh:+.2f}%）。手数料・税金・労力を考えると、シンプルなBuy&Holdの方が合理的かもしれません。')
        else:
            summary_lines.append(f'⚠️ 「買って放置」（+{bh_return_pct:.2f}%）に **{vs_bh:.2f}%** 劣りました。この銘柄ではタイミング売買よりも長期保有の方が有効でした。')

    if trades:
        summary_lines.append(f'📊 シグナルに従って **{len(trades)}回** の売買を実行。勝率は **{win_rate:.1f}%**（{win_count}勝{len(trades)-win_count}敗）、平均保有期間 **{avg_holding:.0f}日**、1取引あたり平均 **{avg_profit_pct:+.2f}%** のリターンでした。')
        if best_trade:
            summary_lines.append(f'🥇 最高の取引: {best_trade["buy_date"]} 買い → {best_trade["sell_date"]} 売り、**{best_trade["profit_pct"]:+.2f}%**（{best_trade.get("holding_days",0)}日保有）')
        if worst_trade and worst_trade != best_trade:
            summary_lines.append(f'💔 最悪の取引: {worst_trade["buy_date"]} 買い → {worst_trade["sell_date"]} 売り、**{worst_trade["profit_pct"]:+.2f}%**（{worst_trade.get("holding_days",0)}日保有）')
    else:
        summary_lines.append('⚠️ シミュレーション期間中、戦略が買い・売りシグナルを出さず取引が発生しませんでした。期間を延ばすか、別の戦略を試してみてください。')

    summary_lines.append(f'📉 最大ドローダウン（過去最大の含み損）は **-{max_dd_pct:.2f}%**。心理的にここまでの含み損に耐えられるかが重要です。')

    return {
        'symbol': symbol,
        'company_name': company_name,
        'sector': sector_ja,
        'industry_desc': industry_desc,
        'current_price': round(current_price, 2),
        'market_cap': mc_str,
        'strategy': strategy,
        'direction': direction,
        'strategy_name': strat['name'],
        'strategy_rule_buy': strat['rule_buy'],
        'strategy_rule_sell': strat['rule_sell'],
        'strategy_description': strat['description'],
        'period': period,
        'years': round(years, 1),
        'initial_capital': initial_capital,
        'final_value': round(final_value, 2),
        'total_return': round(total_return, 2),
        'total_return_pct': round(total_return_pct, 2),
        'annual_return_pct': round(annual_return_pct, 2),
        'bh_return_pct': round(bh_return_pct, 2),
        'vs_bh_pct': round(total_return_pct - bh_return_pct, 2),
        'trade_count': len(trades),
        'win_rate': round(win_rate, 1),
        'max_drawdown_pct': round(max_dd_pct, 2),
        'avg_holding_days': round(avg_holding, 1),
        'avg_profit_pct': round(avg_profit_pct, 2),
        'best_trade': best_trade,
        'worst_trade': worst_trade,
        'shares_held': shares,
        'cash_remaining': round(cash, 2),
        'unrealized_pnl': round(unrealized, 2),
        'trades': trades,
        'equity_curve': equity_curve,
        'start_date': hist.index[0].strftime('%Y-%m-%d'),
        'end_date': hist.index[-1].strftime('%Y-%m-%d'),
        'summary_lines': summary_lines,
    }


@app.route('/api/backtest', methods=['POST'])
def backtest_endpoint():
    data = request.get_json()
    symbol    = (data.get('symbol') or '').strip()
    strategy  = data.get('strategy', 'combined')
    period    = data.get('period', '2y')
    capital   = float(data.get('capital', 1000000))
    direction = data.get('direction', 'long')
    if direction not in ('long', 'short', 'both'):
        direction = 'long'

    if not symbol:
        return jsonify({'error':'銘柄を指定してください'}), 400
    try:
        result = run_backtest(symbol, strategy, period, capital, direction=direction)
        if 'error' in result:
            return jsonify(result), 400
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': f'シミュレーション失敗: {e}'}), 500


@app.route('/api/deep_analysis', methods=['POST'])
def deep_analysis():
    data = request.get_json()
    symbol = data.get('symbol', '').strip()
    import re
    if re.fullmatch(r'\d{4,5}', symbol):
        symbol = symbol + '.T'
    else:
        symbol = symbol.upper()
    try:
        ticker  = yf.Ticker(symbol)
        info    = ticker.info or {}
        news    = ticker.news or []
        titles  = [(n.get('content') or {}).get('title') or n.get('title','') for n in news[:3]]
        titles_ja = [translate_ja(t) for t in titles if t]

        # indicators / fundamentalsはリクエストから受け取る（再計算不要）
        indicators   = data.get('indicators', {})
        fundamentals = data.get('fundamentals', {})
        verdict      = data.get('verdict', 'NEUTRAL')
        score        = data.get('score', 50)

        sections = generate_deep_analysis(symbol, info, indicators, fundamentals, verdict, score, titles_ja)
        return jsonify({'sections': sections})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ============================================================
# 売買アクションプラン生成
# ============================================================
def generate_action_plan(symbol, indicators, verdict, score, price, week52_high, week52_low, fundamentals=None, atr=None, adx=None, pivot=None, is_forex=False, digits=2, pip_unit=None):
    """
    具体的な売買アクション提案を生成する。
    Returns: dict with action, strategy_type, entry, stop_loss, take_profits, position_size,
             holding_period, exit_conditions, confidence, reasoning
    """
    # ────── 1. アクション判定（5択） ──────
    rsi_val = (indicators.get('rsi') or {}).get('value') or 50
    macd_sig = (indicators.get('macd') or {}).get('signal', 'neutral')
    bb_sig   = (indicators.get('bollinger') or {}).get('signal', 'neutral')
    ma_sig   = (indicators.get('ma') or {}).get('signal', 'neutral')
    ma5  = (indicators.get('ma') or {}).get('ma5')
    ma25 = (indicators.get('ma') or {}).get('ma25')
    ma75 = (indicators.get('ma') or {}).get('ma75')
    bb_upper = (indicators.get('bollinger') or {}).get('upper')
    bb_lower = (indicators.get('bollinger') or {}).get('lower')

    # 52週内位置
    w52_pos = None
    if week52_high and week52_low and week52_high > week52_low:
        w52_pos = (price - week52_low) / (week52_high - week52_low) * 100

    # アクション判定
    if score >= 70:
        if rsi_val < 65 and (not bb_upper or price < bb_upper * 0.98):
            action = 'BUY_NOW'
            action_label = '🟢 今すぐ買い'
            action_color = 'buy'
        else:
            action = 'WAIT_PULLBACK'
            action_label = '🟡 押し目待ち（過熱気味）'
            action_color = 'weak_buy'
    elif score >= 55:
        if rsi_val > 70 or (bb_upper and price > bb_upper * 0.99):
            action = 'WAIT_PULLBACK'
            action_label = '🟡 押し目待ち'
            action_color = 'weak_buy'
        elif w52_pos and w52_pos > 95:
            action = 'WAIT_PULLBACK'
            action_label = '🟡 高値圏 — 押し目待ち推奨'
            action_color = 'weak_buy'
        else:
            action = 'BUY_NOW'
            action_label = '🟢 慎重に買い'
            action_color = 'buy'
    elif score >= 40:
        action = 'WATCH'
        action_label = '⚪ 様子見'
        action_color = 'neutral'
    elif score >= 25:
        action = 'SELL_OR_AVOID'
        action_label = '🟠 売り検討 / 新規エントリー回避'
        action_color = 'weak_sell'
    else:
        action = 'SELL_NOW'
        action_label = '🔴 売り推奨 / 撤退検討'
        action_color = 'sell'

    # ────── 2. 戦略タイプ判定 ──────
    adx_val = (adx if isinstance(adx, (int, float)) else None) or (indicators.get('adx') or {}).get('value')
    atr_val = (atr if isinstance(atr, (int, float)) else None) or (indicators.get('atr') or {}).get('value')

    # ボラティリティから時間軸推定
    atr_pct = (atr_val / price * 100) if (atr_val and price) else 0.5
    if is_forex:
        # FXは時間軸が短め
        if atr_pct > 0.8 and adx_val and adx_val > 30:
            strategy_type = 'デイトレ〜スキャル'
            strategy_short = 'デイトレ'
            holding_period = '数時間 〜 1営業日'
        elif adx_val and adx_val > 25:
            strategy_type = 'スイングトレード'
            strategy_short = 'スイング'
            holding_period = '数日 〜 2週間'
        else:
            strategy_type = '短期スイング / レンジ取引'
            strategy_short = '短期スイング'
            holding_period = '1日 〜 1週間'
    else:
        # 株式は時間軸が長め
        if atr_pct > 4:
            strategy_type = 'スイングトレード（高ボラ銘柄）'
            strategy_short = 'スイング'
            holding_period = '1週間 〜 1ヶ月'
        elif adx_val and adx_val > 25:
            strategy_type = 'スイング〜中期保有'
            strategy_short = 'スイング'
            holding_period = '2週間 〜 数ヶ月'
        elif fundamentals and (fundamentals.get('roe') or 0) > 15:
            strategy_type = '中長期保有（優良株）'
            strategy_short = '中長期'
            holding_period = '数ヶ月 〜 1年以上'
        else:
            strategy_type = 'スイングトレード'
            strategy_short = 'スイング'
            holding_period = '1週間 〜 1ヶ月'

    # ────── 3. エントリー価格 ──────
    if action in ('BUY_NOW', 'SELL_NOW'):
        entry_price = price
        entry_condition = '現在価格で即エントリー'
    elif action == 'WAIT_PULLBACK':
        # MA25 or BB中央 or S1 で押し目買い狙い
        candidates = []
        if ma25:           candidates.append(('MA25', ma25))
        if pivot and pivot.get('s1'): candidates.append(('S1ピボット', pivot['s1']))
        if bb_lower:       candidates.append(('BB下限', bb_lower))
        # 現在価格未満で最も近いものを選ぶ
        candidates = [c for c in candidates if c[1] < price]
        if candidates:
            candidates.sort(key=lambda x: -x[1])  # 高い順（=現在価格に近い順）
            entry_label, entry_price = candidates[0]
            entry_condition = f'{entry_label}（{round(entry_price, digits)}）への押し目を待ってエントリー'
        else:
            entry_price = price * 0.98
            entry_condition = f'現在価格の-2%（約 {round(entry_price, digits)}）まで待ってエントリー'
    elif action == 'SELL_OR_AVOID':
        entry_price = price
        entry_condition = '保有中なら戻り（MA25 or R1）で売却、新規買いは見送り'
    else:  # WATCH
        entry_price = None
        entry_condition = '明確なシグナルが出るまで待機（RSI<35 or MACDゴールデンクロス等）'

    # ────── 4. 損切りライン ──────
    sl_price = None
    sl_reason = ''
    if entry_price and action in ('BUY_NOW', 'WAIT_PULLBACK'):
        candidates = []
        if atr_val:        candidates.append(('ATR×1.5', entry_price - atr_val * 1.5))
        if pivot and pivot.get('s2'): candidates.append(('S2ピボット', pivot['s2']))
        if ma75:           candidates.append(('MA75', ma75))
        if week52_low:     candidates.append(('52週安値', week52_low))
        # 最も近い（=損失が小さい）SLを選ぶ
        candidates = [c for c in candidates if c[1] < entry_price]
        if candidates:
            candidates.sort(key=lambda x: -x[1])
            sl_reason, sl_price = candidates[0]
        else:
            sl_price = entry_price * 0.95
            sl_reason = '-5%（規定）'
    elif entry_price and action in ('SELL_NOW', 'SELL_OR_AVOID'):
        # 売りポジションの損切りは上方向
        if atr_val: sl_price = entry_price + atr_val * 1.5; sl_reason = 'ATR×1.5'
        elif ma25:  sl_price = ma25 * 1.02; sl_reason = 'MA25 +2%'

    # ────── 5. 利確ライン（3段階） ──────
    take_profits = []
    if entry_price and sl_price and action in ('BUY_NOW', 'WAIT_PULLBACK'):
        risk = entry_price - sl_price
        if risk > 0:
            tp1 = entry_price + risk * 1.0   # リスクリワード 1:1
            tp2 = entry_price + risk * 2.0   # 1:2
            tp3 = entry_price + risk * 3.0   # 1:3
            # 52週高値があれば、それ以上は不自然なので調整
            if week52_high and tp3 > week52_high * 1.1:
                tp3 = week52_high * 1.05
            take_profits = [
                {'label': 'TP1（1/3利確）', 'price': round(tp1, digits), 'rr': '1:1', 'pct': round((tp1-entry_price)/entry_price*100, 2)},
                {'label': 'TP2（1/3利確）', 'price': round(tp2, digits), 'rr': '1:2', 'pct': round((tp2-entry_price)/entry_price*100, 2)},
                {'label': 'TP3（残り）',   'price': round(tp3, digits), 'rr': '1:3', 'pct': round((tp3-entry_price)/entry_price*100, 2)},
            ]
    elif entry_price and sl_price and action in ('SELL_NOW', 'SELL_OR_AVOID'):
        risk = sl_price - entry_price
        if risk > 0:
            tp1 = entry_price - risk * 1.0
            tp2 = entry_price - risk * 2.0
            tp3 = entry_price - risk * 3.0
            if week52_low and tp3 < week52_low * 0.9:
                tp3 = week52_low * 0.95
            take_profits = [
                {'label': 'TP1（1/3利確）', 'price': round(tp1, digits), 'rr': '1:1', 'pct': round((tp1-entry_price)/entry_price*100, 2)},
                {'label': 'TP2（1/3利確）', 'price': round(tp2, digits), 'rr': '1:2', 'pct': round((tp2-entry_price)/entry_price*100, 2)},
                {'label': 'TP3（残り）',   'price': round(tp3, digits), 'rr': '1:3', 'pct': round((tp3-entry_price)/entry_price*100, 2)},
            ]

    # ────── 6. ポジションサイズ計算（口座100万円・リスク1%基準） ──────
    position_calc = []
    for account_size in [1_000_000, 3_000_000, 10_000_000]:
        risk_amount_1pct = account_size * 0.01
        risk_amount_2pct = account_size * 0.02
        if entry_price and sl_price and abs(entry_price - sl_price) > 0:
            unit_risk = abs(entry_price - sl_price)
            if is_forex and pip_unit:
                # FXは pips ベース
                pips = unit_risk / pip_unit
                # 1Lot = 10万通貨で1pipsあたり1000円相当（USDJPY前提の概算）
                lots_1pct = risk_amount_1pct / (pips * 1000) if pips > 0 else 0
                lots_2pct = risk_amount_2pct / (pips * 1000) if pips > 0 else 0
                position_calc.append({
                    'account': account_size,
                    'risk_1pct_label': f'{lots_1pct:.2f} Lot（10万通貨単位）',
                    'risk_2pct_label': f'{lots_2pct:.2f} Lot（10万通貨単位）',
                    'sl_distance': f'{round(pips, 1)} pips',
                })
            else:
                shares_1pct = int(risk_amount_1pct / unit_risk)
                shares_2pct = int(risk_amount_2pct / unit_risk)
                position_calc.append({
                    'account': account_size,
                    'risk_1pct_label': f'{shares_1pct:,}株（投資額 約{int(shares_1pct*entry_price):,}円）',
                    'risk_2pct_label': f'{shares_2pct:,}株（投資額 約{int(shares_2pct*entry_price):,}円）',
                    'sl_distance': f'1株あたり {round(unit_risk, digits)} の損失リスク',
                })

    # ────── 7. 撤退条件 ──────
    exit_conditions = []
    if action in ('BUY_NOW', 'WAIT_PULLBACK'):
        if sl_price:
            exit_conditions.append(f'❌ <b>損切り発動</b>: 価格が <b>{round(sl_price, digits)}</b>（{sl_reason}）を下回ったら即時撤退。')
        exit_conditions.append('📉 <b>MACDデッドクロス</b>が発生したら、利益を守るためポジション縮小。')
        exit_conditions.append('📐 <b>MA5がMA25を下抜け</b>たら、短期トレンド転換のサイン。半分以上利確検討。')
        if w52_pos and w52_pos > 90:
            exit_conditions.append('🚨 <b>52週高値圏</b>での反落シグナル（長い上ヒゲ、出来高伴う陰線）に注意。')
        exit_conditions.append(f'⏰ <b>時間切れ</b>: {holding_period} 経っても利益が出なければシナリオ崩れと判断し撤退。')
    elif action in ('SELL_NOW', 'SELL_OR_AVOID'):
        if sl_price:
            exit_conditions.append(f'❌ <b>損切り発動</b>: 価格が <b>{round(sl_price, digits)}</b> を上回ったら売りポジション撤退。')
        exit_conditions.append('📈 <b>MACDゴールデンクロス</b>が発生したら売りシナリオ無効。')
    else:
        exit_conditions.append('🔍 ポジション保有していなければ、明確なシグナル発生まで待機。')

    # ────── 8. エントリー条件 ──────
    entry_conditions = []
    if action == 'BUY_NOW':
        entry_conditions.append(f'✅ <b>現在価格 {round(price, digits)} で買い注文</b>')
        if rsi_val and rsi_val < 35:
            entry_conditions.append(f'✅ RSI {round(rsi_val,1)} は売られすぎゾーン — 反発期待')
        if macd_sig in ('buy', 'weak_buy'):
            entry_conditions.append('✅ MACDが買いサイン点灯中')
        if ma_sig in ('buy', 'weak_buy'):
            entry_conditions.append('✅ 移動平均トレンドが上向き')
    elif action == 'WAIT_PULLBACK':
        entry_conditions.append(f'⏳ <b>{entry_condition}</b>')
        entry_conditions.append('⏳ 押し目で RSI < 50 まで冷えたら絶好の買い場')
        entry_conditions.append('⏳ 出来高を伴う反発確認後にエントリー')
    elif action == 'WATCH':
        entry_conditions.append('🔍 RSI < 35（売られすぎ反発）')
        entry_conditions.append('🔍 MACDゴールデンクロス発生')
        entry_conditions.append('🔍 MA5 が MA25 を上抜け')
    elif action == 'SELL_NOW':
        entry_conditions.append(f'⚠️ <b>現在価格 {round(price, digits)} で売却 / ショート</b>')
    elif action == 'SELL_OR_AVOID':
        entry_conditions.append('⚠️ 保有中なら早めの利確・損切り検討')
        entry_conditions.append('⚠️ 新規買いは見送り、戻り売り狙い')

    # ────── 9. 信頼度（A/B/C） ──────
    confidence = 'B'
    confidence_reason = ''
    if score >= 75 or score <= 25:
        confidence = 'A'
        confidence_reason = '複数指標が同方向に強く一致しており、確度の高いシグナルです。'
    elif 45 <= score <= 55:
        confidence = 'C'
        confidence_reason = '指標がまちまちで方向感が不明確。様子見が安全。'
    else:
        confidence = 'B'
        confidence_reason = 'シグナルは出ているが、確信度は中程度。リスク管理を厳格に。'

    # ────── 10. ワンライナー総括 ──────
    if action == 'BUY_NOW':
        summary = f'今が買いのチャンス。{round(price, digits)} で買い、{round(sl_price, digits) if sl_price else "—"} で損切り、{take_profits[0]["price"] if take_profits else "—"} で1/3利確から始めましょう。'
    elif action == 'WAIT_PULLBACK':
        summary = f'方向性は買いだが現在は過熱気味。{round(entry_price, digits)} まで押すのを待ってからエントリーしましょう。'
    elif action == 'WATCH':
        summary = '今は売買シグナルが弱い局面。新規エントリーは控え、明確なサイン発生まで待つのがベスト。'
    elif action == 'SELL_OR_AVOID':
        summary = '下落リスクが優勢。保有中なら利確・損切りを進め、新規買いは控えるべき局面。'
    else:  # SELL_NOW
        summary = f'明確な売りシグナル発生中。保有中なら撤退、{round(price, digits)} で売却 / ショート検討。'

    return {
        'action': action,
        'action_label': action_label,
        'action_color': action_color,
        'strategy_type': strategy_type,
        'strategy_short': strategy_short,
        'holding_period': holding_period,
        'entry_price': round(entry_price, digits) if entry_price else None,
        'entry_condition': entry_condition,
        'entry_checklist': entry_conditions,
        'stop_loss': round(sl_price, digits) if sl_price else None,
        'stop_loss_reason': sl_reason,
        'take_profits': take_profits,
        'position_sizing': position_calc,
        'exit_conditions': exit_conditions,
        'confidence': confidence,
        'confidence_reason': confidence_reason,
        'summary': summary,
        'current_price': round(price, digits) if price else None,
    }


@app.route('/api/action_plan', methods=['POST'])
def action_plan_endpoint():
    data = request.get_json()
    try:
        symbol     = (data.get('symbol') or '').strip()
        indicators = data.get('indicators', {})
        fundamentals = data.get('fundamentals')
        verdict    = data.get('verdict', 'NEUTRAL')
        score      = data.get('score', 50)
        price      = float(data.get('price', 0))
        w52h       = data.get('week52_high')
        w52l       = data.get('week52_low')
        plan = generate_action_plan(
            symbol, indicators, verdict, score, price, w52h, w52l,
            fundamentals=fundamentals, digits=2, is_forex=False
        )
        return jsonify(plan)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/forex/action_plan', methods=['POST'])
def forex_action_plan_endpoint():
    data = request.get_json()
    try:
        symbol     = normalize_forex_symbol(data.get('symbol') or '')
        indicators = data.get('indicators', {})
        verdict    = data.get('verdict', 'NEUTRAL')
        score      = data.get('score', 50)
        price      = float(data.get('price', 0))
        w52h       = data.get('week52_high')
        w52l       = data.get('week52_low')
        pivot      = data.get('pivot_points')
        digits     = data.get('digits', 4)
        is_jpy = symbol.endswith('JPY=X')
        pip_unit = 0.01 if is_jpy else 0.0001
        plan = generate_action_plan(
            symbol, indicators, verdict, score, price, w52h, w52l,
            pivot=pivot, is_forex=True, digits=digits, pip_unit=pip_unit
        )
        return jsonify(plan)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ============================================================
# 為替（FX）分析エンドポイント
# ============================================================
@app.route('/forex')
def forex_page():
    return render_template('forex.html')


def quick_analyze_forex(symbol):
    """通貨ペアのクイック分析（スキャン用）"""
    try:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period="3mo")
        if hist.empty or len(hist) < 20:
            return None
        close = hist['Close']
        rsi_s = ta.momentum.RSIIndicator(close, window=14).rsi()
        macd_obj = ta.trend.MACD(close)
        bb = ta.volatility.BollingerBands(close, window=20)
        ma25 = close.rolling(25).mean()
        ma75 = close.rolling(75).mean()

        def sv(s):
            try:
                v = s.iloc[-1]
                return None if np.isnan(v) else float(v)
            except: return None

        cur = sv(close)
        prev = float(close.iloc[-2]) if len(close) >= 2 else cur
        change = ((cur - prev) / prev * 100) if prev else 0

        rsi_sig, _  = get_rsi_signal(sv(rsi_s))
        macd_sig, _ = get_macd_signal(sv(macd_obj.macd()), sv(macd_obj.macd_signal()), sv(macd_obj.macd_diff()))
        bb_sig, _   = get_bb_signal(cur, sv(bb.bollinger_hband()), sv(bb.bollinger_lband()), sv(bb.bollinger_mavg()))
        ma_sig, _   = get_ma_signal(cur, sv(close.rolling(5).mean()), sv(ma25), sv(ma75))
        # 出来高はFXでは無いので除外
        verdict, score = score_to_verdict([rsi_sig, macd_sig, bb_sig, ma_sig])

        # 52週レンジ
        hist52 = ticker.history(period="1y")
        w52_high = float(hist52['Close'].max()) if not hist52.empty else None
        w52_low  = float(hist52['Close'].min()) if not hist52.empty else None
        w52_pos = None
        if w52_high and w52_low and cur and w52_high > w52_low:
            w52_pos = round((cur - w52_low) / (w52_high - w52_low) * 100)

        # ワンライナーコメント
        parts = []
        if verdict in ('STRONG_BUY','BUY'): parts.append('強い買いシグナル')
        elif verdict == 'WEAK_BUY':         parts.append('やや買い優勢')
        elif verdict in ('STRONG_SELL','SELL'): parts.append('強い売りシグナル')
        elif verdict == 'WEAK_SELL':        parts.append('やや売り優勢')
        else:                                parts.append('方向感なし')
        rv = sv(rsi_s)
        if rv:
            if rv < 35:   parts.append('RSI売られすぎ圏')
            elif rv > 70: parts.append('RSI過熱気味')
        if w52_pos and w52_pos >= 90: parts.append('52週高値圏')
        elif w52_pos and w52_pos <= 10: parts.append('52週安値圏')
        comment = ' · '.join(parts[:3])

        meta = get_forex_meta(symbol)

        # FX価格は小数点以下が重要（USDJPYは小数2位、ドルストレートは小数4位）
        digits = 2 if symbol.startswith(('USDJPY','EURJPY','GBPJPY','AUDJPY','NZDJPY','CADJPY','CHFJPY','TRYJPY','ZARJPY','MXNJPY')) else 4

        return {
            'symbol': symbol,
            'name': meta['name'],
            'desc': meta['desc'],
            'price': round(cur, digits) if cur else None,
            'change': round(change, 2),
            'score': score,
            'verdict': verdict,
            'rsi': round(sv(rsi_s), 1) if sv(rsi_s) else None,
            'signals': {'rsi': rsi_sig, 'macd': macd_sig, 'bb': bb_sig, 'ma': ma_sig},
            'w52_pos': w52_pos,
            'w52_high': round(w52_high, digits) if w52_high else None,
            'w52_low':  round(w52_low,  digits) if w52_low  else None,
            'comment': comment,
            'digits': digits,
        }
    except Exception as e:
        return None


@app.route('/api/forex/scan', methods=['GET'])
def forex_scan():
    from concurrent.futures import ThreadPoolExecutor
    results = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(quick_analyze_forex, p['symbol']): p for p in FOREX_PAIRS}
        for fut, p in futures.items():
            r = fut.result()
            if r: results.append(r)
    results.sort(key=lambda x: x['score'], reverse=True)
    return jsonify({'pairs': results, 'updated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S')})


@app.route('/api/forex/analyze', methods=['POST'])
def forex_analyze():
    data = request.get_json()
    raw_symbol = (data.get('symbol') or '').strip()
    if not raw_symbol:
        return jsonify({'error': '通貨ペアを入力してください'}), 400
    symbol = normalize_forex_symbol(raw_symbol)

    try:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period="6mo")
        if hist.empty:
            return jsonify({'error': f'{symbol} のデータが取得できません。例: USDJPY, EURUSD, GBPJPY'}), 404

        close = hist['Close']
        high  = hist['High']
        low   = hist['Low']

        rsi_series  = ta.momentum.RSIIndicator(close, window=14).rsi()
        macd_obj    = ta.trend.MACD(close)
        macd_line   = macd_obj.macd()
        macd_signal = macd_obj.macd_signal()
        macd_hist   = macd_obj.macd_diff()
        bb          = ta.volatility.BollingerBands(close, window=20)
        bb_upper, bb_lower, bb_mid = bb.bollinger_hband(), bb.bollinger_lband(), bb.bollinger_mavg()
        ma5  = close.rolling(5).mean()
        ma25 = close.rolling(25).mean()
        ma75 = close.rolling(75).mean()
        # FX特化指標
        atr14 = ta.volatility.AverageTrueRange(high, low, close, window=14).average_true_range()
        adx14 = ta.trend.ADXIndicator(high, low, close, window=14).adx()
        stoch = ta.momentum.StochasticOscillator(high, low, close, window=14, smooth_window=3)
        stoch_k = stoch.stoch()
        stoch_d = stoch.stoch_signal()

        def last(s):
            try:
                v = s.iloc[-1]
                return None if np.isnan(v) else float(v)
            except: return None

        cur_close = round(last(close), 4)
        prev = float(close.iloc[-2]) if len(close) >= 2 else cur_close
        price_change = ((cur_close - prev) / prev * 100) if prev else 0

        cur_rsi = last(rsi_series)
        cur_macd = last(macd_line)
        rsi_sig, rsi_desc = get_rsi_signal(cur_rsi)
        macd_sig, macd_desc = get_macd_signal(cur_macd, last(macd_signal), last(macd_hist))
        cur_bb_upper, cur_bb_lower, cur_bb_mid = last(bb_upper), last(bb_lower), last(bb_mid)
        bb_sig, bb_desc = get_bb_signal(cur_close, cur_bb_upper, cur_bb_lower, cur_bb_mid)
        cur_ma5, cur_ma25, cur_ma75 = last(ma5), last(ma25), last(ma75)
        ma_sig, ma_desc = get_ma_signal(cur_close, cur_ma5, cur_ma25, cur_ma75)

        verdict, score = score_to_verdict([rsi_sig, macd_sig, bb_sig, ma_sig])

        # 52週ハイ・ロー
        hist52 = ticker.history(period="1y")
        week52_high = float(hist52['Close'].max()) if not hist52.empty else None
        week52_low  = float(hist52['Close'].min()) if not hist52.empty else None

        # ───────── FX特化分析 ─────────
        # ATR（平均真の値幅 = 平均的な日次変動幅）
        cur_atr = last(atr14)
        # ADX（トレンド強度: 25以上で強いトレンド）
        cur_adx = last(adx14)
        # Stochastic（買われすぎ/売られすぎ）
        cur_stoch_k = last(stoch_k)
        cur_stoch_d = last(stoch_d)

        # 直近1日のOHLCからピボットポイント計算
        last_high = float(high.iloc[-1])
        last_low  = float(low.iloc[-1])
        last_cls  = float(close.iloc[-1])
        pivot = (last_high + last_low + last_cls) / 3
        r1 = 2*pivot - last_low
        r2 = pivot + (last_high - last_low)
        r3 = last_high + 2*(pivot - last_low)
        s1 = 2*pivot - last_high
        s2 = pivot - (last_high - last_low)
        s3 = last_low - 2*(last_high - pivot)

        # 直近30日のスイングハイ・ロー（サポレジ）
        recent = hist.tail(30)
        swing_high = float(recent['High'].max())
        swing_low  = float(recent['Low'].min())

        # 平均日次レンジ（pips相当）
        is_jpy = symbol.endswith('JPY=X')
        pip_unit = 0.01 if is_jpy else 0.0001
        avg_daily_range_price = float((high - low).tail(20).mean())
        avg_daily_range_pips = avg_daily_range_price / pip_unit
        atr_pips = (cur_atr / pip_unit) if cur_atr else None

        # ATRシグナル
        atr_pct = (cur_atr / cur_close * 100) if cur_atr and cur_close else 0
        if atr_pct > 1.0: atr_sig, atr_desc = 'sell', f'ATR {cur_atr:.4f}（{atr_pct:.2f}%）— ボラティリティ高、注意'
        elif atr_pct > 0.6: atr_sig, atr_desc = 'weak_sell', f'ATR {cur_atr:.4f}（{atr_pct:.2f}%）— やや高ボラ'
        elif atr_pct > 0.3: atr_sig, atr_desc = 'neutral', f'ATR {cur_atr:.4f}（{atr_pct:.2f}%）— 標準的なボラ'
        else: atr_sig, atr_desc = 'weak_buy', f'ATR {cur_atr:.4f}（{atr_pct:.2f}%）— 低ボラ・レンジ気味'

        # ADXシグナル（トレンド強度）
        if cur_adx is None:
            adx_sig, adx_desc = 'neutral', 'データ不足'
        elif cur_adx > 40: adx_sig, adx_desc = 'buy' if cur_ma5 and cur_ma25 and cur_ma5 > cur_ma25 else 'sell', f'ADX {cur_adx:.1f} — 強いトレンド発生中'
        elif cur_adx > 25: adx_sig, adx_desc = 'weak_buy' if cur_ma5 and cur_ma25 and cur_ma5 > cur_ma25 else 'weak_sell', f'ADX {cur_adx:.1f} — トレンドあり'
        else: adx_sig, adx_desc = 'neutral', f'ADX {cur_adx:.1f} — トレンドなし（レンジ相場）'

        # Stochasticシグナル
        if cur_stoch_k is None:
            stoch_sig, stoch_desc = 'neutral', 'データ不足'
        elif cur_stoch_k < 20: stoch_sig, stoch_desc = 'buy', f'Stoch %K {cur_stoch_k:.1f} — 売られすぎ圏'
        elif cur_stoch_k > 80: stoch_sig, stoch_desc = 'sell', f'Stoch %K {cur_stoch_k:.1f} — 買われすぎ圏'
        else: stoch_sig, stoch_desc = 'neutral', f'Stoch %K {cur_stoch_k:.1f} — 中立圏'

        # チャートデータ（直近90日）
        tail = hist.tail(90)
        bb_u_t = bb_upper.tail(90); bb_l_t = bb_lower.tail(90); bb_m_t = bb_mid.tail(90)
        ma25_t = ma25.tail(90); ma75_t = ma75.tail(90)

        def sv(s, idx):
            try:
                v = s.iloc[idx]
                return None if np.isnan(v) else round(float(v), 4)
            except: return None

        chart_data = []
        for i in range(len(tail)):
            chart_data.append({
                'date': tail.index[i].strftime('%Y-%m-%d'),
                'open': round(float(tail['Open'].iloc[i]), 4),
                'high': round(float(tail['High'].iloc[i]), 4),
                'low':  round(float(tail['Low'].iloc[i]),  4),
                'close':round(float(tail['Close'].iloc[i]),4),
                'volume': 0,  # FXは出来高なし
                'bb_upper': sv(bb_u_t, i),
                'bb_lower': sv(bb_l_t, i),
                'bb_mid':   sv(bb_m_t, i),
                'ma25':     sv(ma25_t, i),
                'ma75':     sv(ma75_t, i),
            })
        rsi_t = rsi_series.tail(90)
        rsi_chart = [{'date': rsi_t.index[i].strftime('%Y-%m-%d'),
                      'value': sv(rsi_t, i)} for i in range(len(rsi_t))]
        macd_t = macd_line.tail(90); msig_t = macd_signal.tail(90); mhst_t = macd_hist.tail(90)
        macd_chart = [{'date': macd_t.index[i].strftime('%Y-%m-%d'),
                       'macd': sv(macd_t, i), 'signal': sv(msig_t, i), 'hist': sv(mhst_t, i)}
                      for i in range(len(macd_t))]

        meta = get_forex_meta(symbol)
        digits = 2 if any(symbol.startswith(p) for p in ('USDJPY','EURJPY','GBPJPY','AUDJPY','NZDJPY','CADJPY','CHFJPY','TRYJPY','ZARJPY','MXNJPY')) else 4

        return jsonify({
            'symbol': symbol,
            'pair_name': meta['name'],
            'pair_desc': meta['desc'],
            'price': round(cur_close, digits),
            'price_change': round(price_change, 2),
            'verdict': verdict,
            'score': score,
            'digits': digits,
            'indicators': {
                'rsi':  {'value': round(cur_rsi, 1) if cur_rsi else None, 'signal': rsi_sig, 'desc': rsi_desc},
                'macd': {'value': round(cur_macd, 4) if cur_macd else None, 'signal': macd_sig, 'desc': macd_desc},
                'bollinger': {'signal': bb_sig, 'desc': bb_desc,
                              'upper': round(cur_bb_upper, digits) if cur_bb_upper else None,
                              'lower': round(cur_bb_lower, digits) if cur_bb_lower else None},
                'ma': {'ma5': round(cur_ma5, digits) if cur_ma5 else None,
                       'ma25': round(cur_ma25, digits) if cur_ma25 else None,
                       'ma75': round(cur_ma75, digits) if cur_ma75 else None,
                       'signal': ma_sig, 'desc': ma_desc},
                'atr':   {'value': round(cur_atr, digits) if cur_atr else None, 'pips': round(atr_pips,1) if atr_pips else None, 'pct': round(atr_pct,2), 'signal': atr_sig, 'desc': atr_desc},
                'adx':   {'value': round(cur_adx, 1) if cur_adx else None, 'signal': adx_sig, 'desc': adx_desc},
                'stoch': {'k': round(cur_stoch_k, 1) if cur_stoch_k else None, 'd': round(cur_stoch_d, 1) if cur_stoch_d else None, 'signal': stoch_sig, 'desc': stoch_desc},
            },
            'pivot_points': {
                'pivot': round(pivot, digits),
                'r1': round(r1, digits), 'r2': round(r2, digits), 'r3': round(r3, digits),
                's1': round(s1, digits), 's2': round(s2, digits), 's3': round(s3, digits),
            },
            'support_resistance': {
                'recent_high_30d': round(swing_high, digits),
                'recent_low_30d':  round(swing_low,  digits),
                'distance_to_high_pips': round((swing_high - cur_close) / pip_unit, 1),
                'distance_to_low_pips':  round((cur_close - swing_low)  / pip_unit, 1),
            },
            'volatility': {
                'atr_pips': round(atr_pips, 1) if atr_pips else None,
                'avg_daily_range_pips': round(avg_daily_range_pips, 1),
                'pip_unit': pip_unit,
            },
            'week52_high': round(week52_high, digits) if week52_high else None,
            'week52_low':  round(week52_low,  digits) if week52_low  else None,
            'chart': chart_data,
            'rsi_chart': rsi_chart,
            'macd_chart': macd_chart,
            'updated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        })
    except Exception as e:
        return jsonify({'error': f'分析失敗: {e}'}), 500


@app.route('/api/forex/chart', methods=['POST'])
def forex_chart():
    data = request.get_json()
    symbol = normalize_forex_symbol(data.get('symbol') or '')
    period = data.get('period', '6mo')
    PERIOD_MAP = {
        '1mo':  ('3mo','1d',22), '3mo':  ('6mo','1d',65),
        '6mo':  ('1y','1d',130), '1y':   ('2y','1d',252),
        '2y':   ('3y','1wk',104),'5y':   ('5y','1wk',260),
    }
    fp, iv, n = PERIOD_MAP.get(period, ('1y','1d',252))
    try:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period=fp, interval=iv)
        if hist.empty: return jsonify({'error':'データなし'}), 404
        close = hist['Close']
        bb_o = ta.volatility.BollingerBands(close, window=20)
        bb_u, bb_l, bb_m = bb_o.bollinger_hband(), bb_o.bollinger_lband(), bb_o.bollinger_mavg()
        ma25 = close.rolling(25).mean(); ma75 = close.rolling(75).mean()
        rsi_s = ta.momentum.RSIIndicator(close, window=14).rsi()
        m_o = ta.trend.MACD(close)
        m_l, m_s, m_h = m_o.macd(), m_o.macd_signal(), m_o.macd_diff()
        tail = hist.tail(n)
        def sv(s, idx):
            try:
                v = s.iloc[idx]
                return None if np.isnan(v) else round(float(v), 4)
            except: return None
        chart = []
        for i in range(len(tail)):
            off = len(hist) - len(tail) + i
            chart.append({
                'date': tail.index[i].strftime('%Y-%m-%d'),
                'open': round(float(tail['Open'].iloc[i]), 4),
                'high': round(float(tail['High'].iloc[i]), 4),
                'low':  round(float(tail['Low'].iloc[i]),  4),
                'close':round(float(tail['Close'].iloc[i]),4),
                'volume': 0,
                'bb_upper': sv(bb_u, off), 'bb_lower': sv(bb_l, off), 'bb_mid': sv(bb_m, off),
                'ma25': sv(ma25, off), 'ma75': sv(ma75, off),
            })
        rsi_tail = rsi_s.tail(n)
        rsi_chart = [{'date': rsi_tail.index[i].strftime('%Y-%m-%d'),
                      'value': sv(rsi_tail, i)} for i in range(len(rsi_tail))]
        ml_t = m_l.tail(n); ms_t = m_s.tail(n); mh_t = m_h.tail(n)
        macd_chart = [{'date': ml_t.index[i].strftime('%Y-%m-%d'),
                       'macd': sv(ml_t, i), 'signal': sv(ms_t, i), 'hist': sv(mh_t, i)}
                      for i in range(len(ml_t))]
        return jsonify({'chart': chart, 'rsi_chart': rsi_chart, 'macd_chart': macd_chart})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


def generate_forex_deep_analysis(symbol, indicators, pivot, sr, volatility, verdict, score, week52_high, week52_low, current_price, digits):
    """為替用の詳細考察セクションを生成"""
    sections = []
    base = symbol.replace('=X','')
    c1, c2 = (base[:3], base[3:]) if len(base) == 6 else ('', '')
    cb1 = CENTRAL_BANK_RATES.get(c1, {})
    cb2 = CENTRAL_BANK_RATES.get(c2, {})
    rate_diff = (cb1.get('rate', 0) - cb2.get('rate', 0)) if (cb1 and cb2) else None
    meta = get_forex_meta(symbol)
    pip_unit = volatility.get('pip_unit', 0.0001)

    # ───── 1. 通貨ペアの特性 ─────
    s1 = f"<b>{meta['name']}（{base}）</b>は{meta.get('desc','')}"
    if cb1 and cb2:
        s1 += f"\n\n<b>{c1}</b>: {cb1.get('bank','')}が政策金利を <b>{cb1.get('rate')}%</b> に設定。{cb1.get('stance','')}"
        s1 += f"\n<b>{c2}</b>: {cb2.get('bank','')}が政策金利を <b>{cb2.get('rate')}%</b> に設定。{cb2.get('stance','')}"
    sections.append({'title': '📌 通貨ペアの特性', 'content': s1})

    # ───── 2. 金利差・キャリートレード ─────
    if rate_diff is not None:
        s2 = f"<b>{c1}金利 {cb1.get('rate')}% − {c2}金利 {cb2.get('rate')}% = 金利差 {rate_diff:+.2f}%</b>\n\n"
        if rate_diff > 3:
            s2 += f"📈 <b>{c1}買い／{c2}売り</b> 方向のキャリートレードが有利。長期保有でスワップポイント収入が期待できますが、為替変動リスクとの兼ね合いに注意。"
        elif rate_diff > 0:
            s2 += f"📊 {c1}の方が金利が高く、{base}買い方向にスワップが付きやすい状況。"
        elif rate_diff > -3:
            s2 += f"📊 {c2}の方が金利が高く、{base}売り方向にスワップが付きやすい状況。"
        else:
            s2 += f"📉 <b>{c2}買い／{c1}売り</b> 方向のキャリーが有利。{base}を売り持ちでスワップ受取が期待できます。"
        if cb1.get('stance') or cb2.get('stance'):
            s2 += f"\n\n<b>今後の見通し:</b> {c1}は{cb1.get('next','')}、{c2}は{cb2.get('next','')}。金融政策の方向性の違いがトレンドを生み出す主要因です。"
        sections.append({'title': '💰 金利差・キャリートレード環境', 'content': s2})

    # ───── 3. テクニカル総評 ─────
    s3_parts = []
    rsi = indicators.get('rsi', {})
    macd = indicators.get('macd', {})
    bb = indicators.get('bollinger', {})
    ma = indicators.get('ma', {})
    adx = indicators.get('adx', {})
    stoch = indicators.get('stoch', {})

    # 総合判定
    if score >= 70:    s3_parts.append(f"📈 <b>強い買いバイアス</b>（スコア {score}pt）。複数の指標が買いを示唆しています。")
    elif score >= 55:  s3_parts.append(f"📊 <b>やや買い優勢</b>（スコア {score}pt）。慎重な押し目買いが選択肢です。")
    elif score >= 45:  s3_parts.append(f"⚖️ <b>方向感なし</b>（スコア {score}pt）。明確なエントリーは難しい局面です。")
    elif score >= 30:  s3_parts.append(f"📉 <b>やや売り優勢</b>（スコア {score}pt）。戻り売りの選択肢があります。")
    else:              s3_parts.append(f"⚠️ <b>強い売りバイアス</b>（スコア {score}pt）。複数の指標が売りを示唆しています。")

    # トレンド強度（ADX）
    if adx.get('value'):
        if adx['value'] > 40:
            s3_parts.append(f"🔥 <b>ADX {adx['value']}</b> — 非常に強いトレンドが発生中。順張り戦略が機能しやすい局面です。")
        elif adx['value'] > 25:
            s3_parts.append(f"📐 <b>ADX {adx['value']}</b> — トレンドが形成されています。順張り中心で。")
        else:
            s3_parts.append(f"➡️ <b>ADX {adx['value']}</b> — トレンドなし（レンジ相場）。逆張り戦略が機能しやすい局面です。")

    # RSI/Stochの過熱感
    if rsi.get('value'):
        if rsi['value'] < 30: s3_parts.append(f"⚡ <b>RSI {rsi['value']}</b> — 売られすぎ圏。短期反発を期待した買いの選択肢。")
        elif rsi['value'] > 70: s3_parts.append(f"⚡ <b>RSI {rsi['value']}</b> — 買われすぎ圏。利確・短期売りの選択肢。")
    if stoch.get('k'):
        if stoch['k'] < 20: s3_parts.append(f"📊 <b>Stochastic %K {stoch['k']}</b> — 売られすぎ。反発の兆しを探したい局面。")
        elif stoch['k'] > 80: s3_parts.append(f"📊 <b>Stochastic %K {stoch['k']}</b> — 買われすぎ。反落の警戒が必要。")

    # MA配置
    if ma.get('ma5') and ma.get('ma25') and ma.get('ma75'):
        m5, m25, m75 = ma['ma5'], ma['ma25'], ma['ma75']
        if m5 > m25 > m75:
            s3_parts.append(f"📐 移動平均は <b>パーフェクトオーダー（上昇配列）</b>: MA5 > MA25 > MA75。強い上昇トレンドの典型形です。")
        elif m5 < m25 < m75:
            s3_parts.append(f"📐 移動平均は <b>パーフェクトオーダー（下降配列）</b>: MA5 < MA25 < MA75。強い下降トレンドの典型形です。")

    sections.append({'title': '📊 テクニカル分析の総評', 'content': '\n\n'.join(s3_parts)})

    # ───── 4. ボラティリティ・値動き分析 ─────
    s4 = ''
    atr_pips = volatility.get('atr_pips')
    avg_pips = volatility.get('avg_daily_range_pips')
    if atr_pips and avg_pips:
        s4 += f"<b>ATR（14日）: {atr_pips} pips</b> — 平均的な日次変動幅です。\n"
        s4 += f"<b>過去20日の平均レンジ: {avg_pips} pips</b>\n\n"
        if atr_pips > avg_pips * 1.3:
            s4 += "🔥 現在は<b>通常より高ボラティリティ</b>。値動きが激しいため、損切り幅を広めに設定しレバレッジを下げて取引するのが安全です。重要指標発表や地政学リスクの可能性。"
        elif atr_pips < avg_pips * 0.7:
            s4 += "💤 現在は<b>通常より低ボラティリティ</b>（凪相場）。レンジ取引が機能しやすいですが、ブレイクアウトが起きると一気に動く可能性があります。"
        else:
            s4 += "✅ ボラティリティは<b>標準的な水準</b>。通常のリスク管理で取引可能。"
        s4 += f"\n\n<b>推奨損切り幅:</b> ATRの1〜1.5倍（約 {round(atr_pips, 0)}〜{round(atr_pips*1.5, 0)} pips）が一般的な目安です。"
    sections.append({'title': '📈 ボラティリティ・値動き分析', 'content': s4 or 'データ不足'})

    # ───── 5. 主要な節目価格 ─────
    s5 = "現在価格: <b>" + f'{current_price:.{digits}f}' + "</b>\n\n"
    s5 += "<b>📊 ピボットポイント（デイトレ用節目）</b>\n"
    s5 += f"• R3 抵抗: {pivot['r3']:.{digits}f}\n"
    s5 += f"• R2 抵抗: {pivot['r2']:.{digits}f}\n"
    s5 += f"• R1 抵抗: {pivot['r1']:.{digits}f}\n"
    s5 += f"• <b>ピボット: {pivot['pivot']:.{digits}f}</b>（基準）\n"
    s5 += f"• S1 支持: {pivot['s1']:.{digits}f}\n"
    s5 += f"• S2 支持: {pivot['s2']:.{digits}f}\n"
    s5 += f"• S3 支持: {pivot['s3']:.{digits}f}\n\n"
    s5 += "<b>🎯 30日間の高値・安値（中期サポレジ）</b>\n"
    s5 += f"• 30日高値: <b>{sr['recent_high_30d']:.{digits}f}</b>（現在から +{sr['distance_to_high_pips']} pips）\n"
    s5 += f"• 30日安値: <b>{sr['recent_low_30d']:.{digits}f}</b>（現在から -{sr['distance_to_low_pips']} pips）\n\n"
    if week52_high and week52_low:
        s5 += f"<b>📍 52週レンジ</b>\n"
        s5 += f"• 52週高値: {week52_high:.{digits}f}\n"
        s5 += f"• 52週安値: {week52_low:.{digits}f}\n"
    sections.append({'title': '🎯 主要な節目価格・サポレジ', 'content': s5})

    # ───── 6. 取引シナリオ ─────
    s6_parts = []
    if score >= 60:
        # 買いシナリオ
        atr_p = atr_pips or 50
        entry = current_price
        sl = current_price - atr_p * 1.5 * pip_unit
        tp = current_price + atr_p * 2 * pip_unit
        s6_parts.append(f"📈 <b>買いシナリオ（順張り）</b>\n• エントリー: 現値 {entry:.{digits}f} 付近、または S1（{pivot['s1']:.{digits}f}）への押し目\n• 損切り: <b>{sl:.{digits}f}</b>（-{round(atr_p*1.5)} pips）\n• 利確: <b>{tp:.{digits}f}</b>（+{round(atr_p*2)} pips、リスクリワード 1:1.3）")
        s6_parts.append(f"⚠️ <b>シナリオ無効化条件</b>\n• MA25を明確に下抜け、または S2（{pivot['s2']:.{digits}f}）を割れたらトレンド転換の可能性大。即撤退検討。")
    elif score <= 40:
        # 売りシナリオ
        atr_p = atr_pips or 50
        entry = current_price
        sl = current_price + atr_p * 1.5 * pip_unit
        tp = current_price - atr_p * 2 * pip_unit
        s6_parts.append(f"📉 <b>売りシナリオ（順張り）</b>\n• エントリー: 現値 {entry:.{digits}f} 付近、または R1（{pivot['r1']:.{digits}f}）への戻り\n• 損切り: <b>{sl:.{digits}f}</b>（+{round(atr_p*1.5)} pips）\n• 利確: <b>{tp:.{digits}f}</b>（-{round(atr_p*2)} pips、リスクリワード 1:1.3）")
        s6_parts.append(f"⚠️ <b>シナリオ無効化条件</b>\n• MA25を明確に上抜け、または R2（{pivot['r2']:.{digits}f}）を超えたらトレンド転換の可能性大。即撤退検討。")
    else:
        # レンジシナリオ
        s6_parts.append(f"🔄 <b>レンジ取引シナリオ</b>\n• 30日安値 {sr['recent_low_30d']:.{digits}f} 付近で買い、30日高値 {sr['recent_high_30d']:.{digits}f} 付近で売り\n• ブレイクアウトしたらドテンも検討\n• ADXが上昇してトレンド転換のサインを警戒")
    s6_parts.append(f"💡 <b>ポジションサイズ目安</b>\n口座資金の1〜2%が損失上限になるよう、損切り幅から逆算してロット数を決めるのが基本です。")
    sections.append({'title': '🎯 取引シナリオと戦略', 'content': '\n\n'.join(s6_parts)})

    # ───── 7. 注意すべきリスク ─────
    s7 = []
    s7.append("<b>⚠️ 為替取引の主要リスク</b>")
    s7.append("• <b>金融政策の急変</b>: 中央銀行の利上げ・利下げ・QE変更で大きく動きます。")
    s7.append("• <b>為替介入</b>: 特に円ペアは日本財務省・日銀の介入リスクあり。")
    s7.append("• <b>地政学リスク</b>: 戦争・選挙・関税・経済制裁などで急変動。")
    s7.append("• <b>重要経済指標</b>: 米雇用統計（毎月第1金曜）、FOMC、CPI発表前後は要注意。")
    if 'JPY' in base:
        s7.append("• <b>円特有のリスク</b>: 日銀政策決定会合、財務省介入の可能性、リスクオン/オフでの円需給変化。")
    if c1 in ['TRY','ZAR','MXN'] or c2 in ['TRY','ZAR','MXN']:
        s7.append("• <b>新興国通貨リスク</b>: 流動性低下、急激な信用不安、政情不安定リスクが高め。")
    if rate_diff and abs(rate_diff) > 5:
        s7.append(f"• <b>金利差大</b>: 金利差 {rate_diff:+.2f}% は大きく、キャリートレード巻き戻しで急騰・急落のリスク。")
    sections.append({'title': '⚠️ 注意すべきリスク要因', 'content': '\n\n'.join(s7)})

    return sections


@app.route('/api/forex/deep_analysis', methods=['POST'])
def forex_deep_analysis():
    data = request.get_json()
    symbol = normalize_forex_symbol(data.get('symbol') or '')
    indicators = data.get('indicators', {})
    pivot      = data.get('pivot_points', {})
    sr         = data.get('support_resistance', {})
    volatility = data.get('volatility', {})
    verdict    = data.get('verdict', 'NEUTRAL')
    score      = data.get('score', 50)
    w52h       = data.get('week52_high')
    w52l       = data.get('week52_low')
    cur_price  = data.get('price', 0)
    digits     = data.get('digits', 4)
    try:
        sections = generate_forex_deep_analysis(symbol, indicators, pivot, sr, volatility, verdict, score, w52h, w52l, cur_price, digits)
        return jsonify({'sections': sections})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/forex/backtest', methods=['POST'])
def forex_backtest():
    data = request.get_json()
    raw_symbol = (data.get('symbol') or '').strip()
    if not raw_symbol:
        return jsonify({'error':'通貨ペアを指定してください'}), 400
    symbol = normalize_forex_symbol(raw_symbol)
    strategy = data.get('strategy', 'combined')
    period   = data.get('period', '2y')
    capital  = float(data.get('capital', 1000000))

    try:
        result = run_backtest(symbol, strategy, period, capital)
        if 'error' in result:
            return jsonify(result), 400
        # FX用に会社情報をペア情報に置き換え
        meta = get_forex_meta(symbol)
        result['company_name'] = meta['name']
        result['industry_desc'] = meta['desc']
        result['sector'] = '為替'
        result['market_cap'] = None
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': f'シミュレーション失敗: {e}'}), 500


# ============================================================
# 🤖 自動売買エンジン（仮想資金）
# ============================================================
import os as _os
import json as _json

AUTOTRADE_FILE = _os.path.join(_os.path.dirname(__file__), 'autotrade_state.json')

DEFAULT_AUTOTRADE = {
    'cash': 1_000_000,           # 現金残高
    'initial_capital': 1_000_000, # 初期資金
    'positions': {},              # {symbol: {shares, avg_price, buy_date, buy_reason}}
    'trade_history': [],          # 取引履歴
    'equity_curve': [],           # 日次資産推移
    'started_at': None,           # 開始日時
    'last_run_at': None,          # 最終実行日時
    'settings': {
        'max_positions': 5,        # 同時保有最大数
        'position_size_pct': 18,   # 1銘柄あたりの資金割合（%）
        'mode': 'mixed',           # スキャンモード
        'min_score_buy': 65,       # 買いシグナル発動最低スコア
        'max_score_sell': 35,      # 売りシグナル発動最高スコア
    }
}

def load_autotrade_state():
    if not _os.path.exists(AUTOTRADE_FILE):
        return dict(DEFAULT_AUTOTRADE)
    try:
        with open(AUTOTRADE_FILE, 'r', encoding='utf-8') as f:
            d = _json.load(f)
        # 不足キーを補完
        for k, v in DEFAULT_AUTOTRADE.items():
            if k not in d:
                d[k] = v
        return d
    except:
        return dict(DEFAULT_AUTOTRADE)

def save_autotrade_state(state):
    try:
        with open(AUTOTRADE_FILE, 'w', encoding='utf-8') as f:
            _json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f'autotrade save failed: {e}')


@app.route('/api/autotrade/status', methods=['GET'])
def autotrade_status():
    state = load_autotrade_state()
    # ポジションの現在価値を計算
    positions_with_value = []
    total_position_value = 0
    for sym, pos in state['positions'].items():
        try:
            t = yf.Ticker(sym)
            hist = t.history(period='5d')
            cur_price = float(hist['Close'].iloc[-1]) if not hist.empty else pos['avg_price']
        except:
            cur_price = pos['avg_price']
        value = cur_price * pos['shares']
        cost = pos['avg_price'] * pos['shares']
        pnl = value - cost
        pnl_pct = (pnl / cost * 100) if cost else 0
        total_position_value += value
        positions_with_value.append({
            'symbol': sym,
            'name': get_jp_company_name(sym, sym),
            'logo_url': f'https://assets.parqet.com/logos/symbol/{sym}?format=png',
            'shares': pos['shares'],
            'avg_price': pos['avg_price'],
            'cur_price': round(cur_price, 2),
            'cost': round(cost, 2),
            'value': round(value, 2),
            'pnl': round(pnl, 2),
            'pnl_pct': round(pnl_pct, 2),
            'buy_date': pos.get('buy_date'),
            'buy_reason': pos.get('buy_reason', ''),
            'holding_days': (datetime.now() - datetime.strptime(pos['buy_date'], '%Y-%m-%d')).days if pos.get('buy_date') else 0,
        })

    total_equity = state['cash'] + total_position_value
    initial = state['initial_capital']
    total_pnl = total_equity - initial
    total_pnl_pct = (total_pnl / initial * 100) if initial else 0

    # 勝率計算
    win_count = sum(1 for t in state['trade_history'] if t.get('profit', 0) > 0)
    total_trades = len([t for t in state['trade_history'] if t.get('action') == 'sell'])
    win_rate = (win_count / total_trades * 100) if total_trades else 0

    return jsonify({
        'cash': round(state['cash'], 2),
        'initial_capital': initial,
        'total_position_value': round(total_position_value, 2),
        'total_equity': round(total_equity, 2),
        'total_pnl': round(total_pnl, 2),
        'total_pnl_pct': round(total_pnl_pct, 2),
        'positions': positions_with_value,
        'trade_count': total_trades,
        'win_rate': round(win_rate, 1),
        'trade_history': state['trade_history'][-30:],  # 直近30件
        'equity_curve': state['equity_curve'][-90:],    # 直近90日
        'settings': state['settings'],
        'started_at': state.get('started_at'),
        'last_run_at': state.get('last_run_at'),
    })


@app.route('/api/autotrade/run', methods=['POST'])
def autotrade_run():
    """シグナル判定して自動売買を1回実行"""
    state = load_autotrade_state()
    if not state.get('started_at'):
        state['started_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    settings = state['settings']
    actions_log = []

    # ① 既存ポジションのチェック → 売りシグナル出てたら売却
    positions_to_remove = []
    for sym, pos in list(state['positions'].items()):
        try:
            r = quick_analyze(sym)
            if not r: continue
            cur_price = r['price']
            # 売りシグナル（スコア低い）or ストップロス（-7%）or 利益確定（+15%）
            cost = pos['avg_price'] * pos['shares']
            value = cur_price * pos['shares']
            pnl_pct = (value - cost) / cost * 100
            should_sell = False
            sell_reason = ''
            if r['score'] <= settings['max_score_sell']:
                should_sell = True
                sell_reason = f'売りシグナル発生（スコア {r["score"]}pt）: {r.get("comment","")}'
            elif pnl_pct <= -7:
                should_sell = True
                sell_reason = f'損切り発動（-7%超）: 含み損 {pnl_pct:.2f}%'
            elif pnl_pct >= 15:
                should_sell = True
                sell_reason = f'利益確定（+15%到達）: 含み益 +{pnl_pct:.2f}%'

            if should_sell:
                proceeds = cur_price * pos['shares']
                profit = proceeds - cost
                state['cash'] += proceeds
                state['trade_history'].append({
                    'action': 'sell',
                    'symbol': sym,
                    'name': get_jp_company_name(sym, sym),
                    'shares': pos['shares'],
                    'price': round(cur_price, 2),
                    'amount': round(proceeds, 2),
                    'profit': round(profit, 2),
                    'profit_pct': round(pnl_pct, 2),
                    'reason': sell_reason,
                    'date': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    'holding_days': (datetime.now() - datetime.strptime(pos['buy_date'], '%Y-%m-%d')).days,
                })
                positions_to_remove.append(sym)
                actions_log.append(f'🔴 売却: {sym} {pos["shares"]}株 @ {cur_price} → 損益 {profit:+.0f}円 ({pnl_pct:+.2f}%)')
        except Exception as e:
            actions_log.append(f'⚠️ {sym} エラー: {e}')

    for s in positions_to_remove:
        del state['positions'][s]

    # ② 新規買い候補を探す（スコア高い順）
    if len(state['positions']) < settings['max_positions']:
        # スキャン実行
        try:
            symbols_list = get_scan_symbols(settings['mode'])
            from concurrent.futures import ThreadPoolExecutor
            candidates = []
            with ThreadPoolExecutor(max_workers=8) as ex:
                futures = {ex.submit(quick_analyze, w['symbol']): w for w in symbols_list}
                for fut, w in futures.items():
                    r = fut.result()
                    if r and r['score'] >= settings['min_score_buy'] and r['symbol'] not in state['positions']:
                        candidates.append(r)
            candidates.sort(key=lambda x: x['score'], reverse=True)

            # 上位から買付
            for cand in candidates:
                if len(state['positions']) >= settings['max_positions']: break
                # 1銘柄あたりの予算
                budget = state['cash'] * settings['position_size_pct'] / 100
                if budget < cand['price']: continue  # 1株も買えない
                shares = int(budget / cand['price'])
                if shares <= 0: continue
                cost = shares * cand['price']
                if state['cash'] < cost: continue

                state['cash'] -= cost
                state['positions'][cand['symbol']] = {
                    'shares': shares,
                    'avg_price': cand['price'],
                    'buy_date': datetime.now().strftime('%Y-%m-%d'),
                    'buy_reason': f'スコア {cand["score"]}pt: {cand.get("comment","")}',
                }
                state['trade_history'].append({
                    'action': 'buy',
                    'symbol': cand['symbol'],
                    'name': get_jp_company_name(cand['symbol'], cand.get('name', cand['symbol'])),
                    'shares': shares,
                    'price': cand['price'],
                    'amount': round(cost, 2),
                    'reason': f'スコア {cand["score"]}pt: {cand.get("comment","")}',
                    'date': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                })
                actions_log.append(f'🟢 買付: {cand["symbol"]} {shares}株 @ {cand["price"]} ({cand["score"]}pt)')
        except Exception as e:
            actions_log.append(f'⚠️ スキャン失敗: {e}')

    # 資産推移を記録
    total_value = state['cash']
    for sym, pos in state['positions'].items():
        try:
            t = yf.Ticker(sym)
            hist = t.history(period='5d')
            if not hist.empty:
                total_value += float(hist['Close'].iloc[-1]) * pos['shares']
        except:
            total_value += pos['avg_price'] * pos['shares']

    today_str = datetime.now().strftime('%Y-%m-%d')
    # 同日エントリは更新
    if state['equity_curve'] and state['equity_curve'][-1]['date'] == today_str:
        state['equity_curve'][-1]['equity'] = round(total_value, 2)
    else:
        state['equity_curve'].append({'date': today_str, 'equity': round(total_value, 2)})

    state['last_run_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    save_autotrade_state(state)

    return jsonify({
        'success': True,
        'actions': actions_log,
        'positions_count': len(state['positions']),
        'cash': round(state['cash'], 2),
        'total_equity': round(total_value, 2),
        'message': f'{len(actions_log)}件のアクション実行' if actions_log else '今回はアクションなし（保有継続）',
    })


@app.route('/api/autotrade/reset', methods=['POST'])
def autotrade_reset():
    """リセット（最初からやり直す）"""
    data = request.get_json() or {}
    capital = float(data.get('capital', 1_000_000))
    new_state = dict(DEFAULT_AUTOTRADE)
    new_state['cash'] = capital
    new_state['initial_capital'] = capital
    new_state['positions'] = {}
    new_state['trade_history'] = []
    new_state['equity_curve'] = []
    new_state['started_at'] = None
    new_state['last_run_at'] = None
    save_autotrade_state(new_state)
    return jsonify({'success': True, 'capital': capital})


@app.route('/api/autotrade/settings', methods=['POST'])
def autotrade_settings():
    """設定変更"""
    data = request.get_json() or {}
    state = load_autotrade_state()
    for k in ('max_positions', 'position_size_pct', 'mode', 'min_score_buy', 'max_score_sell'):
        if k in data:
            state['settings'][k] = data[k]
    save_autotrade_state(state)
    return jsonify({'success': True, 'settings': state['settings']})


if __name__ == '__main__':
    import os
    port = int(os.environ.get('PORT', 5050))
    # threaded=True で同時複数リクエスト処理可能、落ちにくくなる
    app.run(debug=False, port=port, host='0.0.0.0', threaded=True, use_reloader=False)
