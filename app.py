# 1. ایمپورت کتابخانه‌ها
import os
from flask import Flask, request, jsonify
from flask_cors import CORS
from tvDatafeed import TvDatafeed, Interval
import pandas as pd
import traceback

# 2. ایجاد اپلیکیشن Flask و فعال‌سازی CORS
app = Flask(__name__)
CORS(app)  # این خط به مرورگر شما اجازه می‌دهد از این API استفاده کند

# 3. نگاشت تایم‌فریم‌های متنی به فرمت کتابخانه tvdatafeed
INTERVAL_MAP = {
    '5m': Interval.in_5_minute,
    '15m': Interval.in_15_minute,
    '1h': Interval.in_1_hour,
    '4h': Interval.in_4_hour,
    '1d': Interval.in_daily
}

# ============================================================================
# تابع تبدیل دیتافریم پانداس به لیست از دیکشنری‌ها (فرمت مورد قبول JSON)
# ============================================================================
def dataframe_to_candle_list(df: pd.DataFrame) -> list:
    """
    دیتافریم دریافتی از tvdatafeed را به لیستی از کندل‌ها تبدیل می‌کند.
    """
    if df is None or df.empty:
        return []
    
    candles = []
    for index, row in df.iterrows():
        candle = {
            "time": index.strftime('%Y-%m-%dT%H:%M:%S.000Z') if hasattr(index, 'strftime') else str(index),
            "open": float(row['open']),
            "high": float(row['high']),
            "low": float(row['low']),
            "close": float(row['close']),
            "volume": float(row['volume'])
        }
        candles.append(candle)
    return candles


# ============================================================================
# اندپوینت اصلی API: دریافت دامیننس
# ============================================================================
@app.route('/api/dominance', methods=['GET'])
def get_dominance():
    """
    نمونه درخواست: GET /api/dominance?symbol=BTC.D&timeframes=5m,15m,1h,4h,1d&limit=200
    """
    try:
        # 1. گرفتن پارامترها از URL
        symbol = request.args.get('symbol', default='BTC.D', type=str)
        timeframes_param = request.args.get('timeframes', default='5m,15m,1h,4h,1d', type=str)
        limit = request.args.get('limit', default=200, type=int)

        # 2. دریافت username و password از متغیرهای محیطی (برای امنیت)
        tv_username = os.environ.get('TV_USERNAME')
        tv_password = os.environ.get('TV_PASSWORD')

        if not tv_username or not tv_password:
            return jsonify({"error": "TradingView credentials not set on server"}), 500

        # 3. اتصال به TradingView
        tv = TvDatafeed(tv_username, tv_password)

        # 4. تجزیه لیست تایم‌فریم‌ها
        timeframe_list = [tf.strip() for tf in timeframes_param.split(',')]

        result_data = {}

        # 5. حلقه روی تایم‌فریم‌ها و دریافت داده
        for tf in timeframe_list:
            if tf not in INTERVAL_MAP:
                result_data[tf] = {"error": f"Unsupported timeframe: {tf}"}
                continue

            interval = INTERVAL_MAP[tf]

            # دریافت داده از TradingView
            # نکته بسیار مهم: برای دامیننس، صرافی باید "CRYPTO" باشد و نماد مثلاً "BTC.D"
            df = tv.get_hist(
                symbol=symbol,
                exchange='CRYPTO',      # صرافی برای دامیننس
                interval=interval,
                n_bars=limit
            )

            if df is not None and not df.empty:
                candles_list = dataframe_to_candle_list(df)
                result_data[tf] = {
                    "candles": candles_list,
                    "count": len(candles_list)
                }
            else:
                result_data[tf] = {
                    "candles": [],
                    "count": 0,
                    "error": "No data returned from TradingView"
                }

        # 6. ساختن پاسخ نهایی
        response = {
            "symbol": symbol,
            "timeframes": timeframe_list,
            "data": result_data
        }

        return jsonify(response)

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


# ============================================================================
# اندپوینت ساده برای تست
# ============================================================================
@app.route('/')
def home():
    return "TradingView Dominance API is running. Use /api/dominance endpoint."


# ============================================================================
# اجرای برنامه (برای تست محلی)
# ============================================================================
if __name__ == '__main__':
    # در محیط توسعه local از این پورت استفاده کن
    app.run(host='0.0.0.0', port=5000, debug=True)