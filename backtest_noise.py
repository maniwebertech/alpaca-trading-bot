"""
Backtest the Noise Band Momentum strategy (noise_band.py) on 5-minute bars from Alpaca.

Fills follow TradingView's strategy rules used by the Pine Script:
  - signals are decided at a bar's close and filled at the NEXT bar's open (market order)
  - take profit / stop loss are live from the entry bar on; inside a bar TradingView assumes
    open -> high -> low -> close if the open is nearer the high, else open -> low -> high -> close
  - the end-of-day flatten fills at the close of the 15:50-15:55 bar
  - SLIPPAGE_TICKS on market and stop fills (the script uses 1 tick); no commission (Alpaca)

    python backtest_noise.py                       # QQQ, 12 months, all presets
    python backtest_noise.py --until 2025-11-17    # the 12 months before that date
    python backtest_noise.py --symbol SPY --qty 100 --trades
"""

import argparse
import os
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from dotenv import load_dotenv

from backtest import NY, fetch
from noise_band import PRESETS, NoiseBandEngine, Params

TICK = 0.01
SLIPPAGE_TICKS = 1
DEFAULT_QTY = 80  # ~ the dollar move of 1 MNQ contract ($2 per Nasdaq-100 point ~ 80 QQQ shares)


def simulate(bars: pd.DataFrame, p: Params, qty: int, start: pd.Timestamp):
    eng = NoiseBandEngine(p, TICK)
    slip = SLIPPAGE_TICKS * TICK
    pos, entry, tp, sl, entry_time, pending = 0, 0.0, 0.0, 0.0, None, None
    trades = []

    def close_trade(ts, price, why):
        nonlocal pos
        trades.append(dict(entry_time=entry_time, exit_time=ts, side="long" if pos > 0 else "short",
                           entry=entry, exit=price, pnl=(price - entry) * pos, why=why))
        pos = 0

    for ts, b in bars.iterrows():
        o, h, l, c, v = b["open"], b["high"], b["low"], b["close"], b["volume"]

        # 1. market orders from the previous bar fill at this open
        if pending is not None:
            kind, tp_d, sl_d = pending
            pending = None
            if kind == "exit" and pos:
                close_trade(ts, o - slip if pos > 0 else o + slip, "Band exit")
            elif kind in ("long", "short") and pos == 0 and ts >= start:
                direction = 1 if kind == "long" else -1
                entry = o + slip * direction
                pos, entry_time = direction * qty, ts
                tp, sl = entry + direction * tp_d, entry - direction * sl_d

        # 2. bracket (take profit / stop loss) inside this bar
        if pos:
            long_ = pos > 0
            if (long_ and o <= sl) or (not long_ and o >= sl):
                close_trade(ts, o - slip if long_ else o + slip, "SL")
            elif (long_ and o >= tp) or (not long_ and o <= tp):
                close_trade(ts, o, "TP")
            else:
                path = (h, l) if abs(h - o) < abs(l - o) else (l, h)
                for extreme in path:
                    if extreme == h:
                        if long_ and h >= tp:
                            close_trade(ts, tp, "TP"); break
                        if not long_ and h >= sl:
                            close_trade(ts, sl + slip, "SL"); break
                    else:
                        if long_ and l <= sl:
                            close_trade(ts, sl - slip, "SL"); break
                        if not long_ and l <= tp:
                            close_trade(ts, tp, "TP"); break

        # 3. strategy decision at this bar's close
        d = eng.step(ts, o, h, l, c, v, pos)
        if d.action == "flatten_now" and pos:
            close_trade(ts, c - slip if pos > 0 else c + slip, "EOD flat")
        elif d.action == "exit":
            pending = ("exit", 0, 0)
        elif d.action in ("long", "short"):
            pending = (d.action, d.tp_dist, d.sl_dist)

    return pd.DataFrame(trades)


def summarize(t: pd.DataFrame, weeks: float):
    if t.empty:
        return dict(trades=0)
    wins, losses = t[t.pnl > 0], t[t.pnl <= 0]
    eq = t.pnl.cumsum()
    return dict(
        trades=len(t), per_week=len(t) / weeks, win=len(wins) / len(t) * 100,
        pf=wins.pnl.sum() / -losses.pnl.sum() if len(losses) and losses.pnl.sum() < 0 else float("inf"),
        net=t.pnl.sum(), avg=t.pnl.mean(), max_dd=(eq - eq.cummax().clip(lower=0)).min(),
        longs=(t.side == "long").sum(), shorts=(t.side == "short").sum(),
        by_exit=t.why.value_counts().to_dict(),
        win_days=(t.groupby(t.exit_time.dt.date).pnl.sum() > 0).mean() * 100,
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="QQQ")
    ap.add_argument("--months", type=int, default=12)
    ap.add_argument("--until", help="last date to test, YYYY-MM-DD (default: yesterday)")
    ap.add_argument("--qty", type=int, default=DEFAULT_QTY)
    ap.add_argument("--trades", action="store_true", help="print the last 15 trades of Prop Safe")
    args = ap.parse_args()

    load_dotenv(Path(__file__).resolve().parent / ".env")
    client = StockHistoricalDataClient(os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY"))
    day_after = (pd.Timestamp(args.until) + timedelta(days=1)) if args.until else pd.Timestamp(datetime.now())
    end = day_after.tz_localize(NY).normalize() - timedelta(minutes=1)
    start = end.normalize() - pd.DateOffset(months=args.months) + timedelta(days=1)
    raw, feed = fetch(client, args.symbol, TimeFrame(5, TimeFrameUnit.Minute), start - timedelta(days=75), end)
    bars = raw.between_time("09:30", "15:55")
    weeks = (end - start).days / 7

    test = bars[bars.index >= start]
    bh = (test.close.iloc[-1] - test.open.iloc[0]) * args.qty
    print(f"\n{args.symbol} Noise Band Momentum | {start.date()} to {end.date()} | 5-min {feed} data | "
          f"{args.qty} shares | slippage {SLIPPAGE_TICKS} tick, no commission\n")
    print(f"{'Preset':14} {'Trades':>6} {'/week':>6} {'Win%':>5} {'PF':>5} {'Net $':>9} {'Avg $':>7} "
          f"{'Max DD $':>9} {'Win days':>8}  Long/Short  Exits")
    results = {}
    for name, p in PRESETS.items():
        t = simulate(bars, p, args.qty, start)
        results[name] = t
        s = summarize(t, weeks)
        if not s["trades"]:
            print(f"{name:14} no trades"); continue
        print(f"{name:14} {s['trades']:>6} {s['per_week']:>6.1f} {s['win']:>5.0f} {s['pf']:>5.2f} {s['net']:>9,.0f} "
              f"{s['avg']:>7.1f} {s['max_dd']:>9,.0f} {s['win_days']:>7.0f}%  {s['longs']:>4}/{s['shorts']:<5} {s['by_exit']}")
    print(f"\nBuy and hold {args.qty} {args.symbol} over the same period: ${bh:,.0f} "
          f"({(test.close.iloc[-1] / test.open.iloc[0] - 1) * 100:.1f}%)")

    if args.trades:
        t = results["Prop Safe"].tail(15)
        print("\nLast 15 Prop Safe trades:")
        for _, r in t.iterrows():
            print(f"  {r.entry_time:%Y-%m-%d %H:%M} -> {r.exit_time:%H:%M}  {r.side:5}  {r.entry:8.2f} -> {r.exit:8.2f}  "
                  f"{r.pnl:+8.0f}  {r.why}")


if __name__ == "__main__":
    main()
