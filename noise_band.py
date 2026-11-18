"""
Noise Band Momentum engine - Python port of the trading logic in the Pine Script
"Xiznit Noise Band Momentum" v2.1, adapted to trade QQQ instead of MNQ.

Only the order logic is ported. Chart visuals (glow, HUD, Noise Grid, alerts) are not needed.
Add-ons that are OFF by default in the script (cooldown, V-override, news blackout,
loss guard, profit lock) are not ported.

How it works (from the script's description):
  Each session a "noise band" is built around max/min(today's open, prior session close).
  Band width = average absolute % move from the open at the same time of day over the last
  N sessions. A close outside the band at a check time (every 30 min from 10:00 to 15:00 NY)
  means the day is trending: trade that way, only with the 20-day trend. Take profit and stop
  loss are sized from the daily ATR. Flat every day at 15:55.

The same engine is used by the backtester and by the live bot, so both make the same decisions.
Feed it regular-hours bars (09:30-16:00 New York) in time order, one at a time.
"""

from collections import deque
from dataclasses import dataclass, replace
from datetime import date

import pandas as pd


@dataclass(frozen=True)
class Params:
    vm: float = 1.0                 # noise band multiplier
    lookback: int = 14              # sessions averaged per time-of-day slot
    check_every: int = 30           # minutes between entry checks
    trend_len: int = 20             # SMA of session closes (0 = off)
    trend_open: bool = False        # True = trend decided once at the open (Ultra-Safe)
    vwap_entry: bool = False        # entries also need a close beyond VWAP
    datr_len: int = 14              # daily ATR length (RMA)
    tgt_mult: float = 0.08          # take profit, x daily ATR
    stop_mult: float = 0.15         # stop loss, x daily ATR
    exit_mode: str = "band_or_vwap" # band_or_vwap | vwap | band | off
    first_check: int = 10 * 60      # minutes after midnight NY
    last_check: int = 15 * 60
    flat_at: int = 15 * 60 + 55
    max_trades: int = 12            # entries per day
    use_holiday: bool = True        # early flatten on US holiday half-days
    tf: int = 5                     # bar size in minutes
    sess_start: int = 9 * 60 + 30
    sess_end: int = 16 * 60


# Presets from the script. "Prop Safe+" also has a $300-per-MNQ-contract daily loss guard,
# which has no direct QQQ equivalent, so it is left out.
PRESETS = {
    "Prop Safe": Params(),
    "Higher Output": Params(lookback=20, check_every=15, tgt_mult=0.10),
    "Ultra-Safe": Params(trend_open=True, vwap_entry=True, exit_mode="vwap"),
}


def is_early_close(d: date) -> bool:
    """Same holiday / half-day list as the script (CME calendar). For stocks most of these
    days are full holidays (no bars at all); the half-days that matter are July 3,
    the day after Thanksgiving and Christmas Eve."""
    y, mo, dd, dw = d.year, d.month, d.day, d.isoweekday()  # 1 = Monday ... 7 = Sunday
    wk, mon = dw <= 5, dw == 1
    return any([
        mo == 1 and mon and 15 <= dd <= 21,
        mo == 2 and mon and 15 <= dd <= 21,
        mo == 5 and mon and dd >= 25,
        mo == 6 and y >= 2022 and ((dd == 19 and wk) or (dd == 18 and dw == 5) or (dd == 20 and mon)),
        mo == 7 and (((dd in (3, 4)) and wk) or (dd == 5 and mon)),
        mo == 9 and mon and dd <= 7,
        mo == 11 and ((dw == 4 and 22 <= dd <= 28) or (dw == 5 and 23 <= dd <= 29)),
        mo == 12 and dd == 24 and wk,
    ])


@dataclass
class Decision:
    action: str | None = None   # "flatten_now" | "exit" | "long" | "short" | None
    tp_dist: float = 0.0        # $ distance from entry to take profit
    sl_dist: float = 0.0        # $ distance from entry to stop loss
    reason: str = ""
    upper: float | None = None  # noise band, for logging
    lower: float | None = None
    vwap: float | None = None
    trend_sma: float | None = None
    is_check: bool = False


class NoiseBandEngine:
    def __init__(self, p: Params = Params(), tick: float = 0.01):
        self.p, self.tick = p, tick
        n_slots = (p.sess_end - p.sess_start) // p.tf
        self.mem = [deque(maxlen=p.lookback) for _ in range(n_slots)]
        self.min_obs = max(1, round(p.lookback * 10.0 / 14.0))
        self.d_open = self.pc = self.d_high = self.d_low = self.d_close = self.prev_close = self.datr = None
        self.cum_pv = self.cum_v = 0.0
        self.last_day = None
        self.trades_day = 0
        self.d_closes: list[float] = []

    def step(self, ts: pd.Timestamp, o: float, h: float, l: float, c: float, v: float, position: float) -> Decision:
        """Process one finished bar (ts = bar OPEN time, New York). `position` = shares held
        at this bar's close (+ long, - short, 0 flat). Returns what to do."""
        p = self.p
        bar_mod = ts.hour * 60 + ts.minute
        if not (p.sess_start <= bar_mod < p.sess_end):
            return Decision("flatten_now" if position else None, reason="outside session")
        close_mod = bar_mod + p.tf
        day = ts.date()

        # ---- daily state
        if day != self.last_day:
            if self.d_close is not None:
                tr = (self.d_high - self.d_low) if self.prev_close is None else max(
                    self.d_high - self.d_low, abs(self.d_high - self.prev_close), abs(self.d_low - self.prev_close))
                self.datr = tr if self.datr is None else self.datr + (tr - self.datr) / p.datr_len
                self.prev_close = self.pc = self.d_close
                self.d_closes.append(self.d_close)
                del self.d_closes[:-300]
            self.d_open, self.d_high, self.d_low = o, h, l
            self.cum_pv = self.cum_v = 0.0
            self.trades_day = 0
            self.last_day = day
        self.d_high, self.d_low, self.d_close = max(self.d_high, h), min(self.d_low, l), c
        self.cum_pv += (h + l + c) / 3 * v
        self.cum_v += v

        # ---- time-of-day noise: average of prior sessions for this slot, then store today's
        hist = self.mem[(bar_mod - p.sess_start) // p.tf]
        sigma = sum(hist) / len(hist) if len(hist) >= self.min_obs else None
        hist.append(abs(c / self.d_open - 1))

        vwap = self.cum_pv / self.cum_v if self.cum_v > 0 else None
        pc = self.pc if self.pc is not None else self.d_open
        ref_hi, ref_lo = max(self.d_open, pc), min(self.d_open, pc)
        ub = ref_hi * (1 + p.vm * sigma) if sigma is not None else None
        lb = ref_lo * (1 - p.vm * sigma) if sigma is not None else None

        # ---- trend filter
        dsma = sum(self.d_closes[-p.trend_len:]) / p.trend_len if p.trend_len and len(self.d_closes) >= p.trend_len else None
        last_dc = self.d_closes[-1] if self.d_closes else None
        if p.trend_len == 0:
            trend_up = trend_dn = True
        elif p.trend_open:
            trend_up = dsma is not None and last_dc > dsma
            trend_dn = dsma is not None and last_dc < dsma
        else:
            trend_up = dsma is not None and c > dsma
            trend_dn = dsma is not None and c < dsma

        early = p.use_holiday and is_early_close(day)
        last_mod = 11 * 60 if early else p.last_check
        flat_mod = 11 * 60 + 25 if early else p.flat_at

        is_check = close_mod % p.check_every == 0 and close_mod >= p.first_check
        can_enter = (is_check and close_mod <= last_mod and close_mod < flat_mod and ub is not None
                     and self.datr is not None and self.trades_day < p.max_trades)
        vw_ok_l = not p.vwap_entry or (vwap is not None and c > vwap)
        vw_ok_s = not p.vwap_entry or (vwap is not None and c < vwap)
        long_sig = can_enter and c > ub and trend_up and vw_ok_l
        short_sig = can_enter and c < lb and trend_dn and vw_ok_s

        ex_l = ex_s = None
        if ub is not None:
            if p.exit_mode == "vwap":
                ex_l = ex_s = vwap
            elif p.exit_mode == "band":
                ex_l, ex_s = ub, lb
            elif p.exit_mode == "band_or_vwap" and vwap is not None:
                ex_l, ex_s = max(ub, vwap), min(lb, vwap)
        exit_long = is_check and position > 0 and ex_l is not None and c < ex_l
        exit_short = is_check and position < 0 and ex_s is not None and c > ex_s

        d = Decision(upper=ub, lower=lb, vwap=vwap, trend_sma=dsma, is_check=is_check)
        if close_mod >= flat_mod and position:
            d.action, d.reason = "flatten_now", "end of day"
        elif exit_long or exit_short:
            d.action, d.reason = "exit", "band exit"
        elif position == 0 and (long_sig or short_sig):
            self.trades_day += 1
            d.action = "long" if long_sig else "short"
            d.reason = "close above upper band" if long_sig else "close below lower band"
            d.tp_dist = max(1, round(p.tgt_mult * self.datr / self.tick)) * self.tick
            d.sl_dist = max(1, round(p.stop_mult * self.datr / self.tick)) * self.tick
        return d


def preset(name: str, **overrides) -> Params:
    return replace(PRESETS[name], **overrides)
