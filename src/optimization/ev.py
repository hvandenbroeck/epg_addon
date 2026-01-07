"""EV (Electric Vehicle) charging optimization.

This module implements simple threshold-based EV charging optimization.
It selects all timeslots where the electricity price is below a specified
maximum threshold.
"""
import logging

logger = logging.getLogger(__name__)


def optimize_ev(
    prices: list[float], 
    slot_minutes: int, 
    max_price: float, 
    slot_to_time
) -> list[str]:
    """Optimize EV charging by selecting all timeslots where price is below threshold.
    
    This is a simple threshold-based approach that selects all slots where
    the electricity price is at or below the specified maximum price.
    
    Args:
        prices: List of prices per slot
        slot_minutes: Duration of each slot in minutes
        max_price: Maximum acceptable price for charging (EUR/kWh)
        slot_to_time: Function to convert slot index to time string (HH:MM)
        
    Returns:
        List of start times for EV charging as HH:MM strings
    """
    slots = [i for i in range(len(prices)) if prices[i] <= max_price]
    
    if slots:
        logger.info(f"🚗 EV: selected {len(slots)} slots below {max_price:.4f} EUR/kWh")
    else:
        logger.info(f"🚗 EV: no slots below {max_price:.4f} EUR/kWh threshold")
    
    return [slot_to_time(i, slot_minutes) for i in slots]
