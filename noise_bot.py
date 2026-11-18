"""
Live Noise Band Momentum bot for QQQ on Alpaca, with prop-firm risk rules.

Runs during the New York session. Every 5 minutes, right after a bar closes, it rebuilds the
strategy state from Alpaca 5-minute bars (same engine as the backtest) and acts on the decision:
  - entry   -> market order with a bracket (take profit + stop loss held by Alpaca)
  - exit    -> cancel the bracket and close the position
  - 15:55   -> flatten everything
Every 30 seconds it also checks the account against the daily loss stop / max loss / target.

    python noise_bot.py --until 16:01           # trade until 16:01 New York time
    python noise_bot.py --until 12:40           # first half of the day (GitHub job 1)
    python noise_bot.py --check                 # evaluate the latest bar once, send nothing
    python noise_bot.py --until 16:01 --dry-run # full day, log decisions only

Logs: logs/noise_YYYY-MM-DD.log (New York date, times in ET).
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
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderClass, OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import GetOrdersRequest, MarketOrderRequest, StopLossRequest, TakeProfitRequest
from dotenv import load_dotenv

import config_noise as cfg
from noise_band import PRESETS, NoiseBandEngine
from prop_rules import PropRules

NY = ZoneInfo("America/New_York")
BASE = Path(__file__).resolve().parent
TICK = 0.01
HISTORY_DAYS = 75          # calendar days of 5-min bars to rebuild the noise memory, ATR and trend
EARLIEST_START = (8, 55)   # started earlier than this (wrong daylight-saving trigger) -> exit
LATEST_START = (15, 58)    # started later than this -> nothing left to do today
BAR_DELAY = 20             # seconds after a bar closes before reading it (lets the bar finalize)

log = logging.getLogger("noise_bot")


def setup_logging():
    (BASE / "logs").mkdir(exist_ok=True)
    handlers = [logging.FileHandler(BASE / "logs" / f"noise_{datetime.now(NY):%Y-%m-%d}.log", encoding="utf-8")]
    if sys.stdout is not None:
        handlers.append(logging.StreamHandler(sys.stdout))
    fmt = logging.Formatter("%(asctime)s ET  %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    fmt.converter = lambda ts: datetime.fromtimestamp(ts, NY).timetuple()
    for h in handlers:
        h.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=handlers)


def publish_log():
    """On GitHub Actions: commit the logs folder and push, so the log can be read during the day.
    Does nothing on your PC. Never stops the bot if it fails."""
    if os.getenv("GITHUB_ACTIONS") != "true":
        return
    import subprocess
    cmds = [
        ["git", "config", "user.name", "trading-bot"],
        ["git", "config", "user.email", "trading-bot@users.noreply.github.com"],
        ["git", "add", "logs"],
        ["git", "commit", "-q", "-m", f"Noise bot log {datetime.now(NY):%Y-%m-%d %H:%M} ET (hourly)"],
        ["git", "pull", "-q", "--rebase", "--autostash"],
        ["git", "push", "-q"],
    ]
    for c in cmds:
        r = subprocess.run(c, cwd=BASE, capture_output=True, text=True, timeout=60)
        if r.returncode != 0 and c[1] == "commit":
            return  # nothing new to save
        if r.returncode != 0:
            log.warning("Log upload step %s failed: %s", " ".join(c[:2]), (r.stderr or r.stdout).strip()[:200])
            return
    log.info("Log uploaded to GitHub.")


class Bot:
    def __init__(self, dry_run: bool):
        load_dotenv(BASE / ".env")
        key, secret = os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY")
        paper = os.getenv("ALPACA_PAPER", "true").strip().lower() != "false"
        if not key or not secret:
            raise RuntimeError("Alpaca keys missing (.env locally, GitHub Secrets on GitHub).")
        self.trade = TradingClient(key, secret, paper=paper)
        self.data = StockHistoricalDataClient(key, secret)
        self.paper, self.dry = paper, dry_run or cfg.DRY_RUN
        self.sym = cfg.SYMBOL
        self.params = PRESETS[cfg.PRESET]
        self.rules = PropRules()
        self.feed = DataFeed.IEX          # free plan: recent SIP data is not allowed
        self.history = None
        self.last_bar = None
        self.day_locked_logged = False
        self.halted_logged = False

    # ------------------------------------------------------------------ data
    def bars(self, start, end) -> pd.DataFrame:
        req = StockBarsRequest(symbol_or_symbols=self.sym, timeframe=TimeFrame(5, TimeFrameUnit.Minute),
                               start=start, end=end, adjustment=Adjustment.ALL, feed=self.feed)
        df = self.data.get_stock_bars(req).df
        if df.empty:
            return df
        df = df.xs(self.sym, level="symbol")
        df.index = df.index.tz_convert(NY)
        return df[["open", "high", "low", "close", "volume"]].between_time("09:30", "15:55")

    def load_history(self):
        today = datetime.now(NY).replace(hour=0, minute=0, second=0, microsecond=0)
        self.history = self.bars(today - timedelta(days=HISTORY_DAYS), today)
        log.info("Loaded %d history bars (%s to %s), feed=%s", len(self.history),
                 self.history.index[0].date(), self.history.index[-1].date(), self.feed.value)

    def completed_bars(self) -> pd.DataFrame:
        now = datetime.now(NY)
        today_start = now.replace(hour=9, minute=30, second=0, microsecond=0)
        today = self.bars(today_start, now) if now >= today_start else self.history.iloc[0:0]
        today = today[today.index + timedelta(minutes=5) <= now - timedelta(seconds=5)]  # finished bars only
        return pd.concat([self.history, today])

    # --------------------------------------------------------------- account
    def account(self):
        a = self.trade.get_account()
        equity, sod = float(a.equity), float(a.last_equity)   # last_equity = equity at the previous close
        return equity, sod, equity - sod

    def position_qty(self) -> int:
        try:
            return int(float(self.trade.get_open_position(self.sym).qty))
        except Exception:
            return 0

    def flatten(self, why: str):
        qty = self.position_qty()
        open_orders = self.trade.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[self.sym]))
        if not qty and not open_orders:
            return
        if self.dry:
            log.info("DRY RUN: would flatten %s %s (%s)", qty, self.sym, why)
            return
        for o in open_orders:
            try:
                self.trade.cancel_order_by_id(o.id)
            except Exception as exc:
                log.warning("Could not cancel order %s: %s", o.id, exc)
        if qty:
            time.sleep(1)
            self.trade.close_position(self.sym)
            log.info("FLATTENED %s shares of %s: %s", qty, self.sym, why)

    # ---------------------------------------------------------------- guards
    def check_guards(self) -> bool:
        """Returns True when new entries are allowed. Closes positions if a stop is hit."""
        equity, sod, day_pnl = self.account()
        halt = self.rules.halt_reason(equity)
        if halt:
            if not self.halted_logged:
                log.warning("TRADING STOPPED: %s", halt)
                self.halted_logged = True
            self.flatten(halt)
            return False
        if self.rules.day_locked(day_pnl, sod):
            if not self.day_locked_logged:
                log.warning("DAILY STOP HIT: day P&L %+.0f <= -%.0f. Closing and stopping for today.",
                            day_pnl, self.rules.daily_stop_usd(sod))
                self.day_locked_logged = True
            self.flatten("daily loss stop")
            return False
        return True

    # ------------------------------------------------------------- strategy
    def evaluate(self):
        bars = self.completed_bars()
        if bars.empty or bars.index[-1] == self.last_bar:
            return
        self.last_bar = bars.index[-1]
        pos = self.position_qty()
        eng = NoiseBandEngine(self.params, TICK)
        rows = list(bars.itertuples())
        for r in rows[:-1]:                               # rebuild state from history
            eng.step(r.Index, r.open, r.high, r.low, r.close, r.volume, 0)
        r = rows[-1]
        d = eng.step(r.Index, r.open, r.high, r.low, r.close, r.volume, pos)

        # status line for every finished 5-minute bar
        fmt = lambda x: "-" if x is None else f"{x:.2f}"
        equity, sod, day_pnl = self.account()
        log.info("Bar %s close=%.2f | band %s / %s | VWAP %s | 20d SMA %s | pos %d | day P&L %+.0f | equity %.0f%s",
                 r.Index.strftime("%H:%M"), r.close, fmt(d.upper), fmt(d.lower), fmt(d.vwap), fmt(d.trend_sma),
                 pos, day_pnl, equity, " | CHECK" if d.is_check else "")

        if d.action == "flatten_now":
            self.flatten(d.reason)
        elif d.action == "exit":
            self.flatten(d.reason)
        elif d.action in ("long", "short"):
            self.enter(d, r)
        elif d.is_check:
            log.info("Check %s: no signal.", (r.Index + timedelta(minutes=5)).strftime("%H:%M"))

    def enter(self, d, r):
        if not self.check_guards():
            log.info("Signal %s skipped: account stop active.", d.action.upper())
            return
        equity, sod, day_pnl = self.account()
        qty, why = self.rules.size(r.close, d.sl_dist, equity, day_pnl, sod)
        if not qty:
            log.info("Signal %s skipped: %s", d.action.upper(), why)
            return
        side = OrderSide.BUY if d.action == "long" else OrderSide.SELL
        sgn = 1 if d.action == "long" else -1
        tp = round(r.close + sgn * d.tp_dist, 2)
        sl = round(r.close - sgn * d.sl_dist, 2)
        check_time = (r.Index + timedelta(minutes=5)).strftime("%Y%m%d-%H%M")
        log.info("SIGNAL %s (%s): %s | TP %.2f (+%.2f) SL %.2f (-%.2f)",
                 d.action.upper(), d.reason, why, tp, d.tp_dist, sl, d.sl_dist)
        if self.dry:
            log.info("DRY RUN: order not sent.")
            return
        try:
            o = self.trade.submit_order(MarketOrderRequest(
                symbol=self.sym, qty=qty, side=side, time_in_force=TimeInForce.DAY,
                order_class=OrderClass.BRACKET,
                take_profit=TakeProfitRequest(limit_price=tp), stop_loss=StopLossRequest(stop_price=sl),
                client_order_id=f"nb-{self.sym}-{check_time}"))
            log.info("ORDER SENT: %s %d %s bracket | id %s | status %s", side.value.upper(), qty, self.sym, o.id, o.status)
        except Exception as exc:
            if "client_order_id" in str(exc).lower():
                log.info("Order for this check was already sent by another run. Skipping.")
            else:
                log.error("Order failed: %s", exc)

    # ---------------------------------------------------------------- report
    def hourly_report(self):
        try:
            equity, sod, day_pnl = self.account()
            today = datetime.now(NY).replace(hour=0, minute=0, second=0, microsecond=0)
            fills = [o for o in self.trade.get_orders(GetOrdersRequest(
                status=QueryOrderStatus.CLOSED, symbols=[self.sym], after=today, limit=200))
                if o.filled_at is not None]
            log.info("HOURLY REPORT | equity %.0f | day P&L %+.0f (%+.2f%%) | position %d | fills today %d | "
                     "daily stop room $%.0f", equity, day_pnl, day_pnl / sod * 100 if sod else 0,
                     self.position_qty(), len(fills), self.rules.daily_stop_usd(sod) + day_pnl)
        except Exception as exc:
            log.warning("Hourly report failed: %s", exc)
        publish_log()

    # ------------------------------------------------------------------ main
    def run(self, until_hm: tuple[int, int]):
        clock = self.trade.get_clock()
        now = datetime.now(NY)
        if not clock.is_open and clock.next_open.astimezone(NY).date() != now.date():
            log.info("Market closed today (holiday). Next open %s. Exiting.", clock.next_open)
            return
        equity, sod, day_pnl = self.account()
        log.info("=== Noise Band bot | %s %s | %s | %s | until %02d:%02d ET ===", self.sym, cfg.PRESET,
                 "PAPER" if self.paper else "LIVE", "DRY RUN" if self.dry else "ORDERS ON", *until_hm)
        log.info("Equity %.0f | start of day %.0f | risk/trade $%.0f | daily stop -$%.0f | halt below $%.0f | target $%.0f",
                 equity, sod, self.rules.risk_usd, self.rules.daily_stop_usd(sod),
                 self.rules.account_size * (1 - self.rules.max_loss_stop_pct / 100),
                 self.rules.account_size * (1 + self.rules.target_pct / 100))
        if now.time() < datetime.strptime("09:31", "%H:%M").time() and self.position_qty():
            log.warning("Position left over from a previous day: closing it at the open.")
        self.load_history()

        end = now.replace(hour=until_hm[0], minute=until_hm[1], second=0, microsecond=0)
        last_guard = 0.0
        last_report_hour = now.hour
        while True:
            now = datetime.now(NY)
            if now >= end:
                break
            mins = now.hour * 60 + now.minute
            # hourly report, uploaded to GitHub (at :02, clear of the bar evaluation at :00)
            if now.hour != last_report_hour and now.minute >= 2:
                last_report_hour = now.hour
                self.hourly_report()
            if 9 * 60 + 30 <= mins < 16 * 60:
                if time.time() - last_guard >= 30:
                    last_guard = time.time()
                    try:
                        self.check_guards()
                    except Exception as exc:
                        log.error("Guard check failed: %s", exc)
                if mins == 9 * 60 + 30 and now.second < 40 and self.position_qty():
                    self.flatten("position left over from a previous day")
                # safety net inside the session: nothing may be open after 15:58 (trading hours end 16:00)
                if mins == 15 * 60 + 58 and now.second < 30:
                    self.flatten("end of session safety check (15:58)")
                # right after each 5-minute bar closes
                if now.minute % 5 == 0 and BAR_DELAY <= now.second < BAR_DELAY + 25:
                    try:
                        self.evaluate()
                    except Exception:
                        log.exception("Evaluation failed:")
            time.sleep(5)

        if end.hour * 60 + end.minute >= 15 * 60 + 56:
            if self.position_qty():
                log.error("Position still open after 16:00 - check Alpaca. Not sending orders outside 9:30-16:00.")
            equity, sod, day_pnl = self.account()
            log.info("=== Session done | day P&L %+.0f | equity %.0f ===", day_pnl, equity)
        else:
            log.info("=== Handing over to the next job (position %d) ===", self.position_qty())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--until", default="16:01", help="stop at this New York time, HH:MM")
    ap.add_argument("--check", action="store_true", help="evaluate the latest finished bar once and exit (dry run)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    now = datetime.now(NY)
    if not args.check:
        hm = (now.hour, now.minute)
        if now.weekday() >= 5:
            print("Weekend in New York. Exiting."); return
        if hm < EARLIEST_START or hm >= LATEST_START:
            print(f"Started at {now:%H:%M} New York time, outside {EARLIEST_START[0]}:{EARLIEST_START[1]:02d}-"
                  f"{LATEST_START[0]}:{LATEST_START[1]:02d}. Exiting."); return

    setup_logging()
    try:
        bot = Bot(dry_run=args.dry_run or args.check)
        if args.check:
            equity, sod, day_pnl = bot.account()
            log.info("=== CHECK RUN | %s %s | equity %.0f | position %d ===", bot.sym, cfg.PRESET, equity, bot.position_qty())
            bot.load_history()
            bot.evaluate()
            log.info("Check run finished (no orders sent).")
            return
        h, m = map(int, args.until.split(":"))
        bot.run((h, m))
    except Exception:
        log.exception("Bot stopped with an error:")
        sys.exit(1)


if __name__ == "__main__":
    main()
