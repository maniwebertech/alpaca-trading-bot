"""
Backtest moving average crossover settings on past data.

Rules (same as the live bot):
  - BUY when the fast MA crosses above the slow MA, SELL when it crosses below.
  - Long only: a sell just closes the position (no short selling).
  - The signal is taken at a bar's close and filled at the NEXT bar's open.
  - Only regular trading hours (9:30-16:00 New York) are used for intraday bars.
  - Positions may be held overnight, like the live bot.
  - Costs: Alpaca has no commission; SLIPPAGE_PCT per side covers the bid/ask spread.

Data comes from Alpaca (your keys in .env), so every setting is tested on the same period.

    python backtest.py              # last 12 months, AAPL
    python backtest.py --months 6 --symbol MSFT
"""

import argparse
import os
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from dotenv import load_dotenv

NY = "America/New_York"
SLIPPAGE_PCT = 0.02  # % lost per side to the spread

# (label, bar size, fast, slow, MA type)
SETTINGS = [
    ("Current: 20/50 SMA, daily", "1D", 20, 50, "SMA"),
    ("5/20 SMA, daily", "1D", 5, 20, "SMA"),
    ("9/21 EMA, daily", "1D", 9, 21, "EMA"),
    ("20/50 SMA, 1-hour", "1h", 20, 50, "SMA"),
    ("9/21 EMA, 1-hour", "1h", 9, 21, "EMA"),
    ("20/50 SMA, 15-min", "15min", 20, 50, "SMA"),
    ("9/21 EMA, 15-min", "15min", 9, 21, "EMA"),
    ("9/21 EMA, 5-min", "5min", 9, 21, "EMA"),
]


def fetch(client, symbol, timeframe, start, end, feeds=(DataFeed.SIP, DataFeed.IEX)):
    last_error = None
    for feed in feeds:
        try:
            req = StockBarsRequest(symbol_or_symbols=symbol, timeframe=timeframe, start=start, end=end,
                                   adjustment=Adjustment.ALL, feed=feed)
            df = client.get_stock_bars(req).df
            if not df.empty:
                df = df.xs(symbol, level="symbol")
                df.index = df.index.tz_convert(NY)
                return df[["open", "high", "low", "close", "volume"]], feed.value
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f"Could not download {symbol} bars: {last_error}")


def resample_rth(bars_5m: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Regular-hours bars of size `rule`, aligned to the 9:30 open."""
    rth = bars_5m.between_time("09:30", "15:55")
    out = rth.resample(rule, origin="start_day", offset="9h30min", label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
    return out.dropna(subset=["open"])


def backtest(bars, fast, slow, kind, start):
    close = bars["close"]
    if kind == "EMA":
        f, s = close.ewm(span=fast, adjust=False).mean(), close.ewm(span=slow, adjust=False).mean()
    else:
        f, s = close.rolling(fast).mean(), close.rolling(slow).mean()

    above = (f > s)
    prev_above = above.shift(1)
    valid = s.notna() & s.shift(1).notna()
    buy = valid & (prev_above == False) & above
    sell = valid & (prev_above == True) & ~above

    slip = SLIPPAGE_PCT / 100
    opens = bars["open"].values
    idx = bars.index
    in_test = idx >= start

    trades, position, entry, entry_time = [], False, 0.0, None
    equity = [1.0]
    for i in range(len(bars) - 1):
        if not in_test[i]:
            continue
        nxt = opens[i + 1]
        if buy.iloc[i] and not position:
            position, entry, entry_time = True, nxt * (1 + slip), idx[i + 1]
        elif sell.iloc[i] and position:
            exit_price = nxt * (1 - slip)
            trades.append((entry_time, idx[i + 1], entry, exit_price))
            equity.append(equity[-1] * exit_price / entry)
            position = False
    open_pnl = None
    if position:  # mark the still-open trade at the last close
        last = bars["close"].iloc[-1]
        open_pnl = last / entry - 1
        equity.append(equity[-1] * last / entry)

    eq = pd.Series(equity)
    rets = [x / e - 1 for _, _, e, x in trades]
    test_bars = bars[in_test]
    return {
        "trades": len(trades) + (1 if position else 0),
        "closed": len(trades),
        "win_rate": (sum(r > 0 for r in rets) / len(rets) * 100) if rets else float("nan"),
        "total_ret": (eq.iloc[-1] - 1) * 100,
        "usd_per_share": sum(x - e for _, _, e, x in trades) + ((bars["close"].iloc[-1] - entry) if position else 0),
        "max_dd": ((eq / eq.cummax()) - 1).min() * 100,
        "avg_trade": (sum(rets) / len(rets) * 100) if rets else float("nan"),
        "open": position,
        "buy_hold": (test_bars["close"].iloc[-1] / test_bars["open"].iloc[0] - 1) * 100,
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--symbol", default="AAPL")
    p.add_argument("--months", type=int, default=12)
    p.add_argument("--until", help="last date to test, YYYY-MM-DD (default: yesterday)")
    args = p.parse_args()

    load_dotenv(Path(__file__).resolve().parent / ".env")
    client = StockHistoricalDataClient(os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY"))

    day_after = (pd.Timestamp(args.until) + timedelta(days=1)) if args.until else pd.Timestamp(datetime.now())
    end = day_after.tz_localize(NY).normalize() - timedelta(minutes=1)  # through the end of the day before
    start = end.normalize() - pd.DateOffset(months=args.months) + timedelta(days=1)
    warmup_daily = start - timedelta(days=120)   # room for a 50-day MA
    warmup_intra = start - timedelta(days=30)    # room for a 50-bar MA on 1-hour bars

    daily, feed_d = fetch(client, args.symbol, TimeFrame.Day, warmup_daily, end)
    five, feed_i = fetch(client, args.symbol, TimeFrame(5, TimeFrameUnit.Minute), warmup_intra, end)
    bars = {"1D": daily, "5min": resample_rth(five, "5min"),
            "15min": resample_rth(five, "15min"), "1h": resample_rth(five, "1h")}

    print(f"\n{args.symbol} backtest {start.date()} to {end.date()} | data feed: {feed_d}/{feed_i} | "
          f"slippage {SLIPPAGE_PCT}% per side | long only, fill at next bar open\n")
    hdr = f"{'Setting':27} {'Trades':>6} {'Win %':>6} {'Return':>8} {'$/share':>8} {'Avg trade':>9} {'Max DD':>7}"
    print(hdr)
    print("-" * len(hdr))
    bh = None
    for label, tf, fast, slow, kind in SETTINGS:
        r = backtest(bars[tf], fast, slow, kind, start)
        bh = r["buy_hold"] if tf == "1D" else bh
        print(f"{label:27} {r['trades']:>6} {r['win_rate']:>6.0f} {r['total_ret']:>7.1f}% "
              f"{r['usd_per_share']:>8.2f} {r['avg_trade']:>8.2f}% {r['max_dd']:>6.1f}%" + ("  (open)" if r["open"] else ""))
    d = daily[daily.index >= start]
    print(f"\nBuy and hold {args.symbol} the whole period: {bh:.1f}% "
          f"(${d['close'].iloc[-1] - d['open'].iloc[0]:.2f} per share)")


if __name__ == "__main__":
    main()
