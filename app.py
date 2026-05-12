import os, json, time, math, secrets, requests, threading
from datetime import datetime, timezone
from flask import Flask, request, jsonify, make_response
from collections import deque

app = Flask(__name__)

# ── CREDENTIALS ───────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = "PASTE_YOUR_TOKEN_HERE"
TELEGRAM_CHAT_ID   = "7411219487"
DASHBOARD_PASSWORD = "signal123"
# ─────────────────────────────────────────────────────────────────

MAX_SIGNALS = 500
signals     = deque(maxlen=MAX_SIGNALS)
sessions    = set()

scan_state = {
    "running":       False,
    "enabled":       True,        # manual ON/OFF toggle
    "current_pair":  "",
    "pairs_done":    0,
    "total_pairs":   0,
    "scan_count":    0,
    "signals_found": 0,
    "last_scan":     None,
    "log":           deque(maxlen=80),
}
scan_lock = threading.Lock()

TOP_PAIRS = ["BTC_USDT","ETH_USDT","SOL_USDT","BNB_USDT","XRP_USDT","DOGE_USDT"]
MEXC_BASE = "https://contract.mexc.com/api/v1/contract"

# ════════════════════════════════════════════════════════════════════
# MEXC API
# ════════════════════════════════════════════════════════════════════
def get_all_pairs():
    try:
        r = requests.get(f"{MEXC_BASE}/detail", timeout=15)
        data = r.json()
        if not data.get("success"): return []
        return sorted([i["symbol"] for i in data.get("data",[]) if i.get("state")==0])
    except Exception as e:
        log(f"Pairs error: {e}"); return []

def get_candles(symbol, interval, limit=100):
    try:
        r = requests.get(f"{MEXC_BASE}/kline/{symbol}",
                         params={"interval":interval,"limit":limit}, timeout=10)
        data = r.json()
        if not data.get("success") or not data.get("data"): return []
        raw = data["data"]
        times=raw.get("time",[]); opens=raw.get("open",[])
        highs=raw.get("high",[]); lows=raw.get("low",[])
        closes=raw.get("close",[])
        out=[]
        for i in range(len(times)):
            try:
                out.append({"time":int(times[i]),"open":float(opens[i]),
                            "high":float(highs[i]),"low":float(lows[i]),"close":float(closes[i])})
            except: continue
        return out
    except: return []

def get_ticker(symbol):
    try:
        r = requests.get(f"{MEXC_BASE}/ticker", params={"symbol":symbol}, timeout=6)
        data = r.json()
        if data.get("success") and data.get("data"):
            d = data["data"]
            if isinstance(d, list): d = d[0]
            return {
                "price":  float(d.get("lastPrice",0)),
                "change": float(d.get("priceChangePercent",0)),
                "high":   float(d.get("high24h",0)),
                "low":    float(d.get("low24h",0)),
                "vol":    float(d.get("volume24h",0)),
            }
    except: pass
    return None

# ════════════════════════════════════════════════════════════════════
# MARKET STRUCTURE
# ════════════════════════════════════════════════════════════════════
def find_swings(candles, n=3):
    highs=[c["high"] for c in candles]; lows=[c["low"] for c in candles]
    sh=[]; sl=[]
    for i in range(n, len(candles)-n):
        if all(highs[i]>=highs[i-j] and highs[i]>=highs[i+j] for j in range(1,n+1)):
            sh.append((i,highs[i]))
        if all(lows[i]<=lows[i-j] and lows[i]<=lows[i+j] for j in range(1,n+1)):
            sl.append((i,lows[i]))
    return sh, sl

def detect_trend(candles, lookback=80):
    c = candles[-lookback:] if len(candles)>=lookback else candles
    if len(c)<20: return "NEUTRAL", [], []
    sh, sl = find_swings(c, n=3)
    if len(sh)>=2 and len(sl)>=2:
        hh = sh[-1][1]>sh[-2][1]; hl = sl[-1][1]>sl[-2][1]
        lh = sh[-1][1]<sh[-2][1]; ll = sl[-1][1]<sl[-2][1]
        if hh and hl: return "BULLISH", sh, sl
        if lh and ll: return "BEARISH", sh, sl
    closes=[c["close"] for c in c[-20:]]
    a1=sum(closes[:10])/10; a2=sum(closes[10:])/10
    if a2>a1*1.004: return "BULLISH", sh, sl
    if a2<a1*0.996: return "BEARISH", sh, sl
    return "NEUTRAL", sh, sl

def is_continuous_structure(sh, sl, direction, min_points=3):
    """Confirm price is CONTINUOUSLY printing HH/HL or LH/LL — not just one swing."""
    if direction=="BULLISH":
        if len(sh)<min_points or len(sl)<min_points: return False
        # Each swing high higher than previous
        highs_ok = all(sh[i][1]>sh[i-1][1] for i in range(1,len(sh)))
        lows_ok  = all(sl[i][1]>sl[i-1][1] for i in range(1,len(sl)))
        return highs_ok and lows_ok
    else:
        if len(sh)<min_points or len(sl)<min_points: return False
        highs_ok = all(sh[i][1]<sh[i-1][1] for i in range(1,len(sh)))
        lows_ok  = all(sl[i][1]<sl[i-1][1] for i in range(1,len(sl)))
        return highs_ok and lows_ok

# ════════════════════════════════════════════════════════════════════
# ORDER BLOCK DETECTION
# ════════════════════════════════════════════════════════════════════
OB_TIMEFRAMES = ["Hour4","Hour2","Min60","Min45"]

def find_orderblocks(candles, direction, swing_highs, swing_lows):
    """
    Detect valid Order Blocks:
    Bullish OB: last bearish candle before a strong bullish move
                must be at or near the LAST HIGHER LOW
    Bearish OB: last bullish candle before a strong bearish move
                must be at or near the LAST LOWER HIGH

    Returns list of OB dicts sorted newest first.
    """
    obs = []
    if len(candles) < 5: return obs

    for i in range(2, len(candles)-2):
        c     = candles[i]
        c_next = candles[i+1]
        c_prev = candles[i-1]

        if direction == "BULLISH":
            # Bullish OB = last bearish candle (close<open) before bullish expansion
            is_bearish_candle = c["close"] < c["open"]
            # Next candle is strong bullish (closes above OB high)
            strong_move = c_next["close"] > c["high"] and c_next["close"] > c_next["open"]
            if is_bearish_candle and strong_move:
                ob = {
                    "top":    c["open"],          # top of bearish OB = open
                    "bot":    c["close"],          # bot = close
                    "high":   c["high"],
                    "low":    c["low"],
                    "idx":    i,
                    "time":   c["time"],
                    "type":   "BULLISH_OB",
                }
                obs.append(ob)
        else:
            # Bearish OB = last bullish candle before bearish expansion
            is_bullish_candle = c["close"] > c["open"]
            strong_move = c_next["close"] < c["low"] and c_next["close"] < c_next["open"]
            if is_bullish_candle and strong_move:
                ob = {
                    "top":    c["close"],         # top of bullish OB = close
                    "bot":    c["open"],           # bot = open
                    "high":   c["high"],
                    "low":    c["low"],
                    "idx":    i,
                    "time":   c["time"],
                    "type":   "BEARISH_OB",
                }
                obs.append(ob)

    return sorted(obs, key=lambda x: x["idx"], reverse=True)

def ob_at_key_level(ob, direction, swing_highs, swing_lows, tolerance=0.003):
    """
    Check OB is at the last Higher Low (bullish) or last Lower High (bearish).
    tolerance = 0.3% price tolerance for "at" the level.
    """
    if direction=="BULLISH" and swing_lows:
        last_hl = swing_lows[-1][1]
        # OB zone should overlap with the last higher low
        within = (ob["bot"] <= last_hl*(1+tolerance) and ob["top"] >= last_hl*(1-tolerance))
        return within
    elif direction=="BEARISH" and swing_highs:
        last_lh = swing_highs[-1][1]
        within = (ob["top"] >= last_lh*(1-tolerance) and ob["bot"] <= last_lh*(1+tolerance))
        return within
    return False

def previous_obs_respected(obs, candles, direction, min_respected=1):
    """
    Verify that at least `min_respected` previous OBs were respected
    (price reacted significantly from them — not just tapped and blew through).
    """
    if len(obs) < 2: return False
    respected = 0
    # Check all OBs except the most recent one
    for ob in obs[1:]:
        ob_idx = ob["idx"]
        # Look at candles after this OB
        after = candles[ob_idx+1 : ob_idx+8]
        if not after: continue
        if direction=="BULLISH":
            # Price should have bounced up after tapping the bullish OB
            tap   = any(c["low"] <= ob["top"] for c in after[:3])
            react = any(c["close"] > ob["top"] * 1.002 for c in after)
            if tap and react: respected += 1
        else:
            tap   = any(c["high"] >= ob["bot"] for c in after[:3])
            react = any(c["close"] < ob["bot"] * 0.998 for c in after)
            if tap and react: respected += 1
    return respected >= min_respected

def liquidity_sweep_before_ob(candles, ob, direction):
    """
    Before the OB forms, there must be a liquidity sweep —
    a wick (or body) that sweeps below a recent low (bullish) or above a recent high (bearish).
    Even a wick counts.
    """
    idx = ob["idx"]
    lookback = candles[max(0, idx-15):idx]  # 15 candles before OB
    if not lookback: return False

    recent_lows  = [c["low"]  for c in lookback]
    recent_highs = [c["high"] for c in lookback]

    if direction=="BULLISH":
        # Need a candle that swept below recent lows (liquidity grab)
        prev_low = min(recent_lows[:-1]) if len(recent_lows)>1 else recent_lows[0]
        swept = any(c["low"] < prev_low for c in lookback[-5:])
        return swept
    else:
        prev_high = max(recent_highs[:-1]) if len(recent_highs)>1 else recent_highs[0]
        swept = any(c["high"] > prev_high for c in lookback[-5:])
        return swept

def price_tapping_ob(candles, ob, direction):
    """
    Check if current/recent price is tapping INTO the OB zone.
    The most recent candles should have touched the OB.
    """
    recent = candles[-5:]
    if direction=="BULLISH":
        return any(c["low"] <= ob["top"] and c["high"] >= ob["bot"] for c in recent)
    else:
        return any(c["high"] >= ob["bot"] and c["low"] <= ob["top"] for c in recent)

# ════════════════════════════════════════════════════════════════════
# CRT ON 4H (must form FROM the orderblock)
# ════════════════════════════════════════════════════════════════════
def detect_crt_from_ob(candles_4h, ob, direction):
    """
    Detect a 4H CRT formation that originates FROM the orderblock.
    C1 (CRT candle) must overlap with the OB zone.
    Returns list of valid CRT dicts.
    """
    found = []
    if len(candles_4h) < 5: return found

    for offset in range(1, min(12, len(candles_4h)-2)):
        i3 = len(candles_4h)-1-offset
        i2 = i3-1; i1 = i2-1
        if i1 < 0: break

        c1 = candles_4h[i1]
        c2 = candles_4h[i2]
        c3 = candles_4h[i3]

        crh = c1["high"]; crl = c1["low"]
        cr_range = crh - crl
        if cr_range <= 0: continue

        # C1 must be rooted in the OB zone
        c1_in_ob = (c1["low"] <= ob["top"] and c1["high"] >= ob["bot"])
        if not c1_in_ob: continue

        if direction=="BULLISH":
            swept     = c2["low"] < crl
            c2_inside = crl <= c2["close"] <= crh
            wick_ok   = (c2["close"]-c2["low"]) > cr_range*0.05
            c3_bull   = c3["close"] > c3["open"]
            if swept and c2_inside and wick_ok:
                entry  = c2["close"]
                sl     = c2["low"]
                tp     = crh
                risk   = abs(entry-sl); reward = abs(tp-entry)
                rr     = round(reward/risk,2) if risk>0 else 0
                if rr >= 3.0:
                    found.append({"direction":"BUY","c1":c1,"c2":c2,"c3":c3,
                                  "crh":crh,"crl":crl,"entry":round(entry,8),
                                  "sl":round(sl,8),"tp":round(tp,8),"rr":rr,
                                  "sweep":round(crl-c2["low"],8),
                                  "c3_confirms":c3_bull})
        else:
            swept     = c2["high"] > crh
            c2_inside = crl <= c2["close"] <= crh
            wick_ok   = (c2["high"]-c2["close"]) > cr_range*0.05
            c3_bear   = c3["close"] < c3["open"]
            if swept and c2_inside and wick_ok:
                entry  = c2["close"]
                sl     = c2["high"]
                tp     = crl
                risk   = abs(sl-entry); reward = abs(entry-tp)
                rr     = round(reward/risk,2) if risk>0 else 0
                if rr >= 3.0:
                    found.append({"direction":"SELL","c1":c1,"c2":c2,"c3":c3,
                                  "crh":crh,"crl":crl,"entry":round(entry,8),
                                  "sl":round(sl,8),"tp":round(tp,8),"rr":rr,
                                  "sweep":round(c2["high"]-crh,8),
                                  "c3_confirms":c3_bear})
    return found

# ════════════════════════════════════════════════════════════════════
# TBS
# ════════════════════════════════════════════════════════════════════
def check_tbs(symbol, direction, crl, crh):
    for tf in ["Min60","Min30","Min15"]:
        candles = get_candles(symbol, tf, limit=50)
        if not candles: continue
        for c in reversed(candles[-25:]):
            if direction=="BUY":
                if c["low"]<crl and c["close"]>crl: return True, tf, "MODERATE"
                if min(c["open"],c["close"])<crl and max(c["open"],c["close"])>crl: return True, tf, "STRONG"
            else:
                if c["high"]>crh and c["close"]<crh: return True, tf, "MODERATE"
                if max(c["open"],c["close"])>crh and min(c["open"],c["close"])<crh: return True, tf, "STRONG"
    return False, None, None

# ════════════════════════════════════════════════════════════════════
# CHOCH
# ════════════════════════════════════════════════════════════════════
def check_choch(symbol, tf, direction):
    candles = get_candles(symbol, tf, limit=50)
    if not candles or len(candles)<5: return False, None
    recent = candles[-30:]
    sh=[]; sl=[]
    for i in range(2,len(recent)-2):
        c=recent[i]
        if (c["high"]>recent[i-1]["high"] and c["high"]>recent[i-2]["high"] and
            c["high"]>recent[i+1]["high"] and c["high"]>=recent[i+2]["high"]):
            sh.append((i,c["high"]))
        if (c["low"]<recent[i-1]["low"] and c["low"]<recent[i-2]["low"] and
            c["low"]<recent[i+1]["low"] and c["low"]<=recent[i+2]["low"]):
            sl.append((i,c["low"]))
    if direction=="BUY":
        if not sh:
            for i in range(len(recent)-1,0,-1):
                c=recent[i]; p=recent[i-1]
                if c["close"]>p["high"] and c["close"]>c["open"]:
                    return True, round(p["high"],8)
            return False, None
        last_idx,last_val=sh[-1]
        for i in range(last_idx+1,len(recent)):
            c=recent[i]
            if c["close"]>last_val and c["close"]>c["open"]:
                return True, round(last_val,8)
    else:
        if not sl:
            for i in range(len(recent)-1,0,-1):
                c=recent[i]; p=recent[i-1]
                if c["close"]<p["low"] and c["close"]<c["open"]:
                    return True, round(p["low"],8)
            return False, None
        last_idx,last_val=sl[-1]
        for i in range(last_idx+1,len(recent)):
            c=recent[i]
            if c["close"]<last_val and c["close"]<c["open"]:
                return True, round(last_val,8)
    return False, None

# ════════════════════════════════════════════════════════════════════
# FVG + IFVG  (entry at TIP)
# ════════════════════════════════════════════════════════════════════
def find_fvg(symbol, tf, direction):
    candles = get_candles(symbol, tf, limit=60)
    if not candles or len(candles)<5: return False, None, None, None, None
    fresh=[]; ifvg=[]
    for i in range(len(candles)-3):
        c1=candles[i]; c3=candles[i+2]
        if direction=="BUY":
            if c3["low"]>c1["high"]:
                zbot=c1["high"]; ztop=c3["low"]
                mit=any(candles[j]["low"]<=ztop for j in range(i+3,len(candles)))
                if not mit:
                    fresh.append({"type":"FVG","entry":round(zbot,8),
                                  "zone_top":round(ztop,8),"zone_bot":round(zbot,8),"idx":i})
                else:
                    ifvg.append({"type":"IFVG","entry":round(ztop,8),
                                 "zone_top":round(ztop,8),"zone_bot":round(zbot,8),"idx":i})
        else:
            if c3["high"]<c1["low"]:
                ztop=c1["low"]; zbot=c3["high"]
                mit=any(candles[j]["high"]>=zbot for j in range(i+3,len(candles)))
                if not mit:
                    fresh.append({"type":"FVG","entry":round(ztop,8),
                                  "zone_top":round(ztop,8),"zone_bot":round(zbot,8),"idx":i})
                else:
                    ifvg.append({"type":"IFVG","entry":round(zbot,8),
                                 "zone_top":round(ztop,8),"zone_bot":round(zbot,8),"idx":i})
    if fresh:
        b=max(fresh,key=lambda x:x["idx"])
        return True,b["type"],b["entry"],b["zone_top"],b["zone_bot"]
    if ifvg:
        b=max(ifvg,key=lambda x:x["idx"])
        return True,b["type"],b["entry"],b["zone_top"],b["zone_bot"]
    return False,None,None,None,None

# ════════════════════════════════════════════════════════════════════
# SIGNAL SCORING  (0-100)
# ════════════════════════════════════════════════════════════════════
def score_signal(crt, trend, ob, liq_swept, ob_respected, at_key_level,
                 tbs_found, tbs_strength, fvg_found, fvg_type, choch_found,
                 continuous_structure):
    score=0; details=[]
    direction=crt["direction"]; rr=crt["rr"]
    cr_range=crt["crh"]-crt["crl"]
    c2=crt["c2"]

    # 1. Continuous market structure (15pts) — NEW stricter check
    if continuous_structure:
        score+=15; details.append("✅ Continuous HH/HL or LH/LL structure (+15)")
    else:
        details.append("❌ Structure not continuous (0)")

    # 2. Trend alignment (10pts)
    if (direction=="BUY" and trend=="BULLISH") or (direction=="SELL" and trend=="BEARISH"):
        score+=10; details.append("✅ Trend aligned (+10)")
    else:
        details.append("❌ Counter-trend (+0)")

    # 3. OB at key level — last HL or LH (15pts)
    if at_key_level:
        score+=15; details.append("✅ OB at last key swing level (+15)")
    else:
        details.append("❌ OB not at key level (+0)")

    # 4. Liquidity sweep before OB (15pts)
    if liq_swept:
        score+=15; details.append("✅ Liquidity sweep before OB (+15)")
    else:
        details.append("❌ No liquidity sweep (+0)")

    # 5. Previous OBs respected (10pts)
    if ob_respected:
        score+=10; details.append("✅ Previous OBs respected (+10)")
    else:
        details.append("⚠️ Previous OBs not confirmed (+0)")

    # 6. RR quality (10pts)
    if rr>=5:   score+=10; details.append(f"✅ Exceptional RR {rr}R (+10)")
    elif rr>=4: score+=8;  details.append(f"✅ Strong RR {rr}R (+8)")
    elif rr>=3: score+=6;  details.append(f"⚠️ Minimum RR {rr}R (+6)")

    # 7. TBS (10pts)
    if tbs_found:
        pts=10 if tbs_strength=="STRONG" else 7
        score+=pts; details.append(f"✅ TBS {tbs_strength} (+{pts})")
    else:
        details.append("❌ No TBS (+0)")

    # 8. CHOCH (8pts)
    if choch_found:
        score+=8; details.append("✅ CHOCH confirmed (+8)")
    else:
        details.append("⚠️ No CHOCH (+0)")

    # 9. FVG/IFVG entry (7pts)
    if fvg_found:
        score+=7; details.append(f"✅ {fvg_type} entry tip (+7)")
    else:
        details.append("⚠️ No FVG/IFVG (+0)")

    # Bonus: C3 confirms
    if crt.get("c3_confirms"):
        score=min(score+5,100); details.append("✅ C3 confirms direction (+5)")

    grade = "A+" if score>=85 else "A" if score>=72 else "B" if score>=58 else "C" if score>=44 else "D"
    return min(score,100), grade, details

# ════════════════════════════════════════════════════════════════════
# TELEGRAM
# ════════════════════════════════════════════════════════════════════
def send_telegram(msg):
    if not TELEGRAM_BOT_TOKEN or "PASTE" in TELEGRAM_BOT_TOKEN: return False
    try:
        r=requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                        json={"chat_id":TELEGRAM_CHAT_ID,"text":msg,"parse_mode":"HTML"},timeout=10)
        return r.status_code==200
    except: return False

def fmt_tg(sig):
    e="🟢" if sig["direction"]=="BUY" else "🔴"
    bars="█"*(sig["score"]//10)+"░"*(10-sig["score"]//10)
    tbs=f"✅ {sig.get('tbs_tf','')}" if sig.get("tbs_found") else "❌"
    fvg=f"✅ {sig.get('fvg_type','')} @ {sig.get('fvg_entry','')}" if sig.get("fvg_found") else "❌"
    choch="✅" if sig.get("choch_found") else "❌"
    ob_tf=sig.get("ob_tf","–")
    return (
        f"{e} <b>CRT SIGNAL — {sig['direction']}</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"<b>Pair:</b>       {sig['symbol']}\n"
        f"<b>OB TF:</b>      {ob_tf}\n"
        f"<b>Trend:</b>      {sig['trend']}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"<b>Entry:</b>      {sig['entry']} ({sig.get('entry_type','FVG')})\n"
        f"<b>SL:</b>         {sig['sl']}\n"
        f"<b>TP:</b>         {sig['tp']}\n"
        f"<b>RR:</b>         {sig['rr']}R\n"
        f"<b>CRH:</b>        {sig['crh']}\n"
        f"<b>CRL:</b>        {sig['crl']}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"<b>Score:</b>      {sig['score']}/100 [{bars}] {sig['grade']}\n"
        f"<b>TBS:</b>        {tbs}\n"
        f"<b>FVG/IFVG:</b>   {fvg}\n"
        f"<b>CHOCH:</b>      {choch}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"<i>CRT Scanner • {sig['timestamp']}</i>"
    )

# ════════════════════════════════════════════════════════════════════
# LOGGER
# ════════════════════════════════════════════════════════════════════
def log(msg):
    ts=datetime.now(timezone.utc).strftime("%H:%M:%S")
    line=f"[{ts}] {msg}"; print(line)
    with scan_lock: scan_state["log"].appendleft(line)

# ════════════════════════════════════════════════════════════════════
# MAIN SCAN LOGIC
# ════════════════════════════════════════════════════════════════════
def scan_pair(symbol):
    results=[]
    # Fetch 4H candles for CRT (main TF)
    candles_4h=get_candles(symbol,"Hour4",limit=200)
    if not candles_4h or len(candles_4h)<30: return results

    # Detect trend and structure
    trend, sh, sl = detect_trend(candles_4h)
    if trend=="NEUTRAL": return results   # skip neutral markets

    # Require CONTINUOUS structure (strict)
    continuous=is_continuous_structure(sh, sl, trend, min_points=3)
    if not continuous: return results

    direction="BUY" if trend=="BULLISH" else "SELL"

    # Find OBs on multiple timeframes
    for ob_tf_name in OB_TIMEFRAMES:
        ob_candles=get_candles(symbol, ob_tf_name, limit=150)
        if not ob_candles or len(ob_candles)<20: continue

        obs=find_orderblocks(ob_candles, direction, sh, sl)
        if not obs: continue

        # Check if previous OBs were respected
        ob_respected=previous_obs_respected(obs, ob_candles, direction, min_respected=1)

        # Work with the most recent valid OB
        for ob in obs[:3]:   # check top 3 most recent OBs
            # Must be at the last key swing level
            at_key=ob_at_key_level(ob, direction, sh, sl)
            if not at_key: continue

            # Must have liquidity swept before OB
            liq_swept=liquidity_sweep_before_ob(ob_candles, ob, direction)
            if not liq_swept: continue

            # Price must be tapping the OB right now
            tapping=price_tapping_ob(candles_4h, ob, direction)
            if not tapping: continue

            # Now look for 4H CRT forming FROM this OB
            crts=detect_crt_from_ob(candles_4h, ob, direction)
            if not crts: continue

            for crt in crts:
                # TBS on 1H/30m/15m
                tbs_found,tbs_tf,tbs_strength=check_tbs(symbol, direction, crt["crl"], crt["crh"])

                # CHOCH on 15m (or 5m for 4H CRT)
                choch_found,choch_level=check_choch(symbol,"Min15",direction)

                # FVG/IFVG on 15m
                fvg_found,fvg_type,fvg_entry,fvg_top,fvg_bot=find_fvg(symbol,"Min15",direction)

                # Entry priority: FVG tip > CHOCH level > C2 close
                if fvg_found and fvg_entry:
                    entry=fvg_entry
                    entry_type=fvg_type
                elif choch_found and choch_level:
                    entry=choch_level
                    entry_type="CHOCH"
                else:
                    entry=crt["entry"]
                    entry_type="C2 Close"

                sl_price=crt["sl"]; tp_price=crt["tp"]
                risk=abs(entry-sl_price); reward=abs(tp_price-entry)
                rr=round(reward/risk,2) if risk>0 else 0
                if rr<3.0: continue    # strict 3R minimum

                crt_s=dict(crt); crt_s["entry"]=entry; crt_s["rr"]=rr
                score,grade,details=score_signal(
                    crt_s, trend, ob, liq_swept, ob_respected, at_key,
                    tbs_found, tbs_strength, fvg_found, fvg_type, choch_found, continuous
                )

                sig={
                    "symbol":    symbol,
                    "tf":        "Hour4",
                    "ob_tf":     ob_tf_name,
                    "direction": direction,
                    "trend":     trend,
                    "entry":     round(entry,8),
                    "entry_type":entry_type,
                    "sl":        round(sl_price,8),
                    "tp":        round(tp_price,8),
                    "rr":        rr,
                    "crh":       crt["crh"],
                    "crl":       crt["crl"],
                    "ob_top":    ob["top"],
                    "ob_bot":    ob["bot"],
                    "score":     score,
                    "grade":     grade,
                    "details":   details,
                    "tbs_found":    tbs_found,
                    "tbs_tf":       tbs_tf or "–",
                    "tbs_strength": tbs_strength or "–",
                    "fvg_found":    fvg_found,
                    "fvg_type":     fvg_type or "–",
                    "fvg_entry":    fvg_entry or "–",
                    "fvg_top":      fvg_top or "–",
                    "fvg_bot":      fvg_bot or "–",
                    "choch_found":  choch_found,
                    "choch_level":  choch_level or "–",
                    "liq_swept":    liq_swept,
                    "ob_respected": ob_respected,
                    "continuous":   continuous,
                    "timestamp":    datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                }
                results.append(sig)
                break  # one signal per OB TF is enough
            if results: break  # one valid signal per pair per scan
    return results

# ════════════════════════════════════════════════════════════════════
# SCANNER LOOP
# ════════════════════════════════════════════════════════════════════
def scanner_loop():
    with scan_lock: scan_state["running"]=True
    log("🚀 CRT Scanner started — fetching all MEXC perpetual pairs...")
    while True:
        try:
            with scan_lock:
                if not scan_state["enabled"]:
                    log("⏸ Scanner paused — waiting...")
                    scan_state["running"]=False
            if not scan_state["enabled"]:
                time.sleep(5); continue
            with scan_lock: scan_state["running"]=True

            pairs=get_all_pairs()
            if not pairs:
                log("⚠️ Could not fetch pairs — retrying in 30s")
                time.sleep(30); continue

            with scan_lock:
                scan_state["total_pairs"]=len(pairs)
                scan_state["pairs_done"]=0
                scan_state["scan_count"]+=1

            log(f"🔄 Scan #{scan_state['scan_count']} — {len(pairs)} pairs")

            for i,symbol in enumerate(pairs):
                if not scan_state["enabled"]: break
                with scan_lock:
                    scan_state["current_pair"]=symbol
                    scan_state["pairs_done"]=i+1
                try:
                    res=scan_pair(symbol)
                    for sig in res:
                        signals.appendleft(sig)
                        with scan_lock: scan_state["signals_found"]+=1
                        log(f"🎯 {sig['direction']} {symbol} | OB:{sig['ob_tf']} | Score:{sig['score']} {sig['grade']} | RR:{sig['rr']}R")
                        send_telegram(fmt_tg(sig))
                except: pass
                time.sleep(0.5)

            with scan_lock: scan_state["last_scan"]=datetime.now(timezone.utc).strftime("%H:%M UTC")
            log(f"✅ Scan #{scan_state['scan_count']} done. Restarting...")
        except Exception as e:
            log(f"❌ Loop error: {e}"); time.sleep(15)

# ════════════════════════════════════════════════════════════════════
# HTML PAGES
# ════════════════════════════════════════════════════════════════════
LOGIN_HTML=r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/><meta name="viewport" content="width=device-width,initial-scale=1.0"/>
<title>CRT Scanner</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Nunito:wght@400;700;800;900&display=swap');
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Nunito',sans-serif;background:#0a0f1e;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px;overflow:hidden}
.bg{position:fixed;inset:0;z-index:0;overflow:hidden}
.star{position:absolute;background:#fff;border-radius:50%;animation:twinkle 3s infinite}
@keyframes twinkle{0%,100%{opacity:.2;transform:scale(1)}50%{opacity:1;transform:scale(1.3)}}
.planet{position:absolute;border-radius:50%;animation:orbit 20s linear infinite}
.p1{width:120px;height:120px;background:radial-gradient(circle at 35% 35%,#ff6b6b,#c0392b);top:-30px;right:10%;box-shadow:0 0 40px rgba(255,107,107,.5);animation-duration:25s}
.p2{width:70px;height:70px;background:radial-gradient(circle at 35% 35%,#f9ca24,#f0932b);bottom:5%;left:5%;box-shadow:0 0 25px rgba(249,202,36,.4);animation-duration:18s;animation-direction:reverse}
.p3{width:50px;height:50px;background:radial-gradient(circle at 35% 35%,#6c5ce7,#a29bfe);top:30%;left:-10px;box-shadow:0 0 20px rgba(108,92,231,.4);animation-duration:30s}
@keyframes orbit{0%{transform:translateY(0) rotate(0)}100%{transform:translateY(-20px) rotate(360deg)}}
.card{background:rgba(13,20,40,.9);border:2px solid rgba(99,179,237,.3);border-radius:28px;padding:44px 36px;width:100%;max-width:420px;position:relative;z-index:10;backdrop-filter:blur(20px);box-shadow:0 0 60px rgba(99,179,237,.15),0 32px 80px rgba(0,0,0,.7)}
.logo{text-align:center;margin-bottom:32px}
.logo-icon{font-size:4rem;display:block;margin-bottom:10px;animation:bounce 2s ease-in-out infinite}
@keyframes bounce{0%,100%{transform:translateY(0)}50%{transform:translateY(-10px)}}
.logo-title{font-size:1.9rem;font-weight:900;background:linear-gradient(135deg,#63b3ed,#9f7aea,#fc8181);-webkit-background-clip:text;-webkit-text-fill-color:transparent;letter-spacing:-.02em}
.logo-sub{font-size:.82rem;color:#718096;margin-top:5px;letter-spacing:.08em;text-transform:uppercase}
.label{font-size:.78rem;font-weight:800;color:#718096;letter-spacing:.1em;text-transform:uppercase;display:block;margin-bottom:8px}
.input{width:100%;padding:14px 18px;background:rgba(255,255,255,.05);border:2px solid rgba(99,179,237,.2);border-radius:14px;color:#e2e8f0;font-size:1rem;font-family:'Nunito',sans-serif;font-weight:700;outline:none;transition:all .2s;margin-bottom:20px}
.input:focus{border-color:#63b3ed;background:rgba(99,179,237,.08);box-shadow:0 0 0 4px rgba(99,179,237,.15)}
.input::placeholder{color:#4a5568}
.btn{width:100%;padding:15px;background:linear-gradient(135deg,#667eea,#764ba2);color:#fff;border:none;border-radius:14px;font-size:1.05rem;font-weight:900;font-family:'Nunito',sans-serif;cursor:pointer;transition:all .2s;letter-spacing:.02em;position:relative;overflow:hidden}
.btn::after{content:'';position:absolute;inset:0;background:linear-gradient(135deg,rgba(255,255,255,.15),transparent);opacity:0;transition:opacity .2s}
.btn:hover::after{opacity:1}
.btn:hover{transform:translateY(-3px);box-shadow:0 12px 30px rgba(102,126,234,.5)}
.err{background:rgba(252,129,129,.1);border:2px solid rgba(252,129,129,.3);border-radius:12px;padding:12px;font-size:.84rem;color:#fc8181;margin-bottom:16px;text-align:center;display:none;font-weight:700}
.err.show{display:block}
.badges{display:flex;gap:8px;margin-top:24px;flex-wrap:wrap;justify-content:center}
.badge{background:rgba(99,179,237,.1);border:1.5px solid rgba(99,179,237,.2);border-radius:20px;padding:5px 12px;font-size:.7rem;color:#63b3ed;font-weight:800}
</style>
</head>
<body>
<div class="bg">
  <div class="planet p1"></div><div class="planet p2"></div><div class="planet p3"></div>
  <script>
    for(let i=0;i<60;i++){
      const s=document.createElement('div');
      s.className='star';
      const sz=Math.random()*3+1;
      s.style.cssText=`width:${sz}px;height:${sz}px;top:${Math.random()*100}%;left:${Math.random()*100}%;animation-delay:${Math.random()*3}s;animation-duration:${2+Math.random()*3}s`;
      document.querySelector('.bg').appendChild(s);
    }
  </script>
</div>
<div class="card">
  <div class="logo">
    <span class="logo-icon">🚀</span>
    <div class="logo-title">CRT Scanner</div>
    <div class="logo-sub">MEXC Perpetual Futures</div>
  </div>
  <div class="err" id="err"></div>
  <label class="label">Password</label>
  <input class="input" type="password" id="pw" placeholder="Enter password" autofocus/>
  <button class="btn" id="btn" onclick="login()">🔓 Launch Dashboard</button>
  <div class="badges">
    <span class="badge">🎯 CRT Strategy</span>
    <span class="badge">📦 Order Blocks</span>
    <span class="badge">💧 Liq Sweep</span>
    <span class="badge">⚡ FVG Entry</span>
    <span class="badge">🔄 Auto Scan</span>
  </div>
</div>
<script>
function login(){
  const pw=document.getElementById('pw').value.trim();
  const err=document.getElementById('err');
  const btn=document.getElementById('btn');
  if(!pw){err.textContent='Please enter your password 🔑';err.classList.add('show');return;}
  btn.textContent='🚀 Launching...';btn.disabled=true;err.classList.remove('show');
  fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:pw})})
    .then(r=>r.json()).then(d=>{
      if(d.ok){btn.textContent='✅ Loading!';setTimeout(()=>window.location.href='/dashboard',300);}
      else{err.textContent='❌ Wrong password! Try again.';err.classList.add('show');btn.textContent='🔓 Launch Dashboard';btn.disabled=false;document.getElementById('pw').value='';document.getElementById('pw').focus();}
    }).catch(()=>{btn.textContent='🔓 Launch Dashboard';btn.disabled=false;});
}
document.getElementById('pw').addEventListener('keydown',e=>{if(e.key==='Enter')login();});
</script>
</body>
</html>"""

DASHBOARD_HTML=r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/><meta name="viewport" content="width=device-width,initial-scale=1.0"/>
<title>CRT Scanner 🚀</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Nunito:wght@400;600;700;800;900&family=JetBrains+Mono:wght@400;700&display=swap');
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#070d1a;--s1:#0d1528;--s2:#111d35;--s3:#172040;
  --border:#1e2d50;--border2:#2a4070;
  --blue:#63b3ed;--purple:#9f7aea;--green:#68d391;--red:#fc8181;
  --yellow:#fbd38d;--pink:#f687b3;--cyan:#76e4f7;--orange:#f6ad55;
  --text:#e2e8f0;--dim:#a0aec0;--muted:#4a5568;
}
body{font-family:'Nunito',sans-serif;background:var(--bg);color:var(--text);min-height:100vh;padding-bottom:80px}

/* ── HEADER ── */
.hdr{background:rgba(13,21,40,.97);border-bottom:2px solid var(--border);position:sticky;top:0;z-index:200;backdrop-filter:blur(20px)}
.hdr-in{max-width:1260px;margin:0 auto;padding:0 20px;height:62px;display:flex;align-items:center;justify-content:space-between;gap:12px}
.brand{display:flex;align-items:center;gap:10px}
.brand-rocket{font-size:1.6rem;animation:rock 3s ease-in-out infinite}
@keyframes rock{0%,100%{transform:rotate(-5deg)}50%{transform:rotate(5deg)}}
.brand-name{font-size:1.1rem;font-weight:900;background:linear-gradient(135deg,#63b3ed,#9f7aea);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.pulse-row{display:flex;align-items:center;gap:8px}
.pulse{width:10px;height:10px;border-radius:50%;background:var(--green);box-shadow:0 0 10px var(--green);animation:pp 2s infinite}
.pulse.off{background:var(--red);box-shadow:0 0 10px var(--red);animation:none}
@keyframes pp{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.4;transform:scale(.7)}}
.pulse-txt{font-size:.75rem;color:var(--green);font-weight:800}
.pulse-txt.off{color:var(--red)}
.hdr-right{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.scan-chip{background:rgba(99,179,237,.12);border:1.5px solid rgba(99,179,237,.3);border-radius:20px;padding:5px 12px;font-size:.72rem;font-weight:800;color:var(--blue);font-family:'JetBrains Mono',monospace}

/* ── TOGGLE BUTTON ── */
.toggle-btn{padding:8px 18px;border:2px solid;border-radius:20px;font-family:'Nunito',sans-serif;font-size:.8rem;font-weight:900;cursor:pointer;transition:all .25s;letter-spacing:.02em}
.toggle-btn.on{background:rgba(252,129,129,.12);border-color:var(--red);color:var(--red)}
.toggle-btn.on:hover{background:rgba(252,129,129,.25);transform:scale(1.05)}
.toggle-btn.off{background:rgba(104,211,145,.12);border-color:var(--green);color:var(--green)}
.toggle-btn.off:hover{background:rgba(104,211,145,.25);transform:scale(1.05)}
.btn-logout{padding:7px 14px;background:transparent;border:1.5px solid var(--border);border-radius:12px;color:var(--muted);font-size:.75rem;font-weight:800;cursor:pointer;font-family:'Nunito',sans-serif;transition:all .2s}
.btn-logout:hover{border-color:var(--red);color:var(--red)}

/* ── PROGRESS ── */
.prog-wrap{background:var(--s1);border-bottom:1px solid var(--border);padding:8px 20px}
.prog-inner{max-width:1260px;margin:0 auto;display:flex;align-items:center;gap:14px}
.prog-track{flex:1;height:6px;background:var(--s3);border-radius:3px;overflow:hidden}
.prog-fill{height:100%;background:linear-gradient(90deg,#63b3ed,#9f7aea,#f687b3);border-radius:3px;transition:width .5s ease;animation:shimmer 2s infinite}
@keyframes shimmer{0%{filter:brightness(1)}50%{filter:brightness(1.3)}100%{filter:brightness(1)}}
.prog-txt{font-size:.7rem;color:var(--muted);font-family:'JetBrains Mono',monospace;white-space:nowrap}
.cur-pair{font-size:.7rem;color:var(--blue);font-family:'JetBrains Mono',monospace;white-space:nowrap;max-width:180px;overflow:hidden;text-overflow:ellipsis}

/* ── LIVE PRICES ── */
.prices-section{max-width:1260px;margin:20px auto 0;padding:0 20px}
.prices-title{font-size:.72rem;font-weight:800;color:var(--muted);text-transform:uppercase;letter-spacing:.1em;margin-bottom:10px}
.prices-grid{display:grid;grid-template-columns:repeat(6,1fr);gap:10px}
.price-card{background:var(--s1);border:2px solid var(--border);border-radius:16px;padding:14px 12px;text-align:center;transition:all .3s;position:relative;overflow:hidden}
.price-card::before{content:'';position:absolute;top:0;left:0;right:0;height:3px;border-radius:3px 3px 0 0}
.price-card.up::before{background:linear-gradient(90deg,var(--green),var(--cyan))}
.price-card.down::before{background:linear-gradient(90deg,var(--red),var(--pink))}
.price-card:hover{transform:translateY(-4px);border-color:var(--border2);box-shadow:0 12px 30px rgba(0,0,0,.4)}
.pc-sym{font-size:.72rem;font-weight:900;color:var(--muted);letter-spacing:.06em;margin-bottom:4px}
.pc-price{font-size:.92rem;font-weight:900;font-family:'JetBrains Mono',monospace;margin-bottom:4px}
.pc-change{font-size:.72rem;font-weight:800;padding:2px 8px;border-radius:20px;display:inline-block}
.pc-change.up{background:rgba(104,211,145,.15);color:var(--green)}
.pc-change.down{background:rgba(252,129,129,.15);color:var(--red)}

/* ── STATS ── */
.stats{max-width:1260px;margin:16px auto 0;padding:0 20px;display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));gap:10px}
.stat{background:var(--s1);border:2px solid var(--border);border-radius:16px;padding:14px 16px;position:relative;overflow:hidden;transition:all .2s}
.stat:hover{transform:translateY(-3px);border-color:var(--border2)}
.stat::after{content:'';position:absolute;top:0;left:0;right:0;height:3px;border-radius:3px 3px 0 0}
.st-total::after{background:linear-gradient(90deg,var(--blue),var(--purple))}
.st-buy::after{background:var(--green)}
.st-sell::after{background:var(--red)}
.st-scans::after{background:var(--yellow)}
.st-pairs::after{background:var(--pink)}
.stat-lbl{font-size:.62rem;color:var(--muted);text-transform:uppercase;letter-spacing:.08em;font-weight:800;margin-bottom:6px}
.stat-val{font-size:1.5rem;font-weight:900;font-family:'JetBrains Mono',monospace}
.stat-sub{font-size:.65rem;color:var(--muted);margin-top:4px;font-weight:700}

/* ── TABS ── */
.tabs-wrap{max-width:1260px;margin:20px auto 0;padding:0 20px}
.tabs{display:flex;gap:4px;background:var(--s1);border:2px solid var(--border);border-radius:16px;padding:4px;margin-bottom:18px;overflow-x:auto}
.tab{flex:1;min-width:85px;padding:9px 8px;border:none;border-radius:12px;font-family:'Nunito',sans-serif;font-size:.78rem;font-weight:900;cursor:pointer;transition:all .2s;color:var(--muted);background:transparent;white-space:nowrap;text-align:center}
.tab.active{background:linear-gradient(135deg,var(--blue),var(--purple));color:#fff;box-shadow:0 4px 16px rgba(99,179,237,.4)}

/* ── FILTERS ── */
.filter-row{display:flex;align-items:center;justify-content:space-between;margin-bottom:14px;flex-wrap:wrap;gap:8px}
.sec-t{font-size:.95rem;font-weight:900}
.filters{display:flex;gap:7px;flex-wrap:wrap;align-items:center}
.fsel{background:var(--s2);border:1.5px solid var(--border);border-radius:10px;color:var(--text);padding:7px 10px;font-size:.74rem;font-family:'Nunito',sans-serif;font-weight:700;outline:none;transition:border-color .2s}
.fsel:focus{border-color:var(--blue)}

/* ── SIGNAL CARDS ── */
.sig-list{display:flex;flex-direction:column;gap:12px}
.empty-state{display:flex;flex-direction:column;align-items:center;justify-content:center;padding:80px 20px;background:var(--s1);border:2px dashed var(--border);border-radius:20px;text-align:center;gap:12px}
.empty-ico{font-size:4rem;animation:bounce 2s ease-in-out infinite}
@keyframes bounce{0%,100%{transform:translateY(0)}50%{transform:translateY(-12px)}}
.empty-t{font-size:1.1rem;font-weight:900}
.empty-s{font-size:.84rem;color:var(--muted);max-width:380px;line-height:1.6;font-weight:700}

.sig-card{background:var(--s1);border:2px solid var(--border);border-radius:18px;padding:18px 20px;animation:cfi .35s cubic-bezier(.34,1.56,.64,1);transition:all .25s}
.sig-card:hover{transform:translateY(-4px);box-shadow:0 16px 40px rgba(0,0,0,.5);border-color:var(--border2)}
.sig-card.buy{border-left:4px solid var(--green);background:linear-gradient(135deg,rgba(104,211,145,.04),var(--s1))}
.sig-card.sell{border-left:4px solid var(--red);background:linear-gradient(135deg,rgba(252,129,129,.04),var(--s1))}
@keyframes cfi{from{opacity:0;transform:translateY(-14px) scale(.97)}to{opacity:1;transform:translateY(0) scale(1)}}

.sig-top{display:flex;align-items:center;gap:9px;flex-wrap:wrap;margin-bottom:14px}
.dir-badge{font-size:.72rem;font-weight:900;padding:5px 13px;border-radius:20px;letter-spacing:.05em}
.dir-badge.BUY{background:rgba(104,211,145,.18);color:var(--green);border:1.5px solid rgba(104,211,145,.4)}
.dir-badge.SELL{background:rgba(252,129,129,.18);color:var(--red);border:1.5px solid rgba(252,129,129,.4)}
.sym-name{font-size:1rem;font-weight:900;letter-spacing:.03em}
.tf-chip{font-size:.68rem;color:var(--cyan);background:rgba(118,228,247,.1);border:1.5px solid rgba(118,228,247,.25);padding:3px 9px;border-radius:20px;font-weight:800}
.ob-chip{font-size:.65rem;color:var(--orange);background:rgba(246,173,85,.1);border:1.5px solid rgba(246,173,85,.25);padding:3px 9px;border-radius:20px;font-weight:800}
.trend-chip{font-size:.66px;font-weight:900;padding:3px 9px;border-radius:20px;font-size:.66rem}
.trend-chip.BULLISH{background:rgba(104,211,145,.12);color:var(--green);border:1.5px solid rgba(104,211,145,.25)}
.trend-chip.BEARISH{background:rgba(252,129,129,.12);color:var(--red);border:1.5px solid rgba(252,129,129,.25)}
.grade-badge{font-size:.76rem;font-weight:900;padding:4px 11px;border-radius:10px;font-family:'JetBrains Mono',monospace}
.gAp{background:rgba(104,211,145,.2);color:var(--green)}.gA{background:rgba(99,179,237,.2);color:var(--blue)}
.gB{background:rgba(251,211,141,.2);color:var(--yellow)}.gC{background:rgba(252,129,129,.18);color:var(--red)}
.gD{background:rgba(74,85,104,.2);color:var(--muted)}
.sig-ts{font-size:.65rem;color:var(--muted);margin-left:auto;font-weight:700}

.sig-levels{display:grid;grid-template-columns:repeat(auto-fit,minmax(115px,1fr));gap:8px;margin-bottom:12px}
.level{background:var(--s2);border:1.5px solid var(--border);border-radius:12px;padding:10px 13px;transition:border-color .2s}
.level:hover{border-color:var(--border2)}
.level-lbl{font-size:.6rem;color:var(--muted);text-transform:uppercase;letter-spacing:.07em;margin-bottom:4px;font-weight:800}
.level-val{font-size:.82rem;font-weight:800;font-family:'JetBrains Mono',monospace}
.lv-entry{color:var(--blue)}.lv-sl{color:var(--red)}.lv-tp{color:var(--green)}.lv-rr{color:var(--yellow)}.lv-fvg{color:var(--orange)}

.sig-pills{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px}
.pill{font-size:.67rem;font-weight:800;padding:4px 10px;border-radius:20px}
.pill-ok{background:rgba(104,211,145,.12);color:var(--green);border:1.5px solid rgba(104,211,145,.25)}
.pill-no{background:rgba(74,85,104,.08);color:var(--muted);border:1.5px solid var(--border)}
.pill-warn{background:rgba(251,211,141,.1);color:var(--yellow);border:1.5px solid rgba(251,211,141,.25)}

.score-row{display:flex;align-items:center;gap:10px}
.score-lbl{font-size:.68rem;color:var(--muted);white-space:nowrap;width:72px;font-weight:800}
.score-track{flex:1;height:8px;background:var(--s3);border-radius:4px;overflow:hidden}
.score-fill{height:100%;border-radius:4px;transition:width .7s ease}
.score-num{font-size:.73rem;font-weight:900;font-family:'JetBrains Mono',monospace;white-space:nowrap;width:58px;text-align:right}

.det-toggle{font-size:.71rem;color:var(--blue);cursor:pointer;margin-top:9px;display:inline-block;font-weight:800;transition:color .2s}
.det-toggle:hover{color:var(--purple)}
.det-box{display:none;margin-top:10px;background:var(--s2);border:1.5px solid var(--border);border-radius:12px;padding:12px;font-size:.73rem;color:var(--dim);line-height:1.9;font-weight:700}
.det-box.open{display:block}

/* ── LOG ── */
.log-wrap{background:var(--s1);border:2px solid var(--border);border-radius:18px;overflow:hidden}
.log-hdr{padding:14px 18px;border-bottom:1.5px solid var(--border);display:flex;align-items:center;justify-content:space-between}
.log-ttl{font-size:.88rem;font-weight:900}
.log-body{padding:14px 18px;max-height:480px;overflow-y:auto;font-family:'JetBrains Mono',monospace;font-size:.72rem;line-height:1.9;color:var(--dim)}
.ll-sig{color:var(--green)}.ll-err{color:var(--red)}.ll-inf{color:var(--blue)}

/* ── TOAST ── */
.toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%) translateY(100px);background:var(--s2);border:2px solid var(--border2);border-radius:14px;padding:13px 24px;font-size:.85rem;font-weight:800;box-shadow:0 20px 50px rgba(0,0,0,.6);opacity:0;transition:all .35s cubic-bezier(.34,1.56,.64,1);pointer-events:none;z-index:9999;white-space:nowrap}
.toast.show{transform:translateX(-50%) translateY(0);opacity:1}

/* ── PAUSED BANNER ── */
.paused-banner{background:rgba(252,129,129,.1);border-bottom:2px solid rgba(252,129,129,.3);padding:10px 20px;text-align:center;font-size:.82rem;font-weight:900;color:var(--red);display:none}
.paused-banner.show{display:block}

@media(max-width:700px){
  .stats{grid-template-columns:1fr 1fr}
  .prices-grid{grid-template-columns:repeat(3,1fr)}
  .hdr-in{padding:0 12px}.tabs-wrap{padding:0 12px}
  .prices-section{padding:0 12px}.stats{padding:0 12px}
  .sig-levels{grid-template-columns:1fr 1fr}
  .scan-chip{display:none}
}
</style>
</head>
<body>

<div class="paused-banner" id="paused-banner">⏸ Scanner is PAUSED — tap Resume to restart scanning</div>

<div class="hdr">
  <div class="hdr-in">
    <div class="brand">
      <span class="brand-rocket">🚀</span>
      <span class="brand-name">CRT Scanner</span>
    </div>
    <div class="pulse-row">
      <div class="pulse" id="pulse-dot"></div>
      <span class="pulse-txt" id="pulse-txt">Scanning...</span>
    </div>
    <div class="hdr-right">
      <div class="scan-chip" id="scan-chip">Scan #0</div>
      <button class="toggle-btn on" id="toggle-btn" onclick="toggleScanner()">⏹ Stop Bot</button>
      <button class="btn-logout" onclick="logout()">Logout</button>
    </div>
  </div>
</div>

<div class="prog-wrap">
  <div class="prog-inner">
    <span class="cur-pair" id="cur-pair">Initialising...</span>
    <div class="prog-track"><div class="prog-fill" id="prog-fill" style="width:0%"></div></div>
    <span class="prog-txt" id="prog-txt">0 / 0</span>
  </div>
</div>

<!-- LIVE PRICES -->
<div class="prices-section">
  <div class="prices-title">📈 Top 6 Live Prices</div>
  <div class="prices-grid" id="prices-grid">
    <div class="price-card"><div class="pc-sym">Loading...</div></div>
    <div class="price-card"><div class="pc-sym">Loading...</div></div>
    <div class="price-card"><div class="pc-sym">Loading...</div></div>
    <div class="price-card"><div class="pc-sym">Loading...</div></div>
    <div class="price-card"><div class="pc-sym">Loading...</div></div>
    <div class="price-card"><div class="pc-sym">Loading...</div></div>
  </div>
</div>

<!-- STATS -->
<div class="stats">
  <div class="stat st-total"><div class="stat-lbl">Total Signals</div><div class="stat-val" id="s-total">0</div><div class="stat-sub">All time</div></div>
  <div class="stat st-buy"><div class="stat-lbl">🟢 BUY</div><div class="stat-val" id="s-buy">0</div></div>
  <div class="stat st-sell"><div class="stat-lbl">🔴 SELL</div><div class="stat-val" id="s-sell">0</div></div>
  <div class="stat st-scans"><div class="stat-lbl">Scans Done</div><div class="stat-val" id="s-scans">0</div><div class="stat-sub" id="s-last">–</div></div>
  <div class="stat st-pairs"><div class="stat-lbl">Pairs Done</div><div class="stat-val" id="s-pairs">0</div><div class="stat-sub">This scan</div></div>
</div>

<!-- TABS -->
<div class="tabs-wrap">
  <div class="tabs">
    <button class="tab active" onclick="sw('signals',this)">📊 Signals</button>
    <button class="tab" onclick="sw('log',this)">🖥 Live Log</button>
  </div>

  <!-- SIGNALS -->
  <div id="tab-signals">
    <div class="filter-row">
      <div class="sec-t">🎯 CRT Signals</div>
      <div class="filters">
        <select class="fsel" id="fd" onchange="renderSigs()">
          <option value="">All Directions</option>
          <option value="BUY">🟢 BUY</option>
          <option value="SELL">🔴 SELL</option>
        </select>
        <select class="fsel" id="fg" onchange="renderSigs()">
          <option value="">All Grades</option>
          <option value="A+">A+ Only</option>
          <option value="A">A Only</option>
          <option value="B">B+</option>
        </select>
        <select class="fsel" id="fob" onchange="renderSigs()">
          <option value="">All OB TFs</option>
          <option value="Hour4">4H OB</option>
          <option value="Hour2">2H OB</option>
          <option value="Min60">1H OB</option>
          <option value="Min45">45m OB</option>
        </select>
      </div>
    </div>
    <div class="sig-list" id="sig-list">
      <div class="empty-state">
        <div class="empty-ico">🔭</div>
        <div class="empty-t">Scanning the markets...</div>
        <div class="empty-s">Looking for high-quality CRT setups with OB confluence, liquidity sweeps and FVG entries. Only 3R+ signals shown.</div>
      </div>
    </div>
  </div>

  <!-- LOG -->
  <div id="tab-log" style="display:none">
    <div class="log-wrap">
      <div class="log-hdr"><span class="log-ttl">🖥 Live Scanner Log</span><span style="font-size:.7rem;color:#4a5568;font-weight:700">Updates every 3s</span></div>
      <div class="log-body" id="log-body">Loading...</div>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
(function(){
'use strict';
let allSigs=[],toastT,activeTab='signals';
const $=id=>document.getElementById(id);

function toast(m,d=3200){
  const t=$('toast');t.textContent=m;t.classList.add('show');
  clearTimeout(toastT);toastT=setTimeout(()=>t.classList.remove('show'),d);
}

function scoreColor(s){
  if(s>=85)return'#68d391';if(s>=72)return'#63b3ed';
  if(s>=58)return'#fbd38d';return'#fc8181';
}

function fmt(v){
  if(v===null||v===undefined||v==='–'||v===false)return'–';
  const n=Number(v);if(isNaN(n))return String(v);
  if(n>=10000)return n.toLocaleString(undefined,{maximumFractionDigits:2});
  if(n>=1)return n.toFixed(4);
  return n.toFixed(6);
}

function fmtPrice(v){
  const n=Number(v);if(isNaN(n))return'–';
  if(n>=10000)return'$'+n.toLocaleString(undefined,{maximumFractionDigits:2});
  if(n>=1)return'$'+n.toFixed(4);
  return'$'+n.toFixed(6);
}

// Live prices
const TOP=['BTC_USDT','ETH_USDT','SOL_USDT','BNB_USDT','XRP_USDT','DOGE_USDT'];
async function fetchPrices(){
  try{
    const r=await fetch('/api/prices');
    const data=await r.json();
    const grid=$('prices-grid');
    grid.innerHTML=TOP.map(sym=>{
      const d=data[sym];
      if(!d)return`<div class="price-card"><div class="pc-sym">${sym.replace('_USDT','')}</div><div class="pc-price" style="color:#4a5568">–</div></div>`;
      const up=d.change>=0;
      const name=sym.replace('_USDT','');
      return`<div class="price-card ${up?'up':'down'}">
        <div class="pc-sym">${name}/USDT</div>
        <div class="pc-price" style="color:${up?'#68d391':'#fc8181'}">${fmtPrice(d.price)}</div>
        <span class="pc-change ${up?'up':'down'}">${up?'▲':'▼'} ${Math.abs(d.change).toFixed(2)}%</span>
      </div>`;
    }).join('');
  }catch{}
}

function buildCard(s,idx){
  const dir=s.direction||'BUY';
  const score=s.score||0;
  const grade=s.grade||'–';
  const gc={'A+':'gAp','A':'gA','B':'gB','C':'gC','D':'gD'}[grade]||'gD';
  const tfLabel={'Hour4':'4H','Hour2':'2H','Min60':'1H','Min30':'30m','Min15':'15m','Min5':'5m'}[s.ob_tf]||s.ob_tf||'–';

  const tbs=s.tbs_found?`<span class="pill pill-ok">✅ TBS ${s.tbs_tf||''}</span>`:`<span class="pill pill-no">❌ TBS</span>`;
  const fvg=s.fvg_found?`<span class="pill pill-ok">✅ ${s.fvg_type||'FVG'}</span>`:`<span class="pill pill-no">❌ FVG</span>`;
  const choch=s.choch_found?`<span class="pill pill-ok">✅ CHOCH</span>`:`<span class="pill pill-no">❌ CHOCH</span>`;
  const liq=s.liq_swept?`<span class="pill pill-ok">💧 Liq Swept</span>`:`<span class="pill pill-warn">⚠️ No Sweep</span>`;
  const obr=s.ob_respected?`<span class="pill pill-ok">📦 OB Respected</span>`:`<span class="pill pill-warn">⚠️ OB Unconfirmed</span>`;
  const cont=s.continuous?`<span class="pill pill-ok">📈 Continuous Structure</span>`:`<span class="pill pill-warn">⚠️ Weak Structure</span>`;

  const details=(s.details||[]).join('<br>');

  return`<div class="sig-card ${dir.toLowerCase()}">
    <div class="sig-top">
      <span class="dir-badge ${dir}">${dir}</span>
      <span class="sym-name">${s.symbol||'–'}</span>
      <span class="tf-chip">4H CRT</span>
      <span class="ob-chip">OB: ${tfLabel}</span>
      <span class="trend-chip ${s.trend}">${s.trend}</span>
      <span class="grade-badge ${gc}">${grade}</span>
      <span class="sig-ts">${s.timestamp||''}</span>
    </div>
    <div class="sig-levels">
      <div class="level"><div class="level-lbl">Entry (${s.entry_type||'FVG'})</div><div class="level-val lv-entry">${fmt(s.entry)}</div></div>
      <div class="level"><div class="level-lbl">Stop Loss</div><div class="level-val lv-sl">${fmt(s.sl)}</div></div>
      <div class="level"><div class="level-lbl">Take Profit</div><div class="level-val lv-tp">${fmt(s.tp)}</div></div>
      <div class="level"><div class="level-lbl">Risk:Reward</div><div class="level-val lv-rr">${s.rr}R</div></div>
      <div class="level"><div class="level-lbl">FVG Top</div><div class="level-val lv-fvg">${fmt(s.fvg_top)}</div></div>
      <div class="level"><div class="level-lbl">FVG Bot</div><div class="level-val lv-fvg">${fmt(s.fvg_bot)}</div></div>
      <div class="level"><div class="level-lbl">CRH</div><div class="level-val">${fmt(s.crh)}</div></div>
      <div class="level"><div class="level-lbl">CRL</div><div class="level-val">${fmt(s.crl)}</div></div>
      <div class="level"><div class="level-lbl">OB Top</div><div class="level-val" style="color:#f6ad55">${fmt(s.ob_top)}</div></div>
      <div class="level"><div class="level-lbl">OB Bot</div><div class="level-val" style="color:#f6ad55">${fmt(s.ob_bot)}</div></div>
    </div>
    <div class="sig-pills">${tbs}${fvg}${choch}${liq}${obr}${cont}</div>
    <div class="score-row">
      <span class="score-lbl">Score</span>
      <div class="score-track"><div class="score-fill" style="width:${score}%;background:${scoreColor(score)}"></div></div>
      <span class="score-num" style="color:${scoreColor(score)}">${score}/100</span>
    </div>
    ${details?`<span class="det-toggle" onclick="toggleDet(${idx})">▼ Score breakdown</span>
    <div class="det-box" id="det-${idx}">${details}</div>`:''}
  </div>`;
}

window.toggleDet=function(i){const b=$('det-'+i);if(b)b.classList.toggle('open');};

window.renderSigs=function(){
  const dirF=$('fd').value, grF=$('fg').value, obF=$('fob').value;
  let f=allSigs.filter(s=>{
    if(dirF&&s.direction!==dirF)return false;
    if(obF&&s.ob_tf!==obF)return false;
    if(grF){
      if(grF==='A+'&&s.grade!=='A+')return false;
      if(grF==='A'&&s.grade!=='A')return false;
      if(grF==='B'&&!['A+','A','B'].includes(s.grade))return false;
    }
    return true;
  });
  const list=$('sig-list');
  if(!f.length){
    list.innerHTML='<div class="empty-state"><div class="empty-ico">🔭</div><div class="empty-t">Scanning markets...</div><div class="empty-s">High-quality CRT setups with OB confluence, liquidity sweeps and FVG entries. Only 3R+ signals shown.</div></div>';
    return;
  }
  list.innerHTML=f.slice(0,100).map((s,i)=>buildCard(s,i)).join('');
};

let lastCount=0;
async function fetchSigs(){
  try{
    const r=await fetch('/api/signals?limit=200');
    const data=await r.json();
    allSigs=data;
    if(data.length>lastCount&&lastCount>0){
      const n=data[0];
      toast(`🎯 ${n.direction} ${n.symbol} | OB:${n.ob_tf} | ${n.score}/100 ${n.grade} | ${n.rr}R`);
    }
    lastCount=data.length;
    renderSigs();
  }catch{}
}

async function fetchStats(){
  try{
    const r=await fetch('/api/stats');const d=await r.json();
    $('s-total').textContent=d.total||0;
    $('s-buy').textContent=d.buys||0;
    $('s-sell').textContent=d.sells||0;
  }catch{}
}

async function fetchState(){
  try{
    const r=await fetch('/api/scan-state');const d=await r.json();
    const pct=d.total_pairs>0?Math.round(d.pairs_done/d.total_pairs*100):0;
    $('prog-fill').style.width=pct+'%';
    $('prog-txt').textContent=`${d.pairs_done}/${d.total_pairs}`;
    $('cur-pair').textContent=d.current_pair?`Scanning: ${d.current_pair}`:'Waiting...';
    $('s-scans').textContent=d.scan_count||0;
    $('s-pairs').textContent=d.pairs_done||0;
    $('s-last').textContent=d.last_scan?`Last: ${d.last_scan}`:'–';
    $('scan-chip').textContent=`Scan #${d.scan_count||0}`;

    // Update toggle button and pulse
    const enabled=d.enabled!==false;
    const btn=$('toggle-btn');
    const dot=$('pulse-dot');
    const ptxt=$('pulse-txt');
    const banner=$('paused-banner');

    if(enabled){
      btn.textContent='⏹ Stop Bot';btn.className='toggle-btn on';
      dot.className='pulse';ptxt.textContent='Scanning...';ptxt.className='pulse-txt';
      banner.classList.remove('show');
    }else{
      btn.textContent='▶ Resume Bot';btn.className='toggle-btn off';
      dot.className='pulse off';ptxt.textContent='Paused';ptxt.className='pulse-txt off';
      banner.classList.add('show');
    }
  }catch{}
}

async function fetchLog(){
  if(activeTab!=='log')return;
  try{
    const r=await fetch('/api/log');const d=await r.json();
    const body=$('log-body');
    body.innerHTML=d.log.map(l=>{
      const cls=l.includes('🎯')||l.includes('SIGNAL')?'ll-sig':l.includes('❌')||l.includes('Error')?'ll-err':'ll-inf';
      return`<div class="log-line ${cls}">${l}</div>`;
    }).join('');
  }catch{}
}

window.toggleScanner=async function(){
  try{
    const r=await fetch('/api/toggle-scanner',{method:'POST'});
    const d=await r.json();
    toast(d.enabled?'▶ Scanner resumed!':'⏸ Scanner paused!');
    await fetchState();
  }catch{toast('❌ Toggle failed');}
};

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

async function poll(){
  await Promise.all([fetchSigs(),fetchStats(),fetchState(),fetchLog(),fetchPrices()]);
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
    token=request.cookies.get("session")
    if token and token in sessions:
        return make_response(DASHBOARD_HTML,200,{"Content-Type":"text/html"})
    return make_response(LOGIN_HTML,200,{"Content-Type":"text/html"})

@app.route("/dashboard")
def dashboard():
    token=request.cookies.get("session")
    if not token or token not in sessions:
        return make_response(LOGIN_HTML,200,{"Content-Type":"text/html"})
    return make_response(DASHBOARD_HTML,200,{"Content-Type":"text/html"})

@app.route("/api/login",methods=["POST"])
def api_login():
    data=request.get_json(silent=True) or {}
    if data.get("password")==DASHBOARD_PASSWORD:
        token=secrets.token_hex(32); sessions.add(token)
        resp=make_response(jsonify({"ok":True}))
        resp.set_cookie("session",token,max_age=86400*7,httponly=True,samesite="Lax")
        return resp
    return jsonify({"ok":False}),401

@app.route("/api/logout",methods=["POST"])
def api_logout():
    token=request.cookies.get("session"); sessions.discard(token)
    resp=make_response(jsonify({"ok":True})); resp.delete_cookie("session")
    return resp

@app.route("/api/toggle-scanner",methods=["POST"])
def api_toggle():
    with scan_lock:
        scan_state["enabled"]=not scan_state["enabled"]
        enabled=scan_state["enabled"]
    log(f"{'▶ Scanner RESUMED' if enabled else '⏸ Scanner PAUSED'} by user")
    return jsonify({"enabled":enabled})

@app.route("/api/signals")
def api_signals():
    limit=min(int(request.args.get("limit",200)),MAX_SIGNALS)
    return jsonify(list(signals)[:limit])

@app.route("/api/stats")
def api_stats():
    all_s=list(signals)
    return jsonify({"total":len(all_s),
                    "buys": sum(1 for s in all_s if s.get("direction")=="BUY"),
                    "sells":sum(1 for s in all_s if s.get("direction")=="SELL")})

@app.route("/api/scan-state")
def api_scan_state():
    with scan_lock:
        return jsonify({k:v for k,v in scan_state.items() if k!="log"})

@app.route("/api/log")
def api_log():
    with scan_lock: return jsonify({"log":list(scan_state["log"])})

@app.route("/api/prices")
def api_prices():
    out={}
    for sym in TOP_PAIRS:
        t=get_ticker(sym)
        if t: out[sym]=t
    return jsonify(out)

@app.route("/health")
def health():
    return jsonify({"status":"healthy","signals":len(signals),"scanning":scan_state["running"]}),200

# ════════════════════════════════════════════════════════════════════
# STARTUP
# ════════════════════════════════════════════════════════════════════
def start_scanner():
    t=threading.Thread(target=scanner_loop,daemon=True); t.start()
    log("Scanner thread launched.")

with app.app_context():
    start_scanner()

if __name__=="__main__":
    port=int(os.environ.get("PORT",5000))
    app.run(host="0.0.0.0",port=port,debug=False)
