"""
Curtailment History Manager

Records which historical 15-minute slots had grid export blocked (PV curtailed) so the
affected hours can be excluded from solar-production model training. When export is blocked
during negative/low prices the inverter throttles PV output to track house load, so the
recorded solar production for those periods is suppressed and would bias the forecast toward
under-prediction.

Ground truth is the grid-export switch entity (the one that *enables* export): a state of
``off`` means export is blocked and PV is being curtailed - the same semantic used at runtime
in devices/ev_solar_charge.py.

Home Assistant's ``/api/history/period`` only returns state history within the recorder's
retention window (``purge_keep_days``, ~10 days by default), while the solar model trains on a
much longer window (``prediction_days_back``, e.g. 90 days). To cover the full training window
we accumulate detected curtailed 15-minute slots into the shared ``db.json`` TinyDB (table
``curtailment``) on every prediction run; over time the store reaches full coverage.

Storage is at 15-minute resolution (the system's native slot size), keyed in UTC. The solar
model itself is hourly, so ``get_curtailed_hours()`` aggregates slots up to whole hours to
match the training merge key (``timestamp.dt.floor('h').dt.tz_localize(None)`` in
prediction.py, which yields UTC-naive hour timestamps).
"""

import logging
from datetime import datetime, timedelta, timezone

import aiohttp
import pandas as pd
from tinydb import TinyDB, Query

from ..config import CONFIG

logger = logging.getLogger(__name__)

# Storage resolution: one row per curtailed 15-minute slot.
SLOT_MINUTES = 15
SLOTS_PER_HOUR = 60 // SLOT_MINUTES

# A 15-minute slot counts as curtailed when export was blocked for at least this fraction of
# it. Using a fraction (rather than "any") avoids recording slots where the switch merely
# flipped briefly around a slot boundary.
CURTAILED_FRACTION_THRESHOLD = 0.5

# An hour is excluded from training when at least this many of its four slots were curtailed.
# The hourly training target is the SUM over the whole hour, so even one curtailed 15-min slot
# suppresses that hour's total and would pollute the model - drop the whole hour if any slot
# was curtailed.
HOUR_CURTAILED_MIN_SLOTS = 1

# How far back to keep rows in the store (housekeeping bound; reads filter by days_back).
KEEP_DAYS = 400

# How far back to request state history from HA. HA caps this to the recorder retention
# window regardless, so asking for the full training window is harmless.
_FETCH_DAYS = 120


class CurtailmentHistoryManager:
    """Persists curtailed hours (grid export blocked) in the shared db.json TinyDB."""

    def __init__(self, db_path='db.json'):
        """Initialize the manager.

        Args:
            db_path: Path to the shared TinyDB database file (default 'db.json', the same file
                     prediction.py uses to cache predictions).
        """
        self.db_path = db_path

    async def record_recent(self, switch_entity, access_token):
        """Fetch the export switch's recent state history from HA and persist curtailed hours.

        Fails open: on any error (no entity, no HA URL, request failure, empty history) it logs
        and returns without raising, so the caller's model training is never broken.

        Args:
            switch_entity: The grid-export switch entity id (``off`` == export blocked).
            access_token: Home Assistant access token.
        """
        if not switch_entity:
            return

        ha_url = CONFIG.get('options', {}).get('ha_url')
        if not ha_url:
            logger.warning("⚠️ No ha_url configured; cannot record curtailment history")
            return

        end_time = datetime.now(timezone.utc)
        start_time = end_time - timedelta(days=_FETCH_DAYS)

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }
        url = (
            f"{ha_url}/api/history/period/{start_time.isoformat()}"
            f"?filter_entity_id={switch_entity}"
        )

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers) as response:
                    if response.status != 200:
                        logger.warning(
                            f"⚠️ Failed to fetch curtailment history for {switch_entity}: "
                            f"HTTP {response.status}"
                        )
                        return
                    data = await response.json()
        except Exception as e:
            logger.warning(f"⚠️ Error fetching curtailment history for {switch_entity}: {e}")
            return

        if not data or not data[0]:
            logger.info(f"ℹ️ No curtailment history returned for {switch_entity}")
            return

        events = self._parse_events(data[0])
        if not events:
            logger.info(f"ℹ️ No usable state changes for {switch_entity}")
            return

        curtailed_slots = self._curtailed_slots_from_events(events, end_time)
        self._store(curtailed_slots)
        logger.info(
            f"📝 Recorded {len(curtailed_slots)} curtailed {SLOT_MINUTES}-min slot(s) for "
            f"{switch_entity} into curtailment history"
        )

    @staticmethod
    def _parse_events(entity_data):
        """Parse HA history entries into a sorted list of (utc_datetime, state) tuples."""
        events = []
        for state_change in entity_data:
            last_changed = state_change.get('last_changed')
            state = state_change.get('state')
            if not last_changed or state is None:
                continue
            try:
                if last_changed.endswith('Z'):
                    last_changed = last_changed[:-1] + '+00:00'
                ts = datetime.fromisoformat(last_changed).astimezone(timezone.utc)
            except (ValueError, TypeError):
                continue
            events.append((ts, str(state).lower()))
        events.sort(key=lambda e: e[0])
        return events

    @staticmethod
    def _curtailed_slots_from_events(events, end_time):
        """Return the set of UTC 15-minute slot-start datetimes that were curtailed (export off).

        Treats the state timeline as a step function: each event's state holds until the next
        event (or ``end_time``). Accumulates the seconds spent ``off`` per 15-minute slot and
        marks a slot curtailed when that fraction reaches CURTAILED_FRACTION_THRESHOLD.
        """
        slot_seconds = SLOT_MINUTES * 60
        off_seconds_by_slot = {}
        for i, (t0, state) in enumerate(events):
            if state != 'off':
                continue
            t1 = events[i + 1][0] if i + 1 < len(events) else end_time
            seg_end = min(t1, end_time)
            cursor = t0
            while cursor < seg_end:
                slot_minute = (cursor.minute // SLOT_MINUTES) * SLOT_MINUTES
                slot_start = cursor.replace(minute=slot_minute, second=0, microsecond=0)
                slot_end = slot_start + timedelta(minutes=SLOT_MINUTES)
                chunk_end = min(seg_end, slot_end)
                off_seconds_by_slot[slot_start] = (
                    off_seconds_by_slot.get(slot_start, 0.0)
                    + (chunk_end - cursor).total_seconds()
                )
                cursor = chunk_end

        threshold_seconds = CURTAILED_FRACTION_THRESHOLD * slot_seconds
        return {
            slot_start
            for slot_start, off_seconds in off_seconds_by_slot.items()
            if off_seconds >= threshold_seconds
        }

    def _store(self, curtailed_slots):
        """Upsert curtailed 15-min slots into the db.json 'curtailment' table and prune old rows."""
        cutoff_date = (datetime.now(timezone.utc).date() - timedelta(days=KEEP_DAYS)).isoformat()
        with TinyDB(self.db_path) as db:
            table = db.table('curtailment')
            q = Query()
            for slot_start in curtailed_slots:
                naive = slot_start.replace(tzinfo=None)  # UTC-naive, matches training merge key
                date_str = naive.date().isoformat()
                table.upsert(
                    {'date': date_str, 'timestamp': naive.isoformat()},
                    q.timestamp == naive.isoformat(),
                )
            table.remove(q.date < cutoff_date)

    def get_curtailed_hours(self, days_back):
        """Return a set of UTC-naive hour Timestamps to exclude from the (hourly) training set.

        Reads the stored 15-minute slots and aggregates them up to whole hours: an hour is
        returned when at least HOUR_CURTAILED_MIN_SLOTS of its slots were curtailed (default 1,
        i.e. any curtailed slot drops the hour, since the hourly training target is the sum over
        the whole hour). The returned Timestamps line up with prediction.py's
        ``timestamp_aligned`` (UTC-naive, hour-floored) so the caller can drop matching rows.
        """
        start_date = (datetime.now(timezone.utc).date() - timedelta(days=days_back)).isoformat()
        try:
            with TinyDB(self.db_path) as db:
                table = db.table('curtailment')
                rows = table.search(Query().date >= start_date)
        except Exception as e:
            logger.warning(f"⚠️ Could not read curtailment history: {e}")
            return set()

        slots_per_hour = {}
        for row in rows:
            ts = row.get('timestamp')
            if not ts:
                continue
            hour_start = pd.Timestamp(ts).floor('h')
            slots_per_hour[hour_start] = slots_per_hour.get(hour_start, 0) + 1
        return {
            hour_start
            for hour_start, count in slots_per_hour.items()
            if count >= HOUR_CURTAILED_MIN_SLOTS
        }

    def get_curtailed_slots(self, days_back):
        """Return a set of UTC-naive 15-minute slot Timestamps within the window.

        Slot-resolution accessor (the raw stored granularity) for any consumer that works at
        the native 15-minute slot size rather than whole hours.
        """
        start_date = (datetime.now(timezone.utc).date() - timedelta(days=days_back)).isoformat()
        try:
            with TinyDB(self.db_path) as db:
                table = db.table('curtailment')
                rows = table.search(Query().date >= start_date)
        except Exception as e:
            logger.warning(f"⚠️ Could not read curtailment history: {e}")
            return set()
        return {pd.Timestamp(row['timestamp']) for row in rows if row.get('timestamp')}
