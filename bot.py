"""
Moving average crossover trading bot.

1. Downloads the last LOOKBACK_DAYS daily bars for SYMBOL from yfinance.
2. Calculates the SHORT_WINDOW and LONG_WINDOW simple moving averages.
3. Checks the two most recent completed days for a real crossover:
     - short MA was at/below the long MA yesterday and is above it today  -> BUY
     - short MA was at/above the long MA yesterday and is below it today  -> SELL
4. Sends a market order to the Alpaca paper trading account.

Usage:
    python bot.py                # run now (uses DRY_RUN from config.py)
    python bot.py --dry-run      # run now, never send an order
    python bot.py --scheduled    # used by Windows Task Scheduler: waits for
                                 # 9:30 AM New York time, skips holidays

Logs are written to logs/bot_YYYY-MM-DD.log (one file per New York trading day).
"""

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf
from dotenv import load_dotenv

import config

NY_TZ = ZoneInfo("America/New_York")
BASE_DIR = Path(__file__).resolve().parent   # works no matter which folder we're started from
LOG_DIR = BASE_DIR / "logs"

# Scheduled mode: only run if started between these New York times.
# The scheduler starts the bot early (GitHub Actions can start late), then the bot waits for 9:30.
SCHEDULE_EARLIEST = (8, 45)
SCHEDULE_OPEN = (9, 30)
SCHEDULE_LATEST = (9, 55)

log = logging.getLogger("ma_bot")


def setup_logging() -> None:
    LOG_DIR.mkdir(exist_ok=True)
    log_file = LOG_DIR / f"bot_{datetime.now(NY_TZ):%Y-%m-%d}.log"
    handlers = [logging.FileHandler(log_file, encoding="utf-8")]
    if sys.stdout is not None:  # pythonw.exe (hidden window) has no console
        handlers.append(logging.StreamHandler(sys.stdout))
    # Log times in New York time (ET), whether running on your PC or on GitHub's UTC servers.
    formatter = logging.Formatter("%(asctime)s ET  %(levelname)-7s %(message)s")
    formatter.converter = lambda ts: datetime.fromtimestamp(ts, NY_TZ).timetuple()
    for handler in handlers:
        handler.setFormatter(formatter)
    logging.basicConfig(level=logging.INFO, handlers=handlers)


# --------------------------------------------------------------------------- #
# Data
# --------------------------------------------------------------------------- #
def fetch_prices(symbol: str, lookback_days: int) -> pd.Series:
    """Return the closing prices of the last `lookback_days` completed trading days."""
    # Ask for a year so we always have enough rows, then keep the last N trading days.
    hist = yf.Ticker(symbol).history(period="1y", interval="1d", auto_adjust=True)
    if hist.empty:
        raise RuntimeError(f"yfinance returned no data for {symbol}")

    closes = hist["Close"].dropna()

    # Drop today's bar while the market is still open: it is not a finished day
    # and its moving averages would change (and could flip a signal) until 4 PM ET.
    now_ny = datetime.now(NY_TZ)
    last_bar_date = closes.index[-1].date()
    if last_bar_date == now_ny.date() and now_ny.hour < 16:
        log.info("Ignoring today's unfinished bar (%s).", last_bar_date)
        closes = closes.iloc[:-1]

    return closes.tail(lookback_days)


# --------------------------------------------------------------------------- #
# Strategy
# --------------------------------------------------------------------------- #
def add_moving_averages(closes: pd.Series, short_window: int, long_window: int) -> pd.DataFrame:
    df = pd.DataFrame({"close": closes})
    df["ma_short"] = df["close"].rolling(short_window).mean()
    df["ma_long"] = df["close"].rolling(long_window).mean()
    return df.dropna()


def detect_crossover(df: pd.DataFrame) -> str | None:
    """Compare the last two days. Returns 'buy', 'sell' or None."""
    if len(df) < 2:
        raise RuntimeError("Not enough data to compare two days of moving averages.")

    prev, curr = df.iloc[-2], df.iloc[-1]

    if prev["ma_short"] <= prev["ma_long"] and curr["ma_short"] > curr["ma_long"]:
        return "buy"
    if prev["ma_short"] >= prev["ma_long"] and curr["ma_short"] < curr["ma_long"]:
        return "sell"
    return None


# --------------------------------------------------------------------------- #
# Alpaca
# --------------------------------------------------------------------------- #
def get_trading_client():
    from alpaca.trading.client import TradingClient

    load_dotenv(BASE_DIR / ".env")
    api_key = os.getenv("ALPACA_API_KEY")
    secret_key = os.getenv("ALPACA_SECRET_KEY")
    paper = os.getenv("ALPACA_PAPER", "true").strip().lower() != "false"

    if not api_key or not secret_key or "your_" in api_key or "your_" in secret_key:
        raise RuntimeError("Alpaca keys missing. Put your paper keys in the .env file.")

    return TradingClient(api_key, secret_key, paper=paper), paper


def current_position_qty(client, symbol: str) -> float:
    """Shares currently held (negative if short, 0 if none)."""
    try:
        return float(client.get_open_position(symbol).qty)
    except Exception:  # Alpaca raises an APIError when there is no position
        return 0.0


def place_order(client, symbol: str, qty: float, side: str):
    from alpaca.common.exceptions import APIError
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import MarketOrderRequest

    # One order ID per symbol, side and New York day: if the bot is accidentally run
    # twice on the same day, Alpaca rejects the second order instead of trading twice.
    client_order_id = f"ma-{symbol}-{side}-{datetime.now(NY_TZ):%Y%m%d}"
    try:
        order = client.submit_order(
            MarketOrderRequest(
                symbol=symbol,
                qty=qty,
                side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
                client_order_id=client_order_id,
            )
        )
    except APIError as exc:
        if "client_order_id" in str(exc).lower() and "unique" in str(exc).lower():
            log.info("A %s order for %s was already sent today (%s). Not sending another.",
                     side.upper(), symbol, client_order_id)
            return None
        raise
    log.info("Order sent: %s %s %s | id=%s status=%s", side.upper(), qty, symbol, order.id, order.status)
    return order


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def run(dry_run: bool) -> None:
    symbol = config.SYMBOL
    if config.SHORT_WINDOW >= config.LONG_WINDOW:
        raise ValueError("SHORT_WINDOW must be smaller than LONG_WINDOW in config.py")
    if config.LOOKBACK_DAYS < config.LONG_WINDOW + 1:
        raise ValueError("LOOKBACK_DAYS must be at least LONG_WINDOW + 1 in config.py")

    log.info("=== %s | MA %d/%d | qty %s | %s ===", symbol, config.SHORT_WINDOW,
             config.LONG_WINDOW, config.TRADE_QTY, "DRY RUN" if dry_run else "LIVE ORDERS (paper)")

    # Connect to Alpaca first, so every log shows the keys work and the account state.
    try:
        client, paper = get_trading_client()
    except RuntimeError:
        if not dry_run:
            raise
        client, paper = None, True
        log.info("No Alpaca keys found. Dry run continues without the account check.")
    if client is not None:
        acct = client.get_account()
        log.info("Alpaca %s account %s...: status=%s cash=%s equity=%s",
                 "PAPER" if paper else "LIVE", str(acct.account_number)[:3], acct.status.value
                 if hasattr(acct.status, "value") else acct.status, acct.cash, acct.equity)
        if not paper:
            log.warning("ALPACA_PAPER=false: this is a LIVE account.")

    closes = fetch_prices(symbol, config.LOOKBACK_DAYS)
    log.info("Downloaded %d trading days (%s to %s).", len(closes),
             closes.index[0].date(), closes.index[-1].date())

    df = add_moving_averages(closes, config.SHORT_WINDOW, config.LONG_WINDOW)
    prev, curr = df.iloc[-2], df.iloc[-1]
    for label, row in (("Previous", prev), ("Latest", curr)):
        log.info("%-8s %s  close=%.2f  MA%d=%.2f  MA%d=%.2f", label, row.name.date(), row["close"],
                 config.SHORT_WINDOW, row["ma_short"], config.LONG_WINDOW, row["ma_long"])

    signal = detect_crossover(df)
    if signal is None:
        log.info("No crossover. Nothing to do.")
        return
    log.info("Crossover detected: %s signal.", signal.upper())

    if client is None:
        log.info("Dry run without Alpaca keys: would %s %s %s.", signal, config.TRADE_QTY, symbol)
        return

    held = current_position_qty(client, symbol)
    log.info("Current position: %s shares of %s.", held, symbol)

    qty = config.TRADE_QTY
    if signal == "buy" and held > 0:
        log.info("Already holding %s. Skipping buy so a re-run doesn't buy twice.", symbol)
        return
    if signal == "sell" and not config.ALLOW_SHORT:
        if held <= 0:
            log.info("No shares to sell and ALLOW_SHORT is False. Skipping.")
            return
        qty = min(qty, held)

    if dry_run:
        log.info("DRY RUN: would send %s %s %s.", signal.upper(), qty, symbol)
        return

    place_order(client, symbol, qty, signal)


def wait_for_market_open() -> bool:
    """Scheduled mode: check the New York time, wait until 9:30, skip holidays.
    Returns True if the bot should run now."""
    now = datetime.now(NY_TZ)
    earliest = now.replace(hour=SCHEDULE_EARLIEST[0], minute=SCHEDULE_EARLIEST[1], second=0, microsecond=0)
    open_at = now.replace(hour=SCHEDULE_OPEN[0], minute=SCHEDULE_OPEN[1], second=5, microsecond=0)
    latest = now.replace(hour=SCHEDULE_LATEST[0], minute=SCHEDULE_LATEST[1], second=0, microsecond=0)

    # The task fires at two local times (one per US daylight-saving season); only one matches.
    if not (earliest <= now <= latest):
        print(f"Scheduled start at {now:%H:%M} New York time is outside the "
              f"{SCHEDULE_EARLIEST[0]}:{SCHEDULE_EARLIEST[1]:02d}-{SCHEDULE_LATEST[0]}:{SCHEDULE_LATEST[1]:02d} window. Exiting.")
        return False
    if now.weekday() >= 5:
        print("Weekend in New York. Exiting.")
        return False

    setup_logging()
    if now < open_at:
        wait = (open_at - now).total_seconds()
        log.info("Scheduled run started. Waiting %.0f s until 9:30 AM New York time.", wait)
        time.sleep(wait)

    client, _ = get_trading_client()
    clock = client.get_clock()
    if not clock.is_open:
        log.info("Market is closed today (holiday or early closure). Next open: %s. Exiting.", clock.next_open)
        return False
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Moving average crossover bot for Alpaca paper trading")
    parser.add_argument("--dry-run", action="store_true", help="show the decision but never send an order")
    parser.add_argument("--scheduled", action="store_true",
                        help="for Task Scheduler: wait for 9:30 AM New York time and skip holidays")
    args = parser.parse_args()

    try:
        if args.scheduled:
            if not wait_for_market_open():
                return
        else:
            setup_logging()
        run(dry_run=args.dry_run or config.DRY_RUN)
    except Exception:
        if not logging.getLogger().handlers:
            setup_logging()
        log.exception("Bot stopped with an error:")
        sys.exit(1)


if __name__ == "__main__":
    main()
