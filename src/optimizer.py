import logging
import json
from datetime import datetime, timedelta
import aiohttp
from tinydb import TinyDB, Query
import asyncio

from .devices import Devices
from .scheduler import Scheduler
from .optimization import optimize_wp, optimize_hw, optimize_battery, optimize_bat_discharge, optimize_ev
from .utils import slot_to_time, slots_to_iso_ranges, merge_sequential_timeslots, time_to_slot
from .config import CONFIG
from .price_fetcher import EntsoeePriceFetcher
from .devices_config import devices_config

logger = logging.getLogger(__name__)


class HeatpumpOptimizer:
    def __init__(self, access_token, scheduler=None):
        self.ha_url = CONFIG['options']['ha_url']
        self.headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }
        self.devices = Devices(access_token)
        self.scheduler_instance = Scheduler(scheduler, self.devices)
        
        # Initialize ENTSO-E price fetcher
        entsoe_token = CONFIG['options'].get('entsoe_api_token', '')
        entsoe_country = CONFIG['options'].get('entsoe_country_code', 'BE')
        self.price_fetcher = EntsoeePriceFetcher(entsoe_token, entsoe_country) if entsoe_token else None

    async def get_state(self, entity_id):
        """Get the state of an entity from Home Assistant."""
        url = f"{self.ha_url}/api/states/{entity_id}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=self.headers) as response:
                if response.status == 200:
                    return await response.json()
                return None

    async def call_service(self, service, **service_data):
        """Call a Home Assistant service."""
        domain, service_name = service.split('/')
        
        url = f"{self.ha_url}/api/services/{domain}/{service_name}"
        
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=self.headers, json=service_data) as response:
                return response.status == 200

    def _get_device_state(self, device):
        """Get the last run state for a device from TinyDB.
        
        Returns:
            dict with 'last_run_end' (datetime or None) and 'locked_starts' (list of datetimes)
        """
        with TinyDB('db.json') as db:
            state_doc = db.get(Query().id == f"{device}_state")
        
        if not state_doc:
            return {'last_run_end': None, 'locked_starts': []}
        
        last_run_end = None
        if state_doc.get('last_run_end'):
            try:
                last_run_end = datetime.fromisoformat(state_doc['last_run_end'])
            except:
                pass
        
        locked_starts = []
        for start_str in state_doc.get('locked_starts', []):
            try:
                locked_starts.append(datetime.fromisoformat(start_str))
            except:
                pass
        
        return {'last_run_end': last_run_end, 'locked_starts': locked_starts}

    def _save_device_state(self, device, last_run_end, scheduled_starts):
        """Save device state to TinyDB for the next optimization run.
        
        Args:
            device: Device name (e.g., 'wp', 'hw')
            last_run_end: datetime of when the last run ended (or will end)
            scheduled_starts: list of datetime objects for scheduled start times
        """
        with TinyDB('db.json') as db:
            state = {
                'id': f"{device}_state",
                'last_run_end': last_run_end.isoformat() if last_run_end else None,
                'locked_starts': [s.isoformat() for s in scheduled_starts],
                'updated_at': datetime.now().isoformat()
            }
            db.upsert(state, Query().id == f"{device}_state")
        logger.debug(f"💾 Saved {device} state: last_run_end={last_run_end}, locked_starts={len(scheduled_starts)}")

    def _calculate_initial_gap(self, device, horizon_start, slot_minutes, block_hours):
        """Calculate how many slots since the device last ran.
        
        Args:
            device: Device name
            horizon_start: datetime when the optimization horizon starts
            slot_minutes: Duration of each slot in minutes
            block_hours: Duration of each block in hours
            
        Returns:
            Number of slots since last run ended (0 if currently running or just ended)
        """
        state = self._get_device_state(device)
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

    def _get_locked_slots(self, device, horizon_start, lock_end_datetime, slot_minutes, block_hours):
        """Get slot indices that are locked (already scheduled and shouldn't be changed).
        
        Locked slots are:
        - Scheduled starts that fall within the lock window (now + lock_hours)
        - Already executed starts
        
        Args:
            device: Device name
            horizon_start: datetime when horizon starts
            lock_end_datetime: datetime until which slots are locked
            slot_minutes: Duration of each slot
            block_hours: Duration of each block in hours
            
        Returns:
            Set of slot indices that are locked
        """
        state = self._get_device_state(device)
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

    async def run_optimization(self):
        """Main optimization logic using rolling horizon."""
        # Slot configuration - pricing data is 15-minute intervals
        SLOT_MINUTES = 15
        LOCK_HOURS = 2  # Don't reschedule actions within 2 hours

        # Heat Pump (WP) configuration - NEW sliding window approach
        WP_BLOCK_HOURS = 1      # Minimum runtime when turned on
        WP_MIN_GAP_HOURS = 3    # Minimum gap between runs (prevent rapid cycling)
        WP_MAX_GAP_HOURS = 8    # Maximum gap between runs (must run at least every 6 hours)

        # Hot Water (HW) configuration - NEW sliding window approach
        HW_BLOCK_HOURS = 1      # Minimum runtime when turned on
        HW_MIN_GAP_HOURS = 6    # Minimum gap between runs
        HW_MAX_GAP_HOURS = 12   # Maximum gap between runs

        # Battery configuration - percentage-based
        BAT_CHARGE_TIME_PCT = CONFIG['options'].get('battery_charge_time_percentage', 0.25)
        BAT_DISCHARGE_TIME_PCT = CONFIG['options'].get('battery_discharge_time_percentage', 0.25)
        
        # Battery optimization parameters (new)
        BAT_MIN_PRICE_DIFFERENTIAL = CONFIG['options'].get('battery_min_price_differential', 0.05)
        BAT_ROUND_TRIP_EFFICIENCY = CONFIG['options'].get('battery_round_trip_efficiency', 0.90)
        BAT_CAPACITY_KWH = CONFIG['options'].get('battery_capacity_kwh', 10.0)
        
        # Calculate cycle cost per kWh
        # cycle_cost_eur is the total cost per complete charge/discharge cycle
        # We divide by capacity to get cost per kWh cycled
        BAT_CYCLE_COST_TOTAL = CONFIG['options'].get('battery_cycle_cost_eur', 0.02)
        BAT_CYCLE_COST_PER_KWH = BAT_CYCLE_COST_TOTAL / BAT_CAPACITY_KWH if BAT_CAPACITY_KWH > 0 else 0.0
        
        EV_MAX_PRICE = 0.02  # 6 cents per kWh

        logger.info("🔎 Starting energy optimization using ENTSO-E prices (rolling horizon)...")
        logger.info(f"🔋 Battery optimization settings: min_differential={BAT_MIN_PRICE_DIFFERENTIAL:.3f} EUR/kWh, "
                   f"efficiency={BAT_ROUND_TRIP_EFFICIENCY:.1%}, cycle_cost={BAT_CYCLE_COST_PER_KWH:.4f} EUR/kWh")

        # Check if price fetcher is configured
        if not self.price_fetcher:
            logger.error("⚠️ ENTSO-E price fetcher not configured. Please set entsoe_api_token in config.json")
            return

        # Fetch horizon prices (from now until end of tomorrow)
        horizon_data = self.price_fetcher.get_horizon_prices(lock_hours=LOCK_HOURS)
        
        if not horizon_data:
            logger.error("⚠️ Failed to fetch price data from ENTSO-E")
            return

        prices = horizon_data['prices']
        horizon_start = horizon_data['horizon_start']
        horizon_end = horizon_data['horizon_end']
        lock_end_slot = horizon_data['lock_end_slot']
        # Use SLOT_MINUTES from config (should match price_fetcher's slot_minutes)
        slot_minutes = SLOT_MINUTES
        
        lock_end_datetime = horizon_start + timedelta(hours=LOCK_HOURS)
        
        logger.info(f"📊 Horizon: {horizon_start} to {horizon_end} ({len(prices)} slots)")
        logger.info(f"🔒 Lock window: until {lock_end_datetime} (slot {lock_end_slot})")

        results = {}
        
        # ===== HEAT PUMP OPTIMIZATION (iterate over all WP devices) =====
        wp_devices = devices_config.get_devices_by_type('wp')
        for wp_device in wp_devices:
            device_name = wp_device.name
            wp_initial_gap = self._calculate_initial_gap(device_name, horizon_start, slot_minutes, WP_BLOCK_HOURS)
            wp_locked_slots = self._get_locked_slots(device_name, horizon_start, lock_end_datetime, slot_minutes, WP_BLOCK_HOURS)
            
            wp_times = optimize_wp(
                prices=prices,
                slot_minutes=slot_minutes,
                block_hours=WP_BLOCK_HOURS,
                min_gap_hours=WP_MIN_GAP_HOURS,
                max_gap_hours=WP_MAX_GAP_HOURS,
                locked_slots=wp_locked_slots,
                initial_gap_slots=wp_initial_gap,
                horizon_start_datetime=horizon_start,
                slot_to_time=slot_to_time
            )
            results[device_name] = wp_times
            
            # Calculate last run end and save state for this WP device
            if wp_times:
                wp_slot_indices = [time_to_slot(t, slot_minutes) for t in wp_times]
                last_wp_slot = max(wp_slot_indices)
                last_wp_end = horizon_start + timedelta(minutes=(last_wp_slot + int(WP_BLOCK_HOURS * 60 / slot_minutes)) * slot_minutes)
                wp_scheduled_starts = [horizon_start + timedelta(minutes=idx * slot_minutes) for idx in wp_slot_indices]
                self._save_device_state(device_name, last_wp_end, wp_scheduled_starts)

        # ===== HOT WATER OPTIMIZATION (iterate over all HW devices) =====
        hw_devices = devices_config.get_devices_by_type('hw')
        for hw_device in hw_devices:
            device_name = hw_device.name
            hw_initial_gap = self._calculate_initial_gap(device_name, horizon_start, slot_minutes, HW_BLOCK_HOURS)
            hw_locked_slots = self._get_locked_slots(device_name, horizon_start, lock_end_datetime, slot_minutes, HW_BLOCK_HOURS)
            
            hw_times = optimize_hw(
                prices=prices,
                slot_minutes=slot_minutes,
                block_hours=HW_BLOCK_HOURS,
                min_gap_hours=HW_MIN_GAP_HOURS,
                max_gap_hours=HW_MAX_GAP_HOURS,
                locked_slots=hw_locked_slots,
                initial_gap_slots=hw_initial_gap,
                horizon_start_datetime=horizon_start,
                slot_to_time=slot_to_time
            )
            results[device_name] = hw_times
            
            # Calculate last run end and save state for this HW device
            if hw_times:
                hw_slot_indices = [time_to_slot(t, slot_minutes) for t in hw_times]
                last_hw_slot = max(hw_slot_indices)
                last_hw_end = horizon_start + timedelta(minutes=(last_hw_slot + int(HW_BLOCK_HOURS * 60 / slot_minutes)) * slot_minutes)
                hw_scheduled_starts = [horizon_start + timedelta(minutes=idx * slot_minutes) for idx in hw_slot_indices]
                self._save_device_state(device_name, last_hw_end, hw_scheduled_starts)

        # ===== BATTERY CHARGE OPTIMIZATION (iterate over all bat_charge devices) =====
        bat_charge_devices = devices_config.get_devices_by_type('bat_charge')
        for bat_device in bat_charge_devices:
            device_name = bat_device.name
            bat_charge_times = optimize_battery(
                prices=prices,
                slot_minutes=slot_minutes,
                charge_time_percentage=BAT_CHARGE_TIME_PCT,
                slot_to_time=slot_to_time,
                min_price_differential=BAT_MIN_PRICE_DIFFERENTIAL,
                round_trip_efficiency=BAT_ROUND_TRIP_EFFICIENCY,
                cycle_cost_per_kwh=BAT_CYCLE_COST_PER_KWH,
                capacity_kwh=BAT_CAPACITY_KWH
            )
            results[device_name] = bat_charge_times
        
        # ===== BATTERY DISCHARGE OPTIMIZATION (iterate over all bat_discharge devices) =====
        bat_discharge_devices = devices_config.get_devices_by_type('bat_discharge')
        for bat_device in bat_discharge_devices:
            device_name = bat_device.name
            bat_discharge_times = optimize_bat_discharge(
                prices=prices,
                slot_minutes=slot_minutes,
                discharge_time_percentage=BAT_DISCHARGE_TIME_PCT,
                slot_to_time=slot_to_time,
                min_price_differential=BAT_MIN_PRICE_DIFFERENTIAL,
                round_trip_efficiency=BAT_ROUND_TRIP_EFFICIENCY,
                cycle_cost_per_kwh=BAT_CYCLE_COST_PER_KWH,
                capacity_kwh=BAT_CAPACITY_KWH
            )
            results[device_name] = bat_discharge_times

        # ===== EV OPTIMIZATION (iterate over all EV devices) =====
        ev_devices = devices_config.get_devices_by_type('ev')
        for ev_device in ev_devices:
            device_name = ev_device.name
            ev_times = optimize_ev(prices, slot_minutes, EV_MAX_PRICE, slot_to_time)
            results[device_name] = ev_times

        logger.info(f"⚙️ Optimization Results: {json.dumps(results)}")

        # Convert results to ISO time ranges for scheduling
        # Build device_block_minutes dynamically based on device type
        device_block_minutes = {}
        for device in devices_config.devices:
            if device.type == 'wp':
                device_block_minutes[device.name] = int(WP_BLOCK_HOURS * 60)
            elif device.type == 'hw':
                device_block_minutes[device.name] = int(HW_BLOCK_HOURS * 60)
            elif device.type == 'bat_charge':
                device_block_minutes[device.name] = slot_minutes  # Single slot
            elif device.type == 'bat_discharge':
                device_block_minutes[device.name] = slot_minutes  # Single slot
            elif device.type == 'ev':
                device_block_minutes[device.name] = slot_minutes  # Single slot
        
        iso_times = []
        
        # Process all devices in results
        for device_name, times in results.items():
            if times:
                iso_times.append(slots_to_iso_ranges(
                    times, device_name, horizon_start.date(), horizon_start,
                    block_minutes=device_block_minutes.get(device_name, slot_minutes)
                ))

        iso_times_merged = merge_sequential_timeslots(iso_times)

        # Save schedule to TinyDB
        with TinyDB('db.json') as db:
            db.upsert({
                "id": "schedule",
                "schedule": iso_times_merged,
                "horizon_start": horizon_start.isoformat(),
                "horizon_end": horizon_end.isoformat(),
                "updated_at": datetime.now().isoformat()
            }, Query().id == "schedule")
        
        logger.info(f"✅ Optimization complete. Schedule saved to TinyDB.")
        
        # Schedule actions from database
        await self.scheduler_instance.schedule_actions()