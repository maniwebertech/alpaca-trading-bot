"""
Gold Breakout bot: "Gold Breakout 15M Strategy [WillyAlgoTrader]" traded on GLD, around the clock (24/5).
Uses its OWN Alpaca paper account (keys in .env.gold), separate from the QQQ bots.

How it maps the TradingView strategy to Alpaca:
  - Range high / low between RANGE_START_HOUR and RANGE_END_HOUR (broker time), from 15-minute bars
    (SIP 4:00-20:00 ET + BOATS overnight 20:00-4:00 ET) plus the live prices the bot saw while running.
  - TradingView rests two stop orders on the range boundaries. Alpaca only accepts LIMIT orders outside
    9:30-16:00 ET, so the bot watches the price itself: when it crosses a boundary it sends a limit order
    a little beyond the bid/ask (fills at once). One trade per broker day.
  - Stop loss SL_PCT % from the actual fill (or opposite boundary), also watched by the bot. No take profit.
  - Any position is closed at CLOSE_HOUR (broker time).
  - The bot rebuilds everything from Alpaca when restarted (position, today's trade, range).

    python gold_bot.py                    # run until stopped (Ctrl+C)
    python gold_bot.py --hours 5.8        # run 5.8 hours, then exit (GitHub Actions job)
    python gold_bot.py --check            # print range / price / plan once, send nothing
    python gold_bot.py --dry-run          # run, log decisions only

Logs: logs/gold_YYYY-MM-DD.log (New York date, times in ET).
"""

import argparse
import logging
import math
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestQuoteRequest, StockLatestTradeRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import (GetOrdersRequest, GetPortfolioHistoryRequest, LimitOrderRequest,
                                     MarketOrderRequest)
from dotenv import load_dotenv

import config_gold as cfg

NY = ZoneInfo("America/New_York")
UTC = timezone.utc
BASE = Path(__file__).resolve().parent
DELAY = timedelta(minutes=16)      # free plan: historical SIP / BOATS data must be older than 15 minutes
MAX_ENTRY_TRIES = 3                # entry limit orders that did not fill before the day is given up

log = logging.getLogger("gold_bot")


# ---------------------------------------------------------------------- logging
def setup_logging():
    (BASE / "logs").mkdir(exist_ok=True)
    handlers = [logging.FileHandler(BASE / "logs" / f"gold_{datetime.now(NY):%Y-%m-%d}.log", encoding="utf-8")]
    if sys.stdout is not None:
        handlers.append(logging.StreamHandler(sys.stdout))
    fmt = logging.Formatter("%(asctime)s ET  %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    fmt.converter = lambda ts: datetime.fromtimestamp(ts, NY).timetuple()
    for h in handlers:
        h.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=handlers)


def publish_log():
    """On GitHub Actions: commit the logs folder and push. Does nothing on your PC."""
    if os.getenv("GITHUB_ACTIONS") != "true":
        return
    import subprocess
    cmds = [
        ["git", "config", "user.name", "trading-bot"],
        ["git", "config", "user.email", "trading-bot@users.noreply.github.com"],
        ["git", "add", "logs"],
        ["git", "commit", "-q", "-m", f"Gold bot log {datetime.now(NY):%Y-%m-%d %H:%M} ET (hourly)"],
        ["git", "pull", "-q", "--rebase", "--autostash"],
        ["git", "push", "-q"],
    ]
    for c in cmds:
        r = subprocess.run(c, cwd=BASE, capture_output=True, text=True, timeout=60)
        if r.returncode != 0 and c[1] == "commit":
            return
        if r.returncode != 0:
            log.warning("Log upload step %s failed: %s", " ".join(c[:2]), (r.stderr or r.stdout).strip()[:200])
            return
    log.info("Log uploaded to GitHub.")


# ------------------------------------------------------------------ broker time
def to_broker(t: datetime) -> datetime:
    """UTC/aware time -> naive broker clock time."""
    if cfg.BROKER_TIME_MODE == "ny7":
        return t.astimezone(NY).replace(tzinfo=None) + timedelta(hours=7)
    return t.astimezone(ZoneInfo(cfg.BROKER_TZ)).replace(tzinfo=None)


def from_broker(b: datetime) -> datetime:
    """Naive broker clock time -> aware UTC."""
    if cfg.BROKER_TIME_MODE == "ny7":
        return (b - timedelta(hours=7)).replace(tzinfo=NY).astimezone(UTC)
    return b.replace(tzinfo=ZoneInfo(cfg.BROKER_TZ)).astimezone(UTC)


def phase_of(b: datetime) -> str:
    if cfg.RANGE_START_HOUR <= b.hour < cfg.RANGE_END_HOUR:
        return "range"
    if cfg.RANGE_END_HOUR <= b.hour < cfg.CLOSE_HOUR:
        return "trade"
    return "closed"


def is_regular_session(t: datetime) -> bool:
    et = t.astimezone(NY)
    return et.weekday() < 5 and (9, 30) <= (et.hour, et.minute) < (16, 0)


def is_overnight_session(t: datetime) -> bool:
    et = t.astimezone(NY)
    return et.hour >= 20 or et.hour < 4


@dataclass
class Quote:
    bid: float
    ask: float
    ts: datetime
    source: str
    realtime: bool

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def spread_pct(self) -> float:
        return (self.ask - self.bid) / self.mid * 100 if self.mid else 99.0


@dataclass
class Day:
    key: str                                   # broker date YYYYMMDD
    date: datetime                             # broker midnight (naive)
    live_hi: float | None = None               # live prices seen during the range window
    live_lo: float | None = None
    live_last: datetime | None = None
    range_hi: float | None = None
    range_lo: float | None = None
    range_logged: bool = False
    traded: bool = False                       # one trade per day
    entry_tries: int = 0
    halted: bool = False                       # equity guard / prop stop fired
    start_equity: float | None = None          # equity at broker midnight (prop daily loss base)
    equity_peak: float | None = None
    notes: set = field(default_factory=set)    # messages already logged today

    @property
    def range_start(self) -> datetime:
        return from_broker(self.date + timedelta(hours=cfg.RANGE_START_HOUR))

    @property
    def range_end(self) -> datetime:
        return from_broker(self.date + timedelta(hours=cfg.RANGE_END_HOUR))


class GoldBot:
    def __init__(self, dry_run: bool):
        load_dotenv(BASE / ".env.gold")
        key, secret = os.getenv("GOLD_ALPACA_API_KEY"), os.getenv("GOLD_ALPACA_SECRET_KEY")
        if not key or not secret or "your_" in key:
            raise RuntimeError("Gold account keys missing: put GOLD_ALPACA_API_KEY / GOLD_ALPACA_SECRET_KEY "
                               "in .env.gold (locally) or GitHub Secrets.")
        self.paper = os.getenv("GOLD_ALPACA_PAPER", "true").strip().lower() != "false"
        self.trade = TradingClient(key, secret, paper=self.paper)
        self.data = StockHistoricalDataClient(key, secret)
        self.dry = dry_run or cfg.DRY_RUN
        self.sym = cfg.SYMBOL
        self.day: Day | None = None
        self.sl: float | None = None               # active stop of the open position
        self.last_status = None
        self.last_guard = 0.0
        self.last_prop = 0.0

    # ------------------------------------------------------------------ data
    def quote(self) -> Quote | None:
        """Freshest real-time quote, or a 15-min delayed price (realtime=False) when none exists."""
        now = datetime.now(UTC)
        if cfg.DATA_PLAN == "plus":
            feeds = [DataFeed.BOATS] if is_overnight_session(now) else [DataFeed.SIP]
        else:
            feeds = [DataFeed.OVERNIGHT] if is_overnight_session(now) else [DataFeed.IEX]
        for f in feeds:
            try:
                q = self.data.get_stock_latest_quote(StockLatestQuoteRequest(symbol_or_symbols=self.sym, feed=f))[self.sym]
            except Exception as exc:
                log.debug("quote %s failed: %s", f.value, exc)
                continue
            age = (now - q.timestamp).total_seconds()
            if q.bid_price > 0 and q.ask_price > 0 and q.ask_price >= q.bid_price and age <= cfg.MAX_QUOTE_AGE_SEC:
                return Quote(float(q.bid_price), float(q.ask_price), q.timestamp, f.value, True)
        # no real-time price: delayed SIP / overnight trade (good enough only to watch an open stop)
        for f in (DataFeed.DELAYED_SIP, DataFeed.OVERNIGHT):
            try:
                t = self.data.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=self.sym, feed=f))[self.sym]
            except Exception:
                continue
            if (now - t.timestamp) <= timedelta(minutes=30):
                p = float(t.price)
                return Quote(p, p, t.timestamp, f"{f.value}-delayed", False)
        return None

    def bars_range(self, start: datetime, end: datetime) -> tuple[float | None, float | None, datetime | None]:
        """High / low of 15-minute bars that START in [start, end). Returns (hi, lo, covered_until)."""
        now = datetime.now(UTC)
        stop = min(end, now - (timedelta(minutes=1) if cfg.DATA_PLAN == "plus" else DELAY))
        if stop <= start:
            return None, None, None
        hi = lo = covered = None
        for f in (DataFeed.SIP, DataFeed.BOATS):
            try:
                df = self.data.get_stock_bars(StockBarsRequest(
                    symbol_or_symbols=self.sym, timeframe=TimeFrame(15, TimeFrameUnit.Minute),
                    start=start, end=stop, feed=f)).df
            except Exception as exc:
                key = f"bars-{f.value}"
                if key not in self.day.notes:
                    self.day.notes.add(key)
                    log.warning("Could not read %s bars: %s", f.value, str(exc)[:150])
                continue
            if df.empty:
                continue
            df = df.xs(self.sym, level="symbol")
            df = df[(df.index >= start) & (df.index < end)]
            if df.empty:
                continue
            hi = max(hi, float(df.high.max())) if hi is not None else float(df.high.max())
            lo = min(lo, float(df.low.min())) if lo is not None else float(df.low.min())
            last = df.index.max().to_pydatetime() + timedelta(minutes=15)
            covered = max(covered, last) if covered else last
        return hi, lo, covered

    # --------------------------------------------------------------- account
    def position(self) -> tuple[float, float]:
        """(signed qty, average entry price)"""
        try:
            p = self.trade.get_open_position(self.sym)
            return float(p.qty), float(p.avg_entry_price)
        except Exception:
            return 0.0, 0.0

    def equity(self) -> float:
        return float(self.trade.get_account().equity)

    def cancel_open_orders(self):
        for o in self.trade.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[self.sym])):
            try:
                self.trade.cancel_order_by_id(o.id)
            except Exception as exc:
                log.warning("Could not cancel order %s: %s", o.id, exc)

    def wait_fill(self, order_id, seconds: float):
        o = None
        for _ in range(int(seconds / 0.5)):
            o = self.trade.get_order_by_id(order_id)
            if o.status.value in ("filled", "canceled", "expired", "rejected"):
                break
            time.sleep(0.5)
        return o

    # ------------------------------------------------------------------- day
    def new_day(self, b: datetime):
        d = Day(key=b.strftime("%Y%m%d"), date=b.replace(hour=0, minute=0, second=0, microsecond=0))
        # rebuild "already traded today" from Alpaca (bot may have restarted)
        prefix = f"gb-{self.sym}-{d.key}-"
        try:
            orders = self.trade.get_orders(GetOrdersRequest(
                status=QueryOrderStatus.ALL, symbols=[self.sym], after=d.range_start - timedelta(hours=1), limit=200))
            mine = [o for o in orders if (o.client_order_id or "").startswith(prefix) and "-E" in o.client_order_id]
            d.entry_tries = len(mine)
            d.traded = any(float(o.filled_qty or 0) > 0 for o in mine) or d.entry_tries >= MAX_ENTRY_TRIES
        except Exception as exc:
            log.warning("Could not read today's orders: %s", exc)
        d.start_equity = self.day_start_equity(d)
        self.day = d
        log.info("Prop | %s | day start equity %.0f | bot daily stop at %.0f (firm %.0f) | bot halts below %.0f "
                 "(firm %.0f) | target %.0f%s", cfg.PHASE, d.start_equity, self.daily_floor(), self.daily_floor(firm=True),
                 self.max_floor(), self.max_floor(firm=True), self.target_equity(),
                 "" if self.stop_at_target() else " (keeps trading)")
        log.info("Broker day %s | range %s-%s ET | trading until %s ET%s", d.key,
                 d.range_start.astimezone(NY).strftime("%a %H:%M"), d.range_end.astimezone(NY).strftime("%a %H:%M"),
                 from_broker(d.date + timedelta(hours=cfg.CLOSE_HOUR)).astimezone(NY).strftime("%a %H:%M"),
                 " | already traded today" if d.traded else "")

    # ------------------------------------------------------------ prop rules
    def day_start_equity(self, d: Day) -> float:
        """Account equity at broker midnight (start of the prop firm's trading day)."""
        midnight = from_broker(d.date)
        try:
            h = self.trade.get_portfolio_history(GetPortfolioHistoryRequest(
                timeframe="15Min", start=midnight - timedelta(hours=3), end=midnight + timedelta(minutes=1),
                extended_hours=True, intraday_reporting="continuous"))
            pts = [(t, e) for t, e in zip(h.timestamp or [], h.equity or []) if e]
            before = [e for t, e in pts if t <= midnight.timestamp()]
            if before:
                return float(before[-1])
        except Exception as exc:
            log.warning("Could not read portfolio history (%s); using current equity as the day start.", str(exc)[:120])
        return self.equity()

    def daily_floor(self, firm: bool = False) -> float:
        pct = cfg.FIRM_DAILY_LOSS_PCT if firm else cfg.DAILY_STOP_PCT
        base = self.day.start_equity
        # firms base the daily limit on the starting balance or the day's opening equity: use the smaller amount
        return base - min(cfg.ACCOUNT_SIZE, base) * pct / 100

    def max_floor(self, firm: bool = False) -> float:
        return cfg.ACCOUNT_SIZE * (1 - (cfg.FIRM_MAX_LOSS_PCT if firm else cfg.MAX_LOSS_STOP_PCT) / 100)

    def target_equity(self) -> float:
        return cfg.ACCOUNT_SIZE * (1 + cfg.PHASE_TARGET_PCT[cfg.PHASE] / 100)

    def stop_at_target(self) -> bool:
        return cfg.STOP_AT_TARGET and cfg.PHASE != "pro"

    def prop_block(self, equity: float) -> str | None:
        """Reason why no position may be open / no trade taken, or None."""
        if equity <= self.max_floor():
            return f"MAX LOSS STOP: equity {equity:,.0f} <= {self.max_floor():,.0f} (firm fails at {self.max_floor(True):,.0f})"
        if equity <= self.daily_floor():
            return (f"DAILY STOP: equity {equity:,.0f} <= {self.daily_floor():,.0f} "
                    f"(day start {self.day.start_equity:,.0f}, firm limit {self.daily_floor(True):,.0f})")
        if self.stop_at_target() and equity >= self.target_equity():
            return f"TARGET REACHED: equity {equity:,.0f} >= {self.target_equity():,.0f} ({cfg.PHASE}). Trading stopped."
        return None

    def ensure_range(self, now: datetime) -> bool:
        """Fixes today's range once all of it is known. Returns True when ready."""
        d = self.day
        if d.range_hi is not None:
            return True
        hi, lo, covered = self.bars_range(d.range_start, d.range_end)
        if d.live_hi is not None:
            hi = max(hi, d.live_hi) if hi is not None else d.live_hi
            lo = min(lo, d.live_lo) if lo is not None else d.live_lo
        # complete when bars reach the end, or the bot itself watched the last minutes of the window
        tail_live = d.live_last is not None and d.live_last >= d.range_end - timedelta(minutes=16)
        tail_bars = covered is not None and covered >= d.range_end
        delayed_done = now >= d.range_end + (timedelta(minutes=1) if cfg.DATA_PLAN == "plus" else DELAY)
        if not (tail_live or tail_bars or delayed_done):
            if "wait-range" not in d.notes:
                d.notes.add("wait-range")
                log.info("Range: waiting for the delayed bars of the last 15 minutes (bot was not watching then).")
            return False
        if hi is None or lo is None or hi <= lo:
            if "no-range" not in d.notes:
                d.notes.add("no-range")
                log.info("No range data for %s (market closed?). No trade today.", d.key)
            d.traded = True
            return False
        d.range_hi, d.range_lo = round(hi, 2), round(lo, 2)
        sl_l, sl_s = self.stops_from(d.range_hi, d.range_lo)
        log.info("RANGE %s: high %.2f  low %.2f  width %.2f (%.2f%%) | long above %.2f (SL %.2f), short below %.2f (SL %.2f)%s",
                 d.key, d.range_hi, d.range_lo, d.range_hi - d.range_lo, (d.range_hi - d.range_lo) / d.range_lo * 100,
                 d.range_hi, sl_l, d.range_lo, sl_s, "" if cfg.ALLOW_SHORT else " [shorts off]")
        return True

    def stops_from(self, long_ref: float, short_ref: float) -> tuple[float, float]:
        if cfg.SL_MODE == "range":
            return self.day.range_lo, self.day.range_hi
        return round(long_ref * (1 - cfg.SL_PCT / 100), 2), round(short_ref * (1 + cfg.SL_PCT / 100), 2)

    # --------------------------------------------------------------- trading
    def size(self, entry_ref: float, sl: float, equity: float) -> int:
        risk = abs(entry_ref - sl)
        if cfg.SIZE_MODE == "fixed":
            raw = cfg.FIXED_QTY
        elif cfg.SIZE_MODE == "risk_usd":
            raw = cfg.RISK_PER_TRADE_USD / risk if risk > 0 else 0
        else:
            raw = equity * cfg.RISK_PCT / 100 / risk if risk > 0 else 0
        cap = equity * cfg.MAX_LEVERAGE / entry_ref
        return int(math.floor(min(raw, cap) + 1e-9))   # whole shares: shorts cannot be fractional

    def enter(self, long: bool, q: Quote):
        d = self.day
        ref = d.range_hi if long else d.range_lo
        sl_l, sl_s = self.stops_from(d.range_hi, d.range_lo)
        sl_ref = sl_l if long else sl_s
        equity = self.equity()
        qty = self.size(ref, sl_ref, equity)
        side = "LONG" if long else "SHORT"
        risk_usd = qty * abs(ref - sl_ref)
        floor = max(self.daily_floor(), self.max_floor())
        if equity - risk_usd * 1.2 < floor:      # 20% margin for slippage / gaps through the stop
            log.info("BREAKOUT %s skipped: a full stop-out ($%.0f + 20%% slippage) would go below the prop stop "
                     "%.0f (equity %.0f). No trade today.", side, risk_usd, floor, equity)
            d.traded = True
            return
        if qty < 1:
            log.info("BREAKOUT %s but size rounds to 0 shares. No trade today.", side)
            d.traded = True
            return
        px = round(q.ask * (1 + cfg.LIMIT_OFFSET_PCT / 100), 2) if long else round(q.bid * (1 - cfg.LIMIT_OFFSET_PCT / 100), 2)
        log.info("BREAKOUT %s: price %.2f (bid %.2f / ask %.2f, %s) crossed %.2f | %d shares, limit %.2f | "
                 "planned risk $%.0f (%.2f%% of equity %.0f)", side, q.mid, q.bid, q.ask, q.source, ref, qty, px,
                 qty * abs(ref - sl_ref), qty * abs(ref - sl_ref) / equity * 100, equity)
        if self.dry:
            log.info("DRY RUN: order not sent.")
            d.traded = True
            return
        d.entry_tries += 1
        cid = f"gb-{self.sym}-{d.key}-E{d.entry_tries}"
        try:
            o = self.trade.submit_order(LimitOrderRequest(
                symbol=self.sym, qty=qty, side=OrderSide.BUY if long else OrderSide.SELL, limit_price=px,
                time_in_force=TimeInForce.DAY, extended_hours=True, client_order_id=cid))
        except Exception as exc:
            if "client_order_id" in str(exc).lower():
                log.info("Entry %s already sent by another run.", cid)
                d.traded = True
            else:
                log.error("Entry order rejected: %s", exc)
                if d.entry_tries >= MAX_ENTRY_TRIES:
                    d.traded = True
            return
        o = self.wait_fill(o.id, 20)
        filled = float(o.filled_qty or 0)
        if o.status.value != "filled":
            try:
                self.trade.cancel_order_by_id(o.id)
            except Exception:
                pass
            time.sleep(1)
            o = self.trade.get_order_by_id(o.id)
            filled = float(o.filled_qty or 0)
        if filled <= 0:
            log.warning("Entry limit %.2f did not fill in 20 s (try %d/%d).", px, d.entry_tries, MAX_ENTRY_TRIES)
            if d.entry_tries >= MAX_ENTRY_TRIES:
                log.warning("Giving up today's breakout.")
                d.traded = True
            return
        d.traded = True
        fill = float(o.filled_avg_price)
        if cfg.SL_MODE == "pct":   # TradingView re-sets the stop from the actual fill
            self.sl = round(fill * (1 - cfg.SL_PCT / 100), 2) if long else round(fill * (1 + cfg.SL_PCT / 100), 2)
        else:
            self.sl = sl_ref
        log.info("FILLED %s %g %s @ %.2f (breakout level %.2f, diff %+.2f) | STOP %.2f | risk $%.0f",
                 side, filled, self.sym, fill, ref, fill - ref, self.sl, filled * abs(fill - self.sl))

    def exit(self, qty: float, why: str, q: Quote | None):
        long = qty > 0
        now = datetime.now(UTC)
        if self.dry:
            log.info("DRY RUN: would close %g %s (%s).", qty, self.sym, why)
            return
        self.cancel_open_orders()
        time.sleep(0.5)
        side = OrderSide.SELL if long else OrderSide.BUY
        n = abs(qty)
        try:
            if is_regular_session(now):
                o = self.trade.submit_order(MarketOrderRequest(symbol=self.sym, qty=n, side=side,
                                                               time_in_force=TimeInForce.DAY))
                kind = "market"
            else:
                if q is None:
                    log.error("Must close (%s) but no price available. Will retry.", why)
                    return
                off = cfg.LIMIT_OFFSET_PCT / 100 * (1 if q.realtime else 3)
                px = round(q.bid * (1 - off), 2) if long else round(q.ask * (1 + off), 2)
                o = self.trade.submit_order(LimitOrderRequest(symbol=self.sym, qty=n, side=side, limit_price=px,
                                                              time_in_force=TimeInForce.DAY, extended_hours=True))
                kind = f"limit {px:.2f}"
        except Exception as exc:
            log.error("Exit order rejected (%s): %s. Will retry.", why, exc)
            return
        o = self.wait_fill(o.id, 20)
        if o.status.value == "filled":
            log.info("CLOSED %g %s @ %.2f (%s, %s)", n, self.sym, float(o.filled_avg_price), why, kind)
            self.sl = None
        else:
            try:
                self.trade.cancel_order_by_id(o.id)
            except Exception:
                pass
            log.warning("Exit %s not filled in 20 s (%s, filled %s). Retrying with a new price.",
                        kind, o.status.value, o.filled_qty)

    # ------------------------------------------------------------------ loop
    def step(self):
        now = datetime.now(UTC)
        b = to_broker(now)
        if self.day is None or self.day.key != b.strftime("%Y%m%d"):
            self.new_day(b)
        d = self.day
        ph = phase_of(b)
        q = self.quote()
        qty, avg = self.position()
        rt_ok = q is not None and q.realtime and q.spread_pct <= cfg.MAX_SPREAD_PCT

        if ph == "range" and rt_ok:
            d.live_hi = q.mid if d.live_hi is None else max(d.live_hi, q.mid)
            d.live_lo = q.mid if d.live_lo is None else min(d.live_lo, q.mid)
            d.live_last = now

        # prop firm stops: checked every 30 s, close everything when hit
        if time.time() - self.last_prop >= 30:
            self.last_prop = time.time()
            eq = self.equity()
            why = self.prop_block(eq)
            if why:
                if not d.halted:
                    log.warning("%s Closing any position, no more trades %s.", why,
                                "today" if why.startswith("DAILY") else "until you change the settings")
                d.halted = d.traded = True
                if qty:
                    self.exit(qty, why.split(":")[0].lower(), q)
                    return
            elif cfg.PHASE == "pro" and "consistency" not in d.notes:
                total, day = eq - cfg.ACCOUNT_SIZE, eq - d.start_equity
                if total > 0 and day > 0 and day / total * 100 > cfg.CONSISTENCY_PCT:
                    d.notes.add("consistency")
                    log.warning("Consistency: today's profit %.0f is %.0f%% of total profit %.0f (> %.0f%%).",
                                day, day / total * 100, total, cfg.CONSISTENCY_PCT)

        # equity guard (off by default, as in TradingView)
        if cfg.GUARD_ON and time.time() - self.last_guard >= 30:
            self.last_guard = time.time()
            eq = self.equity()
            d.equity_peak = max(d.equity_peak or eq, eq)
            if not d.halted and (d.equity_peak - eq) / d.equity_peak * 100 >= cfg.GUARD_DD_PCT:
                log.warning("EQUITY GUARD: equity %.0f is %.1f%% below today's peak %.0f. Closing, no more trades today.",
                            eq, (d.equity_peak - eq) / d.equity_peak * 100, d.equity_peak)
                d.halted = d.traded = True
                if qty:
                    self.exit(qty, "equity guard", q)
                    return

        if qty:
            if self.sl is None:          # restarted with an open position: rebuild the stop
                if cfg.SL_MODE == "pct":
                    self.sl = round(avg * (1 - cfg.SL_PCT / 100), 2) if qty > 0 else round(avg * (1 + cfg.SL_PCT / 100), 2)
                elif self.ensure_range(now):
                    self.sl = d.range_lo if qty > 0 else d.range_hi
                log.info("Open position %g %s @ %.2f found | stop %s", qty, self.sym, avg,
                         f"{self.sl:.2f}" if self.sl else "unknown")
            if ph != "trade":
                self.exit(qty, f"time exit {cfg.CLOSE_HOUR}:00 broker time", q)
            elif q is not None and self.sl is not None:
                price = q.mid
                if (qty > 0 and price <= self.sl) or (qty < 0 and price >= self.sl):
                    log.info("STOP HIT: price %.2f (%s) vs stop %.2f", price, q.source, self.sl)
                    self.exit(qty, "stop loss", q)
                elif not q.realtime and "delayed-stop" not in d.notes:
                    d.notes.add("delayed-stop")
                    log.warning("Only 15-min delayed prices right now (%s): the stop is watched late.", q.source)
        elif ph == "trade" and not d.traded and self.ensure_range(now):
            if rt_ok:
                if q.mid >= d.range_hi:
                    self.enter(True, q)
                elif cfg.ALLOW_SHORT and q.mid <= d.range_lo:
                    self.enter(False, q)
            elif q is not None and q.realtime and "wide" not in d.notes:
                d.notes.add("wide")
                log.info("Spread %.2f%% > %.2f%%: waiting for a tighter market before any entry.",
                         q.spread_pct, cfg.MAX_SPREAD_PCT)

        self.status(now, b, ph, q, qty)

    def status(self, now, b, ph, q, qty):
        slot = now.replace(minute=now.minute - now.minute % 15, second=0, microsecond=0)
        if slot == self.last_status:
            return
        self.last_status = slot
        d = self.day
        rng = f"{d.range_lo:.2f}-{d.range_hi:.2f}" if d.range_hi else (
            f"building {d.live_lo:.2f}-{d.live_hi:.2f} (live)" if d.live_hi else "-")
        px = f"{q.mid:.2f} ({q.source}{'' if q.realtime else ''}, spread {q.spread_pct:.2f}%)" if q else "no data"
        try:
            eq = f"{self.equity():.0f}"
        except Exception:
            eq = "?"
        log.info("Status | broker %s %s | phase %s | price %s | range %s | pos %g%s | %s | equity %s",
                 b.strftime("%a %H:%M"), d.key, ph, px, rng, qty,
                 f" stop {self.sl:.2f}" if qty and self.sl else "",
                 "done today" if d.traded else "waiting", eq)

    def run(self, hours: float | None):
        a = self.trade.get_account()
        log.info("=== Gold Breakout bot | %s | %s | %s | data %s | equity %.0f | %s ===", self.sym,
                 "PAPER" if self.paper else "LIVE", "DRY RUN" if self.dry else "ORDERS ON", cfg.DATA_PLAN,
                 float(a.equity), f"for {hours} h" if hours else "until stopped")
        log.info("Range %02d:00-%02d:00, close %02d:00 broker time (%s) | SL %s | risk %s | max leverage %.1fx | shorts %s",
                 cfg.RANGE_START_HOUR, cfg.RANGE_END_HOUR, cfg.CLOSE_HOUR,
                 "NY+7h" if cfg.BROKER_TIME_MODE == "ny7" else cfg.BROKER_TZ,
                 f"{cfg.SL_PCT}% of entry" if cfg.SL_MODE == "pct" else "range boundary",
                 f"${cfg.RISK_PER_TRADE_USD:.0f} per trade" if cfg.SIZE_MODE == "risk_usd" else
                 f"{cfg.RISK_PCT}% of equity" if cfg.SIZE_MODE == "risk_pct" else f"{cfg.FIXED_QTY} shares",
                 cfg.MAX_LEVERAGE, "on" if cfg.ALLOW_SHORT else "off")
        end = time.time() + hours * 3600 if hours else None
        last_publish = datetime.now(NY).hour
        while end is None or time.time() < end:
            try:
                self.step()
            except Exception:
                log.exception("Step failed:")
                time.sleep(10)
            hn = datetime.now(NY)
            if hn.hour != last_publish and hn.minute >= 2:
                last_publish = hn.hour
                publish_log()
            time.sleep(cfg.POLL_SECONDS)
        qty, _ = self.position()
        log.info("=== Run finished (position %g). The next run takes over. ===", qty)

    def check(self):
        now = datetime.now(UTC)
        b = to_broker(now)
        self.new_day(b)
        q = self.quote()
        qty, avg = self.position()
        log.info("CHECK | broker time %s | phase %s | equity %.0f | position %g @ %.2f", b.strftime("%a %Y-%m-%d %H:%M"),
                 phase_of(b), self.equity(), qty, avg)
        log.info("Price: %s", f"bid {q.bid:.2f} ask {q.ask:.2f} mid {q.mid:.2f} spread {q.spread_pct:.2f}% "
                              f"({q.source}, {q.ts.astimezone(NY):%H:%M:%S} ET)" if q else "none")
        # range of the current broker day, or of the last finished one when still building
        hi, lo, cov = self.bars_range(self.day.range_start, self.day.range_end)
        if hi is None and phase_of(b) != "trade":
            prev = Day(key="prev", date=self.day.date - timedelta(days=1))
            hi, lo, cov = self.bars_range(prev.range_start, prev.range_end)
            log.info("Previous day's range (%s-%s ET):", prev.range_start.astimezone(NY).strftime("%a %H:%M"),
                     prev.range_end.astimezone(NY).strftime("%a %H:%M"))
        if hi is not None:
            self.day.range_hi, self.day.range_lo = hi, lo
            sl_l, sl_s = self.stops_from(hi, lo)
            eq = self.equity()
            log.info("Range high %.2f low %.2f (bars up to %s ET) | long > %.2f SL %.2f qty %d | short < %.2f SL %.2f qty %d",
                     hi, lo, cov.astimezone(NY).strftime("%a %H:%M"), hi, sl_l, self.size(hi, sl_l, eq),
                     lo, sl_s, self.size(lo, sl_s, eq))
        else:
            log.info("No range bars yet.")
        log.info("Check finished (no orders sent).")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=None, help="run this many hours, then exit")
    ap.add_argument("--check", action="store_true", help="show range / price / plan once, send nothing")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    setup_logging()
    try:
        bot = GoldBot(dry_run=args.dry_run or args.check)
        if args.check:
            bot.check()
        else:
            bot.run(args.hours)
    except KeyboardInterrupt:
        log.info("Stopped by user.")
    except Exception:
        log.exception("Bot stopped with an error:")
        sys.exit(1)


if __name__ == "__main__":
    main()
