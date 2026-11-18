"""
Trading bot settings. Edit these values to change what the bot trades.

API credentials do NOT go here. They live in the .env file.
"""

# Stock to trade (Yahoo Finance / Alpaca ticker)
SYMBOL = "AAPL"

# Moving average periods, in trading days
SHORT_WINDOW = 20   # fast moving average
LONG_WINDOW = 50    # slow moving average

# Number of shares per order
TRADE_QTY = 1

# How many trading days of history to download from yfinance
LOOKBACK_DAYS = 100

# True = only print what the bot would do, never send orders
DRY_RUN = False

# False = a sell signal only sells shares you already own (no short selling)
ALLOW_SHORT = False
