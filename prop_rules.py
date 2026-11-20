"""
Prop-firm risk rules: position size from a fixed % risk, daily loss stop, max loss stop,
profit target stop. Used by both the live bot and the backtest, so they behave the same.
"""

import math
from dataclasses import dataclass

import config_noise as cfg


@dataclass(frozen=True)
class PropRules:
    account_size: float = cfg.ACCOUNT_SIZE
    risk_per_trade_usd: float = cfg.RISK_PER_TRADE_USD
    daily_stop_pct: float = cfg.DAILY_STOP_PCT
    firm_daily_pct: float = cfg.FIRM_DAILY_LOSS_PCT
    max_loss_stop_pct: float = cfg.MAX_LOSS_STOP_PCT
    firm_max_loss_pct: float = cfg.FIRM_MAX_LOSS_PCT
    target_pct: float = cfg.PROFIT_TARGET_PCT
    stop_at_target: bool = cfg.STOP_AT_TARGET
    max_pos_value_pct: float = cfg.MAX_POSITION_VALUE_PCT

    @property
    def risk_usd(self) -> float:
        return float(self.risk_per_trade_usd)

    def daily_stop_usd(self, start_of_day_equity: float) -> float:
        # Firms base the daily limit on the starting balance or the day's opening balance:
        # use whichever gives the smaller (safer) number.
        return min(self.account_size, start_of_day_equity) * self.daily_stop_pct / 100

    def halt_reason(self, equity: float) -> str | None:
        """Account-level stops that end trading for good (until you change the settings)."""
        if equity <= self.account_size * (1 - self.max_loss_stop_pct / 100):
            return f"max loss stop: equity {equity:,.0f} <= {self.account_size * (1 - self.max_loss_stop_pct / 100):,.0f}"
        if self.stop_at_target and equity >= self.account_size * (1 + self.target_pct / 100):
            return f"profit target reached: equity {equity:,.0f} >= {self.account_size * (1 + self.target_pct / 100):,.0f}"
        return None

    def day_locked(self, day_pnl: float, start_of_day_equity: float) -> bool:
        return day_pnl <= -self.daily_stop_usd(start_of_day_equity)

    def size(self, price: float, sl_dist: float, equity: float, day_pnl: float,
             start_of_day_equity: float) -> tuple[int, str]:
        """Shares to trade so a stop-out loses ~risk_usd. Returns (0, reason) when not allowed."""
        why = self.halt_reason(equity)
        if why:
            return 0, why
        stop = self.daily_stop_usd(start_of_day_equity)
        room = stop + day_pnl                      # $ left before today's stop
        if room < self.risk_usd:
            return 0, f"not enough room left today (${room:,.0f} < ${self.risk_usd:,.0f} risk)"
        qty = math.floor(self.risk_usd / sl_dist)
        cap = math.floor(equity * self.max_pos_value_pct / 100 / price)
        qty = min(qty, cap)
        if qty < 1:
            return 0, "position size rounds to 0 shares"
        return qty, f"{qty} shares = ${qty * sl_dist:,.0f} risk at the stop (${qty * price:,.0f} position)"
