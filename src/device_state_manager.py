"""Device State Manager.

Manages device state persistence using TinyDB for tracking:
- Last run times (for gap calculations in optimization)
- Locked/scheduled starts (for rescheduling protection)

This module handles all TinyDB operations related to device state,
keeping the optimizer focused on orchestration logic.
"""
import logging
from datetime import datetime, timedelta
from tinydb import TinyDB, Query

logger = logging.getLogger(__name__)


class DeviceStateManager:
    """Manages device state persistence in TinyDB."""

    def __init__(self, db_path: str = 'db.json'):
        """Initialize the device state manager.
        
        Args:
            db_path: Path to the TinyDB database file
        """
        self.db_path = db_path

    def get_device_state(self, device: str) -> dict:
        """Get the last run state for a device from TinyDB.
        
        Args:
            device: Device name (e.g., 'wp', 'hw', 'battery')
            
        Returns:
            dict with:
                - 'last_run_end': datetime or None - when the last run ended
                - 'locked_starts': list of datetimes - scheduled start times
        """
        with TinyDB(self.db_path) as db:
            state_doc = db.get(Query().id == f"{device}_state")
        
        if not state_doc:
            return {'last_run_end': None, 'locked_starts': []}
        
        last_run_end = None
        if state_doc.get('last_run_end'):
            try:
                last_run_end = datetime.fromisoformat(state_doc['last_run_end'])
            except (ValueError, TypeError):
                pass
        
        locked_starts = []
        for start_str in state_doc.get('locked_starts', []):
            try:
                locked_starts.append(datetime.fromisoformat(start_str))
            except (ValueError, TypeError):
                pass
        
        return {'last_run_end': last_run_end, 'locked_starts': locked_starts}

    def save_device_state(self, device: str, last_run_end: datetime | None, 
                          scheduled_starts: list[datetime]):
        """Save device state to TinyDB for the next optimization run.
        
        Args:
            device: Device name (e.g., 'wp', 'hw')
            last_run_end: datetime of when the last run ended (or will end)
            scheduled_starts: list of datetime objects for scheduled start times
        """
        with TinyDB(self.db_path) as db:
            state = {
                'id': f"{device}_state",
                'last_run_end': last_run_end.isoformat() if last_run_end else None,
                'locked_starts': [s.isoformat() for s in scheduled_starts],
                'updated_at': datetime.now().isoformat()
            }
            db.upsert(state, Query().id == f"{device}_state")
        logger.debug(f"💾 Saved {device} state: last_run_end={last_run_end}, locked_starts={len(scheduled_starts)}")

    def calculate_initial_gap(self, device: str, horizon_start: datetime, 
                               slot_minutes: int, block_hours: float) -> int:
        """Calculate how many slots since the device last ran.
        
        This is used by the optimizer to determine when the device must run
        based on max_gap constraints.
        
        Args:
            device: Device name
            horizon_start: datetime when the optimization horizon starts
            slot_minutes: Duration of each slot in minutes
            block_hours: Duration of each block in hours
            
        Returns:
            Number of slots since last run ended (0 if currently running or just ended)
        """
        state = self.get_device_state(device)
        last_run_end = state['last_run_end']
        
        if last_run_end is None:
            # No previous run recorded - assume we need to run soon
            # Return a moderate gap that won't force immediate run but will prioritize early
            logger.info(f"📊 {device}: No previous run recorded, using default initial gap")
            return int(4 * 60 / slot_minutes)  # Assume 4 hours gap
        
        if last_run_end >= horizon_start:
            # Last run ends in the future (within or after horizon start)
            logger.info(f"📊 {device}: Last run ends at {last_run_end}, horizon starts at {horizon_start}")
            return 0
        
        # Calculate gap in slots
        gap_minutes = (horizon_start - last_run_end).total_seconds() / 60
        gap_slots = int(gap_minutes / slot_minutes)
        logger.info(f"📊 {device}: Last run ended {gap_minutes:.0f} min ago ({gap_slots} slots)")
        return gap_slots

    def get_locked_slots(self, device: str, horizon_start: datetime, 
                          lock_end_datetime: datetime, slot_minutes: int) -> set[int]:
        """Get slot indices that are locked (already scheduled and shouldn't be changed).
        
        Locked slots are:
        - Scheduled starts that fall within the lock window (now + lock_hours)
        - Already executed starts
        
        Args:
            device: Device name
            horizon_start: datetime when horizon starts
            lock_end_datetime: datetime until which slots are locked
            slot_minutes: Duration of each slot in minutes
            
        Returns:
            Set of slot indices that are locked
        """
        state = self.get_device_state(device)
        locked_starts = state['locked_starts']
        
        locked_slots = set()
        for start_dt in locked_starts:
            # Only lock if the start is:
            # 1. Within the horizon
            # 2. Before the lock end time
            if start_dt >= horizon_start and start_dt < lock_end_datetime:
                slot_idx = int((start_dt - horizon_start).total_seconds() / 60 / slot_minutes)
                if slot_idx >= 0:
                    locked_slots.add(slot_idx)
                    logger.debug(f"🔒 {device}: Locked slot {slot_idx} (start at {start_dt})")
        
        return locked_slots
