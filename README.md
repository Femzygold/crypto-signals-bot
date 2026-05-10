# 📡 CryptoSignal Bot

TradingView webhook → Telegram alerts + live dashboard.

## Files (only 5!)
```
app.py            ← entire app (Flask + dashboard HTML inside)
requirements.txt  ← dependencies
Procfile          ← Railway/Heroku start command
runtime.txt       ← Python version
railway.toml      ← Railway config
```

## Deploy to Railway
1. Push this repo to GitHub
2. Go to railway.app → New Project → Deploy from GitHub
3. Add environment variables:
   - `TELEGRAM_BOT_TOKEN` — from @BotFather
   - `TELEGRAM_CHAT_ID`   — your Telegram user ID

## TradingView Webhook
Set your webhook URL to: `https://your-app.railway.app/webhook`

Alert message JSON:
```json
{
  "action":    "{{strategy.order.action}}",
  "symbol":    "{{ticker}}",
  "price":     "{{close}}",
  "timeframe": "{{interval}}",
  "message":   "Signal fired"
}
```

## API
| Route | Method | Description |
|-------|--------|-------------|
| `/` | GET | Dashboard |
| `/webhook` | POST | Receive TradingView alert |
| `/api/signals` | GET | JSON list of signals |
| `/test-signal` | POST | Send a fake test signal |
| `/health` | GET | Health check |
