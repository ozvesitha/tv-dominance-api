#!/usr/bin/env python3
"""
OKX v5 Futures-only Data Fetcher — نسخه نهایی کامل (v5)
- فقط فیوچرز (بدون اسپات)
- ترتیب زمانی صعودی برای همه داده‌ها
- صفحه‌بندی صحیح کندل‌ها با before/after
- Taker Volume از اندپوینت قرارداد
- Top Trader L/S جداگانه
- nextFundingTime از funding-rate
- OI history محدود به 72 (سقف OKX)
- delta و delta_ratio تمام کندل‌ها با صفحه‌بندی عمیق history-trades
- همگام‌سازی واحد Open Interest (current در USD، history در USD)
- cvd_min و cvd_max واقعی از cvd_10s
"""

import os
import sys
import json
import time
import random
from datetime import datetime, timezone
from pathlib import Path
import requests

# ======================== تنظیمات ========================
TIMEFRAMES = ['1m', '5m', '15m', '1h', '4h', '1d']
DOMINANCE_TIMEFRAMES = ['1h', '4h']
CANDLE_LIMITS = {'1m': 240, '5m': 100, '15m': 200, '1h': 200, '4h': 100, '1d': 60}
DOMINANCE_LIMIT = 100
ORDERBOOK_LIMIT = 400
OI_5M_LIMIT = 72
OI_1H_LIMIT = 72
OI_1D_LIMIT = 15
FUNDING_LIMIT = 30

WHALE_THRESHOLD_LOW = 50000
WHALE_THRESHOLD = 100000
MEGA_WHALE_THRESHOLD = 1000000

EXCHANGE_TIMEOUT = 25
DOMINANCE_TIMEOUT = 45

DOMINANCE_API_URL = 'https://tv-dominance-api.onrender.com/api/dominance'
DOMINANCE_SYMBOLS = ['CRYPTOCAP:USDT.D']
OKX_BASE = 'https://www.okx.com'

OUTPUT_DIR = Path('data')

COMPACT_LIMITS = {
    'candles': {'1m': 120, '5m': 60, '15m': 200, '1h': 200, '4h': 60, '1d': 40},
    'oi': {'5m': 30, '1h': 30, '1d': 10},
    'funding': 15,
    'ls1h': 60,
    'ls15m': 30,
    'dominance': 50,
    'liquidations': 20
}

# ======================== توابع کمکی ========================
def log(msg: str):
    print(msg, flush=True)

def to_iso(ms: int) -> str:
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return f"{dt.strftime('%Y-%m-%dT%H:%M:%S')}.{ms % 1000:03d}Z"

def parse_iso(s: str) -> int:
    return int(datetime.fromisoformat(s.replace('Z', '+00:00')).timestamp() * 1000)

def safe_fetch(url: str, retries: int = 2):
    for i in range(retries + 1):
        try:
            res = requests.get(url, timeout=EXCHANGE_TIMEOUT)
            if res.status_code == 200:
                return res.json()
            if i == retries:
                return None
            time.sleep(0.4 * (i + 1))
        except Exception:
            if i == retries:
                return None
            time.sleep(0.4 * (i + 1))
    return None

def to_okx_symbol(symbol: str) -> str:
    base = symbol.replace('USDT', '')
    return f"{base}-USDT-SWAP"

# ======================== کندل‌ها ========================
def fetch_okx_candles(symbol: str, timeframe: str, custom_limit: int = None):
    okx_symbol = to_okx_symbol(symbol)
    bar_map = {'1m': '1m', '5m': '5m', '15m': '15m', '1h': '1H', '4h': '4H', '1d': '1D'}
    bar = bar_map[timeframe]
    limit = custom_limit if custom_limit is not None else CANDLE_LIMITS.get(timeframe, 200)

    all_candles = []
    first_url = f"{OKX_BASE}/api/v5/market/candles?instId={okx_symbol}&bar={bar}&limit=100"
    res = safe_fetch(first_url)
    if not res or res.get('code') != '0' or not res.get('data'):
        return []

    all_candles.extend(res['data'])

    while len(all_candles) < limit and res.get('data'):
        oldest_ts = int(res['data'][-1][0])
        next_url = f"{OKX_BASE}/api/v5/market/history-candles?instId={okx_symbol}&bar={bar}&limit=100&after={oldest_ts}"
        res = safe_fetch(next_url)
        if not res or res.get('code') != '0' or not res.get('data'):
            break
        all_candles.extend(res['data'])
        time.sleep(0.2)

    # OKX نزولی برمی‌گرداند → صعودی می‌کنیم (قدیمی‌ترین اول)
    all_candles = list(reversed(all_candles))

    out = []
    for c in all_candles:
        out.append({
            'time': to_iso(int(c[0])),
            'open': float(c[1]),
            'high': float(c[2]),
            'low': float(c[3]),
            'close': float(c[4]),
            'volume': float(c[5]),
            'quote_volume': float(c[6]),
            'trades': 0,
            'taker_buy_base': 0,
            'taker_buy_quote': 0,
            'delta': 0,
            'delta_ratio': 0
        })
    return out

# ======================== تریدها (با صفحه‌بندی عمیق و حذف تکراری) ========================
def fetch_okx_trades(symbol: str, target_limit: int = 50000):
    """
    جمع‌آوری تریدهای تاریخی با صفحه‌بندی عمیق.
    برای پر کردن دلتای ۱۲۰ کندل 1m به حدود ۵۰۰۰۰ ترید نیاز است.
    """
    okx_symbol = to_okx_symbol(symbol)
    all_trades = []
    seen_ids = set()
    after = None

    for _ in range(target_limit // 100):
        url = f"{OKX_BASE}/api/v5/market/history-trades?instId={okx_symbol}&limit=100"
        if after:
            url += f"&after={after}"
        res = safe_fetch(url)
        if not res or res.get('code') != '0' or not res['data']:
            break
        for t in res['data']:
            trade_id = t['tradeId']
            if trade_id in seen_ids:
                continue
            seen_ids.add(trade_id)
            all_trades.append({
                'time': to_iso(int(t['ts'])),
                'price': float(t['px']),
                'amount': float(t['sz']),
                'quote': float(t['px']) * float(t['sz']),
                'side': t['side']
            })
        after = res['data'][-1]['tradeId']
        time.sleep(0.1)

    # صعودی مرتب می‌کنیم (قدیمی‌ترین اول)
    all_trades.sort(key=lambda x: parse_iso(x['time']))

    whales = [t for t in all_trades if t['quote'] >= WHALE_THRESHOLD_LOW]
    return all_trades, whales

# ======================== عمق بازار ========================
def fetch_okx_orderbook(symbol: str):
    okx_symbol = to_okx_symbol(symbol)
    url = f"{OKX_BASE}/api/v5/market/books?instId={okx_symbol}&sz={ORDERBOOK_LIMIT}"
    data = safe_fetch(url)
    if not data or data.get('code') != '0' or not data.get('data'):
        return None
    book = data['data'][0]
    return {
        'bids': [[float(b[0]), float(b[1])] for b in book['bids']],
        'asks': [[float(a[0]), float(a[1])] for a in book['asks']],
        'lastUpdateId': book['ts']
    }

# ======================== Open Interest (همگام‌سازی واحد) ========================
def fetch_okx_open_interest_current(symbol: str, mark_price: float = None):
    """
    OI فعلی را به دو صورت برمی‌گرداند:
    - oi_contracts: تعداد قرارداد (طبق مستندات OKX)
    - oi_usd: ارزش دلاری (اگر mark_price موجود باشد)
    برای همگام‌سازی با تاریخچه (که USD است)، از oi_usd استفاده می‌کنیم.
    """
    okx_symbol = to_okx_symbol(symbol)
    url = f"{OKX_BASE}/api/v5/public/open-interest?instId={okx_symbol}"
    data = safe_fetch(url)
    if not data or data.get('code') != '0' or not data.get('data'):
        return None
    d = data['data'][0]
    oi_contracts = float(d['oi'])
    oi_usd = oi_contracts * mark_price if mark_price else None
    return {
        'oi_contracts': oi_contracts,
        'oi_usd': oi_usd,
        'time': to_iso(int(d['ts']))
    }

def fetch_okx_open_interest_hist(symbol: str, period: str, limit: int):
    """
    تاریخچه OI از rubik/stat/contracts/open-interest-volume
    این اندپوینت oi را به صورت USD ارزش برمی‌گرداند.
    """
    base = symbol.replace('USDT', '')
    period_map = {'5m': '5m', '1h': '1H', '1d': '1D'}
    p = period_map[period]
    url = f"{OKX_BASE}/api/v5/rubik/stat/contracts/open-interest-volume?ccy={base}&period={p}"
    data = safe_fetch(url)
    if not data or data.get('code') != '0' or not data.get('data'):
        return []
    raw = data['data'][:limit]
    raw.reverse()  # صعودی
    return [{
        'time': to_iso(int(d[0])),
        'oi_usd': float(d[1]),  # واحد: USD (مطابق مستندات OKX)
        'oi_value': 0
    } for d in raw]

# ======================== Funding ========================
def fetch_okx_funding_history(symbol: str, limit: int):
    okx_symbol = to_okx_symbol(symbol)
    url = f"{OKX_BASE}/api/v5/public/funding-rate-history?instId={okx_symbol}&limit={limit}"
    data = safe_fetch(url)
    if not data or data.get('code') != '0' or not data.get('data'):
        return []
    raw = data['data']
    raw.reverse()  # صعودی
    return [{
        'time': to_iso(int(d['fundingTime'])),
        'rate': float(d['fundingRate'])
    } for d in raw]

def fetch_okx_funding_current(symbol: str):
    okx_symbol = to_okx_symbol(symbol)
    url = f"{OKX_BASE}/api/v5/public/funding-rate?instId={okx_symbol}"
    data = safe_fetch(url)
    if not data or data.get('code') != '0' or not data.get('data'):
        return None
    d = data['data'][0]
    return {
        'current': float(d['fundingRate']),
        'next_funding_time': to_iso(int(d['nextFundingTime'])) if d.get('nextFundingTime') else None,
        'prev_funding_time': to_iso(int(d['prevFundingTime'])) if d.get('prevFundingTime') else None
    }

# ======================== Long/Short (Global + Top Trader جدا) ========================
def _parse_ls_data(raw_data, period):
    res = []
    for d in raw_data:
        ratio = float(d[1])
        short = 1 / (1 + ratio) if ratio > 0 else 0
        long_ = ratio / (1 + ratio) if ratio > 0 else 0
        res.append({'time': to_iso(int(d[0])), 'long': long_, 'short': short, 'ratio': ratio})
    res.reverse()  # صعودی

    if period == '15m':
        agg = []
        for i in range(0, len(res), 3):
            chunk = res[i:i+3]
            if len(chunk) == 3:
                agg.append({
                    'time': chunk[0]['time'],
                    'long': sum(c['long'] for c in chunk) / 3,
                    'short': sum(c['short'] for c in chunk) / 3,
                    'ratio': sum(c['ratio'] for c in chunk) / 3,
                })
        return agg
    return res

def fetch_okx_global_ls(symbol: str, period: str, limit: int):
    base = symbol.replace('USDT', '')
    period_map = {'5m': '5m', '15m': '5m', '1h': '1H'}
    p = period_map[period]
    url = f"{OKX_BASE}/api/v5/rubik/stat/contracts/long-short-account-ratio?ccy={base}&period={p}"
    data = safe_fetch(url)
    if not data or data.get('code') != '0' or not data.get('data'):
        return []
    res = _parse_ls_data(data['data'], period)
    return res[-limit:] if limit else res

def fetch_okx_top_trader_ls(symbol: str, period: str, limit: int):
    okx_symbol = to_okx_symbol(symbol)
    period_map = {'5m': '5m', '15m': '5m', '1h': '1H'}
    p = period_map[period]
    url = (f"{OKX_BASE}/api/v5/rubik/stat/contracts/"
           f"long-short-account-ratio-contract-top-trader?instId={okx_symbol}&period={p}")
    data = safe_fetch(url)
    if not data or data.get('code') != '0' or not data.get('data'):
        return []
    res = _parse_ls_data(data['data'], period)
    return res[-limit:] if limit else res

# ======================== Taker Volume ========================
def fetch_okx_taker_volume(symbol: str, period: str, limit: int):
    base = symbol.replace('USDT', '')
    period_map = {'5m': '5m', '15m': '5m', '1h': '1H'}
    p = period_map[period]
    url = (f"{OKX_BASE}/api/v5/rubik/stat/taker-volume"
           f"?ccy={base}&instType=CONTRACTS&period={p}")
    data = safe_fetch(url)
    if not data or data.get('code') != '0' or not data.get('data'):
        return []
    res = []
    for d in data['data']:
        sell = float(d[1])
        buy = float(d[2])
        res.append({
            'time': to_iso(int(d[0])),
            'buy_vol': buy,
            'sell_vol': sell,
            'ratio': buy / sell if sell > 0 else 0
        })
    res.reverse()  # صعودی

    if period == '15m':
        agg = []
        for i in range(0, len(res), 3):
            chunk = res[i:i+3]
            if len(chunk) == 3:
                b = sum(c['buy_vol'] for c in chunk)
                s = sum(c['sell_vol'] for c in chunk)
                agg.append({
                    'time': chunk[0]['time'],
                    'buy_vol': b, 'sell_vol': s,
                    'ratio': b / s if s > 0 else 0
                })
        return agg[-limit:]
    return res[-limit:]

# ======================== Ticker / Mark / Index ========================
def fetch_okx_ticker(symbol: str):
    okx_symbol = to_okx_symbol(symbol)
    data = safe_fetch(f"{OKX_BASE}/api/v5/market/ticker?instId={okx_symbol}")
    return data['data'][0] if data and data.get('code') == '0' and data.get('data') else None

def fetch_okx_mark_price(symbol: str):
    okx_symbol = to_okx_symbol(symbol)
    data = safe_fetch(f"{OKX_BASE}/api/v5/public/mark-price?instId={okx_symbol}")
    return data['data'][0] if data and data.get('code') == '0' and data.get('data') else None

def fetch_okx_index_price(symbol: str):
    okx_symbol = to_okx_symbol(symbol)
    data = safe_fetch(f"{OKX_BASE}/api/v5/market/index-tickers?instId={okx_symbol.replace('-SWAP','')}")
    return data['data'][0] if data and data.get('code') == '0' and data.get('data') else None

# ======================== Dominance ========================
def fetch_dominance(symbol: str, timeframes: list, limit: int):
    url = f"{DOMINANCE_API_URL}?symbol={symbol}&timeframes={','.join(timeframes)}&limit={limit}"
    try:
        res = requests.get(url, timeout=DOMINANCE_TIMEOUT)
        if res.status_code == 200:
            return res.json()
        return None
    except:
        return None

# ======================== محاسبات ========================
def compute_cvd(trades, cvd_10s=None):
    if not trades:
        return None
    buy = sell = 0.0
    for t in trades:
        if t['side'] == 'buy':
            buy += t['quote']
        else:
            sell += t['quote']
    cvd = buy - sell
    cvd_min = cvd_max = cvd
    if cvd_10s:
        cvd_min = min(b['cvd_cum'] for b in cvd_10s)
        cvd_max = max(b['cvd_cum'] for b in cvd_10s)
    return {
        'total_buy_volume': buy,
        'total_sell_volume': sell,
        'net_delta': cvd,
        'cvd_final': cvd,
        'cvd_min': cvd_min,
        'cvd_max': cvd_max
    }

def aggregate_cvd_10s(trades, window_ms=10000):
    if not trades:
        return []
    buckets = {}
    cum = 0.0
    for t in trades:
        ts = parse_iso(t['time'])
        bs = (ts // window_ms) * window_ms
        if bs not in buckets:
            buckets[bs] = {'buy': 0.0, 'sell': 0.0, 'count': 0}
        if t['side'] == 'buy':
            buckets[bs]['buy'] += t['quote']
            cum += t['quote']
        else:
            buckets[bs]['sell'] += t['quote']
            cum -= t['quote']
        buckets[bs]['count'] += 1
        buckets[bs]['cvd_cum'] = cum

    out = []
    for bs in sorted(buckets.keys()):
        b = buckets[bs]
        out.append({
            'time': to_iso(bs),
            'buy': round(b['buy']),
            'sell': round(b['sell']),
            'net': round(b['buy'] - b['sell']),
            'cvd_cum': round(b['cvd_cum']),
            'count': b['count']
        })
    return out

def aggregate_buy_sell_1m(trades, window_ms=60000):
    if not trades:
        return []
    buckets = {}
    for t in trades:
        ts = parse_iso(t['time'])
        bs = (ts // window_ms) * window_ms
        if bs not in buckets:
            buckets[bs] = {'buy': 0.0, 'sell': 0.0, 'count': 0}
        if t['side'] == 'buy':
            buckets[bs]['buy'] += t['quote']
        else:
            buckets[bs]['sell'] += t['quote']
        buckets[bs]['count'] += 1
    out = []
    for bs in sorted(buckets.keys()):
        b = buckets[bs]
        out.append({
            'time': to_iso(bs),
            'buy': round(b['buy']),
            'sell': round(b['sell']),
            'net': round(b['buy'] - b['sell']),
            'count': b['count']
        })
    return out

def enrich_candles_with_delta(candles, trades):
    """delta و delta_ratio را از تریدهای واقعی برای هر کندل پر می‌کند."""
    if not candles or not trades:
        return candles
    if len(candles) < 2:
        return candles

    tf_ms = parse_iso(candles[1]['time']) - parse_iso(candles[0]['time'])

    buckets = {}
    for t in trades:
        ts = parse_iso(t['time'])
        b = (ts // tf_ms) * tf_ms
        if b not in buckets:
            buckets[b] = {'buy': 0.0, 'sell': 0.0}
        if t['side'] == 'buy':
            buckets[b]['buy'] += t['quote']
        else:
            buckets[b]['sell'] += t['quote']

    for c in candles:
        c_start = parse_iso(c['time'])
        b = buckets.get(c_start)
        if b:
            buy = b['buy']
            sell = b['sell']
            total = buy + sell
            c['taker_buy_quote'] = round(buy, 2)
            c['delta'] = round(buy - sell, 2)
            c['delta_ratio'] = round((buy - sell) / total, 4) if total > 0 else 0
    return candles

def extract_large_trades(trades, threshold=100000, limit=20):
    if not trades:
        return []
    big = [t for t in trades if t['quote'] >= threshold][-limit:]
    return [{'time': t['time'], 'price': t['price'], 'quote': round(t['quote']), 'side': t['side']} for t in big]

def classify_whales(trades, low, mid, mega):
    lw = [t for t in trades if t['quote'] >= low]
    mw = [t for t in trades if t['quote'] >= mid]
    gw = [t for t in trades if t['quote'] >= mega]
    return lw, mw, gw

def summarize_whales(lst):
    if not lst:
        return {'count': 0, 'buy_count': 0, 'sell_count': 0, 'buy_value': 0,
                'sell_value': 0, 'net_value': 0, 'trades': []}
    bv = sum(t['quote'] for t in lst if t['side'] == 'buy')
    sv = sum(t['quote'] for t in lst if t['side'] == 'sell')
    bc = sum(1 for t in lst if t['side'] == 'buy')
    sc = sum(1 for t in lst if t['side'] == 'sell')
    return {'count': len(lst), 'buy_count': bc, 'sell_count': sc,
            'buy_value': bv, 'sell_value': sv, 'net_value': bv - sv,
            'trades': lst[-50:]}

def compute_orderbook_multi(ob, mid):
    if not ob or not mid or not ob['bids'] or not ob['asks']:
        return None
    levels = [0.001, 0.005, 0.01, 0.02]
    res = {'mid_price': mid,
           'spread': ob['asks'][0][0] - ob['bids'][0][0],
           'best_bid': ob['bids'][0][0], 'best_ask': ob['asks'][0][0],
           'best_bid_qty': ob['bids'][0][1], 'best_ask_qty': ob['asks'][0][1],
           'levels': {}, 'bid_walls': [], 'ask_walls': []}
    for pct in levels:
        up = mid * (1 + pct)
        lo = mid * (1 - pct)
        bv = sum(p * q for p, q in ob['bids'] if p >= lo)
        av = sum(p * q for p, q in ob['asks'] if p <= up)
        bl = sum(1 for p, q in ob['bids'] if p >= lo)
        al = sum(1 for p, q in ob['asks'] if p <= up)
        res['levels'][f'pct_{pct}'] = {
            'bid_value': bv, 'ask_value': av,
            'imbalance': bv / (bv + av) if (bv + av) > 0 else None,
            'bid_levels': bl, 'ask_levels': al
        }
    u1 = mid * 1.01
    l1 = mid * 0.99
    for p, q in ob['bids']:
        if p >= l1 and p * q > 500000:
            res['bid_walls'].append([p, q, p * q])
    for p, q in ob['asks']:
        if p <= u1 and p * q > 500000:
            res['ask_walls'].append([p, q, p * q])
    res['bid_walls'].sort(key=lambda x: -x[2])
    res['ask_walls'].sort(key=lambda x: -x[2])
    res['bid_walls'] = res['bid_walls'][:20]
    res['ask_walls'] = res['ask_walls'][:20]
    return res

def compute_vwap(candles, n):
    if not candles:
        return None
    recent = candles[-n:]
    spv = sum(((c['high'] + c['low'] + c['close']) / 3) * c['volume'] for c in recent)
    sv = sum(c['volume'] for c in recent)
    return spv / sv if sv > 0 else None

def compute_atr(candles, period=14):
    if not candles or len(candles) < period + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]['high'], candles[i]['low'], candles[i - 1]['close']
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr = sum(trs[:period]) / period
    for i in range(period, len(trs)):
        atr = (atr * (period - 1) + trs[i]) / period
    last_close = candles[-1]['close']
    return {'atr': round(atr, 8),
            'atr_pct': round(atr / last_close * 100, 4) if last_close > 0 else None,
            'period': period}

def compute_pdh_pdl(candles_1d):
    if not candles_1d or len(candles_1d) < 2:
        return None
    prev = candles_1d[-2]
    cur = candles_1d[-1]
    return {'pdh': prev['high'], 'pdl': prev['low'], 'prev_close': prev['close'],
            'current_day_high': cur['high'], 'current_day_low': cur['low'],
            'current_day_open': cur['open']}

def compute_vp(candles, bins=50, value_area_pct=0.7):
    if not candles:
        return None
    mn = min(c['low'] for c in candles)
    mx = max(c['high'] for c in candles)
    if mn == mx:
        return None
    bs = (mx - mn) / bins
    profile = [0.0] * bins
    for c in candles:
        lb = max(0, int((c['low'] - mn) / bs))
        hb = min(bins - 1, int((c['high'] - mn) / bs))
        vpb = c['volume'] / max(1, hb - lb + 1)
        for i in range(lb, hb + 1):
            profile[i] += vpb

    poc_idx = profile.index(max(profile))
    poc = mn + (poc_idx + 0.5) * bs
    total = sum(profile)
    target = total * value_area_pct
    cum = profile[poc_idx]
    lo = hi = poc_idx
    while cum < target and (lo > 0 or hi < bins - 1):
        lv = profile[lo - 1] if lo > 0 else -1
        hv = profile[hi + 1] if hi < bins - 1 else -1
        if hv >= lv:
            hi += 1
            cum += profile[hi]
        else:
            lo -= 1
            cum += profile[lo]

    val = mn + lo * bs
    vah = mn + (hi + 1) * bs
    avg = total / bins
    hvn = [{'price': round(mn + (i + 0.5) * bs, 6), 'volume': profile[i]}
           for i in range(bins) if profile[i] > avg * 1.5]
    lvn = [{'price': round(mn + (i + 0.5) * bs, 6), 'volume': profile[i]}
           for i in range(bins) if 0 < profile[i] < avg * 0.3]
    hvn.sort(key=lambda x: -x['volume'])
    lvn.sort(key=lambda x: x['volume'])

    return {'poc': round(poc, 6), 'vah': round(vah, 6), 'val': round(val, 6),
            'total_volume': total,
            'price_range': {'min': mn, 'max': mx, 'bins': bins, 'bin_size': bs},
            'hvn': hvn[:12], 'lvn': lvn[:12]}

def detect_cascades(trades, min_quote=20000, window_ms=500, min_count=3):
    """تریدها صعودی هستند؛ window_ms بر حسب میلی‌ثانیه"""
    if not trades:
        return []
    out = []
    cur = None
    for t in trades:
        if t['quote'] < min_quote:
            continue
        ts = parse_iso(t['time'])
        if cur and cur['side'] == t['side'] and (ts - cur['_lastTs']) < window_ms:
            cur['total'] += t['quote']
            cur['count'] += 1
            cur['_lastTs'] = ts
            cur['lastPrice'] = t['price']
            cur['lastTime'] = t['time']
        else:
            if cur and cur['count'] >= min_count:
                out.append({k: v for k, v in cur.items() if not k.startswith('_')})
            cur = {'startTime': t['time'], 'lastTime': t['time'], 'side': t['side'],
                   'count': 1, 'total': t['quote'],
                   'firstPrice': t['price'], 'lastPrice': t['price'], '_lastTs': ts}
    if cur and cur['count'] >= min_count:
        out.append({k: v for k, v in cur.items() if not k.startswith('_')})
    return out[-50:]

# ======================== Main ========================
def fetch_all_data(symbol: str):
    symbol = symbol.strip().upper()
    if not symbol:
        raise ValueError("Symbol required")

    log(f"🔄 [1/8] کندل‌ها برای {symbol}...")
    timeframe_data = {}
    for tf in TIMEFRAMES:
        try:
            candles = fetch_okx_candles(symbol, tf)
            timeframe_data[tf] = {'candles': candles}
            if candles:
                log(f"  {tf}: {len(candles)} کندل | از {candles[0]['time']} تا {candles[-1]['time']}")
            else:
                log(f"  {tf}: خالی")
        except Exception as e:
            timeframe_data[tf] = {'candles': [], 'error': str(e)}
            log(f"  {tf} خطا: {e}")

    log(f"📡 [2/8] تریدها (50000)...")
    all_trades, whale_trades = fetch_okx_trades(symbol, 50000)
    log(f"  کل: {len(all_trades)} | وال: {len(whale_trades)}")

    log(f"📡 [3/8] عمق بازار...")
    orderbook = fetch_okx_orderbook(symbol)

    log(f"📡 [4/8] Open Interest...")
    # ابتدا قیمت mark را می‌گیریم تا OI را به USD تبدیل کنیم
    mark_price = fetch_okx_mark_price(symbol)
    mark_px = float(mark_price['markPx']) if mark_price else None
    oi_current = fetch_okx_open_interest_current(symbol, mark_px)
    oi_hist_5m = fetch_okx_open_interest_hist(symbol, '5m', OI_5M_LIMIT)
    oi_hist_1h = fetch_okx_open_interest_hist(symbol, '1h', OI_1H_LIMIT)
    oi_hist_1d = fetch_okx_open_interest_hist(symbol, '1d', OI_1D_LIMIT)

    log(f"📡 [5/8] Funding / L-S / Taker...")
    funding_hist = fetch_okx_funding_history(symbol, FUNDING_LIMIT)
    funding_cur = fetch_okx_funding_current(symbol)
    global_ls_1h = fetch_okx_global_ls(symbol, '1h', 60)
    global_ls_15m = fetch_okx_global_ls(symbol, '15m', 30)
    top_ls_1h = fetch_okx_top_trader_ls(symbol, '1h', 60)
    top_ls_15m = fetch_okx_top_trader_ls(symbol, '15m', 30)
    tk_1h = fetch_okx_taker_volume(symbol, '1h', 60)
    tk_15m = fetch_okx_taker_volume(symbol, '15m', 30)

    log(f"📡 [6/8] Ticker / Mark / Index...")
    ticker24h = fetch_okx_ticker(symbol)
    index_price = fetch_okx_index_price(symbol)

    log(f"🧮 [7/8] محاسبات: CVD / Delta / Depth / VP / Cascade...")
    cvd10s = aggregate_cvd_10s(all_trades)
    bs1m = aggregate_buy_sell_1m(all_trades)
    cvd = compute_cvd(all_trades, cvd10s)
    large = extract_large_trades(whale_trades, WHALE_THRESHOLD, 20)
    lw, mw, gw = classify_whales(whale_trades, WHALE_THRESHOLD_LOW, WHALE_THRESHOLD, MEGA_WHALE_THRESHOLD)

    # delta برای کندل‌ها از تریدهای واقعی
    for tf in ['1m', '5m', '15m']:
        timeframe_data[tf]['candles'] = enrich_candles_with_delta(
            timeframe_data[tf]['candles'], all_trades
        )

    mid = None
    if orderbook and orderbook['bids'] and orderbook['asks']:
        mid = (orderbook['bids'][0][0] + orderbook['asks'][0][0]) / 2
    ob_multi = compute_orderbook_multi(orderbook, mid)
    vwap = compute_vwap(timeframe_data['15m']['candles'], 96)
    atr = {tf: compute_atr(timeframe_data[tf]['candles'], 14) for tf in TIMEFRAMES}
    pdh_pdl = compute_pdh_pdl(timeframe_data['1d']['candles'])
    vp5m = compute_vp(timeframe_data['5m']['candles'])
    vp15m = compute_vp(timeframe_data['15m']['candles'])
    cascades = detect_cascades(all_trades)

    log(f"🔄 [8/8] USDT.D...")
    dom_data = fetch_dominance('CRYPTOCAP:USDT.D', DOMINANCE_TIMEFRAMES, DOMINANCE_LIMIT)
    dominance = {'CRYPTOCAP:USDT.D': dom_data['data'] if dom_data and not dom_data.get('error') else {'error': 'Failed'}}

    premium = None
    if mark_price:
        premium = {
            'mark_price': float(mark_price['markPx']),
            'index_price': float(index_price['idxPx']) if index_price and index_price.get('idxPx') else float(mark_price['markPx']),
            'last_funding_rate': funding_cur['current'] if funding_cur else (funding_hist[-1]['rate'] if funding_hist else None),
            'next_funding_time': funding_cur['next_funding_time'] if funding_cur else None
        }

    output = {
        'metadata': {
            'symbol': symbol,
            'exchange': 'okx_swap',
            'timestamp': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z',
            'timeframes': TIMEFRAMES,
            'dominance_symbols': DOMINANCE_SYMBOLS,
            'whale_low_threshold_usd': WHALE_THRESHOLD_LOW,
            'whale_threshold_usd': WHALE_THRESHOLD,
            'mega_whale_threshold_usd': MEGA_WHALE_THRESHOLD,
            'version': 'okx_v5_fixed'
        },
        'data': {
            'coin': {
                'symbol': symbol,
                'timeframes': timeframe_data,
                'extras': {
                    'cvd_10s': cvd10s,
                    'buy_sell_1m': bs1m,
                    'large_trades_last': large,
                    'whale_trades_low': summarize_whales(lw),
                    'whale_trades': summarize_whales(mw),
                    'mega_whale_trades': summarize_whales(gw),
                    'orderbook': {'bids': orderbook['bids'][:ORDERBOOK_LIMIT],
                                  'asks': orderbook['asks'][:ORDERBOOK_LIMIT],
                                  'lastUpdateId': orderbook['lastUpdateId']} if orderbook else None,
                    'orderbook_depth_multi': ob_multi,
                    'cvd': cvd,
                    'vwap_24h': vwap,
                    'atr': atr,
                    'pdh_pdl': pdh_pdl,
                    'volume_profile_5m': vp5m,
                    'volume_profile_15m': vp15m,
                    'liquidation_cascades': cascades,
                    'derivatives': {
                        'open_interest': {
                            'current': oi_current,
                            'history_5m': oi_hist_5m,
                            'history_1h': oi_hist_1h,
                            'history_1d': oi_hist_1d
                        },
                        'funding_rate': {
                            'current': funding_cur['current'] if funding_cur else (funding_hist[-1]['rate'] if funding_hist else None),
                            'next_funding_time': funding_cur['next_funding_time'] if funding_cur else None,
                            'history': funding_hist
                        },
                        'global_long_short': {'1h': global_ls_1h, '15m': global_ls_15m},
                        'top_trader_long_short': {'1h': top_ls_1h, '15m': top_ls_15m},
                        'taker_volume': {'1h': tk_1h, '15m': tk_15m}
                    },
                    'ticker_24h': {
                        'price_change': str(float(ticker24h['last']) - float(ticker24h['open24h'])),
                        'price_change_pct': str((float(ticker24h['last']) - float(ticker24h['open24h'])) / float(ticker24h['open24h']) * 100),
                        'high': float(ticker24h['high24h']),
                        'low': float(ticker24h['low24h']),
                        'volume': float(ticker24h['vol24h']),
                        'quote_volume': float(ticker24h['volCcy24h'])
                    } if ticker24h else None,
                    'premium_index': premium
                }
            },
            'dominance': dominance
        }
    }
    return output

def build_compact_output(full):
    out = json.loads(json.dumps(full))
    lim = COMPACT_LIMITS

    for tf, td in out['data']['coin']['timeframes'].items():
        if isinstance(td.get('candles'), list):
            td['candles'] = td['candles'][-lim['candles'].get(tf, 100):]

    ex = out['data']['coin']['extras']
    if ex.get('orderbook'):
        ex['orderbook'] = {'lastUpdateId': ex['orderbook'].get('lastUpdateId'),
                           'raw_removed': True, 'note': 'Use orderbook_depth_multi'}

    d = ex.get('derivatives', {})
    oi = d.get('open_interest', {})
    for k, l in [('history_5m', '5m'), ('history_1h', '1h'), ('history_1d', '1d')]:
        if isinstance(oi.get(k), list):
            oi[k] = oi[k][-lim['oi'][l]:]
    if isinstance(d.get('funding_rate', {}).get('history'), list):
        d['funding_rate']['history'] = d['funding_rate']['history'][-lim['funding']:]

    for key in ['global_long_short', 'top_trader_long_short', 'taker_volume']:
        v = d.get(key)
        if isinstance(v, dict):
            if isinstance(v.get('1h'), list):
                v['1h'] = v['1h'][-lim['ls1h']:]
            if isinstance(v.get('15m'), list):
                v['15m'] = v['15m'][-lim['ls15m']:]

    dom = out['data'].get('dominance', {})
    for sym, v in dom.items():
        if isinstance(v, list):
            dom[sym] = v[-lim['dominance']:]
        elif isinstance(v, dict) and 'error' not in v:
            for tf, arr in list(v.items()):
                if isinstance(arr, list):
                    v[tf] = arr[-lim['dominance']:]

    if isinstance(ex.get('liquidation_cascades'), list):
        ex['liquidation_cascades'] = ex['liquidation_cascades'][-lim['liquidations']:]

    out['metadata']['compact'] = True
    out['metadata']['compact_limits'] = lim
    return out

if __name__ == '__main__':
    symbol = os.environ.get('SYMBOL', 'SOLUSDT').strip().upper()
    log(f"=== OKX Fetcher v5 (نهایی) | {symbol} ===")
    try:
        full_data = fetch_all_data(symbol)
        compact_data = build_compact_output(full_data)
        OUTPUT_DIR.mkdir(exist_ok=True)
        out_path = OUTPUT_DIR / f"{symbol}.json"
        with open(out_path, 'w', encoding='utf-8') as f:
            json.dump(compact_data, f, ensure_ascii=False, separators=(',', ':'))
        size_kb = out_path.stat().st_size / 1024
        log(f"\n✅ ذخیره شد: {out_path} ({size_kb:.1f} KB)")
        log(f"   تعداد کندل: 1m={len(compact_data['data']['coin']['timeframes']['1m']['candles'])} "
            f"| 15m={len(compact_data['data']['coin']['timeframes']['15m']['candles'])} "
            f"| 1h={len(compact_data['data']['coin']['timeframes']['1h']['candles'])} "
            f"| 4h={len(compact_data['data']['coin']['timeframes']['4h']['candles'])} "
            f"| 1d={len(compact_data['data']['coin']['timeframes']['1d']['candles'])}")
    except Exception as e:
        log(f"❌ خطا: {e}")
        sys.exit(1)
