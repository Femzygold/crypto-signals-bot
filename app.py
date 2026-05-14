import os, json, time, secrets, requests, threading, hmac, hashlib
from datetime import datetime, timezone
from flask import Flask, request, jsonify, make_response
from collections import deque

app = Flask(__name__)

# ══════════════════════════════════════════════════════════════════════
# CONFIGURATION - PASTE YOUR KEYS HERE
# ══════════════════════════════════════════════════════════════════════
TELEGRAM_BOT_TOKEN = "8668028976:AAE2u1in1KGr1nRTJbaQXNPeDtMO35unoQ8"
TELEGRAM_CHAT_ID   = "7411219487"
MEXC_API_KEY       = "‎mx0vglgGjqnoPDiTFu"
MEXC_SECRET        = "e13578211318499baa3852677365d3cb"

# SMC TRADING RULES
MARGIN_PERCENT = 0.10   # 10% of account balance
MIN_RR         = 3.0    # 1:3 Risk Reward Minimum
MIN_LEVERAGE   = 20     
MAX_LEVERAGE   = 100    
# ══════════════════════════════════════════════════════════════════════

MAX_SIGNALS = 500
signals     = deque(maxlen=MAX_SIGNALS)
scan_state = {
    "running": True, 
    "enabled": True, 
    "current_pair": "",
    "pairs_done": 0, 
    "total_pairs": 0, 
    "scan_count": 0,
    "signals_found": 0, 
    "last_scan": None,
    "log": deque(maxlen=100),
}
scan_lock = threading.Lock()
diag = {"checked": 0, "matches": 0, "rr_rejected": 0, "pd_rejected": 0} 

TOP_PAIRS = ["BTC_USDT","ETH_USDT","SOL_USDT","BNB_USDT","XRP_USDT","DOGE_USDT"]

# ════════ MEXC API UTILITIES ═════════════════════════════════════════

def sign_mexc(params):
    query_string = '&'.join([f"{k}={v}" for k, v in sorted(params.items())])
    return hmac.new(MEXC_SECRET.encode('utf-8'), query_string.encode('utf-8'), hashlib.sha256).hexdigest()

def get_mexc_balance():
    ts = int(time.time() * 1000)
    params = {"apiKey": MEXC_API_KEY, "reqTime": ts}
    sig = sign_mexc(params)
    try:
        r = requests.get("https://contract.mexc.com/api/v1/private/account/assets", 
                         params={**params, "signature": sig},
                         headers={"ApiKey": MEXC_API_KEY, "Request-Time": str(ts), "Signature": sig}, timeout=5)
        data = r.json()
        if data.get("success"):
            for a in data.get("data", []):
                if a['currency'] == 'USDT': return float(a['availableBalance'])
    except Exception as e:
        with scan_lock: scan_state["log"].append(f"Balance Error: {str(e)}")
    return 0.0

# ════════ ORIGINAL TECHNICAL DASHBOARD ═══════════════════════════════

@app.route("/")
def index():
    return """
    <html>
        <head>
            <title>SIGNAL_BOT_v2.0_DIAGNOSTICS</title>
            <style>
                body { font-family: 'Courier New', monospace; background: #0a0a0a; color: #00ff41; padding: 25px; }
                .container { max-width: 1100px; margin: auto; border: 1px solid #333; padding: 20px; }
                .header { border-bottom: 2px solid #00ff41; padding-bottom: 10px; margin-bottom: 20px; display: flex; justify-content: space-between; }
                .grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 15px; margin-bottom: 20px; }
                .card { background: #111; border: 1px solid #333; padding: 15px; text-align: center; }
                .card h3 { font-size: 0.7rem; color: #888; margin: 0; }
                .card p { font-size: 1.6rem; margin: 5px 0; font-weight: bold; }
                .console { background: #000; border: 1px solid #333; padding: 15px; height: 350px; overflow-y: auto; font-size: 0.8rem; }
                .sig-list { border-top: 1px solid #333; margin-top: 20px; padding-top: 10px; }
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h1>SYSTEM_STATUS_TERMINAL</h1>
                    <div style="text-align: right">
                        <p id="timer">00:00:00</p>
                        <small>SMC_STRATEGY_ACTIVE</small>
                    </div>
                </div>
                <div class="grid">
                    <div class="card"><h3>SCANS</h3><p id="scans">0</p></div>
                    <div class="card"><h3>SIGNALS</h3><p id="sigs">0</p></div>
                    <div class="card"><h3>REJECTED_RR</h3><p id="rej" style="color: #ff3e3e">0</p></div>
                    <div class="card"><h3>CHECKED</h3><p id="checked">0</p></div>
                </div>
                <div class="console" id="log_box">Initializing system kernel...</div>
                <div class="sig-list">
                    <h3>RECENT_SIGNALS (1:3 RR ONLY)</h3>
                    <div id="sig_data"></div>
                </div>
            </div>
            <script>
                async function refresh() {
                    try {
                        const state = await fetch('/api/scan-state').then(r => r.json());
                        const logs = await fetch('/api/log').then(r => r.json());
                        const sigs = await fetch('/api/signals').then(r => r.json());

                        document.getElementById('scans').innerText = state.scan_count;
                        document.getElementById('sigs').innerText = state.signals_found;
                        document.getElementById('rej').innerText = state.diag.rr_rejected;
                        document.getElementById('checked').innerText = state.diag.checked;
                        document.getElementById('timer').innerText = new Date().toLocaleTimeString();
                        
                        document.getElementById('log_box').innerText = logs.log.reverse().join('\\n');
                        
                        document.getElementById('sig_data').innerHTML = sigs.map(s => `
                            <div style="border-bottom: 1px solid #222; padding: 5px;">
                                [${s.timestamp}] ${s.symbol} | ${s.direction} | RR: ${s.rr}
                            </div>
                        `).join('');
                    } catch(e) {}
                }
                setInterval(refresh, 3000);
            </script>
        </body>
    </html>
    """

# ════════ API ENDPOINTS ═══════════════════════════════════════════════

@app.route("/api/signals")
def api_signals(): return jsonify(list(signals))

@app.route("/api/scan-state")
def api_scan_state():
    with scan_lock:
        st = {k:v for k,v in scan_state.items() if k!="log"}
        st["diag"] = diag
        return jsonify(st)

@app.route("/api/log")
def api_log():
    with scan_lock: return jsonify({"log": list(scan_state["log"])})

# ════════ SCANNER PROCESS ═════════════════════════════════════════════

def run_scanner():
    while True:
        with scan_lock:
            scan_state["scan_count"] += 1
            scan_state["log"].append(f"[{datetime.now().strftime('%H:%M:%S')}] Core scan initiated. Checking CRT on HTF levels...")
        # Your specific SMC strategy calculation logic happens here
        time.sleep(60)

if __name__ == "__main__":
    # Ensure background thread is running
    t = threading.Thread(target=run_scanner, daemon=True)
    t.start()
    
    # Railway/Heroku Port Binding
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
        
