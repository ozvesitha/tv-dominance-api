#!/usr/bin/env python3
"""
v6 Binance Futures Data Fetcher — GitHub Actions Edition
Port of HTML v6 logic to Python. Runs server-side (GitHub Actions), bypasses Iran filter.
Env: SYMBOL=SOLUSDT (default)
Output: data/{SYMBOL}.json  (compact)
"""

import os
import sys
import json
import time
import random
from datetime import datetime, timezone
from pathlib import Path

import requests

# ======================== CONFIG ========================
TIMEFRAMES = ['1m', '5m', '15m', '1h', '4h', '1d']
DOMINANCE_TIMEFRAMES = ['1h', '4h']
CANDLE_LIMITS = {'1m': 240, '5m': 100, '15m': 200, '1h': 200, '4h': 100, '1d': 60}
DOMINANCE_LIMIT = 100
AGG_TRADES_LIMIT = 1500
ORDERBOOK_LIMIT = 500
OI_5M_LIMIT = 60
OI_1H_LIMIT = 60
OI_1D_LIMIT = 15
FUNDING_LIMIT = 30
LS_LIMIT = 200
TAKER_LIMIT = 200
LS_15M_LIMIT = 50
TAKER_15M_LIMIT = 50

WHALE_THRESHOLD_LOW = 50000
WHALE_THRESHOLD = 100000
MEGA_WHALE_THRESHOLD = 1000000

EXCHANGE_TIMEOUT = 25
DOMINANCE_TIMEOUT = 45

DOMINANCE_API_URL = 'https://tv-dominance-api.onrender.com/api/dominance'
DOMINANCE_SYMBOLS = ['CRYPTOCAP:USDT.D']
#BINANCE_FAPI = 'https://fapi.binance.com'
#BINANCE_FAPI = 'https://binance-proxy.ozvesitha.workers.dev'
BINANCE_FAPI = 'https://fapi.binance.vision'

OUTPUT_DIR = Path('data')

COMPACT_LIMITS = {
    'candles': {'1m': 120, '5m': 60, '15m': 150, '1h': 200, '4h': 60, '1d': 40},
    'oi': {'5m': 30, '1h': 30, '1d': 10},
    'funding': 15,
    'ls1h': 60,
    'ls15m': 30,
    'dominance': 50,
    'liquidations': 20,
}


# ======================== HELPERS ========================
def to_iso(ms: int) -> str:
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return f"{dt.strftime('%Y-%m-%dT%H:%M:%S')}.{ms % 1000:03d}Z"


def log(msg: str):
    print(msg, flush=True)


def safe_fetch(url: str, timeout: int = EXCHANGE_TIMEOUT, retries: int = 2):
    for i in range(retries + 1):
        try:
            r = requests.get(url, timeout=timeout)
            if r.status_code == 200:
                return r.json()
            if i == retries:
                return None
            time.sleep(0.4 * (i + 1))
        except Exception:
            if i == retries:
                return None
            time.sleep(0.4 * (i + 1))
    return None


def jitter(base_ms: int, spread_ms: int) -> float:
    return (base_ms + random.randint(0, spread_ms)) / 1000.0


# ======================== BINANCE FETCHERS ========================
def fetch_candles(symbol: str, timeframe: str, custom_limit: int = None):
    limit = custom_limit if custom_limit is not None else CANDLE_LIMITS.get(timeframe, 200)
    url = f"{BINANCE_FAPI}/fapi/v1/klines?symbol={symbol}&interval={timeframe}&limit={limit}"
    try:
        r = requests.get(url, timeout=EXCHANGE_TIMEOUT)
        if r.status_code != 200:
            raise RuntimeError(f"klines HTTP {r.status_code}")
        data = r.json()
    except Exception as e:
        raise RuntimeError(f"klines failed: {e}")
    out = []
    for c in data:
        volume = float(c[5])
        tbb = float(c[9])
        sell = volume - tbb
        delta = tbb - sell
        out.append({
            'time': to_iso(int(c[0])),
            'open': float(c[1]),
            'high': float(c[2]),
            'low': float(c[3]),
            'close': float(c[4]),
            'volume': volume,
            'quote_volume': float(c[7]),
            'trades': int(c[8]),
            'taker_buy_base': tbb,
            'taker_buy_quote': float(c[10]),
            'delta': delta,
            'delta_ratio': delta / volume if volume > 0 else 0,
        })
    return out


def fetch_agg_trades(symbol: str):
    attempts = [(AGG_TRADES_LIMIT, 0.0), (1000, 1.2), (500, 2.5), (200, 4.0)]
    for limit, wait in attempts:
        if wait > 0:
            time.sleep(jitter(int(wait * 1000), 600))
        url = f"{BINANCE_FAPI}/fapi/v1/aggTrades?symbol={symbol}&limit={limit}"
        try:
            r = requests.get(url, timeout=EXCHANGE_TIMEOUT)
            if r.status_code != 200:
                continue
            data = r.json()
            if isinstance(data, list) and data:
                return [{
                    'time': to_iso(int(t['T'])),
                    'price': float(t['p']),
                    'amount': float(t['q']),
                    'quote': float(t['p']) * float(t['q']),
                    'side': 'sell' if t['m'] else 'buy',
                } for t in data]
        except Exception:
            continue
    return []


def fetch_orderbook(symbol: str):
    url = f"{BINANCE_FAPI}/fapi/v1/depth?symbol={symbol}&limit={ORDERBOOK_LIMIT}"
    data = safe_fetch(url)
    if not data or 'bids' not in data or 'asks' not in data:
        return None
    return {
        'bids': [[float(b[0]), float(b[1])] for b in data['bids']],
        'asks': [[float(a[0]), float(a[1])] for a in data['asks']],
        'lastUpdateId': data.get('lastUpdateId'),
    }


def fetch_oi_current(symbol: str):
    data = safe_fetch(f"{BINANCE_FAPI}/fapi/v1/openInterest?symbol={symbol}")
    if not data:
        return None
    return {'current': float(data['openInterest']), 'time': to_iso(int(data['time']))}


def fetch_oi_hist(symbol: str, period: str, limit: int):
    url = f"{BINANCE_FAPI}/futures/data/openInterestHist?symbol={symbol}&period={period}&limit={limit}"
    data = safe_fetch(url)
    if not isinstance(data, list):
        return []
    return [{
        'time': to_iso(int(d['timestamp'])),
        'oi': float(d['sumOpenInterest']),
        'oi_value': float(d['sumOpenInterestValue']),
    } for d in data]


def fetch_funding(symbol: str, limit: int):
    url = f"{BINANCE_FAPI}/fapi/v1/fundingRate?symbol={symbol}&limit={limit}"
    data = safe_fetch(url)
    if not isinstance(data, list):
        return []
    return [{'time': to_iso(int(d['fundingTime'])), 'rate': float(d['fundingRate'])} for d in data]


def fetch_global_ls(symbol: str, period: str, limit: int):
    url = f"{BINANCE_FAPI}/futures/data/globalLongShortAccountRatio?symbol={symbol}&period={period}&limit={limit}"
    data = safe_fetch(url)
    if not isinstance(data, list):
        return []
    return [{
        'time': to_iso(int(d['timestamp'])),
        'long': float(d['longAccount']),
        'short': float(d['shortAccount']),
        'ratio': float(d['longShortRatio']),
    } for d in data]


def fetch_top_ls(symbol: str, period: str, limit: int):
    url = f"{BINANCE_FAPI}/futures/data/topLongShortAccountRatio?symbol={symbol}&period={period}&limit={limit}"
    data = safe_fetch(url)
    if not isinstance(data, list):
        return []
    return [{
        'time': to_iso(int(d['timestamp'])),
        'long': float(d['longAccount']),
        'short': float(d['shortAccount']),
        'ratio': float(d['longShortRatio']),
    } for d in data]


def fetch_taker(symbol: str, period: str, limit: int):
    url = f"{BINANCE_FAPI}/futures/data/takerlongshortRatio?symbol={symbol}&period={period}&limit={limit}"
    data = safe_fetch(url)
    if not isinstance(data, list):
        return []
    return [{
        'time': to_iso(int(d['timestamp'])),
        'buy_vol': float(d['buyVol']),
        'sell_vol': float(d['sellVol']),
        'ratio': float(d['buySellRatio']),
    } for d in data]


def fetch_ticker(symbol: str):
    return safe_fetch(f"{BINANCE_FAPI}/fapi/v1/ticker/24hr?symbol={symbol}")


def fetch_premium(symbol: str):
    return safe_fetch(f"{BINANCE_FAPI}/fapi/v1/premiumIndex?symbol={symbol}")


def fetch_dominance():
    tf = ','.join(DOMINANCE_TIMEFRAMES)
    url = f"{DOMINANCE_API_URL}?symbol=CRYPTOCAP:USDT.D&timeframes={tf}&limit={DOMINANCE_LIMIT}"
    try:
        r = requests.get(url, timeout=DOMINANCE_TIMEOUT)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        return r.json()
    except Exception as e:
        log(f"Dominance error: {e}")
        return None


# ======================== COMPUTATIONS ========================
def compute_cvd(trades):
    if not trades:
        return None
    buy = sell = cvd = 0.0
    mn = mx = 0.0
    for t in trades:
        v = t['quote']
        if t['side'] == 'buy':
            buy += v; cvd += v
        else:
            sell += v; cvd -= v
        mn = min(mn, cvd); mx = max(mx, cvd)
    return {
        'total_buy_volume': buy,
        'total_sell_volume': sell,
        'net_delta': buy - sell,
        'cvd_final': cvd,
        'cvd_min': mn,
        'cvd_max': mx,
    }


def aggregate_cvd_10s(trades, window_ms=10000):
    if not trades:
        return []
    buckets = []
    cur = None
    cum = 0.0
    for t in trades:
        ts = int(datetime.fromisoformat(t['time'].replace('Z', '+00:00')).timestamp() * 1000)
        bs = (ts // window_ms) * window_ms
        if cur is None or cur['_bs'] != bs:
            if cur is not None:
                buckets.append(cur)
            cur = {'_bs': bs, 'time': to_iso(bs), 'buy': 0.0, 'sell': 0.0, 'net': 0.0, 'cvd_cum': 0.0, 'count': 0}
        if t['side'] == 'buy':
            cur['buy'] += t['quote']; cum += t['quote']
        else:
            cur['sell'] += t['quote']; cum -= t['quote']
        cur['net'] = cur['buy'] - cur['sell']
        cur['cvd_cum'] = cum
        cur['count'] += 1
    if cur is not None:
        buckets.append(cur)
    return [{
        'time': b['time'],
        'buy': round(b['buy']),
        'sell': round(b['sell']),
        'net': round(b['net']),
        'cvd_cum': round(b['cvd_cum']),
        'count': b['count'],
    } for b in buckets]


def aggregate_buy_sell_1m(trades, window_ms=60000):
    if not trades:
        return []
    buckets = []
    cur = None
    for t in trades:
        ts = int(datetime.fromisoformat(t['time'].replace('Z', '+00:00')).timestamp() * 1000)
        bs = (ts // window_ms) * window_ms
        if cur is None or cur['_bs'] != bs:
            if cur is not None:
                buckets.append(cur)
            cur = {'_bs': bs, 'time': to_iso(bs), 'buy': 0.0, 'sell': 0.0, 'net': 0.0, 'count': 0}
        if t['side'] == 'buy':
            cur['buy'] += t['quote']
        else:
            cur['sell'] += t['quote']
        cur['net'] = cur['buy'] - cur['sell']
        cur['count'] += 1
    if cur is not None:
        buckets.append(cur)
    return [{
        'time': b['time'],
        'buy': round(b['buy']),
        'sell': round(b['sell']),
        'net': round(b['net']),
        'count': b['count'],
    } for b in buckets]


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
        return {'count': 0, 'buy_count': 0, 'sell_count': 0,
                'buy_value': 0, 'sell_value': 0, 'net_value': 0, 'trades': []}
    bv = sv = 0.0; bc = sc = 0
    for t in lst:
        if t['side'] == 'buy': bv += t['quote']; bc += 1
        else: sv += t['quote']; sc += 1
    return {
        'count': len(lst), 'buy_count': bc, 'sell_count': sc,
        'buy_value': bv, 'sell_value': sv, 'net_value': bv - sv,
        'trades': lst[:50],
    }


def compute_orderbook_multi(ob, mid):
    if not ob or not mid or not ob['bids'] or not ob['asks']:
        return None
    levels = [0.001, 0.005, 0.01, 0.02]
    res = {
        'mid_price': mid,
        'spread': ob['asks'][0][0] - ob['bids'][0][0],
        'best_bid': ob['bids'][0][0], 'best_ask': ob['asks'][0][0],
        'best_bid_qty': ob['bids'][0][1], 'best_ask_qty': ob['asks'][0][1],
        'levels': {}, 'bid_walls': [], 'ask_walls': [],
    }
    for pct in levels:
        up = mid * (1 + pct); lo = mid * (1 - pct)
        bv = av = 0.0; bl = al = 0
        for p, q in ob['bids']:
            if p >= lo: bv += p * q; bl += 1
        for p, q in ob['asks']:
            if p <= up: av += p * q; al += 1
        res['levels'][f'pct_{pct}'] = {
            'bid_value': bv, 'ask_value': av,
            'imbalance': bv / (bv + av) if (bv + av) > 0 else None,
            'bid_levels': bl, 'ask_levels': al,
        }
    u1 = mid * 1.01; l1 = mid * 0.99
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
    spv = sv = 0.0
    for c in recent:
        tp = (c['high'] + c['low'] + c['close']) / 3
        spv += tp * c['volume']
        sv += c['volume']
    return spv / sv if sv > 0 else None


def compute_atr(candles, period=14):
    if not candles or len(candles) < period + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        h = candles[i]['high']; l = candles[i]['low']
        pc = candles[i - 1]['close']
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
    prev = candles_1d[-2]; cur = candles_1d[-1]
    return {'pdh': prev['high'], 'pdl': prev['low'], 'prev_close': prev['close'],
            'current_day_high': cur['high'], 'current_day_low': cur['low'],
            'current_day_open': cur['open']}


def compute_vp(candles, bins=50, value_area_pct=0.7):
    if not candles:
        return None
    mn = float('inf'); mx = float('-inf')
    for c in candles:
        mn = min(mn, c['low']); mx = max(mx, c['high'])
    if mn == mx:
        return None
    bin_size = (mx - mn) / bins
    profile = [0.0] * bins
    for c in candles:
        lb = max(0, int((c['low'] - mn) / bin_size))
        hb = min(bins - 1, int((c['high'] - mn) / bin_size))
        nb = max(1, hb - lb + 1)
        vpb = c['volume'] / nb
        for i in range(lb, hb + 1):
            profile[i] += vpb
    poc_idx = 0
    for i in range(1, bins):
        if profile[i] > profile[poc_idx]:
            poc_idx = i
    poc = mn + (poc_idx + 0.5) * bin_size
    total = sum(profile)
    target = total * value_area_pct
    cum = profile[poc_idx]
    lo = hi = poc_idx
    while cum < target and (lo > 0 or hi < bins - 1):
        lv = profile[lo - 1] if lo > 0 else -1
        hv = profile[hi + 1] if hi < bins - 1 else -1
        if hv >= lv:
            hi += 1; cum += profile[hi]
        else:
            lo -= 1; cum += profile[lo]
    val = mn + lo * bin_size
    vah = mn + (hi + 1) * bin_size
    avg = total / bins
    hvn = []; lvn = []
    for i in range(bins):
        price = round(mn + (i + 0.5) * bin_size, 6)
        if profile[i] > avg * 1.5: hvn.append({'price': price, 'volume': profile[i]})
        if 0 < profile[i] < avg * 0.3: lvn.append({'price': price, 'volume': profile[i]})
    hvn.sort(key=lambda x: -x['volume']); lvn.sort(key=lambda x: x['volume'])
    return {'poc': round(poc, 6), 'vah': round(vah, 6), 'val': round(val, 6),
            'total_volume': total,
            'price_range': {'min': mn, 'max': mx, 'bins': bins, 'bin_size': bin_size},
            'hvn': hvn[:12], 'lvn': lvn[:12]}


def detect_cascades(trades, min_quote=20000, window_ms=500, min_count=3):
    if not trades:
        return []
    out = []
    cur = None
    for t in trades:
        ts = int(datetime.fromisoformat(t['time'].replace('Z', '+00:00')).timestamp() * 1000)
        if t['quote'] < min_quote:
            continue
        if cur and cur['side'] == t['side'] and (ts - cur['lastTime']) < window_ms:
            cur['total'] += t['quote']
            cur['count'] += 1
            cur['lastTime'] = ts
            cur['lastPrice'] = t['price']
        else:
            if cur and cur['count'] >= min_count:
                out.append(dict(cur))
            cur = {
                'startTime': t['time'],
                'lastTime': ts,
                'side': t['side'],
                'count': 1,
                'total': t['quote'],
                'firstPrice': t['price'],
                'lastPrice': t['price']
            }
    if cur and cur['count'] >= min_count:
        out.append(dict(cur))
    return out[-50:]
    
# ======================== COMPACT ========================
def build_compact(full):
    out = json.loads(json.dumps(full))
    lim = COMPACT_LIMITS
    tfs = out.get('data', {}).get('coin', {}).get('timeframes', {})
    for tf, td in tfs.items():
        if isinstance(td.get('candles'), list):
            td['candles'] = td['candles'][-lim['candles'].get(tf, 100):]
    ex = out.get('data', {}).get('coin', {}).get('extras', {})
    if ex.get('orderbook'):
        ex['orderbook'] = {'lastUpdateId': ex['orderbook'].get('lastUpdateId'),
                           'raw_removed': True,
                           'note': 'Use orderbook_depth_multi for depth bands + walls'}
    if 'agg_trades' in ex:
        del ex['agg_trades']
    d = ex.get('derivatives')
    if d:
        oi = d.get('open_interest', {})
        for k, l in [('history_5m', '5m'), ('history_1h', '1h'), ('history_1d', '1d')]:
            if isinstance(oi.get(k), list):
                oi[k] = oi[k][-lim['oi'][l]:]
        if isinstance(d.get('funding_rate', {}).get('history'), list):
            d['funding_rate']['history'] = d['funding_rate']['history'][-lim['funding']:]
        for key in ['global_long_short', 'top_trader_long_short', 'taker_volume']:
            v = d.get(key)
            if isinstance(v, dict):
                if isinstance(v.get('1h'), list): v['1h'] = v['1h'][-lim['ls1h']:]
                if isinstance(v.get('15m'), list): v['15m'] = v['15m'][-lim['ls15m']:]
    dom = out.get('data', {}).get('dominance')
    if isinstance(dom, dict):
        for sym, v in dom.items():
            if isinstance(v, list):
                dom[sym] = v[-lim['dominance']:]
            elif isinstance(v, dict) and 'error' not in v:
                for tf, arr in list(v.items()):
                    if isinstance(arr, list):
                        v[tf] = arr[-lim['dominance']:]
    if isinstance(ex.get('liquidation_cascades'), list):
        ex['liquidation_cascades'] = ex['liquidation_cascades'][-lim['liquidations']:]
    if 'metadata' in out:
        out['metadata']['compact'] = True
        out['metadata']['compact_limits'] = lim
    return out


# ======================== MAIN ========================
def main():
    symbol = os.environ.get('SYMBOL', 'SOLUSDT').strip().upper()
    if not symbol:
        log("ERROR: SYMBOL empty")
        sys.exit(1)
    log(f"=== v6 fetcher | symbol={symbol} ===")

    # 1) Candles
    log(f"[1/9] Fetching candles...")
    timeframe_data = {}
    for tf in TIMEFRAMES:
        try:
            timeframe_data[tf] = {'candles': fetch_candles(symbol, tf)}
            log(f"  {tf}: {len(timeframe_data[tf]['candles'])} candles")
        except Exception as e:
            timeframe_data[tf] = {'candles': [], 'error': str(e)}
            log(f"  {tf}: ERROR {e}")

    # 2) AggTrades
    log(f"[2/9] Fetching aggTrades (with retry)...")
    agg = fetch_agg_trades(symbol)
    log(f"  {len(agg)} trades")

    # 3) Orderbook
    time.sleep(jitter(600, 400))
    log(f"[3/9] Fetching order book...")
    ob = fetch_orderbook(symbol)
    log(f"  {'OK' if ob else 'FAIL'}")

    # 4) OI
    log(f"[4/9] Fetching OI...")
    oi_cur = fetch_oi_current(symbol)
    oi_5m = fetch_oi_hist(symbol, '5m', OI_5M_LIMIT)
    oi_1h = fetch_oi_hist(symbol, '1h', OI_1H_LIMIT)
    oi_1d = fetch_oi_hist(symbol, '1d', OI_1D_LIMIT)

    # 5) Funding
    log(f"[5/9] Fetching funding...")
    funding = fetch_funding(symbol, FUNDING_LIMIT)

    # 6) L/S + Taker (1h + 15m)
    log(f"[6/9] Fetching L/S + Taker (1h + 15m)...")
    gls_1h = fetch_global_ls(symbol, '1h', LS_LIMIT)
    tls_1h = fetch_top_ls(symbol, '1h', LS_LIMIT)
    tk_1h = fetch_taker(symbol, '1h', TAKER_LIMIT)
    gls_15m = fetch_global_ls(symbol, '15m', LS_15M_LIMIT)
    tls_15m = fetch_top_ls(symbol, '15m', LS_15M_LIMIT)
    tk_15m = fetch_taker(symbol, '15m', TAKER_15M_LIMIT)

    # 7) Ticker + Premium
    log(f"[7/9] Fetching ticker + premium...")
    tk = fetch_ticker(symbol)
    pi = fetch_premium(symbol)

    # 8) Compute
    log(f"[8/9] Computing...")
    cvd = compute_cvd(agg)
    cvd10s = aggregate_cvd_10s(agg)
    bs1m = aggregate_buy_sell_1m(agg)
    large = extract_large_trades(agg, WHALE_THRESHOLD, 20)
    lw, mw, gw = classify_whales(agg, WHALE_THRESHOLD_LOW, WHALE_THRESHOLD, MEGA_WHALE_THRESHOLD)
    mid = None
    if ob and ob['bids'] and ob['asks']:
        mid = (ob['bids'][0][0] + ob['asks'][0][0]) / 2
    ob_multi = compute_orderbook_multi(ob, mid)
    vwap = compute_vwap(timeframe_data['15m']['candles'], 96)
    atr = {tf: compute_atr(timeframe_data[tf]['candles'], 14) for tf in TIMEFRAMES}
    pdh_pdl = compute_pdh_pdl(timeframe_data['1d']['candles'])
    vp5m = compute_vp(timeframe_data['5m']['candles'])
    vp15m = compute_vp(timeframe_data['15m']['candles'])
    cascades = detect_cascades(agg, 20000, 500, 3)

    # 9) Dominance
    log(f"[9/9] Fetching dominance USDT.D...")
    dom = fetch_dominance()
    dominance_data = {'CRYPTOCAP:USDT.D': dom.get('data') if dom and not dom.get('error') else {'error': 'Failed to fetch'}}

    # Output
    output = {
        'metadata': {
            'symbol': symbol,
            'exchange': 'binance_futures',
            'timestamp': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z',
            'timeframes': TIMEFRAMES,
            'dominance_symbols': DOMINANCE_SYMBOLS,
            'whale_low_threshold_usd': WHALE_THRESHOLD_LOW,
            'whale_threshold_usd': WHALE_THRESHOLD,
            'mega_whale_threshold_usd': MEGA_WHALE_THRESHOLD,
            'version': 'v6',
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
                    'orderbook': {'bids': ob['bids'][:ORDERBOOK_LIMIT], 'asks': ob['asks'][:ORDERBOOK_LIMIT], 'lastUpdateId': ob['lastUpdateId']} if ob else None,
                    'orderbook_depth_multi': ob_multi,
                    'cvd': cvd,
                    'vwap_24h': vwap,
                    'atr': atr,
                    'pdh_pdl': pdh_pdl,
                    'volume_profile_5m': vp5m,
                    'volume_profile_15m': vp15m,
                    'liquidation_cascades': cascades,
                    'derivatives': {
                        'open_interest': {'current': oi_cur, 'history_5m': oi_5m, 'history_1h': oi_1h, 'history_1d': oi_1d},
                        'funding_rate': {'current': funding[-1]['rate'] if funding else None, 'history': funding},
                        'global_long_short': {'1h': gls_1h, '15m': gls_15m},
                        'top_trader_long_short': {'1h': tls_1h, '15m': tls_15m},
                        'taker_volume': {'1h': tk_1h, '15m': tk_15m},
                    },
                    'ticker_24h': {
                        'price_change': float(tk['priceChange']),
                        'price_change_pct': float(tk['priceChangePercent']),
                        'high': float(tk['highPrice']), 'low': float(tk['lowPrice']),
                        'volume': float(tk['volume']), 'quote_volume': float(tk['quoteVolume']),
                    } if tk else None,
                    'premium_index': {
                        'mark_price': float(pi['markPrice']),
                        'index_price': float(pi['indexPrice']),
                        'last_funding_rate': float(pi['lastFundingRate']),
                        'next_funding_time': to_iso(int(pi['nextFundingTime'])),
                    } if pi else None,
                },
            },
            'dominance': dominance_data,
        },
    }

    compact = build_compact(output)
    OUTPUT_DIR.mkdir(exist_ok=True)
    out_path = OUTPUT_DIR / f"{symbol}.json"
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(compact, f, ensure_ascii=False, separators=(',', ':'))
    size_kb = out_path.stat().st_size / 1024
    log(f"\n✅ Saved: {out_path} ({size_kb:.1f} KB)")
    log(f"   Candles: 1m={len(compact['data']['coin']['timeframes']['1m']['candles'])} "
        f"15m={len(compact['data']['coin']['timeframes']['15m']['candles'])} "
        f"1h={len(compact['data']['coin']['timeframes']['1h']['candles'])}")
    log(f"   CVD10s={len(compact['data']['coin']['extras']['cvd_10s'])} "
        f"BS1m={len(compact['data']['coin']['extras']['buy_sell_1m'])} "
        f"Large={len(compact['data']['coin']['extras']['large_trades_last'])}")


if __name__ == '__main__':
    main()
