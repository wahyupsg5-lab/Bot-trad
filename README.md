# Bot Live SMC v5 (swing 5-5)

Bot SMC: BOS H1 -> FVG kuat -> limit entry -> SL invalidasi -> trailing.
Perubahan dari versi sebelumnya: **deteksi swing 5-kiri & 5-kanan** (fractal 5-5),
dengan candle penembus (breaker) = candle terakhir yang CLOSE (`df.iloc[-2]`),
TIDAK perlu konfirmasi kanan-5 — cukup close menembus swing yang sudah valid.

## ⚠️ Peringatan jujur
Semua backtest realistis (M5 + biaya) menunjukkan strategi ini **rugi tipis** (PF ~0.85-1.0)
setelah biaya, lintas-coin dan lintas-kuartal. Jalankan ini sebagai **forward-test**:
- set `TESTNET=true`, ATAU pakai ukuran/risiko sangat kecil di real,
- tujuannya membandingkan eksekusi nyata vs backtest, bukan mencari profit.

## Deploy (Railway)
1. Push file ini ke GitHub -> Railway Deploy from GitHub.
2. Set Variables:
   - `API_KEY`, `API_SECRET`  (kunci Bybit; pakai kunci TESTNET bila TESTNET=true)
   - `TESTNET` = `true` (disarankan) atau `false`
3. Log live: buka `https://<service>.up.railway.app/logs`

## Catatan
- Daftar koin ada di `SYMBOLS` (sekitar baris 96) — sunting sesuai keinginan.
- Ubah `SWING_BARS` (default 5) bila ingin uji sensitivitas swing.
