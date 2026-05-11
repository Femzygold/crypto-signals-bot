import os
import json
import time
import math
import secrets
import requests
import threading
from datetime import datetime, timezone
from flask import Flask, request, jsonify, make_response
from collections import deque

app = Flask(__name__)

# ── CREDENTIALS (hardcoded - no env vars needed) ──────────────────
TELEGRAM_BOT_TOKEN = "8668028976:AAE2u1in1KGr1nRTJbaQXNPeDtMO35unoQ8"
TELEGRAM_CHAT_ID   = "7411219487"
DASHBOARD_PASSWORD = "signal123"
# ─────────────────────────────────────────────────────────────────

MAX_SIGNALS = 500
signals     = deque(maxlen=MAX_SIGNALS)
sessions    = set()

# Scan state (shared across threads)
scan_state = {
    "running":        False,
    "current_pair":   "",
    "current_tf":     "",
    "pairs_done":     0,
    "total_pairs":    0,
    "scan_count":     0,       # how many full scans completed
    "signals_found":  0,
    "last_scan_time": None,
    "log":            deque(maxlen=60),   # last 60 log lines
}
scan_lock = threading.Lock()


# ════════════════════════════════════════════════════════════════════
# MEXC API
# ════════════════════════════════════════════════════════════════════

MEXC_BASE = "https://contract.mexc.com/api/v1/contract"

def get_all_pairs():
    """Fetch every active perpetual futures pair from MEXC."""
    try:
        r = requests.get(f"{MEXC_BASE}/detail", timeout=15)
        r.raise_for_status()
        data = r.json()
        if not data.get("success"):
            return []
        pairs = []
        for item in data.get("data", []):
            if item.get("state") == 0:   # 0 = enabled/listed
                pairs.append(item["symbol"])
        return sorted(pairs)
    except Exception as e:
        log(f"Error fetching pairs: {e}")
        return []


def get_candles(symbol, interval, limit=100):
    """
    Fetch OHLCV candles from MEXC perpetual.
    interval options: Min1 Min5 Min15 Min30 Min60 Hour4 Hour8 Day1 Week1 Month1
    Returns list of dicts sorted oldest→newest.
    """
    try:
        url    = f"{MEXC_BASE}/kline/{symbol}"
        params = {"interval": interval, "limit": limit}
        r      = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        if not data.get("success") or not data.get("data"):
            return []
        raw    = data["data"]
        times  = raw.get("time",  [])
        opens  = raw.get("open",  [])
        highs  = raw.get("high",  [])
        lows   = raw.get("low",   [])
        closes = raw.get("close", [])
        candles = []
        for i in range(len(times)):
            try:
                candles.append({
                    "time":  int(times[i]),
                    "open":  float(opens[i]),
                    "high":  float(highs[i]),
                    "low":   float(lows[i]),
                    "close": float(closes[i]),
                })
            except (ValueError, IndexError):
                continue
        return candles   # already oldest→newest from MEXC
    except Exception as e:
        return []


# ════════════════════════════════════════════════════════════════════
# TREND DETECTION  (HH/HL = Bullish | LH/LL = Bearish)
# ════════════════════════════════════════════════════════════════════

def find_swings(candles, n=3):
    """Return list of (index, price, 'H'/'L') swing points."""
    highs  = [c["high"]  for c in candles]
    lows   = [c["low"]   for c in candles]
    swings = []
    for i in range(n, len(candles) - n):
        if all(highs[i] >= highs[i-j] and highs[i] >= highs[i+j] for j in range(1, n+1)):
            swings.append((i, highs[i], "H"))
        elif all(lows[i] <= lows[i-j] and lows[i] <= lows[i+j] for j in range(1, n+1)):
            swings.append((i, lows[i], "L"))
    return swings


def detect_trend(candles, lookback=60):
    """Determine trend from swing structure on last `lookback` candles."""
    if len(candles) < 20:
        return "NEUTRAL"
    c      = candles[-lookback:] if len(candles) >= lookback else candles
    swings = find_swings(c, n=2)

    sh = [(i, p) for i, p, t in swings if t == "H"]
    sl = [(i, p) for i, p, t in swings if t == "L"]

    if len(sh) >= 2 and len(sl) >= 2:
        hh = sh[-1][1] > sh[-2][1]
        hl = sl[-1][1] > sl[-2][1]
        lh = sh[-1][1] < sh[-2][1]
        ll = sl[-1][1] < sl[-2][1]
        if hh and hl:  return "BULLISH"
        if lh and ll:  return "BEARISH"

    # Fallback: price vs 20-period midpoint
    closes     = [c["close"] for c in candles[-20:]]
    avg_first  = sum(closes[:10]) / 10
    avg_last   = sum(closes[10:]) / 10
    if avg_last > avg_first * 1.003:   return "BULLISH"
    if avg_last < avg_first * 0.997:   return "BEARISH"
    return "NEUTRAL"


# ════════════════════════════════════════════════════════════════════
# CRT DETECTION
# ════════════════════════════════════════════════════════════════════

def detect_crt(candles, trend):
    """
    Scan last 15 completed candle sets for CRT formations.
    Returns list of raw CRT dicts (pre-scoring).
    """
    found = []
    if len(candles) < 4:
        return found

    # We need candle 3 to be complete (not current forming candle)
    # So we look at candles[-4] through candles[-2] as the triplet
    limit = min(15, len(candles) - 2)

    for offset in range(1, limit):
        i3 = len(candles) - 1 - offset    # C3 index (distribution, completed)
        i2 = i3 - 1                        # C2 index (manipulation)
        i1 = i2 - 1                        # C1 index (CRT / accumulation)
        if i1 < 0:
            break

        c1 = candles[i1]
        c2 = candles[i2]
        c3 = candles[i3]

        crh = c1["high"]
        crl = c1["low"]
        cr_range = crh - crl
        if cr_range <= 0:
            continue

        # ── Bullish CRT ───────────────────────────────────────────
        # Trend must be BULLISH (or NEUTRAL for looser scan)
        if trend in ("BULLISH", "NEUTRAL"):
            swept_low      = c2["low"] < crl                          # C2 sweeps below CRL
            close_inside   = crl <= c2["close"] <= crh                # C2 closes back inside
            wick_exists    = (c2["close"] - c2["low"]) > cr_range * 0.05  # Has lower wick
            c3_bullish     = c3["close"] > c3["open"]                  # C3 is bullish candle

            if swept_low and close_inside and wick_exists:
                entry  = c2["close"]
                sl     = c2["low"]
                tp     = crh
                risk   = abs(entry - sl)
                reward = abs(tp - entry)
                rr     = round(reward / risk, 2) if risk > 0 else 0
                if rr >= 2.0:
                    found.append({
                        "direction": "BUY",
                        "c1": c1, "c2": c2, "c3": c3,
                        "crh": crh, "crl": crl,
                        "entry": round(entry, 6),
                        "sl":    round(sl, 6),
                        "tp":    round(tp, 6),
                        "rr":    rr,
                        "sweep": round(crl - c2["low"], 6),
                        "c3_confirms": c3_bullish,
                    })

        # ── Bearish CRT ───────────────────────────────────────────
        if trend in ("BEARISH", "NEUTRAL"):
            swept_high     = c2["high"] > crh                          # C2 sweeps above CRH
            close_inside   = crl <= c2["close"] <= crh                # C2 closes back inside
            wick_exists    = (c2["high"] - c2["close"]) > cr_range * 0.05  # Has upper wick
            c3_bearish     = c3["close"] < c3["open"]                  # C3 is bearish candle

            if swept_high and close_inside and wick_exists:
                entry  = c2["close"]
                sl     = c2["high"]
                tp     = crl
                risk   = abs(sl - entry)
                reward = abs(entry - tp)
                rr     = round(reward / risk, 2) if risk > 0 else 0
                if rr >= 2.0:
                    found.append({
                        "direction": "SELL",
                        "c1": c1, "c2": c2, "c3": c3,
                        "crh": crh, "crl": crl,
                        "entry": round(entry, 6),
                        "sl":    round(sl, 6),
                        "tp":    round(tp, 6),
                        "rr":    rr,
                        "sweep": round(c2["high"] - crh, 6),
                        "c3_confirms": c3_bearish,
                    })

    return found


# ════════════════════════════════════════════════════════════════════
# TBS — TURTLE BODY SOUP CONFIRMATION
# ════════════════════════════════════════════════════════════════════

def check_tbs(symbol, main_tf, direction, crl, crh):
    """
    1D CRT  → check Min60 → Min30 → Min15
    4H CRT  → check Min15 → Min5
    Returns (confirmed: bool, tf_found: str, strength: str)
    """
    tfs = ["Min60", "Min30", "Min15"] if main_tf == "Day1" else ["Min15", "Min5"]

    for tf in tfs:
        candles = get_candles(symbol, tf, limit=60)
        if not candles:
            continue
        recent = candles[-30:]
        for c in reversed(recent):
            if direction == "BUY":
                # Body closed below CRL then recovered
                body_swept  = min(c["open"], c["close"]) < crl and max(c["open"], c["close"]) > crl
                wick_swept  = c["low"] < crl and c["close"] > crl
                if body_swept:
                    return True, tf, "STRONG"
                if wick_swept:
                    return True, tf, "MODERATE"
            else:
                body_swept  = max(c["open"], c["close"]) > crh and min(c["open"], c["close"]) < crh
                wick_swept  = c["high"] > crh and c["close"] < crh
                if body_swept:
                    return True, tf, "STRONG"
                if wick_swept:
                    return True, tf, "MODERATE"

    return False, None, None


# ════════════════════════════════════════════════════════════════════
# FVG DETECTION
# ════════════════════════════════════════════════════════════════════

def find_fvg(symbol, tf, direction):
    """
    Bullish FVG: candle[i+2].low > candle[i].high  (gap between C1 high and C3 low)
    Bearish FVG: candle[i+2].high < candle[i].low
    Returns (found: bool, fvg_level: float)
    """
    candles = get_candles(symbol, tf, limit=30)
    if not candles or len(candles) < 3:
        return False, None

    for i in range(len(candles) - 3, -1, -1):
        c1 = candles[i]
        c3 = candles[i + 2]
        if direction == "BUY":
            if c3["low"] > c1["high"]:
                fvg_mid = (c3["low"] + c1["high"]) / 2
                return True, round(fvg_mid, 6)
        else:
            if c3["high"] < c1["low"]:
                fvg_mid = (c3["high"] + c1["low"]) / 2
                return True, round(fvg_mid, 6)

    return False, None


# ════════════════════════════════════════════════════════════════════
# CISD — CHANGE IN STATE OF DELIVERY
# ════════════════════════════════════════════════════════════════════

def check_cisd(symbol, tf, direction):
    """
    After sweep: look for a candle that closes opposite to the sweep direction
    on the lower timeframe — first sign of reversal.
    """
    candles = get_candles(symbol, tf, limit=20)
    if not candles or len(candles) < 3:
        return False

    recent = candles[-10:]
    for i in range(1, len(recent)):
        c = recent[i]
        prev = recent[i-1]
        if direction == "BUY":
            # Looking for bullish displacement: close above prev high
            if c["close"] > prev["high"] and c["close"] > c["open"]:
                return True
        else:
            # Bearish displacement: close below prev low
            if c["close"] < prev["low"] and c["close"] < c["open"]:
                return True
    return False


# ════════════════════════════════════════════════════════════════════
# SIGNAL SCORING  (0 → 100)
# ════════════════════════════════════════════════════════════════════

def score_signal(crt, trend, tbs_found, tbs_tf, tbs_strength, fvg_found, cisd_found):
    score   = 0
    details = []

    crh      = crt["crh"]
    crl      = crt["crl"]
    cr_range = crh - crl
    c2       = crt["c2"]
    direction= crt["direction"]

    # 1. Trend alignment (20 pts)
    if (direction == "BUY"  and trend == "BULLISH") or \
       (direction == "SELL" and trend == "BEARISH"):
        score += 20; details.append("✅ Trend aligned (+20)")
    elif trend == "NEUTRAL":
        score += 8;  details.append("⚠️ Neutral trend (+8)")
    else:
        details.append("❌ Counter-trend (+0)")

    # 2. RR quality (20 pts)
    rr = crt["rr"]
    if rr >= 5:
        score += 20; details.append(f"✅ Exceptional RR {rr}R (+20)")
    elif rr >= 4:
        score += 16; details.append(f"✅ Strong RR {rr}R (+16)")
    elif rr >= 3:
        score += 12; details.append(f"✅ Good RR {rr}R (+12)")
    else:
        score += 8;  details.append(f"⚠️ Minimum RR {rr}R (+8)")

    # 3. Wick quality on C2 (15 pts)
    if direction == "BUY":
        wick_size = c2["close"] - c2["low"]
    else:
        wick_size = c2["high"] - c2["close"]
    wick_ratio = wick_size / cr_range if cr_range > 0 else 0
    if wick_ratio >= 0.6:
        score += 15; details.append("✅ Strong rejection wick (+15)")
    elif wick_ratio >= 0.35:
        score += 9;  details.append("⚠️ Moderate wick (+9)")
    else:
        score += 4;  details.append("⚠️ Small wick (+4)")

    # 4. Sweep depth (10 pts)
    sweep_ratio = crt["sweep"] / cr_range if cr_range > 0 else 0
    if sweep_ratio >= 0.4:
        score += 10; details.append("✅ Deep liquidity sweep (+10)")
    elif sweep_ratio >= 0.15:
        score += 6;  details.append("⚠️ Moderate sweep (+6)")
    else:
        score += 2;  details.append("⚠️ Shallow sweep (+2)")

    # 5. TBS confirmation (15 pts)
    if tbs_found:
        if tbs_strength == "STRONG":
            score += 15; details.append(f"✅ TBS confirmed {tbs_tf} body (+15)")
        else:
            score += 10; details.append(f"⚠️ TBS confirmed {tbs_tf} wick (+10)")
    else:
        details.append("❌ No TBS found (+0)")

    # 6. FVG entry (10 pts)
    if fvg_found:
        score += 10; details.append("✅ FVG entry zone found (+10)")
    else:
        details.append("⚠️ No FVG (+0)")

    # 7. CISD confirmation (10 pts)
    if cisd_found:
        score += 10; details.append("✅ CISD confirmed (+10)")
    else:
        details.append("⚠️ No CISD yet (+0)")

    # Bonus: C3 confirms direction
    if crt.get("c3_confirms"):
        score = min(score + 5, 100)
        details.append("✅ C3 confirms direction (+5)")

    grade = "A+" if score >= 85 else "A" if score >= 75 else "B" if score >= 60 else "C" if score >= 45 else "D"

    return min(score, 100), grade, details


# ════════════════════════════════════════════════════════════════════
# TELEGRAM
# ════════════════════════════════════════════════════════════════════

def send_telegram(msg):
    if not TELEGRAM_BOT_TOKEN or "PASTE" in TELEGRAM_BOT_TOKEN:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={
            "chat_id":    TELEGRAM_CHAT_ID,
            "text":       msg,
            "parse_mode": "HTML",
        }, timeout=10)
        return r.status_code == 200
    except:
        return False


def format_signal_message(sig):
    emoji  = "🟢" if sig["direction"] == "BUY" else "🔴"
    grade  = sig.get("grade", "–")
    score  = sig.get("score", 0)
    bars   = "█" * (score // 10) + "░" * (10 - score // 10)
    tbs    = f"✅ {sig.get('tbs_tf','')}" if sig.get("tbs_found") else "❌ Not found"
    fvg    = f"✅ {sig.get('fvg_level','')}" if sig.get("fvg_found") else "❌ Not found"
    cisd   = "✅ Confirmed" if sig.get("cisd_found") else "❌ Not confirmed"
    return (
        f"{emoji} <b>CRT SIGNAL — {sig['direction']}</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"<b>Pair:</b>      {sig['symbol']}\n"
        f"<b>Timeframe:</b> {sig['tf']}\n"
        f"<b>Trend:</b>     {sig['trend']}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"<b>Entry:</b>     {sig['entry']}\n"
        f"<b>SL:</b>        {sig['sl']}\n"
        f"<b>TP:</b>        {sig['tp']}\n"
        f"<b>RR:</b>        {sig['rr']}R\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"<b>Score:</b>     {score}/100 [{bars}] {grade}\n"
        f"<b>TBS:</b>       {tbs}\n"
        f"<b>FVG:</b>       {fvg}\n"
        f"<b>CISD:</b>      {cisd}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"<i>CRT Scanner • {sig['timestamp']}</i>"
    )


# ════════════════════════════════════════════════════════════════════
# LOGGER
# ════════════════════════════════════════════════════════════════════

def log(msg):
    ts  = datetime.now(timezone.utc).strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with scan_lock:
        scan_state["log"].appendleft(line)


# ════════════════════════════════════════════════════════════════════
# CONTINUOUS SCANNER (runs in background thread)
# ════════════════════════════════════════════════════════════════════

TFS = ["Day1", "Hour4"]   # Only 1D and 4H per strategy

def scan_pair(symbol):
    """Full CRT scan on one pair across both timeframes."""
    results = []
    for tf in TFS:
        candles = get_candles(symbol, tf, limit=150)
        if not candles or len(candles) < 10:
            continue

        trend = detect_trend(candles)
        crts  = detect_crt(candles, trend)

        for crt in crts:
            direction = crt["direction"]

            # TBS confirmation
            tbs_found, tbs_tf, tbs_strength = check_tbs(symbol, tf, direction, crt["crl"], crt["crh"])

            # Lower TF for FVG and CISD
            lower_tf = "Min60" if tf == "Day1" else "Min15"
            fvg_found, fvg_level = find_fvg(symbol, lower_tf, direction)
            cisd_found            = check_cisd(symbol, lower_tf, direction)

            score, grade, details = score_signal(
                crt, trend, tbs_found, tbs_tf, tbs_strength, fvg_found, cisd_found
            )

            sig = {
                "symbol":    symbol,
                "tf":        tf,
                "direction": direction,
                "trend":     trend,
                "entry":     crt["entry"],
                "sl":        crt["sl"],
                "tp":        crt["tp"],
                "rr":        crt["rr"],
                "crh":       crt["crh"],
                "crl":       crt["crl"],
                "score":     score,
                "grade":     grade,
                "details":   details,
                "tbs_found":    tbs_found,
                "tbs_tf":       tbs_tf or "–",
                "tbs_strength": tbs_strength or "–",
                "fvg_found":    fvg_found,
                "fvg_level":    fvg_level or "–",
                "cisd_found":   cisd_found,
                "timestamp":    datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            }
            results.append(sig)

    return results


def scanner_loop():
    """
    Infinite loop:
      1. Fetch all MEXC perp pairs
      2. Scan each pair on 1D + 4H
      3. Store and alert valid signals
      4. Restart immediately from step 1
    """
    with scan_lock:
        scan_state["running"] = True

    log("🚀 Scanner started — fetching all MEXC perpetual pairs...")

    while True:
        try:
            pairs = get_all_pairs()
            if not pairs:
                log("⚠️ Could not fetch pairs — retrying in 30s")
                time.sleep(30)
                continue

            with scan_lock:
                scan_state["total_pairs"] = len(pairs)
                scan_state["pairs_done"]  = 0
                scan_state["scan_count"] += 1

            scan_num = scan_state["scan_count"]
            log(f"🔄 Scan #{scan_num} started — {len(pairs)} pairs")

            for i, symbol in enumerate(pairs):
                with scan_lock:
                    scan_state["current_pair"] = symbol
                    scan_state["current_tf"]   = "1D + 4H"
                    scan_state["pairs_done"]   = i + 1

                try:
                    results = scan_pair(symbol)
                    for sig in results:
                        signals.appendleft(sig)
                        with scan_lock:
                            scan_state["signals_found"] += 1
                        log(f"🎯 SIGNAL: {sig['direction']} {symbol} {sig['tf']} | Score: {sig['score']}/100 {sig['grade']} | RR: {sig['rr']}R")
                        send_telegram(format_signal_message(sig))
                except Exception as e:
                    pass   # skip bad pairs silently

                # Small delay per pair to respect rate limits
                time.sleep(0.4)

            with scan_lock:
                scan_state["last_scan_time"] = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")

            log(f"✅ Scan #{scan_num} complete — {len(pairs)} pairs scanned. Restarting immediately...")

        except Exception as e:
            log(f"❌ Scanner error: {e}")
            time.sleep(15)


# ════════════════════════════════════════════════════════════════════
# LOGIN HTML
# ════════════════════════════════════════════════════════════════════

LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/><meta name="viewport" content="width=device-width,initial-scale=1.0"/>
<title>CRT Scanner — Login</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700;800&display=swap');
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Inter',sans-serif;background:#030508;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px;overflow:hidden}
.bg{position:fixed;inset:0;z-index:0}
.orb{position:absolute;border-radius:50%;filter:blur(100px);opacity:.15}
.o1{width:500px;height:500px;background:#3b82f6;top:-200px;left:-100px;animation:drift 12s ease-in-out infinite}
.o2{width:400px;height:400px;background:#8b5cf6;bottom:-150px;right:-100px;animation:drift 10s ease-in-out infinite reverse}
.o3{width:250px;height:250px;background:#10b981;top:40%;left:30%;animation:drift 14s ease-in-out infinite 3s}
@keyframes drift{0%,100%{transform:translate(0,0)}33%{transform:translate(30px,-20px)}66%{transform:translate(-20px,30px)}}
.card{background:rgba(10,14,26,.8);border:1px solid rgba(59,130,246,.2);border-radius:24px;padding:48px 40px;width:100%;max-width:420px;position:relative;z-index:10;backdrop-filter:blur(24px);box-shadow:0 32px 80px rgba(0,0,0,.7)}
.brand{text-align:center;margin-bottom:36px}
.brand-icon{font-size:3.5rem;display:block;margin-bottom:12px;filter:drop-shadow(0 0 20px rgba(59,130,246,.6))}
.brand-name{font-size:1.7rem;font-weight:800;background:linear-gradient(135deg,#3b82f6,#8b5cf6);-webkit-background-clip:text;-webkit-text-fill-color:transparent;letter-spacing:-.03em}
.brand-sub{font-size:.8rem;color:#475569;margin-top:4px;letter-spacing:.05em;text-transform:uppercase}
.label{font-size:.72rem;font-weight:700;color:#475569;letter-spacing:.08em;text-transform:uppercase;display:block;margin-bottom:7px}
.input{width:100%;padding:14px 16px;background:rgba(255,255,255,.04);border:1.5px solid rgba(255,255,255,.08);border-radius:12px;color:#e2e8f0;font-size:.95rem;font-family:'Inter',sans-serif;outline:none;transition:all .2s;margin-bottom:20px}
.input:focus{border-color:#3b82f6;background:rgba(59,130,246,.06);box-shadow:0 0 0 3px rgba(59,130,246,.12)}
.input::placeholder{color:#334155}
.submit{width:100%;padding:15px;background:linear-gradient(135deg,#3b82f6,#6366f1);color:#fff;border:none;border-radius:12px;font-size:1rem;font-weight:700;font-family:'Inter',sans-serif;cursor:pointer;transition:all .2s;letter-spacing:.01em;position:relative;overflow:hidden}
.submit:hover{transform:translateY(-2px);box-shadow:0 12px 32px rgba(59,130,246,.45)}
.submit:active{transform:translateY(0)}
.err{background:rgba(239,68,68,.1);border:1px solid rgba(239,68,68,.3);border-radius:10px;padding:12px 14px;font-size:.82rem;color:#f87171;margin-bottom:16px;text-align:center;display:none}
.err.show{display:block}
.pills{display:flex;gap:8px;margin-top:28px;flex-wrap:wrap;justify-content:center}
.pill{background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.08);border-radius:20px;padding:6px 12px;font-size:.7rem;color:#475569;display:flex;align-items:center;gap:5px}
</style>
</head>
<body>
<div class="bg"><div class="orb o1"></div><div class="orb o2"></div><div class="orb o3"></div></div>
<div class="card">
  <div class="brand">
    <span class="brand-icon">📡</span>
    <div class="brand-name">CRT Scanner</div>
    <div class="brand-sub">MEXC Perpetual Futures</div>
  </div>
  <div class="err" id="err"></div>
  <label class="label">Password</label>
  <input class="input" type="password" id="pw" placeholder="Enter password" autofocus/>
  <button class="submit" id="btn" onclick="login()">🔓 Access Dashboard</button>
  <div class="pills">
    <div class="pill">📊 CRT Strategy</div>
    <div class="pill">🔄 Continuous Scan</div>
    <div class="pill">🤖 Telegram Alerts</div>
    <div class="pill">⚡ All Perp Pairs</div>
    <div class="pill">🏆 Signal Scoring</div>
  </div>
</div>
<script>
function login(){
  const pw=document.getElementById('pw').value.trim();
  const err=document.getElementById('err');
  const btn=document.getElementById('btn');
  if(!pw){err.textContent='Please enter your password.';err.classList.add('show');return;}
  btn.textContent='Verifying...';btn.disabled=true;err.classList.remove('show');
  fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:pw})})
    .then(r=>r.json()).then(d=>{
      if(d.ok){btn.textContent='✅ Success!';setTimeout(()=>window.location.href='/dashboard',400);}
      else{err.textContent='❌ Wrong password.';err.classList.add('show');btn.textContent='🔓 Access Dashboard';btn.disabled=false;document.getElementById('pw').value='';document.getElementById('pw').focus();}
    }).catch(()=>{btn.textContent='🔓 Access Dashboard';btn.disabled=false;});
}
document.getElementById('pw').addEventListener('keydown',e=>{if(e.key==='Enter')login();});
</script>
</body>
</html>"""


# ════════════════════════════════════════════════════════════════════
# DASHBOARD HTML
# ════════════════════════════════════════════════════════════════════

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/><meta name="viewport" content="width=device-width,initial-scale=1.0"/>
<title>CRT Scanner Dashboard</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;600&display=swap');
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#030508;--s1:#080c14;--s2:#0d1220;--s3:#111827;
  --border:#1a2234;--border2:#243047;
  --blue:#3b82f6;--indigo:#6366f1;--green:#10b981;--red:#ef4444;
  --yellow:#f59e0b;--purple:#8b5cf6;--cyan:#06b6d4;
  --text:#e2e8f0;--dim:#94a3b8;--muted:#475569;
}
body{font-family:'Inter',sans-serif;background:var(--bg);color:var(--text);min-height:100vh;padding-bottom:80px}

/* ── HEADER ── */
.hdr{background:rgba(8,12,20,.95);border-bottom:1px solid var(--border);position:sticky;top:0;z-index:200;backdrop-filter:blur(16px)}
.hdr-in{max-width:1200px;margin:0 auto;padding:0 20px;height:58px;display:flex;align-items:center;justify-content:space-between;gap:16px}
.brand{display:flex;align-items:center;gap:10px}
.brand-icon{font-size:1.4rem}
.brand-name{font-size:1rem;font-weight:800;letter-spacing:-.02em}
.brand-name span{background:linear-gradient(135deg,#3b82f6,#8b5cf6);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.pulse-wrap{display:flex;align-items:center;gap:6px}
.pulse{width:8px;height:8px;border-radius:50%;background:var(--green);box-shadow:0 0 8px var(--green);animation:pp 2s infinite}
@keyframes pp{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.5;transform:scale(.7)}}
.pulse-txt{font-size:.72rem;color:var(--green);font-weight:600}
.hdr-right{display:flex;align-items:center;gap:10px}
.scan-badge{background:rgba(59,130,246,.12);border:1px solid rgba(59,130,246,.25);border-radius:20px;padding:5px 12px;font-size:.72rem;font-weight:700;color:var(--blue);font-family:'JetBrains Mono',monospace}
.btn-logout{padding:6px 14px;background:transparent;border:1px solid var(--border);border-radius:8px;color:var(--muted);font-size:.73rem;font-weight:600;cursor:pointer;font-family:'Inter',sans-serif;transition:all .2s}
.btn-logout:hover{border-color:var(--red);color:var(--red)}

/* ── SCAN PROGRESS BAR ── */
.progress-bar-wrap{background:var(--s1);border-bottom:1px solid var(--border);padding:8px 20px;display:flex;align-items:center;gap:12px}
.progress-bar-inner{max-width:1200px;margin:0 auto;width:100%;display:flex;align-items:center;gap:12px}
.progress-track{flex:1;height:4px;background:var(--s3);border-radius:2px;overflow:hidden}
.progress-fill{height:100%;background:linear-gradient(90deg,var(--blue),var(--purple));border-radius:2px;transition:width .5s ease;width:0%}
.progress-txt{font-size:.7rem;color:var(--muted);white-space:nowrap;font-family:'JetBrains Mono',monospace}
.current-pair{font-size:.7rem;color:var(--blue);font-family:'JetBrains Mono',monospace;white-space:nowrap;max-width:200px;overflow:hidden;text-overflow:ellipsis}

/* ── STATS ── */
.stats{max-width:1200px;margin:20px auto 0;padding:0 20px;display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
.stat{background:var(--s1);border:1px solid var(--border);border-radius:14px;padding:16px 18px;position:relative;overflow:hidden;transition:transform .2s,border-color .2s}
.stat:hover{transform:translateY(-2px);border-color:var(--border2)}
.stat::after{content:'';position:absolute;top:0;left:0;right:0;height:2px;border-radius:2px 2px 0 0}
.st-total::after{background:linear-gradient(90deg,var(--blue),var(--indigo))}
.st-buy::after{background:var(--green)}
.st-sell::after{background:var(--red)}
.st-scan::after{background:var(--yellow)}
.st-pairs::after{background:var(--purple)}
.stat-lbl{font-size:.62rem;color:var(--muted);text-transform:uppercase;letter-spacing:.08em;font-weight:600;margin-bottom:8px}
.stat-val{font-size:1.5rem;font-weight:800;font-family:'JetBrains Mono',monospace;line-height:1}
.stat-sub{font-size:.68rem;color:var(--muted);margin-top:5px}

/* ── TABS ── */
.tabs-wrap{max-width:1200px;margin:24px auto 0;padding:0 20px}
.tabs{display:flex;gap:3px;background:var(--s1);border:1px solid var(--border);border-radius:13px;padding:4px;margin-bottom:20px;overflow-x:auto}
.tab{flex:1;min-width:90px;padding:9px 8px;border:none;border-radius:10px;font-family:'Inter',sans-serif;font-size:.76rem;font-weight:700;cursor:pointer;transition:all .2s;color:var(--muted);background:transparent;white-space:nowrap;text-align:center}
.tab.active{background:var(--blue);color:#fff;box-shadow:0 4px 14px rgba(59,130,246,.4)}

/* ── SIGNAL CARDS ── */
.sig-list{display:flex;flex-direction:column;gap:10px}
.empty-state{display:flex;flex-direction:column;align-items:center;justify-content:center;padding:70px 20px;background:var(--s1);border:1px dashed var(--border);border-radius:16px;text-align:center;gap:10px}
.empty-ico{font-size:3rem}.empty-t{font-size:1rem;font-weight:700}.empty-s{font-size:.82rem;color:var(--muted);max-width:340px;line-height:1.6}

.sig-card{background:var(--s1);border:1px solid var(--border);border-radius:16px;padding:18px 20px;animation:cfi .3s ease;transition:all .2s;cursor:default}
.sig-card:hover{transform:translateY(-2px);box-shadow:0 12px 40px rgba(0,0,0,.5);border-color:var(--border2)}
.sig-card.buy{border-left:3px solid var(--green)}.sig-card.sell{border-left:3px solid var(--red)}
@keyframes cfi{from{opacity:0;transform:translateY(-10px)}to{opacity:1;transform:translateY(0)}}

.sig-top{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:12px}
.dir-badge{font-size:.7rem;font-weight:800;padding:4px 12px;border-radius:20px;letter-spacing:.05em}
.dir-badge.BUY{background:rgba(16,185,129,.15);color:var(--green);border:1px solid rgba(16,185,129,.3)}
.dir-badge.SELL{background:rgba(239,68,68,.15);color:var(--red);border:1px solid rgba(239,68,68,.3)}
.sym-name{font-size:1rem;font-weight:800;letter-spacing:.03em}
.tf-chip{font-size:.68rem;color:var(--cyan);background:rgba(6,182,212,.1);border:1px solid rgba(6,182,212,.25);padding:3px 9px;border-radius:20px;font-weight:600}
.trend-chip{font-size:.65rem;font-weight:700;padding:3px 9px;border-radius:20px}
.trend-chip.BULLISH{background:rgba(16,185,129,.1);color:var(--green);border:1px solid rgba(16,185,129,.2)}
.trend-chip.BEARISH{background:rgba(239,68,68,.1);color:var(--red);border:1px solid rgba(239,68,68,.2)}
.trend-chip.NEUTRAL{background:rgba(100,116,139,.1);color:var(--muted);border:1px solid rgba(100,116,139,.2)}
.grade-badge{font-size:.75rem;font-weight:800;padding:4px 10px;border-radius:8px;font-family:'JetBrains Mono',monospace}
.grade-Ap{background:rgba(16,185,129,.2);color:var(--green)}.grade-A{background:rgba(59,130,246,.2);color:var(--blue)}
.grade-B{background:rgba(245,158,11,.2);color:var(--yellow)}.grade-C{background:rgba(239,68,68,.15);color:var(--red)}
.grade-D{background:rgba(100,116,139,.15);color:var(--muted)}
.sig-ts{font-size:.65rem;color:var(--muted);margin-left:auto}

.sig-levels{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:8px;margin-bottom:12px}
.level{background:var(--s2);border:1px solid var(--border);border-radius:10px;padding:10px 12px}
.level-lbl{font-size:.6rem;color:var(--muted);text-transform:uppercase;letter-spacing:.06em;margin-bottom:4px;font-weight:600}
.level-val{font-size:.82rem;font-weight:700;font-family:'JetBrains Mono',monospace}
.level-val.entry{color:var(--blue)}.level-val.sl{color:var(--red)}.level-val.tp{color:var(--green)}
.level-val.rr{color:var(--yellow)}

.sig-confirms{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px}
.confirm-pill{font-size:.67rem;font-weight:600;padding:3px 9px;border-radius:20px}
.cp-ok{background:rgba(16,185,129,.1);color:var(--green);border:1px solid rgba(16,185,129,.2)}
.cp-no{background:rgba(100,116,139,.07);color:var(--muted);border:1px solid var(--border)}

.score-bar-wrap{display:flex;align-items:center;gap:10px}
.score-lbl{font-size:.68rem;color:var(--muted);white-space:nowrap;width:70px}
.score-track{flex:1;height:6px;background:var(--s3);border-radius:3px;overflow:hidden}
.score-fill{height:100%;border-radius:3px;transition:width .6s ease}
.score-num{font-size:.72rem;font-weight:700;font-family:'JetBrains Mono',monospace;white-space:nowrap;width:55px;text-align:right}

.details-toggle{font-size:.7rem;color:var(--blue);cursor:pointer;margin-top:8px;display:inline-block}
.details-box{display:none;margin-top:10px;background:var(--s2);border:1px solid var(--border);border-radius:10px;padding:12px;font-size:.73rem;color:var(--dim);line-height:1.8}
.details-box.open{display:block}

/* ── LIVE LOG ── */
.log-wrap{background:var(--s1);border:1px solid var(--border);border-radius:14px;overflow:hidden}
.log-header{padding:12px 16px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between}
.log-title{font-size:.82rem;font-weight:700}
.log-body{padding:12px 16px;max-height:460px;overflow-y:auto;font-family:'JetBrains Mono',monospace;font-size:.72rem;color:var(--dim);line-height:1.9}
.log-line.sig{color:var(--green)}.log-line.err{color:var(--red)}.log-line.inf{color:var(--blue)}

/* ── TOAST ── */
.toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%) translateY(100px);background:var(--s2);border:1px solid var(--border2);border-radius:12px;padding:13px 24px;font-size:.84rem;font-weight:600;box-shadow:0 16px 48px rgba(0,0,0,.6);opacity:0;transition:all .3s cubic-bezier(.34,1.56,.64,1);pointer-events:none;z-index:9999;white-space:nowrap}
.toast.show{transform:translateX(-50%) translateY(0);opacity:1}

@media(max-width:640px){
  .stats{grid-template-columns:1fr 1fr}
  .hdr-in{padding:0 14px}.tabs-wrap{padding:0 14px}
  .sig-levels{grid-template-columns:1fr 1fr}
  .scan-badge{display:none}
}
</style>
</head>
<body>

<div class="hdr">
  <div class="hdr-in">
    <div class="brand">
      <span class="brand-icon">📡</span>
      <span class="brand-name"><span>CRT</span> Scanner</span>
    </div>
    <div class="pulse-wrap">
      <div class="pulse"></div>
      <span class="pulse-txt" id="pulse-txt">Scanning...</span>
    </div>
    <div class="hdr-right">
      <div class="scan-badge" id="scan-badge">Scan #0</div>
      <button class="btn-logout" onclick="logout()">Logout</button>
    </div>
  </div>
</div>

<div class="progress-bar-wrap">
  <div class="progress-bar-inner">
    <span class="current-pair" id="cur-pair">Initialising...</span>
    <div class="progress-track"><div class="progress-fill" id="prog-fill"></div></div>
    <span class="progress-txt" id="prog-txt">0 / 0</span>
  </div>
</div>

<div class="stats">
  <div class="stat st-total"><div class="stat-lbl">Total Signals</div><div class="stat-val" id="s-total">0</div><div class="stat-sub">All time</div></div>
  <div class="stat st-buy"><div class="stat-lbl">🟢 BUY</div><div class="stat-val" id="s-buy">0</div></div>
  <div class="stat st-sell"><div class="stat-lbl">🔴 SELL</div><div class="stat-val" id="s-sell">0</div></div>
  <div class="stat st-scan"><div class="stat-lbl">Scans Done</div><div class="stat-val" id="s-scans">0</div><div class="stat-sub" id="s-last">–</div></div>
  <div class="stat st-pairs"><div class="stat-lbl">Pairs Scanned</div><div class="stat-val" id="s-pairs">0</div><div class="stat-sub">This scan</div></div>
</div>

<div class="tabs-wrap">
  <div class="tabs">
    <button class="tab active" onclick="sw('signals',this)">📊 Signals</button>
    <button class="tab" onclick="sw('log',this)">🖥 Live Log</button>
  </div>

  <!-- SIGNALS -->
  <div id="tab-signals">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px;flex-wrap:wrap;gap:8px">
      <div style="font-size:.9rem;font-weight:700">Latest CRT Signals</div>
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
        <select id="filter-dir" onchange="renderSignals()" style="background:#0d1220;border:1px solid #1a2234;border-radius:8px;color:#e2e8f0;padding:6px 10px;font-size:.75rem;font-family:'Inter',sans-serif">
          <option value="">All Directions</option>
          <option value="BUY">BUY Only</option>
          <option value="SELL">SELL Only</option>
        </select>
        <select id="filter-tf" onchange="renderSignals()" style="background:#0d1220;border:1px solid #1a2234;border-radius:8px;color:#e2e8f0;padding:6px 10px;font-size:.75rem;font-family:'Inter',sans-serif">
          <option value="">All Timeframes</option>
          <option value="Day1">1D Only</option>
          <option value="Hour4">4H Only</option>
        </select>
        <select id="filter-grade" onchange="renderSignals()" style="background:#0d1220;border:1px solid #1a2234;border-radius:8px;color:#e2e8f0;padding:6px 10px;font-size:.75rem;font-family:'Inter',sans-serif">
          <option value="">All Grades</option>
          <option value="A+">A+ Only</option>
          <option value="A">A Only</option>
          <option value="B">B+</option>
        </select>
      </div>
    </div>
    <div class="sig-list" id="sig-list">
      <div class="empty-state">
        <div class="empty-ico">🔍</div>
        <div class="empty-t">Scanning markets...</div>
        <div class="empty-s">The scanner is running through all MEXC perpetual futures pairs looking for CRT formations. Signals will appear here automatically.</div>
      </div>
    </div>
  </div>

  <!-- LOG -->
  <div id="tab-log" style="display:none">
    <div class="log-wrap">
      <div class="log-header">
        <span class="log-title">🖥 Scanner Live Log</span>
        <span style="font-size:.7rem;color:#475569">Auto-updates every 3s</span>
      </div>
      <div class="log-body" id="log-body">Loading...</div>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
(function(){
  'use strict';
  let allSignals=[], toastT, activeTab='signals';
  const $=id=>document.getElementById(id);

  function toast(m,d=3000){
    const t=$('toast');t.textContent=m;t.classList.add('show');
    clearTimeout(toastT);toastT=setTimeout(()=>t.classList.remove('show'),d);
  }

  function scoreColor(s){
    if(s>=85)return'#10b981';if(s>=70)return'#3b82f6';
    if(s>=55)return'#f59e0b';return'#ef4444';
  }

  function fmt(v){
    if(v===null||v===undefined||v==='–')return'–';
    const n=Number(v);if(isNaN(n))return v;
    if(n>=1000)return n.toLocaleString(undefined,{maximumFractionDigits:2});
    if(n>=1)return n.toFixed(4);
    return n.toFixed(6);
  }

  function buildCard(s,idx){
    const dir=s.direction||'BUY';
    const score=s.score||0;
    const grade=s.grade||'–';
    const gradeClass='grade-'+(grade==='A+'?'Ap':grade);
    const tbs=s.tbs_found?`<span class="confirm-pill cp-ok">✅ TBS ${s.tbs_tf}</span>`:`<span class="confirm-pill cp-no">❌ TBS</span>`;
    const fvg=s.fvg_found?`<span class="confirm-pill cp-ok">✅ FVG</span>`:`<span class="confirm-pill cp-no">❌ FVG</span>`;
    const cisd=s.cisd_found?`<span class="confirm-pill cp-ok">✅ CISD</span>`:`<span class="confirm-pill cp-no">❌ CISD</span>`;
    const details=(s.details||[]).join('<br>');
    const tfLabel=s.tf==='Day1'?'1D':s.tf==='Hour4'?'4H':s.tf;
    return `<div class="sig-card ${dir.toLowerCase()}">
      <div class="sig-top">
        <span class="dir-badge ${dir}">${dir}</span>
        <span class="sym-name">${s.symbol||'–'}</span>
        <span class="tf-chip">${tfLabel}</span>
        <span class="trend-chip ${s.trend}">${s.trend}</span>
        <span class="grade-badge ${gradeClass}">${grade}</span>
        <span class="sig-ts">${s.timestamp||''}</span>
      </div>
      <div class="sig-levels">
        <div class="level"><div class="level-lbl">Entry</div><div class="level-val entry">${fmt(s.entry)}</div></div>
        <div class="level"><div class="level-lbl">Stop Loss</div><div class="level-val sl">${fmt(s.sl)}</div></div>
        <div class="level"><div class="level-lbl">Take Profit</div><div class="level-val tp">${fmt(s.tp)}</div></div>
        <div class="level"><div class="level-lbl">Risk:Reward</div><div class="level-val rr">${s.rr}R</div></div>
        <div class="level"><div class="level-lbl">CRH</div><div class="level-val">${fmt(s.crh)}</div></div>
        <div class="level"><div class="level-lbl">CRL</div><div class="level-val">${fmt(s.crl)}</div></div>
      </div>
      <div class="sig-confirms">${tbs}${fvg}${cisd}</div>
      <div class="score-bar-wrap">
        <span class="score-lbl">Signal Score</span>
        <div class="score-track"><div class="score-fill" style="width:${score}%;background:${scoreColor(score)}"></div></div>
        <span class="score-num" style="color:${scoreColor(score)}">${score}/100</span>
      </div>
      ${details?`<span class="details-toggle" onclick="toggleD(${idx})">▼ Score breakdown</span>
      <div class="details-box" id="det-${idx}">${details}</div>`:''}
    </div>`;
  }

  window.toggleD=function(idx){
    const b=$('det-'+idx);if(b)b.classList.toggle('open');
  };

  window.renderSignals=function(){
    const dirF=$('filter-dir').value;
    const tfF=$('filter-tf').value;
    const grF=$('filter-grade').value;
    let filtered=allSignals.filter(s=>{
      if(dirF&&s.direction!==dirF)return false;
      if(tfF&&s.tf!==tfF)return false;
      if(grF){
        if(grF==='A+'&&s.grade!=='A+')return false;
        if(grF==='A'&&s.grade!=='A')return false;
        if(grF==='B'&&!['A+','A','B'].includes(s.grade))return false;
      }
      return true;
    });
    const list=$('sig-list');
    if(!filtered.length){
      list.innerHTML='<div class="empty-state"><div class="empty-ico">🔍</div><div class="empty-t">Scanning markets...</div><div class="empty-s">The scanner is continuously scanning all MEXC perpetual futures. Signals matching your filters will appear here.</div></div>';
      return;
    }
    list.innerHTML=filtered.slice(0,100).map((s,i)=>buildCard(s,i)).join('');
  };

  let lastSigCount=0;

  async function fetchSignals(){
    try{
      const r=await fetch('/api/signals?limit=200');
      const data=await r.json();
      allSignals=data;
      if(data.length>lastSigCount&&lastSigCount>0){
        const n=data[0];
        toast(`🎯 New Signal: ${n.direction} ${n.symbol} ${n.tf==='Day1'?'1D':'4H'} | Score: ${n.score}/100 ${n.grade}`);
      }
      lastSigCount=data.length;
      renderSignals();
    }catch(e){}
  }

  async function fetchStats(){
    try{
      const r=await fetch('/api/stats');const d=await r.json();
      $('s-total').textContent=d.total||0;
      $('s-buy').textContent=d.buys||0;
      $('s-sell').textContent=d.sells||0;
    }catch{}
  }

  async function fetchScanState(){
    try{
      const r=await fetch('/api/scan-state');const d=await r.json();
      const pct=d.total_pairs>0?Math.round(d.pairs_done/d.total_pairs*100):0;
      $('prog-fill').style.width=pct+'%';
      $('prog-txt').textContent=`${d.pairs_done} / ${d.total_pairs}`;
      $('cur-pair').textContent=d.current_pair?`Scanning: ${d.current_pair}`:'Waiting...';
      $('s-scans').textContent=d.scan_count||0;
      $('s-pairs').textContent=d.pairs_done||0;
      $('s-last').textContent=d.last_scan_time?`Last: ${d.last_scan_time}`:'–';
      $('scan-badge').textContent=`Scan #${d.scan_count||0}`;
      $('pulse-txt').textContent=d.running?`Scanning ${d.current_pair||'...'}`:'Idle';
    }catch{}
  }

  async function fetchLog(){
    if(activeTab!=='log')return;
    try{
      const r=await fetch('/api/log');const d=await r.json();
      const body=$('log-body');
      body.innerHTML=d.log.map(l=>{
        const cls=l.includes('SIGNAL')||l.includes('🎯')?'sig':l.includes('Error')||l.includes('❌')?'err':'inf';
        return`<div class="log-line ${cls}">${l}</div>`;
      }).join('');
    }catch{}
  }

  window.sw=function(tab,btn){
    activeTab=tab;
    document.querySelectorAll('.tab').forEach(b=>b.classList.remove('active'));
    btn.classList.add('active');
    $('tab-signals').style.display=tab==='signals'?'block':'none';
    $('tab-log').style.display=tab==='log'?'block':'none';
    if(tab==='log')fetchLog();
  };

  window.logout=function(){
    fetch('/api/logout',{method:'POST'}).finally(()=>window.location.href='/');
  };

  // Poll
  async function poll(){
    await Promise.all([fetchSignals(),fetchStats(),fetchScanState(),fetchLog()]);
    setTimeout(poll,3000);
  }
  poll();
})();
</script>
</body>
</html>"""


# ════════════════════════════════════════════════════════════════════
# FLASK ROUTES
# ════════════════════════════════════════════════════════════════════

@app.route("/")
def root():
    token = request.cookies.get("session")
    if token and token in sessions:
        return make_response(DASHBOARD_HTML, 200, {"Content-Type": "text/html"})
    return make_response(LOGIN_HTML, 200, {"Content-Type": "text/html"})


@app.route("/dashboard")
def dashboard():
    token = request.cookies.get("session")
    if not token or token not in sessions:
        return make_response(LOGIN_HTML, 200, {"Content-Type": "text/html"})
    return make_response(DASHBOARD_HTML, 200, {"Content-Type": "text/html"})


@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.get_json(silent=True) or {}
    if data.get("password") == DASHBOARD_PASSWORD:
        token = secrets.token_hex(32)
        sessions.add(token)
        resp = make_response(jsonify({"ok": True}))
        resp.set_cookie("session", token, max_age=86400 * 7, httponly=True, samesite="Lax")
        return resp
    return jsonify({"ok": False}), 401


@app.route("/api/logout", methods=["POST"])
def api_logout():
    token = request.cookies.get("session")
    sessions.discard(token)
    resp = make_response(jsonify({"ok": True}))
    resp.delete_cookie("session")
    return resp


@app.route("/api/signals")
def api_signals():
    limit = min(int(request.args.get("limit", 200)), MAX_SIGNALS)
    return jsonify(list(signals)[:limit])


@app.route("/api/stats")
def api_stats():
    all_s = list(signals)
    return jsonify({
        "total": len(all_s),
        "buys":  sum(1 for s in all_s if s.get("direction") == "BUY"),
        "sells": sum(1 for s in all_s if s.get("direction") == "SELL"),
    })


@app.route("/api/scan-state")
def api_scan_state():
    with scan_lock:
        return jsonify({k: v for k, v in scan_state.items() if k != "log"})


@app.route("/api/log")
def api_log():
    with scan_lock:
        return jsonify({"log": list(scan_state["log"])})


@app.route("/health")
def health():
    return jsonify({"status": "healthy", "signals": len(signals), "scanning": scan_state["running"]}), 200


# ════════════════════════════════════════════════════════════════════
# STARTUP
# ════════════════════════════════════════════════════════════════════

def start_scanner():
    t = threading.Thread(target=scanner_loop, daemon=True)
    t.start()
    log("Scanner thread launched.")


with app.app_context():
    start_scanner()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
