#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OMNI-BOT v26.0 - GOLD (XAUUSD) QUANT ENGINE
--------------------------------------------
Perbaikan besar dari v25:
  1. DATA ASLI via yfinance (Spot + DXY) - TIDAK ada simulasi/hardcode.
  2. FAIL-CLOSE: kalau data gagal, bot STOP & alert, bukan trading pakai data palsu.
  3. Intermarket DXY + SMC (sweep & FVG) filter.
  4. Wilder ATR (True Range) untuk SL.
  5. % Risk position sizing (bukan lot mati).
  6. Journal loop PnL: posisi lama di-recap otomatis (TP/SL).
  7. Semua HTML-entity & threshold liar dibersihkan.

Cara pakai:
  pip install yfinance pandas numpy matplotlib requests pytz
  export TELEGRAM_TOKEN="..."
  export TELEGRAM_CHAT_ID="..."
  python omni_bot.py --once            # jalan 1 siklus
"""

import os
import sys
import json
import time
import argparse
from datetime import datetime
import pytz
import requests
import pandas as pd
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import yfinance as yf

plt.rcParams["axes.unicode_minus"] = False

# ===================== CONFIG =====================
WIB = pytz.timezone("Asia/Jakarta")
TELE_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
TELE_CHAT    = os.getenv("TELEGRAM_CHAT_ID", "")

STATE_FILE   = "bot_state.json"
JOURNAL_FILE = "trade_journal.json"

# --- Data source ---
GOLD_SYMBOL = "GC=F"        # COMEX gold futures (via Yahoo)
DXY_SYMBOL  = "DX-Y.NYB"    # US Dollar Index (via Yahoo)
TIMEFRAME   = "15m"
BARS        = 500

# --- Signal weights (total = 1.0) ---
W_DXY       = 0.40          # intermarket
W_SMC       = 0.35          # SMC sweep/FVG
W_MOM       = 0.25          # momentum (EMA + Kalman)

SIGNAL_THRESHOLD = 0.55     # |score| di atas ini => BUY/SELL, di bawah => NEUTRAL

# --- Risk ---
RISK_PCT       = 0.01       # 1% equity per trade
ATR_MULT       = 1.5        # jarak SL = ATR * multiplier
TP_RATIOS      = [1.5, 2.5, 4.0, 6.0]   # multiple dari jarak SL

# ==================================================

def log(m):
    print(f"[{datetime.now(WIB).strftime('%H:%M:%S %d-%m-%Y')} WIB] {m}", flush=True)


# ---------------- Telegram ----------------
def send_text(m):
    if not TELE_TOKEN or not TELE_CHAT:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELE_TOKEN}/sendMessage",
            json={"chat_id": TELE_CHAT, "text": m, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as e:
        log(f"telegram err: {e}")


def send_photo(cap, p):
    if not TELE_TOKEN or not TELE_CHAT:
        return
    try:
        with open(p, "rb") as f:
            requests.post(
                f"https://api.telegram.org/bot{TELE_TOKEN}/sendPhoto",
                data={"chat_id": TELE_CHAT, "caption": cap, "parse_mode": "HTML"},
                files={"photo": f},
                timeout=20,
            )
    except Exception as e:
        log(f"photo err: {e}")
        send_text(cap)


# ---------------- File helpers ----------------
def load(p, default):
    try:
        if os.path.exists(p):
            with open(p) as f:
                return json.load(f)
    except Exception as e:
        log(f"load {p} err: {e}")
    return default


def save(p, d):
    try:
        tmp = p + ".tmp"
        with open(tmp, "w") as f:
            json.dump(d, f, indent=2)
        os.replace(tmp, p)
    except Exception as e:
        log(f"save {p} err: {e}")


# ---------------- DATA LAYER (REAL) ----------------
def fetch_ohlcv(symbol):
    """Ambil OHLCV real. Kalau kosong/gagal -> return None (FAIL-CLOSE)."""
    try:
        df = yf.download(symbol, period="5d", interval=TIMEFRAME,
                         progress=False, auto_adjust=True)
        if df is None or df.empty or len(df) < 100:
            log(f"data kosong untuk {symbol}")
            return None
        df = df.rename(columns={
            "Open": "open", "High": "high",
            "Low": "low", "Close": "close", "Volume": "volume"
        })
        df = df[["open", "high", "low", "close", "volume"]].dropna()
        # pastikan pakai bar terakhir (tail) -> waktu utk timezone lokal
        df.index = pd.to_datetime(df.index).tz_localize(None)
        return df.tail(BARS)
    except Exception as e:
        log(f"fetch {symbol} err: {e}")
        return None


def get_market_data():
    """Ambil gold + DXY. Salah satu gagal => abort (bukan simulasi)."""
    gold = None
    dxy = None
    for attempt in range(3):          # retry 3x
        gold = fetch_ohlcv(GOLD_SYMBOL)
        if gold is not None:
            break
        log(f"retry gold ({attempt+1}/3)")
        time.sleep(5)
    if gold is None:
        return None, None             # FAIL-CLOSE

    dxy = fetch_ohlcv(DXY_SYMBOL)     # DXY boleh gagal -> weight dialihkan
    if dxy is None:
        log("DXY gagal -> intermarket dimatikan, weight dialihkan ke SMC")
    return gold, dxy


# ---------------- Indicators ----------------
def wilder_atr(df, n=14):
    """True Range + Wilder smoothing (ewm alpha=1/n)."""
    tr = np.maximum(
        df["high"] - df["low"],
        np.maximum(
            (df["high"] - df["close"].shift()).abs(),
            (df["low"] - df["close"].shift()).abs(),
        ),
    )
    return float(tr.ewm(alpha=1 / n, adjust=False).mean().iloc[-1])


def kalman_series(prices):
    """Kalman lebih masuk akal: noise skala sesuai harga, bukan konstanta liar."""
    n = len(prices)
    filtered = np.zeros(n)
    # varian diukur dari data -> auto-skala
    Q = float(np.var(np.diff(prices))) * 0.01
    R = float(np.var(prices)) * 0.001
    xhat = float(prices[0])
    P = float(R)
    for i in range(n):
        Pm = P + Q
        K = Pm / (Pm + R)
        xhat += K * (float(prices[i]) - xhat)
        P = (1 - K) * Pm
        filtered[i] = xhat
    return filtered


def detect_sweep(df, lookback=20):
    """Liquidity sweep: wick nembus swing lalu ditolak balik struktur."""
    last = df.iloc[-1]
    prior = df.iloc[-lookback:-1]
    swing_hi = float(prior["high"].max())
    swing_lo = float(prior["low"].min())
    close = float(last["close"])
    if float(last["low"]) < swing_lo and close > swing_lo:
        return "BUY"        # sweep bawah (stop hunt long) -> reversal naik
    if float(last["high"]) > swing_hi and close < swing_hi:
        return "SELL"       # sweep atas (stop hunt short) -> reversal turun
    return None


def detect_fvg(df, lookback=20):
    """Fair Value Gap bullish/bearish pada 3 candle terakhir area revisi."""
    last3 = df.tail(3)
    if len(last3) < 3:
        return None
    c1, c2, c3 = last3.iloc[0], last3.iloc[1], last3.iloc[2]
    if float(c3.low) > float(c1.high) and float(c2.close) > float(c1.close):
        return "BUY"        # bullish FVG (gap naik)
    if float(c3.high) < float(c1.low) and float(c2.close) < float(c1.close):
        return "SELL"       # bearish FVG (gap turun)
    return None


# ---------------- Signal Engine ----------------
def build_scores(gold, dxy):
    close = float(gold["close"].iloc[-1])
    ema20 = float(gold["close"].ewm(span=20).mean().iloc[-1])
    kf = kalman_series(gold["close"].values)
    kf_now = float(kf[-1])

    # 1) Intermarket DXY (bias invers: DXY turun -> gold naik)
    if dxy is not None and len(dxy) > 50:
        dxy_now = float(dxy["close"].iloc[-1])
        dxy_sma = float(dxy["close"].rolling(50).mean().iloc[-1])
        dxy_sig = 1.0 if dxy_now < dxy_sma else -1.0
    else:
        dxy_sig = 0.0

    # 2) SMC: sweep > FVG > momentum structure
    sweep = detect_sweep(gold)
    fvg = detect_fvg(gold)
    smc_sig = 0.0
    if sweep is not None:
        smc_sig = 1.0 if sweep == "BUY" else -1.0
    elif fvg is not None:
        smc_sig = 0.7 if fvg == "BUY" else -0.7
    else:  # fallback: posisi harga vs ema
        smc_sig = 1.0 if close > ema20 else -1.0

    # 3) Momentum: harga vs EMA + Kalman
    mom_sig = (1.0 if close > ema20 else -1.0) * 0.6 + \
              (1.0 if close > kf_now else -1.0) * 0.4

    # ---- reweight kalau DXY mati ----
    if dxy_sig == 0.0:
        w_dxy, w_smc, w_mom = 0.0, W_SMC + W_DXY, W_MOM
    else:
        w_dxy, w_smc, w_mom = W_DXY, W_SMC, W_MOM

    score = w_dxy * dxy_sig + w_smc * smc_sig + w_mom * mom_sig
    return score, close


def analyze():
    gold, dxy = get_market_data()
    if gold is None:
        send_text("🚫 DATA OFF - bot STOP. Data gold gagal didapat, tidak trading dummy.")
        log("FAIL-CLOSE: gold data unavailable")
        return None

    score, close = build_scores(gold, dxy)

    # conv index 0-100 dari skor
    conviction = min(100.0, abs(score) * 100.0)

    if score > SIGNAL_THRESHOLD:
        signal = "BUY"
    elif score < -SIGNAL_THRESHOLD:
        signal = "SELL"
    else:
        signal = "NEUTRAL"

    atr = wilder_atr(gold)
    sl_dist = max(atr * ATR_MULT, atr)      # minimal 1x ATR (jaga absurd kecil)
    tps = [(sl_dist * r) for r in TP_RATIOS]

    return {
        "gold": gold, "score": score, "close": close,
        "conviction": conviction, "signal": signal,
        "sl_dist": sl_dist, "tps": tps,
    }


# ---------------- PnL Loop (recap posisi lama) ----------------
def recap_open_positions():
    journal = load(JOURNAL_FILE, [])
    gold, _ = get_market_data()
    if gold is None:
        return
    last_price = float(gold["close"].iloc[-1])
    now = datetime.now(WIB)

    for t in journal:
        if t.get("status") != "OPEN":
            continue
        entry = float(t["entry"])
        sl = float(t["sl"])
        tp3 = float(t["tp3"])
        direction = t["signal"]

        hit = None
        if direction == "BUY":
            if last_price <= sl:
                hit, pnl_pct = "SL", (sl - entry) / entry
            elif last_price >= tp3:
                hit, pnl_pct = "TP3", (tp3 - entry) / entry
        else:
            if last_price >= sl:
                hit, pnl_pct = "SL", (entry - sl) / entry
            elif last_price <= tp3:
                hit, pnl_pct = "TP3", (entry - tp3) / entry

        if hit:
            t["status"] = "CLOSED"
            t["exit"] = last_price
            t["exit_time"] = now.isoformat()
            t["result"] = hit
            t["pnl_pct"] = round(pnl_pct * 100, 2)
            msg = (f"📌 RECAP {t['symbol']} [{hit}]\n"
                   f"Entry {entry:.2f} -> Exit {last_price:.2f}\n"
                   f"PnL: {t['pnl_pct']:+.2f}%")
            log(msg)
            send_text(msg)

    save(JOURNAL_FILE, journal)


# ---------------- Chart ----------------
def make_chart(res, entry, sl, tp3, signal):
    df = res["gold"].tail(80)
    if df.empty:
        return None
    fig, ax = plt.subplots(figsize=(10, 5))
    fig.patch.set_facecolor("#0e0e0e")
    ax.set_facecolor("#0e0e0e")
    ax.plot(df.index, df["close"], color="#FFD700", linewidth=2,
            label=f"Gold {res['close']:.2f}")
    ax.axhline(entry, color="white", ls="--", lw=1.4, label=f"ENTRY {entry:.2f}")
    ax.axhline(sl, color="#FF3B3B", lw=1.3, label=f"SL {sl:.2f}")
    ax.axhline(tp3, color="yellow", lw=1.5, label=f"TP3 {tp3:.2f}")
    ax.set_title(
        f"OMNI-SIGNAL {signal} | Score {res['score']:+.2f} | "
        f"Conviction {res['conviction']:.0f}%",
        color="white", fontsize=11, fontweight="bold",
    )
    ax.legend(loc="upper left", fontsize=8, facecolor="#222", labelcolor="white")
    ax.grid(alpha=0.15)
    ax.tick_params(colors="white", labelsize=8)
    fig.autofmt_xdate()
    plt.tight_layout()
    path = "/tmp/chart_omni.png"
    plt.savefig(path, dpi=150, facecolor="#0e0e0e")
    plt.close()
    return path


# ---------------- MAIN ----------------
def main():
    now = datetime.now(WIB)
    # Weekend: Jumat 21:00 WIB s/d Senin 06:00 WIB market off
    if now.weekday() == 4 and now.hour >= 21:
        log("Market tutup akhir pekan")
        return 0
    if now.weekday() == 5 or now.weekday() == 6:
        log("Market tutup (weekend)")
        return 0
    if now.weekday() == 0 and now.hour < 6:
        log("Market tutup (early Monday)")
        return 0

    log("INIT OMNI-BOT v26 ...")
    recap_open_positions()          # 1) tutup posisi lama yang kena TP/SL dulu

    res = analyze()                 # 2) sinyal baru
    if res is None:
        return 1

    entry = res["close"]
    signal = res["signal"]

    if signal == "NEUTRAL":
        log(f"NEUTRAL, score={res['score']:+.2f}, conv={res['conviction']:.0f}%")
        return 0

    # ---- setup arah ----
    if signal == "BUY":
        sl = entry - res["sl_dist"]
        targets = [entry + t for t in res["tps"]]
    else:
        sl = entry + res["sl_dist"]
        targets = [entry - t for t in res["tps"]]

    tp3 = targets[2]

    # ---- sizing % risk (equity skala: default 10k, sesuaikan) ----
    equity = float(load(STATE_FILE, {}).get("equity", 10000.0))
    risk_usd = equity * RISK_PCT
    qty = max(risk_usd / res["sl_dist"], 0.0001)

    # ---- simpan posisi open ke journal ----
    journal = load(JOURNAL_FILE, [])
    journal.append({
        "symbol": GOLD_SYMBOL, "time": now.isoformat(),
        "signal": signal, "entry": entry,
        "sl": sl, "tp3": tp3,
        "qty": round(qty, 4), "status": "OPEN",
    })
    save(JOURNAL_FILE, journal)

    caption = (
        f"🚨 OMNI-SIGNAL: GOLD [{signal}] ({res['conviction']:.0f}%)\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"🔹 Entry  : {entry:.2f}\n"
        f"🔹 SL     : {sl:.2f}  (1R)\n"
        f"🔹 TP1    : {targets[0]:.2f} (1.5R)\n"
        f"🔹 TP2    : {targets[1]:.2f} (2.5R)\n"
        f"🔹 TP3    : {tp3:.2f} (4R)  < primary\n"
        f"🔹 TP4    : {targets[3]:.2f} (6R)\n"
        f"🔹 Qty    : {qty:.4f}  (risk {RISK_PCT*100:.0f}%)\n"
        f"🔹 Score  : {res['score']:+.2f}\n"
        f"⏰ {now.strftime('%H:%M WIB')}"
    )
    chart = make_chart(res, entry, sl, tp3, signal)
    if chart:
        send_photo(caption, chart)
    else:
        send_text(caption)

    log(caption.replace("\n", " | "))
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--once", action="store_true", help="single cycle")
    args = p.parse_args()
    if args.once:
        sys.exit(main())
    # loop mode: jalan tiap interval 15m
    while True:
        sys.exit_code = main()
        log(f"done exit={sys.exit_code}, sleep 15m")
        time.sleep(900)
