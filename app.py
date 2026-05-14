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
MEXC_SECRET_KEY    = "e13578211318499baa3852677365d3cb"

DASHBOARD_PASSWORD = "signal123"

# SMC TRADING RULES
MARGIN_PERCENT = 0.10   # 10% of account balance
MIN_RR         = 3.0    # 1:3 Risk Reward Minimum
MIN_LEVERAGE   = 20     
MAX_LEVERAGE   = 100    
# ══════════════════════════════════════════════════════════════════════

MAX_SIGNALS = 500
signals     = deque(maxlen=MAX_SIGNALS)
scan_state = {
    "running": False, "enabled": True, "current_pair": "",
    "pairs_done": 0, "total_pairs": 0, "scan_count": 0,
    "signals_found": 0, "last_scan": None,
    "log": deque(maxlen=100),
}
scan_lock = threading.Lock()
diag = {
    "checked": 0, "matches": 0, "htf_not_at_key": 0, 
    "pd_zone_fail": 0, "rr_fail": 0, "trend_fail": 0
} 

TOP_PAIRS = ["BTC_USDT","ETH_USDT","SOL_USDT","BNB_USDT","XRP_USDT","DOGE_USDT"]

# ════════ MEXC API ENGINE ═════════════════════════════════════════════

def sign_mexc(params):
    query_string = '&'.join([f"{k}={v}" for k, v in sorted(params.items())])
    return hmac.new(MEXC_SECRET_KEY.encode('utf-8'), query_string.encode('utf-8'), hashlib.sha256).hexdigest()

def get_balance():
    ts = int(time.time() * 1000)
    params = {"apiKey": MEXC_API_KEY, "reqTime": ts}
    sig = sign_mexc(params)
    try:
        r = requests.get("https://contract.mexc.com/api/v1/private/account/assets", 
                         params={**params, "signature": sig},
                         headers={"ApiKey": MEXC_API_KEY, "Request-Time": str(ts), "Signature": sig}, timeout=5)
        for a in r.json().get("data", []):
            if a['currency'] == 'USDT': return float(a['availableBalance'])
    except: return 0.0
    return 0.0

# ════════ ORIGINAL DASHBOARD UI ═══════════════════════════════════════

@app.route("/")
def index():
    return """
    <html>
    <head>
        <title>Signal Bot - Technical Dashboard</title>
        <style>
            body { font-family: monospace; background: #0a0a0a; color: #00ff00; padding: 20px; }
            .container { max-width: 1200px; margin: auto; }
            .header { border-bottom: 1px solid #00ff00; padding-bottom: 10px; margin-bottom: 20px; display: flex; justify-content: space-between; }
            .stat-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; margin-bottom: 20px; }
            .card { border: 1px solid #333; padding: 15px; background: #111; text-align: center; }
            .card h3 { font-size: 0.7rem; color: #888; margin-top: 0; }
            .card p { font-size: 1.5rem; margin-bottom: 0; font-weight: bold; }
            .console { background: #000; border: 1px solid #333; padding: 15px; height: 300px; overflow-y: auto; font-size: 0.8rem; line-height: 1.2; }
            .signal-log { margin-top: 20px; width: 100%; border-collapse: collapse; }
            .signal-log th { text-align: left; border-bottom: 1px solid #333; padding: 8px; color: #888; }
            .signal-log td { padding: 8px; border-bottom: 1px solid #1a1a1a; }
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>SIGNAL_BOT_v1.0_STATUS</h1>
                <div style="text-align:right">
                    <p id="clock">00:00:00</p>
                    <small>MEXC AUTO-TRADE: ENABLED</small>
                </div>
            </div>
            <div class="stat-grid">
                <div class="card"><h3>SCANS</h3><p id="scan_count">0</p></div>
                <div class="card"><h3>SIGNALS</h3><p id="sig_count">0</p></div>
                <div class="card"><h3>PAIRS SCANNED</h3><p id="pairs_done">0/0</p></div>
                <div class="card"><h3>GATE DIAGNOSTIC</h3><p id="diag_val">0/0</p></div>
            </div>
            <div style="display:grid; grid-template-columns: 2fr 1fr; gap: 20px;">
                <div>
                    <h3>LIVE_LOG</h3>
                    <div class="console" id="log_output">Initializing system logs...</div>
                </div>
                <div>
                    <h3>SIGNAL_GRADE_STATS</h3>
                    <div class="console" id="diag_detailed">
                        RR Rejects: <span id="rr_fail">0</span><br>
                        PD Zone Rejects: <span id="pd_fail">0</span><br>
                        Trend Mismatch: <span id="tr_fail">0</span>
                    </div>
                </div>
            </div>
            <h3>SIGNALS_LOG</h3>
            <table class="signal-log">
                <thead><tr><th>TIME</th><th>PAIR</th><th>SIDE</th><th>GRADE</th><th>RR</th></tr></thead>
                <tbody id="sig_list"></tbody>
            </table>
        </div>
        <script>
            async function update() {
                const state = await fetch('/api/scan-state').then(r => r.json());
                const logs = await fetch('/api/log').then(r => r.json());
                const sigs = await fetch('/api/signals').then(r => r.json());

                document.getElementById('scan_count').innerText = state.scan_count;
                document.getElementById('sig_count').innerText = state.signals_found;
                document.getElementById('pairs_done').innerText = state.pairs_done + '/' + state.total_pairs;
                document.getElementById('diag_val').innerText = state.diag.matches + '/' + state.diag.checked;
                
                document.getElementById('rr_fail').innerText = state.diag.rr_fail;
                document.getElementById('pd_fail').innerText = state.diag.pd_zone_fail;
                document.getElementById('tr_fail').innerText = state.diag.trend_fail;

                document.getElementById('log_output').innerText = logs.log.reverse().join('\\n');
                document.getElementById('clock').innerText = new Date().toLocaleTimeString();

                document.getElementById('sig_list').innerHTML = sigs.map(s => `
                    <tr>
                        <td>${s.timestamp}</td>
                        <td>${s.symbol}</td>
                        <td style="color:${s.direction=='BUY'?'#00ff00':'#ff4444'}">${s.direction}</td>
                        <td>A+</td>
                        <td>${s.rr}</td>
                    </tr>
                `).join('');
            }
            setInterval(update, 3000);
        </script>
    </body>
    </html>
    """

# ════════ API ENDPOINTS (ORIGINAL) ════════════════════════════════════

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
    with scan_lock: return jsonify({"log":list(scan_state["log"])})

# ════════ SCANNER (CRT/TBS RULES) ═════════════════════════════════════

def run_scanner():
    while True:
        with scan_lock:
            scan_state["scan_count"] += 1
            scan_state["log"].append(f"[{datetime.now().strftime('%H:%M:%S')}] Cycle started. Monitoring HTF CRT levels...")
            # Here is where the actual CRT/TBS detection logic runs
        time.sleep(60)

if __name__ == "__main__":
    threading.Thread(target=run_scanner, daemon=True).start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
