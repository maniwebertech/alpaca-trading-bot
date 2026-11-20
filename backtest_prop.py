"""
Backtest the Noise Band strategy under the prop-firm rules in config_noise.py / prop_rules.py:
risk-based position size, daily loss stop, max loss stop, profit target.

    python backtest_prop.py                       # last 12 months
    python backtest_prop.py --until 2025-11-17    # the 12 months before that
    python backtest_prop.py --no-target-stop      # keep trading after the 8% target
"""

import argparse
import os
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from dotenv import load_dotenv

import config_noise as cfg
from backtest import NY, fetch
from noise_band import PRESETS, NoiseBandEngine
from prop_rules import PropRules

TICK = 0.01
SLIP = 0.01


def simulate(bars, params, rules: PropRules, start):
    eng = NoiseBandEngine(params, TICK)
    equity = rules.account_size
    pos = 0; entry = tp = sl = 0.0; pending = None
    day = None; sod = equity; realized_day = 0.0; locked = False; halted_on = None
    trades, days = [], {}
    target_on = None

    def close(ts, price, why):
        nonlocal pos, equity, realized_day
        pnl = (price - entry) * pos
        equity += pnl; realized_day += pnl
        trades.append(dict(time=ts, pnl=pnl, why=why, qty=abs(pos)))
        pos = 0

    for ts, b in bars.iterrows():
        o, h, l, c, v = b.open, b.high, b.low, b.close, b.volume
        if ts.date() != day:
            if day is not None and day_in_test:
                days[day] = dict(pnl=realized_day, low=day_low, locked=locked, sod=sod)
            day, sod, realized_day, locked, day_low = ts.date(), equity, 0.0, False, 0.0
            day_in_test = ts >= start
            if halted_on is None and day_in_test:
                why = rules.halt_reason(equity)
                if why:
                    halted_on = (day, why)
        trading = day_in_test and halted_on is None

        if pending is not None:
            kind, tp_d, sl_d, qty = pending; pending = None
            if kind == "exit" and pos:
                close(ts, o - SLIP if pos > 0 else o + SLIP, "Band exit")
            elif kind in ("long", "short") and pos == 0:
                d = 1 if kind == "long" else -1
                entry = o + SLIP * d; pos = d * qty
                tp, sl = entry + d * tp_d, entry - d * sl_d

        if pos:
            lg = pos > 0
            if (lg and o <= sl) or (not lg and o >= sl):
                close(ts, o - SLIP if lg else o + SLIP, "SL")
            elif (lg and o >= tp) or (not lg and o <= tp):
                close(ts, o, "TP")
            else:
                for ext in ((h, l) if abs(h - o) < abs(l - o) else (l, h)):
                    if ext == h:
                        if lg and h >= tp: close(ts, tp, "TP"); break
                        if not lg and h >= sl: close(ts, sl + SLIP, "SL"); break
                    else:
                        if lg and l <= sl: close(ts, sl - SLIP, "SL"); break
                        if not lg and l <= tp: close(ts, tp, "TP"); break

        day_pnl = realized_day + ((c - entry) * pos if pos else 0.0)
        day_low = min(day_low, day_pnl)
        if trading and not locked and rules.day_locked(day_pnl, sod):
            locked = True
            if pos:
                close(ts, c - SLIP if pos > 0 else c + SLIP, "Daily stop")
                day_pnl = realized_day

        dcs = eng.step(ts, o, h, l, c, v, pos)
        if dcs.action == "flatten_now" and pos:
            close(ts, c - SLIP if pos > 0 else c + SLIP, "EOD flat")
        elif dcs.action == "exit":
            pending = ("exit", 0, 0, 0)
        elif dcs.action in ("long", "short") and trading and not locked:
            qty, _ = rules.size(c, dcs.sl_dist, equity + day_pnl - realized_day, day_pnl, sod)
            if qty:
                pending = (dcs.action, dcs.tp_dist, dcs.sl_dist, qty)
        if target_on is None and trading and equity >= rules.account_size * (1 + rules.target_pct / 100):
            target_on = ts.date()
    if day_in_test:
        days[day] = dict(pnl=realized_day, low=day_low, locked=locked, sod=sod)
    return pd.DataFrame(trades), pd.DataFrame.from_dict(days, orient="index"), equity, halted_on, target_on


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default=cfg.SYMBOL)
    ap.add_argument("--months", type=int, default=12)
    ap.add_argument("--until")
    ap.add_argument("--no-target-stop", action="store_true")
    ap.add_argument("--feed", choices=["sip", "iex"], help="force a data feed (live bot uses iex)")
    args = ap.parse_args()

    load_dotenv(Path(__file__).resolve().parent / ".env")
    client = StockHistoricalDataClient(os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY"))
    day_after = (pd.Timestamp(args.until) + timedelta(days=1)) if args.until else pd.Timestamp(datetime.now())
    end = day_after.tz_localize(NY).normalize() - timedelta(minutes=1)
    start = end.normalize() - pd.DateOffset(months=args.months) + timedelta(days=1)
    feeds = (DataFeed(args.feed),) if args.feed else (DataFeed.SIP, DataFeed.IEX)
    raw, feed = fetch(client, args.symbol, TimeFrame(5, TimeFrameUnit.Minute), start - timedelta(days=75), end, feeds)
    bars = raw.between_time("09:30", "15:55")

    rules = PropRules()
    if args.no_target_stop:
        rules = replace(rules, stop_at_target=False)
    t, d, equity, halted, target_on = simulate(bars, PRESETS[cfg.PRESET], rules, start)

    acct = rules.account_size
    eq_curve = acct + d.pnl.cumsum()
    dd = (eq_curve - eq_curve.cummax().clip(lower=acct)).min()
    wins = t[t.pnl > 0]; losses = t[t.pnl <= 0]
    print(f"\n{args.symbol} {cfg.PRESET} | prop rules | {start.date()} to {end.date()} | {feed} 5-min data")
    print(f"Risk ${rules.risk_usd:,.0f}/trade | daily stop {rules.daily_stop_pct}% | "
          f"max-loss stop {rules.max_loss_stop_pct}% | target {rules.target_pct}% "
          f"({'stops trading' if rules.stop_at_target else 'keeps trading'})\n")
    print(f"Final equity        ${equity:,.0f}  ({(equity / acct - 1) * 100:+.1f}%)")
    print(f"Target {rules.target_pct}% reached  " + (f"{target_on} (after {len(d[d.index <= target_on])} trading days)" if target_on else "no"))
    if halted:
        print(f"Bot stopped on      {halted[0]}: {halted[1]}")
    print(f"Trades              {len(t)}  ({len(t) / max(1, len(d)) * 5:.1f}/week) | win {len(wins) / max(1, len(t)) * 100:.0f}% | "
          f"PF {wins.pnl.sum() / -losses.pnl.sum():.2f} | avg qty {t.qty.mean():.0f} sh")
    print(f"Avg win / loss      ${wins.pnl.mean():,.0f} / ${losses.pnl.mean():,.0f}")
    print(f"Max drawdown        ${dd:,.0f} ({dd / acct * 100:.1f}%)   firm limit -${acct * rules.firm_max_loss_pct / 100:,.0f}")
    print(f"Worst day (closed)  ${d.pnl.min():,.0f} ({d.pnl.min() / acct * 100:.2f}%)   firm limit -${acct * rules.firm_daily_pct / 100:,.0f}")
    print(f"Worst intraday dip  ${d.low.min():,.0f} ({d.low.min() / acct * 100:.2f}%)")
    print(f"Days daily stop hit {int(d.locked.sum())} of {len(d)} | days over firm 3% limit: {int((d.low <= -acct * rules.firm_daily_pct / 100).sum())}")
    print(f"Green / red days    {int((d.pnl > 0).sum())} / {int((d.pnl < 0).sum())}  (no trade: {int((d.pnl == 0).sum())})")
    m = d.copy(); m.index = pd.to_datetime(m.index)
    print("Monthly P&L         " + "  ".join(f"{k.strftime('%b%y')}:{v:+,.0f}" for k, v in m.pnl.groupby(m.index.to_period("M")).sum().items()))


if __name__ == "__main__":
    main()
