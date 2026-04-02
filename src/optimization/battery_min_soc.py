"""Battery minimum SOC calculation based on price categories.

This module categorizes all time slots into low, medium, and high price groups
and calculates the appropriate minimum SOC for each slot. All slots are included
regardless of whether they are charge or discharge slots, so that the inverter's
minimum discharge SOC is always set appropriately:

- Low-cost slots  → higher min SOC floor (preserve battery for expensive periods)
- Medium-cost slots → medium min SOC floor
- High-cost slots → lower min SOC floor (allow more discharge when prices are high)
"""
import logging
import numpy as np
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

PRICE_CATEGORY_LOW = "low"
PRICE_CATEGORY_MEDIUM = "medium"
PRICE_CATEGORY_HIGH = "high"


def categorize_slots_by_price(
    prices: list[float],
    slot_minutes: int,
    horizon_start: datetime,
    min_soc_low: float = 30.0,
    min_soc_medium: float = 20.0,
    min_soc_high: float = 10.0,
) -> list[dict]:
    """Categorize all time slots into low, medium, and high price groups.

    Uses the 33rd and 67th price percentiles as category boundaries. All slots
    are included regardless of their charge/discharge status.

    Args:
        prices: List of electricity prices per slot (EUR/kWh).
        slot_minutes: Duration of each slot in minutes.
        horizon_start: Start datetime of the optimization horizon.
        min_soc_low: Minimum SOC (%) for low-cost slots – highest floor,
            preserving battery capacity for expensive periods.
        min_soc_medium: Minimum SOC (%) for medium-cost slots.
        min_soc_high: Minimum SOC (%) for high-cost slots – lowest floor,
            allowing maximum discharge when prices are high.

    Returns:
        List of dicts, one per slot, each containing:
            - ``slot_time``:    ISO-format datetime string for the slot start.
            - ``price``:        Electricity price for this slot (EUR/kWh).
            - ``category``:     ``"low"``, ``"medium"``, or ``"high"``.
            - ``min_soc_percent``: Minimum SOC (%) to apply at this slot.
    """
    if not prices:
        return []

    p33 = float(np.percentile(prices, 33))
    p67 = float(np.percentile(prices, 67))

    logger.info(
        f"🔋 Battery min SOC categorization: p33={p33:.4f}, p67={p67:.4f} EUR/kWh | "
        f"min_soc: low={min_soc_low}%, medium={min_soc_medium}%, high={min_soc_high}%"
    )

    result = []
    for i, price in enumerate(prices):
        if price <= p33:
            category = PRICE_CATEGORY_LOW
            min_soc = min_soc_low
        elif price <= p67:
            category = PRICE_CATEGORY_MEDIUM
            min_soc = min_soc_medium
        else:
            category = PRICE_CATEGORY_HIGH
            min_soc = min_soc_high

        slot_dt = horizon_start + timedelta(minutes=i * slot_minutes)
        result.append({
            "slot_time": slot_dt.isoformat(),
            "price": price,
            "category": category,
            "min_soc_percent": min_soc,
        })

    counts = {
        PRICE_CATEGORY_LOW: 0,
        PRICE_CATEGORY_MEDIUM: 0,
        PRICE_CATEGORY_HIGH: 0,
    }
    for entry in result:
        counts[entry["category"]] += 1
    logger.info(
        f"🔋 Battery min SOC slot counts: "
        f"low={counts[PRICE_CATEGORY_LOW]}, "
        f"medium={counts[PRICE_CATEGORY_MEDIUM]}, "
        f"high={counts[PRICE_CATEGORY_HIGH]}"
    )

    return result
