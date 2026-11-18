"""
Settings for the Noise Band Momentum bot (noise_bot.py).
Prop-firm rules: $100k account, 1-Step challenge.
"""

SYMBOL = "QQQ"
PRESET = "Ultra-Safe"            # "Ultra-Safe", "Prop Safe" or "Higher Output"

# ---- Account and firm limits ----
ACCOUNT_SIZE = 100_000           # starting balance the firm's % rules are based on
FIRM_DAILY_LOSS_PCT = 3.0        # firm: max daily loss (3% = $3,000)
FIRM_MAX_LOSS_PCT = 10.0         # firm: max total loss (10% -> account may not drop below $90,000)
PROFIT_TARGET_PCT = 8.0          # firm: target to pass (8% = $108,000)

# ---- Bot safety limits (kept well inside the firm limits) ----
RISK_PER_TRADE_PCT = 0.5         # $ lost if the stop loss is hit (0.5% = $500)
DAILY_STOP_PCT = 1.5             # bot stops for the day at -1.5% (half the firm's 3%)
MAX_LOSS_STOP_PCT = 8.0          # bot stops completely at -8% (firm fails you at -10%)
STOP_AT_TARGET = True            # stop trading once the profit target is reached
MAX_POSITION_VALUE_PCT = 190     # never hold more than 1.9x the account in QQQ (Alpaca allows 2x)

DRY_RUN = False                  # True = log decisions only, never send orders
