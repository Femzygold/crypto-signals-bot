import os, json, time, secrets, requests, threading
from datetime import datetime, timezone
from flask import Flask, request, jsonify, make_response
from collections import deque

app = Flask(__name__)

TELEGRAM_BOT_TOKEN = "8668028976:AAE2u1in1KGr1nRTJbaQXNPeDtMO35unoQ8"
TELEGRAM_CHAT_ID   = "7411219487"
DASHBOARD_PASSWORD = "signal123"

MAX_SIGNALS = 500
signals     = deque(maxlen=MAX_SIGNALS)
sessions    = set()

scan_state = {
    "running": False, "enabled": True, "current_pair": "",
    "pairs_done": 0, "total_pairs": 0, "scan_count": 0,
    "signals_found": 0, "last_scan": None,
    "log": deque(maxlen=100),
}
scan_lock = threading.Lock()

TOP_PAIRS = ["BTC_USDT","ETH_USDT","SOL_USDT","BNB_USDT","XRP_USDT","DOGE_USDT"]
MEXC_BASE = "https://contract.mexc.com/api/v1/contract"
CRT_TFS   = ["Day1","Hour4","Hour3","Hour2","Min60"]
OB_TFS    = ["Hour4","Hour3","Hour2","Min60","Min45"]
TBS_TFS   = ["Min30","Min15","Min10","Min5"]

# ════════ MEXC API ═══════════════════════════════════════════════════

def get_all_pairs():
    try:
        r = requests.get(f"{MEXC_BASE}/detail", timeout=15)
        data = r.json()
        if not data.get("success"): return []
        pairs = []
        for item in data.get("data", []):
            sym = item.get("symbol","")
            if item.get("state") == 0 and sym.endswith("_USDT"):
                pairs.append(sym)
        return sorted(pairs)
    except Exception as e:
        log(f"Pairs error: {e}"); return []

def get_candles(symbol, interval, limit=150):
    try:
        r = requests.get(f"{MEXC_BASE}/kline/{symbol}",
                         params={"interval":interval,"limit":limit}, timeout=10)
        data = r.json()
        if not data.get("success") or not data.get("data"): return []
        raw = data["data"]
        out = []
        times=raw.get("time",[]); opens=raw.get("open",[])
        highs=raw.get("high",[]); lows=raw.get("low",[]); closes=raw.get("close",[])
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
            return {"price":float(d.get("lastPrice",0)),"change":float(d.get("priceChangePercent",0)),
                    "high":float(d.get("high24h",0)),"low":float(d.get("low24h",0))}
    except: pass
    return None

# ════════ MARKET STRUCTURE ═══════════════════════════════════════════

def find_swings(candles, n=2):
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
    if len(c)<20: return "NEUTRAL",[],[]
    sh,sl = find_swings(c,n=2)
    if len(sh)>=2 and len(sl)>=2:
        hh=sh[-1][1]>sh[-2][1]; hl=sl[-1][1]>sl[-2][1]
        lh=sh[-1][1]<sh[-2][1]; ll=sl[-1][1]<sl[-2][1]
        if hh and hl: return "BULLISH",sh,sl
        if lh and ll: return "BEARISH",sh,sl
    closes=[c["close"] for c in c[-20:]]
    a1=sum(closes[:10])/10; a2=sum(closes[10:])/10
    if a2>a1*1.003: return "BULLISH",sh,sl
    if a2<a1*0.997: return "BEARISH",sh,sl
    return "NEUTRAL",sh,sl

def is_continuous(sh, sl, direction, min_pts=2):
    if direction=="BULLISH":
        if len(sh)<min_pts or len(sl)<min_pts: return False
        return all(sh[i][1]>sh[i-1][1] for i in range(1,len(sh))) and \
               all(sl[i][1]>sl[i-1][1] for i in range(1,len(sl)))
    else:
        if len(sh)<min_pts or len(sl)<min_pts: return False
        return all(sh[i][1]<sh[i-1][1] for i in range(1,len(sh))) and \
               all(sl[i][1]<sl[i-1][1] for i in range(1,len(sl)))

# ════════ ORDER BLOCKS ════════════════════════════════════════════════

def find_obs(candles, direction):
    obs = []
    if len(candles)<5: return obs
    for i in range(2, len(candles)-2):
        c=candles[i]; cn=candles[i+1]
        if direction=="BULLISH":
            if c["close"]<c["open"] and cn["close"]>c["high"] and cn["close"]>cn["open"]:
                obs.append({"top":c["open"],"bot":c["close"],"high":c["high"],"low":c["low"],"idx":i,"time":c["time"],"type":"BULLISH_OB"})
        else:
            if c["close"]>c["open"] and cn["close"]<c["low"] and cn["close"]<cn["open"]:
                obs.append({"top":c["close"],"bot":c["open"],"high":c["high"],"low":c["low"],"idx":i,"time":c["time"],"type":"BEARISH_OB"})
    return sorted(obs, key=lambda x:x["idx"], reverse=True)

def ob_at_key_level(ob, direction, sh, sl, tol=0.012):
    """Check OB is near last Higher Low (bull) or last Lower High (bear). Widened to 0.8%."""
    if direction=="BULLISH" and sl:
        last_hl = sl[-1][1]
        return ob["bot"] <= last_hl*(1+tol) and ob["top"] >= last_hl*(1-tol)
    elif direction=="BEARISH" and sh:
        last_lh = sh[-1][1]
        return ob["top"] >= last_lh*(1-tol) and ob["bot"] <= last_lh*(1+tol)
    return False

def ob_in_pd_zone(ob, candles, direction):
    """OB must be in Discount zone (bull) or Premium zone (bear)."""
    if not candles or len(candles)<20: return False,"UNKNOWN"
    recent = candles[-50:]
    swing_high = max(c["high"] for c in recent)
    swing_low  = min(c["low"]  for c in recent)
    full_range = swing_high - swing_low
    if full_range<=0: return False,"UNKNOWN"
    eq = swing_low + full_range*0.5
    ob_mid = (ob["top"]+ob["bot"])/2
    if direction=="BULLISH":
        return ob_mid<eq, ("DISCOUNT" if ob_mid<eq else "PREMIUM")
    else:
        return ob_mid>eq, ("PREMIUM" if ob_mid>eq else "DISCOUNT")

def prev_obs_respected(obs, candles, direction, min_resp=1):
    """Check at least 1 previous OB got respected (price reacted from it)."""
    if len(obs)<2: return False
    respected=0
    for ob in obs[1:]:
        after = candles[ob["idx"]+1 : ob["idx"]+10]
        if not after: continue
        if direction=="BULLISH":
            tap    = any(c["low"]<=ob["top"] for c in after[:4])
            react  = any(c["close"]>ob["top"]*1.002 for c in after)
            if tap and react: respected+=1
        else:
            tap    = any(c["high"]>=ob["bot"] for c in after[:4])
            react  = any(c["close"]<ob["bot"]*0.998 for c in after)
            if tap and react: respected+=1
    return respected>=min_resp

def liq_sweep_before_ob(candles, ob, direction):
    """Liquidity sweep (wick OR body) before OB forms."""
    idx = ob["idx"]
    lb  = candles[max(0,idx-20):idx]
    if not lb: return False
    if direction=="BULLISH":
        prev_low = min(c["low"] for c in lb[:-1]) if len(lb)>1 else lb[0]["low"]
        return any(c["low"]<prev_low for c in lb[-8:])
    else:
        prev_high = max(c["high"] for c in lb[:-1]) if len(lb)>1 else lb[0]["high"]
        return any(c["high"]>prev_high for c in lb[-8:])

def price_tapping_ob(candles, ob, direction):
    """Price tapping OB in the last 14 candles."""
    recent = candles[-14:]
    if direction=="BULLISH":
        return any(c["low"]<=ob["top"] and c["high"]>=ob["bot"] for c in recent)
    else:
        return any(c["high"]>=ob["bot"] and c["low"]<=ob["top"] for c in recent)

# ════════ CRT DETECTION ══════════════════════════════════════════════

def detect_crt(candles, direction, ob=None):
    """
    Detect 3-candle CRT formation.
    If ob is provided, C1 must be rooted inside the OB zone.
    Returns list of valid CRT dicts with RR >= 3.
    """
    found = []
    if len(candles)<5: return found
    limit = min(20, len(candles)-2)
    for offset in range(1, limit):
        i3=len(candles)-1-offset; i2=i3-1; i1=i2-1
        if i1<0: break
        c1=candles[i1]; c2=candles[i2]; c3=candles[i3]
        crh=c1["high"]; crl=c1["low"]; cr_range=crh-crl
        if cr_range<=0: continue
        # If OB given, C1 must overlap OB
        if ob:
            if not (c1["low"]<=ob["top"] and c1["high"]>=ob["bot"]):
                continue
        if direction=="BULLISH":
            swept     = c2["low"] < crl
            c2_inside = crl <= c2["close"] <= crh
            wick_ok   = (c2["close"]-c2["low"]) > cr_range*0.03
            c3_bull   = c3["close"] > c3["open"]
            if swept and c2_inside and wick_ok:
                entry=c2["close"]; sl=c2["low"]; tp=crh
                risk=abs(entry-sl); reward=abs(tp-entry)
                rr=round(reward/risk,2) if risk>0 else 0
                if rr>=3.0:
                    found.append({"direction":"BUY","c1":c1,"c2":c2,"c3":c3,
                                  "crh":crh,"crl":crl,"entry":round(entry,8),
                                  "sl":round(sl,8),"tp":round(tp,8),"rr":rr,
                                  "sweep":round(crl-c2["low"],8),"c3_confirms":c3_bull})
        else:
            swept     = c2["high"] > crh
            c2_inside = crl <= c2["close"] <= crh
            wick_ok   = (c2["high"]-c2["close"]) > cr_range*0.03
            c3_bear   = c3["close"] < c3["open"]
            if swept and c2_inside and wick_ok:
                entry=c2["close"]; sl=c2["high"]; tp=crl
                risk=abs(sl-entry); reward=abs(entry-tp)
                rr=round(reward/risk,2) if risk>0 else 0
                if rr>=3.0:
                    found.append({"direction":"SELL","c1":c1,"c2":c2,"c3":c3,
                                  "crh":crh,"crl":crl,"entry":round(entry,8),
                                  "sl":round(sl,8),"tp":round(tp,8),"rr":rr,
                                  "sweep":round(c2["high"]-crh,8),"c3_confirms":c3_bear})
    return found

# ════════ TBS ════════════════════════════════════════════════════════

def check_tbs(symbol, direction, crl, crh):
    """
    Turtle Body Soup — strict body close only.
    Checks: Min30 → Min15 → Min10 → Min5
    Bullish: candle body CLOSES below CRL, next candle closes back above CRL.
    Bearish: candle body CLOSES above CRH, next candle closes back below CRH.
    Wicks do NOT count.
    """
    for tf in TBS_TFS:
        candles = get_candles(symbol, tf, limit=100)
        if not candles or len(candles)<4: continue
        recent = candles[-60:]
        for i in range(len(recent)-1):
            c   = recent[i]
            nxt = recent[i+1]
            if direction=="BUY":
                if c["close"] < crl and nxt["close"] > crl:
                    return True, tf
            else:
                if c["close"] > crh and nxt["close"] < crh:
                    return True, tf
    return False, None

# ════════ CHOCH ═══════════════════════════════════════════════════════

def check_choch(symbol, tf, direction):
    """Change of Character — structural break on LTF."""
    candles = get_candles(symbol, tf, limit=60)
    if not candles or len(candles)<5: return False, None
    recent = candles[-35:]
    sh=[]; sl=[]
    for i in range(2, len(recent)-2):
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
            return False,None
        last_idx,last_val = sh[-1]
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
            return False,None
        last_idx,last_val = sl[-1]
        for i in range(last_idx+1,len(recent)):
            c=recent[i]
            if c["close"]<last_val and c["close"]<c["open"]:
                return True, round(last_val,8)
    return False,None

# ════════ FVG + IFVG ══════════════════════════════════════════════════

def find_fvg(symbol, tf, direction):
    """
    Entry at the TIP of the FVG/IFVG.
    Bullish FVG tip = C1.high (bottom of gap, first touch entering from below).
    Bearish FVG tip = C1.low  (top of gap, first touch entering from above).
    IFVG = previously filled FVG, now flipped S/R.
    """
    candles = get_candles(symbol, tf, limit=80)
    if not candles or len(candles)<5: return False,None,None,None,None
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

# ════════ MULTI-TF HELPERS ════════════════════════════════════════════

def check_choch_multi(symbol, tfs, direction):
    """Try CHOCH across multiple LTFs, return first found."""
    for tf in tfs:
        found, level = check_choch(symbol, tf, direction)
        if found and level:
            return True, level
    return False, None

def find_fvg_multi(symbol, tfs, direction):
    """Try FVG/IFVG across multiple LTFs, return best (most recent)."""
    for tf in tfs:
        found, fvg_type, fvg_entry, fvg_top, fvg_bot = find_fvg(symbol, tf, direction)
        if found and fvg_entry:
            return found, fvg_type, fvg_entry, fvg_top, fvg_bot
    return False, None, None, None, None

# ════════ SIGNAL SCORING ═════════════════════════════════════════════

def score_signal(crt, trend, liq_swept, tbs_found, tbs_tf,
                 fvg_found, fvg_type, choch_found, continuous,
                 is_1d, ob=None, at_key=False, ob_resp=False, ob_zone=None):
    score=0; details=[]
    direction=crt["direction"]; rr=crt["rr"]

    # 1. Continuous structure (20 pts)
    if continuous:
        score+=20; details.append("✅ Continuous market structure (+20)")
    else:
        details.append("⚠️ Weak structure (+0)")

    # 2. Trend alignment (10 pts)
    if (direction=="BUY" and trend=="BULLISH") or (direction=="SELL" and trend=="BEARISH"):
        score+=10; details.append("✅ Trend aligned (+10)")
    else:
        details.append("❌ Counter-trend (+0)")

    # 3. TBS — mandatory gatekeeper (20 pts)
    if tbs_found:
        score+=20; details.append(f"✅ TBS body close on {tbs_tf} (+20)")
    else:
        details.append("❌ No TBS — gate failed (+0)")

    # 4. Liquidity sweep (15 pts)
    if liq_swept:
        score+=15; details.append("✅ Liquidity sweep confirmed (+15)")
    else:
        details.append("⚠️ No liquidity sweep (+0)")

    # 5. RR quality (10 pts)
    if rr>=5:   score+=10; details.append(f"✅ Exceptional {rr}R (+10)")
    elif rr>=4: score+=8;  details.append(f"✅ Strong {rr}R (+8)")
    elif rr>=3: score+=6;  details.append(f"⚠️ Minimum {rr}R (+6)")

    # 6. CHOCH (10 pts)
    if choch_found:
        score+=10; details.append("✅ CHOCH confirmed (+10)")
    else:
        details.append("⚠️ No CHOCH (+0)")

    # 7. FVG/IFVG (10 pts)
    if fvg_found:
        score+=10; details.append(f"✅ {fvg_type} entry tip found (+10)")
    else:
        details.append("⚠️ No FVG/IFVG (+0)")

    if not is_1d:
        # 8. OB at key level (5 pts bonus)
        if at_key:
            score+=5; details.append("✅ OB at key swing level (+5)")
        # 9. OB in premium/discount (5 pts bonus)
        if ob_zone in ("DISCOUNT","PREMIUM"):
            score+=5; details.append(f"✅ OB in {ob_zone} zone (+5)")
        # 10. Previous OBs respected (5 pts bonus)
        if ob_resp:
            score+=5; details.append("✅ Previous OBs respected (+5)")

    # Triple confluence bonus: TBS + FVG + CHOCH all confirmed (8 pts)
    if tbs_found and fvg_found and choch_found:
        score=min(score+8,100); details.append("✅ Triple confluence: TBS+FVG+CHOCH (+8)")

    # C3 confirms direction (5 pts bonus)
    if crt.get("c3_confirms"):
        score=min(score+5,100); details.append("✅ C3 confirms (+5)")

    grade="A+" if score>=88 else "A" if score>=75 else "B" if score>=60 else "C" if score>=45 else "D"
    return min(score,100), grade, details

# ════════ TELEGRAM ════════════════════════════════════════════════════

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
    tbs=f"✅ {sig.get('tbs_tf','–')}" if sig.get("tbs_found") else "❌"
    fvg=f"✅ {sig.get('fvg_type','–')} @ {sig.get('fvg_entry','–')}" if sig.get("fvg_found") else "❌"
    choch="✅" if sig.get("choch_found") else "⚠️"
    tf_label={"Day1":"1D","Hour4":"4H","Hour3":"3H","Hour2":"2H","Min60":"1H"}.get(sig.get("tf",""),"–")
    ob_info=f"\n<b>OB TF:</b>      {sig.get('ob_tf','–')} | {sig.get('ob_zone','–')}" if sig.get("ob_tf") and sig.get("ob_tf")!="N/A" else ""
    return (
        f"{e} <b>CRT SIGNAL — {sig['direction']}</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"<b>Pair:</b>       {sig['symbol']}\n"
        f"<b>CRT TF:</b>     {tf_label}{ob_info}\n"
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

# ════════ LOGGER ══════════════════════════════════════════════════════

def log(msg):
    ts=datetime.now(timezone.utc).strftime("%H:%M:%S")
    line=f"[{ts}] {msg}"; print(line)
    with scan_lock: scan_state["log"].appendleft(line)

# ════════ SCAN PAIR ═══════════════════════════════════════════════════

def get_ltf_for_crt(crt_tf):
    """Return the best LTF for CHOCH and FVG based on CRT timeframe."""
    return {
        "Day1":  "Min60",
        "Hour4": "Min15",
        "Hour3": "Min15",
        "Hour2": "Min10",
        "Min60": "Min5",
    }.get(crt_tf, "Min15")

def scan_pair(symbol):
    results = []

    # Fetch 4H candles for trend (common reference)
    ref_candles = get_candles(symbol, "Hour4", limit=200)
    if not ref_candles or len(ref_candles)<30: return results

    trend, sh, sl = detect_trend(ref_candles)
    if trend=="NEUTRAL": return results

    continuous = is_continuous(sh, sl, trend, min_pts=2)
    if not continuous: return results

    direction = "BUY" if trend=="BULLISH" else "SELL"

    # ── PATH A: 1D CRT (no OB required) ─────────────────────────────
    candles_1d = get_candles(symbol, "Day1", limit=120)
    if candles_1d and len(candles_1d)>=10:
        crts = detect_crt(candles_1d, direction, ob=None)
        for crt in crts:
            # Liq sweep check on 1D
            fake_ob = {"idx": len(candles_1d)-3, "top": crt["crh"], "bot": crt["crl"]}
            liq = liq_sweep_before_ob(candles_1d, fake_ob, direction)

            # TBS on LTFs
            tbs_found, tbs_tf = check_tbs(symbol, direction, crt["crl"], crt["crh"])
            if not tbs_found: continue  # MANDATORY

            ltf = get_ltf_for_crt("Day1")
            choch_found, choch_level = check_choch_multi(symbol, ["Min60","Min30"], direction)
            fvg_found, fvg_type, fvg_entry, fvg_top, fvg_bot = find_fvg_multi(symbol, ["Min60","Min30"], direction)

            if fvg_found and fvg_entry:
                entry=fvg_entry; entry_type=fvg_type
            elif choch_found and choch_level:
                entry=choch_level; entry_type="CHOCH"
            else:
                entry=crt["entry"]; entry_type="C2 Close"

            sl_p=crt["sl"]; tp_p=crt["tp"]
            risk=abs(entry-sl_p); reward=abs(tp_p-entry)
            rr=round(reward/risk,2) if risk>0 else 0
            if rr<3.0: continue

            crt_s=dict(crt); crt_s["entry"]=entry; crt_s["rr"]=rr
            score,grade,details=score_signal(
                crt_s,trend,liq,tbs_found,tbs_tf,
                fvg_found,fvg_type,choch_found,continuous,is_1d=True)

            results.append({
                "symbol":symbol,"tf":"Day1","ob_tf":"N/A","ob_zone":"–",
                "direction":direction,"trend":trend,
                "entry":round(entry,8),"entry_type":entry_type,
                "sl":round(sl_p,8),"tp":round(tp_p,8),"rr":rr,
                "crh":crt["crh"],"crl":crt["crl"],
                "ob_top":"–","ob_bot":"–",
                "score":score,"grade":grade,"details":details,
                "tbs_found":tbs_found,"tbs_tf":tbs_tf or "–",
                "fvg_found":fvg_found,"fvg_type":fvg_type or "–",
                "fvg_entry":fvg_entry or "–","fvg_top":fvg_top or "–","fvg_bot":fvg_bot or "–",
                "choch_found":choch_found,"choch_level":choch_level or "–",
                "liq_swept":liq,"ob_respected":False,"continuous":continuous,
                "timestamp":datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            })
            break  # one 1D signal per pair

    if results: return results

    # ── PATH B: 4H/3H/2H/1H CRT inside OB ───────────────────────────
    for crt_tf in ["Hour4","Hour3","Hour2","Min60"]:
        if results: break

        crt_candles = get_candles(symbol, crt_tf, limit=200)
        if not crt_candles or len(crt_candles)<20: continue

        for ob_tf in OB_TFS:
            if results: break
            ob_candles = get_candles(symbol, ob_tf, limit=150)
            if not ob_candles or len(ob_candles)<20: continue

            obs = find_obs(ob_candles, direction)
            if not obs: continue

            ob_resp = prev_obs_respected(obs, ob_candles, direction, min_resp=1)

            for ob in obs[:4]:
                at_key = ob_at_key_level(ob, direction, sh, sl)
                if not at_key: continue

                in_zone, zone_name = ob_in_pd_zone(ob, ob_candles, direction)
                if not in_zone: continue

                liq = liq_sweep_before_ob(ob_candles, ob, direction)
                if not liq: continue

                tapping = price_tapping_ob(crt_candles, ob, direction)
                if not tapping: continue

                crts = detect_crt(crt_candles, direction, ob=ob)
                if not crts: continue

                for crt in crts:
                    # TBS — MANDATORY
                    tbs_found, tbs_tf = check_tbs(symbol, direction, crt["crl"], crt["crh"])
                    if not tbs_found: continue

                    ltf = get_ltf_for_crt(crt_tf)
                    fallback_ltfs = {"Hour4":["Min15","Min10"],"Hour3":["Min15","Min10"],"Hour2":["Min10","Min5"],"Min60":["Min5"]}.get(crt_tf,[ltf])
                    choch_found, choch_level = check_choch_multi(symbol, fallback_ltfs, direction)
                    fvg_ltfs = {"Hour4":["Min15","Min10"],"Hour3":["Min15","Min10"],"Hour2":["Min10","Min5"],"Min60":["Min5"]}.get(crt_tf,[ltf])
                    fvg_found, fvg_type, fvg_entry, fvg_top, fvg_bot = find_fvg_multi(symbol, fvg_ltfs, direction)

                    if fvg_found and fvg_entry:
                        entry=fvg_entry; entry_type=fvg_type
                    elif choch_found and choch_level:
                        entry=choch_level; entry_type="CHOCH"
                    else:
                        entry=crt["entry"]; entry_type="C2 Close"

                    sl_p=crt["sl"]; tp_p=crt["tp"]
                    risk=abs(entry-sl_p); reward=abs(tp_p-entry)
                    rr=round(reward/risk,2) if risk>0 else 0
                    if rr<3.0: continue

                    crt_s=dict(crt); crt_s["entry"]=entry; crt_s["rr"]=rr
                    score,grade,details=score_signal(
                        crt_s,trend,liq,tbs_found,tbs_tf,
                        fvg_found,fvg_type,choch_found,continuous,
                        is_1d=False,ob=ob,at_key=at_key,ob_resp=ob_resp,ob_zone=zone_name)

                    results.append({
                        "symbol":symbol,"tf":crt_tf,"ob_tf":ob_tf,"ob_zone":zone_name,
                        "direction":direction,"trend":trend,
                        "entry":round(entry,8),"entry_type":entry_type,
                        "sl":round(sl_p,8),"tp":round(tp_p,8),"rr":rr,
                        "crh":crt["crh"],"crl":crt["crl"],
                        "ob_top":ob["top"],"ob_bot":ob["bot"],
                        "score":score,"grade":grade,"details":details,
                        "tbs_found":tbs_found,"tbs_tf":tbs_tf or "–",
                        "fvg_found":fvg_found,"fvg_type":fvg_type or "–",
                        "fvg_entry":fvg_entry or "–","fvg_top":fvg_top or "–","fvg_bot":fvg_bot or "–",
                        "choch_found":choch_found,"choch_level":choch_level or "–",
                        "liq_swept":liq,"ob_respected":ob_resp,"continuous":continuous,
                        "timestamp":datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                    })
                    break
                if results: break
    return results

# ════════ SCANNER LOOP ════════════════════════════════════════════════

def scanner_loop():
    with scan_lock: scan_state["running"]=True
    log("🚀 CRT Scanner started — USDT perpetual pairs only")
    while True:
        try:
            with scan_lock:
                if not scan_state["enabled"]:
                    scan_state["running"]=False
            if not scan_state["enabled"]:
                time.sleep(5); continue
            with scan_lock: scan_state["running"]=True

            pairs = get_all_pairs()
            if not pairs:
                log("⚠️ No pairs fetched — retrying in 30s")
                time.sleep(30); continue

            with scan_lock:
                scan_state["total_pairs"]=len(pairs)
                scan_state["pairs_done"]=0
                scan_state["scan_count"]+=1

            log(f"🔄 Scan #{scan_state['scan_count']} — {len(pairs)} USDT pairs")

            for i,symbol in enumerate(pairs):
                if not scan_state["enabled"]: break
                with scan_lock:
                    scan_state["current_pair"]=symbol
                    scan_state["pairs_done"]=i+1
                try:
                    res = scan_pair(symbol)
                    for sig in res:
                        # Deduplication: skip if same symbol+direction+tf seen in last 50 signals
                        recent_sigs = list(signals)[:50]
                        duplicate = any(
                            s.get("symbol")==sig["symbol"] and
                            s.get("direction")==sig["direction"] and
                            s.get("tf")==sig["tf"]
                            for s in recent_sigs
                        )
                        if duplicate:
                            log(f"⏭ SKIP duplicate: {sig['direction']} {symbol} {sig['tf']}")
                            continue
                        signals.appendleft(sig)
                        with scan_lock: scan_state["signals_found"]+=1
                        tf_lbl={"Day1":"1D","Hour4":"4H","Hour3":"3H","Hour2":"2H","Min60":"1H"}.get(sig["tf"],"–")
                        log(f"🎯 {sig['direction']} {symbol} | {tf_lbl} | OB:{sig['ob_tf']} | Score:{sig['score']} {sig['grade']} | RR:{sig['rr']}R | TBS:{sig['tbs_tf']}")
                        send_telegram(fmt_tg(sig))
                except: pass
                time.sleep(0.35)
                # Log progress every 50 pairs
                if (i+1) % 50 == 0:
                    log(f"📊 Progress: {i+1}/{scan_state['total_pairs']} pairs scanned this cycle")

            with scan_lock: scan_state["last_scan"]=datetime.now(timezone.utc).strftime("%H:%M UTC")
            log(f"✅ Scan #{scan_state['scan_count']} done — {len(pairs)} pairs. Restarting...")

        except Exception as e:
            log(f"❌ Scanner error: {e}"); time.sleep(15)

# ════════ HTML ════════════════════════════════════════════════════════

LOGIN_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/><meta name="viewport" content="width=device-width,initial-scale=1.0"/>
<title>CRT Scanner</title>
<link href="https://fonts.googleapis.com/css2?family=Bebas+Neue&family=DM+Sans:wght@300;400;500;600&family=JetBrains+Mono:wght@400;700&display=swap" rel="stylesheet"/>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{--ink:#07080f;--gold:#e8b84b;--gold2:#f5d07a;--green:#00e5a0;--red:#ff4d6d;--dim:#3a3d52}
body{font-family:'DM Sans',sans-serif;background:var(--ink);min-height:100vh;display:flex;align-items:center;justify-content:center;overflow:hidden;padding:20px}
.grid-bg{position:fixed;inset:0;background-image:linear-gradient(rgba(232,184,75,.04) 1px,transparent 1px),linear-gradient(90deg,rgba(232,184,75,.04) 1px,transparent 1px);background-size:44px 44px;z-index:0}
.grid-bg::after{content:'';position:fixed;inset:0;background:radial-gradient(ellipse 80% 70% at 50% 50%,transparent 20%,var(--ink) 100%)}
.ticker{position:fixed;top:0;left:0;right:0;height:30px;background:rgba(232,184,75,.05);border-bottom:1px solid rgba(232,184,75,.1);display:flex;align-items:center;overflow:hidden;z-index:5}
.ti{display:flex;gap:36px;animation:tick 35s linear infinite;white-space:nowrap;padding-left:100%}
@keyframes tick{0%{transform:translateX(0)}100%{transform:translateX(-50%)}}
.ti span{font-family:'JetBrains Mono',monospace;font-size:.62rem;color:rgba(232,184,75,.5);letter-spacing:.05em}
.ti .up{color:#00e5a0}.ti .dn{color:#ff4d6d}
.wrap{position:relative;z-index:10;width:100%;max-width:430px;margin-top:30px}
.corner{position:absolute;width:18px;height:18px;border-color:var(--gold);border-style:solid;opacity:.5}
.tl{top:-6px;left:-6px;border-width:2px 0 0 2px}.tr{top:-6px;right:-6px;border-width:2px 2px 0 0}
.bl{bottom:-6px;left:-6px;border-width:0 0 2px 2px}.br{bottom:-6px;right:-6px;border-width:0 2px 2px 0}
.card{background:rgba(11,13,23,.97);border:1px solid rgba(232,184,75,.16);border-radius:3px;padding:42px 38px 34px;backdrop-filter:blur(24px);box-shadow:0 0 60px rgba(232,184,75,.05),0 40px 80px rgba(0,0,0,.8),inset 0 1px 0 rgba(232,184,75,.07)}
.head{text-align:center;margin-bottom:34px}
.logo-ring{width:68px;height:68px;border-radius:50%;background:radial-gradient(circle at 35% 30%,rgba(232,184,75,.22),rgba(232,184,75,.03));border:1px solid rgba(232,184,75,.28);display:flex;align-items:center;justify-content:center;margin:0 auto 14px;font-size:1.7rem;position:relative;animation:rp 4s ease-in-out infinite}
@keyframes rp{0%,100%{box-shadow:0 0 0 0 rgba(232,184,75,.2)}50%{box-shadow:0 0 0 12px rgba(232,184,75,0)}}
.logo-ring::before{content:'';position:absolute;inset:-5px;border-radius:50%;border:1px dashed rgba(232,184,75,.18);animation:sp 20s linear infinite}
@keyframes sp{to{transform:rotate(360deg)}}
.title{font-family:'Bebas Neue',sans-serif;font-size:2.5rem;letter-spacing:.12em;background:linear-gradient(135deg,var(--gold2),var(--gold),#b8861e);-webkit-background-clip:text;-webkit-text-fill-color:transparent;line-height:1;margin-bottom:5px}
.sub{font-size:.68rem;color:rgba(200,230,255,.3);letter-spacing:.18em;text-transform:uppercase}
.lbl{font-size:.63rem;font-weight:600;color:rgba(232,184,75,.45);letter-spacing:.14em;text-transform:uppercase;margin-bottom:7px;display:flex;align-items:center;gap:6px}
.lbl::before{content:'';width:14px;height:1px;background:rgba(232,184,75,.28)}
.inp{width:100%;padding:12px 15px;background:rgba(255,255,255,.03);border:1px solid rgba(232,184,75,.14);border-radius:2px;color:#e8f0ff;font-size:.92rem;font-family:'DM Sans',sans-serif;outline:none;transition:all .2s;margin-bottom:18px;letter-spacing:.04em}
.inp:focus{border-color:rgba(232,184,75,.42);background:rgba(232,184,75,.04);box-shadow:0 0 0 3px rgba(232,184,75,.07)}
.inp::placeholder{color:rgba(200,210,255,.18);font-size:.82rem}
.btn{width:100%;padding:13px;background:linear-gradient(135deg,#b8861e,var(--gold),var(--gold2));color:#07080f;border:none;border-radius:2px;font-family:'Bebas Neue',sans-serif;font-size:1.05rem;letter-spacing:.14em;cursor:pointer;transition:all .25s;position:relative;overflow:hidden}
.btn::before{content:'';position:absolute;top:0;left:-100%;width:100%;height:100%;background:linear-gradient(90deg,transparent,rgba(255,255,255,.18),transparent);transition:left .4s}
.btn:hover::before{left:100%}
.btn:hover{transform:translateY(-2px);box-shadow:0 8px 28px rgba(232,184,75,.32)}
.err{background:rgba(255,77,109,.07);border:1px solid rgba(255,77,109,.22);border-radius:2px;padding:9px 13px;font-size:.78rem;color:var(--red);margin-bottom:13px;display:none;letter-spacing:.02em}
.err.show{display:block}
.tags{display:flex;gap:5px;margin-top:22px;flex-wrap:wrap;justify-content:center}
.tag{background:transparent;border:1px solid rgba(200,230,255,.07);border-radius:2px;padding:3px 9px;font-size:.58rem;color:rgba(200,230,255,.25);letter-spacing:.07em;text-transform:uppercase}
.status{display:flex;align-items:center;justify-content:center;gap:7px;margin-top:16px;font-family:'JetBrains Mono',monospace;font-size:.58rem;color:rgba(232,184,75,.3);letter-spacing:.07em}
.sdot{width:4px;height:4px;border-radius:50%;background:var(--green);box-shadow:0 0 5px var(--green);animation:bl 2s infinite}
@keyframes bl{0%,100%{opacity:1}50%{opacity:.25}}
</style>
</head>
<body>
<div class="grid-bg"></div>
<div class="ticker"><div class="ti" id="ti"></div></div>
<div class="wrap">
  <div class="corner tl"></div><div class="corner tr"></div>
  <div class="corner bl"></div><div class="corner br"></div>
  <div class="card">
    <div class="head">
      <div class="logo-ring">📡</div>
      <div class="title">CRT Scanner</div>
      <div class="sub">MEXC Perpetual Futures · USDT Only</div>
    </div>
    <div class="err" id="err"></div>
    <div class="lbl">Access Code</div>
    <input class="inp" type="password" id="pw" placeholder="Enter password" autofocus/>
    <button class="btn" id="btn" onclick="login()">LAUNCH TERMINAL</button>
    <div class="tags">
      <span class="tag">CRT</span><span class="tag">Order Blocks</span>
      <span class="tag">TBS Body</span><span class="tag">FVG Entry</span>
      <span class="tag">3R+ Only</span><span class="tag">USDT Pairs</span>
    </div>
    <div class="status"><div class="sdot"></div><span>SCANNER ACTIVE · ALL MEXC USDT PERP PAIRS</span></div>
  </div>
</div>
<script>
const tp=[['BTC','65,420','▲+1.2%',1],['ETH','3,218','▼-0.4%',0],['SOL','172','▲+2.8%',1],['BNB','582','▲+0.7%',1],['XRP','0.584','▼-1.1%',0],['DOGE','0.121','▲+3.2%',1],['AVAX','35.2','▼-0.8%',0],['LINK','14.3','▲+1.5%',1],['ARB','1.12','▲+0.9%',1],['OP','2.34','▼-1.4%',0]];
const t=document.getElementById('ti');
[...tp,...tp].forEach(([s,p,c,u])=>{t.innerHTML+=`<span>${s}/USDT $${p}</span><span class="${u?'up':'dn'}">${c}</span>`;});
function login(){
  const pw=document.getElementById('pw').value.trim();
  const err=document.getElementById('err'); const btn=document.getElementById('btn');
  if(!pw){err.textContent='⚠ Access code required';err.classList.add('show');return;}
  btn.textContent='AUTHENTICATING...';btn.disabled=true;err.classList.remove('show');
  fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:pw})})
    .then(r=>r.json()).then(d=>{
      if(d.ok){btn.textContent='✓ ACCESS GRANTED';setTimeout(()=>window.location.href='/dashboard',350);}
      else{err.textContent='⛔ Invalid access code.';err.classList.add('show');btn.textContent='LAUNCH TERMINAL';btn.disabled=false;document.getElementById('pw').value='';document.getElementById('pw').focus();}
    }).catch(()=>{btn.textContent='LAUNCH TERMINAL';btn.disabled=false;});
}
document.getElementById('pw').addEventListener('keydown',e=>{if(e.key==='Enter')login();});
</script>
</body>
</html>"""

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/><meta name="viewport" content="width=device-width,initial-scale=1.0"/>
<title>CRT Scanner Terminal</title>
<link href="https://fonts.googleapis.com/css2?family=Bebas+Neue&family=DM+Sans:ital,wght@0,300;0,400;0,500;0,600;0,700;1,400&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet"/>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --ink:#07080f;--ink2:#0c0e1c;--ink3:#111428;--ink4:#171b34;
  --gold:#e8b84b;--gold2:#f5d07a;--gold3:#b8861e;
  --green:#00e5a0;--red:#ff4d6d;--blue:#4da6ff;--purple:#b06aff;--cyan:#00d4ff;--orange:#ff8c42;
  --text:#d8e0f0;--dim:#5a6080;--muted:#2a2e48;
  --border:rgba(232,184,75,.1);--border2:rgba(232,184,75,.22);
}
html{scroll-behavior:smooth}
body{font-family:'DM Sans',sans-serif;background:var(--ink);color:var(--text);min-height:100vh;padding-bottom:80px}
body::before{content:'';position:fixed;inset:0;background:repeating-linear-gradient(0deg,transparent,transparent 3px,rgba(0,0,0,.025) 3px,rgba(0,0,0,.025) 4px);pointer-events:none;z-index:999}
.gbg{position:fixed;inset:0;background-image:linear-gradient(rgba(232,184,75,.022) 1px,transparent 1px),linear-gradient(90deg,rgba(232,184,75,.022) 1px,transparent 1px);background-size:60px 60px;z-index:0;pointer-events:none}
.gbg::after{content:'';position:fixed;inset:0;background:radial-gradient(ellipse 90% 50% at 50% 0%,rgba(232,184,75,.035) 0%,transparent 65%)}

/* HEADER */
.hdr{background:rgba(7,8,15,.97);border-bottom:1px solid var(--border2);position:sticky;top:0;z-index:200;backdrop-filter:blur(20px)}
.hdr::after{content:'';position:absolute;bottom:0;left:0;right:0;height:1px;background:linear-gradient(90deg,transparent,rgba(232,184,75,.35),transparent)}
.hdr-in{max-width:1360px;margin:0 auto;padding:0 22px;height:58px;display:flex;align-items:center;justify-content:space-between;gap:14px}
.brand{display:flex;align-items:center;gap:12px}
.blogo{width:34px;height:34px;border-radius:50%;background:radial-gradient(circle at 35% 30%,rgba(232,184,75,.28),rgba(232,184,75,.05));border:1px solid rgba(232,184,75,.28);display:flex;align-items:center;justify-content:center;font-size:.95rem;animation:rp 4s ease-in-out infinite;flex-shrink:0}
@keyframes rp{0%,100%{box-shadow:0 0 0 0 rgba(232,184,75,.2)}50%{box-shadow:0 0 0 7px rgba(232,184,75,0)}}
.btext{display:flex;flex-direction:column;line-height:1.1}
.bname{font-family:'Bebas Neue',sans-serif;font-size:1.05rem;letter-spacing:.15em;background:linear-gradient(135deg,var(--gold2),var(--gold));-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.bsub{font-family:'JetBrains Mono',monospace;font-size:.52rem;color:var(--dim);letter-spacing:.08em}
.scan-status{display:flex;align-items:center;gap:7px;background:rgba(0,229,160,.05);border:1px solid rgba(0,229,160,.14);border-radius:2px;padding:5px 13px}
.sdot{width:6px;height:6px;border-radius:50%;background:var(--green);animation:sd 2s infinite;flex-shrink:0}
.sdot.off{background:var(--red);animation:none}
@keyframes sd{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.35;transform:scale(.65)}}
.stxt{font-family:'JetBrains Mono',monospace;font-size:.6rem;color:var(--green);letter-spacing:.05em;white-space:nowrap}
.stxt.off{color:var(--red)}
.hdr-right{display:flex;align-items:center;gap:7px}
.scan-num{font-family:'JetBrains Mono',monospace;font-size:.62rem;color:var(--dim);background:var(--ink3);border:1px solid var(--muted);border-radius:2px;padding:4px 9px;letter-spacing:.05em}
.tbtn{padding:7px 15px;border:1px solid;border-radius:2px;font-family:'Bebas Neue',sans-serif;font-size:.78rem;letter-spacing:.12em;cursor:pointer;transition:all .2s;white-space:nowrap}
.tbtn.on{background:rgba(255,77,109,.07);border-color:rgba(255,77,109,.32);color:var(--red)}
.tbtn.on:hover{background:rgba(255,77,109,.14);transform:translateY(-1px)}
.tbtn.off{background:rgba(0,229,160,.07);border-color:rgba(0,229,160,.28);color:var(--green)}
.tbtn.off:hover{background:rgba(0,229,160,.13);transform:translateY(-1px)}
.obtn{padding:7px 13px;background:transparent;border:1px solid var(--muted);border-radius:2px;color:var(--dim);font-family:'Bebas Neue',sans-serif;font-size:.78rem;letter-spacing:.1em;cursor:pointer;transition:all .2s}
.obtn:hover{border-color:var(--red);color:var(--red)}

/* PAUSED BANNER */
.pb{background:rgba(255,77,109,.05);border-bottom:1px solid rgba(255,77,109,.18);padding:9px;text-align:center;font-family:'JetBrains Mono',monospace;font-size:.67rem;color:var(--red);letter-spacing:.07em;display:none}
.pb.show{display:block}

/* PROGRESS */
.prog{background:rgba(10,11,20,.85);border-bottom:1px solid var(--border);padding:8px 22px;position:relative;z-index:10}
.prog-in{max-width:1360px;margin:0 auto;display:flex;align-items:center;gap:14px}
.prog-lbl{font-family:'JetBrains Mono',monospace;font-size:.6rem;color:var(--dim);letter-spacing:.04em;white-space:nowrap;min-width:200px;overflow:hidden;text-overflow:ellipsis}
.prog-track{flex:1;height:3px;background:var(--muted);border-radius:1px;overflow:hidden}
.prog-fill{height:100%;background:linear-gradient(90deg,var(--gold3),var(--gold),var(--gold2));border-radius:1px;transition:width .5s ease;box-shadow:0 0 8px rgba(232,184,75,.35)}
.prog-cnt{font-family:'JetBrains Mono',monospace;font-size:.6rem;color:var(--dim);white-space:nowrap}

/* SECTION WRAPPER */
.sec{max-width:1360px;margin:22px auto 0;padding:0 22px}
.sec-hdr{display:flex;align-items:center;gap:10px;margin-bottom:11px}
.sec-title{font-family:'Bebas Neue',sans-serif;font-size:.9rem;letter-spacing:.15em;color:rgba(232,184,75,.55);white-space:nowrap}
.sec-line{flex:1;height:1px;background:var(--border)}
.sec-note{font-family:'JetBrains Mono',monospace;font-size:.55rem;color:var(--dim);letter-spacing:.05em}

/* PRICES */
.prices-grid{display:grid;grid-template-columns:repeat(6,1fr);gap:9px}
.pc{background:var(--ink2);border:1px solid var(--border);border-radius:2px;padding:13px 13px 11px;position:relative;overflow:hidden;transition:all .22s;cursor:default}
.pc::before{content:'';position:absolute;top:0;left:0;right:0;height:2px;background:var(--muted);transition:background .3s}
.pc.up::before{background:linear-gradient(90deg,var(--green),rgba(0,229,160,.25))}
.pc.dn::before{background:linear-gradient(90deg,var(--red),rgba(255,77,109,.25))}
.pc:hover{border-color:var(--border2);transform:translateY(-3px);box-shadow:0 10px 36px rgba(0,0,0,.5)}
.pc-sym{font-family:'Bebas Neue',sans-serif;font-size:.72rem;letter-spacing:.1em;color:var(--dim);margin-bottom:5px}
.pc-price{font-family:'JetBrains Mono',monospace;font-size:.85rem;font-weight:700;margin-bottom:5px;line-height:1}
.pc-price.up{color:var(--green)}.pc-price.dn{color:var(--red)}
.pc-chg{font-family:'JetBrains Mono',monospace;font-size:.62rem;font-weight:700;padding:2px 7px;border-radius:2px;display:inline-block}
.pc-chg.up{background:rgba(0,229,160,.09);color:var(--green)}.pc-chg.dn{background:rgba(255,77,109,.09);color:var(--red)}

/* STATS */
.stats-grid{display:grid;grid-template-columns:repeat(5,1fr);gap:9px}
.sc{background:var(--ink2);border:1px solid var(--border);border-radius:2px;padding:15px 17px;position:relative;overflow:hidden;transition:all .2s}
.sc:hover{border-color:var(--border2);transform:translateY(-2px)}
.sc::after{content:'';position:absolute;bottom:0;left:0;right:0;height:2px}
.s0::after{background:linear-gradient(90deg,var(--gold),transparent)}
.s1::after{background:linear-gradient(90deg,var(--green),transparent)}
.s2::after{background:linear-gradient(90deg,var(--red),transparent)}
.s3::after{background:linear-gradient(90deg,var(--blue),transparent)}
.s4::after{background:linear-gradient(90deg,var(--purple),transparent)}
.sc-lbl{font-family:'JetBrains Mono',monospace;font-size:.52rem;color:var(--dim);letter-spacing:.09em;text-transform:uppercase;margin-bottom:7px}
.sc-val{font-family:'Bebas Neue',sans-serif;font-size:1.9rem;letter-spacing:.04em;line-height:1;color:var(--gold2)}
.sc-sub{font-size:.62rem;color:var(--dim);margin-top:4px;font-weight:300}

/* TABS */
.tab-wrap{max-width:1360px;margin:22px auto 0;padding:0 22px}
.tabs{display:flex;gap:0;border-bottom:1px solid var(--border);margin-bottom:18px}
.tab{padding:10px 20px;border:none;background:transparent;font-family:'Bebas Neue',sans-serif;font-size:.82rem;letter-spacing:.15em;cursor:pointer;color:var(--dim);transition:all .2s;border-bottom:2px solid transparent;margin-bottom:-1px}
.tab:hover{color:var(--gold)}.tab.active{color:var(--gold2);border-bottom-color:var(--gold)}

/* FILTERS */
.frow{display:flex;align-items:center;justify-content:space-between;margin-bottom:15px;flex-wrap:wrap;gap:9px}
.ftitle{font-family:'Bebas Neue',sans-serif;font-size:.98rem;letter-spacing:.15em;color:var(--gold)}
.fgrp{display:flex;gap:6px;flex-wrap:wrap}
.fsel{background:var(--ink3);border:1px solid var(--muted);border-radius:2px;color:var(--text);padding:7px 10px;font-size:.7rem;font-family:'DM Sans',sans-serif;font-weight:500;outline:none;transition:border-color .2s}
.fsel:focus{border-color:var(--gold)}

/* EMPTY */
.empty{display:flex;flex-direction:column;align-items:center;justify-content:center;padding:85px 20px;background:var(--ink2);border:1px dashed var(--muted);border-radius:2px;text-align:center;gap:12px}
.empty-ico{font-size:3rem;opacity:.4}
.empty-t{font-family:'Bebas Neue',sans-serif;font-size:1.2rem;letter-spacing:.15em;color:var(--dim)}
.empty-s{font-size:.8rem;color:var(--dim);max-width:400px;line-height:1.7;font-weight:300}

/* SIGNAL CARDS */
.sig-list{display:flex;flex-direction:column;gap:10px}
.scard{background:var(--ink2);border:1px solid var(--border);border-radius:2px;padding:18px 20px;animation:cfa .35s cubic-bezier(.16,1,.3,1);transition:all .2s;position:relative;overflow:hidden}
.scard::before{content:'';position:absolute;top:0;left:0;bottom:0;width:3px}
.scard.buy::before{background:linear-gradient(180deg,var(--green),rgba(0,229,160,.15))}
.scard.sell::before{background:linear-gradient(180deg,var(--red),rgba(255,77,109,.15))}
.scard:hover{border-color:var(--border2);transform:translateY(-3px);box-shadow:0 14px 45px rgba(0,0,0,.55)}
@keyframes cfa{from{opacity:0;transform:translateY(-10px)}to{opacity:1;transform:translateY(0)}}

.card-hdr{display:flex;align-items:center;gap:9px;flex-wrap:wrap;margin-bottom:13px;padding-bottom:11px;border-bottom:1px solid var(--border)}
.dtag{font-family:'Bebas Neue',sans-serif;font-size:.78rem;letter-spacing:.14em;padding:4px 11px;border-radius:2px;border:1px solid;flex-shrink:0}
.dtag.BUY{background:rgba(0,229,160,.07);border-color:rgba(0,229,160,.28);color:var(--green)}
.dtag.SELL{background:rgba(255,77,109,.07);border-color:rgba(255,77,109,.28);color:var(--red)}
.csym{font-family:'Bebas Neue',sans-serif;font-size:1.1rem;letter-spacing:.07em;color:var(--text)}
.chips{display:flex;gap:5px;flex-wrap:wrap;align-items:center}
.chip{font-family:'JetBrains Mono',monospace;font-size:.58rem;padding:2px 7px;border-radius:2px;letter-spacing:.04em;border:1px solid}
.chip-tf{color:var(--cyan);border-color:rgba(0,212,255,.18);background:rgba(0,212,255,.05)}
.chip-ob{color:var(--orange);border-color:rgba(255,140,66,.18);background:rgba(255,140,66,.05)}
.chip-tr.BULLISH{color:var(--green);border-color:rgba(0,229,160,.18);background:rgba(0,229,160,.05)}
.chip-tr.BEARISH{color:var(--red);border-color:rgba(255,77,109,.18);background:rgba(255,77,109,.05)}
.chip-tr.NEUTRAL{color:var(--dim);border-color:var(--muted);background:transparent}
.gtag{font-family:'Bebas Neue',sans-serif;font-size:.82rem;letter-spacing:.1em;padding:3px 9px;border-radius:2px;margin-left:auto;border:1px solid;flex-shrink:0}
.gAp{color:#00e5a0;border-color:rgba(0,229,160,.32);background:rgba(0,229,160,.07)}
.gA{color:var(--gold2);border-color:rgba(232,184,75,.32);background:rgba(232,184,75,.06)}
.gB{color:#4da6ff;border-color:rgba(77,166,255,.28);background:rgba(77,166,255,.05)}
.gC{color:var(--orange);border-color:rgba(255,140,66,.28);background:rgba(255,140,66,.05)}
.gD{color:var(--dim);border-color:var(--muted);background:transparent}
.cts{font-family:'JetBrains Mono',monospace;font-size:.56rem;color:var(--dim);white-space:nowrap}

/* LEVELS */
.lvl-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(108px,1fr));gap:7px;margin-bottom:13px}
.lv{background:var(--ink3);border:1px solid var(--muted);border-radius:2px;padding:9px 11px;transition:border-color .2s}
.lv:hover{border-color:rgba(232,184,75,.18)}
.lv-lbl{font-family:'JetBrains Mono',monospace;font-size:.52rem;color:var(--dim);letter-spacing:.07em;text-transform:uppercase;margin-bottom:4px}
.lv-val{font-family:'JetBrains Mono',monospace;font-size:.78rem;font-weight:700}
.lv-e .lv-val{color:var(--gold2)}.lv-s .lv-val{color:var(--red)}.lv-t .lv-val{color:var(--green)}
.lv-r .lv-val{color:var(--cyan)}.lv-f .lv-val{color:var(--orange)}.lv-o .lv-val{color:var(--purple)}

/* CONFIRMS */
.cfms{display:flex;gap:5px;flex-wrap:wrap;margin-bottom:11px}
.cf{font-family:'JetBrains Mono',monospace;font-size:.56rem;padding:3px 8px;border-radius:2px;letter-spacing:.04em;border:1px solid}
.cf-ok{color:var(--green);border-color:rgba(0,229,160,.2);background:rgba(0,229,160,.055)}
.cf-no{color:var(--dim);border-color:var(--muted);background:transparent}
.cf-w{color:var(--orange);border-color:rgba(255,140,66,.18);background:rgba(255,140,66,.045)}
.cf-g{color:var(--gold);border-color:rgba(232,184,75,.22);background:rgba(232,184,75,.045)}

/* SCORE */
.srow{display:flex;align-items:center;gap:11px}
.slbl{font-family:'JetBrains Mono',monospace;font-size:.56rem;color:var(--dim);letter-spacing:.05em;white-space:nowrap;width:50px}
.strack{flex:1;height:4px;background:var(--muted);border-radius:1px;overflow:hidden}
.sfill{height:100%;border-radius:1px;transition:width .85s ease}
.snum{font-family:'Bebas Neue',sans-serif;font-size:.88rem;letter-spacing:.05em;width:62px;text-align:right;white-space:nowrap}

/* DETAILS */
.dettog{display:inline-flex;align-items:center;gap:4px;margin-top:9px;font-family:'JetBrains Mono',monospace;font-size:.58rem;color:rgba(232,184,75,.35);cursor:pointer;letter-spacing:.06em;transition:color .18s;border:none;background:transparent;padding:0}
.dettog:hover{color:var(--gold)}
.detbox{display:none;margin-top:9px;background:var(--ink3);border:1px solid var(--muted);border-radius:2px;padding:13px;font-family:'JetBrains Mono',monospace;font-size:.62rem;color:var(--dim);line-height:1.9;letter-spacing:.02em}
.detbox.open{display:block}

/* LOG */
.log-wrap{background:var(--ink2);border:1px solid var(--border);border-radius:2px;overflow:hidden}
.log-hdr{padding:12px 17px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between}
.log-ttl{font-family:'Bebas Neue',sans-serif;font-size:.82rem;letter-spacing:.15em;color:var(--gold)}
.log-sub{font-family:'JetBrains Mono',monospace;font-size:.58rem;color:var(--dim);letter-spacing:.05em}
.log-body{padding:13px 17px;max-height:520px;overflow-y:auto;font-family:'JetBrains Mono',monospace;font-size:.66rem;line-height:1.95;color:var(--dim)}
.log-body::-webkit-scrollbar{width:3px}
.log-body::-webkit-scrollbar-track{background:transparent}
.log-body::-webkit-scrollbar-thumb{background:var(--muted);border-radius:1px}
.ll-s{color:var(--green)}.ll-e{color:var(--red)}.ll-i{color:rgba(77,166,255,.65)}

/* TOAST */
.toast{position:fixed;bottom:26px;left:50%;transform:translateX(-50%) translateY(100px);background:var(--ink3);border:1px solid var(--border2);border-radius:2px;padding:11px 22px;font-family:'JetBrains Mono',monospace;font-size:.7rem;font-weight:500;letter-spacing:.04em;box-shadow:0 18px 55px rgba(0,0,0,.7);opacity:0;transition:all .4s cubic-bezier(.16,1,.3,1);pointer-events:none;z-index:9999;white-space:nowrap}
.toast.show{transform:translateX(-50%) translateY(0);opacity:1}
.toast.bt{border-color:rgba(0,229,160,.3);color:var(--green)}
.toast.st{border-color:rgba(255,77,109,.3);color:var(--red)}

@media(max-width:820px){
  .stats-grid{grid-template-columns:1fr 1fr 1fr}
  .prices-grid{grid-template-columns:repeat(3,1fr)}
  .hdr-in,.sec,.tab-wrap{padding:0 14px}.prog{padding:7px 14px}.scan-num{display:none}
  .lvl-grid{grid-template-columns:1fr 1fr}
}
@media(max-width:480px){
  .stats-grid{grid-template-columns:1fr 1fr}
  .prices-grid{grid-template-columns:repeat(2,1fr)}
}
</style>
</head>
<body>
<div class="gbg"></div>
<div class="pb" id="pb">⏸ SCANNER PAUSED — PRESS RESUME TO RESTART</div>

<header class="hdr">
  <div class="hdr-in">
    <div class="brand">
      <div class="blogo">📡</div>
      <div class="btext">
        <span class="bname">CRT Scanner</span>
        <span class="bsub">MEXC USDT PERPETUAL FUTURES</span>
      </div>
    </div>
    <div class="scan-status">
      <div class="sdot" id="sdot"></div>
      <span class="stxt" id="stxt">SCANNING...</span>
    </div>
    <div class="hdr-right">
      <span class="scan-num" id="snum">SCAN #0</span>
      <button class="tbtn on" id="tbtn" onclick="toggleScanner()">■ STOP</button>
      <button class="obtn" onclick="logout()">EXIT</button>
    </div>
  </div>
</header>

<div class="prog">
  <div class="prog-in">
    <span class="prog-lbl" id="cpair">INITIALISING...</span>
    <div class="prog-track"><div class="prog-fill" id="pfill" style="width:0%"></div></div>
    <span class="prog-cnt" id="pcnt">0 / 0</span>
  </div>
</div>

<div class="sec">
  <div class="sec-hdr">
    <span class="sec-title">Live Prices</span>
    <div class="sec-line"></div>
    <span class="sec-note" id="pupd">–</span>
  </div>
  <div class="prices-grid" id="pgrid">
    <div class="pc" style="min-height:72px"></div><div class="pc" style="min-height:72px"></div>
    <div class="pc" style="min-height:72px"></div><div class="pc" style="min-height:72px"></div>
    <div class="pc" style="min-height:72px"></div><div class="pc" style="min-height:72px"></div>
  </div>
</div>

<div class="sec" style="margin-top:13px">
  <div class="stats-grid">
    <div class="sc s0"><div class="sc-lbl">Total Signals</div><div class="sc-val" id="st">0</div><div class="sc-sub">All time</div></div>
    <div class="sc s1"><div class="sc-lbl">Buy Signals</div><div class="sc-val" style="color:var(--green)" id="sb">0</div></div>
    <div class="sc s2"><div class="sc-lbl">Sell Signals</div><div class="sc-val" style="color:var(--red)" id="ss">0</div></div>
    <div class="sc s3"><div class="sc-lbl">Scans Done</div><div class="sc-val" style="color:var(--blue)" id="sc2">0</div><div class="sc-sub" id="sl2">–</div></div>
    <div class="sc s4"><div class="sc-lbl">Pairs This Scan</div><div class="sc-val" style="color:var(--purple)" id="sp">0</div></div>
  </div>
</div>

<div class="tab-wrap">
  <div class="tabs">
    <button class="tab active" onclick="sw('signals',this)">Signals</button>
    <button class="tab" onclick="sw('log',this)">Live Log</button>
  </div>

  <div id="tab-signals">
    <div class="frow">
      <div class="ftitle">CRT Signals</div>
      <div class="fgrp">
        <select class="fsel" id="fd" onchange="renderSigs()"><option value="">All Directions</option><option value="BUY">BUY</option><option value="SELL">SELL</option></select>
        <select class="fsel" id="fg" onchange="renderSigs()"><option value="">All Grades</option><option value="A+">A+</option><option value="A">A</option><option value="B">B+</option></select>
        <select class="fsel" id="ftf" onchange="renderSigs()"><option value="">All CRT TFs</option><option value="Day1">1D CRT</option><option value="Hour4">4H CRT</option><option value="Hour3">3H CRT</option><option value="Hour2">2H CRT</option><option value="Min60">1H CRT</option></select>
        <select class="fsel" id="fob" onchange="renderSigs()"><option value="">All OB TFs</option><option value="Hour4">4H OB</option><option value="Hour3">3H OB</option><option value="Hour2">2H OB</option><option value="Min60">1H OB</option><option value="Min45">45m OB</option></select>
      </div>
    </div>
    <div class="sig-list" id="slist">
      <div class="empty"><div class="empty-ico">📡</div><div class="empty-t">Scanning Markets</div><div class="empty-s">Scanning all MEXC USDT perpetual futures for CRT setups with OB confluence, TBS body close, CHOCH and FVG/IFVG entry. Minimum 3R.</div></div>
    </div>
  </div>

  <div id="tab-log" style="display:none">
    <div class="log-wrap">
      <div class="log-hdr"><span class="log-ttl">Scanner Log</span><span class="log-sub">UPDATES EVERY 3S</span></div>
      <div class="log-body" id="lbody">Loading...</div>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
(function(){
'use strict';
let allSigs=[],toastT,activeTab='signals',prevP={},tick=0,lastCount=0;
const $=id=>document.getElementById(id);

function toast(m,t,d=3500){
  const el=$('toast');el.textContent=m;
  el.className='toast show'+(t==='buy'?' bt':t==='sell'?' st':'');
  clearTimeout(toastT);toastT=setTimeout(()=>el.classList.remove('show'),d);
}

function scoreColor(s){
  return s>=88?'var(--green)':s>=75?'var(--gold2)':s>=60?'var(--blue)':s>=45?'var(--orange)':'var(--dim)';
}

function fmt(v){
  if(v===null||v===undefined||v==='–'||v===false||v==='false')return'–';
  const n=Number(v);if(isNaN(n))return String(v);
  if(n>=10000)return n.toLocaleString(undefined,{maximumFractionDigits:2});
  if(n>=1)return n.toFixed(4);return n.toFixed(6);
}

function fmtP(v){
  const n=Number(v);if(!n)return'–';
  if(n>=10000)return'$'+n.toLocaleString(undefined,{maximumFractionDigits:2});
  if(n>=1)return'$'+n.toFixed(4);return'$'+n.toFixed(6);
}

const TF_MAP={'Day1':'1D','Hour4':'4H','Hour3':'3H','Hour2':'2H','Min60':'1H','Min45':'45m','Min30':'30m','Min15':'15m','Min10':'10m','Min5':'5m'};
const TOP=['BTC_USDT','ETH_USDT','SOL_USDT','BNB_USDT','XRP_USDT','DOGE_USDT'];

async function fetchPrices(){
  try{
    const r=await fetch('/api/prices');const data=await r.json();
    $('pupd').textContent='UPDATED '+new Date().toLocaleTimeString();
    $('pgrid').innerHTML=TOP.map(sym=>{
      const d=data[sym],name=sym.replace('_USDT','');
      if(!d)return`<div class="pc"><div class="pc-sym">${name}/USDT</div><div class="pc-price" style="color:var(--dim)">–</div></div>`;
      const up=d.change>=0;
      return`<div class="pc ${up?'up':'dn'}">
        <div class="pc-sym">${name}/USDT</div>
        <div class="pc-price ${up?'up':'dn'}">${fmtP(d.price)}</div>
        <span class="pc-chg ${up?'up':'dn'}">${up?'▲':'▼'} ${Math.abs(d.change).toFixed(2)}%</span>
      </div>`;
    }).join('');
  }catch{}
}

function buildCard(s,idx){
  const dir=(s.direction||'BUY').toUpperCase();
  const sc=s.score||0;
  const gr=s.grade||'–';
  const gc={'A+':'gAp','A':'gA','B':'gB','C':'gC','D':'gD'}[gr]||'gD';
  const crtTF=TF_MAP[s.tf]||s.tf||'–';
  const obTF=TF_MAP[s.ob_tf]||s.ob_tf||'–';
  const cf=(ok,l)=>`<span class="cf ${ok?'cf-ok':'cf-no'}">${ok?'✓':'✗'} ${l}</span>`;
  const cfw=(ok,l)=>`<span class="cf ${ok?'cf-ok':'cf-w'}">${ok?'✓':'⚠'} ${l}</span>`;
  const cfg=(ok,l)=>`<span class="cf ${ok?'cf-g':'cf-no'}">${ok?'◆':'◇'} ${l}</span>`;
  const details=(s.details||[]).join('\n');
  const isND=s.ob_tf==='N/A'||s.tf==='Day1';
  return`<div class="scard ${dir.toLowerCase()}">
    <div class="card-hdr">
      <span class="dtag ${dir}">${dir}</span>
      <span class="csym">${s.symbol||'–'}</span>
      <div class="chips">
        <span class="chip chip-tf">${crtTF} CRT</span>
        ${!isND?`<span class="chip chip-ob">OB ${obTF}</span>`:''}
        <span class="chip chip-tr ${s.trend}">${s.trend}</span>
      </div>
      <span class="gtag ${gc}">${gr}</span>
      <span class="cts">${s.timestamp||''}</span>
    </div>
    <div class="lvl-grid">
      <div class="lv lv-e"><div class="lv-lbl">Entry·${s.entry_type||'FVG'}</div><div class="lv-val">${fmt(s.entry)}</div></div>
      <div class="lv lv-s"><div class="lv-lbl">Stop Loss</div><div class="lv-val">${fmt(s.sl)}</div></div>
      <div class="lv lv-t"><div class="lv-lbl">Take Profit</div><div class="lv-val">${fmt(s.tp)}</div></div>
      <div class="lv lv-r"><div class="lv-lbl">Risk·Reward</div><div class="lv-val">${s.rr}R</div></div>
      <div class="lv lv-f"><div class="lv-lbl">FVG Top</div><div class="lv-val">${fmt(s.fvg_top)}</div></div>
      <div class="lv lv-f"><div class="lv-lbl">FVG Bot</div><div class="lv-val">${fmt(s.fvg_bot)}</div></div>
      <div class="lv"><div class="lv-lbl">CRH</div><div class="lv-val" style="color:var(--gold)">${fmt(s.crh)}</div></div>
      <div class="lv"><div class="lv-lbl">CRL</div><div class="lv-val" style="color:var(--gold)">${fmt(s.crl)}</div></div>
      ${s.ob_top&&s.ob_top!=='–'?`<div class="lv lv-o"><div class="lv-lbl">OB Top</div><div class="lv-val">${fmt(s.ob_top)}</div></div>`:''}
      ${s.ob_bot&&s.ob_bot!=='–'?`<div class="lv lv-o"><div class="lv-lbl">OB Bot</div><div class="lv-val">${fmt(s.ob_bot)}</div></div>`:''}
    </div>
    <div class="cfms">
      ${cf(s.tbs_found,'TBS · '+TF_MAP[s.tbs_tf||'']||s.tbs_tf||'–')}
      ${cf(s.fvg_found,(s.fvg_type||'FVG'))}
      ${cf(s.choch_found,'CHOCH')}
      ${cfw(s.liq_swept,'LIQ SWEEP')}
      ${!isND?cfw(s.ob_respected,'OB RESPECT'):''}
      ${s.ob_zone&&s.ob_zone!=='–'?cfg(true,s.ob_zone):isND?cfg(true,'1D NO OB'):''}
      ${cfw(s.continuous,'STRUCTURE')}
    </div>
    <div class="srow">
      <span class="slbl">SCORE</span>
      <div class="strack"><div class="sfill" style="width:${sc}%;background:${scoreColor(sc)}"></div></div>
      <span class="snum" style="color:${scoreColor(sc)}">${sc}/100</span>
    </div>
    ${details?`<button class="dettog" onclick="toggleDet(${idx})">▶ BREAKDOWN</button>
    <div class="detbox" id="det-${idx}"><pre style="font-size:.6rem;line-height:1.85;white-space:pre-wrap">${details}</pre></div>`:''}
  </div>`;
}

window.toggleDet=function(i){
  const b=$('det-'+i);if(!b)return;
  b.classList.toggle('open');
  const t=b.previousElementSibling;
  if(t)t.textContent=b.classList.contains('open')?'▼ BREAKDOWN':'▶ BREAKDOWN';
};

window.renderSigs=function(){
  const dF=$('fd').value,gF=$('fg').value,tfF=$('ftf').value,obF=$('fob').value;
  let f=allSigs.filter(s=>{
    if(dF&&s.direction!==dF)return false;
    if(tfF&&s.tf!==tfF)return false;
    if(obF&&s.ob_tf!==obF)return false;
    if(gF){
      if(gF==='A+'&&s.grade!=='A+')return false;
      if(gF==='A'&&s.grade!=='A')return false;
      if(gF==='B'&&!['A+','A','B'].includes(s.grade))return false;
    }
    return true;
  });
  const list=$('slist');
  if(!f.length){
    list.innerHTML='<div class="empty"><div class="empty-ico">📡</div><div class="empty-t">Scanning Markets</div><div class="empty-s">Scanning all MEXC USDT perpetual futures for CRT setups. TBS body close is mandatory. Minimum 3R.</div></div>';
    return;
  }
  list.innerHTML=f.slice(0,100).map((s,i)=>buildCard(s,i)).join('');
};

async function fetchSigs(){
  try{
    const r=await fetch('/api/signals?limit=200');const data=await r.json();
    allSigs=data;
    if(data.length>lastCount&&lastCount>0){
      const n=data[0];
      toast(`NEW · ${n.direction} ${n.symbol} · ${n.score}/100 ${n.grade} · ${n.rr}R`,n.direction==='BUY'?'buy':'sell');
    }
    lastCount=data.length; renderSigs();
  }catch{}
}

async function fetchStats(){
  try{
    const r=await fetch('/api/stats');const d=await r.json();
    $('st').textContent=d.total||0;$('sb').textContent=d.buys||0;$('ss').textContent=d.sells||0;
  }catch{}
}

async function fetchState(){
  try{
    const r=await fetch('/api/scan-state');const d=await r.json();
    const pct=d.total_pairs>0?Math.round(d.pairs_done/d.total_pairs*100):0;
    $('pfill').style.width=pct+'%';
    $('pcnt').textContent=`${d.pairs_done}/${d.total_pairs}`;
    $('cpair').textContent=d.current_pair?`SCANNING: ${d.current_pair}`:'WAITING...';
    $('sc2').textContent=d.scan_count||0;$('sp').textContent=d.pairs_done||0;
    $('sl2').textContent=d.last_scan?`LAST: ${d.last_scan}`:'–';
    $('snum').textContent=`SCAN #${d.scan_count||0}`;
    const en=d.enabled!==false;
    $('tbtn').textContent=en?'■ STOP':'▶ RESUME';$('tbtn').className='tbtn '+(en?'on':'off');
    $('sdot').className='sdot'+(en?'':' off');
    $('stxt').textContent=en?'SCANNING...':'PAUSED';$('stxt').className='stxt'+(en?'':' off');
    $('pb').className='pb'+(en?'':' show');
  }catch{}
}

async function fetchLog(){
  if(activeTab!=='log')return;
  try{
    const r=await fetch('/api/log');const d=await r.json();
    $('lbody').innerHTML=d.log.map(l=>{
      const cls=l.includes('🎯')?'ll-s':l.includes('❌')||l.includes('Error')?'ll-e':'ll-i';
      return`<div class="${cls}">${l}</div>`;
    }).join('');
  }catch{}
}

window.toggleScanner=async function(){
  try{
    const r=await fetch('/api/toggle-scanner',{method:'POST'});const d=await r.json();
    toast(d.enabled?'▶ SCANNER RESUMED':'■ SCANNER PAUSED',d.enabled?'buy':'sell');
    await fetchState();
  }catch{toast('ERROR: TOGGLE FAILED');}
};

window.sw=function(tab,btn){
  activeTab=tab;
  document.querySelectorAll('.tab').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
  $('tab-signals').style.display=tab==='signals'?'block':'none';
  $('tab-log').style.display=tab==='log'?'block':'none';
  if(tab==='log')fetchLog();
};

window.logout=function(){fetch('/api/logout',{method:'POST'}).finally(()=>window.location.href='/');};

async function poll(){
  tick++;
  const ps=[fetchSigs(),fetchStats(),fetchState(),fetchLog()];
  if(tick%2===0)ps.push(fetchPrices());
  await Promise.all(ps);
  setTimeout(poll,3000);
}
fetchPrices();poll();
})();
</script>
</body>
</html>"""

# ════════ FLASK ROUTES ════════════════════════════════════════════════

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
        en=scan_state["enabled"]
    log(f"{'▶ RESUMED' if en else '⏸ PAUSED'} by user")
    return jsonify({"enabled":en})

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
    with scan_lock: return jsonify({k:v for k,v in scan_state.items() if k!="log"})

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

# ════════ STARTUP ═════════════════════════════════════════════════════

def start_scanner():
    t=threading.Thread(target=scanner_loop,daemon=True); t.start()
    log("Scanner thread launched.")

with app.app_context():
    start_scanner()

if __name__=="__main__":
    port=int(os.environ.get("PORT",5000))
    app.run(host="0.0.0.0",port=port,debug=False)
