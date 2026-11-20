"""
Settings for the Gold Breakout bot (gold_bot.py).
Port of "Gold Breakout 15M Strategy [WillyAlgoTrader]" (TradingView, tested on XAUUSD),
traded on GLD (SPDR Gold Shares) on a SEPARATE Alpaca paper account (keys in .env.gold).

The strategy, in short:
  - Range: high / low of price between RANGE_START_HOUR and RANGE_END_HOUR (broker time)
  - Entry: first break above the range high (long) or below the range low (short). One trade per day
  - Stop:  SL_PCT % from the actual fill price (or the opposite range boundary). No take profit
  - Exit:  stop loss, or forced close at CLOSE_HOUR (broker time)
"""

SYMBOL = "GLD"

# ---- Broker time (same meaning as the TradingView inputs) ----
# "fixed": use BROKER_TZ all year (TradingView default: GMT+3)
# "ny7":   New York time + 7 hours (GMT+2 winter / GMT+3 summer, most FX/CFD brokers)
BROKER_TIME_MODE = "fixed"
BROKER_TZ = "Etc/GMT-3"          # = GMT+3 (the sign is inverted in "Etc/" names)

RANGE_START_HOUR = 0             # range starts building (broker time)
RANGE_END_HOUR = 7               # range fixed, breakout trading starts
CLOSE_HOUR = 20                  # any open position is closed

# ---- Risk ----
SL_MODE = "pct"                  # "pct" = SL_PCT % of entry, "range" = opposite range boundary
SL_PCT = 1.0
SIZE_MODE = "risk_usd"           # "risk_usd" = RISK_PER_TRADE_USD lost at the stop (always the same $),
                                 # "risk_pct" = equity x RISK_PCT %, "fixed" = FIXED_QTY shares
RISK_PER_TRADE_USD = 500         # fixed $ lost if the stop loss is hit (never changes with equity)
RISK_PCT = 1.0
FIXED_QTY = 10
MAX_LEVERAGE = 1.9               # position value / equity cap (Alpaca allows max 2x overnight)
ALLOW_SHORT = True               # GLD is shortable; set False to trade long breakouts only

# ---- Prop firm rules: $100k 2-Steps "8/5" plan ----
#   Phase 1: target 8%   Phase 2: target 5%   Pro (funded): 10% for scaling, 50% consistency per day
#   All stages: max daily loss 3%, max loss 10% (account may never drop below $90,000)
# The daily loss is counted per broker day (00:00 broker time, GMT+3 = usual MT5 server day),
# starting from the account equity at that midnight.
ACCOUNT_SIZE = 100_000           # starting balance the firm's % rules are based on
PHASE = "phase1"                 # "phase1", "phase2" or "pro"
PHASE_TARGET_PCT = {"phase1": 8.0, "phase2": 5.0, "pro": 10.0}
FIRM_DAILY_LOSS_PCT = 3.0        # firm: max daily loss (3% = $3,000)
FIRM_MAX_LOSS_PCT = 10.0         # firm: max total loss (10% -> never below $90,000)
CONSISTENCY_PCT = 50.0           # pro: one day's profit should stay below 50% of total profit (warning only)

DAILY_STOP_PCT = 1.5             # bot stops for the day at -1.5% (half the firm's 3%)
MAX_LOSS_STOP_PCT = 8.0          # bot stops completely at -8% (firm fails you at -10%)
STOP_AT_TARGET = True            # phase 1/2: stop trading once the target is reached (not used in "pro")


GUARD_ON = False                # close everything when equity falls GUARD_DD_PCT % from the day's peak
GUARD_DD_PCT = 4.0

# ---- Market data ----
# "free": real-time prices only 20:00-04:00 ET (overnight feed) and ~9:30-16:00 ET (IEX).
#         04:00-9:30 and 16:00-20:00 ET only 15-minute delayed prices exist: the bot takes no new
#         entries then and checks the stop on delayed prices (late!).
# "plus": Algo Trader Plus subscription, real-time prices around the clock (SIP + BOATS).
DATA_PLAN = "free"

# ---- Execution (Alpaca accepts only LIMIT orders outside 9:30-16:00 ET) ----
LIMIT_OFFSET_PCT = 0.30          # entry/exit limit placed this % beyond bid/ask so it fills at once
MAX_SPREAD_PCT = 0.50            # skip entries while the bid/ask spread is wider than this
MAX_QUOTE_AGE_SEC = 120          # older quote = market closed / no data, do nothing
POLL_SECONDS = 5                 # how often price is checked

DRY_RUN = False                  # True = log decisions only, never send orders
