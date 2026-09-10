"""Pure long-option capital rules shared by execution and historical portfolios."""
import math
from decimal import Decimal, ROUND_HALF_UP


def limit_price_at_mid(bid, ask):
    if not all(math.isfinite(v) and v > 0 for v in (bid, ask)) or ask < bid:
        raise ValueError("Invalid option quote")
    # Check capital against the actual cent-rounded limit sent to the broker.
    return float(((Decimal(str(bid)) + Decimal(str(ask))) / 2).quantize(
        Decimal('0.01'), rounding=ROUND_HALF_UP))


def capital_rejection(premium, qty, premium_limit, available_cash, employed,
                      exposure_limit, starting_capital, positions=0,
                      max_positions=2, duplicate=False, group_count=0,
                      max_group_positions=1):
    if not math.isfinite(qty) or qty != 1:
        return "MAX_CONTRACTS_REACHED"
    if not math.isfinite(premium) or premium <= 0:
        return "OTHER"
    if duplicate:
        return "DUPLICATE_POSITION"
    if positions >= max_positions or group_count >= max_group_positions:
        return "MAX_STRATEGY_EXPOSURE_REACHED"
    if (premium > available_cash or
            employed + premium > min(exposure_limit, starting_capital)):
        return "MAX_STRATEGY_EXPOSURE_REACHED"
    # Capital-only premium rejections therefore really passed the other guards.
    if premium > premium_limit:
        return "PREMIUM_OVER_LIMIT"
    return None
