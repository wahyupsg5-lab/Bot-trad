import pandas as pd
import numpy as np
from pybit.unified_trading import HTTP
import os
import time
import sys
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

# ============================================================
# LOG SERVER — akses via https://xxx.up.railway.app/logs
# ============================================================
LOG_FILE = "bot.log"

class _Tee:
    """Redirect print() ke stdout DAN file sekaligus, dengan timestamp WIB per baris."""
    def __init__(self):
        self._out     = sys.__stdout__
        self._file    = open(LOG_FILE, 'a', buffering=1, encoding='utf-8')
        self._newline = True
    def write(self, msg):
        import datetime
        out = ''
        for ch in msg:
            if self._newline and ch != '\n':
                out += (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=7)).strftime('[%H:%M:%S] ')
                self._newline = False
            out += ch
            if ch == '\n':
                self._newline = True
        self._out.write(out)
        self._file.write(out)
    def flush(self):
        self._out.flush()
        self._file.flush()

sys.stdout = _Tee()

class _LogHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in ('/logs', '/logs?'):
            self.send_response(404); self.end_headers(); return
        try:
            with open(LOG_FILE, 'r', encoding='utf-8') as f:
                lines = f.readlines()
            data = ''.join(lines[-200:]).encode('utf-8')
        except:
            data = b''
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain; charset=utf-8')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(data)
    def log_message(self, *a):
        pass

PORT = int(os.environ.get('PORT', 8080))
threading.Thread(
    target=lambda: HTTPServer(('0.0.0.0', PORT), _LogHandler).serve_forever(),
    daemon=True
).start()
print(f"📡 Log server jalan di port {PORT} → /logs")

# ============================================================
# CONFIG
# ============================================================
API_KEY    = os.environ.get('API_KEY', '')
API_SECRET = os.environ.get('API_SECRET', '')
CATEGORY   = "linear"
TESTNET    = os.environ.get('TESTNET', 'false').lower() == 'true'

if not API_KEY or not API_SECRET:
    raise ValueError("❌ API_KEY dan API_SECRET belum diset!")

session = HTTP(testnet=TESTNET, api_key=API_KEY, api_secret=API_SECRET)

# ── Strategy params (sinkron dengan backtest.py) ─────────────
SL_MULT          = 6.2    # SL = SL_MULT × gap_size dari entry (fallback)
TRAIL_STOP       = 0.5    # trailing distance = TRAIL_STOP × dist (sinkron backtest Trail=0.5R)
TRAIL_ACT_R      = 2.5    # trail aktif setelah +TRAIL_ACT_R (Bybit min > trailingStop)
TRAIL_TIMEOUT_DAYS = 3    # close posisi jika peak tidak bergerak selama N hari (sinkron backtest)
SBR_MODE         = True   # True = SBR entry di C1.close + SL di C1.low, False = OCL entry lama
ENTRY_MODE       = 'fvg_limit'  # 'fvg_sbr' (market saat touch) | 'fvg_limit' (limit langsung di BOS)
TOUCH_VOL_MIN    = 0.8    # touch candle volume min (× avg 20 M5 candle) — hanya dipakai fvg_sbr
MAX_GAP_PCT      = 0.006  # max gap_size / entry_price (FVG ≤ 0.60%)
MAX_CONCURRENT   = 5      # maks order limit aktif + posisi bersamaan
APPROACH_R       = 2.0    # place limit saat harga dalam 2R dari entry
REQUIRE_BOS      = False  # True = BOS H1 dulu; False = FVG kuat langsung (FVG-only mode)

SYMBOLS = [
    # 14 coin aktif — sinkron dengan backtest fvg_limit Jan2025–Apr2026
    '1000BONKUSDT', 'BERAUSDT', 'SHIB1000USDT', 'JUPUSDT',
    'ORCAUSDT', 'XRPUSDT', 'TAOUSDT',
    'SOLUSDT', 'AAVEUSDT',
    'GMXUSDT', 'LTCUSDT', 'ICPUSDT',
    'BELUSDT', 'VIRTUALUSDT',
]

ATR_THRESHOLD = {
    # ATR P25 dari backtest fvg_limit Jan2025–Apr2026
    '1000BONKUSDT'  : 0.0031,   # P25=0.308%
    'BERAUSDT'      : 0.0031,   # P25=0.305%
    'SHIB1000USDT'  : 0.0019,   # P25=0.188%
    'JUPUSDT'       : 0.0028,   # P25=0.278%
    'ORCAUSDT'      : 0.0021,   # P25=0.214%
    'XRPUSDT'       : 0.0018,   # P25=0.185%
    'TAOUSDT'       : 0.0031,   # P25=0.313%
    'SOLUSDT'       : 0.0022,   # P25=0.217%
    'AAVEUSDT'      : 0.0026,   # P25=0.259%
    'GMXUSDT'       : 0.0020,   # P25=0.203%
    'LTCUSDT'       : 0.0018,   # P25=0.178%
    'ICPUSDT'       : 0.0023,   # P25=0.231%
    'BELUSDT'       : 0.0021,   # P25=0.214%
    'VIRTUALUSDT'   : 0.0036,   # P25=0.363%
}

pending          = {}
active_positions = {}
instrument_cache = {}
done_setups      = {}   # coin -> {swing_val, stype, used_ocl} — cegah re-entry di BOS yang sama


# ============================================================
# FUNGSI DATA
# ============================================================

def get_data(symbol, interval, limit=200):
    try:
        res = session.get_kline(
            category=CATEGORY, symbol=symbol,
            interval=interval, limit=limit
        )
        if res['retCode'] == 0:
            df = pd.DataFrame(
                res['result']['list'],
                columns=['ts','open','high','low','close','vol','turnover']
            )
            df[['open','high','low','close','vol','turnover','ts']] = \
                df[['open','high','low','close','vol','turnover','ts']].apply(pd.to_numeric)
            return df.iloc[::-1].reset_index(drop=True)
        print(f"⚠️ get_data {symbol} {interval}: {res.get('retMsg','')}")
        return None
    except Exception as e:
        print(f"⚠️ get_data {symbol} {interval}: {e}")
        return None


# ============================================================
# INSTRUMENT INFO
# ============================================================

def get_instrument_info(symbol):
    if symbol in instrument_cache:
        return instrument_cache[symbol]
    try:
        res = session.get_instruments_info(category=CATEGORY, symbol=symbol)
        if res['retCode'] == 0:
            info = res['result']['list'][0]
            lot  = info['lotSizeFilter']
            data = {
                'min_qty'     : float(lot['minOrderQty']),
                'qty_step'    : float(lot['qtyStep']),
                'tick_size'   : float(info['priceFilter']['tickSize']),
                'max_leverage': float(info.get('leverageFilter', {}).get('maxLeverage', 10)),
            }
            instrument_cache[symbol] = data
            return data
    except Exception as e:
        print(f"⚠️ instrument_info {symbol}: {e}")
    return {'min_qty': 0.01, 'qty_step': 0.01, 'tick_size': 0.0001}


def round_qty(qty, step):
    step_str  = f'{step:.10f}'.rstrip('0')
    precision = len(step_str.split('.')[-1]) if '.' in step_str else 0
    return round(int(qty / step) * step, precision)


def round_price(price, tick):
    tick_str  = f'{tick:.10f}'.rstrip('0')
    precision = len(tick_str.split('.')[-1]) if '.' in tick_str else 0
    return round(round(price / tick) * tick, precision)


# ============================================================
# INDICATORS
# ============================================================

def calc_ema(series, period):
    return series.ewm(span=period, adjust=False).mean()

def calc_atr(df, period=14):
    h, l, pc = df['high'], df['low'], df['close'].shift(1)
    tr = pd.concat([h-l, (h-pc).abs(), (l-pc).abs()], axis=1).max(axis=1)
    return tr.rolling(period).mean()


# ============================================================
# SWING DETECTION
# ============================================================

def find_last_swing_bos(df):
    highs, lows = [], []
    for i in range(2, len(df) - 2):
        h = df['high'].iloc[i]
        l = df['low'].iloc[i]
        if (df['high'].iloc[i-2] < h and df['high'].iloc[i-1] < h and
                df['high'].iloc[i+1] < h and df['high'].iloc[i+2] < h):
            highs.append({'val': h, 'idx': i, 'ts': df['ts'].iloc[i]})
        if (df['low'].iloc[i-2] > l and df['low'].iloc[i-1] > l and
                df['low'].iloc[i+1] > l and df['low'].iloc[i+2] > l):
            lows.append({'val': l, 'idx': i, 'ts': df['ts'].iloc[i]})
    return highs, lows


# ============================================================
# FVG — dengan volume fields untuk fvg_strong
# ============================================================

def _gap_vol_fields(df, c3_idx):
    """Extract volume + OCL + C1 fields untuk FVG (df dalam H1). C1=c3_idx-2."""
    c2_idx   = c3_idx - 1
    c1_idx   = c3_idx - 2
    c2_close = float(df['close'].iloc[c2_idx]) if c2_idx >= 0 else 0.0
    c3_open  = float(df['open'].iloc[c3_idx])  if c3_idx < len(df) else 0.0
    c1_open  = float(df['open'].iloc[c1_idx])  if c1_idx >= 0 else 0.0
    c1_close = float(df['close'].iloc[c1_idx]) if c1_idx >= 0 else 0.0
    c1_low   = float(df['low'].iloc[c1_idx])   if c1_idx >= 0 else 0.0
    c1_high  = float(df['high'].iloc[c1_idx])  if c1_idx >= 0 else 0.0
    base = {'c2_close': c2_close, 'c3_open': c3_open,
            'c1_open': c1_open, 'c1_close': c1_close,
            'c1_low': c1_low,   'c1_high': c1_high}
    if 'vol' not in df.columns:
        return {**base, 'c3_vol': 0.0, 'vol_max10h': 0.0}
    c3_vol    = float(df['vol'].iloc[c3_idx])
    avg_start = max(0, c3_idx - 5)
    vol_max   = float(df['vol'].iloc[avg_start:c3_idx].max()) if c3_idx > 0 else 0.0
    return {**base, 'c3_vol': c3_vol, 'vol_max10h': vol_max}


def get_internal_gaps(df, stype, bos_idx, lookback=60):
    gaps = []
    scan_start = max(2, bos_idx - lookback)

    # Pre-BOS FVG  (C1=i-2, C2=i-1, C3=i)
    for i in range(bos_idx - 1, scan_start, -1):
        gap = None
        if stype == "Long" and df['high'].iloc[i-2] < df['low'].iloc[i]:
            if not (df['close'].iloc[i-2] > df['open'].iloc[i-2] and
                    df['close'].iloc[i-1] > df['open'].iloc[i-1] and
                    df['close'].iloc[i]   > df['open'].iloc[i]):
                continue
            gap = {"top": df['low'].iloc[i], "bottom": df['high'].iloc[i-2], "zone": "pre"}
            gap.update(_gap_vol_fields(df, i))
        elif stype == "Short" and df['low'].iloc[i-2] > df['high'].iloc[i]:
            if not (df['close'].iloc[i-2] < df['open'].iloc[i-2] and
                    df['close'].iloc[i-1] < df['open'].iloc[i-1] and
                    df['close'].iloc[i]   < df['open'].iloc[i]):
                continue
            gap = {"top": df['low'].iloc[i-2], "bottom": df['high'].iloc[i], "zone": "pre"}
            gap.update(_gap_vol_fields(df, i))
        if gap:
            is_fresh = True
            for j in range(i + 1, bos_idx + 1):
                if stype == "Long"  and df['close'].iloc[j] < gap['bottom']: is_fresh = False; break
                if stype == "Short" and df['close'].iloc[j] > gap['top']:    is_fresh = False; break
            if is_fresh:
                gaps.append(gap)

    # Post-BOS FVG  (C1=i-1, C2=i, C3=i+1)
    post_end = len(df) - 2
    for i in range(bos_idx + 1, post_end):
        if i + 1 >= len(df): continue
        gap = None
        if stype == "Long" and df['high'].iloc[i-1] < df['low'].iloc[i+1]:
            if not (df['close'].iloc[i-1] > df['open'].iloc[i-1] and
                    df['close'].iloc[i]   > df['open'].iloc[i]   and
                    df['close'].iloc[i+1] > df['open'].iloc[i+1]):
                continue
            gap = {"top": df['low'].iloc[i+1], "bottom": df['high'].iloc[i-1], "zone": "post"}
            gap.update(_gap_vol_fields(df, i + 1))
        elif stype == "Short" and df['low'].iloc[i-1] > df['high'].iloc[i+1]:
            if not (df['close'].iloc[i-1] < df['open'].iloc[i-1] and
                    df['close'].iloc[i]   < df['open'].iloc[i]   and
                    df['close'].iloc[i+1] < df['open'].iloc[i+1]):
                continue
            gap = {"top": df['low'].iloc[i-1], "bottom": df['high'].iloc[i+1], "zone": "post"}
            gap.update(_gap_vol_fields(df, i + 1))
        if gap:
            is_fresh = True
            for j in range(i + 2, len(df)):
                if stype == "Long"  and df['close'].iloc[j] < gap['bottom']: is_fresh = False; break
                if stype == "Short" and df['close'].iloc[j] > gap['top']:    is_fresh = False; break
            if is_fresh:
                gaps.append(gap)

    if stype == "Long":
        gaps.sort(key=lambda g: g['top'], reverse=True)
    else:
        gaps.sort(key=lambda g: g['bottom'])
    return gaps


def fvg_fully_broken(candle, fvg, stype):
    if stype == "Long":  return candle['close'] < fvg['bottom']
    else:                return candle['close'] > fvg['top']

def candle_touches_fvg(candle, fvg, stype):
    if stype == "Long":
        return candle['low'] <= fvg['top'] and not fvg_fully_broken(candle, fvg, stype)
    else:
        return candle['high'] >= fvg['bottom'] and not fvg_fully_broken(candle, fvg, stype)


def _get_strong_fvgs(df_h1, stype, bos_idx, choch_level=None):
    """FVG kuat: C3 vol > avg20H, c3_open ada, c1_close ada, CHOCH filter, MAX_GAP_PCT filter."""
    gaps = get_internal_gaps(df_h1, stype, bos_idx)
    # Hanya FVG dengan volume kuat + C1/C3 fields valid
    gaps = [g for g in gaps
            if g.get('c3_vol', 0) > g.get('vol_max10h', 0) > 0
            and g.get('c3_open', 0) > 0
            and g.get('c1_close', 0) > 0]
    # Filter FVG yang straddle CHOCH
    if choch_level:
        if stype == "Long":
            gaps = [g for g in gaps if g['bottom'] >= choch_level]
        else:
            gaps = [g for g in gaps if g['top'] <= choch_level]
    # MAX_GAP_PCT: gap tidak boleh terlalu besar
    result = []
    for g in gaps:
        gap_size = g['top'] - g['bottom']
        ocl      = float(g.get('c3_open', g['bottom'] if stype == 'Short' else g['top']))
        if ocl > 0 and MAX_GAP_PCT > 0 and gap_size / ocl > MAX_GAP_PCT:
            continue
        result.append(g)
    return result


def _scan_fvg_only(df_h1):
    """
    FVG-only mode: scan kedua arah, pilih FVG kuat paling recent (c3_idx terbesar).
    Tidak butuh BOS — langsung dari H1 data.
    """
    best_c3i = -1
    chosen = None; best_stype = None
    for s in ['Long', 'Short']:
        gaps = get_internal_gaps(df_h1, s, len(df_h1) - 1)
        strong = [g for g in gaps
                  if g.get('c3_vol', 0) > g.get('vol_max10h', 0) > 0
                  and g.get('c1_close', 0) > 0]
        for g in reversed(strong):  # paling recent dulu
            c1_c = float(g.get('c1_close', 0))
            c1_l = float(g.get('c1_low',   0))
            c1_h = float(g.get('c1_high',  0))
            if c1_c <= 0 or c1_h <= c1_l: continue
            c1_mid = (c1_h + c1_l) / 2.0
            if s == 'Long'  and c1_c <= c1_mid: continue
            if s == 'Short' and c1_c >= c1_mid: continue
            gap_sz = float(g['top']) - float(g['bottom'])
            if c1_c > 0 and MAX_GAP_PCT > 0 and gap_sz / c1_c > MAX_GAP_PCT: continue
            c3i = g.get('c3_idx', 0)
            if c3i > best_c3i:
                best_c3i = c3i; chosen = g; best_stype = s
            break  # reversed → yang pertama lolos sudah paling recent untuk arah ini
    return chosen, best_stype


# ============================================================
# FUNGSI ORDER
# ============================================================

def place_market_order(symbol, side, entry, sl, trail_dist):
    """
    Market order dengan trailing stop.
    trail_dist = jarak trailing dalam harga (= TRAIL_STOP × dist).
    SL awal = entry - dist (Long) / entry + dist (Short).
    """
    try:
        info    = get_instrument_info(symbol)
        res_bal = session.get_wallet_balance(accountType="UNIFIED", coin="USDT")
        acct    = res_bal['result']['list'][0]
        balance = float(acct['totalEquity'])
        avail   = float(acct.get('totalAvailableBalance') or balance)
        risk_usd = balance * 0.01
        dist     = abs(entry - sl)
        if dist == 0:
            print(f"⚠️ {symbol}: dist entry-SL = 0, skip.")
            return None

        min_dist = entry * 0.002   # 0.2% — sinkron dengan outer check dan backtest MIN_DIST_PCT
        if dist < min_dist:
            dist = min_dist
            sl   = entry - dist if side == "Buy" else entry + dist

        raw_qty = risk_usd / dist
        qty     = round_qty(raw_qty, info['qty_step'])
        if qty < info['min_qty']:
            print(f"⚠️ {symbol}: Qty {qty} < minOrderQty {info['min_qty']}, skip.")
            return None

        sl_r         = round_price(sl,         info['tick_size'])
        trail_dist_r = round_price(trail_dist,  info['tick_size'])
        if trail_dist_r <= 0:
            trail_dist_r = round_price(dist * TRAIL_STOP, info['tick_size'])

        lev_int = 10
        try:
            max_lev = float(info.get('max_leverage', 10))
            lev_int = int(min(10, max_lev))
            res_lev = session.set_leverage(category=CATEGORY, symbol=symbol,
                                           buyLeverage=str(lev_int), sellLeverage=str(lev_int))
            if res_lev.get('retCode', -1) not in (0, 110043):
                print(f"   ⚠️ {symbol}: set_leverage gagal: {res_lev.get('retMsg','')} "
                      f"(code:{res_lev.get('retCode')}) — coba lanjut")
        except Exception as e:
            if '110043' not in str(e):
                print(f"   ⚠️ {symbol}: set_leverage error: {e} — coba lanjut")

        required_margin = (qty * entry) / lev_int
        if required_margin > avail * 0.9:
            print(f"⚠️ {symbol}: Margin tidak cukup — butuh ~${required_margin:.2f} "
                  f"(lev {lev_int}x), avail ${avail:.2f} / equity ${balance:.2f}. Skip.")
            return None

        print(f"   Balance:{balance:.2f} Avail:{avail:.2f} Risk:{risk_usd:.2f} Dist:{dist:.6f} "
              f"Trail:{trail_dist:.6f} Qty:{qty} SL:{sl_r} Lev:{lev_int}x "
              f"Margin:~${required_margin:.2f}")

        res = session.place_order(
            category=CATEGORY, symbol=symbol, side=side,
            orderType="Market", qty=str(qty),
            stopLoss=str(sl_r),
            positionIdx=0,
            timeInForce="IOC"
        )
        if res['retCode'] == 0:
            return res['result']['orderId']
        print(f"⚠️ {symbol}: Order ditolak → {res.get('retMsg','')} (code:{res['retCode']})")
        return None
    except Exception as e:
        print(f"⚠️ {symbol}: place_order error → {e}")
        return None


def close_position(symbol, side, qty_str):
    """
    Force-close posisi dengan market order reduceOnly.
    Dipakai untuk trail timeout: tutup posisi yang peak-nya stuck 3 hari.
    """
    try:
        close_side = 'Sell' if side == 'Buy' else 'Buy'
        info  = get_instrument_info(symbol)
        qty_r = round_qty(float(qty_str), info['qty_step'])
        if qty_r <= 0:
            print(f"⚠️ {symbol}: close_position qty=0, skip.")
            return False
        res = session.place_order(
            category=CATEGORY, symbol=symbol,
            side=close_side, orderType="Market",
            qty=str(qty_r), reduceOnly=True,
            positionIdx=0, timeInForce="IOC"
        )
        if res.get('retCode') == 0:
            print(f"⏹️  {symbol}: Posisi ditutup (trail timeout) @ market")
            return True
        print(f"⚠️ {symbol}: close_position gagal → {res.get('retMsg','')} (code:{res.get('retCode')})")
        return False
    except Exception as e:
        print(f"⚠️ {symbol}: close_position error → {e}")
        return False


def place_limit_order(symbol, side, entry_p, sl_p):
    """
    Limit order GTC di entry_p, SL + trailing stop langsung dalam satu order.
    """
    try:
        info    = get_instrument_info(symbol)
        res_bal = session.get_wallet_balance(accountType="UNIFIED", coin="USDT")
        acct    = res_bal['result']['list'][0]
        balance = float(acct['totalEquity'])
        avail   = float(acct.get('totalAvailableBalance') or balance)
        risk_usd = balance * 0.01
        dist     = abs(entry_p - sl_p)
        if dist == 0:
            print(f"⚠️ {symbol}: dist entry-SL = 0, skip.")
            return None

        min_dist = entry_p * 0.002
        if dist < min_dist:
            dist  = min_dist
            sl_p  = entry_p - dist if side == "Buy" else entry_p + dist

        raw_qty = risk_usd / dist
        qty     = round_qty(raw_qty, info['qty_step'])
        if qty < info['min_qty']:
            print(f"⚠️ {symbol}: Qty {qty} < minOrderQty {info['min_qty']}, skip.")
            return None

        entry_r  = round_price(entry_p,                     info['tick_size'])
        sl_r     = round_price(sl_p,                        info['tick_size'])
        trail_r  = round_price(TRAIL_STOP * dist,           info['tick_size'])
        active_r = round_price(
            entry_p + TRAIL_ACT_R * dist if side == "Buy"
            else entry_p - TRAIL_ACT_R * dist,             info['tick_size'])

        lev_int = 10
        try:
            max_lev = float(info.get('max_leverage', 10))
            lev_int = int(min(10, max_lev))
            res_lev = session.set_leverage(category=CATEGORY, symbol=symbol,
                                           buyLeverage=str(lev_int), sellLeverage=str(lev_int))
            if res_lev.get('retCode', -1) not in (0, 110043):   # 110043 = sudah di leverage ini
                print(f"   ⚠️ {symbol}: set_leverage gagal: {res_lev.get('retMsg','')} "
                      f"(code:{res_lev.get('retCode')}) — coba lanjut")
        except Exception as e:
            if '110043' not in str(e):
                print(f"   ⚠️ {symbol}: set_leverage error: {e} — coba lanjut")

        # Pre-check margin pakai available balance (bukan totalEquity) — sudah dikurangi open orders
        required_margin = (qty * entry_p) / lev_int
        if required_margin > avail * 0.9:
            print(f"⚠️ {symbol}: Margin tidak cukup — butuh ~${required_margin:.2f} "
                  f"(lev {lev_int}x), avail ${avail:.2f} / equity ${balance:.2f}. Skip.")
            return None

        print(f"   Balance:{balance:.2f} Avail:{avail:.2f} Risk:{risk_usd:.2f} Dist:{dist:.6f} "
              f"Trail:{trail_r} ActiveP:{active_r} Qty:{qty} Entry:{entry_r} SL:{sl_r} "
              f"Lev:{lev_int}x Margin:~${required_margin:.2f}")

        res = session.place_order(
            category=CATEGORY, symbol=symbol, side=side,
            orderType="Limit", qty=str(qty),
            price=str(entry_r),
            stopLoss=str(sl_r),
            trailingStop=str(trail_r),
            activePrice=str(active_r),
            positionIdx=0,
            timeInForce="GTC"
        )
        if res['retCode'] == 0:
            return res['result']['orderId']
        print(f"⚠️ {symbol}: Limit order ditolak → {res.get('retMsg','')} (code:{res['retCode']})")
        return None
    except Exception as e:
        print(f"⚠️ {symbol}: place_limit_order error → {e}")
        return None


def cancel_order(symbol, order_id):
    """Batalkan pending order di Bybit."""
    try:
        res = session.cancel_order(category=CATEGORY, symbol=symbol, orderId=order_id)
        if res['retCode'] == 0:
            print(f"   ✅ {symbol}: Order {order_id[:8]}… dibatalkan.")
        else:
            print(f"   ⚠️ {symbol}: Cancel gagal → {res.get('retMsg','')} (code:{res['retCode']})")
    except Exception as e:
        print(f"   ⚠️ {symbol}: cancel_order error → {e}")


def _order_exists(symbol, order_id):
    """True jika limit order masih aktif (belum filled/cancelled) di Bybit."""
    try:
        res = session.get_open_orders(category=CATEGORY, symbol=symbol, orderId=order_id)
        if res['retCode'] == 0:
            for o in res['result']['list']:
                if o.get('orderId') == order_id and \
                        o.get('orderStatus') in ('New', 'PartiallyFilled', 'Untriggered'):
                    return True
            return False
    except Exception:
        pass
    return False


def _order_was_filled(symbol, order_id):
    """True jika order sudah Filled (cek history Bybit)."""
    try:
        res = session.get_order_history(
            category=CATEGORY, symbol=symbol, orderId=order_id, limit=1
        )
        if res['retCode'] == 0 and res['result']['list']:
            return res['result']['list'][0].get('orderStatus') == 'Filled'
    except Exception:
        pass
    return False


def get_open_position(symbol):
    try:
        res = session.get_positions(category=CATEGORY, symbol=symbol)
        if res['retCode'] == 0:
            for pos in res['result']['list']:
                if float(pos['size']) > 0:
                    return pos
        return None
    except:
        return None


def move_sl(symbol, new_sl, side="Buy"):
    try:
        res = session.set_trading_stop(
            category=CATEGORY, symbol=symbol,
            stopLoss=str(new_sl),
            positionIdx=0
        )
        return res['retCode'] == 0
    except:
        return False


# ============================================================
# TRAILING SL + REVERSE POSITION
# ============================================================

def _get_actual_exit_price(symbol):
    """
    Query Bybit closed PnL untuk ambil harga exit actual posisi terakhir.
    Lebih akurat dari last_price (mark price di cek sebelumnya).
    """
    try:
        res = session.get_closed_pnl(category=CATEGORY, symbol=symbol, limit=1)
        if res['retCode'] == 0 and res['result']['list']:
            last = res['result']['list'][0]
            exit_p = float(last.get('avgExitPrice', 0))
            if exit_p > 0:
                return exit_p
    except Exception as e:
        print(f"⚠️ {symbol}: get_closed_pnl error: {e}")
    return None


def check_trailing_sl(coin):
    """
    Dipanggil setiap M5 close.
    Cek apakah posisi sudah tutup. Jika ya, update done_setups dan hapus dari active_positions.
    Jika masih buka, update last_price dan set trailing stop jika belum dipasang.
    """
    if coin not in active_positions:
        return

    p   = active_positions[coin]
    pos = get_open_position(coin)

    if pos is None:
        actual_exit = _get_actual_exit_price(coin)
        exit_str    = f"{actual_exit:.6f}" if actual_exit else "?"
        entry       = p['entry']
        side        = p['side']
        rev_count   = p.get('rev_count', 0)
        orig_ocl    = p.get('orig_ocl', entry)

        # Detect SL hit: exit at a loss (below entry for Long, above for Short)
        is_sl = (actual_exit is not None and (
            (side == 'Buy'  and actual_exit <= entry) or
            (side == 'Sell' and actual_exit >= entry)
        ))

        if is_sl and rev_count < 1:
            rev_side  = 'Sell' if side == 'Buy' else 'Buy'
            rev_stype = 'Short' if side == 'Buy' else 'Long'
            rev_dist  = p.get('dist', 0)
            rev_sl    = (actual_exit + rev_dist) if rev_side == 'Sell' else (actual_exit - rev_dist)
            rev_trail = TRAIL_STOP * rev_dist
            print(f"🔄 {coin}: SL hit @ {exit_str} — reverse {rev_stype} (rev#{rev_count+1})")
            order_id = place_market_order(coin, rev_side, actual_exit, rev_sl, rev_trail)
            if order_id:
                time.sleep(1)
                pos_new = get_open_position(coin)
                if pos_new:
                    rev_entry = float(pos_new.get('avgPrice', actual_exit or entry))
                    active_positions[coin] = {
                        'side'          : rev_side,
                        'entry'         : rev_entry,
                        'sl'            : rev_sl,
                        'dist'          : rev_dist,
                        'trail_dist'    : rev_trail,
                        'trail_engaged' : False,
                        'trail_set'     : False,
                        'last_price'    : rev_entry,
                        'entry_time'    : time.time(),
                        'peak'          : rev_entry,
                        'peak_time'     : time.time(),
                        'swing_val'     : p.get('swing_val'),
                        'bos_type'      : rev_stype,
                        'rev_count'     : rev_count + 1,
                        'orig_ocl'      : orig_ocl,
                    }
                    print(f"✅ {coin}: Reverse {rev_stype} entry:{rev_entry:.6f} sl:{rev_sl:.6f}")
                else:
                    print(f"⚠️ {coin}: Reverse order placed tapi posisi belum terdeteksi.")
                    done_setups[coin] = {
                        'swing_val': p.get('swing_val'),
                        'stype'    : p.get('bos_type'),
                        'used_ocl' : orig_ocl,
                    }
                    del active_positions[coin]
            else:
                print(f"⚠️ {coin}: Gagal open reverse — skip.")
                done_setups[coin] = {
                    'swing_val': p.get('swing_val'),
                    'stype'    : p.get('bos_type'),
                    'used_ocl' : orig_ocl,
                }
                del active_positions[coin]
        else:
            print(f"📭 {coin}: Posisi tutup @ {exit_str}.")
            done_setups[coin] = {
                'swing_val': p.get('swing_val'),
                'stype'    : p.get('bos_type'),
                'used_ocl' : orig_ocl,
            }
            del active_positions[coin]
        return

    # Posisi masih buka — update last_price, peak, dan cek trail timeout
    try:
        curr_price = float(pos['markPrice'])
        active_positions[coin]['last_price'] = curr_price

        entry = p['entry']
        dist  = p.get('dist', 0)
        side  = p['side']

        # Track peak (favorable extreme) dan waktu terakhir peak bergerak
        peak      = p.get('peak', entry)
        peak_time = p.get('peak_time', p.get('entry_time', time.time()))
        new_peak  = max(peak, curr_price) if side == 'Buy' else min(peak, curr_price)
        if new_peak != peak:
            active_positions[coin]['peak']      = new_peak
            active_positions[coin]['peak_time'] = time.time()
            peak_time = time.time()

        # Trail timeout: close jika peak tidak bergerak selama TRAIL_TIMEOUT_DAYS hari
        timeout_sec = TRAIL_TIMEOUT_DAYS * 24 * 3600
        if time.time() - peak_time > timeout_sec:
            qty_pos = pos.get('size', '0')
            hours_stuck = (time.time() - peak_time) / 3600
            print(f"⏰ {coin}: Trail timeout {TRAIL_TIMEOUT_DAYS} hari "
                  f"(peak stuck {hours_stuck:.1f}h) — force close @ market")
            if close_position(coin, side, qty_pos):
                done_setups[coin] = {
                    'swing_val': p.get('swing_val'),
                    'stype'    : p.get('bos_type'),
                    'used_ocl' : p.get('orig_ocl', entry),
                }
                del active_positions[coin]
            return

        # Pasang trailing stop via set_trading_stop saat pertama posisi terdeteksi
        # activePrice = entry + TRAIL_ACT_R×dist → trail aktif setelah +1.5R profit (sinkron backtest)
        if TRAIL_STOP > 0 and dist > 0 and not p.get('trail_set', False):
            trail_dist = p.get('trail_dist', TRAIL_STOP * dist)
            info       = get_instrument_info(coin)
            tick       = info.get('tick_size', 0.0001)
            trail_r    = round_price(trail_dist, tick)
            active_p   = round_price(entry + TRAIL_ACT_R * dist if side == "Buy" else entry - TRAIL_ACT_R * dist, tick)
            print(f"🔧 {coin}: Pasang trail: trailingStop={trail_r} activePrice={active_p} "
                  f"(entry={entry:.6f} dist={dist:.6f} = {dist/entry*100:.3f}%, act={TRAIL_ACT_R}R)")
            if trail_r > 0 and active_p > 0:
                try:
                    res_ts = session.set_trading_stop(
                        category=CATEGORY, symbol=coin,
                        trailingStop=str(trail_r),
                        activePrice=str(active_p),
                        positionIdx=0
                    )
                    if res_ts['retCode'] == 0:
                        active_positions[coin]['trail_set'] = True
                        print(f"📍 {coin}: Trailing stop {trail_r} dipasang "
                              f"(aktif @ {active_p} = entry+{TRAIL_ACT_R}R)")
                    else:
                        print(f"⚠️ {coin}: Gagal set trailing stop: "
                              f"{res_ts.get('retMsg','')} (code:{res_ts['retCode']})")
                except Exception as e:
                    print(f"⚠️ {coin}: set_trading_stop error: {e}")

        if dist > 0 and not p.get('trail_engaged', False):
            if side == "Buy"  and curr_price >= entry + TRAIL_ACT_R * dist:
                active_positions[coin]['trail_engaged'] = True
                print(f"✅ {coin}: Trail engaged @ {curr_price:.6f} (+{TRAIL_ACT_R}R)")
            elif side == "Sell" and curr_price <= entry - TRAIL_ACT_R * dist:
                active_positions[coin]['trail_engaged'] = True
                print(f"✅ {coin}: Trail engaged @ {curr_price:.6f} (+{TRAIL_ACT_R}R)")
    except Exception:
        pass


# ============================================================
# KONEKSI
# ============================================================

def test_connection():
    try:
        res = session.get_server_time()
        if res['retCode'] == 0:
            print(f"✅ Koneksi Bybit OK | Server time: {res['result']['timeSecond']}")
            return True
        print(f"❌ Bybit error: {res}")
        return False
    except Exception as e:
        print(f"❌ Gagal konek: {e}")
        return False


# ============================================================
# REPLAY H1 — reconstruct state saat startup (fvg_strong)
# ============================================================

def replay_h1(coin, df_h1):
    sh_h1, sl_h1 = find_last_swing_bos(df_h1)
    if not sh_h1 or not sl_h1:
        return None

    closed_h1 = df_h1.iloc[-2]
    is_long = False; is_short = False
    swing_val = None; bos_idx = None

    for sh in sh_h1[-3:]:
        if closed_h1['close'] > sh['val']:
            is_long   = True
            swing_val = sh['val']
            bos_idx   = sl_h1[-1]['idx'] if sl_h1 else sh['idx']
    for sl in sl_h1[-3:]:
        if closed_h1['close'] < sl['val']:
            is_short  = True
            swing_val = sl['val']
            bos_idx   = sh_h1[-1]['idx'] if sh_h1 else sl['idx']

    if not (is_long or is_short):
        return None

    stype = "Short" if is_short else "Long"

    if stype == "Long":
        sl_below    = [s for s in sl_h1 if s['val'] < swing_val]
        choch_level = sl_below[-1]['val'] if sl_below else None
    else:
        sh_above    = [s for s in sh_h1 if s['val'] > swing_val]
        choch_level = sh_above[-1]['val'] if sh_above else None

    gaps = _get_strong_fvgs(df_h1, stype, bos_idx, choch_level)
    if not gaps:
        return None

    bos_ts = df_h1['ts'].iloc[bos_idx]
    state  = {
        'type'        : stype,
        'phase'       : 'WAIT_FVG_TOUCH',
        'fvg_list'    : gaps,
        'fvg_idx'     : 0,
        'bos_ts'      : bos_ts,
        'bos_idx'     : bos_idx,
        'swing_val'   : swing_val,
        'choch_level' : choch_level,
    }

    choch_str = f"{choch_level:.6g}" if choch_level else "—"
    print(f"\n📊 {coin}: BOS {stype} | Swing: {swing_val:.6g} | {len(gaps)} FVG kuat")
    print(f"   ⛔ CHOCH batal: {choch_str}")
    for gi, g in enumerate(gaps):
        ocl      = g.get('c3_open', 0)
        sbr_lvl  = g.get('c1_close', 0)
        gap_size = g['top'] - g['bottom']
        ref_p    = ocl if ocl > 0 else (g['bottom'] if stype == 'Short' else g['top'])
        lbl      = ("RBS" if stype == "Long" else "SBR") if SBR_MODE else "OCL"
        entry_v  = sbr_lvl if SBR_MODE and sbr_lvl > 0 else ocl
        mode_lbl = f"{lbl}:{entry_v:.6g}"
        print(f"   FVG {gi+1}: bot:{g['bottom']:.6g} top:{g['top']:.6g} "
              f"{mode_lbl} gap:{abs(gap_size)/ref_p*100:.3f}%" if ref_p > 0 else
              f"   FVG {gi+1}: bot:{g['bottom']:.6g} top:{g['top']:.6g} {mode_lbl}")
    return state


def reconstruct_state():
    for coin in SYMBOLS:
        try:
            time.sleep(1)
            df_h1 = get_data(coin, "60", limit=100)
            if df_h1 is None:
                continue
            state = replay_h1(coin, df_h1)
            if state:
                pending[coin] = state
        except Exception as e:
            print(f"⚠️ Replay {coin}: {e}")
    print(f"🔍 Selesai. {len(pending)} coin dimonitor.\n")


# ============================================================
# CORE LOOP — fvg_strong strategy
# BOS H1 → FVG kuat (C3 vol > avg20H) → OCL touch M5
# → Touch vol filter → Entry market + trailing stop
# ============================================================

def run_bot():
    print("SMC FVG STRONG BOT — BOS H1 → Strong FVG → OCL M5 Entry")
    if not test_connection():
        print("⛔ Tidak bisa konek ke Bybit.")
        return
    if ENTRY_MODE != 'fvg_limit':
        reconstruct_state()

    while True:
        now      = time.time()
        sec      = now % 300
        wait_sec = 300 - sec + 2
        if wait_sec > 300:
            wait_sec = 2
        print(f"⏱️  Tunggu candle M5 close: {wait_sec:.0f} detik...")
        time.sleep(wait_sec)

        # Cek trailing SL semua posisi aktif
        for coin in list(active_positions.keys()):
            try:
                check_trailing_sl(coin)
            except Exception as e:
                print(f"⚠️ Trailing SL {coin}: {e}")

        # Summary slot setiap M5
        n_active   = len(active_positions)
        n_waitfill = sum(1 for s in pending.values() if s.get('phase') == 'WAIT_FILL')
        n_approach = sum(1 for s in pending.values() if s.get('phase') == 'WAIT_APPROACH')
        slots_used = n_active + n_waitfill
        print(f"\n{'='*55}")
        print(f"📊 SLOT: {slots_used}/{MAX_CONCURRENT} terpakai "
              f"(posisi:{n_active} | limit:{n_waitfill} | watch:{n_approach})")
        if active_positions:
            print(f"   Aktif: {', '.join(active_positions.keys())}")
        if pending:
            for c, s in pending.items():
                ph = s.get('phase','?')
                print(f"   {c}: {ph} {s.get('type','?')} @ {s.get('entry',0):.6g}")
        print(f"{'='*55}")

        for coin in SYMBOLS:
            try:
                time.sleep(3)

                # Jika posisi aktif sedang jalan, tidak buka setup baru
                if coin in active_positions:
                    continue

                df_h1_live = get_data(coin, "60", limit=100)
                if df_h1_live is None:
                    continue

                if REQUIRE_BOS:
                    sh_h1, sl_h1 = find_last_swing_bos(df_h1_live)
                    if not sh_h1 or not sl_h1:
                        continue
                else:
                    sh_h1, sl_h1 = None, None

                closed_h1 = df_h1_live.iloc[-2]
                curr_h1   = df_h1_live.iloc[-1]

                # ── PROSES SETUP PENDING ─────────────────────────────
                if coin in pending:
                    setup    = pending[coin]
                    stype    = setup['type']
                    bos_idx  = setup.get('bos_idx', 0)
                    swing_val = setup.get('swing_val')

                    # CHOCH check — struktur batal
                    choch_level = setup.get('choch_level')
                    if choch_level:
                        if stype == "Long"  and curr_h1['close'] < choch_level:
                            if setup.get('order_id'):
                                cancel_order(coin, setup['order_id'])
                            done_setups.pop(coin, None)   # struktur reset → boleh re-entry nanti
                            print(f"🔄 {coin}: CHOCH — swing low {choch_level:.6f} ditembus. Setup batal.")
                            del pending[coin]; continue
                        if stype == "Short" and curr_h1['close'] > choch_level:
                            if setup.get('order_id'):
                                cancel_order(coin, setup['order_id'])
                            done_setups.pop(coin, None)
                            print(f"🔄 {coin}: CHOCH — swing high {choch_level:.6f} ditembus. Setup batal.")
                            del pending[coin]; continue

                    if setup.get('fvg_only'):
                        # FVG-only: cek apakah ada FVG lebih baru → ganti setup
                        if setup.get('phase') == 'WAIT_APPROACH':
                            new_ch, new_st = _scan_fvg_only(df_h1_live)
                            if new_ch:
                                new_ocl = float(new_ch.get('c1_close', 0))
                                old_ocl = setup.get('orig_ocl', 0)
                                ocl_changed = (old_ocl <= 0 or
                                               abs(new_ocl - old_ocl) / max(new_ocl, 1e-9) > 0.001 or
                                               new_st != setup['type'])
                                if ocl_changed:
                                    print(f"🔄 {coin}: FVG-only — FVG baru ({new_st} @ {new_ocl:.6f}) "
                                          f"gantikan lama ({setup['type']} @ {old_ocl:.6f})")
                                    del pending[coin]
                                    # Buat pending baru dari FVG ini
                                    c1_c = new_ocl
                                    c1_l = float(new_ch.get('c1_low', 0))
                                    c1_h = float(new_ch.get('c1_high', 0))
                                    if new_st == 'Long':
                                        dist_n = max(c1_c - c1_l, 0.0)
                                        entry_n = c1_c; sl_n = c1_l
                                    else:
                                        dist_n = max(c1_h - c1_c, 0.0)
                                        entry_n = c1_c; sl_n = c1_h
                                    if dist_n >= c1_c * 0.002:
                                        pending[coin] = {
                                            'type': new_st, 'phase': 'WAIT_APPROACH',
                                            'entry': entry_n, 'sl': sl_n, 'dist': dist_n,
                                            'orig_ocl': c1_c, 'choch_level': None,
                                            'swing_val': None, 'fvg_only': True,
                                        }
                                        print(f"   ✅ Setup baru: {new_st} Entry:{entry_n:.6f} "
                                              f"SL:{sl_n:.6f} dist:{dist_n/c1_c*100:.3f}%")
                                    continue
                    else:
                        # BOS mode: refresh FVG list dari bos_idx
                        bos_ts_val = setup.get('bos_ts', 0)
                        bos_rows   = df_h1_live.index[df_h1_live['ts'] == bos_ts_val]
                        if len(bos_rows) > 0:
                            bos_idx = int(bos_rows[0])
                            pending[coin]['bos_idx'] = bos_idx
                        if bos_idx < len(df_h1_live):
                            fresh_gaps = _get_strong_fvgs(df_h1_live, stype, bos_idx, choch_level)
                            if fresh_gaps:
                                pending[coin]['fvg_list'] = fresh_gaps
                        else:
                            print(f"⚠️ {coin}: bos_idx {bos_idx} out of range H1 len={len(df_h1_live)}")

                        fvg_list = pending[coin]['fvg_list']
                        if not fvg_list:
                            if setup.get('order_id'):
                                cancel_order(coin, setup['order_id'])
                            print(f"🗑️ {coin}: Tidak ada FVG kuat tersisa.")
                            del pending[coin]; continue

                    # ── WAIT_APPROACH: monitoring harga mendekati FVG, belum pasang order ──
                    if setup['phase'] == 'WAIT_APPROACH':
                        curr_price = float(curr_h1['close'])
                        entry  = setup['entry']
                        dist   = setup['dist']
                        thr    = APPROACH_R * dist
                        approaching = (stype == 'Long'  and curr_price <= entry + thr) or \
                                      (stype == 'Short' and curr_price >= entry - thr)
                        # Log status approach setiap iterasi
                        if stype == 'Long':
                            approach_thr = entry + thr
                            gap_to_thr   = curr_price - approach_thr   # negatif = belum sampai
                        else:
                            approach_thr = entry - thr
                            gap_to_thr   = approach_thr - curr_price   # negatif = belum sampai
                        gap_pct = gap_to_thr / entry * 100
                        if approaching:
                            status_str = f"✅ DALAM RANGE"
                        else:
                            status_str = f"⏳ kurang {abs(gap_pct):.2f}% ke threshold"
                        print(f"👁️  {coin} WAIT | {stype} | now:{curr_price:.6f} "
                              f"entry:{entry:.6f} thr:{approach_thr:.6f} | {status_str}")

                        if approaching:
                            # Validasi arah: limit hanya valid jika harga belum melewati OCL
                            # Long: curr > entry (harga di atas, menunggu turun ke OCL)
                            # Short: curr < entry (harga di bawah, menunggu naik ke OCL)
                            direction_valid = (stype == 'Long'  and curr_price > entry) or \
                                              (stype == 'Short' and curr_price < entry)
                            if not direction_valid:
                                print(f"⛔ {coin}: Harga {curr_price:.6f} sudah melewati OCL "
                                      f"{entry:.6f} ({stype}) — setup dibatalkan.")
                                done_setups.pop(coin, None)
                                del pending[coin]; continue

                            active_count = len(active_positions) + sum(
                                1 for s in pending.values() if s.get('phase') == 'WAIT_FILL')
                            if active_count >= MAX_CONCURRENT:
                                print(f"⏸️  {coin}: Harga mendekati tapi slot penuh "
                                      f"({active_count}/{MAX_CONCURRENT})")
                            else:
                                side_order = "Buy" if stype == "Long" else "Sell"
                                order_id   = place_limit_order(coin, side_order, entry, setup['sl'])
                                if order_id:
                                    setup['phase']    = 'WAIT_FILL'
                                    setup['order_id'] = order_id
                                    print(f"📍 {coin}: Limit dipasang @ {entry:.6f} "
                                          f"(harga {curr_price:.6f} dalam {APPROACH_R}R)")
                        continue

                    # ── WAIT_FILL: limit order placed, nunggu fill ──────
                    if setup['phase'] == 'WAIT_FILL':
                        # Jika harga mundur keluar approach range → cancel, kembali WAIT_APPROACH
                        curr_price = float(curr_h1['close'])
                        entry_w = setup['entry']; dist_w = setup['dist']
                        thr_w   = APPROACH_R * dist_w
                        price_away = (stype == 'Long'  and curr_price > entry_w + thr_w) or \
                                     (stype == 'Short' and curr_price < entry_w - thr_w)
                        if price_away:
                            oid = setup.get('order_id')
                            if oid:
                                cancel_order(coin, oid)
                            setup['phase'] = 'WAIT_APPROACH'
                            setup.pop('order_id', None)
                            print(f"📤 {coin}: Limit dibatalkan (harga {curr_price:.6f} mundur "
                                  f"> {APPROACH_R}R dari {entry_w:.6f}). Kembali menunggu.")
                            continue

                        pos = get_open_position(coin)
                        if pos:
                            entry_p    = setup['entry']
                            sl_p       = setup['sl']
                            side_order = "Buy" if stype == "Long" else "Sell"
                            actual_entry = float(pos.get('avgPrice', entry_p))

                            # Recalc dist dari actual fill price
                            actual_dist = abs(actual_entry - sl_p)
                            min_dist    = actual_entry * 0.002
                            if actual_dist < min_dist:
                                actual_dist = min_dist
                                sl_p = actual_entry - actual_dist if side_order == "Buy" \
                                       else actual_entry + actual_dist
                                print(f"⚠️ {coin}: Fill {actual_entry:.6f} vs OCL "
                                      f"{entry_p:.6f} — SL diperlebar ke {sl_p:.6f}")

                            trail_d  = TRAIL_STOP * actual_dist
                            info     = get_instrument_info(coin)
                            tick     = info.get('tick_size', 0.0001)
                            sl_r     = round_price(sl_p, tick)
                            trail_r  = round_price(trail_d, tick)
                            active_p = round_price(
                                actual_entry + TRAIL_ACT_R * actual_dist if side_order == "Buy"
                                else actual_entry - TRAIL_ACT_R * actual_dist, tick)

                            # Selalu panggil set_trading_stop saat fill — Bybit tidak honor
                            # trailingStop/activePrice pada limit order yang belum terisi.
                            trail_set_ok = False
                            for _attempt in range(3):
                                try:
                                    res_ts = session.set_trading_stop(
                                        category=CATEGORY, symbol=coin,
                                        stopLoss=str(sl_r),
                                        trailingStop=str(trail_r),
                                        activePrice=str(active_p),
                                        positionIdx=0
                                    )
                                    if res_ts.get('retCode', -1) == 0:
                                        trail_set_ok = True
                                        print(f"🛡️  {coin}: SL={sl_r} Trail={trail_r} "
                                              f"activePrice={active_p} (+{TRAIL_ACT_R}R) dipasang")
                                        break
                                    else:
                                        print(f"⚠️ {coin}: set_trading_stop gagal (attempt {_attempt+1}): "
                                              f"{res_ts.get('retMsg','')} (code:{res_ts.get('retCode')})")
                                        time.sleep(2)
                                except Exception as e:
                                    print(f"⚠️ {coin}: set_trading_stop error (attempt {_attempt+1}): {e}")
                                    time.sleep(2)
                            if not trail_set_ok:
                                print(f"⚠️ {coin}: Trail gagal — retry di M5 berikutnya")
                            active_positions[coin] = {
                                'side'          : side_order,
                                'entry'         : actual_entry,
                                'sl'            : sl_p,
                                'dist'          : actual_dist,
                                'trail_dist'    : trail_d,
                                'trail_engaged' : False,
                                'trail_set'     : trail_set_ok,  # False = retry by check_trailing_sl
                                'last_price'    : actual_entry,
                                'entry_time'    : time.time(),
                                'peak'          : actual_entry,
                                'peak_time'     : time.time(),
                                'swing_val'     : setup.get('swing_val'),
                                'bos_type'      : stype,
                                'rev_count'     : 0,
                                'orig_ocl'      : setup.get('orig_ocl', setup.get('entry')),
                            }
                            done_setups[coin] = {
                                'swing_val': setup.get('swing_val'),
                                'stype'    : stype,
                                'used_ocl' : setup.get('entry'),
                            }
                            del pending[coin]
                            print(f"✅ {coin}: Limit filled! Entry:{actual_entry:.6f} "
                                  f"SL:{sl_p:.6f} Trail aktif setelah +{TRAIL_ACT_R}R")
                        else:
                            # Posisi belum terbuka — cek apakah order masih ada di Bybit
                            oid = setup.get('order_id')
                            if oid and not _order_exists(coin, oid):
                                # Order hilang dari open orders — cek apakah sudah filled atau cancel
                                was_filled = _order_was_filled(coin, oid)
                                if was_filled:
                                    # Filled + SL kena dlm 1 candle → coba reverse
                                    exit_p = _get_actual_exit_price(coin)
                                    exit_str = f"{exit_p:.6f}" if exit_p else "?"
                                    print(f"⚠️ {coin}: Limit filled+SL 1 candle @ {exit_str} "
                                          f"→ coba reverse.")
                                    done_setups[coin] = {
                                        'swing_val': setup.get('swing_val'),
                                        'stype'    : stype,
                                        'used_ocl' : setup.get('entry'),
                                    }
                                    del pending[coin]
                                    if exit_p:
                                        dist_s   = setup.get('dist', 0)
                                        rev_side = 'Sell' if stype == 'Long' else 'Buy'
                                        rev_stype = 'Short' if stype == 'Long' else 'Long'
                                        rev_sl   = (exit_p + dist_s) if rev_side == 'Sell' \
                                                   else (exit_p - dist_s)
                                        rev_trail = TRAIL_STOP * dist_s
                                        print(f"🔄 {coin}: Reverse {rev_stype} @ {exit_p:.6f} "
                                              f"(rev#1)")
                                        rev_oid = place_market_order(
                                            coin, rev_side, exit_p, rev_sl, rev_trail)
                                        if rev_oid:
                                            time.sleep(1)
                                            pos_rev = get_open_position(coin)
                                            if pos_rev:
                                                rev_entry = float(
                                                    pos_rev.get('avgPrice', exit_p))
                                                active_positions[coin] = {
                                                    'side'          : rev_side,
                                                    'entry'         : rev_entry,
                                                    'sl'            : rev_sl,
                                                    'dist'          : dist_s,
                                                    'trail_dist'    : rev_trail,
                                                    'trail_engaged' : False,
                                                    'trail_set'     : False,
                                                    'last_price'    : rev_entry,
                                                    'entry_time'    : time.time(),
                                                    'swing_val'     : setup.get('swing_val'),
                                                    'bos_type'      : rev_stype,
                                                    'rev_count'     : 1,
                                                    'orig_ocl'      : setup.get(
                                                        'orig_ocl', setup.get('entry')),
                                                }
                                                print(f"✅ {coin}: Reverse {rev_stype} "
                                                      f"entry:{rev_entry:.6f} sl:{rev_sl:.6f}")
                                            else:
                                                print(f"⚠️ {coin}: Reverse placed tapi posisi "
                                                      f"belum terdeteksi.")
                                else:
                                    # Dibatalkan (bukan filled) — setup selesai
                                    print(f"⚠️ {coin}: Limit order dibatalkan. Setup selesai.")
                                    done_setups[coin] = {
                                        'swing_val': setup.get('swing_val'),
                                        'stype'    : stype,
                                        'used_ocl' : setup.get('entry'),
                                    }
                                    del pending[coin]
                            else:
                                print(f"⏳ {coin}: Nunggu fill limit @ {setup['entry']:.6f} | "
                                      f"SL:{setup['sl']:.6f} | {stype} | H1:{curr_h1['close']:.6g}")
                        continue

                    # ── WAIT_FVG_TOUCH: scan M5 untuk OCL touch ──────
                    if setup['phase'] == 'WAIT_FVG_TOUCH':
                        # Ambil M5 terbaru — hanya butuh beberapa candle terakhir
                        df_m5 = get_data(coin, "5", limit=30)
                        if df_m5 is None:
                            continue

                        # Candle-candle yang valid: exclude candle live terakhir
                        df_m5_closed = df_m5.iloc[:-1]

                        found = False
                        for fvg in fvg_list:
                            gap_size = float(fvg['top']) - float(fvg['bottom'])
                            if gap_size <= 0:
                                continue

                            # ── SBR MODE: trigger & entry di C1.close (demand/supply zone) ──
                            # ── LEGACY MODE: trigger & entry di OCL = C3.open ────────────
                            if SBR_MODE:
                                c1_close = float(fvg.get('c1_close', 0))
                                c1_low   = float(fvg.get('c1_low',   0))
                                c1_high  = float(fvg.get('c1_high',  0))
                                if c1_close <= 0:
                                    continue
                                trigger_lvl = c1_close   # entry juga di sini (market order)
                                if stype == "Long":
                                    sl_nat = c1_low - gap_size * 0.1
                                else:
                                    sl_nat = c1_high + gap_size * 0.1
                            else:
                                ocl = float(fvg.get('c3_open',
                                            fvg['bottom'] if stype == 'Short' else fvg['top']))
                                if ocl <= 0:
                                    continue
                                trigger_lvl = ocl
                                sl_nat = (trigger_lvl - SL_MULT * gap_size if stype == "Long"
                                          else trigger_lvl + SL_MULT * gap_size)

                            # Scan hanya 3 candle terakhir (15 menit) — entry di harga market
                            scan_start = max(len(df_m5_closed) - 3, 0)
                            for ki in range(len(df_m5_closed) - 1, scan_start - 1, -1):
                                ck = df_m5_closed.iloc[ki]

                                touched = False
                                if stype == "Long"  and float(ck['low'])  <= trigger_lvl:
                                    touched = True
                                if stype == "Short" and float(ck['high']) >= trigger_lvl:
                                    touched = True
                                if not touched:
                                    continue

                                # FVG belum broken (close masih valid)
                                if stype == "Long"  and float(ck['close']) < float(fvg['bottom']):
                                    continue
                                if stype == "Short" and float(ck['close']) > float(fvg['top']):
                                    continue

                                # Touch volume filter
                                avg_start = max(0, ki - 20)
                                avg_tvol  = float(df_m5_closed.iloc[avg_start:ki]['vol'].mean()) \
                                            if ki > 0 else 0.0
                                t_vol     = float(ck['vol'])
                                tvol_ratio = t_vol / avg_tvol if avg_tvol > 0 else 0.0
                                if TOUCH_VOL_MIN > 0 and 0 < tvol_ratio < TOUCH_VOL_MIN:
                                    print(f"⚠️ {coin}: Touch vol rendah "
                                          f"({tvol_ratio:.2f}× < {TOUCH_VOL_MIN}×), skip.")
                                    continue

                                # Entry params
                                entry_p = trigger_lvl
                                dist    = abs(entry_p - sl_nat)
                                if dist < entry_p * 0.002:   # min 0.2% SL distance
                                    continue
                                sl_p    = sl_nat
                                trail_d = TRAIL_STOP * dist

                                side_order = "Buy" if stype == "Long" else "Sell"
                                mode_tag = ("RBS" if stype == "Long" else "SBR") if SBR_MODE else "OCL"
                                print(f"\n🎯 {coin}: {mode_tag} Touch! {stype} @ {entry_p:.6f} "
                                      f"| SL:{sl_p:.6f} dist:{dist/entry_p*100:.3f}% "
                                      f"| Trail:{trail_d:.6f} "
                                      f"| GapPct:{gap_size/entry_p*100:.3f}% "
                                      f"| VolRatio:{tvol_ratio:.2f}×")

                                order_id = place_market_order(coin, side_order,
                                                              entry_p, sl_p, trail_d)
                                if order_id:
                                    active_positions[coin] = {
                                        'side'          : side_order,
                                        'entry'         : entry_p,
                                        'sl'            : sl_p,
                                        'dist'          : dist,
                                        'trail_dist'    : trail_d,
                                        'trail_engaged' : False,
                                        'last_price'    : entry_p,
                                        'rev_count'     : 0,
                                        'entry_time'    : time.time(),
                                    }
                                    del pending[coin]
                                    found = True
                                    break
                                else:
                                    del pending[coin]
                                    found = True
                                    break

                            if found:
                                break

                        if not found:
                            if SBR_MODE:
                                lvl_list = [f"{float(g.get('c1_close', 0)):.6g}"
                                            for g in fvg_list if float(g.get('c1_close', 0)) > 0]
                            else:
                                lvl_list = [f"{float(g.get('c3_open', 0)):.6g}"
                                            for g in fvg_list if float(g.get('c3_open', 0)) > 0]
                            lvl_str = " / ".join(lvl_list) if lvl_list else "—"
                            mode_tag = ("RBS" if stype == "Long" else "SBR") if SBR_MODE else "OCL"
                            print(f"⏳ {coin}: Nunggu {mode_tag} touch @ {lvl_str} | "
                                  f"{len(fvg_list)} FVG | {stype} | "
                                  f"Harga H1: {curr_h1['close']:.6g}")
                    continue

                # ── SCAN SETUP BARU ───────────────────────────────────
                if not REQUIRE_BOS:
                    # FVG-only: langsung scan FVG kuat tanpa BOS
                    chosen_fvg, stype = _scan_fvg_only(df_h1_live)
                    if not chosen_fvg or not stype:
                        print(f"   {coin}: tidak ada FVG kuat")
                        continue
                    c1_c = float(chosen_fvg['c1_close'])
                    c1_l = float(chosen_fvg['c1_low'])
                    c1_h = float(chosen_fvg['c1_high'])
                    if stype == 'Long':
                        dist = max(c1_c - c1_l, 0.0)
                        entry_adj = c1_c; sl_entry = c1_l
                    else:
                        dist = max(c1_h - c1_c, 0.0)
                        entry_adj = c1_c; sl_entry = c1_h
                    if dist < c1_c * 0.002:
                        print(f"   {coin}: FVG dist terlalu kecil ({dist/c1_c*100:.3f}%)")
                        continue
                    existing = pending.get(coin)
                    old_ocl = existing.get('orig_ocl', 0) if existing else 0
                    if existing and abs(c1_c - old_ocl) / max(c1_c, 1e-9) < 0.001 and existing.get('type') == stype:
                        continue  # FVG sama di pending, skip
                    # Cek done_setups: jangan re-entry di OCL yang baru saja selesai trade
                    done = done_setups.get(coin)
                    if done and done.get('stype') == stype:
                        done_ocl = done.get('used_ocl', 0)
                        if done_ocl > 0 and abs(c1_c - done_ocl) / max(c1_c, 1e-9) < 0.001:
                            continue  # OCL sama dengan trade terakhir, skip
                    if existing and existing.get('order_id'):
                        cancel_order(coin, existing['order_id'])
                    pending[coin] = {
                        'type': stype, 'phase': 'WAIT_APPROACH',
                        'entry': entry_adj, 'sl': sl_entry, 'dist': dist,
                        'orig_ocl': c1_c, 'choch_level': None,
                        'swing_val': None, 'fvg_only': True,
                    }
                    gap_s = float(chosen_fvg['top']) - float(chosen_fvg['bottom'])
                    print(f"\n📊 {coin} | FVG-only {stype} | OCL:{c1_c:.6f} "
                          f"Entry:{entry_adj:.6f} SL:{sl_entry:.6f} "
                          f"dist:{dist/c1_c*100:.3f}% | Gap:{gap_s/c1_c*100:.3f}%")
                    continue

                # BOS mode
                is_long = False; is_short = False
                swing_val = None; bos_idx = None

                for sh in sh_h1[-3:]:
                    if closed_h1['close'] > sh['val']:
                        is_long   = True
                        swing_val = sh['val']
                        bos_idx   = sl_h1[-1]['idx'] if sl_h1 else sh['idx']
                for sl in sl_h1[-3:]:
                    if closed_h1['close'] < sl['val']:
                        is_short  = True
                        swing_val = sl['val']
                        bos_idx   = sh_h1[-1]['idx'] if sh_h1 else sl['idx']

                if not (is_long or is_short):
                    print(f"   {coin}: tidak ada BOS H1")
                    continue
                if swing_val is None or bos_idx is None:
                    continue
                stype = "Short" if is_short else "Long"

                # CHOCH level
                if stype == "Long":
                    sl_below    = [s for s in sl_h1 if s['val'] < swing_val]
                    choch_level = sl_below[-1]['val'] if sl_below else None
                else:
                    sh_above    = [s for s in sh_h1 if s['val'] > swing_val]
                    choch_level = sh_above[-1]['val'] if sh_above else None

                # FVG kuat
                df_h1_snap = df_h1_live.copy()
                gaps = _get_strong_fvgs(df_h1_snap, stype, bos_idx, choch_level)
                if not gaps:
                    print(f"   {coin}: BOS {stype} @ {swing_val:.6g} — tidak ada FVG kuat")
                    continue

                # Deduplikasi: jangan overwrite setup yang sama
                existing = pending.get(coin)
                if existing and existing.get('swing_val') == swing_val and existing.get('type') == stype:
                    continue

                # Overwrite BOS berbeda: batalkan limit order lama jika ada
                if existing and existing.get('order_id'):
                    cancel_order(coin, existing['order_id'])

                bos_ts    = df_h1_snap['ts'].iloc[bos_idx]
                choch_str = f"{choch_level:.6g}" if choch_level else "—"

                if ENTRY_MODE == 'fvg_limit':
                    # Ambil FVG pertama yang valid
                    chosen_fvg = None
                    for g in gaps:
                        c1_c = float(g.get('c1_close', 0))
                        c1_l = float(g.get('c1_low',   0))
                        c1_h = float(g.get('c1_high',  0))
                        if c1_c <= 0 or c1_h <= c1_l:
                            continue
                        c1_mid = (c1_h + c1_l) / 2.0
                        if stype == "Long"  and c1_c <= c1_mid: continue
                        if stype == "Short" and c1_c >= c1_mid: continue
                        chosen_fvg = g; break

                    if not chosen_fvg:
                        continue

                    c1_c   = float(chosen_fvg['c1_close'])
                    c1_l   = float(chosen_fvg['c1_low'])
                    c1_h   = float(chosen_fvg['c1_high'])
                    gap_s  = float(chosen_fvg['top']) - float(chosen_fvg['bottom'])

                    # Entry di OCL (c1_close), SL di c1_low/c1_high (ujung 100% c1)
                    if stype == 'Long':
                        entry_adj = c1_c
                        dist      = max(c1_c - c1_l, 0.0)
                        sl_entry  = c1_l
                    else:
                        entry_adj = c1_c
                        dist      = max(c1_h - c1_c, 0.0)
                        sl_entry  = c1_h

                    if dist < c1_c * 0.002:
                        continue  # SL terlalu dekat entry

                    # OCL flip: BOS sama + OCL sama → entry dibalik (zone sudah ditest)
                    done = done_setups.get(coin)
                    stype_eff  = stype
                    choch_eff  = choch_level
                    if done and isinstance(done, dict):
                        if done.get('swing_val') == swing_val and done.get('stype') == stype:
                            used_ocl = done.get('used_ocl', 0)
                            if used_ocl > 0 and abs(c1_c - used_ocl) / c1_c < 0.001:
                                # OCL sama → flip direction (mirror dist di sisi berlawanan)
                                stype_eff = 'Short' if stype == 'Long' else 'Long'
                                if stype_eff == 'Short':
                                    entry_adj = c1_c
                                    sl_entry  = c1_c + dist  # SL di atas OCL untuk Short
                                else:
                                    entry_adj = c1_c
                                    sl_entry  = c1_c - dist  # SL di bawah OCL untuk Long
                                choch_eff = None
                            # else: OCL beda (FVG fresh) → proceed normal

                    choch_str = f"{choch_eff:.6g}" if choch_eff else "—"
                    print(f"\n📊 {coin} | BOS {stype_eff} | OCL:{c1_c:.6f} "
                          f"Entry:{entry_adj:.6f} SL(100%):{sl_entry:.6f} "
                          f"dist:{dist/c1_c*100:.3f}% | GapPct:{gap_s/c1_c*100:.3f}% | CHOCH:{choch_str}")

                    # Simpan sebagai WAIT_APPROACH — order belum dipasang, belum pakai margin
                    pending[coin] = {
                        'type'        : stype_eff,
                        'phase'       : 'WAIT_APPROACH',
                        'entry'       : entry_adj,
                        'sl'          : sl_entry,
                        'dist'        : dist,
                        'orig_ocl'    : c1_c,   # OCL asli (c1_close) untuk flip check
                        'fvg_list'    : gaps,
                        'bos_ts'      : bos_ts,
                        'bos_idx'     : bos_idx,
                        'swing_val'   : swing_val,
                        'choch_level' : choch_eff,
                    }
                else:
                    pending[coin] = {
                        'type'        : stype,
                        'phase'       : 'WAIT_FVG_TOUCH',
                        'fvg_list'    : gaps,
                        'fvg_idx'     : 0,
                        'bos_ts'      : bos_ts,
                        'bos_idx'     : bos_idx,
                        'swing_val'   : swing_val,
                        'choch_level' : choch_level,
                    }
                    print(f"\n📊 {coin} | BOS {stype} | Swing: {swing_val:.6g} | C: {curr_h1['close']:.6g}")
                    print(f"   ⛔ CHOCH batal: {choch_str}")
                    print(f"   {len(gaps)} FVG kuat tersedia:")
                    for i, g in enumerate(gaps):
                        ocl      = g.get('c3_open', 0)
                        sbr_lvl  = g.get('c1_close', 0)
                        gap_size = g['top'] - g['bottom']
                        ref_p    = ocl if ocl > 0 else (g['bottom'] if stype == 'Short' else g['top'])
                        lbl      = ("RBS" if stype == "Long" else "SBR") if SBR_MODE else "OCL"
                        entry_v  = sbr_lvl if SBR_MODE and sbr_lvl > 0 else ocl
                        mode_lbl = f"{lbl}:{entry_v:.6g}"
                        print(f"   FVG {i+1}: bot:{g['bottom']:.6g} top:{g['top']:.6g} "
                              f"{mode_lbl} gap:{abs(gap_size)/ref_p*100:.3f}%" if ref_p > 0 else
                              f"   FVG {i+1}: bot:{g['bottom']:.6g} top:{g['top']:.6g} {mode_lbl}")

            except Exception as e:
                print(f"⚠️ Error {coin}: {e}"); continue


if __name__ == "__main__":
    run_bot()
