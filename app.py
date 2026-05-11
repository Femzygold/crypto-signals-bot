"""
Crypto Trading Signal App
TradingView Webhook → Telegram Bot + Live Dashboard
All HTML/CSS/JS is inline — no templates folder needed!
"""

import os
import json
import requests
from datetime import datetime
from flask import Flask, request, jsonify
from collections import deque

app = Flask(__name__)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8668028976:AAE2u1in1KGr1nRTJbaQXNPeDtMO35unoQ8")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "7411219487")
WEBHOOK_SECRET     = os.environ.get("WEBHOOK_SECRET", "")
MAX_SIGNALS        = 100

signals: deque = deque(maxlen=MAX_SIGNALS)


# ── Telegram ────────────────────────────────────────────────────────────────

def send_telegram(message: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        app.logger.warning("Telegram credentials not set.")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
        }, timeout=10)
        resp.raise_for_status()
        return True
    except requests.RequestException as e:
        app.logger.error(f"Telegram error: {e}")
        return False


def format_telegram_message(signal: dict) -> str:
    emoji  = "🟢" if signal.get("action") == "BUY" else "🔴"
    action = signal.get("action", "N/A")
    symbol = signal.get("symbol", "N/A")
    price  = signal.get("price", "N/A")
    tf     = signal.get("timeframe", "N/A")
    msg    = signal.get("message", "")
    ts     = signal.get("timestamp", "")
    lines  = [
        f"{emoji} <b>TRADING SIGNAL</b> {emoji}",
        "",
        f"<b>Action:</b>    {action}",
        f"<b>Symbol:</b>    {symbol}",
        f"<b>Price:</b>     {price}",
        f"<b>Timeframe:</b> {tf}",
    ]
    if msg: lines.append(f"<b>Note:</b>      {msg}")
    if ts:  lines.append(f"<b>Time:</b>      {ts}")
    lines.append("\n<i>Powered by CryptoSignal Bot 🤖</i>")
    return "\n".join(lines)


# ── Dashboard HTML (all inline, no templates folder) ────────────────────────

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1.0"/>
<title>CryptoSignal Dashboard</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{--bg:#0b0e17;--panel:#111520;--panel2:#161b29;--border:#1e2640;--blue:#3b82f6;--green:#10b981;--red:#ef4444;--yellow:#f59e0b;--text:#e2e8f0;--muted:#64748b;--dim:#94a3b8}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:var(--bg);color:var(--text);min-height:100vh;padding-bottom:50px}
/* Header */
.hdr{background:var(--panel);border-bottom:1px solid var(--border);position:sticky;top:0;z-index:100}
.hdr-in{max-width:1000px;margin:0 auto;padding:0 16px;height:56px;display:flex;align-items:center;justify-content:space-between}
.logo{display:flex;align-items:center;gap:9px;font-size:1.05rem;font-weight:800}
.live-badge{background:var(--red);color:#fff;font-size:.58rem;font-weight:800;padding:2px 6px;border-radius:4px;letter-spacing:.05em;animation:blink 2s infinite}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.4}}
.hdr-right{display:flex;align-items:center;gap:10px}
.upd{font-size:.72rem;color:var(--muted)}
.dot{width:9px;height:9px;border-radius:50%;background:var(--muted);transition:background .3s}
.dot.ok{background:var(--green);box-shadow:0 0 5px var(--green)}
.dot.err{background:var(--red)}
/* Stats */
.stats{max-width:1000px;margin:20px auto 0;padding:0 16px;display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px}
.stat{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:16px}
.stat.buy{border-color:rgba(16,185,129,.3)}.stat.sell{border-color:rgba(239,68,68,.3)}
.stat-lbl{font-size:.68rem;color:var(--muted);text-transform:uppercase;letter-spacing:.06em;margin-bottom:5px}
.stat-val{font-size:1.4rem;font-weight:700}
.wh-url{font-size:.72rem!important;color:var(--blue);word-break:break-all}
/* Main */
.main{max-width:1000px;margin:24px auto 0;padding:0 16px}
.row{display:flex;align-items:center;justify-content:space-between;margin-bottom:16px;gap:10px;flex-wrap:wrap}
.sec-title{font-size:.95rem;font-weight:700}
.btns{display:flex;gap:8px}
.btn{padding:7px 14px;border-radius:8px;border:none;font-family:inherit;font-size:.78rem;font-weight:700;cursor:pointer;transition:all .16s}
.btn-p{background:var(--blue);color:#fff}.btn-p:hover{background:#2563eb}
.btn-g{background:var(--panel2);color:var(--dim);border:1px solid var(--border)}.btn-g:hover{color:var(--text);border-color:var(--blue)}
/* Signal list */
.list{display:flex;flex-direction:column;gap:9px;min-height:100px}
.empty{display:flex;flex-direction:column;align-items:center;justify-content:center;padding:50px 20px;background:var(--panel);border:1px dashed var(--border);border-radius:10px;text-align:center;gap:8px}
.empty-ico{font-size:2.5rem}.empty-t{font-size:.95rem;font-weight:600}.empty-s{font-size:.8rem;color:var(--muted);max-width:320px;line-height:1.5}
/* Signal card */
.card{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:14px 18px;animation:fadein .25s ease}
.card.buy{border-left:3px solid var(--green)}.card.sell{border-left:3px solid var(--red)}
@keyframes fadein{from{opacity:0;transform:translateY(-6px)}to{opacity:1;transform:translateY(0)}}
.card-top{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:7px}
.badge{font-size:.67rem;font-weight:800;padding:3px 9px;border-radius:20px;letter-spacing:.03em}
.badge.BUY{background:rgba(16,185,129,.15);color:var(--green)}
.badge.SELL{background:rgba(239,68,68,.15);color:var(--red)}
.badge.SIGNAL{background:rgba(59,130,246,.15);color:var(--blue)}
.sym{font-size:.95rem;font-weight:700}
.price{font-size:.88rem;color:var(--yellow);font-weight:600}
.tf{font-size:.68rem;color:var(--muted);background:var(--panel2);border:1px solid var(--border);padding:2px 7px;border-radius:20px}
.card-bot{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:5px}
.card-msg{font-size:.8rem;color:var(--dim)}
.card-time{font-size:.7rem;color:var(--muted)}
/* Help */
.help{margin-top:30px;background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:22px}
.help-t{font-size:.95rem;font-weight:700;margin-bottom:13px}
.steps-list{padding-left:18px;display:flex;flex-direction:column;gap:7px;font-size:.84rem;color:var(--dim);line-height:1.6;margin-bottom:18px}
.steps-list b{color:var(--text)}
.code-wrap{display:flex;flex-direction:column;gap:5px;margin-bottom:12px}
.code-lbl{font-size:.67rem;color:var(--muted);text-transform:uppercase;letter-spacing:.06em}
.code{background:#07090f;border:1px solid var(--border);border-radius:7px;padding:13px;font-size:.78rem;color:#7dd3fc;line-height:1.7;overflow-x:auto;white-space:pre;font-family:"Fira Code",monospace}
.wh-row{display:flex;align-items:center;gap:9px;background:#07090f;border:1px solid var(--border);border-radius:7px;padding:9px 12px;flex-wrap:wrap}
.wh-code{font-size:.78rem;color:var(--blue);word-break:break-all;flex:1;font-family:monospace}
/* Toast */
.toast{position:fixed;bottom:20px;left:50%;transform:translateX(-50%) translateY(70px);background:var(--panel2);border:1px solid var(--border);border-radius:8px;padding:11px 20px;font-size:.82rem;font-weight:600;opacity:0;transition:all .3s;pointer-events:none;white-space:nowrap;z-index:999}
.toast.show{transform:translateX(-50%) translateY(0);opacity:1}
@media(max-width:560px){.stats{grid-template-columns:1fr 1fr}.hdr-in{padding:0 12px}.main{padding:0 12px}.help{padding:16px 14px}}
</style>
</head>
<body>
<div class="hdr">
  <div class="hdr-in">
    <div class="logo">📡 CryptoSignal <span class="live-badge">LIVE</span></div>
    <div class="hdr-right">
      <span class="upd" id="upd">–</span>
      <div class="dot" id="dot"></div>
    </div>
  </div>
</div>

<div class="stats">
  <div class="stat"><div class="stat-lbl">Total Signals</div><div class="stat-val" id="s-total">–</div></div>
  <div class="stat buy"><div class="stat-lbl">🟢 BUY</div><div class="stat-val" id="s-buy">–</div></div>
  <div class="stat sell"><div class="stat-lbl">🔴 SELL</div><div class="stat-val" id="s-sell">–</div></div>
  <div class="stat"><div class="stat-lbl">Webhook URL</div><div class="stat-val wh-url" id="s-url">–</div></div>
</div>

<div class="main">
  <div class="row">
    <div class="sec-title">Latest Signals</div>
    <div class="btns">
      <button class="btn btn-g" id="btn-test">🧪 Test Signal</button>
      <button class="btn btn-p" id="btn-ref">↺ Refresh</button>
    </div>
  </div>

  <div class="list" id="list">
    <div class="empty">
      <div class="empty-ico">📭</div>
      <div class="empty-t">No signals yet</div>
      <div class="empty-s">Send a TradingView alert to your webhook or tap Test Signal.</div>
    </div>
  </div>

  <div class="help">
    <div class="help-t">⚡ How to connect TradingView</div>
    <ol class="steps-list">
      <li>Open any chart → click <b>Alerts</b> → <b>Create Alert</b></li>
      <li>Under Notifications → enable <b>Webhook URL</b> → paste your URL</li>
      <li>Set the <b>Message</b> field to the JSON below</li>
      <li>Save — signals appear here + in Telegram instantly!</li>
    </ol>
    <div class="code-wrap">
      <div class="code-lbl">TradingView alert message (copy this)</div>
      <div class="code">{
  "action":    "{{strategy.order.action}}",
  "symbol":    "{{ticker}}",
  "price":     "{{close}}",
  "timeframe": "{{interval}}",
  "message":   "Signal fired"
}</div>
    </div>
    <div class="code-wrap">
      <div class="code-lbl">Your Webhook URL</div>
      <div class="wh-row">
        <span class="wh-code" id="wh-code">Loading…</span>
        <button class="btn btn-g" style="padding:5px 10px;font-size:.72rem" id="btn-copy">📋 Copy</button>
      </div>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
(function(){
  const POLL=5000;
  let lastTs=null, timer=null;
  const $=id=>document.getElementById(id);
  const dot=$('dot'), upd=$('upd'), list=$('list');
  const sTotal=$('s-total'), sBuy=$('s-buy'), sSell=$('s-sell'), sUrl=$('s-url');
  const whCode=$('wh-code'), toast=$('toast');
  let toastT;

  function setWebhook(){
    const u=location.origin+'/webhook';
    sUrl.textContent=u; whCode.textContent=u;
  }
  setWebhook();

  function showToast(m,d=2800){
    toast.textContent=m; toast.classList.add('show');
    clearTimeout(toastT); toastT=setTimeout(()=>toast.classList.remove('show'),d);
  }

  function timeAgo(ts){
    if(!ts)return'';
    const d=Math.floor((Date.now()-new Date(ts.replace(' UTC','Z')))/1000);
    if(d<5)return'just now';
    if(d<60)return d+'s ago';
    if(d<3600)return Math.floor(d/60)+'m ago';
    return Math.floor(d/3600)+'h ago';
  }

  function buildCard(s){
    const action=(s.action||'SIGNAL').toUpperCase();
    const cls=action==='BUY'?'buy':action==='SELL'?'sell':'';
    const price=s.price&&s.price!=='N/A'?'$'+Number(s.price).toLocaleString():'';
    return `<div class="card ${cls}" data-ts="${s.timestamp||''}">
      <div class="card-top">
        <span class="badge ${action}">${action}</span>
        <span class="sym">${s.symbol||'–'}</span>
        <span class="price">${price}</span>
        <span class="tf">${s.timeframe||''}</span>
      </div>
      <div class="card-bot">
        <span class="card-msg">${s.message||''}</span>
        <span class="card-time">${timeAgo(s.timestamp)}</span>
      </div>
    </div>`;
  }

  function render(data){
    if(!data||!data.length){
      list.innerHTML='<div class="empty"><div class="empty-ico">📭</div><div class="empty-t">No signals yet</div><div class="empty-s">Send a TradingView alert or tap Test Signal.</div></div>';
      return;
    }
    const newest=data[0]?.timestamp;
    const isNew=newest&&newest!==lastTs;
    if(isNew){lastTs=newest;showToast('📡 New: '+data[0].action+' '+data[0].symbol);}
    list.innerHTML=data.slice(0,50).map(buildCard).join('');
  }

  async function fetchSignals(){
    try{
      const r=await fetch('/api/signals?limit=50');
      if(!r.ok)throw new Error();
      render(await r.json());
      dot.className='dot ok';
    }catch{dot.className='dot err';}
    upd.textContent='Updated '+new Date().toLocaleTimeString();
  }

  async function fetchStats(){
    try{
      const r=await fetch('/api/stats');
      if(!r.ok)return;
      const d=await r.json();
      sTotal.textContent=d.total??'–';
      sBuy.textContent=d.buys??'–';
      sSell.textContent=d.sells??'–';
    }catch{}
  }

  async function poll(){
    await fetchSignals(); await fetchStats();
    timer=setTimeout(poll,POLL);
  }

  $('btn-ref').onclick=async()=>{
    clearTimeout(timer);
    $('btn-ref').textContent='↺ …';
    await fetchSignals(); await fetchStats();
    $('btn-ref').textContent='↺ Refresh';
    timer=setTimeout(poll,POLL);
  };

  $('btn-test').onclick=async()=>{
    $('btn-test').disabled=true;
    $('btn-test').textContent='🧪 Sending…';
    try{
      const r=await fetch('/test-signal',{method:'POST'});
      const d=await r.json();
      showToast('✅ Test: '+d.signal?.action+' '+d.signal?.symbol);
      await fetchSignals(); await fetchStats();
    }catch{showToast('❌ Failed');}
    $('btn-test').disabled=false;
    $('btn-test').textContent='🧪 Test Signal';
  };

  $('btn-copy').onclick=async()=>{
    try{
      await navigator.clipboard.writeText(location.origin+'/webhook');
      showToast('📋 Webhook URL copied!');
    }catch{showToast('Long-press the URL to copy manually');}
  };

  setInterval(()=>{
    document.querySelectorAll('.card[data-ts]').forEach(c=>{
      const t=c.querySelector('.card-time');
      if(t)t.textContent=timeAgo(c.dataset.ts);
    });
  },30000);

  poll();
})();
</script>
</body>
</html>"""


# ── Routes ──────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return DASHBOARD_HTML, 200, {"Content-Type": "text/html"}


@app.route("/webhook", methods=["POST"])
def webhook():
    if WEBHOOK_SECRET:
        if request.headers.get("X-Webhook-Secret", "") != WEBHOOK_SECRET:
            return jsonify({"error": "Unauthorized"}), 401

    data = {}
    if request.is_json:
        data = request.get_json(silent=True) or {}
    else:
        raw = request.data.decode("utf-8", errors="ignore").strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = {"message": raw}

    signal = {
        "action":    data.get("action", "SIGNAL").upper(),
        "symbol":    data.get("symbol", "UNKNOWN").upper(),
        "price":     str(data.get("price", "N/A")),
        "timeframe": data.get("timeframe", "N/A"),
        "message":   data.get("message", ""),
        "timestamp": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
        "raw":       data,
    }
    signals.appendleft(signal)
    tg_ok = send_telegram(format_telegram_message(signal))
    return jsonify({"status": "ok", "signal": signal, "telegram": tg_ok}), 200


@app.route("/api/signals")
def api_signals():
    limit = min(int(request.args.get("limit", 20)), MAX_SIGNALS)
    return jsonify(list(signals)[:limit])


@app.route("/api/stats")
def api_stats():
    all_s = list(signals)
    return jsonify({
        "total": len(all_s),
        "buys":  sum(1 for s in all_s if s["action"] == "BUY"),
        "sells": sum(1 for s in all_s if s["action"] == "SELL"),
    })


@app.route("/health")
def health():
    return jsonify({"status": "healthy", "signals": len(signals)}), 200


@app.route("/test-signal", methods=["POST"])
def test_signal():
    import random
    fake = {
        "action":    random.choice(["BUY", "SELL"]),
        "symbol":    random.choice(["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"]),
        "price":     str(round(random.uniform(100, 70000), 2)),
        "timeframe": random.choice(["5m", "15m", "1H", "4H", "1D"]),
        "message":   random.choice(["RSI oversold", "MACD crossover", "EMA breakout", "Volume spike"]),
    }
    signal = {**fake, "action": fake["action"].upper(), "symbol": fake["symbol"].upper(),
              "timestamp": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"), "raw": fake}
    signals.appendleft(signal)
    tg_ok = send_telegram(format_telegram_message(signal))
    return jsonify({"status": "ok", "signal": signal, "telegram": tg_ok}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
