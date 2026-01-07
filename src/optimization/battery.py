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


def optimize_bat_discharge(
    prices: list[float], 
    slot_minutes: int, 
    slot_to_time,
    min_discharge_price: float | None = None, 
    price_difference_threshold: float | None = None
) -> list[str]:
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
        
    Returns:
        List of start times for battery discharge as HH:MM strings
    """
    if not prices:
        return []
    
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
    
    # Add price difference logic: mark slots as discharge slots if they are more expensive than earlier slots by threshold
    if price_difference_threshold is not None and price_difference_threshold > 0:
        logger.info(f"🔋 Battery discharge: applying price difference threshold {price_difference_threshold:.4f} EUR/kWh")
        for i in range(len(prices)):
            current_price = prices[i]
            # Check if this slot is more expensive than any earlier slot by at least the threshold
            is_expensive_compared_to_past = any(
                current_price >= prices[j] + price_difference_threshold 
                for j in range(0, i)
            )
            if is_expensive_compared_to_past:
                # Add to eligible slots if not already there
                if not any(slot_idx == i for slot_idx, _ in eligible_slots):
                    eligible_slots.append((i, current_price))
        logger.info(f"🔋 Battery discharge: after price difference logic, {len(eligible_slots)} eligible slots")
    
    if not eligible_slots:
        logger.warning(f"⚠️ No slots above min_discharge_price={min_discharge_price:.4f} EUR/kWh")
        return []
    
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
    
    return [slot_to_time(i, slot_minutes) for i in sorted(selected_slots)]
