"""Battery optimization (charge and discharge scheduling).

This module implements price-based optimization for battery charging
and discharging using historical percentile thresholds.

The optimization:
- Charges when prices are below the historical charge threshold
- Discharges when prices are above the historical discharge threshold
- Uses price difference logic for opportunistic charging/discharging
"""
import logging
import numpy as np

logger = logging.getLogger(__name__)


def optimize_battery(
    prices: list[float], 
    slot_minutes: int, 
    slot_to_time,
    max_charge_price: float | None = None, 
    price_difference_threshold: float | None = None
) -> list[str]:
    """
    Optimize battery charging periods using percentile-based price thresholds.
    Returns all eligible charging slots (limiting is done in limit_battery_cycles).
    
    Args:
        prices: List of prices per slot
        slot_minutes: Duration of each slot in minutes
        slot_to_time: Function to convert slot index to time string
        max_charge_price: Maximum price threshold for charging (from historical percentile).
                         If None, uses fallback based on horizon prices.
        price_difference_threshold: Additional threshold for opportunistic charging.
                                   Mark slot as charge if any future slot is more expensive by this amount.
        
    Returns:
        List of start times for battery charging as HH:MM strings
    """
    if not prices:
        return []
    
    # Use provided max_charge_price or calculate fallback from current horizon
    if max_charge_price is None:
        # Fallback: use 30th percentile of current horizon prices
        max_charge_price = float(np.percentile(prices, 30))
        logger.info(f"🔋 Battery charge: using fallback threshold {max_charge_price:.4f} EUR/kWh (30th percentile of horizon)")
    else:
        logger.info(f"🔋 Battery charge: using historical threshold {max_charge_price:.4f} EUR/kWh")
    
    # Filter slots that are below the max charge price threshold
    eligible_slots = [(i, prices[i]) for i in range(len(prices)) if prices[i] <= max_charge_price]
    logger.info(f"🔋 Battery charge: initially {len(eligible_slots)} eligible slots below threshold {max_charge_price:.4f} EUR/kWh")

    # Add price difference logic: mark slots as charge slots if there's a future slot more expensive by threshold
    if price_difference_threshold is not None and price_difference_threshold > 0:
        logger.info(f"🔋 Battery charge: applying price difference threshold {price_difference_threshold:.4f} EUR/kWh")
        for i in range(len(prices)):
            current_price = prices[i]
            # Check if any future slot is more expensive by at least the threshold
            has_expensive_future = any(
                prices[j] >= current_price + price_difference_threshold 
                for j in range(i + 1, len(prices))
            )
            if has_expensive_future:
                # Add to eligible slots if not already there
                if not any(slot_idx == i for slot_idx, _ in eligible_slots):
                    eligible_slots.append((i, current_price))
        logger.info(f"🔋 Battery charge: after price difference logic, {len(eligible_slots)} eligible slots")
    
    if not eligible_slots:
        logger.warning(f"⚠️ No slots below max_charge_price={max_charge_price:.4f} EUR/kWh")
        return []
    
    # Sort by price and return all eligible slots (limiting is done later)
    eligible_slots.sort(key=lambda x: x[1])
    selected_slots = [slot for slot, _ in eligible_slots]
    
    # Log selection info
    if selected_slots:
        avg_charge_price = sum(prices[i] for i in selected_slots) / len(selected_slots)
        min_price = min(prices[i] for i in selected_slots)
        max_selected_price = max(prices[i] for i in selected_slots)
        logger.info(f"💰 Battery charge: selected {len(selected_slots)} eligible slots "
                   f"(avg={avg_charge_price:.4f}, range={min_price:.4f}-{max_selected_price:.4f} EUR/kWh)")
    
    return [slot_to_time(i, slot_minutes) for i in sorted(selected_slots)]


def calculate_discharge_min_soc(
    discharge_times: list[str],
    prices: list[float],
    slot_minutes: int,
    min_soc_low: float,
    min_soc_medium: float,
    min_soc_high: float,
) -> dict[str, float]:
    """
    Categorize discharge slots by price (low / medium / high) and return the
    minimum SOC that should be maintained during each slot.

    Lower-cost discharge slots get a *higher* min SOC so the battery is
    preserved for more expensive future discharge periods.  High-cost slots
    get the lowest min SOC (``min_soc_high``) to allow maximum extraction.

    The price boundaries are the 33rd and 67th percentiles of the discharge-slot
    prices.  When all discharge prices are identical the slots all fall into the
    "high" category and ``min_soc_high`` is used throughout.

    Args:
        discharge_times: Horizon-relative time strings in HH:MM format.
        prices: Price per slot (indexed by slot number).
        slot_minutes: Slot duration in minutes.
        min_soc_low: Min SOC % applied to the cheapest 33 % of discharge slots.
        min_soc_medium: Min SOC % applied to the middle 33 % of discharge slots.
        min_soc_high: Min SOC % applied to the most expensive 33 % of slots.

    Returns:
        Dict mapping each HH:MM time string to its assigned min SOC percentage.
    """
    if not discharge_times:
        return {}

    def time_to_slot_idx(time_str: str) -> int:
        hour, minute = map(int, time_str.split(':'))
        return (hour * 60 + minute) // slot_minutes

    # Pair each time string with its price
    slot_prices: list[tuple[str, float]] = []
    for t in discharge_times:
        slot_idx = time_to_slot_idx(t)
        price = prices[slot_idx] if slot_idx < len(prices) else 0.0
        slot_prices.append((t, price))

    if not slot_prices:
        return {}

    discharge_price_values = [p for _, p in slot_prices]
    p33 = float(np.percentile(discharge_price_values, 33))
    p67 = float(np.percentile(discharge_price_values, 67))

    result: dict[str, float] = {}
    for time_str, price in slot_prices:
        if price <= p33:
            result[time_str] = min_soc_low      # Low-cost: keep battery in reserve
        elif price <= p67:
            result[time_str] = min_soc_medium   # Medium-cost: moderate reserve
        else:
            result[time_str] = min_soc_high     # High-cost: allow full discharge

    logger.info(
        f"🔋 Discharge min SOC categories: "
        f"{sum(1 for v in result.values() if v == min_soc_low)} low, "
        f"{sum(1 for v in result.values() if v == min_soc_medium)} medium, "
        f"{sum(1 for v in result.values() if v == min_soc_high)} high "
        f"(p33={p33:.4f}, p67={p67:.4f} EUR/kWh)"
    )

    return result


def optimize_bat_discharge(
    prices: list[float], 
    slot_minutes: int, 
    slot_to_time,
    min_discharge_price: float | None = None, 
    price_difference_threshold: float | None = None,
    reference_min_price: float | None = None
) -> tuple[list[str], dict]:
    """
    Optimize battery discharge periods using percentile-based price thresholds.
    Returns all eligible discharge slots (limiting is done in limit_battery_cycles).
    
    Args:
        prices: List of prices per slot
        slot_minutes: Duration of each slot in minutes
        slot_to_time: Function to convert slot index to time string
        min_discharge_price: Minimum price threshold for discharging (from historical percentile).
                            If None, uses fallback based on horizon prices.
        price_difference_threshold: Additional threshold for opportunistic discharging.
                                   Mark slot as discharge if it's more expensive than any slot being processed by this amount.
        reference_min_price: Optional minimum price from original optimization horizon.
                            When provided, this is used for price_difference_threshold calculations
                            instead of looking at earlier slots in current horizon.
                            This preserves discharge decisions when low prices have passed.
        
    Returns:
        Tuple of:
            - List of start times for battery discharge as HH:MM strings
            - Dict with price context (min_price_used) for storing with schedule
    """
    if not prices:
        return [], {'min_price_used': None}
    
    # Use provided min_discharge_price or calculate fallback from current horizon
    if min_discharge_price is None:
        # Fallback: use 70th percentile of current horizon prices
        min_discharge_price = float(np.percentile(prices, 70))
        logger.info(f"🔋 Battery discharge: using fallback threshold {min_discharge_price:.4f} EUR/kWh (70th percentile of horizon)")
    else:
        logger.info(f"🔋 Battery discharge: using historical threshold {min_discharge_price:.4f} EUR/kWh")
    
    # Filter slots that are above the min discharge price threshold
    eligible_slots = [(i, prices[i]) for i in range(len(prices)) if prices[i] >= min_discharge_price]
    logger.info(f"🔋 Battery discharge: initially {len(eligible_slots)} eligible slots above threshold {min_discharge_price:.4f} EUR/kWh")
    
    # Calculate the reference minimum price for price difference logic
    # Use reference_min_price if provided (from previous optimization), otherwise use current horizon min
    if reference_min_price is not None:
        min_price_for_threshold = reference_min_price
        logger.info(f"🔋 Battery discharge: using preserved reference min price {min_price_for_threshold:.4f} EUR/kWh")
    else:
        min_price_for_threshold = min(prices) if prices else 0
        logger.info(f"🔋 Battery discharge: using current horizon min price {min_price_for_threshold:.4f} EUR/kWh")
    
    # Add price difference logic: mark slots as discharge slots if they are more expensive than the reference min by threshold
    if price_difference_threshold is not None and price_difference_threshold > 0:
        logger.info(f"🔋 Battery discharge: applying price difference threshold {price_difference_threshold:.4f} EUR/kWh")
        for i in range(len(prices)):
            current_price = prices[i]
            # Check if this slot is more expensive than the reference min price by at least the threshold
            # This preserves discharge decisions even when the low prices have passed in the horizon
            is_expensive_compared_to_reference = current_price >= min_price_for_threshold + price_difference_threshold
            
            if is_expensive_compared_to_reference:
                # Add to eligible slots if not already there
                if not any(slot_idx == i for slot_idx, _ in eligible_slots):
                    eligible_slots.append((i, current_price))
        logger.info(f"🔋 Battery discharge: after price difference logic, {len(eligible_slots)} eligible slots")
    
    # Return both the times and the price context for storage
    price_context = {
        'min_price_used': min_price_for_threshold
    }
    
    if not eligible_slots:
        logger.warning(f"⚠️ No slots above min_discharge_price={min_discharge_price:.4f} EUR/kWh")
        return [], price_context
    
    # Sort by price descending and return all eligible slots (limiting is done later)
    eligible_slots.sort(key=lambda x: x[1], reverse=True)
    selected_slots = [slot for slot, _ in eligible_slots]
    
    # Log selection info
    if selected_slots:
        avg_discharge_price = sum(prices[i] for i in selected_slots) / len(selected_slots)
        min_selected_price = min(prices[i] for i in selected_slots)
        max_price = max(prices[i] for i in selected_slots)
        logger.info(f"💰 Battery discharge: selected {len(selected_slots)} eligible slots "
                   f"(avg={avg_discharge_price:.4f}, range={min_selected_price:.4f}-{max_price:.4f} EUR/kWh)")
    
    return [slot_to_time(i, slot_minutes) for i in sorted(selected_slots)], price_context
