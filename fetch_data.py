#!/usr/bin/env python3
"""
اسکریپت دریافت داده ترکیبی ارز دیجیتال از 6 صرافی + دامیننس + داده‌های لحظه‌ای
اجرا روی GitHub Actions - بدون نیاز به فیلترشکن
"""

import os
import sys
import json
import requests
from datetime import datetime, timezone
from pathlib import Path

# ============================================================
# تنظیمات (دقیقاً مطابق HTML)
# ============================================================
TIMEFRAMES = ['5m', '15m', '1h', '4h', '1d']
DOMINANCE_TIMEFRAMES = ['1h', '4h', '1d']
CANDLE_LIMIT = 200
EXCHANGE_TIMEOUT = 20

DOMINANCE_API_URL = 'https://tv-dominance-api.onrender.com/api/dominance'
DOMINANCE_SYMBOLS = ['CRYPTOCAP:BTC.D', 'CRYPTOCAP:USDT.D']

BYBIT_LOW_VALUE_MAP = {
    'SHIB': '1000SHIBUSDT', 'BONK': '1000BONKUSDT', 'PEPE': '1000PEPEUSDT',
    'XEC': '1000XECUSDT', 'FLOKI': '1000FLOKIUSDT', '1000SATS': '1000SATSUSDT',
    'DOGS': 'DOGSUSDT', 'NOT': 'NOTUSDT'
}

# ============================================================
# توابع کمکی
# ============================================================
def log(msg):
    print(msg, flush=True)

def get_base(symbol):
    if not symbol.endswith('USDT'):
        return symbol
    return symbol[:-4]

def to_iso_ms(ms):
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).isoformat().replace('+00:00', 'Z')

def to_iso_sec(sec):
    return datetime.fromtimestamp(int(sec), tz=timezone.utc).isoformat().replace('+00:00', 'Z')

def safe_request(url, timeout=EXCHANGE_TIMEOUT):
    try:
        r = requests.get(url, timeout=timeout, headers={'User-Agent': 'Mozilla/5.0'})
        if r.status_code != 200:
            return None
        return r.json()
    except Exception as e:
        log(f"  ⚠️ خطا در {url[:70]}...: {e}")
        return None


# ============================================================
# BINANCE
# ============================================================
def binance_candles(symbol, tf):
    im = {'5m':'5m','15m':'15m','1h':'1h','4h':'4h','1d':'1d'}
    url = f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval={im[tf]}&limit={CANDLE_LIMIT}"
    data = safe_request(url)
    if not data or not isinstance(data, list): raise Exception("Binance candles failed")
    return [{"time": to_iso_ms(c[0]), "open": float(c[1]), "high": float(c[2]),
             "low": float(c[3]), "close": float(c[4]), "volume": float(c[5])} for c in data]

def binance_trades(symbol):
    data = safe_request(f"https://fapi.binance.com/fapi/v1/trades?symbol={symbol}&limit=300")
    if not data: return None
    return [{"time": to_iso_ms(t["time"]), "price": float(t["price"]),
             "amount": float(t["qty"]), "side": "sell" if t["isBuyerMaker"] else "buy"} for t in data]

def binance_orderbook(symbol):
    data = safe_request(f"https://fapi.binance.com/fapi/v1/depth?symbol={symbol}&limit=50")
    if not data: return None
    return {"bids": [[float(b[0]), float(b[1])] for b in data["bids"]],
            "asks": [[float(a[0]), float(a[1])] for a in data["asks"]]}

def binance_oi(symbol):
    data = safe_request(f"https://fapi.binance.com/fapi/v1/openInterest?symbol={symbol}")
    if not data: return None
    return float(data["openInterest"])

def binance_funding(symbol):
    data = safe_request(f"https://fapi.binance.com/fapi/v1/fundingRate?symbol={symbol}&limit=1")
    if not data or len(data) == 0: return None
    return float(data[0]["fundingRate"])

def binance_lsr(symbol):
    data = safe_request(f"https://fapi.binance.com/futures/data/globalLongShortAccountRatio?symbol={symbol}&period=5m&limit=1")
    if not data or len(data) == 0: return None
    return float(data[0]["longShortRatio"])


# ============================================================
# BYBIT
# ============================================================
def bybit_symbol(symbol):
    base = get_base(symbol)
    return BYBIT_LOW_VALUE_MAP.get(base, symbol)

def bybit_candles(symbol, tf):
    im = {'5m':'5','15m':'15','1h':'60','4h':'240','1d':'D'}
    s = bybit_symbol(symbol)
    data = safe_request(f"https://api.bybit.com/v5/market/kline?category=linear&symbol={s}&interval={im[tf]}&limit={CANDLE_LIMIT}")
    if not data or data.get("retCode") != 0: raise Exception("Bybit candles failed")
    return [{"time": to_iso_ms(c[0]), "open": float(c[1]), "high": float(c[2]),
             "low": float(c[3]), "close": float(c[4]), "volume": float(c[5])} for c in data["result"]["list"]]

def bybit_trades(symbol):
    s = bybit_symbol(symbol)
    data = safe_request(f"https://api.bybit.com/v5/market/recent-trade?category=linear&symbol={s}&limit=300")
    if not data or data.get("retCode") != 0: return None
    return [{"time": to_iso_ms(t["time"]), "price": float(t["price"]),
             "amount": float(t["size"]), "side": t["side"].lower()} for t in data["result"]["list"]]

def bybit_orderbook(symbol):
    s = bybit_symbol(symbol)
    data = safe_request(f"https://api.bybit.com/v5/market/orderbook?category=linear&symbol={s}&limit=50")
    if not data or data.get("retCode") != 0: return None
    return {"bids": [[float(b[0]), float(b[1])] for b in data["result"]["b"]],
            "asks": [[float(a[0]), float(a[1])] for a in data["result"]["a"]]}

def bybit_oi(symbol):
    s = bybit_symbol(symbol)
    data = safe_request(f"https://api.bybit.com/v5/market/open-interest?category=linear&symbol={s}&intervalTime=5min&limit=1")
    if not data or data.get("retCode") != 0 or not data["result"]["list"]: return None
    return float(data["result"]["list"][0]["openInterest"])

def bybit_funding(symbol):
    s = bybit_symbol(symbol)
    data = safe_request(f"https://api.bybit.com/v5/market/funding/history?category=linear&symbol={s}&limit=1")
    if not data or data.get("retCode") != 0 or not data["result"]["list"]: return None
    return float(data["result"]["list"][0]["fundingRate"])

def bybit_lsr(symbol):
    return None  # Bybit این را غیرعمومی دارد


# ============================================================
# OKX
# ============================================================
def okx_symbol(symbol):
    return f"{get_base(symbol)}-USDT-SWAP"

def okx_candles(symbol, tf):
    im = {'5m':'5m','15m':'15m','1h':'1H','4h':'4H','1d':'1D'}
    s = okx_symbol(symbol)
    data = safe_request(f"https://www.okx.com/api/v5/market/history-candles?instId={s}&bar={im[tf]}&limit={CANDLE_LIMIT}")
    if not data or data.get("code") != "0": raise Exception("OKX candles failed")
    return [{"time": to_iso_ms(c[0]), "open": float(c[1]), "high": float(c[2]),
             "low": float(c[3]), "close": float(c[4]), "volume": float(c[5])} for c in data["data"]]

def okx_trades(symbol):
    s = okx_symbol(symbol)
    data = safe_request(f"https://www.okx.com/api/v5/market/trades?instId={s}&limit=300")
    if not data or data.get("code") != "0": return None
    return [{"time": to_iso_ms(t["ts"]), "price": float(t["px"]),
             "amount": float(t["sz"]), "side": t["side"]} for t in data["data"]]

def okx_orderbook(symbol):
    s = okx_symbol(symbol)
    data = safe_request(f"https://www.okx.com/api/v5/market/books?instId={s}&sz=50")
    if not data or data.get("code") != "0": return None
    ob = data["data"][0]
    return {"bids": [[float(b[0]), float(b[1])] for b in ob["bids"]],
            "asks": [[float(a[0]), float(a[1])] for a in ob["asks"]]}

def okx_oi(symbol):
    s = okx_symbol(symbol)
    data = safe_request(f"https://www.okx.com/api/v5/public/open-interest?instId={s}")
    if not data or data.get("code") != "0" or not data["data"]: return None
    return float(data["data"][0]["oi"])

def okx_funding(symbol):
    s = okx_symbol(symbol)
    data = safe_request(f"https://www.okx.com/api/v5/public/funding-rate?instId={s}")
    if not data or data.get("code") != "0" or not data["data"]: return None
    return float(data["data"][0]["fundingRate"])

def okx_lsr(symbol):
    return None


# ============================================================
# KUCOIN
# ============================================================
def kucoin_candles(symbol, tf):
    gm = {'5m':5,'15m':15,'1h':60,'4h':240,'1d':1440}
    s = f"{get_base(symbol)}USDTM"
    data = safe_request(f"https://api-futures.kucoin.com/api/v1/kline/query?symbol={s}&granularity={gm[tf]}&limit={CANDLE_LIMIT}")
    if not data or data.get("code") != "200000": raise Exception("KuCoin candles failed")
    return [{"time": to_iso_sec(c[0]), "open": float(c[1]), "high": float(c[2]),
             "low": float(c[3]), "close": float(c[4]), "volume": float(c[5])} for c in data["data"]]

def kucoin_trades(symbol):
    s = f"{get_base(symbol)}USDTM"
    data = safe_request(f"https://api-futures.kucoin.com/api/v1/trade-history?symbol={s}")
    if not data or data.get("code") != "200000": return None
    return [{"time": to_iso_ms(t["time"]), "price": float(t["price"]),
             "amount": float(t["size"]), "side": t["side"]} for t in data["data"]]

def kucoin_orderbook(symbol):
    s = f"{get_base(symbol)}USDTM"
    data = safe_request(f"https://api-futures.kucoin.com/api/v1/level2/depth?symbol={s}&limit=100")
    if not data or data.get("code") != "200000": return None
    return {"bids": [[float(b[0]), float(b[1])] for b in data["data"]["bids"][:20]],
            "asks": [[float(a[0]), float(a[1])] for a in data["data"]["asks"][:20]]}

def kucoin_oi(symbol):
    s = f"{get_base(symbol)}USDTM"
    data = safe_request(f"https://api-futures.kucoin.com/api/v1/open-interest?symbol={s}")
    if not data or data.get("code") != "200000": return None
    return float(data["data"]["openInterest"])

def kucoin_funding(symbol):
    s = f"{get_base(symbol)}USDTM"
    data = safe_request(f"https://api-futures.kucoin.com/api/v1/funding-rate?symbol={s}")
    if not data or data.get("code") != "200000": return None
    return float(data["data"]["fundingRate"])

def kucoin_lsr(symbol):
    return None


# ============================================================
# GATE.IO
# ============================================================
def gateio_candles(symbol, tf):
    im = {'5m':'5m','15m':'15m','1h':'1h','4h':'4h','1d':'1d'}
    c = f"{get_base(symbol)}_USDT"
    data = safe_request(f"https://api.gateio.ws/api/v4/futures/usdt/contracts/{c}/candlesticks?interval={im[tf]}&limit={CANDLE_LIMIT}")
    if not data or not isinstance(data, list): raise Exception("GateIO candles failed")
    return [{"time": to_iso_sec(x[0]), "open": float(x[5]), "high": float(x[3]),
             "low": float(x[4]), "close": float(x[2]), "volume": float(x[1])} for x in data]

def gateio_trades(symbol):
    c = f"{get_base(symbol)}_USDT"
    data = safe_request(f"https://api.gateio.ws/api/v4/futures/usdt/trades?contract={c}&limit=300")
    if not data or not isinstance(data, list): return None
    return [{"time": to_iso_sec(t["create_time"]), "price": float(t["price"]),
             "amount": float(t["size"]), "side": "buy" if t["size"] > 0 else "sell"} for t in data]

def gateio_orderbook(symbol):
    c = f"{get_base(symbol)}_USDT"
    data = safe_request(f"https://api.gateio.ws/api/v4/futures/usdt/order_book?contract={c}&limit=50")
    if not data: return None
    return {"bids": [[float(b["p"]), float(b["s"])] for b in data["bids"]],
            "asks": [[float(a["p"]), float(a["s"])] for a in data["asks"]]}

def gateio_oi(symbol):
    c = f"{get_base(symbol)}_USDT"
    data = safe_request(f"https://api.gateio.ws/api/v4/futures/usdt/contracts/{c}")
    if not data: return None
    return float(data["open_interest"])

def gateio_funding(symbol):
    c = f"{get_base(symbol)}_USDT"
    data = safe_request(f"https://api.gateio.ws/api/v4/futures/usdt/contracts/{c}/funding_rate")
    if not data: return None
    return float(data["funding_rate"])

def gateio_lsr(symbol):
    return None


# ============================================================
# MEXC
# ============================================================
def mexc_candles(symbol, tf):
    im = {'5m':'5m','15m':'15m','1h':'1h','4h':'4h','1d':'1d'}
    s = f"{get_base(symbol)}_USDT"
    data = safe_request(f"https://futures.mexc.com/api/v1/contract/kline?symbol={s}&interval={im[tf]}&limit={CANDLE_LIMIT}")
    if not data or data.get("code") != 200: raise Exception("MEXC candles failed")
    return [{"time": to_iso_ms(c[0]), "open": float(c[1]), "high": float(c[2]),
             "low": float(c[3]), "close": float(c[4]), "volume": float(c[5])} for c in data["data"]]

def mexc_trades(symbol):
    return None  # MEXC ندارد

def mexc_orderbook(symbol):
    s = f"{get_base(symbol)}_USDT"
    data = safe_request(f"https://futures.mexc.com/api/v1/contract/depth?symbol={s}&limit=50")
    if not data or data.get("code") != 200: return None
    return {"bids": [[float(b[0]), float(b[1])] for b in data["data"]["bids"]],
            "asks": [[float(a[0]), float(a[1])] for a in data["data"]["asks"]]}

def mexc_oi(symbol):
    s = f"{get_base(symbol)}_USDT"
    data = safe_request(f"https://futures.mexc.com/api/v1/contract/open_interest?symbol={s}")
    if not data or data.get("code") != 200: return None
    return float(data["data"]["openInterest"])

def mexc_funding(symbol):
    s = f"{get_base(symbol)}_USDT"
    data = safe_request(f"https://futures.mexc.com/api/v1/contract/funding_rate?symbol={s}")
    if not data or data.get("code") != 200: return None
    return float(data["data"]["fundingRate"])

def mexc_lsr(symbol):
    return None


# ============================================================
# لیست صرافی‌ها (به ترتیب اولویت - دقیقاً مطابق HTML)
# ============================================================
EXCHANGES = [
    {'name': 'Binance Futures', 'id': 'binance',
     'candles': binance_candles, 'trades': binance_trades, 'orderbook': binance_orderbook,
     'oi': binance_oi, 'funding': binance_funding, 'lsr': binance_lsr},
    {'name': 'Bybit Futures', 'id': 'bybit',
     'candles': bybit_candles, 'trades': bybit_trades, 'orderbook': bybit_orderbook,
     'oi': bybit_oi, 'funding': bybit_funding, 'lsr': bybit_lsr},
    {'name': 'OKX Futures', 'id': 'okx',
     'candles': okx_candles, 'trades': okx_trades, 'orderbook': okx_orderbook,
     'oi': okx_oi, 'funding': okx_funding, 'lsr': okx_lsr},
    {'name': 'KuCoin Futures', 'id': 'kucoin',
     'candles': kucoin_candles, 'trades': kucoin_trades, 'orderbook': kucoin_orderbook,
     'oi': kucoin_oi, 'funding': kucoin_funding, 'lsr': kucoin_lsr},
    {'name': 'Gate.io Futures', 'id': 'gateio',
     'candles': gateio_candles, 'trades': gateio_trades, 'orderbook': gateio_orderbook,
     'oi': gateio_oi, 'funding': gateio_funding, 'lsr': gateio_lsr},
    {'name': 'MEXC Futures', 'id': 'mexc',
     'candles': mexc_candles, 'trades': mexc_trades, 'orderbook': mexc_orderbook,
     'oi': mexc_oi, 'funding': mexc_funding, 'lsr': mexc_lsr},
]


# ============================================================
# دریافت دامیننس
# ============================================================
def fetch_dominance(symbol, timeframes, limit):
    tf_str = ','.join(timeframes)
    url = f"{DOMINANCE_API_URL}?symbol={symbol}&timeframes={tf_str}&limit={limit}"
    log(f"🌐 دریافت دامیننس {symbol}...")
    try:
        r = requests.get(url, timeout=60, headers={'User-Agent': 'Mozilla/5.0'})
        if r.status_code != 200:
            log(f"  ❌ خطای HTTP {r.status_code}")
            return None
        return r.json()
    except Exception as e:
        log(f"  ❌ خطا: {e}")
        return None


# ============================================================
# تابع اصلی
# ============================================================
def fetch_all_data(symbol):
    used_exchange = None
    exchange_data = None
    extra_data = None

    # ---- تلاش روی صرافی‌ها به ترتیب اولویت ----
    for ex in EXCHANGES:
        log(f"\n🔍 تلاش روی {ex['name']}...")
        try:
            # دریافت کندل‌های همه تایم‌فریم‌ها
            data = {}
            for tf in TIMEFRAMES:
                log(f"  📊 {tf}...")
                candles = ex['candles'](symbol, tf)
                if not candles:
                    raise Exception(f"{tf} خالی برگشت")
                data[tf] = {"candles": candles}
                log(f"    ✅ {len(candles)} کندل")

            exchange_data = data
            used_exchange = ex['id']
            log(f"✅ تمام تایم‌فریم‌ها از {ex['name']} دریافت شد")

            # ---- داده‌های لحظه‌ای ----
            log(f"📡 دریافت داده‌های لحظه‌ای از {ex['name']}...")
            trades = ex['trades'](symbol) if ex['trades'] else None
            orderbook = ex['orderbook'](symbol) if ex['orderbook'] else None
            oi = ex['oi'](symbol) if ex['oi'] else None
            funding = ex['funding'](symbol) if ex['funding'] else None
            lsr = ex['lsr'](symbol) if ex['lsr'] else None

            extra_data = {
                "trades": trades,
                "orderbook": orderbook,
                "derivatives": {
                    "open_interest": oi,
                    "funding_rate": funding,
                    "long_short_ratio": lsr
                }
            }
            break  # موفق شد - از حلقه خارج شو

        except Exception as e:
            log(f"  ⚠️ {ex['name']} ناموفق: {e} — تلاش صرافی بعدی")
            continue

    if not exchange_data:
        raise Exception("❌ تمام صرافی‌ها ناموفق بودند!")

    # ---- دریافت دامیننس ----
    log(f"\n🔄 دریافت دامیننس BTC.D و USDT.D...")
    dominance_data = {}
    for dom_symbol in DOMINANCE_SYMBOLS:
        result = fetch_dominance(dom_symbol, DOMINANCE_TIMEFRAMES, CANDLE_LIMIT)
        if result and not result.get('error'):
            dominance_data[dom_symbol] = result.get('data', {})
        else:
            dominance_data[dom_symbol] = {"error": "Failed to fetch"}

    # ---- خروجی نهایی ----
    output = {
        "metadata": {
            "symbol": symbol,
            "exchange": used_exchange,
            "timestamp": datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
            "timeframes": TIMEFRAMES,
            "dominance_symbols": DOMINANCE_SYMBOLS
        },
        "data": {
            "coin": {
                "symbol": symbol,
                "timeframes": exchange_data,
                "extras": extra_data
            },
            "dominance": dominance_data
        }
    }
    return output


# ============================================================
# اجرا
# ============================================================
def main():
    symbol = os.environ.get('SYMBOL', 'BTCUSDT').strip().upper()
    log(f"🚀 شروع دریافت داده برای: {symbol}")

    try:
        output = fetch_all_data(symbol)
    except Exception as e:
        log(f"❌ خطای کلی: {e}")
        sys.exit(1)

    # ساخت پوشه data
    Path("data").mkdir(exist_ok=True)
    out_file = Path(f"data/{symbol}_latest.json")

    with open(out_file, 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, separators=(',', ':'))

    size_kb = out_file.stat().st_size / 1024
    log(f"\n✅ فایل ذخیره شد: {out_file} ({size_kb:.1f} کیلوبایت)")


if __name__ == '__main__':
    main()
