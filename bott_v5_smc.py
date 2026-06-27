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
ENTRY_FILE = "entries.log"   # khusus catatan entry — TIDAK tergulung oleh log monitoring

def log_entry(text):
    """Catat entry ke entries.log (permanen, tak tergulung) DAN ke /logs."""
    import datetime
    ts = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=7)).strftime('[%Y-%m-%d %H:%M:%S] ')
    try:
        with open(ENTRY_FILE, 'a', encoding='utf-8') as f:
            f.write(ts + text.replace('\n', '\n' + ' ' * len(ts)) + '\n')
    except Exception:
        pass
    print(text)   # juga muncul di /logs

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

LAST_OHLC = {}   # (symbol, interval) -> df OHLC terakhir (diunduh via /ohlc utk diagnostik)

class _LogHandler(BaseHTTPRequestHandler):
    def _send(self, body, ctype='text/plain; charset=utf-8', extra=None):
        if isinstance(body, str):
            body = body.encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Access-Control-Allow-Origin', '*')
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        import datetime as _dt
        path = self.path.split('?', 1)[0]
        query = {}
        if '?' in self.path:
            for kv in self.path.split('?', 1)[1].split('&'):
                if '=' in kv:
                    k, v = kv.split('=', 1); query[k] = v

        if path == '/entries':
            try:
                with open(ENTRY_FILE, 'r', encoding='utf-8') as f:
                    data = f.read()
            except Exception:
                data = '(belum ada entry)'
            return self._send(data)

        if path == '/logs':
            try:
                with open(LOG_FILE, 'r', encoding='utf-8') as f:
                    data = ''.join(f.readlines()[-200:])
            except Exception:
                data = ''
            return self._send(data)

        # ---- Unduh OHLC (diagnostik): /ohlc (halaman tombol) atau /ohlc?symbol=X&tf=60 (CSV) ----
        if path == '/ohlc':
            sym = query.get('symbol'); tf = query.get('tf', '60')
            if sym:   # unduh CSV
                df = LAST_OHLC.get((sym, str(tf)))
                if df is None:
                    return self._send(f"(data {sym} tf{tf} belum ada — tunggu bot scan dulu)")
                rows = ["ts_ms,waktu_WIB,open,high,low,close,volume"]
                for _, r in df.iterrows():
                    t = _dt.datetime.utcfromtimestamp(int(r['ts']) / 1000) + _dt.timedelta(hours=7)
                    rows.append(f"{int(r['ts'])},{t:%Y-%m-%d %H:%M:%S},"
                                f"{r['open']:.10g},{r['high']:.10g},{r['low']:.10g},{r['close']:.10g},{r.get('vol',0):.10g}")
                csv = "\n".join(rows)
                fname = f"{sym}_tf{tf}_{_dt.datetime.utcnow():%Y%m%d_%H%M}.csv"
                return self._send(csv, 'text/csv; charset=utf-8',
                                  {'Content-Disposition': f'attachment; filename="{fname}"'})
            # halaman tombol
            keys = sorted(LAST_OHLC.keys())
            if not keys:
                return self._send("<h3>Belum ada data. Tunggu bot scan beberapa detik lalu refresh.</h3>"
                                  "<a href='/ohlc'>refresh</a>", 'text/html; charset=utf-8')
            syms = sorted({k[0] for k in keys})
            html = ["<html><head><meta charset='utf-8'><title>Unduh OHLC</title>",
                    "<style>body{font-family:sans-serif;background:#111;color:#eee;padding:16px}"
                    "a.btn{display:inline-block;margin:3px;padding:6px 10px;background:#2a6;color:#fff;"
                    "text-decoration:none;border-radius:5px}a.btn.m5{background:#26a}h4{margin:14px 0 4px}</style></head><body>",
                    "<h2>Unduh OHLC (data yg dilihat bot)</h2>",
                    "<p>Klik untuk unduh CSV (ts epoch + waktu WIB + OHLC). Kirim file-nya ke Claude untuk cek break/choch.</p>",
                    "<p><a href='/logs'>/logs</a> · <a href='/entries'>/entries</a> · <a href='/ohlc'>refresh</a></p>"]
            for s in syms:
                html.append(f"<h4>{s}</h4>")
                if (s, '60') in LAST_OHLC:
                    html.append(f"<a class='btn' href='/ohlc?symbol={s}&tf=60'>⬇ H1 (60m)</a>")
                if (s, '5') in LAST_OHLC:
                    html.append(f"<a class='btn m5' href='/ohlc?symbol={s}&tf=5'>⬇ M5</a>")
            html.append("</body></html>")
            return self._send("\n".join(html), 'text/html; charset=utf-8')

        if path == '/':
            return self._send("<html><body style='font-family:sans-serif;background:#111;color:#eee;padding:16px'>"
                              "<h2>SMC bot</h2><p><a href='/logs' style='color:#6cf'>/logs</a> · "
                              "<a href='/entries' style='color:#6cf'>/entries</a> · "
                              "<a href='/ohlc' style='color:#6cf'><b>/ohlc — unduh data OHLC</b></a></p></body></html>",
                              'text/html; charset=utf-8')

        self.send_response(404); self.end_headers()

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
TRAIL_STOP       = 1.0    # trailing distance = TRAIL_STOP × dist (sinkron backtest Trail=0.5R)
TRAIL_ACT_R      = 3.0    # trail aktif setelah +TRAIL_ACT_R (Bybit min > trailingStop)
TRAIL_TIMEOUT_DAYS = 3    # close posisi jika peak tidak bergerak selama N hari (sinkron backtest)
USE_TP           = False  # False = trailing stop AKTIF (TP fix dimatikan)
RR_TP            = 9.0    # TP di 1:RR_TP (4.0 = 1:4)
RISK_PCT         = 0.01   # risk per trade = 1% dari total equity
LEVERAGE         = 25     # leverage (dibatasi max_leverage coin). Naikkan utk hemat margin (slot lebih banyak)
MIN_ORDER_USD    = 5.0    # minimum order value Bybit
ORDER_BUMP_FLOOR = 4.0    # order >= ini & < $5 -> naikkan qty ke $5 (over-risk <=1.25x); di bawah ini skip
SBR_MODE         = True   # True = SBR entry di C1.close + SL di C1.low, False = OCL entry lama
ENTRY_MODE       = 'fvg_limit'  # limit di zona FVG (satu-satunya jalur)
TOUCH_VOL_MIN    = 0.8    # touch candle volume min (× avg 20 M5 candle) — hanya dipakai fvg_sbr
MAX_GAP_PCT      = 0.0    # 0 = TANPA BATAS gap (entry=C1.close, SL=C1.low — lebar gap tak ngaruh)
MAX_CONCURRENT   = 12     # PLAFON KEAMANAN posisi bersamaan (backstop). Pembatas utama = MARGIN.
                          # ⚠️ tiap posisi risiko ~1% → 12 posisi = ~12% jika semua kena SL serentak
                          #    (alt sering jatuh berkorelasi!). Turunkan kalau mau lebih aman.
APPROACH_R       = 2.0    # place limit saat harga dalam 1R dari entry (ujung wick C2)
REQUIRE_BOS      = True   # SMC inti: WAJIB BOS H1 dulu
SL_FRAC          = 1.0    # SL penuh di invalidation C1 low/high (standar SMC)
SL_CAP_RANGE     = 0.01   # jarak entry->SL = 10% range BOS (lihat SL_FIXED_RANGE)
SL_FIXED_RANGE   = True   # True = SL SELALU 10% range BOS (abaikan C1); False = SL ikut C1, di-cap 10% range
MIN_DIST_FLOOR   = True   # True = dist kecil pakai SL minimum 0.2% (bukan di-skip)
INDUCEMENT_ENTRY = True   # True = aktif entry inducement (market, kebalik arah BOS besar) berdampingan dgn limit FVG
INDUCEMENT_ZONE_LO = 0.268 # bos kecil dicari mulai 35% range BOS besar (dari puncak/lembah)
INDUCEMENT_ZONE_HI = 0.99 # ...sampai 60% range. (pita IDM 35-60%)
INDUCEMENT_TF    = "60"   # timeframe cari inducement: "5"=M5, "60"=H1
INDUCEMENT_SWING = 1      # ukuran swing bos kecil MINIMUM: 1-1 (mencakup 2-2..4-4 & asimetris otomatis)
INDUCEMENT_SWING_MAX = 5   # IDM di-SKIP bila kekuatan swing >= ini di KEDUA sisi (= SWING_BARS; skala BOS besar 5-5+)
REQUIRE_IDM_FOR_FVG = True # True = entry FVG limit HANYA bila BOS besar punya IDM mini-BOS di dalamnya (lebih ketat)

# === ENTRY IDM via LIMIT (Fib retrace candle M5 pemicu) ===
# True = entry IDM pakai LIMIT di Fib IDM_LIMIT_FIB dari range candle M5 yg close menembus trigger
#        (Long: 0%=low,100%=high; Short: 0%=high,100%=low). False = market di harga sweep (lama).
IDM_LIMIT_ENTRY    = True
IDM_LIMIT_FIB      = 0.50   # 50% range candle H1 yg membentuk trigger IDM

# --- Filter momentum "candle makan candle" sebelum entry limit IDM ---
# Tujuan: pastikan ada bukti kekuatan buyer/seller asli (bukan cuma sapuan tipis) di
# leg impulsif (choch->puncak) sebelum mempercayai liquidity di balik IDM tsb.
INDUCEMENT_MOMENTUM_FILTER = False
INDUCEMENT_MOMENTUM_MAX_CANDLES = 5   # window maksimum: N candle H1 terbaru (termasuk candle berjalan)
INDUCEMENT_MOMENTUM_MIN_CANDLES = 3   # kalau candle sejak puncak < ini -> jangan entry (data kurang)
IDM_CANCEL_MOVE_PCT = 0.10  # (lama, hanya aktif kalau IDM_M5_ENGULF=False) batalkan limit IDM jika harga bergerak > N×range BOS dari trigger
IDM_M5_ENGULF       = True  # True = setelah trigger tersapu, monitor M5 engulfing dulu sebelum market entry
IDM_CANCEL_RANGE_PCT= 0.20  # hangus permanen jika harga >N×range BOS dari trigger ke arah mana pun (IDM_M5_ENGULF=True)
REQUIRE_FRESH_C1 = True    # True = tolak FVG bila C1.close sudah disentuh candle SETELAH C3 (zona tak fresh)

# --- Filter konfluensi funding rate (window pre-settlement) ---
# Bybit settle funding 3x sehari: 00:00, 08:00, 16:00 UTC (07:00, 15:00, 23:00 WIB).
# FUNDING_WINDOW_MIN menit sebelum settlement: blokir pasang limit baru YG GAK SEARAH funding,
# DAN batalkan limit yg sudah terpasang (FVG pending + IDM idm_pending) yg gak searah.
# Setelah jam settlement lewat: otomatis normal kembali (tidak perlu restart).
# Posisi aktif (sudah terisi) TIDAK disentuh.
FUNDING_FILTER      = False   # True = aktifkan logika window pre-settlement
FUNDING_MIN_EDGE    = 0.0     # ambang batas rate (fraction). 0.0 = cukup searah saja.
FUNDING_WINDOW_MIN  = 60      # menit sebelum settlement yg jadi window aktif
FUNDING_CACHE_TTL   = 300     # detik — cache get_tickers biar tak spam API

# === HEDGE MODE ===
# True = IDM (market, kebalik arah) + limit FVG (searah BOS) boleh JALAN BARENGAN per koin.
# WAJIB: akun Bybit di Hedge Mode (switch_position_mode mode=3) DULU. positionIdx: Buy=1, Sell=2.
# PERINGATAN: ini ubah routing order live; UJI DI TESTNET (TESTNET=true) sebelum live.
ALLOW_HEDGE = True
def _pidx(side):
    """positionIdx Bybit: hedge -> Buy=1/Sell=2; one-way -> 0."""
    return (1 if side == "Buy" else 2) if ALLOW_HEDGE else 0
def _akey(coin, e_stype):
    """Key active_positions: hedge -> per-arah ('COIN|Long'); one-way -> 'COIN'."""
    return f"{coin}|{e_stype}" if ALLOW_HEDGE else coin

# (jalur eksperimen wait_rev DIBUANG — SMC inti only)

SYMBOLS = [
    # 36 coin — sinkron dengan backtest (wait_rev, −INJ)
    'XPLUSDT', 'MNTUSDT', 'PLUMEUSDT', 'HYPEUSDT', 'BNBUSDT', 'BELUSDT', 'BERAUSDT', 'DASHUSDT', 'ROSEUSDT', 'DOGEUSDT', 'USUALUSDT', 'TAOUSDT', 'ESPORTSUSDT', 'LABUSDT', 'HUSDT', 'AVAXUSDT', 'REUSDT', '1000BONKUSDT', 'JUPUSDT', 'ORCAUSDT', 'AAVEUSDT', 'GMXUSDT', 'LTCUSDT', 'ICPUSDT', 'VIRTUALUSDT', 'CFXUSDT', 'UNIUSDT', 'ONDOUSDT', 'SUIUSDT', 'XAUTUSDT', 'ALGOUSDT', 'HBARUSDT', 'EIGENUSDT', 'XRPUSDT', 'SOLUSDT', 'CRVUSDT', 'RENDERUSDT', 'XVGUSDT', 'SANDUSDT', 'AXSUSDT', 'IMXUSDT', 'FARTCOINUSDT', 'OPUSDT', '1000PEPEUSDT', 'TIAUSDT', 'GALAUSDT', 'APEUSDT', 'FLOWUSDT',
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
    'AAVEUSDT'      : 0.0026,   # P25=0.259%
    'GMXUSDT'       : 0.0020,   # P25=0.203%
    'LTCUSDT'       : 0.0018,   # P25=0.178%
    'ICPUSDT'       : 0.0023,   # P25=0.231%
    'VIRTUALUSDT'   : 0.0036,   # P25=0.363%
}

# ── Dist range filter: skip setup kalau dist% di luar sweet spot ────────────
# dist% = (c1_close - c1_low/high) / c1_close × 100
# Range dari bucket analysis backtest Jan2025-Apr2026 (dist dinamis).
DIST_RANGE_FILTER = {
    '1000BONKUSDT' : (0.4, 0.8),   # 0.4-0.6: WR=48% N=159, 0.6-0.8: WR=47% N=53
    'AAVEUSDT'     : (0.6, 1.5),   # 0.8-1: WR=46% N=151, 0.6-0.8: WR=46% N=124
    'BERAUSDT'     : (0.6, 1.5),   # 0.6-0.8: WR=50% N=117, 0.8-1: WR=50% N=198
    'GMXUSDT'      : (1.0, 2.0),   # 1-1.5: WR=47% N=270
    'ICPUSDT'      : (0.6, 1.5),   # 0.8-1: WR=50% N=111, 1-1.5: WR=46% N=226
    'JUPUSDT'      : (1.0, 2.0),   # 1-1.5: WR=47% N=127, 1.5-2: WR=49% N=111
    'LTCUSDT'      : (0.6, 1.5),   # 0.8-1: WR=49% N=123, 1.5-2: WR=46% N=71
    'ORCAUSDT'     : (0.6, 1.5),   # 0.8-1: WR=51% N=196 ★
    'SHIB1000USDT' : (1.0, 2.5),   # 1-1.5: WR=47% N=135, 1.5-2: WR=49% N=121
    'SOLUSDT'      : (1.0, 1.5),   # 1-1.5: WR=50% N=117
    'TAOUSDT'      : (0.6, 1.0),   # 0.8-1: WR=65% N=63 ★, 0.4-0.6: WR=49% N=211
    'VIRTUALUSDT'  : (0.6, 1.5),   # 0.8-1: WR=48% N=82
    'XRPUSDT'      : (0.4, 0.8),   # 0.4-0.6: WR=44% N=114, 0.6-0.8: WR=46% N=113
}

# ── Direction filter per coin ────────────────────────────────────────────────
# Dari analisis win/loss backtest: hanya ambil arah yang WR tinggi.
DIR_FILTER: dict = {
    'JUPUSDT'      : 'Short',
    'AAVEUSDT'     : 'Short',
    '1000BONKUSDT' : 'Short',
    'BERAUSDT'     : None,
    'GMXUSDT'      : None,
    'ICPUSDT'      : None,
    'ORCAUSDT'     : None,
    'SHIB1000USDT' : None,
    'SOLUSDT'      : None,
    'TAOUSDT'      : None,
    'VIRTUALUSDT'  : None,
    'XRPUSDT'      : None,
    'LTCUSDT'      : None,
}

# ── Session filter per coin ──────────────────────────────────────────────────
SESSION_FILTER: dict = {
    '1000BONKUSDT' : None,
    'AAVEUSDT'     : None,
    'BERAUSDT'     : None,
    'GMXUSDT'      : None,
    'ICPUSDT'      : None,
    'JUPUSDT'      : None,
    'LTCUSDT'      : None,
    'ORCAUSDT'     : None,
    'SHIB1000USDT' : None,
    'SOLUSDT'      : None,
    'TAOUSDT'      : None,
    'VIRTUALUSDT'  : None,
    'XRPUSDT'      : None,
}


bot_start_ts     = 0     # di-set saat run_bot() mulai — untuk filter sweep historis IDM
pending          = {}
idm_pending      = {}   # _akey(coin,e_stype) -> limit IDM yg menunggu fill (Fib retrace candle M5)
active_positions = {}
inducement_done  = {}   # coin -> signature struktur BOS besar yg sudah di-entry inducement (anti entry-ulang)
instrument_cache = {}
funding_cache    = {}   # symbol -> {'rate': float, 'ts': float} — cache funding rate (TTL=FUNDING_CACHE_TTL)
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
            df = df.iloc[::-1].reset_index(drop=True)
            LAST_OHLC[(symbol, str(interval))] = df   # cache utk unduhan diagnostik
            return df
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


# ============================================================
# FUNDING RATE (filter konfluensi)
# ============================================================

def get_funding_rate(symbol):
    """Funding rate terkini (fraction, mis. 0.0001 = 0.01%) dari Bybit ticker.
    Di-cache TTL=FUNDING_CACHE_TTL detik biar tak spam API tiap loop coin.
    Kalau API gagal: balikin cache lama (kalau ada) drpd None, biar tahan sesaat gangguan jaringan."""
    now = time.time()
    cached = funding_cache.get(symbol)
    if cached and (now - cached['ts']) < FUNDING_CACHE_TTL:
        return cached['rate']
    try:
        res = session.get_tickers(category=CATEGORY, symbol=symbol)
        if res['retCode'] == 0 and res['result']['list']:
            rate = float(res['result']['list'][0]['fundingRate'])
            funding_cache[symbol] = {'rate': rate, 'ts': now}
            return rate
        print(f"⚠️ funding_rate {symbol}: {res.get('retMsg','')}")
    except Exception as e:
        print(f"⚠️ funding_rate {symbol}: {e}")
    return cached['rate'] if cached else None


def funding_favors(stype, symbol):
    """True kalau funding rate SAAT INI menguntungkan posisi `stype` ('Long'/'Short') saat settlement.
    Bybit: rate POSITIF -> Long bayar Short. rate NEGATIF -> Short bayar Long.
    Kalau rate gagal diambil -> True (jangan blokir entry krn alasan teknis API, bukan krn sinyal funding)."""
    rate = get_funding_rate(symbol)
    if rate is None:
        return True
    if stype == "Long":
        return rate <= -FUNDING_MIN_EDGE
    else:
        return rate >= FUNDING_MIN_EDGE


def in_funding_window():
    """True kalau sekarang dalam FUNDING_WINDOW_MIN menit sebelum salah satu jam settlement Bybit.
    Settlement UTC: 00:00, 08:00, 16:00.  Fungsi ini pakai UTC supaya konsisten tanpa peduli TZ server."""
    import datetime as _dt
    now_utc = _dt.datetime.utcnow()
    mins_utc = now_utc.hour * 60 + now_utc.minute
    for settle_h in (0, 8, 16):
        settle_mins = settle_h * 60
        # selisih menit menuju settlement berikutnya (wrap-around 24 jam)
        diff = (settle_mins - mins_utc) % (24 * 60)
        if 0 < diff <= FUNDING_WINDOW_MIN:   # 0 dikecualikan: pas detik-detik settlement = sudah lewat
            return True
    return False


def cancel_unfavorable_limits(coin):
    """Selama dalam funding window: batalkan limit FVG (pending) & IDM (idm_pending) coin ini
    yang arahnya GAK SEARAH funding rate sekarang. Posisi aktif tidak disentuh."""
    import copy
    # ── FVG limits (pending) ──
    if coin in pending:
        dirs_to_remove = []
        for d, st in list(pending[coin].items()):
            if st.get('phase') != 'WAIT_FILL':
                continue                         # WAIT_APPROACH belum punya limit order -> skip
            stype_limit = st.get('type')
            if stype_limit and not funding_favors(stype_limit, coin):
                oid = st.get('order_id')
                if oid:
                    cancel_order(coin, oid)
                dirs_to_remove.append(d)
                rate = get_funding_rate(coin)
                print(f"   💸 {coin} FVG {stype_limit}: limit dibatalkan (funding window, rate={rate})")
        for d in dirs_to_remove:
            del pending[coin][d]
        if not pending[coin]:
            del pending[coin]
    # ── IDM limits (idm_pending) ──
    for key in list(idm_pending.keys()):
        # key = _akey(coin, e_stype) = f"{coin}_{e_stype}"
        if not key.startswith(coin + "_"):
            continue
        e_stype = key[len(coin)+1:]
        if not funding_favors(e_stype, coin):
            st = idm_pending[key]
            oid = st.get('order_id')
            if oid:
                cancel_order(coin, oid)
            del idm_pending[key]
            rate = get_funding_rate(coin)
            print(f"   💸 {coin} IDM {e_stype}: limit dibatalkan (funding window, rate={rate})")


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

# Swing high/low: butuh SWING_BARS candle lebih rendah/tinggi di KIRI & KANAN.
# Hanya swing yang SUDAH terkonfirmasi penuh (5-kanan terbentuk) yang dikembalikan.
# Candle yang MENEMBUS swing (breaker) dievaluasi terpisah (lihat closed_h1 = df.iloc[-2])
# dan TIDAK perlu konfirmasi kanan-5 — cukup close menembus swing yang sudah valid.
SWING_BARS = 5
# Fraktal HALUS untuk telusur leg (rebreak/extension) di dalam impuls. Lebih halus dari SWING_BARS
# supaya swing-2 minor (mis. retrace dangkal lalu rebreak) tetap terbaca, tapi tak sebising bar mentah.
SUBLEG_BARS = 3

# Filter zona entry: C1.close (entry) harus berada di retrace ENTRY_ZONE_LO..ENTRY_ZONE_HI
# dari range BOS, di mana 0% = ekstrem impulse (swing terbaru), 100% = CHOCH (invalidasi).
# Mis. 0.50..1.00 = hanya zona "diskon" (separuh lebih dalam menuju CHOCH).
ENTRY_ZONE_LO = 0.618   # golden ratio / OTE — C1.close minimal retrace 61.8%
ENTRY_ZONE_HI = 1.00
# Trigger FVG entry = ujung C3 (low[C3] untuk Long, high[C3] untuk Short = batas gap).
# Zona golden ratio dihitung dari C3 ujung, bukan C1 close.
FVG_CANCEL_RANGE_PCT = 0.20   # 20% BOS range dari C3 ujung ke arah BOS → setup hangus

# --- Filter engulfing M5 sebelum entry FVG (C1 close sebagai trigger) ---
# Saat C1 close H1 tersentuh, bot monitor M5 dan tunggu konfirmasi engulfing sebelum market order.
# "Candle fokus" = candle M5 pertama yang menyentuh C1 close, lalu bergeser jika ada wick/close
# yang keluar dari range candle fokus. Entry terjadi saat close candle M5 melewati high candle fokus
# (Long) atau low candle fokus (Short). SL = low_engulfing - SL_ENGULF_PCT*bos_rng (Long).
M5_ENGULF_FILTER  = True    # False = skip filter ini, entry langsung market saat C1 close tersentuh
SL_ENGULF_PCT     = 0.01    # SL = ujung candle fokus ± N% range BOS
REBREAK_INVALID = True  # True = BOS batal bila harga retrace >= RETRACE_LOCK lalu close lewati swing-2 (struktur baru)
ZONE_FROM_RETRACE = True # True = batas bawah zona entry = max(61.8%, retrace terdalam); area yg sudah dilewati retrace tak dipakai
RETRACE_LOCK    = 0.50  # ambang retrace yang "mengunci" swing-2 sebagai puncak (50% range BOS)

def find_last_swing_bos(df, n=SWING_BARS):
    highs, lows = [], []
    hi = df['high'].values; lo = df['low'].values; ts = df['ts'].values
    for i in range(n, len(df) - n):
        h = hi[i]; l = lo[i]
        if all(hi[i-k] < h for k in range(1, n+1)) and all(hi[i+k] < h for k in range(1, n+1)):
            highs.append({'val': h, 'idx': i, 'ts': ts[i]})
        if all(lo[i-k] > l for k in range(1, n+1)) and all(lo[i+k] > l for k in range(1, n+1)):
            lows.append({'val': l, 'idx': i, 'ts': ts[i]})
    return highs, lows


def impulse_anchors(stype, swing_val, brk_idx, sh_h1, sl_h1, df=None):
    """CHOCH = protective low/high = EKSTREM (low terendah / high tertinggi) ANTARA
    swing-1 (yang di-break) dan puncak/lembah swing-2 — yaitu launch impulse, bukan
    swing lama di belakang swing-1. Return (bos_idx, choch_level, peak_val).
    peak_val = swing 5-5 terkonfirmasi yang jadi puncak/lembah (None bila belum terbentuk)."""
    if swing_val is None or brk_idx is None or not sh_h1 or not sl_h1:
        return None, None, None
    if stype == "Long":
        peaks = [x for x in sh_h1 if x['idx'] > brk_idx and x['val'] > swing_val]
        peak_val = max(peaks, key=lambda x: x['val'])['val'] if peaks else None
        # puncak (batas atas pencarian choch) = high tertinggi mentah setelah break
        if df is not None and len(df) > brk_idx + 1:
            peak_idx = int(df['high'].iloc[brk_idx:].idxmax())
        else:
            peak_idx = (max(peaks, key=lambda x: x['val'])['idx'] if peaks else sh_h1[-1]['idx'])
        # CHOCH = swing low 5-5 TERDALAM antara break & puncak (HARUS swing 5-5; kalau tak ada -> skip)
        cands = [x for x in sl_h1 if brk_idx <= x['idx'] < peak_idx]
        if not cands:
            return None, None, peak_val
        ch = min(cands, key=lambda x: x['val'])
        return ch['idx'], ch['val'], peak_val
    else:
        troughs = [x for x in sl_h1 if x['idx'] > brk_idx and x['val'] < swing_val]
        peak_val = min(troughs, key=lambda x: x['val'])['val'] if troughs else None
        if df is not None and len(df) > brk_idx + 1:
            trough_idx = int(df['low'].iloc[brk_idx:].idxmin())
        else:
            trough_idx = (min(troughs, key=lambda x: x['val'])['idx'] if troughs else sl_h1[-1]['idx'])
        cands = [x for x in sh_h1 if brk_idx <= x['idx'] < trough_idx]
        if not cands:
            return None, None, peak_val
        ch = max(cands, key=lambda x: x['val'])
        return ch['idx'], ch['val'], peak_val


def rebreak_invalid(df, start_idx, swing2, choch_level, stype, lock_retr=0.50):
    """True bila SETELAH harga retrace >= lock_retr (dari swing2 ke arah choch),
    ada candle yang CLOSE melewati swing2 (= rebreak, struktur baru).
    swing2 = puncak/lembah swing 5-5 (TETAP). Dihitung historis -> konsisten lintas-redeploy."""
    n = len(df)
    if swing2 is None or start_idx is None or start_idx >= n - 1 or choch_level is None:
        return False
    hi = df['high'].values; lo = df['low'].values; cl = df['close'].values
    if stype == "Long":
        rng = swing2 - choch_level
        if rng <= 0:
            return False
        half = swing2 - lock_retr * rng
        retraced = False
        for k in range(int(start_idx) + 1, n):
            if lo[k] <= half:
                retraced = True
            if retraced and cl[k] > swing2:
                return True
        return False
    else:
        rng = choch_level - swing2
        if rng <= 0:
            return False
        half = swing2 + lock_retr * rng
        retraced = False
        for k in range(int(start_idx) + 1, n):
            if hi[k] >= half:
                retraced = True
            if retraced and cl[k] < swing2:
                return True
        return False


def choch_is_broken(df, bos_idx, choch_level, stype):
    """CHoCH ditembus = SETELAH puncak, ada candle yang CLOSE menembus choch (Long: < choch / Short: > choch).
    Historis -> tetap mati walau harga sudah balik. bos_idx = indeks choch (launch)."""
    n = len(df)
    if bos_idx is None or bos_idx >= n or choch_level is None:
        return False
    if stype == "Long":
        peak_idx = int(df['high'].iloc[bos_idx:].idxmax())
        return bool((df['close'].iloc[peak_idx:] < choch_level).any())
    else:
        peak_idx = int(df['low'].iloc[bos_idx:].idxmin())
        return bool((df['close'].iloc[peak_idx:] > choch_level).any())


def momentum_eaten(df_h1, peak_idx, stype):
    """Cek 'candle makan candle' di leg impulsif BOS besar (`stype`), dari puncak/lembah
    s/d candle TERBARU (termasuk yg belum close).
    Window = maks INDUCEMENT_MOMENTUM_MAX_CANDLES candle terbaru, tak boleh lewat `peak_idx`.
    Kalau jumlah candle di window < INDUCEMENT_MOMENTUM_MIN_CANDLES -> None (data kurang, jangan entry).
    stype Long -> fokus HIGH dimakan (lower bound jadi referensi baru kalau LOW tertembus duluan).
    stype Short -> fokus LOW dimakan (upper bound jadi referensi baru kalau HIGH tertembus duluan).
    'Dimakan' = sisi relevan disentuh ATAU dilewati (>=  / <=), bukan harus tembus.
    Return True/False/None."""
    n = len(df_h1)
    if peak_idx is None or peak_idx < 0 or peak_idx >= n:
        return None
    latest_idx = n - 1
    start_idx = max(peak_idx, latest_idx - (INDUCEMENT_MOMENTUM_MAX_CANDLES - 1))
    window_size = latest_idx - start_idx + 1
    if window_size < INDUCEMENT_MOMENTUM_MIN_CANDLES:
        return None
    ref_hi = float(df_h1['high'].iloc[start_idx])
    ref_lo = float(df_h1['low'].iloc[start_idx])
    for i in range(start_idx + 1, latest_idx + 1):
        hi = float(df_h1['high'].iloc[i])
        lo = float(df_h1['low'].iloc[i])
        if stype == "Long":
            if hi >= ref_hi:
                return True          # sisi relevan (atas) dimakan -> sukses
            if lo <= ref_lo:
                ref_hi, ref_lo = hi, lo   # sisi bawah tertembus -> range lama tak lagi utuh, reset referensi
        else:
            if lo <= ref_lo:
                return True          # sisi relevan (bawah) dimakan -> sukses
            if hi >= ref_hi:
                ref_hi, ref_lo = hi, lo   # sisi atas tertembus -> reset referensi
    return False


def deepest_retrace_lo(df, bos_idx, choch_level, stype):
    """Batas bawah zona entry dinamis = max(ENTRY_ZONE_LO, retrace TERDALAM setelah puncak).
    Area 0..retrace_terdalam sudah dilewati candle retrace -> tak boleh dipakai entry (sudah terisi)."""
    n = len(df)
    if not ZONE_FROM_RETRACE or bos_idx is None or bos_idx >= n or choch_level is None:
        return ENTRY_ZONE_LO
    if stype == "Long":
        sub = df['high'].iloc[bos_idx:]
        B = float(sub.max()); pk = int(sub.idxmax()); rng = B - choch_level
        if rng <= 0: return ENTRY_ZONE_LO
        low_after = float(df['low'].iloc[pk:].min())
        frac = (B - low_after) / rng
    else:
        sub = df['low'].iloc[bos_idx:]
        B = float(sub.min()); pk = int(sub.idxmin()); rng = choch_level - B
        if rng <= 0: return ENTRY_ZONE_LO
        high_after = float(df['high'].iloc[pk:].max())
        frac = (high_after - B) / rng
    return max(ENTRY_ZONE_LO, min(frac, ENTRY_ZONE_HI))


# ============================================================
# FVG — dengan volume fields untuk fvg
# ============================================================

def _gap_vol_fields(df, c3_idx):
    """Extract volume + OCL + C1 fields untuk FVG (df dalam H1). C1=c3_idx-2."""
    c2_idx   = c3_idx - 1
    c1_idx   = c3_idx - 2
    c2_close = float(df['close'].iloc[c2_idx]) if c2_idx >= 0 else 0.0
    c2_low   = float(df['low'].iloc[c2_idx])   if c2_idx >= 0 else 0.0
    c2_high  = float(df['high'].iloc[c2_idx])  if c2_idx >= 0 else 0.0
    c3_open  = float(df['open'].iloc[c3_idx])  if c3_idx < len(df) else 0.0
    c1_open  = float(df['open'].iloc[c1_idx])  if c1_idx >= 0 else 0.0
    c1_close = float(df['close'].iloc[c1_idx]) if c1_idx >= 0 else 0.0
    c1_low   = float(df['low'].iloc[c1_idx])   if c1_idx >= 0 else 0.0
    c1_high  = float(df['high'].iloc[c1_idx])  if c1_idx >= 0 else 0.0
    base = {'c2_close': c2_close, 'c2_low': c2_low, 'c2_high': c2_high, 'c3_open': c3_open,
            'c1_open': c1_open, 'c1_close': c1_close,
            'c1_low': c1_low,   'c1_high': c1_high, 'c3_idx': c3_idx}
    if 'vol' not in df.columns:
        return {**base, 'c3_vol': 0.0, 'vol_max10h': 0.0}
    c3_vol    = float(df['vol'].iloc[c3_idx])
    avg_start = max(0, c3_idx - 5)
    vol_max   = float(df['vol'].iloc[avg_start:c3_idx].max()) if c3_idx > 0 else 0.0
    return {**base, 'c3_vol': c3_vol, 'vol_max10h': vol_max}


def get_internal_gaps(df, stype, bos_idx, lookback=60, require_fresh=True, peak_idx=None):
    """Scan FVG dari bos_idx (CHOCH) sampai peak_idx (puncak) — leg impulsif saja.
    Kalau peak_idx=None, scan sampai akhir data (perilaku lama untuk caller lain).
    require_fresh=True: cek apakah FVG sudah terisi (candle dalam range menyentuh bottom/top gap).
    require_fresh=False: semua gap mentah tanpa filter, freshness diserahkan ke pemanggil."""
    gaps = []
    # Batas akhir scan: peak_idx kalau tersedia, else akhir data
    scan_end = (peak_idx - 1) if (peak_idx is not None and peak_idx > bos_idx) else (len(df) - 2)
    scan_end = min(scan_end, len(df) - 2)

    # Scan FVG dari bos_idx sampai scan_end (CHOCH → puncak)
    # C1=i-1, C2=i, C3=i+1  →  i mulai dari bos_idx+1
    for i in range(bos_idx + 1, scan_end + 1):
        if i + 1 >= len(df): continue
        gap = None
        if stype == "Long" and df['high'].iloc[i-1] < df['low'].iloc[i+1]:
            gap = {"top": df['low'].iloc[i+1], "bottom": df['high'].iloc[i-1], "zone": "impulse"}
            gap.update(_gap_vol_fields(df, i + 1))
        elif stype == "Short" and df['low'].iloc[i-1] > df['high'].iloc[i+1]:
            gap = {"top": df['low'].iloc[i-1], "bottom": df['high'].iloc[i+1], "zone": "impulse"}
            gap.update(_gap_vol_fields(df, i + 1))
        if gap:
            is_fresh = True
            if require_fresh:
                # Cek apakah ada candle SETELAH C3 (dalam range s/d peak_idx) yang menutup gap
                check_end = (peak_idx + 1) if peak_idx is not None else len(df)
                check_end = min(check_end, len(df))
                for j in range(i + 2, check_end):
                    if stype == "Long"  and df['low'].iloc[j]  <= gap['bottom']: is_fresh = False; break
                    if stype == "Short" and df['high'].iloc[j] >= gap['top']:    is_fresh = False; break
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


def _get_fvgs(df_h1, stype, bos_idx, choch_level=None, zone_lo=None, require_fresh=None):
    """FVG biasa (TANPA syarat volume): C1/C3 valid, CHOCH filter, zona entry, MAX_GAP, fresh-C1.
    zone_lo = batas bawah zona (default ENTRY_ZONE_LO). Dipakai utk zona dinamis (>= retrace terdalam).
    require_fresh = override REQUIRE_FRESH_C1 global (None = pakai default global)."""
    fresh_flag = REQUIRE_FRESH_C1 if require_fresh is None else require_fresh
    # peak_idx: high/low tertinggi setelah bos_idx — batas atas scan FVG (CHOCH→puncak)
    if stype == "Long":
        peak_idx_fvg = int(df_h1['high'].iloc[bos_idx:].idxmax()) if len(df_h1) > bos_idx else None
    else:
        peak_idx_fvg = int(df_h1['low'].iloc[bos_idx:].idxmin()) if len(df_h1) > bos_idx else None
    gaps = get_internal_gaps(df_h1, stype, bos_idx, require_fresh=fresh_flag, peak_idx=peak_idx_fvg)
    z_lo = ENTRY_ZONE_LO if zone_lo is None else zone_lo
    # FVG biasa: cukup field C1 (entry) & C3 (OCL) valid — tanpa syarat volume "kuat"
    gaps = [g for g in gaps
            if g.get('c3_open', 0) > 0
            and g.get('c1_close', 0) > 0]
    # Filter FVG yang straddle CHOCH
    if choch_level:
        if stype == "Long":
            gaps = [g for g in gaps if g['bottom'] >= choch_level]
        else:
            gaps = [g for g in gaps if g['top'] <= choch_level]
    # Filter ZONA ENTRY: C1 close harus di retrace z_lo..HI dari range BOS.
    # C1 close = titik golden ratio filter sekaligus trigger entry.
    if choch_level and len(df_h1) > bos_idx:
        if stype == "Long":
            B = float(df_h1['high'].iloc[bos_idx:].max())
            L = float(choch_level)
            rng = B - L
            if rng > 0:
                lo = B - ENTRY_ZONE_HI * rng   # batas terdalam (CHOCH)
                hi = B - z_lo * rng            # batas terdangkal
                gaps = [g for g in gaps if lo <= g.get('c1_close', 0) <= hi]
        else:
            B = float(df_h1['low'].iloc[bos_idx:].min())
            L = float(choch_level)
            rng = L - B
            if rng > 0:
                lo = B + z_lo * rng            # batas terdangkal
                hi = B + ENTRY_ZONE_HI * rng   # batas terdalam (CHOCH)
                gaps = [g for g in gaps if lo <= g.get('c1_close', 0) <= hi]
    # MAX_GAP_PCT: gap tidak boleh terlalu besar
    result = []
    for g in gaps:
        gap_size = g['top'] - g['bottom']
        ocl      = float(g.get('c3_open', g['bottom'] if stype == 'Short' else g['top']))
        if ocl > 0 and MAX_GAP_PCT > 0 and gap_size / ocl > MAX_GAP_PCT:
            continue
        # Fresh-C1: cek apakah FVG masih valid (ujung wick C1 belum tersentuh,
        # atau sudah tersentuh tapi harga belum lari 2R ke arah BOS).
        # Injek _sl_dist ke gap dict agar c1_is_fresh bisa pakai BOS range yg akurat.
        if fresh_flag:
            g['_sl_dist'] = SL_CAP_RANGE * rng if rng > 0 else 0
            if not c1_is_fresh(df_h1, g, stype):
                continue
        result.append(g)
    return result


def c1_is_fresh(df, gap, stype):
    """FVG hangus jika setelah puncak terbentuk ada candle yang menyentuh ujung wick C1
    (bottom gap untuk Long = high[C1], top gap untuk Short = low[C1]) — gap sudah 100% terisi.
    Cek dimulai SETELAH peak_idx karena leg impulsif ke puncak wajar melewati area C1."""
    c3i = gap.get('c3_idx')
    if c3i is None:
        return True
    peak_idx = gap.get('_peak_idx')
    start_k  = (int(peak_idx) + 1) if peak_idx is not None else (int(c3i) + 1)
    start_k  = max(start_k, int(c3i) + 1)
    if stype == "Long":
        c1_wick = float(gap.get('bottom', 0))   # high[C1]
    else:
        c1_wick = float(gap.get('top', 0))       # low[C1]
    if c1_wick <= 0:
        return True
    for k in range(start_k, len(df)):
        if stype == "Long" and float(df['low'].iloc[k]) <= c1_wick:
            return False
        if stype == "Short" and float(df['high'].iloc[k]) >= c1_wick:
            return False
    return True

def c2_wick_still_valid(df, gap, stype, sl_dist):
    """Cek apakah C2 wick masih valid sebagai entry saat ENTRY_C2_WICK=True.
    Kondisi HANGUS: C1.close sudah tersentuh DAN setelah itu harga jalan ke arah BOS
    melebihi C2_WICK_SKIP_R × sl_dist dari C1.close, tanpa pernah menyentuh C2 wick duluan.
    Kondisi VALID: C1.close belum tersentuh (normal/fresh), atau sudah tersentuh tapi
    harga sempat balik ke C2 wick sebelum lari jauh.
    sl_dist = jarak SL sebenarnya yg dipakai (10% BOS range)."""
    c3i = gap.get('c3_idx')
    c1c = float(gap.get('c1_close', 0))
    c2_entry = float(gap.get('c2_low' if stype == 'Long' else 'c2_high', 0))
    if c3i is None or c1c <= 0 or c2_entry <= 0 or sl_dist <= 0:
        return True   # data kurang -> asumsikan valid, biarkan filter lain yg handle

    skip_threshold = 2.0 * sl_dist
    n = len(df)
    c1_touched = False

    for k in range(int(c3i) + 1, n):
        lo = float(df['low'].iloc[k])
        hi = float(df['high'].iloc[k])
        if stype == 'Long':
            if not c1_touched:
                if lo <= c1c:
                    c1_touched = True
                # sebelum C1 tersentuh -> C2 wick pasti belum masalah
            else:
                # C1 sudah tersentuh: cek apakah C2 wick sempat tersentuh duluan
                if lo <= c2_entry:
                    return True   # harga balik ke C2 wick -> masih valid
                # cek apakah harga sudah lari ke arah BOS > skip_threshold dari C1.close
                if hi >= c1c + skip_threshold:
                    return False  # jalan duluan > 2R -> C2 wick hangus
        else:  # Short
            if not c1_touched:
                if hi >= c1c:
                    c1_touched = True
            else:
                if hi >= c2_entry:
                    return True   # harga balik ke C2 wick -> masih valid
                if lo <= c1c - skip_threshold:
                    return False  # jalan duluan > 2R -> C2 wick hangus

    return True  # tidak ada kondisi hangus terdeteksi di data yg tersedia


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
        risk_usd = balance * RISK_PCT
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

        order_value = qty * entry
        if order_value < 5.0:
            print(f"⚠️ {symbol}: Order value ~${order_value:.2f} < $5 minimum Bybit, skip "
                  f"(balance ${balance:.2f}, risk ${risk_usd:.2f}, dist {dist:.6f}).")
            return None

        sl_r         = round_price(sl,         info['tick_size'])
        trail_dist_r = round_price(trail_dist,  info['tick_size'])
        if trail_dist_r <= 0:
            trail_dist_r = round_price(dist * TRAIL_STOP, info['tick_size'])

        lev_int = 10
        try:
            max_lev = float(info.get('max_leverage', 10))
            lev_int = int(min(LEVERAGE, max_lev))
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
            positionIdx=_pidx(side),
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
            positionIdx=_pidx(side), timeInForce="IOC"
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
        risk_usd = balance * RISK_PCT
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

        order_value = qty * entry_p
        if order_value < MIN_ORDER_USD:
            if order_value >= ORDER_BUMP_FLOOR:
                # order sudah dekat $5 -> naikkan qty agar >= $5 (SL tetap, over-risk <=1.25x)
                old_ov = order_value
                qty = round_qty(MIN_ORDER_USD / entry_p, info['qty_step'])
                if qty * entry_p < MIN_ORDER_USD:
                    qty = round_qty(qty + info['qty_step'], info['qty_step'])
                order_value = qty * entry_p
                new_risk = qty * dist
                print(f"⬆️ {symbol}: order ${old_ov:.2f}->${order_value:.2f} "
                      f"(risk ${new_risk:.2f} ~ {new_risk/risk_usd:.2f}x target).")
            else:
                print(f"⚠️ {symbol}: Order ~${order_value:.2f} < ${ORDER_BUMP_FLOOR:.0f} "
                      f"(terlalu jauh dari ${MIN_ORDER_USD:.0f}), skip "
                      f"(balance ${balance:.2f}, risk ${risk_usd:.2f}, dist {dist:.6f}).")
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
            lev_int = int(min(LEVERAGE, max_lev))
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

        if USE_TP:
            tp_r = round_price(entry_p + RR_TP * dist if side == "Buy" else entry_p - RR_TP * dist, info['tick_size'])
            res = session.place_order(
                category=CATEGORY, symbol=symbol, side=side,
                orderType="Limit", qty=str(qty), price=str(entry_r),
                stopLoss=str(sl_r), takeProfit=str(tp_r),
                positionIdx=_pidx(side), timeInForce="GTC")
        else:
            res = session.place_order(
                category=CATEGORY, symbol=symbol, side=side,
                orderType="Limit", qty=str(qty), price=str(entry_r),
                stopLoss=str(sl_r), trailingStop=str(trail_r), activePrice=str(active_r),
                positionIdx=_pidx(side), timeInForce="GTC")
        if res['retCode'] == 0:
            return res['result']['orderId']
        print(f"⚠️ {symbol}: Limit order ditolak → {res.get('retMsg','')} (code:{res['retCode']})")
        return None
    except Exception as e:
        print(f"⚠️ {symbol}: place_limit_order error → {e}")
        return None


def place_market_entry(coin, side, curr_price, sl_p, tp_p):
    """Entry MARKET (untuk inducement) dgn SL+TP langsung. Sizing by risk. Return (order_id, qty) atau (None,None)."""
    try:
        info = get_instrument_info(coin)
        res_bal = session.get_wallet_balance(accountType="UNIFIED", coin="USDT")
        acct = res_bal['result']['list'][0]
        balance = float(acct['totalEquity'])
        avail   = float(acct.get('totalAvailableBalance') or balance)
        risk_usd = balance * RISK_PCT
        dist = abs(curr_price - sl_p)
        if dist <= 0: return None, None
        min_dist = curr_price * 0.002
        if dist < min_dist:
            dist = min_dist
            sl_p = curr_price - dist if side == "Buy" else curr_price + dist
        qty = round_qty(risk_usd / dist, info['qty_step'])
        if qty < info['min_qty']:
            print(f"⚠️ {coin}: induce qty {qty} < min {info['min_qty']}, skip."); return None, None
        if qty * curr_price < MIN_ORDER_USD:
            if qty * curr_price >= ORDER_BUMP_FLOOR:
                qty = round_qty(MIN_ORDER_USD / curr_price, info['qty_step'])
                if qty * curr_price < MIN_ORDER_USD:
                    qty = round_qty(qty + info['qty_step'], info['qty_step'])
            else:
                print(f"⚠️ {coin}: induce order ~${qty*curr_price:.2f} terlalu kecil, skip."); return None, None
        lev_int = int(min(LEVERAGE, float(info.get('max_leverage', 10))))
        try:
            session.set_leverage(category=CATEGORY, symbol=coin, buyLeverage=str(lev_int), sellLeverage=str(lev_int))
        except Exception as e:
            if '110043' not in str(e): print(f"   ⚠️ {coin}: set_leverage: {e}")
        required_margin = (qty * curr_price) / lev_int
        if required_margin > avail * 0.85:
            print(f"⚠️ {coin}: induce margin ~${required_margin:.2f} > avail ${avail:.2f}, skip."); return None, None
        tick = info['tick_size']
        sl_r = round_price(sl_p, tick)
        order_kwargs = dict(category=CATEGORY, symbol=coin, side=side,
                            orderType="Market", qty=str(qty),
                            stopLoss=str(sl_r), positionIdx=_pidx(side), timeInForce="IOC")
        if tp_p is not None:
            tp_r = round_price(tp_p, tick)
            order_kwargs['takeProfit'] = str(tp_r)
        else:
            tp_r = None
        res = session.place_order(**order_kwargs)
        if res['retCode'] == 0:
            print(f"   market entry: qty {qty} SL {sl_r}" + (f" TP {tp_r}" if tp_r else "") +
                  f" (margin ~${required_margin:.2f}, risk ${risk_usd:.2f})")
            return res['result']['orderId'], qty
        print(f"⚠️ {coin}: induce order ditolak → {res.get('retMsg','')} (code:{res['retCode']})")
        return None, None
    except Exception as e:
        print(f"⚠️ {coin}: place_market_entry error → {e}")
        return None, None


def check_inducement_entry(coin, df_h1, sh_h1, sl_h1):
    """Inducement entry (market, KEBALIK arah BOS besar). Berdampingan dgn limit FVG.
    BOS besar Long: inducement long 1-1 di pita 0-61% (dekat puncak); low-nya disapu M5 -> entry SHORT.
    BOS besar Short: cerminannya -> entry LONG. SL = 10% range BOS besar, TP 1:RR_TP."""
    if not INDUCEMENT_ENTRY or (not ALLOW_HEDGE and coin in active_positions):
        return False
    for stype in ("Long", "Short"):
        a = bos_anchors(df_h1, sh_h1, sl_h1, stype)
        if not a:
            continue
        # BOS besar WAJIB punya FVG di zona (sama syarat dgn jalur FVG limit).
        # Tak ada FVG -> BOS ini tak dipakai untuk entry FVG limit MAUPUN entry IDM.
        # NB1: require_fresh=False -> cek ini cuma soal "BOS-nya valid/pernah ada FVG",
        # bukan soal FVG-nya masih bisa dientry. Kalau ikut REQUIRE_FRESH_C1 global,
        # begitu limit FVG TERISI (entry = C1.close, jadi otomatis "disentuh"),
        # _get_fvgs balik kosong dan IDM jadi ikut mati padahal harusnya berdampingan.
        # NB2: zone_lo PAKAI STATIS (ENTRY_ZONE_LO), BUKAN deepest_retrace_lo() yg dinamis.
        # zone_lo dinamis itu utk keperluan PENEMPATAN limit FVG (area yg sudah dilewati
        # retrace tak dipakai lagi) — floor-nya naik terus seiring retrace makin dalam.
        # Kalau dipakai di sini, makin dalam retrace (= makin kuat alasan IDM utk entry
        # reversal), makin besar juga kemungkinan FVG lama "terhapus" dari hasil, sehingga
        # gate ini malah memblokir IDM justru pas momen yg paling IDM butuhkan.
        if not _get_fvgs(df_h1, stype, a['bos_idx'], a['choch_level'], zone_lo=ENTRY_ZONE_LO, require_fresh=False):
            continue
        B = a['B']; rng = a['bos_rng']
        # pita TITIK TRIGGER (level IDM) = 35-55% range BOS besar (dari puncak/lembah ke arah choch)
        if stype == "Long":
            band_lo, band_hi = B - INDUCEMENT_ZONE_HI * rng, B - INDUCEMENT_ZONE_LO * rng
        else:
            band_lo, band_hi = B + INDUCEMENT_ZONE_LO * rng, B + INDUCEMENT_ZONE_HI * rng
        # Jendela waktu: bos kecil dicari HANYA dari choch sampai PUNCAK (impuls), bukan setelah puncak.
        ts_lo = float(df_h1['ts'].iloc[a['bos_idx']])
        ts_hi = float(df_h1['ts'].iloc[a['peak_idx']])
        # Struktur bos kecil: H1 atau M5. TRIGGER (sweep): SELALU M5 close.
        df_m5 = get_data(coin, "5", limit=300)
        if df_m5 is None or len(df_m5) < 3:
            continue
        df_struct = df_h1 if INDUCEMENT_TF == "60" else df_m5
        idm = find_inducement(df_struct, stype, band_lo, band_hi, n=INDUCEMENT_SWING, ts_lo=ts_lo, ts_hi=ts_hi)
        if idm is None:
            continue
        prot = idm['prot']
        # SAPUAN di M5: candle M5 SETELAH puncak yang MENYENTUH level IDM (touch, tak harus tembus).
        # Entry HANYA bila sentuhan PERTAMA = candle M5 CLOSED TERAKHIR (sapuan baru, edge-trigger).
        # IDM harus fresh live: trigger hanya valid kalau belum pernah disentuh
        # sebelum bot jalan. Ini mencegah replay saat redeploy.
        bot_start_ms = bot_start_ts * 1000
        # Cek apakah trigger SUDAH pernah disentuh sebelum bot jalan (historis)
        m5_hist = df_m5[(df_m5['ts'] > ts_hi) & (df_m5['ts'] < bot_start_ms)]
        if stype == "Long":
            already_swept = len(m5_hist[m5_hist['low'] <= prot]) > 0
        else:
            already_swept = len(m5_hist[m5_hist['high'] >= prot]) > 0
        if already_swept:
            continue   # trigger sudah pernah disentuh sebelum bot jalan → skip, tidak fresh

        # Cek sweep dari candle M5 setelah puncak DAN setelah bot_start_ts (live)
        m5_after = df_m5[(df_m5['ts'] > ts_hi) & (df_m5['ts'] >= bot_start_ms)]
        if len(m5_after) == 0:
            continue
        if stype == "Long":
            breaches = m5_after.index[m5_after['low'] <= prot]
        else:
            breaches = m5_after.index[m5_after['high'] >= prot]
        last_closed_idx = df_m5.index[-2]
        if len(breaches) == 0:
            continue                       # IDM belum disapu sejak bot jalan -> tunggu
        # Sweep terjadi live setelah bot jalan → masuk idm_pending.
        sig = (stype, round(a['choch_level'], 10), round(a['swing_val'], 10))
        if inducement_done.get((coin, stype)) == sig:
            continue                       # struktur ini sudah pernah di-entry -> jangan ulang
        if stype == "Long":
            side, e_stype = "Sell", "Short"
        else:
            side, e_stype = "Buy", "Long"
        curr = float(df_m5.iloc[-1]['close'])
        trig = df_m5.iloc[-2]              # candle M5 yg menyapu (closed terakhir)
        sl_dist = SL_CAP_RANGE * rng
        if _akey(coin, e_stype) in active_positions or _akey(coin, e_stype) in idm_pending:
            continue                       # sisi IDM ini sudah terbuka / limit sudah terpasang

        # Filter konfluensi funding: blokir pasang limit IDM baru selama funding window AND gak searah.
        if FUNDING_FILTER and in_funding_window() and not funding_favors(e_stype, coin):
            rate = get_funding_rate(coin)
            print(f"   {coin}: IDM {e_stype} skip -> funding window aktif, gak searah (rate={rate})")
            continue

        if IDM_LIMIT_ENTRY or IDM_M5_ENGULF:
            # Filter momentum
            if INDUCEMENT_MOMENTUM_FILTER:
                eaten = momentum_eaten(df_h1, a['peak_idx'], stype)
                if eaten is not True:
                    reason = "data kurang (<min candle sejak puncak)" if eaten is None else "tak ada candle makan candle"
                    print(f"   {coin}: IDM {stype} skip -> momentum filter gagal ({reason})")
                    continue

            if IDM_M5_ENGULF:
                # ── M5 ENGULF MODE: simpan state monitor, entry nanti saat engulfing dikonfirmasi ──
                print(f"🎯 {coin}: IDM {stype} trigger={prot:.6g} tersapu → monitor M5 engulfing ({e_stype})")
                idm_pending[_akey(coin, e_stype)] = {
                    'coin': coin, 'side': side, 'e_stype': e_stype,
                    'order_id': None,           # tidak ada limit order
                    'entry': None, 'sl': None, 'placed_ts': time.time(),
                    'trigger': prot, 'rng': rng, 'sl_dist': sl_dist,
                    'swing_val': a['swing_val'], 'choch_level': a['choch_level'],
                    'peak_val': a['peak_val'], 'bos_type': e_stype,
                    # M5 monitor state
                    'm5_triggered': False,
                    'm5_focus_hi': 0.0, 'm5_focus_lo': 0.0, 'm5_focus_idx': 0,
                    'm5_hangus': False,
                }
                inducement_done[(coin, stype)] = sig
                rec = (
                    f"════ IDM M5 ENGULF MONITOR ════\n"
                    f"  {coin} | menunggu engulfing {e_stype} M5 | trigger={prot:.6g}\n"
                    f"  BOS BESAR ({stype}): break={a['swing_val']:.6g} choch={a['choch_level']:.6g} "
                    f"puncak={a['peak_val'] if a['peak_val'] is not None else a['B']:.6g} range={rng:.6g}\n"
                    f"  IDM trigger={prot:.6g}@{idm['prot_idx']} | batal jika >±{IDM_CANCEL_RANGE_PCT*100:.0f}% range"
                )
                log_entry(rec)
                if (not ALLOW_HEDGE) and coin in pending:
                    for d, st in list(pending[coin].items()):
                        if st.get('order_id'): cancel_order(coin, st['order_id'])
                    pending.pop(coin, None)
                return True

            # ── LIMIT MODE (IDM_M5_ENGULF=False, IDM_LIMIT_ENTRY=True) ──
            pidx = idm.get('prot_idx')
            if pidx is None or pidx < 0 or pidx >= len(df_struct):
                continue
            idm_candle = df_struct.iloc[pidx]
            hi_c, lo_c = float(idm_candle['high']), float(idm_candle['low'])
            rng_c = hi_c - lo_c
            if rng_c <= 0:
                continue
            if e_stype == "Long":
                entry_p = lo_c + IDM_LIMIT_FIB * rng_c
                sl_p = entry_p - sl_dist
            else:
                entry_p = hi_c - IDM_LIMIT_FIB * rng_c
                sl_p = entry_p + sl_dist
            print(f"🎯 {coin}: INDUCEMENT {stype} disentuh (level {prot:.6g}) → LIMIT {e_stype} @ "
                  f"{entry_p:.6g} ({IDM_LIMIT_FIB*100:.0f}% range candle H1 IDM {lo_c:.6g}-{hi_c:.6g} @idx{pidx}) | SL {sl_p:.6g}")
            oid = place_limit_order(coin, side, entry_p, sl_p)
            if oid:
                idm_pending[_akey(coin, e_stype)] = {
                    'coin': coin, 'side': side, 'e_stype': e_stype, 'order_id': oid,
                    'entry': entry_p, 'sl': sl_p, 'placed_ts': time.time(),
                    'trigger': prot, 'rng': rng,
                    'swing_val': a['swing_val'], 'choch_level': a['choch_level'],
                    'peak_val': a['peak_val'], 'bos_type': e_stype,
                }
                inducement_done[(coin, stype)] = sig
                rec = (
                    f"════ LIMIT INDUCEMENT ({IDM_LIMIT_FIB*100:.0f}% range candle H1 IDM) ════\n"
                    f"  {coin} | LIMIT {e_stype} @ {entry_p:.6g} | SL {sl_p:.6g}\n"
                    f"  BOS BESAR ({stype}): break={a['swing_val']:.6g} choch={a['choch_level']:.6g} "
                    f"puncak={a['peak_val'] if a['peak_val'] is not None else a['B']:.6g} range={rng:.6g}\n"
                    f"  IDM trigger={prot:.6g}@{idm['prot_idx']} (terakhir-di-pita; semua {[round(x,6) for x in idm.get('all_triggers',[prot])]})\n"
                    f"  CANDLE H1 IDM @idx{pidx}: low={lo_c:.6g} high={hi_c:.6g} "
                    f"→ entry {IDM_LIMIT_FIB*100:.0f}% = {entry_p:.6g}\n"
                    f"  Momentum filter: {'ON, lolos (candle makan candle)' if INDUCEMENT_MOMENTUM_FILTER else 'OFF'}"
                )
                log_entry(rec)
                if (not ALLOW_HEDGE) and coin in pending:
                    for d, st in list(pending[coin].items()):
                        if st.get('order_id'): cancel_order(coin, st['order_id'])
                    pending.pop(coin, None)
                return True
            continue

        # --- jalur lama: MARKET di harga sweep (kalau IDM_LIMIT_ENTRY=False) ---
        if e_stype == "Short":
            sl_p, tp_p = curr + sl_dist, curr - RR_TP * sl_dist
        else:
            sl_p, tp_p = curr - sl_dist, curr + RR_TP * sl_dist
        print(f"🎯 {coin}: INDUCEMENT {stype} disapu (level {prot:.6g}, pita {band_lo:.6g}-{band_hi:.6g}) "
              f"→ entry {e_stype} MARKET @ ~{curr:.6g} | SL {sl_p:.6g} TP {tp_p:.6g}")
        if _akey(coin, e_stype) in active_positions:
            continue                       # sisi IDM ini sudah terbuka -> jangan dobel
        oid, qty = place_market_entry(coin, side, curr, sl_p, tp_p)
        if oid:
            active_positions[_akey(coin, e_stype)] = {
                'coin': coin,
                'side': side, 'entry': curr, 'sl': sl_p, 'dist': abs(curr - sl_p),
                'trail_dist': 0, 'trail_engaged': False, 'trail_set': True,
                'last_price': curr, 'entry_time': time.time(),
                'peak': curr, 'peak_time': time.time(),
                'swing_val': a['swing_val'], 'bos_type': e_stype, 'rev_count': 0,
                'orig_ocl': curr, 'choch_level': a['choch_level'], 'peak_val': a['peak_val'],
                'swing2': a['peak_val'], 'kind': 'inducement',
            }
            inducement_done[(coin, stype)] = sig   # tandai struktur ini sudah di-entry (anti entry-ulang)
            rec = (
                f"════ ENTRY INDUCEMENT ════\n"
                f"  {coin} | entry {e_stype} MARKET @ ~{curr:.6g} qty {qty}\n"
                f"  BOS BESAR ({stype}): swing-1(break)={a['swing_val']:.6g} | choch={a['choch_level']:.6g} | "
                f"swing-2(puncak/lembah)={a['peak_val'] if a['peak_val'] is not None else a['B']:.6g} | range={rng:.6g}\n"
                f"  BOS KECIL (induce {INDUCEMENT_TF} {INDUCEMENT_SWING}-{INDUCEMENT_SWING}, dari choch→puncak): "
                f"swing-1={idm['micro_val']:.6g}@{idm['micro_idx']} | "
                f"choch-TRIGGER={prot:.6g}@{idm['prot_idx']} "
                f"({(((a['B']-prot) if stype=='Long' else (prot-a['B']))/rng*100 if rng>0 else 0):.0f}% dari puncak, "
                f"terakhir-di-pita; {idm.get('n_trigger',1)} leg di pita, semua trigger {[round(x,6) for x in idm.get('all_triggers',[prot])]}) | "
                f"pita35-55%={band_lo:.6g}-{band_hi:.6g}\n"
                f"  TRIGGER(M5 close): ts={int(trig['ts'])} low={float(trig['low']):.6g} high={float(trig['high']):.6g} "
                f"close={float(trig['close']):.6g} (menyapu IDM {prot:.6g})\n"
                f"  SL={sl_p:.6g} (10% range) | TP={tp_p:.6g} (1:{RR_TP})"
            )
            log_entry(rec)
            if (not ALLOW_HEDGE) and coin in pending:   # one-way: batalkan limit FVG; hedge: biarkan
                for d, st in list(pending[coin].items()):
                    if st.get('order_id'):
                        cancel_order(coin, st['order_id'])
                pending.pop(coin, None)
            return True
    return False


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


def get_open_position(symbol, want_side=None):
    try:
        res = session.get_positions(category=CATEGORY, symbol=symbol)
        if res['retCode'] == 0:
            for pos in res['result']['list']:
                if float(pos['size']) <= 0:
                    continue
                if ALLOW_HEDGE and want_side is not None and pos.get('side') != want_side:
                    continue            # hedge: ambil HANYA sisi yg diminta (Buy/Sell)
                return pos
        return None
    except:
        return None


def move_sl(symbol, new_sl, side="Buy"):
    try:
        res = session.set_trading_stop(
            category=CATEGORY, symbol=symbol,
            stopLoss=str(new_sl),
            positionIdx=_pidx(side)
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


def check_trailing_sl(key):
    """Dipanggil tiap M5 close untuk SATU posisi (key = 'COIN' one-way / 'COIN|Long' hedge).
    Cek apakah posisi tutup. Jika ya hapus dari active_positions; jika buka set trailing."""
    if key not in active_positions:
        return
    p    = active_positions[key]
    coin = p.get('coin', key)
    side = p.get('side')
    pos  = get_open_position(coin, side)

    if pos is None:
        actual_exit = _get_actual_exit_price(coin)
        exit_str    = f"{actual_exit:.6f}" if actual_exit else "?"
        entry       = p['entry']
        orig_ocl    = p.get('orig_ocl', entry)

        # (reverse-on-SL dibuang — SMC inti: SL kena = trade selesai, tidak balik arah)
        print(f"📭 {coin} {p.get('bos_type','')}: Posisi tutup @ {exit_str}.")
        done_setups[coin] = {
            'swing_val': p.get('swing_val'),
            'stype'    : p.get('bos_type'),
            'used_ocl' : orig_ocl,
        }
        del active_positions[key]
        return

    # Posisi masih buka — update last_price, peak, dan cek trail timeout
    try:
        curr_price = float(pos['markPrice'])
        active_positions[key]['last_price'] = curr_price

        entry = p['entry']
        dist  = p.get('dist', 0)
        side  = p['side']

        # Track peak (favorable extreme) dan waktu terakhir peak bergerak
        peak      = p.get('peak', entry)
        peak_time = p.get('peak_time', p.get('entry_time', time.time()))
        new_peak  = max(peak, curr_price) if side == 'Buy' else min(peak, curr_price)
        if new_peak != peak:
            active_positions[key]['peak']      = new_peak
            active_positions[key]['peak_time'] = time.time()
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
                del active_positions[key]
            return

        # Pasang trailing stop via set_trading_stop saat pertama posisi terdeteksi
        # activePrice = entry + TRAIL_ACT_R×dist → trail aktif setelah +1.5R profit (sinkron backtest)
        if (not USE_TP) and TRAIL_STOP > 0 and dist > 0 and not p.get('trail_set', False):
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
                        positionIdx=_pidx(side)
                    )
                    if res_ts['retCode'] == 0:
                        active_positions[key]['trail_set'] = True
                        print(f"📍 {coin}: Trailing stop {trail_r} dipasang "
                              f"(aktif @ {active_p} = entry+{TRAIL_ACT_R}R)")
                    else:
                        print(f"⚠️ {coin}: Gagal set trailing stop: "
                              f"{res_ts.get('retMsg','')} (code:{res_ts['retCode']})")
                except Exception as e:
                    print(f"⚠️ {coin}: set_trading_stop error: {e}")

        if (not USE_TP) and dist > 0 and not p.get('trail_engaged', False):
            if side == "Buy"  and curr_price >= entry + TRAIL_ACT_R * dist:
                active_positions[key]['trail_engaged'] = True
                print(f"✅ {coin}: Trail engaged @ {curr_price:.6f} (+{TRAIL_ACT_R}R)")
            elif side == "Sell" and curr_price <= entry - TRAIL_ACT_R * dist:
                active_positions[key]['trail_engaged'] = True
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
# REPLAY H1 — reconstruct state saat startup (fvg)
# ============================================================

def replay_h1(coin, df_h1):
    sh_h1, sl_h1 = find_last_swing_bos(df_h1)
    if not sh_h1 or not sl_h1:
        return None

    closed_h1 = df_h1.iloc[-2]
    is_long = False; is_short = False
    swing_val = None; bos_idx = None

    brk_idx = None
    for sh in sh_h1[-3:]:
        if closed_h1['close'] > sh['val']:
            is_long = True; swing_val = sh['val']; brk_idx = sh['idx']
    for sl in sl_h1[-3:]:
        if closed_h1['close'] < sl['val']:
            is_short = True; swing_val = sl['val']; brk_idx = sl['idx']

    if not (is_long or is_short):
        return None

    stype = "Short" if is_short else "Long"
    bos_idx, choch_level, _pk = impulse_anchors(stype, swing_val, brk_idx, sh_h1, sl_h1, df_h1)
    if bos_idx is None or choch_level is None:
        return None

    gaps = _get_fvgs(df_h1, stype, bos_idx, choch_level)
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
    print(f"\n📊 {coin}: BOS {stype} | Swing: {swing_val:.6g} | {len(gaps)} FVG")
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
# CORE LOOP — fvg strategy
# BOS H1 → FVG (C3 vol > avg20H) → OCL touch M5
# → Touch vol filter → Entry market + trailing stop
# ============================================================

# ============================================================
# CORE LOOP — SMC inti
# BOS H1 -> FVG -> Limit entry @ C1.close -> SL C1 invalidation -> Trailing
# ============================================================

def pick_bos_swing(df, sh_h1, sl_h1, stype):
    """Pilih swing-1 BOS: swing 5-5 terbaru yang di-break & menghasilkan struktur LENGKAP (choch 5-5 sah).
    Return (swing_val, brk_idx) atau (None, None)."""
    idx_arr = df.index
    up = (stype == "Long")
    swings = sh_h1 if up else sl_h1
    ext = df['high'] if up else df['low']           # break dihitung pakai WICK (high/low), konsisten dgn puncak
    def _broken(s):
        later = ext[idx_arr > s['idx']]
        if len(later) == 0: return False
        return bool((later > s['val']).any()) if up else bool((later < s['val']).any())
    cands = sorted([s for s in swings[-8:] if _broken(s)], key=lambda x: x['idx'], reverse=True)
    for s in cands:
        bi, ch, pk = impulse_anchors(stype, s['val'], s['idx'], sh_h1, sl_h1, df)
        if bi is not None and ch is not None:
            return s['val'], s['idx']
    if cands:
        return cands[0]['val'], cands[0]['idx']
    return None, None


def apply_latest_leg(df, sh, sl, stype, swing_val, brk_idx, choch_level, peak_val, B, peak_idx, bos_idx):
    """FORWARD-CHAINING sub-puncak fraktal HALUS (n=SUBLEG_BARS) -> baca leg kiri->kanan tapi
    pakai swing tervalidasi (saring noise bar). choch & swing-1 selalu ikut LEG TERAKHIR;
    ambang retrace 50% diukur PER-LEG. Telusuri tiap sub-puncak halus setelah swing-1:
      - high baru TANPA retrace>=50% leg -> EXTENSION (puncak tumbuh, choch tetap)
      - high baru SETELAH retrace>=50%   -> REBREAK -> leg baru:
            swing-1 = puncak lama, choch = protective low/high HALUS TERBARU di leg baru, swing-2 = high baru
            (tak ada protective halus di leg baru -> None / tak ada BOS)
    Return (swing_val, brk_idx, choch_level, peak_val, bos_idx) atau None."""
    fsh, fsl = find_last_swing_bos(df, n=SUBLEG_BARS)
    if stype == "Long":
        peaks = sorted([x for x in fsh if x['idx'] > brk_idx and x['val'] > swing_val], key=lambda x: x['idx'])
    else:
        peaks = sorted([x for x in fsl if x['idx'] > brk_idx and x['val'] < swing_val], key=lambda x: x['idx'])
    if not peaks:
        return (swing_val, brk_idx, choch_level, peak_val, bos_idx)

    def prot_between(i_lo, i_hi):   # protective swing HALUS TERBARU (idx terbesar) di (i_lo, i_hi)
        if stype == "Long":
            c = [x for x in fsl if i_lo <= x['idx'] < i_hi]
        else:
            c = [x for x in fsh if i_lo <= x['idx'] < i_hi]
        return max(c, key=lambda x: x['idx']) if c else None

    def retr(s2v, chv, i_from, i_to):   # retrace >= RETRACE_LOCK leg [chv..s2v] SETELAH candle swing-2?
        a = i_from + 1                  # JANGAN hitung candle swing-2 sendiri (low/high-nya bagian pembentuk swing-2)
        if a > i_to:
            return False
        if stype == "Long":
            half = s2v - RETRACE_LOCK * (s2v - chv)
            return float(df['low'].iloc[a:i_to + 1].min()) <= half
        else:
            half = s2v + RETRACE_LOCK * (chv - s2v)
            return float(df['high'].iloc[a:i_to + 1].max()) >= half

    def rebreak_choch(i_lo, i_hi):   # choch leg rebreak: swing HALUS terbaru, ATAU titik retrace TERDALAM
        nch = prot_between(i_lo, i_hi)
        if nch is not None:
            return nch['val'], nch['idx']
        seg_lo = min(i_lo + 1, i_hi)     # fallback: tak ada swing halus (mis. 1 candle besar) -> retrace terdalam
        if stype == "Long":
            s = df['low'].iloc[seg_lo:i_hi + 1]
            return (float(s.min()), int(s.idxmin())) if len(s) else None
        else:
            s = df['high'].iloc[seg_lo:i_hi + 1]
            return (float(s.max()), int(s.idxmax())) if len(s) else None

    higher = (lambda a, b: a > b) if stype == "Long" else (lambda a, b: a < b)
    # leg 0: choch = choch DALAM (launch) dari impulse_anchors, BUKAN fine-low terbaru
    cur_s1v, cur_s1i = swing_val, brk_idx
    cur_chv, cur_chi = choch_level, bos_idx
    cur_s2v, cur_s2i = peaks[0]['val'], peaks[0]['idx']
    # chain sisa sub-puncak halus
    for p in peaks[1:]:
        if not higher(p['val'], cur_s2v):
            continue
        if retr(cur_s2v, cur_chv, cur_s2i, p['idx']):     # REBREAK (retrace >=50% leg sebenarnya)
            rc = rebreak_choch(cur_s2i, p['idx'])
            if rc is None:
                return None
            cur_s1v, cur_s1i = cur_s2v, cur_s2i
            cur_chv, cur_chi = rc
            cur_s2v, cur_s2i = p['val'], p['idx']
        else:                                             # EXTENSION: choch TETAP, cuma puncak tumbuh
            cur_s2v, cur_s2i = p['val'], p['idx']
    # puncak MENTAH B di luar sub-puncak halus terakhir
    final_peak_val = cur_s2v
    if higher(B, cur_s2v):
        if retr(cur_s2v, cur_chv, cur_s2i, peak_idx):     # REBREAK ke B
            rc = rebreak_choch(cur_s2i, peak_idx)
            if rc is None:
                return None
            cur_s1v, cur_s1i = cur_s2v, cur_s2i
            cur_chv, cur_chi = rc
            final_peak_val = None     # puncak = B mentah (belum jadi swing)
        # else: EXTENSION ke B -> final_peak_val tetap cur_s2v
    return (cur_s1v, cur_s1i, cur_chv, final_peak_val, cur_chi)




def bos_anchors(df, sh_h1, sl_h1, stype):
    """Struktur BOS besar (tanpa perlu FVG) untuk arah `stype`.
    Return dict {swing_val, brk_idx, choch_level, peak_val, B, bos_idx, bos_rng} atau None bila tak ada/invalid."""
    if not sh_h1 or not sl_h1:
        return None
    swing_val, brk_idx = pick_bos_swing(df, sh_h1, sl_h1, stype)
    if swing_val is None:
        return None
    bos_idx, choch_level, peak_val = impulse_anchors(stype, swing_val, brk_idx, sh_h1, sl_h1, df)
    if bos_idx is None or choch_level is None:
        return None
    if stype == "Long":
        sub = df['high'].iloc[bos_idx:]; B = float(sub.max()); peak_idx = int(sub.idxmax())
    else:
        sub = df['low'].iloc[bos_idx:];  B = float(sub.min()); peak_idx = int(sub.idxmin())
    # === ATURAN LEG TERBARU (extension vs rebreak) — bersama jalur FVG ===
    res = apply_latest_leg(df, sh_h1, sl_h1, stype, swing_val, brk_idx, choch_level, peak_val, B, peak_idx, bos_idx)
    if res is None:
        return None
    swing_val, brk_idx, choch_level, peak_val, bos_idx = res
    bos_rng = (B - choch_level) if stype == "Long" else (choch_level - B)
    if bos_rng <= 0:
        return None
    # invalidasi: choch ditembus historis ATAU rebreak swing-2
    if choch_is_broken(df, bos_idx, choch_level, stype):
        return None
    if REBREAK_INVALID and peak_val is not None and \
       rebreak_invalid(df, bos_idx, peak_val, choch_level, stype, RETRACE_LOCK):
        return None
    return {'swing_val': swing_val, 'brk_idx': brk_idx, 'choch_level': choch_level,
            'peak_val': peak_val, 'B': B, 'bos_idx': bos_idx, 'peak_idx': peak_idx, 'bos_rng': bos_rng}


def find_inducement(df_tf, big_stype, band_lo, band_hi, n=1, ts_lo=None, ts_hi=None):
    """Inducement = RANTAI mini-BOS dari choch->puncak (jendela ts_lo..ts_hi).
    Cara: telusuri record-high berturut (Long). Tiap kali record ditembus record berikutnya = 1 leg/IDM.
      - TRIGGER leg = low TERENDAH di antara dua record (raw low candle, eksklusif candle record).
      - Short = cermin: record-LOW, trigger = high TERTINGGI antar record.
    IDM AKTIF = leg TERAKHIR yang trigger-nya jatuh di pita [band_lo,band_hi] (35-60% range BOS besar).
      Kalau leg terakhir terlalu dangkal (<35%, di luar pita) -> mundur ke leg sebelumnya yg di pita.
    Return {prot(trigger), prot_idx, micro_val(peak ditembus), micro_idx, n_trigger, all_triggers} atau None."""
    if df_tf is None or len(df_tf) < (2 * n + 1):
        return None
    sh_tf, sl_tf = find_last_swing_bos(df_tf, n=n)
    if not sh_tf or not sl_tf:
        return None
    ts_col = df_tf['ts']
    def _in_win(idx):
        if ts_lo is None:
            return True
        t = float(ts_col.iloc[idx])
        return ts_lo <= t <= ts_hi
    up = (big_stype == "Long")
    piv = [s for s in (sh_tf if up else sl_tf) if _in_win(s['idx'])]
    piv.sort(key=lambda s: s['idx'])
    if len(piv) < 2:
        return None
    lo_a = df_tf['low'].values; hi_a = df_tf['high'].values
    legs = []   # (trigger_val, trigger_idx, broken_peak_val, broken_peak_idx)
    rec_v, rec_i = piv[0]['val'], piv[0]['idx']
    for s in piv[1:]:
        is_break = (s['val'] > rec_v) if up else (s['val'] < rec_v)
        if not is_break:
            continue                                   # lower-high (Long) / higher-low (Short) -> bukan record, lewati
        a_seg, b_seg = rec_i + 1, s['idx']             # low/high antar record, EKSKLUSIF candle record
        if b_seg > a_seg:
            if up:
                seg = lo_a[a_seg:b_seg]; off = int(seg.argmin()); tval = float(seg.min())
            else:
                seg = hi_a[a_seg:b_seg]; off = int(seg.argmax()); tval = float(seg.max())
            legs.append((tval, a_seg + off, rec_v, rec_i))
        rec_v, rec_i = s['val'], s['idx']              # record maju
    if not legs:
        return None
    inband = [lg for lg in legs if band_lo <= lg[0] <= band_hi]
    if not inband:
        return None
    best = inband[-1]                                  # leg TERAKHIR (terdekat puncak) yg di pita
    return {'prot': best[0], 'prot_idx': best[1], 'micro_val': best[2], 'micro_idx': best[3],
            'n_trigger': len(inband), 'all_triggers': [round(lg[0], 10) for lg in legs]}


def build_setup_from_bos(coin, df_h1_live, sh_h1, sl_h1, closed_h1, verbose=True, force_dir=None):
    """Deteksi BOS H1 terbaru -> FVG -> bangun setup WAIT_APPROACH.
    force_dir='Long'/'Short' => deteksi HANYA arah itu (untuk monitoring dua arah).
    Return (setup_dict, logline) atau (None, None). TIDAK menyentuh pending."""
    if not sh_h1 or not sl_h1:
        return None, None
    is_long = False; is_short = False; swing_val = None; brk_idx = None
    if force_dir in (None, "Long"):
        sv, bi = pick_bos_swing(df_h1_live, sh_h1, sl_h1, "Long")
        if sv is not None: is_long = True; swing_val = sv; brk_idx = bi
    if force_dir in (None, "Short"):
        sv, bi = pick_bos_swing(df_h1_live, sh_h1, sl_h1, "Short")
        if sv is not None: is_short = True; swing_val = sv; brk_idx = bi
    if not (is_long or is_short):
        if verbose: print(f"   {coin}: tidak ada BOS {force_dir or 'H1'}")
        return None, None
    if force_dir == "Long":
        stype = "Long"
    elif force_dir == "Short":
        stype = "Short"
    else:
        stype = "Short" if is_short else "Long"
    bos_idx, choch_level, peak_val = impulse_anchors(stype, swing_val, brk_idx, sh_h1, sl_h1, df_h1_live)
    if swing_val is None or bos_idx is None or choch_level is None:
        if verbose:
            if swing_val is None:
                print(f"   {coin}: tak ada swing 5-5 yang ter-break ({stype})")
            else:
                if stype == "Long":
                    pk_idx = int(df_h1_live['high'].iloc[brk_idx:].idxmax())
                    pk_val = float(df_h1_live['high'].iloc[brk_idx:].max())
                    cand_list = sl_h1; what = "swingLow"
                else:
                    pk_idx = int(df_h1_live['low'].iloc[brk_idx:].idxmin())
                    pk_val = float(df_h1_live['low'].iloc[brk_idx:].min())
                    cand_list = sh_h1; what = "swingHigh"
                tags = []
                for x in cand_list:
                    if x['idx'] < brk_idx:   pos = "✗sblm-break"
                    elif x['idx'] >= pk_idx: pos = "✗stlh-puncak"
                    else:                    pos = "✓DALAM"
                    tags.append(f"{x['val']:.6g}@{x['idx']}[{pos}]")
                body = ', '.join(tags) if tags else '(tak ada swing 5-5 sama sekali)'
                print(f"   {coin}: BOS {stype} tak lengkap — break={swing_val:.6g}@{brk_idx} puncak={pk_val:.6g}@{pk_idx} | {what}5-5 kandidat choch: {body}")
        return None, None
    # Puncak/lembah B + indeksnya (ekstrem langsung, tanpa nunggu)
    if stype == "Long":
        sub = df_h1_live['high'].iloc[bos_idx:]; _B = float(sub.max()); peak_idx = int(sub.idxmax())
    else:
        sub = df_h1_live['low'].iloc[bos_idx:]; _B = float(sub.min()); peak_idx = int(sub.idxmin())
    # === ATURAN LEG TERBARU (extension vs rebreak) — sama dgn jalur inducement ===
    res = apply_latest_leg(df_h1_live, sh_h1, sl_h1, stype, swing_val, brk_idx, choch_level, peak_val, _B, peak_idx, bos_idx)
    if res is None:
        if verbose: print(f"   {coin}: BOS {stype} — swing-2 ditembus & leg baru tanpa choch 5-5 (tunggu BOS baru)")
        return None, None
    swing_val, brk_idx, choch_level, peak_val, bos_idx = res
    bos_rng = (_B - choch_level) if stype == "Long" else (choch_level - _B)
    # CHoCH invalidation HISTORIS: kalau SETELAH puncak ada candle yang CLOSE menembus choch -> BOS mati
    # (walau harga sekarang sudah balik). Sebelumnya cuma cek close terakhir -> bocor.
    seg_cl = df_h1_live['close'].iloc[peak_idx:]
    choch_broken = bool((seg_cl < choch_level).any()) if stype == "Long" else bool((seg_cl > choch_level).any())
    if choch_broken:
        if verbose: print(f"   {coin}: BOS {stype} sudah CHoCH — harga pernah close lewat choch {choch_level:.6g} (mati, tunggu BOS baru)")
        return None, None
    # Invalidasi struktur: swing-2 = puncak swing 5-5; bila harga retrace >= RETRACE_LOCK
    # lalu CLOSE melewati swing-2 -> BOS invalid (struktur baru), tunggu BOS baru.
    if REBREAK_INVALID and peak_val is not None and \
       rebreak_invalid(df_h1_live, bos_idx, peak_val, choch_level, stype, RETRACE_LOCK):
        if verbose: print(f"   {coin}: BOS {stype} INVALID — retrace>={RETRACE_LOCK*100:.0f}% lalu close lewati swing-2 {peak_val:.6g} (tunggu BOS baru)")
        return None, None
    # === GATE: BOS besar WAJIB punya IDM mini-BOS di dalamnya (lebih ketat, simetris dgn jalur IDM) ===
    if REQUIRE_IDM_FOR_FVG:
        if stype == "Long":
            ib_lo, ib_hi = _B - INDUCEMENT_ZONE_HI * bos_rng, _B - INDUCEMENT_ZONE_LO * bos_rng
        else:
            ib_lo, ib_hi = _B + INDUCEMENT_ZONE_LO * bos_rng, _B + INDUCEMENT_ZONE_HI * bos_rng
        its_lo = float(df_h1_live['ts'].iloc[bos_idx])
        its_hi = float(df_h1_live['ts'].iloc[peak_idx])
        df_idm = df_h1_live if INDUCEMENT_TF == "60" else get_data(coin, "5", limit=300)
        idm_chk = None
        if df_idm is not None:
            idm_chk = find_inducement(df_idm, stype, ib_lo, ib_hi, n=INDUCEMENT_SWING, ts_lo=its_lo, ts_hi=its_hi)
        if idm_chk is None:
            if verbose:
                print(f"   {coin}: BOS {stype} TAK ada IDM mini-BOS {INDUCEMENT_SWING}-{INDUCEMENT_SWING} "
                      f"di pita {INDUCEMENT_ZONE_LO*100:.0f}-{INDUCEMENT_ZONE_HI*100:.0f}% (skip FVG limit) | "
                      f"break:{swing_val:.6g} choch:{choch_level:.6g} puncak:{_B:.6g}")
            return None, None
    zlo = deepest_retrace_lo(df_h1_live, bos_idx, choch_level, stype)
    gaps = _get_fvgs(df_h1_live, stype, bos_idx, choch_level, zone_lo=zlo)
    if not gaps:
        if verbose:
            raw = get_internal_gaps(df_h1_live, stype, bos_idx)
            Bp = _B; rng = bos_rng
            z618 = (Bp - zlo * rng) if stype == "Long" else (Bp + zlo * rng)
            tags = []
            for g in raw:
                c1c = float(g.get('c1_close', 0))
                r = ((Bp - c1c) if stype == "Long" else (c1c - Bp)) / rng * 100 if rng > 0 else 0
                if stype == "Long" and g['bottom'] < choch_level:
                    why = "choch"
                elif stype == "Short" and g['top'] > choch_level:
                    why = "choch"
                else:
                    if stype == "Long":
                        lo = Bp - ENTRY_ZONE_HI * rng; hi = Bp - zlo * rng
                    else:
                        lo = Bp + zlo * rng; hi = Bp + ENTRY_ZONE_HI * rng
                    if not (lo <= c1c <= hi):
                        why = "dilewati" if r < zlo * 100 else "zona"
                    else:
                        gs = g['top'] - g['bottom']; ocl = float(g.get('c3_open', 0))
                        if ocl > 0 and MAX_GAP_PCT > 0 and gs / ocl > MAX_GAP_PCT:
                            why = f"gap{gs / ocl * 100:.2f}%"
                        elif REQUIRE_FRESH_C1 and not c1_is_fresh(df_h1_live, g, stype):
                            why = "stale"
                        else:
                            why = "OK"
                tags.append(f"{r:.0f}%:{why}")
            print(f"   {coin}: BOS {stype} tdk ada FVG di zona | break={swing_val:.6g} "
                  f"choch={choch_level:.6g} puncak={Bp:.6g} | rawFVG={len(raw)} "
                  f"[{', '.join(tags)}] (zona>={zlo*100:.1f}%@{z618:.6g}, maxgap={MAX_GAP_PCT*100:.2f}%)")
        return None, None
    bos_ts = df_h1_live['ts'].iloc[bos_idx]
    g0 = gaps[0]
    c1_c = float(g0.get('c1_close', 0)); c1_l = float(g0.get('c1_low', 0)); c1_h = float(g0.get('c1_high', 0))
    if not (c1_c > 0 and c1_h > c1_l):
        return None, None
    gap_s = float(g0['top']) - float(g0['bottom'])
    # Trigger entry = ujung C3 (batas gap: top untuk Long, bottom untuk Short)
    if stype == 'Long':
        entry_adj = c1_c                  # C1 close = trigger sentuhan M5
        dist = 0.0; sl_entry = entry_adj  # akan di-override SL_FIXED_RANGE di bawah
    else:
        entry_adj = c1_c                  # C1 close = trigger sentuhan M5
        dist = 0.0; sl_entry = entry_adj

    import datetime as _dt
    _h_s = _dt.datetime.utcfromtimestamp(df_h1_live.iloc[-1]['ts_ms'] / 1000).hour if 'ts_ms' in df_h1_live.columns else -1
    if _h_s >= 0:
        _sesi = 'Asia' if _h_s < 8 else ('London' if _h_s < 13 else 'NY')
        _allowed = SESSION_FILTER.get(coin)
        if _allowed is not None and _sesi not in _allowed:
            return None, None
    # Filter konfluensi funding: cuma ambil entry baru SELAMA funding window AND gak searah.
    if FUNDING_FILTER and in_funding_window() and not funding_favors(stype, coin):
        if verbose:
            rate = get_funding_rate(coin)
            print(f"   {coin}: BOS {stype} skip FVG limit -> funding window aktif, gak searah (rate={rate})")
        return None, None
    # SL: mode FIXED 10% range BOS (di setiap situasi), atau ikut C1 dengan cap 10% range
    if SL_FIXED_RANGE and bos_rng > 0:
        dist = SL_CAP_RANGE * bos_rng
        sl_entry = entry_adj - dist if stype == 'Long' else entry_adj + dist
    elif SL_CAP_RANGE > 0 and bos_rng > 0 and dist > SL_CAP_RANGE * bos_rng:
        dist = SL_CAP_RANGE * bos_rng
        sl_entry = entry_adj - dist if stype == 'Long' else entry_adj + dist
    # Floor Bybit: kalau dist kepecil, perbesar (jaga-jaga range BOS sangat sempit)
    min_d = entry_adj * 0.002
    if dist < min_d:
        if MIN_DIST_FLOOR:
            dist = min_d; sl_entry = entry_adj - dist if stype == 'Long' else entry_adj + dist
        else:
            return None, None
    # (guard done_setups dihapus — anti-retrade kini lewat REQUIRE_FRESH_C1)
    choch_str = f"{choch_level:.6g}" if choch_level else "—"
    _slr = (dist / bos_rng * 100) if bos_rng > 0 else 0
    logline = (f"\n📊 {coin} | BOS {stype} | break:{swing_val:.6g} puncak:{_B:.6g} CHOCH:{choch_str} | "
               f"OCL:{c1_c:.6f} Entry:{entry_adj:.6f} SL:{sl_entry:.6f} "
               f"dist:{dist/c1_c*100:.3f}% (SL {_slr:.1f}% range) Gap:{gap_s/c1_c*100:.3f}%")
    setup = {
        'type': stype, 'phase': 'WAIT_APPROACH', 'entry': entry_adj, 'sl': sl_entry,
        'dist': dist, 'orig_ocl': c1_c,   # C1 close = trigger sentuhan M5
        'fvg_list': gaps, 'bos_ts': bos_ts, 'bos_rng': bos_rng,
        'created_ts': time.time(),
        'bos_idx': bos_idx, 'swing_val': swing_val, 'choch_level': choch_level,
        'peak_val': _B, 'swing2': peak_val, 'brk_idx': brk_idx,
        # M5 engulfing monitor state
        'm5_c1c_touched': False,
        'm5_focus_hi': 0.0, 'm5_focus_lo': 0.0, 'm5_focus_idx': 0,
    }
    return setup, logline


def _count_slots():
    """Jumlah WAIT_FILL di semua coin & arah (untuk plafon MAX_CONCURRENT)."""
    nf = 0
    for d in pending.values():
        for s in d.values():
            if s.get('phase') == 'WAIT_FILL':
                nf += 1
    return nf


def check_m5_engulfing(coin, setup, df_m5, bos_rng):
    """Monitor M5 setelah C1 close H1 tersentuh. Cari konfirmasi engulfing untuk market order.
    Return: dict {'entry': float, 'sl': float, 'side': str} jika engulfing terkonfirmasi,
            None jika belum ada konfirmasi.
    State disimpan di setup dict:
      'm5_c1c_touched' : bool  — apakah C1 close H1 sudah tersentuh di M5
      'm5_focus_hi'    : float — high candle fokus aktif
      'm5_focus_lo'    : float — low candle fokus aktif
      'm5_focus_idx'   : int   — index candle fokus di df_m5
    """
    if df_m5 is None or len(df_m5) < 2:
        return None
    stype   = setup['type']
    c1c     = float(setup.get('orig_ocl', 0))   # C1 close H1 = threshold sentuhan
    if c1c <= 0:
        return None

    # ── Filter: hanya proses candle M5 yang terbentuk SETELAH setup dibuat ──
    # Mencegah bot mereplay engulfing historis saat redeploy.
    created_ts_ms = setup.get('created_ts', 0) * 1000   # detik → ms
    if created_ts_ms > 0 and 'ts' in df_m5.columns:
        df_m5 = df_m5[df_m5['ts'] >= created_ts_ms].reset_index(drop=True)
        if len(df_m5) < 2:
            return None   # belum ada candle baru sejak setup dibuat

    # ── Init: cari candle M5 PALING AWAL yang menyentuh trigger (scan maju) ──
    # Scan semua candle kecuali yg berjalan (iloc[-1])
    n = len(df_m5)
    closed_end = n - 1   # index eksklusif: loop sampai < closed_end (tidak termasuk candle berjalan)
    if not setup.get('m5_c1c_touched'):
        for i in range(closed_end):
            lo = float(df_m5['low'].iloc[i])
            hi = float(df_m5['high'].iloc[i])
            touched = (stype == 'Long' and lo <= c1c) or (stype == 'Short' and hi >= c1c)
            if touched:
                setup['m5_c1c_touched'] = True
                setup['m5_focus_hi']    = hi
                setup['m5_focus_lo']    = lo
                setup['m5_focus_idx']   = i
                print(f"   {coin} {stype}: C1 close ({c1c:.6g}) tersentuh M5 idx={i} "
                      f"hi={hi:.6g} lo={lo:.6g} — mulai monitor engulfing")
                break
        if not setup.get('m5_c1c_touched'):
            return None   # belum tersentuh

    # ── Scan candle setelah fokus s/d candle closed terbaru (tidak termasuk candle berjalan) ──
    focus_idx = setup.get('m5_focus_idx', 0)
    focus_hi  = float(setup['m5_focus_hi'])
    focus_lo  = float(setup['m5_focus_lo'])

    for i in range(focus_idx + 1, closed_end):
        lo = float(df_m5['low'].iloc[i])
        hi = float(df_m5['high'].iloc[i])
        cl = float(df_m5['close'].iloc[i])

        # ── Cek engulfing DULU sebelum update fokus ──
        # Engulfing valid hanya jika candle yang sama TIDAK sweep sisi berlawanan.
        # Kalau sweep sekaligus close (misal Long: lo<=focus_lo DAN cl>focus_hi) → bukan engulfing,
        # tapi pindah fokus ke candle itu (candle terlalu volatile, tidak bisa dipercaya arahnya).
        long_sweep_opp = (lo <= focus_lo)   # Long: sweep ke bawah (sisi berlawanan)
        short_sweep_opp = (hi >= focus_hi)  # Short: sweep ke atas (sisi berlawanan)

        if stype == 'Long' and cl > focus_hi and not long_sweep_opp:
            entry_p  = focus_hi
            sl_price = focus_lo - SL_ENGULF_PCT * bos_rng
            print(f"   {coin} {stype}: ENGULFING M5 idx={i} close={cl:.6g} > focus_hi={focus_hi:.6g} "
                  f"→ LIMIT entry={entry_p:.6g} SL={sl_price:.6g}")
            setup['m5_focus_idx'] = i
            return {'entry': entry_p, 'sl': sl_price, 'side': 'Buy',
                    'engulf_idx': i, 'focus_hi': focus_hi, 'focus_lo': focus_lo}
        if stype == 'Short' and cl < focus_lo and not short_sweep_opp:
            entry_p  = focus_lo
            sl_price = focus_hi + SL_ENGULF_PCT * bos_rng
            print(f"   {coin} {stype}: ENGULFING M5 idx={i} close={cl:.6g} < focus_lo={focus_lo:.6g} "
                  f"→ LIMIT entry={entry_p:.6g} SL={sl_price:.6g}")
            setup['m5_focus_idx'] = i
            return {'entry': entry_p, 'sl': sl_price, 'side': 'Sell',
                    'engulf_idx': i, 'focus_hi': focus_hi, 'focus_lo': focus_lo}

        # ── Update fokus jika wick MENYENTUH (>=/<= bukan hanya >) atau close break ──
        wick_out       = (hi >= focus_hi) or (lo <= focus_lo)
        close_break_dn = (stype == 'Long'  and cl < focus_lo)
        close_break_up = (stype == 'Short' and cl > focus_hi)
        if wick_out or close_break_dn or close_break_up:
            focus_hi = hi; focus_lo = lo
            setup['m5_focus_hi']  = hi; setup['m5_focus_lo']  = lo
            setup['m5_focus_idx'] = i
            print(f"   {coin} {stype}: fokus pindah ke M5 idx={i} hi={hi:.6g} lo={lo:.6g}")

    return None   # belum ada engulfing


def process_setup(coin, setup, df_h1_live, curr_h1, df_m5=None):
    """Proses 1 setup (1 arah). Mutasi setup in-place.
    Return: 'remove' | 'keep' (WAIT_APPROACH) | 'lock' (WAIT_FILL) | 'fill' (posisi sudah dibuka)."""
    stype = setup['type']; choch_level = setup.get('choch_level'); bos_idx = setup.get('bos_idx', 0)
    # CHOCH invalidation (HISTORIS): kalau harga pernah close menembus choch setelah puncak -> mati
    if choch_level:
        bi0 = setup.get('bos_idx', 0)
        bts0 = setup.get('bos_ts', 0)
        rows0 = df_h1_live.index[df_h1_live['ts'] == bts0]
        if len(rows0) > 0:
            bi0 = int(rows0[0])
        if choch_is_broken(df_h1_live, bi0, choch_level, stype):
            if setup.get('order_id'): cancel_order(coin, setup['order_id'])
            print(f"🔄 {coin} {stype}: CHOCH {choch_level:.6f} sudah ditembus (historis). Setup batal.")
            return 'remove'
    # Invalidasi struktur (historis): retrace >= RETRACE_LOCK lalu close lewati swing-2 (puncak 5-5)
    if REBREAK_INVALID:
        sw2 = setup.get('swing2')
        bi  = setup.get('bos_idx', 0)
        bts = setup.get('bos_ts', 0)
        rows = df_h1_live.index[df_h1_live['ts'] == bts]
        if len(rows) > 0:
            bi = int(rows[0])
        if sw2 is not None and rebreak_invalid(df_h1_live, bi, sw2, choch_level, stype, RETRACE_LOCK):
            if setup.get('order_id'): cancel_order(coin, setup['order_id'])
            print(f"🧱 {coin} {stype}: retrace>={RETRACE_LOCK*100:.0f}% lalu lewati swing-2 {sw2:.6f} — struktur baru, setup batal.")
            return 'remove'
    bos_ts_val = setup.get('bos_ts', 0)
    bos_rows   = df_h1_live.index[df_h1_live['ts'] == bos_ts_val]
    if len(bos_rows) > 0:
        bos_idx = int(bos_rows[0]); setup['bos_idx'] = bos_idx
    if bos_idx < len(df_h1_live):
        fresh = _get_fvgs(df_h1_live, stype, bos_idx, choch_level,
                          zone_lo=deepest_retrace_lo(df_h1_live, bos_idx, choch_level, stype))
        if fresh: setup['fvg_list'] = fresh
    if not setup.get('fvg_list'):
        if setup.get('order_id'): cancel_order(coin, setup['order_id'])
        print(f"🗑️ {coin} {stype}: Tidak ada FVG tersisa / tidak fresh.")
        return 'remove'
    curr_price = float(curr_h1['close'])

    # ── Cek FVG setup hangus: harga lari >= 20% BOS range dari C3 ujung ke arah BOS ──
    if setup.get('phase') == 'WAIT_APPROACH' and setup.get('bos_rng', 0) > 0:
        c3_trig      = float(setup.get('orig_ocl', 0))
        cancel_dist  = FVG_CANCEL_RANGE_PCT * float(setup['bos_rng'])
        if c3_trig > 0 and cancel_dist > 0:
            hi_now = float(curr_h1.get('high', curr_price))
            lo_now = float(curr_h1.get('low',  curr_price))
            hangus = (stype == 'Long'  and lo_now <= c3_trig - cancel_dist) or                      (stype == 'Short' and hi_now >= c3_trig + cancel_dist)
            if hangus:
                print(f"🚫 {coin} {stype}: FVG hangus — harga lari "
                      f">={FVG_CANCEL_RANGE_PCT*100:.0f}% range BOS dari C1 close "
                      f"({c3_trig:.6g}) ke arah BOS tanpa engulfing.")
                return 'remove'

    # ── WAIT_APPROACH ──
    if setup['phase'] == 'WAIT_APPROACH':
        entry = setup['entry']; dist = setup['dist']
        side_order = "Buy" if stype == "Long" else "Sell"

        # ── PATH A: FVG monitor M5 setelah C3 ujung tersentuh ──
        # Tidak pakai APPROACH_R. Monitor dimulai begitu C3 ujung tersentuh di M5.
        if M5_ENGULF_FILTER and df_m5 is not None:
            c3_trig = float(setup.get('orig_ocl', entry))
            bos_rng = float(setup.get('bos_rng') or abs(float(setup.get('peak_val') or 0) - float(setup.get('choch_level') or 0)))
            if setup.get('m5_c1c_touched'):
                print(f"👁️  {coin} {stype} | now:{curr_price:.6f} C1.close:{c3_trig:.6f} | monitor engulfing M5...")
            else:
                _pct_fvg = abs(curr_price - c3_trig) / c3_trig * 100 if c3_trig else 0
                print(f"👁️  {coin} {stype} | now:{curr_price:.6f} C1.close:{c3_trig:.6f} | "
                      f"menunggu sentuhan ({_pct_fvg:.2f}% lagi)")
            active_count = len(active_positions) + _count_slots()
            if active_count >= MAX_CONCURRENT:
                print(f"\u23f8\ufe0f  {coin}: slot penuh ({active_count}/{MAX_CONCURRENT})")
                return 'keep'
            engulf = check_m5_engulfing(coin, setup, df_m5, bos_rng)
            if engulf:
                # Pasang LIMIT ORDER di ujung candle fokus (bukan market order)
                limit_entry = engulf['entry']   # high fokus (Long) / low fokus (Short)
                limit_sl    = engulf['sl']       # low fokus - buffer / high fokus + buffer
                oid = place_limit_order(coin, side_order, limit_entry, limit_sl)
                if oid:
                    setup['phase']    = 'WAIT_FILL'
                    setup['order_id'] = oid
                    setup['entry']    = limit_entry
                    setup['sl']       = limit_sl
                    setup['dist']     = abs(limit_entry - limit_sl)
                    print(f"\U0001f4cd {coin} {stype}: ENGULFING M5 → LIMIT @ {limit_entry:.6f} "
                          f"SL:{limit_sl:.6f} | break:{setup.get('swing_val'):.6g} "
                          f"puncak:{setup.get('peak_val'):.6g}")
                    return 'lock'
            return 'keep'
        # ── PATH B: Fallback limit (M5_ENGULF_FILTER=False) ──
        thr = APPROACH_R * dist
        approaching = (stype == 'Long'  and curr_price <= entry + thr) or                       (stype == 'Short' and curr_price >= entry - thr)
        r_now  = ((curr_price - entry) if stype == 'Long' else (entry - curr_price)) / dist if dist > 0 else 0
        to_arm = r_now - APPROACH_R
        r_info = (f"{r_now:.2f}R dari entry" if approaching else
                  f"{r_now:.2f}R dari entry (pasang di {APPROACH_R:.1f}R, kurang {to_arm:.2f}R lagi)")
        print(f"\U0001f441\ufe0f  {coin} {stype} | now:{curr_price:.6f} entry:{entry:.6f} | {r_info} | "
              f"{'\u2705 DALAM RANGE' if approaching else '\u23f3 menunggu'}")
        if approaching:
            direction_valid = (stype == 'Long' and curr_price > entry) or                               (stype == 'Short' and curr_price < entry)
            if not direction_valid:
                print(f"\u26d4 {coin} {stype}: harga {curr_price:.6f} sudah lewat zona {entry:.6f} \u2014 batal.")
                return 'remove'
            active_count = len(active_positions) + _count_slots()
            if active_count >= MAX_CONCURRENT:
                print(f"\u23f8\ufe0f  {coin}: slot penuh ({active_count}/{MAX_CONCURRENT})")
                return 'keep'
            order_id = place_limit_order(coin, side_order, entry, setup['sl'])
            if order_id:
                setup['phase'] = 'WAIT_FILL'; setup['order_id'] = order_id
                print(f"\U0001f4cd {coin} {stype}: Limit dipasang @ {entry:.6f} (dalam {APPROACH_R}R) | "
                      f"break:{setup.get('swing_val'):.6g} puncak:{setup.get('peak_val'):.6g} CHOCH:{setup.get('choch_level'):.6g}")
                return 'lock'
            return 'remove'
        return 'keep'

    # ── WAIT_FILL ──
    if setup['phase'] == 'WAIT_FILL':
        entry_w = setup['entry']; dist_w = setup['dist']; thr_w = APPROACH_R * dist_w
        price_away = (stype == 'Long'  and curr_price > entry_w + thr_w) or \
                     (stype == 'Short' and curr_price < entry_w - thr_w)
        if price_away:
            if setup.get('order_id'): cancel_order(coin, setup['order_id'])
            setup['phase'] = 'WAIT_APPROACH'; setup.pop('order_id', None)
            print(f"📤 {coin} {stype}: Limit dibatalkan (harga mundur > {APPROACH_R}R). Menunggu lagi.")
            return 'keep'
        pos = get_open_position(coin, 'Buy' if stype == 'Long' else 'Sell')
        if pos:
            entry_p = setup['entry']; sl_p = setup['sl']
            side_order = "Buy" if stype == "Long" else "Sell"
            actual_entry = float(pos.get('avgPrice', entry_p))
            actual_dist  = abs(actual_entry - sl_p)
            min_dist = actual_entry * 0.002
            if actual_dist < min_dist:
                actual_dist = min_dist
                sl_p = actual_entry - actual_dist if side_order == "Buy" else actual_entry + actual_dist
                print(f"⚠️ {coin}: SL diperlebar ke {sl_p:.6f}")
            trail_d = TRAIL_STOP * actual_dist
            info = get_instrument_info(coin); tick = info.get('tick_size', 0.0001)
            sl_r = round_price(sl_p, tick); trail_r = round_price(trail_d, tick)
            active_p = round_price(
                actual_entry + TRAIL_ACT_R * actual_dist if side_order == "Buy"
                else actual_entry - TRAIL_ACT_R * actual_dist, tick)
            trail_set_ok = False
            for _attempt in range(3):
                try:
                    if USE_TP:
                        tp_r = round_price(actual_entry + RR_TP * actual_dist if side_order == "Buy" else actual_entry - RR_TP * actual_dist, tick)
                        res_ts = session.set_trading_stop(category=CATEGORY, symbol=coin, stopLoss=str(sl_r), takeProfit=str(tp_r), positionIdx=_pidx(side_order))
                    else:
                        res_ts = session.set_trading_stop(category=CATEGORY, symbol=coin, stopLoss=str(sl_r), trailingStop=str(trail_r), activePrice=str(active_p), positionIdx=_pidx(side_order))
                    if res_ts.get('retCode', -1) == 0:
                        trail_set_ok = True
                        print(f"🛡️  {coin}: SL={sl_r} " + (f"TP={tp_r} (1:{RR_TP})" if USE_TP else f"Trail={trail_r} act={active_p}"))
                        break
                    else:
                        print(f"⚠️ {coin}: set_trading_stop gagal: {res_ts.get('retMsg','')}"); time.sleep(2)
                except Exception as e:
                    print(f"⚠️ {coin}: set_trading_stop error: {e}"); time.sleep(2)
            if not trail_set_ok:
                print(f"⚠️ {coin}: Trail gagal — retry M5 berikutnya")
            active_positions[_akey(coin, stype)] = {
                'coin': coin,
                'side': side_order, 'entry': actual_entry, 'sl': sl_p, 'dist': actual_dist,
                'trail_dist': trail_d, 'trail_engaged': False, 'trail_set': trail_set_ok,
                'last_price': actual_entry, 'entry_time': time.time(),
                'peak': actual_entry, 'peak_time': time.time(),
                'swing_val': setup.get('swing_val'), 'bos_type': stype, 'rev_count': 0,
                'orig_ocl': setup.get('orig_ocl', setup.get('entry')),
                'choch_level': setup.get('choch_level'), 'peak_val': setup.get('peak_val'),
                'swing2': setup.get('swing2'),
            }
            done_setups[coin] = {'swing_val': setup.get('swing_val'), 'stype': stype, 'used_ocl': setup.get('entry')}
            print(f"✅ {coin} {stype}: Limit filled! Entry:{actual_entry:.6f} SL:{sl_p:.6f} | "
                  f"break:{setup.get('swing_val'):.6g} puncak:{setup.get('peak_val'):.6g} CHOCH:{setup.get('choch_level'):.6g}")
            return 'fill'
        else:
            oid = setup.get('order_id')
            if oid and not _order_exists(coin, oid):
                if _order_was_filled(coin, oid):
                    print(f"📭 {coin} {stype}: Limit filled lalu tutup (SL) — selesai.")
                    return 'remove'
                else:
                    print(f"📤 {coin} {stype}: Limit hilang (cancel) — kembali menunggu.")
                    setup['phase'] = 'WAIT_APPROACH'; setup.pop('order_id', None)
                    return 'keep'
        return 'lock'
    return 'keep'


def check_idm_pending():
    """Cek limit IDM (Fib) yg menunggu: terisi -> active_positions; kadaluarsa -> batalkan."""
    for key in list(idm_pending.keys()):
        p = idm_pending[key]
        coin, side = p['coin'], p['side']
        pos = get_open_position(coin, side)
        if pos is not None and float(pos.get('size', 0) or 0) > 0:
            entry = float(pos.get('avgPrice') or p['entry'])
            dist = abs(entry - p['sl'])
            # pasang trailing/TP LANGSUNG saat fill (sama seperti jalur FVG, andal)
            info = get_instrument_info(coin); tick = info.get('tick_size', 0.0001)
            trail_d = TRAIL_STOP * dist
            sl_r = round_price(p['sl'], tick); trail_r = round_price(trail_d, tick)
            active_p = round_price(entry + TRAIL_ACT_R * dist if side == "Buy"
                                   else entry - TRAIL_ACT_R * dist, tick)
            trail_set_ok = False
            for _attempt in range(3):
                try:
                    if USE_TP:
                        tp_r = round_price(entry + RR_TP * dist if side == "Buy" else entry - RR_TP * dist, tick)
                        res_ts = session.set_trading_stop(category=CATEGORY, symbol=coin, stopLoss=str(sl_r),
                                                          takeProfit=str(tp_r), positionIdx=_pidx(side))
                    else:
                        res_ts = session.set_trading_stop(category=CATEGORY, symbol=coin, stopLoss=str(sl_r),
                                                          trailingStop=str(trail_r), activePrice=str(active_p),
                                                          positionIdx=_pidx(side))
                    if res_ts.get('retCode', -1) == 0:
                        trail_set_ok = True
                        print(f"🛡️  {coin} IDM: SL={sl_r} " + (f"TP={tp_r}" if USE_TP else f"Trail={trail_r} act={active_p}"))
                        break
                    else:
                        print(f"⚠️ {coin} IDM: set_trading_stop gagal: {res_ts.get('retMsg','')}"); time.sleep(2)
                except Exception as e:
                    print(f"⚠️ {coin} IDM: set_trading_stop error: {e}"); time.sleep(2)
            active_positions[key] = {
                'coin': coin, 'side': side, 'entry': entry, 'sl': p['sl'],
                'dist': dist,
                'trail_dist': trail_d, 'trail_engaged': False, 'trail_set': trail_set_ok,
                'last_price': entry, 'entry_time': time.time(),
                'peak': entry, 'peak_time': time.time(),
                'swing_val': p['swing_val'], 'bos_type': p['e_stype'], 'rev_count': 0,
                'orig_ocl': entry, 'choch_level': p['choch_level'], 'peak_val': p['peak_val'],
                'swing2': p['peak_val'], 'kind': 'inducement',
            }
            print(f"✅ {coin}: LIMIT IDM {p['e_stype']} TERISI @ {entry:.6g}")
            log_entry(f"════ FILL INDUCEMENT {p['e_stype']} {coin} @ {entry:.6g} (limit {IDM_LIMIT_FIB*100:.0f}% candle H1) ════")
            del idm_pending[key]
            continue
        trig = p.get('trigger'); rng = p.get('rng')

        # ── IDM WAIT_FILL: limit sudah terpasang — cek cancel conditions ──
        if IDM_M5_ENGULF and p.get('phase') == 'WAIT_FILL' and p.get('order_id'):
            df_m5_c = get_data(coin, "5", limit=10)
            cancel_reason = None
            # 1. CHOCH ditembus
            if p.get('choch_level'):
                df_h1_c = get_data(coin, "60", limit=20)
                if df_h1_c is not None:
                    bos_idx_c = max(0, len(df_h1_c) - 10)
                    if choch_is_broken(df_h1_c, bos_idx_c, p['choch_level'], p['e_stype']):
                        cancel_reason = f"CHOCH {p['choch_level']:.6g} ditembus"
            # 2. Harga lari >20% range BOS ke arah BOS dari limit entry
            if not cancel_reason and df_m5_c is not None and rng:
                cancel_dist = IDM_CANCEL_RANGE_PCT * rng
                limit_e = float(p.get('entry', 0))
                if limit_e > 0:
                    if p['e_stype'] == 'Short' and float(df_m5_c['low'].min()) <= limit_e - cancel_dist:
                        cancel_reason = f"harga lari >{IDM_CANCEL_RANGE_PCT*100:.0f}%rng bawah limit"
                    elif p['e_stype'] == 'Long' and float(df_m5_c['high'].max()) >= limit_e + cancel_dist:
                        cancel_reason = f"harga lari >{IDM_CANCEL_RANGE_PCT*100:.0f}%rng atas limit"
            if cancel_reason:
                cancel_order(coin, p['order_id'])
                print(f"🚫 {coin}: IDM {p['e_stype']} limit dibatalkan — {cancel_reason}")
                del idm_pending[key]
            continue

        # ── IDM M5 ENGULF MODE ──
        if IDM_M5_ENGULF and p.get('order_id') is None and not p.get('m5_hangus'):
            df_m5_idm = get_data(coin, "5", limit=100)
            if df_m5_idm is None:
                continue
            e_stype_idm = p['e_stype']
            bos_rng_idm = rng or 1.0
            # Log status IDM trigger
            if trig:
                _curr_idm = float(df_m5_idm.iloc[-1]['close']) if len(df_m5_idm) > 0 else 0
                _pct_idm  = abs(_curr_idm - trig) / trig * 100 if trig and _curr_idm else 0
                if p.get('m5_triggered'):
                    print(f"👁️  IDM {coin} [{e_stype_idm}] | now:{_curr_idm:.6g} trigger:{trig:.6g} | monitor engulfing M5...")
                else:
                    print(f"👁️  IDM {coin} [{e_stype_idm}] | now:{_curr_idm:.6g} trigger:{trig:.6g} | "
                          f"menunggu sweep ({_pct_idm:.2f}% lagi)")

            # Cek hangus permanen: harga keluar ±20% range BOS dari trigger
            if trig and rng:
                cancel_thr_up   = trig + IDM_CANCEL_RANGE_PCT * rng
                cancel_thr_down = trig - IDM_CANCEL_RANGE_PCT * rng
                hi_max = float(df_m5_idm['high'].max())
                lo_min = float(df_m5_idm['low'].min())
                if hi_max > cancel_thr_up or lo_min < cancel_thr_down:
                    print(f"🚫 {coin}: IDM {e_stype_idm} HANGUS PERMANEN — harga keluar "
                          f"±{IDM_CANCEL_RANGE_PCT*100:.0f}% dari trigger {trig:.6g} "
                          f"(hi={hi_max:.6g} lo={lo_min:.6g} batas={cancel_thr_down:.6g}-{cancel_thr_up:.6g})")
                    # Tandai inducement_done supaya IDM ini tidak masuk lagi di loop berikutnya
                    # key pakai BOS besar (stype = kebalikan e_stype_idm)
                    _bos_stype_h = "Short" if e_stype_idm == "Long" else "Long"
                    _sig_hangus = (p.get('swing_val'), p.get('choch_level'), e_stype_idm)
                    inducement_done[(coin, _bos_stype_h)] = _sig_hangus
                    del idm_pending[key]
                    continue

            # Setup sementara untuk check_m5_engulfing
            # IDM Short (BOS Long): fokus candle yang menyentuh trigger (low candle = trigger level)
            # Engulfing = close < low candle fokus → konfirmasi tekanan jual
            m5_setup = {
                'type': e_stype_idm,
                'orig_ocl': trig,   # gunakan trigger sebagai "C1 close" untuk deteksi sentuhan
                'm5_c1c_touched': p.get('m5_triggered', False),
                'm5_focus_hi': p.get('m5_focus_hi', 0.0),
                'm5_focus_lo': p.get('m5_focus_lo', 0.0),
                'm5_focus_idx': p.get('m5_focus_idx', 0),
                'peak_val': trig + bos_rng_idm,
                'choch_level': trig - bos_rng_idm,
                'created_ts': p.get('placed_ts', 0),   # filter candle historis
            }
            engulf = check_m5_engulfing(coin, m5_setup, df_m5_idm, bos_rng_idm)
            # Simpan state kembali ke idm_pending
            p['m5_triggered']  = m5_setup['m5_c1c_touched']
            p['m5_focus_hi']   = m5_setup['m5_focus_hi']
            p['m5_focus_lo']   = m5_setup['m5_focus_lo']
            p['m5_focus_idx']  = m5_setup['m5_focus_idx']
            if engulf:
                # Pasang LIMIT ORDER di ujung candle fokus M5 (sama seperti FVG)
                limit_entry = engulf['entry']
                limit_sl    = engulf['sl']
                oid = place_limit_order(coin, p['side'], limit_entry, limit_sl)
                if oid:
                    p['order_id'] = oid
                    p['entry']    = limit_entry
                    p['sl']       = limit_sl
                    p['phase']    = 'WAIT_FILL'   # tandai sudah punya limit
                    print(f"\U0001f4cd {coin}: IDM M5 ENGULF {e_stype_idm} → LIMIT @ {limit_entry:.6g} "
                          f"SL:{limit_sl:.6g}")
                    log_entry(f"════ IDM M5 ENGULF LIMIT {e_stype_idm} {coin} @ {limit_entry:.6g} ════")
            continue   # M5 engulf path selesai, lanjut ke key berikutnya

        # ── INVALIDASI PERGERAKAN (mode limit lama) ──
        if trig is not None and rng and not IDM_M5_ENGULF:
            thr = trig - IDM_CANCEL_MOVE_PCT * rng if p['e_stype'] == "Short" else trig + IDM_CANCEL_MOVE_PCT * rng
            df_m5 = get_data(coin, "5", limit=30)
            if df_m5 is not None and len(df_m5) > 0:
                seg = df_m5[df_m5['ts'] >= p['placed_ts'] * 1000]
                if len(seg) > 0:
                    moved = (float(seg['low'].min()) <= thr) if p['e_stype'] == "Short" \
                            else (float(seg['high'].max()) >= thr)
                    if moved:
                        if p.get('order_id'):
                            cancel_order(coin, p['order_id'])
                        print(f"🚫 {coin}: LIMIT IDM {p['e_stype']} batal — harga bergerak "
                              f">{IDM_CANCEL_MOVE_PCT*100:.0f}% range dari trigger {trig:.6g} (lewat {thr:.6g}).")
                        del idm_pending[key]


def run_bot():
    global bot_start_ts
    bot_start_ts = time.time()   # timestamp saat bot mulai jalan
    print("SMC INTI BOT — BOS H1 -> FVG -> Limit @ C1.close -> TP 1:2")
    print(f"CONFIG v9.22 | swing {SWING_BARS}-{SWING_BARS}/sub {SUBLEG_BARS}-{SUBLEG_BARS} | FVG biasa (warna bebas) | "
          f"zona C1 {ENTRY_ZONE_LO*100:.1f}%-{ENTRY_ZONE_HI*100:.0f}%{'(dinamis)' if ZONE_FROM_RETRACE else ''} | "
          f"gap {('<=%.2f%%' % (MAX_GAP_PCT*100)) if MAX_GAP_PCT > 0 else 'bebas'} | "
          f"SL {('FIXED %.0f%% range' % (SL_CAP_RANGE*100)) if SL_FIXED_RANGE else (('C1, cap %.0f%% range' % (SL_CAP_RANGE*100)) if SL_CAP_RANGE > 0 else 'C1')} | "
          f"monitor 2-arah | fresh-C1 {'ON' if REQUIRE_FRESH_C1 else 'off'} | "
          f"FVG butuh IDM {'ON' if REQUIRE_IDM_FOR_FVG else 'off'} | "
          f"risk {RISK_PCT*100:.0f}%/trade | lev {LEVERAGE}x | "
          f"TP {'1:'+str(RR_TP) if USE_TP else 'trailing'} | "
          f"HEDGE {'ON (IDM+FVG barengan)' if ALLOW_HEDGE else 'off (one-way)'} | "
          f"induce {('ON %s rantai-mini-BOS %.0f-%.0f%% [%s]' % (INDUCEMENT_TF, INDUCEMENT_ZONE_LO*100, INDUCEMENT_ZONE_HI*100, ('LIMIT Fib%.1f%%' % (IDM_LIMIT_FIB*100)) if IDM_LIMIT_ENTRY else 'MARKET')) if INDUCEMENT_ENTRY else 'off'} | bump order >=${ORDER_BUMP_FLOOR:.0f}")
    if not test_connection():
        print("⛔ Tidak bisa konek ke Bybit.")
        return
    if ALLOW_HEDGE:
        try:
            r = session.switch_position_mode(category=CATEGORY, coin="USDT", mode=3)
            rc = r.get('retCode', -1)
            if rc == 0:
                print("🔀 Hedge mode AKTIF (switch_position_mode mode=3, semua USDT-perp).")
            elif rc == 110025:
                print("🔀 Hedge mode sudah aktif (tak berubah).")
            else:
                print(f"⚠️ switch_position_mode: {r.get('retMsg','')} (code:{rc}) — "
                      f"set Hedge Mode manual di app & TUTUP semua posisi dulu kalau perlu.")
        except Exception as e:
            print(f"⚠️ switch_position_mode error: {e} — set Hedge Mode manual di app dulu.")

    while True:
        now = time.time()
        sec = now % 300
        wait_sec = 300 - sec + 2
        if wait_sec > 300:
            wait_sec = 2
        print(f"⏱️  Tunggu candle M5 close: {wait_sec:.0f} detik...")
        time.sleep(wait_sec)

        for _k in list(active_positions.keys()):
            try:
                check_trailing_sl(_k)
            except Exception as e:
                print(f"⚠️ Trailing SL {coin}: {e}")

        try:
            check_idm_pending()
        except Exception as e:
            print(f"⚠️ IDM pending: {e}")

        n_active   = len(active_positions)
        n_waitfill = _count_slots()
        n_approach = sum(1 for d in pending.values() for s in d.values() if s.get('phase') == 'WAIT_APPROACH')
        slots_used = n_active + n_waitfill
        print(f"\n{'='*55}")
        print(f"📊 SLOT: {slots_used}/{MAX_CONCURRENT} terpakai (posisi:{n_active} | limit:{n_waitfill} | watch:{n_approach})")
        if active_positions:
            for c, p in active_positions.items():
                c = p.get('coin', c)
                bk = p.get('swing_val'); pk = p.get('peak_val'); ch = p.get('choch_level')
                bk = f"{bk:.6g}" if bk else "—"; pk = f"{pk:.6g}" if pk else "—"; ch = f"{ch:.6g}" if ch else "—"
                print(f"   POSISI {c} {p.get('bos_type','?')} @ {p.get('entry',0):.6g} SL:{p.get('sl',0):.6g} | "
                      f"break:{bk} puncak:{pk} CHOCH:{ch}")
        if pending:
            for c, dirs in pending.items():
                for d, st in dirs.items():
                    bk = st.get('swing_val'); pk = st.get('peak_val'); ch = st.get('choch_level')
                    bk = f"{bk:.6g}" if bk else "—"; pk = f"{pk:.6g}" if pk else "—"; ch = f"{ch:.6g}" if ch else "—"
                    print(f"   {c} [{d}]: {st.get('phase','?')} @ {st.get('entry',0):.6g} | "
                          f"break:{bk} puncak:{pk} CHOCH:{ch}")
        if idm_pending:
            for k, p in idm_pending.items():
                trig_p = p.get('trigger', 0); e_st = p.get('e_stype','?')
                bk = p.get('swing_val'); pk = p.get('peak_val'); ch = p.get('choch_level')
                bk = f"{bk:.6g}" if bk else "—"; pk = f"{pk:.6g}" if pk else "—"; ch = f"{ch:.6g}" if ch else "—"
                phase_lbl = "WAIT_FILL" if p.get('order_id') else "WAIT_TRIGGER"
                print(f"   IDM {p.get('coin','?')} [{e_st}]: {phase_lbl} trigger={trig_p:.6g} | "
                      f"break:{bk} puncak:{pk} CHOCH:{ch}")
        print(f"{'='*55}")

        for coin in SYMBOLS:
            try:
                time.sleep(3)
                # Funding window: batalkan limit yg gak searah sebelum settlement
                if FUNDING_FILTER and in_funding_window():
                    cancel_unfavorable_limits(coin)
                if (not ALLOW_HEDGE) and coin in active_positions:
                    continue
                df_h1_live = get_data(coin, "60", limit=100)
                if df_h1_live is None:
                    continue
                sh_h1, sl_h1 = find_last_swing_bos(df_h1_live)
                closed_h1 = df_h1_live.iloc[-2]
                curr_h1   = df_h1_live.iloc[-1]
                # Fetch M5 hanya jika ada setup C1 close yang sedang monitor engulfing
                _need_m5 = M5_ENGULF_FILTER and coin in pending
                df_m5_live = get_data(coin, "5", limit=300) if _need_m5 else None

                # ── INDUCEMENT ENTRY: selalu dicek, terlepas dari pending FVG ──
                if INDUCEMENT_ENTRY:
                    check_inducement_entry(coin, df_h1_live, sh_h1, sl_h1)

                # ── PROSES SETUP PENDING (per arah) ──────────────────
                if coin in pending:
                    dirs = pending[coin]
                    filled = False
                    for d in list(dirs.keys()):
                        action = process_setup(coin, dirs[d], df_h1_live, curr_h1, df_m5=df_m5_live)
                        if action == 'remove':
                            if dirs[d].get('order_id'):
                                cancel_order(coin, dirs[d]['order_id'])
                            del dirs[d]
                        elif action == 'fill':
                            filled = True
                            for d2 in list(dirs.keys()):
                                if d2 != d and dirs[d2].get('order_id'):
                                    cancel_order(coin, dirs[d2]['order_id'])
                                    print(f"🚫 {coin} {d2}: order lawan dibatalkan (arah {d} terisi).")
                            break
                    if filled:
                        pending.pop(coin, None)
                        continue
                    # Re-deteksi DUA ARAH: tambah/ganti di arah yg masih WAIT_APPROACH atau belum ada
                    for d in ('Long', 'Short'):
                        if ALLOW_HEDGE and _akey(coin, d) in active_positions:
                            continue   # hedge: arah ini posisinya sudah terbuka -> jangan pasang limit lagi
                        cur = dirs.get(d)
                        if cur is not None and cur.get('phase') == 'WAIT_FILL':
                            continue   # arah terkunci (limit terpasang)
                        cand, cand_log = build_setup_from_bos(coin, df_h1_live, sh_h1, sl_h1, closed_h1, verbose=False, force_dir=d)
                        if not cand:
                            continue
                        if cur is None:
                            print(f"➕ {coin} {d}: BOS {d} terdeteksi — tambah pantauan")
                            print(cand_log); dirs[d] = cand
                        elif cand['swing_val'] != cur.get('swing_val'):
                            print(f"🔁 {coin} {d}: BOS lebih baru — ganti (break {cur.get('swing_val')} → {cand['swing_val']:.6g})")
                            print(cand_log); dirs[d] = cand
                        else:
                            # arah & swing sama -> segarkan swing2/FVG (tanpa log) agar invalidasi akurat
                            cur['swing2'] = cand.get('swing2'); cur['peak_val'] = cand.get('peak_val')
                            cur['choch_level'] = cand.get('choch_level'); cur['fvg_list'] = cand.get('fvg_list', cur.get('fvg_list'))
                    if not dirs:
                        pending.pop(coin, None)
                    continue

                # ── SCAN SETUP BARU: deteksi DUA ARAH sekaligus ──
                dirs_new = {}
                for d in ('Long', 'Short'):
                    if ALLOW_HEDGE and _akey(coin, d) in active_positions:
                        continue   # hedge: arah ini posisinya sudah terbuka
                    cand, cand_log = build_setup_from_bos(coin, df_h1_live, sh_h1, sl_h1, closed_h1, verbose=False, force_dir=d)
                    if cand:
                        print(cand_log); dirs_new[d] = cand
                if dirs_new:
                    pending[coin] = dirs_new
                else:
                    # diagnostik DUA ARAH: kenapa tak ada setup (BOS? FVG? stale? invalid?)
                    build_setup_from_bos(coin, df_h1_live, sh_h1, sl_h1, closed_h1, verbose=True, force_dir='Long')
                    build_setup_from_bos(coin, df_h1_live, sh_h1, sl_h1, closed_h1, verbose=True, force_dir='Short')

            except Exception as e:
                print(f"⚠️ Error {coin}: {e}"); continue


if __name__ == "__main__":
    run_bot()
