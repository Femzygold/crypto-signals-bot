import os, json, time, secrets, requests, threading, hmac, hashlib
from datetime import datetime, timezone
from flask import Flask, request, jsonify, make_response
from collections import deque

app = Flask(__name__)

TELEGRAM_BOT_TOKEN = "8668028976:AAE2u1in1KGr1nRTJbaQXNPeDtMO35unoQ8"
TELEGRAM_CHAT_ID   = "7411219487"
DASHBOARD_PASSWORD = "signal123"

# ==================== MEXC KEYS (HARDCODED) ====================
MEXC_API_KEY = "‎mx0vglgGjqnoPDiTFu
‎"
MEXC_API_SECRET = "‎e13578211318499baa3852677365d3cb
‎"

ENABLE_AUTO_TRADING = False   # Change to True when ready
RISK_PERCENT = 0.25
MAX_SIMULTANEOUS_TRADES = 4

TBS_LTF_MAP = {
    "Day1": ["Min60", "Min45"],
    "Hour4": ["Min4", "Min3"],
    "Hour3": ["Min3", "Min2"],
    "Hour2": ["Min2", "Min1"],
    "Min60": ["Min1"]
}

MAX_SIGNALS = 500
signals = deque(maxlen=MAX_SIGNALS)
sessions = set()

scan_state = {
    "running": False, "enabled": True, "current_pair": "",
    "pairs_done": 0, "total_pairs": 0, "scan_count": 0,
    "signals_found": 0, "last_scan": None,
    "log": deque(maxlen=100),
}
scan_lock = threading.Lock()

monitored_setups = []
active_trades = []
trade_history = []

MEXC_BASE = "https://contract.mexc.com/api/v1/contract"
CRT_TFS   = ["Day1","Hour4","Hour3","Hour2","Min60"]
OB_TFS    = ["Hour4","Hour3","Hour2","Min60","Min45"]
TOP_PAIRS = ["BTC_USDT","ETH_USDT","SOL_USDT","BNB_USDT","XRP_USDT","DOGE_USDT"]

class MexcFuturesTrader:
    def __init__(self):
        self.base = "https://contract.mexc.com/api/v1"
        self.key = MEXC_API_KEY
        self.secret = MEXC_API_SECRET

    def _sign(self, params):
        query = '&'.join(f"{k}={v}" for k, v in sorted(params.items()))
        return hmac.new(self.secret.encode(), query.encode(), hashlib.sha256).hexdigest()

    def _req(self, method, endpoint, params=None, signed=False):
        url = f"{self.base}{endpoint}"
        headers = {"X-MEXC-APIKEY": self.key}
        params = params or {}
        if signed:
            params['timestamp'] = int(time.time() * 1000)
            params['signature'] = self._sign(params)
        try:
            if method == "GET":
                r = requests.get(url, params=params, headers=headers, timeout=10)
            else:
                r = requests.request(method, url, json=params, headers=headers, timeout=10)
            return r.json() if r.text else {}
        except Exception as e:
            log(f"MEXC Error: {e}")
            return {}

    def place_limit_order(self, symbol, side, qty, price, leverage=20):
        self._req("POST", "/position/changeLeverage", {"symbol": symbol, "leverage": leverage}, signed=True)
        params = {
            "symbol": symbol,
            "side": side.upper(),
            "type": "LIMIT",
            "vol": str(round(qty, 6)),
            "price": str(price),
            "openType": 2
        }
        result = self._req("POST", "/order/submit", params, signed=True)
        log(f"ORDER {side} {symbol} @ {price} | Success: {result.get('success')}")
        return result

trader = MexcFuturesTrader()

def log(msg):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with scan_lock:
        scan_state["log"].appendleft(line)

# ====================== PASTE YOUR ORIGINAL FULL CODE HERE ======================
# Copy everything from your original app.py (get_all_pairs, get_candles, all functions, HTML, routes, scanner_loop, etc.)

# For now, add your original code starting here ↓

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
        log(f"Pairs error: {e}")
        return []

# ... Paste the rest of your original code here (I recommend copying from your backup) ...

# Temporary scanner to prevent crash
def scanner_loop():
    log("🚀 CRT Scanner + MEXC Trading Engine Started")
    while True:
        time.sleep(30)

# ====================== ROUTES ======================
@app.route("/")
def root():
    token = request.cookies.get("session")
    if token and token in sessions:
        return make_response(DASHBOARD_HTML, 200, {"Content-Type": "text/html"})
    return make_response(LOGIN_HTML, 200, {"Content-Type": "text/html"})

@app.route("/api/active-trades")
def api_active_trades():
    return jsonify(active_trades)

def start_scanner():
    t = threading.Thread(target=scanner_loop, daemon=True)
    t.start()
    log("Bot Started")

with app.app_context():
    start_scanner()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
