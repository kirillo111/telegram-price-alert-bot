"""Telegram price alert bot (public exchange data via CCXT).

Commands:
    /alert BTC > 100000      alert when BTC/USDT rises to 100000
    /alert ETH/USDT < 2500   alert when the price falls to 2500
    /list                    your active alerts
    /remove 3                delete alert #3
    /price BTC               current price

Secrets come from environment variables only:
    TELEGRAM_BOT_TOKEN   token from @BotFather (required)
    ALLOWED_CHAT_IDS     comma-separated chat ids allowed to use the bot (optional)
    EXCHANGE             CCXT exchange id, default bybit
    POLL_SECONDS         check interval, default 30
    ALERTS_FILE          where alerts are stored, default alerts.json
No exchange API keys are needed: only public prices are used.
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Awaitable, Callable

log = logging.getLogger("alert-bot")

MAX_ALERTS_PER_CHAT = 20
ALERT_RE = re.compile(
    r"^\s*([A-Za-z0-9]{2,15})(?:/([A-Za-z0-9]{2,10}))?\s*(>=|<=|>|<)\s*"
    r"([0-9]+(?:[.,][0-9]+)?)\s*$"
)


# ---------- pure logic (covered by tests) ----------

def parse_alert(text: str) -> tuple[str, str, float] | None:
    """'BTC > 100000' -> ('BTC/USDT', '>', 100000.0). Bad format -> None."""
    m = ALERT_RE.match(text)
    if not m:
        return None
    base, quote, op, num = m.groups()
    target = float(num.replace(",", "."))
    if target <= 0:
        return None
    symbol = f"{base.upper()}/{(quote or 'USDT').upper()}"
    return symbol, op[0], target  # '>=' -> '>', '<=' -> '<'


def is_triggered(op: str, target: float, price: float) -> bool:
    return price >= target if op == ">" else price <= target


def fmt_num(x: float) -> str:
    """75506.5 -> '75,506.5'; 1000000.0 -> '1,000,000'; never scientific notation."""
    return f"{x:,.8f}".rstrip("0").rstrip(".")


class AlertStore:
    """Alerts in a JSON file. Writes are atomic so a crash cannot corrupt the file."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.alerts: list[dict] = []
        self.next_id = 1
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.alerts = data["alerts"]
            self.next_id = data["next_id"]
        except (ValueError, KeyError, OSError):
            log.exception("alerts file is broken, starting empty")

    def _save(self) -> None:
        payload = json.dumps({"alerts": self.alerts, "next_id": self.next_id})
        fd, tmp = tempfile.mkstemp(dir=self.path.parent or ".", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
        os.replace(tmp, self.path)

    def for_chat(self, chat_id: int) -> list[dict]:
        return [a for a in self.alerts if a["chat_id"] == chat_id]

    def add(self, chat_id: int, symbol: str, op: str, target: float) -> dict:
        alert = {"id": self.next_id, "chat_id": chat_id,
                 "symbol": symbol, "op": op, "target": target}
        self.next_id += 1
        self.alerts.append(alert)
        self._save()
        return alert

    def remove(self, chat_id: int, alert_id: int) -> bool:
        before = len(self.alerts)
        self.alerts = [a for a in self.alerts
                       if not (a["id"] == alert_id and a["chat_id"] == chat_id)]
        changed = len(self.alerts) != before
        if changed:
            self._save()
        return changed


async def run_checks(store: AlertStore, exchange,
                     send: Callable[[int, str], Awaitable[None]]) -> int:
    """One check cycle: fetch each symbol's price once, send triggered alerts and
    delete them. Returns the number of alerts that fired."""
    fired = 0
    for symbol in sorted({a["symbol"] for a in store.alerts}):
        try:
            price = (await exchange.fetch_ticker(symbol))["last"]
        except Exception as exc:  # network, rate limit, delisted pair
            log.warning("ticker %s failed: %s", symbol, exc)
            continue
        if price is None:
            continue
        for a in [x for x in store.alerts if x["symbol"] == symbol]:
            if is_triggered(a["op"], a["target"], price):
                text = (f"Alert #{a['id']} triggered: {symbol} {a['op']} "
                        f"{fmt_num(a['target'])}\nPrice now: {fmt_num(price)}")
                try:
                    await send(a["chat_id"], text)
                except Exception as exc:
                    log.warning("send failed: %s", exc)
                    continue  # keep the alert, retry on the next cycle
                store.remove(a["chat_id"], a["id"])
                fired += 1
    return fired


# ---------- Telegram ----------

def build_app(token: str, store: AlertStore, exchange, allowed: set[int],
              poll_seconds: int):
    from telegram import Update
    from telegram.ext import Application, CommandHandler, ContextTypes

    def allowed_chat(update: Update) -> bool:
        return not allowed or update.effective_chat.id in allowed

    async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not allowed_chat(update):
            return
        await update.message.reply_text(
            "Crypto price alerts.\n"
            "/alert BTC > 100000\n/alert ETH/USDT < 2500\n"
            "/list  /remove <id>  /price BTC")

    async def alert(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not allowed_chat(update):
            return
        chat_id = update.effective_chat.id
        parsed = parse_alert(" ".join(ctx.args))
        if not parsed:
            await update.message.reply_text("Usage: /alert BTC > 100000")
            return
        if len(store.for_chat(chat_id)) >= MAX_ALERTS_PER_CHAT:
            await update.message.reply_text(
                f"Limit is {MAX_ALERTS_PER_CHAT} alerts. Delete some with /remove <id>.")
            return
        symbol, op, target = parsed
        try:  # also confirms the pair exists
            price = (await exchange.fetch_ticker(symbol))["last"]
        except Exception:
            await update.message.reply_text(f"Pair {symbol} not found on the exchange.")
            return
        a = store.add(chat_id, symbol, op, target)
        await update.message.reply_text(
            f"Alert #{a['id']} set: {symbol} {op} {fmt_num(target)}\n"
            f"Price now: {fmt_num(price)}")

    async def list_(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not allowed_chat(update):
            return
        mine = store.for_chat(update.effective_chat.id)
        text = "\n".join(
            f"#{a['id']}  {a['symbol']} {a['op']} {fmt_num(a['target'])}"
            for a in mine) or "No active alerts."
        await update.message.reply_text(text)

    async def remove(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not allowed_chat(update):
            return
        try:
            alert_id = int(ctx.args[0])
        except (IndexError, ValueError):
            await update.message.reply_text("Usage: /remove 3")
            return
        ok = store.remove(update.effective_chat.id, alert_id)
        await update.message.reply_text("Alert deleted." if ok else "No such alert.")

    async def price(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not allowed_chat(update):
            return
        parsed = parse_alert((ctx.args[0] if ctx.args else "") + " > 1")
        if not parsed:
            await update.message.reply_text("Usage: /price BTC")
            return
        try:
            last = (await exchange.fetch_ticker(parsed[0]))["last"]
        except Exception:
            await update.message.reply_text(f"Pair {parsed[0]} not found.")
            return
        await update.message.reply_text(f"{parsed[0]}: {fmt_num(last)}")

    async def poll(ctx: ContextTypes.DEFAULT_TYPE):
        async def send(chat_id: int, text: str):
            await ctx.bot.send_message(chat_id, text)
        await run_checks(store, exchange, send)

    async def shutdown(_app):
        await exchange.close()

    app = Application.builder().token(token).post_shutdown(shutdown).build()
    for name, fn in [("start", start), ("alert", alert), ("list", list_),
                     ("remove", remove), ("price", price)]:
        app.add_handler(CommandHandler(name, fn))
    app.job_queue.run_repeating(poll, interval=poll_seconds, first=5)
    return app


def main() -> None:
    import ccxt.async_support as ccxt

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    # httpx logs full request URLs, and the bot token is part of them: silence it
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN (the token from @BotFather).")
    allowed = {int(x) for x in os.environ.get("ALLOWED_CHAT_IDS", "").split(",")
               if x.strip()}
    exchange = getattr(ccxt, os.environ.get("EXCHANGE", "bybit"))(
        {"enableRateLimit": True})
    store = AlertStore(Path(os.environ.get("ALERTS_FILE", "alerts.json")))
    app = build_app(token, store, exchange, allowed,
                    int(os.environ.get("POLL_SECONDS", "30")))
    app.run_polling()


if __name__ == "__main__":
    main()
