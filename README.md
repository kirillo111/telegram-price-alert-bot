# Telegram Price Alert Bot

Telegram bot that sends a message when a crypto pair reaches a target price.
Uses public exchange prices through CCXT (Bybit by default). No exchange API keys needed.

## Commands

| Command | What it does |
|---|---|
| `/alert BTC > 100000` | alert when BTC/USDT rises to 100000 |
| `/alert ETH/USDT < 2500` | alert when the price falls to 2500 |
| `/list` | your active alerts |
| `/remove 3` | delete alert #3 |
| `/price BTC` | current price |

An alert fires once, sends the message and is removed.

## Run

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export TELEGRAM_BOT_TOKEN="<token from @BotFather>"
python3 bot.py
```

Optional environment variables: `ALLOWED_CHAT_IDS` (comma-separated chat ids that may use the bot),
`EXCHANGE` (any CCXT exchange id, default `bybit`), `POLL_SECONDS` (default 30), `ALERTS_FILE`.

## Tests

```bash
pip install pytest
python3 -m pytest -q
```

## Notes

- The token is read only from the environment, never stored in code or files.
- A broken or missing `alerts.json` starts empty; writes are atomic.
- If the price check or the Telegram send fails, the alert is kept and retried on the next cycle.
