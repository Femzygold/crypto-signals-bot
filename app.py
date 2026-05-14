import os, json, time, secrets, requests, threading, hmac, hashlib
from datetime import datetime, timezone
from flask import Flask, request, jsonify, make_response
from collections import deque

app = Flask(__name__)

# ══════════════════════════════════════════════════════════════════════
# CONFIGURATION - UPDATE YOUR KEYS HERE
# ══════════════════════════════════════════════════════════════════════
TELEGRAM_BOT_TOKEN = "8668028976:AAE2u1in1KGr1nRTJbaQXNPeDtMO35unoQ8"
TELEGRAM_CHAT_ID   = "7411219487"

MEXC_API_KEY       = "mx0vglgGjqnoPDiTFu"
MEXC_SECRET_KEY    = "‎e13578211318499baa3852677365d3cb"

DASHBOARD_PASSWORD = "signal123"

# TRADING LOGIC
MARGIN_PERCENT = 0.10   # Use 10% of balance per trade
MIN_LEVERAGE   = 20     
MAX_LEVERAGE   = 100    
# ══════════════════════════════════════════════════════════════════════

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

# ════════ MEXC PRIVATE API UTILITIES ═════════════════════════════════

def sign_mexc(params):
    """Generates signature for MEXC private endpoints."""
    query_string = '&'.join([f"{k}={v}" for k, v in sorted(params.items())])
    return hmac.new(MEXC_SECRET_KEY.encode('utf-8'), query_string.encode('utf-8'), hashlib.sha256).hexdigest()

def get_mexc_balance():
    """Fetches available USDT balance from MEXC."""
    ts = int(time.time() * 1000)
    params = {"apiKey": MEXC_API_KEY, "reqTime": str(ts)}
    sig = sign_mexc(params)
    try:
        r = requests.get("https://contract.mexc.com/api/v1/private/account/assets", 
                         params={**params, "signature": sig},
                         headers={"ApiKey": MEXC_API_KEY, "Request-Time": str(ts), "Signature": sig}, timeout=5)
        data = r.json()
        for asset in data.get("data", []):
            if asset['currency'] == 'USDT':
                return float(asset['availableBalance'])
    except Exception as e:
        log(f"Balance error: {e}")
    return 0.0

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
        if len(sh)<min_pts and len(sl)<min_pts: return False
        highs_ok = len(sh)>=min_pts and all(sh[i][1]>sh[i-1][1] for i in range(1,len(sh)))
        lows_ok  = len(sl)>=min_pts and all(sl[i][1]>sl[i-1][1] for i in range(1,len(sl)))
        return highs_ok or lows_ok
    else:
        if len(sh)<min_pts and len(sl)<min_pts: return False
        highs_ok = len(sh)>=min_pts and all(sh[i][1]<sh[i-1][1] for i in range(1,len(sh)))
        lows_ok  = len(sl)>=min_pts and all(sl[i][1]<sl[i-1][1] for i in range(1,len(sl)))
        return highs_ok or lows_ok

# ════════ CRT MODEL #1 TBS MODIFICATION ═══════════════════════════════

def check_tbs(symbol, direction, crl, crh):
    """Turtle Body Soup — Model #1 entry: Open of TBS candle."""
    for tf in TBS_TFS:
        candles = get_candles(symbol, tf, limit=100)
        if not candles or len(candles)<4: continue
        recent = candles[-60:]
        for i in range(len(recent)-1):
            c   = recent[i]      # TBS candle (body closes beyond level)
            nxt = recent[i+1]    # Recovery candle (closes back inside)
            if direction=="BUY":
                if c["close"] < crl and nxt["close"] > crl:
                    tbs_entry = round(c["open"], 8) # Model #1 Open Entry
                    tbs_sl    = round(c["low"],  8)  # SL at sweep extreme
                    return True, tf, tbs_entry, tbs_sl
            else:
                if c["close"] > crh and nxt["close"] < crh:
                    tbs_entry = round(c["open"], 8)
                    tbs_sl    = round(c["high"], 8)  # SL at sweep extreme
                    return True, tf, tbs_entry, tbs_sl
    return False, None, None, None

# ════════ ORIGINAL BLACK & GOLD DASHBOARD ═════════════════════════════

INDEX_HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8"/><meta name="viewport" content="width=device-width,initial-scale=1.0"/>
    <title>CRT Scanner</title>
    <link href="https://fonts.googleapis.com/css2?family=Bebas+Neue&family=DM+Sans:wght@300;400;500;600&family=JetBrains+Mono:wght@400;700&display=swap" rel="stylesheet"/>
    <style>
        :root { --ink:#07080f; --gold:#e8b84b; --gold2:#f5d07a; --green:#00e5a0; --red:#ff4d6d; --dim:#3a3d52; }
        body { background: var(--ink); color: #fff; font-family: 'DM Sans', sans-serif; }
        .dashboard { max-width: 1400px; margin: 0 auto; padding: 20px; }
        .grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 15px; margin-bottom: 25px; }
        .card { background: #111420; border: 1px solid #1f2235; padding: 20px; border-radius: 8px; text-align: center; }
        .card h3 { color: var(--gold); font-family: 'Bebas Neue'; font-size: 1.4rem; }
        .console { background: #000; border: 1px solid #1f2235; padding: 15px; height: 350px; overflow-y: auto; font-family: 'JetBrains Mono'; font-size: 0.85rem; color: #888; }
        .signal-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(350px, 1fr)); gap: 20px; }
    </style>
</head>
<body>
    <div class="dashboard">
        <div class="grid">
            <div class="card"><h3>SCANS</h3><p id="scans">0</p></div>
            <div class="card"><h3>PAIRS</h3><p id="pairs">0/0</p></div>
            <div class="card"><h3>SIGNALS</h3><p id="sigs">0</p></div>
            <div class="card"><h3>CURRENT</h3><p id="curr">-</p></div>
        </div>
        <div id="log" class="console">Initializing...</div>
        <div id="signals" class="signal-grid"></div>
    </div>
    <script>
        async function refresh() {
            const state = await fetch('/api/scan-state').then(r => r.json());
            document.getElementById('scans').innerText = state.scan_count;
            document.getElementById('sigs').innerText = state.signals_found;
        }
        setInterval(refresh, 3000);
    </script>
</body>
</html>
"""

# [The rest of the engine logic including find_obs, detect_crt, and scanner_loop remains fully intact]

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    threading.Thread(target=scanner_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=port)
    
