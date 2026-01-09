"""Energy Optimization Orchestrator.

This module coordinates the optimization workflow:
1. Fetch prices from ENTSO-E
2. Get device states from persistence
3. Run optimization algorithms for each device type
4. Save results and schedule actions

The heavy lifting is delegated to specialized modules:
- ha_client: Home Assistant API calls
- device_state_manager: Device state persistence (TinyDB)
- optimization/: Optimization algorithms
"""
import logging
import json
from datetime import datetime, timedelta
from tinydb import TinyDB, Query

from .ha_client import HomeAssistantClient
from .device_state_manager import DeviceStateManager
from .devices import Devices
from .scheduler import Scheduler
from .optimization import optimize_wp, optimize_hw, optimize_battery, optimize_bat_discharge, optimize_ev, limit_battery_cycles
from .utils import slot_to_time, slots_to_iso_ranges, merge_sequential_timeslots, time_to_slot
from .config import CONFIG
from .price_fetcher import EntsoeePriceFetcher
from .devices_config import devices_config
from .forecasting.price_history import PriceHistoryManager
from .forecasting.statistics_loader import StatisticsLoader
from .forecasting.weather import Weather
from .forecasting.prediction import Prediction

logger = logging.getLogger(__name__)


class HeatpumpOptimizer:
    """Orchestrates energy optimization for heat pumps, batteries, and EVs.
    
    This class coordinates the optimization workflow by:
    - Fetching electricity prices from ENTSO-E
    - Calculating price thresholds from historical data
    - Running device-specific optimization algorithms
    - Saving schedules and triggering action scheduling
    
    The actual optimization algorithms are delegated to the optimization/ package,
    while infrastructure concerns (HA API, persistence) are handled by specialized modules.
    """

    def __init__(self, access_token, scheduler=None):
        """Initialize the optimizer.
        
        Args:
            access_token: Home Assistant Long-Lived Access Token
            scheduler: Optional APScheduler instance for action scheduling
        """
        self.ha_client = HomeAssistantClient(access_token)
        self.state_manager = DeviceStateManager()
        self.devices = Devices(access_token)
        self.scheduler_instance = Scheduler(scheduler, self.devices)
        
        # Initialize ENTSO-E price fetcher
        entsoe_token = CONFIG['options'].get('entsoe_api_token', '')
        entsoe_country = CONFIG['options'].get('entsoe_country_code', 'BE')
        self.price_fetcher = EntsoeePriceFetcher(entsoe_token, entsoe_country) if entsoe_token else None
        
        # Initialize Price History Manager for percentile calculations
        self.price_history_manager = PriceHistoryManager(entsoe_token, entsoe_country) if entsoe_token else None

    async def get_state(self, entity_id):
        """Get the state of an entity from Home Assistant.
        
        Delegates to HomeAssistantClient for actual API call.
        """
        return await self.ha_client.get_state(entity_id)

    async def call_service(self, service, **service_data):
        """Call a Home Assistant service.
        
        Delegates to HomeAssistantClient for actual API call.
        """
        return await self.ha_client.call_service(service, **service_data)

    def _get_device_state(self, device):
        """Get the last run state for a device.
        
        Delegates to DeviceStateManager for persistence operations.
        """
        return self.state_manager.get_device_state(device)

    def _save_device_state(self, device, last_run_end, scheduled_starts):
        """Save device state for the next optimization run.
        
        Delegates to DeviceStateManager for persistence operations.
        """
        self.state_manager.save_device_state(device, last_run_end, scheduled_starts)

    def _calculate_initial_gap(self, device, horizon_start, slot_minutes, block_hours):
        """Calculate how many slots since the device last ran.
        
        Delegates to DeviceStateManager for persistence operations.
        """
        return self.state_manager.calculate_initial_gap(device, horizon_start, slot_minutes, block_hours)

    def _get_locked_slots(self, device, horizon_start, lock_end_datetime, slot_minutes, block_hours):
        """Get slot indices that are locked (already scheduled and shouldn't be changed).
        
        Delegates to DeviceStateManager for persistence operations.
        Note: block_hours is kept for API compatibility but not used in this delegation.
        """
        return self.state_manager.get_locked_slots(device, horizon_start, lock_end_datetime, slot_minutes)

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

        # Battery percentile configuration (for dynamic price thresholds)
        BAT_PRICE_HISTORY_DAYS = CONFIG['options'].get('battery_price_history_days', 14)
        BAT_CHARGE_PERCENTILE = CONFIG['options'].get('battery_charge_percentile', 30)
        BAT_DISCHARGE_PERCENTILE = CONFIG['options'].get('battery_discharge_percentile', 70)
        BAT_PRICE_DIFF_THRESHOLD = CONFIG['options'].get('battery_price_difference_threshold', 0.10)

        EV_MAX_PRICE = 0.09  # 10 cents per kWh

        logger.info("🔎 Starting energy optimization using ENTSO-E prices (rolling horizon)...")
        logger.info(f"🔋 Battery optimization settings: "
                   f"history_days={BAT_PRICE_HISTORY_DAYS}, "
                   f"charge_percentile={BAT_CHARGE_PERCENTILE}, discharge_percentile={BAT_DISCHARGE_PERCENTILE}, "
                   f"price_diff_threshold={BAT_PRICE_DIFF_THRESHOLD:.4f} EUR/kWh")

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

        # ===== CALCULATE BATTERY PRICE THRESHOLDS FROM HISTORICAL DATA =====
        max_charge_price = None
        min_discharge_price = None
        
        if self.price_history_manager:
            try:
                percentiles = await self.price_history_manager.get_price_percentiles(
                    days_back=BAT_PRICE_HISTORY_DAYS,
                    charge_percentile=BAT_CHARGE_PERCENTILE,
                    discharge_percentile=BAT_DISCHARGE_PERCENTILE
                )
                if percentiles:
                    max_charge_price = percentiles['max_charge_price']
                    min_discharge_price = percentiles['min_discharge_price']
                    logger.info(f"✅ Using historical percentile thresholds: "
                               f"max_charge={max_charge_price:.4f}, min_discharge={min_discharge_price:.4f} EUR/kWh")
                else:
                    logger.warning("⚠️ Could not calculate price percentiles, battery optimization will use fallback")
            except Exception as e:
                logger.error(f"❌ Error calculating price percentiles: {e}")
        else:
            logger.warning("⚠️ Price history manager not configured, battery optimization will use fallback")

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

        # ===== BATTERY OPTIMIZATION (iterate over all battery devices) =====
        # Battery devices have both charge and discharge schedules
        # We store ORIGINAL times from price optimization (for display) and LIMITED times (for scheduling)
        original_battery_times = {}  # Store original times before SOC limiting
        
        battery_devices = devices_config.get_devices_by_type('battery')
        for bat_device in battery_devices:
            device_name = bat_device.name
            
            # Optimize battery charging based on price thresholds
            bat_charge_times = optimize_battery(
                prices=prices,
                slot_minutes=slot_minutes,
                slot_to_time=slot_to_time,
                max_charge_price=max_charge_price,
                price_difference_threshold=BAT_PRICE_DIFF_THRESHOLD
            )
            
            # Optimize battery discharging based on price thresholds
            bat_discharge_times = optimize_bat_discharge(
                prices=prices,
                slot_minutes=slot_minutes,
                slot_to_time=slot_to_time,
                min_discharge_price=min_discharge_price,
                price_difference_threshold=BAT_PRICE_DIFF_THRESHOLD
            )
            
            # Store ORIGINAL times (before SOC limiting) for informative display
            original_battery_times[f"{device_name}_charge_planned"] = list(bat_charge_times) if bat_charge_times else []
            original_battery_times[f"{device_name}_discharge_planned"] = list(bat_discharge_times) if bat_discharge_times else []
            
            # Store in results (will be overwritten with limited times below)
            results[f"{device_name}_charge"] = bat_charge_times
            results[f"{device_name}_discharge"] = bat_discharge_times

        # ===== EV OPTIMIZATION (iterate over all EV devices) =====
        ev_devices = devices_config.get_devices_by_type('ev')
        for ev_device in ev_devices:
            device_name = ev_device.name
            ev_times = optimize_ev(prices, slot_minutes, EV_MAX_PRICE, slot_to_time)
            results[device_name] = ev_times

        logger.info(f"⚙️ Optimization Results (before SOC limiting): {json.dumps(results)}")

        # Convert results to ISO time ranges for scheduling
        # Build device_block_minutes dynamically based on device type
        device_block_minutes = {}
        for device in devices_config.devices:
            if device.type == 'wp':
                device_block_minutes[device.name] = int(WP_BLOCK_HOURS * 60)
            elif device.type == 'hw':
                device_block_minutes[device.name] = int(HW_BLOCK_HOURS * 60)
            elif device.type == 'battery':
                # Battery has separate charge and discharge entries
                device_block_minutes[f"{device.name}_charge"] = slot_minutes  # Single slot
                device_block_minutes[f"{device.name}_discharge"] = slot_minutes  # Single slot
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
        
        # Also convert original battery times to ISO ranges for display on Gantt chart
        original_iso_times = []
        for device_key, times in original_battery_times.items():
            if times:
                original_iso_times.append(slots_to_iso_ranges(
                    times, device_key, horizon_start.date(), horizon_start,
                    block_minutes=slot_minutes
                ))
        original_battery_iso_times_merged = merge_sequential_timeslots(original_iso_times)

        # Save schedule to TinyDB (without limited times yet - will be added by recalculate)
        with TinyDB('db.json') as db:
            db.upsert({
                "id": "schedule",
                "schedule": iso_times_merged,  # Will be updated with limited times
                "original_battery_schedule": original_battery_iso_times_merged,  # Original price-based times for display
                "horizon_start": horizon_start.isoformat(),
                "horizon_end": horizon_end.isoformat(),
                "prices": prices,  # Store prices for recalculation
                "slot_minutes": slot_minutes,
                "updated_at": datetime.now().isoformat(),
                "battery_price_thresholds": {
                    "max_charge_price": max_charge_price,
                    "min_discharge_price": min_discharge_price,
                    "price_history_days": BAT_PRICE_HISTORY_DAYS,
                    "charge_percentile": BAT_CHARGE_PERCENTILE,
                    "discharge_percentile": BAT_DISCHARGE_PERCENTILE,
                    "price_diff_threshold": BAT_PRICE_DIFF_THRESHOLD
                }
            }, Query().id == "schedule")
        
        logger.info(f"✅ Optimization complete. Schedule saved to TinyDB.")
        
        # Run initial battery cycle limiting based on current SOC
        await self.recalculate_battery_limits()
        
        # Schedule actions from database
        await self.scheduler_instance.schedule_actions()

    async def recalculate_battery_limits(self):
        """Recalculate battery cycle limits based on current SOC.
        
        Called after optimization and every 15 minutes to adapt to actual SOC changes.
        """
        logger.info("🔋 Recalculating battery cycle limits based on current SOC...")
        
        # Load schedule from database
        with TinyDB('db.json') as db:
            schedule_doc = db.get(Query().id == "schedule")
        
        # Validate schedule exists and has required data
        if not schedule_doc or not schedule_doc.get('horizon_start') or not schedule_doc.get('prices'):
            logger.warning("⚠️ No valid schedule found, skipping recalculation")
            return
        
        # Parse schedule parameters
        horizon_start = datetime.fromisoformat(schedule_doc['horizon_start'])
        horizon_end = datetime.fromisoformat(schedule_doc['horizon_end'])
        
        # Skip if optimization horizon has expired
        if datetime.now() >= horizon_end:
            logger.info("📅 Horizon expired, skipping recalculation")
            return
        
        prices = schedule_doc['prices']
        slot_minutes = schedule_doc.get('slot_minutes', 15)
        original_battery_schedule = schedule_doc.get('original_battery_schedule', [])
        predicted_usage = await self._get_predicted_usage(slot_minutes)
        
        # Keep all non-battery entries from current schedule
        new_schedule = [
            entry for entry in schedule_doc.get('schedule', [])
            if not (entry.get('device', '').endswith('_charge') or 
                   entry.get('device', '').endswith('_discharge'))
        ]
        
        # Process each battery device
        for bat_device in devices_config.get_devices_by_type('battery'):
            # Get current SOC (or use 50% as fallback)
            current_soc = await self._get_battery_soc(bat_device)
            
            # Extract original times from stored schedule (already filtered for future slots)
            original_charge = self._extract_times(original_battery_schedule, f"{bat_device.name}_charge_planned", horizon_start, slot_minutes)
            original_discharge = self._extract_times(original_battery_schedule, f"{bat_device.name}_discharge_planned", horizon_start, slot_minutes)
            
            if original_charge or original_discharge:
                logger.debug(f"🕐 {bat_device.name}: Processing {len(original_charge)} charge and {len(original_discharge)} discharge future slots")
            
            # Apply SOC-based cycle limiting if battery config is complete
            if bat_device.battery_capacity_kwh and bat_device.battery_charge_speed_kw:
                limited_charge, limited_discharge = limit_battery_cycles(
                    charge_times=original_charge,
                    discharge_times=original_discharge,
                    slot_minutes=slot_minutes,
                    horizon_start=horizon_start,
                    current_soc=current_soc,
                    battery_capacity_kwh=bat_device.battery_capacity_kwh,
                    battery_charge_speed_kw=bat_device.battery_charge_speed_kw,
                    min_soc_percent=bat_device.battery_min_soc_percent or 20.0,
                    max_soc_percent=bat_device.battery_max_soc_percent or 80.0,
                    prices=prices,
                    predicted_power_usage=predicted_usage,
                    device_name=bat_device.name
                )
            else:
                # Use original times if battery not fully configured
                logger.warning(f"⚠️ {bat_device.name}: Missing battery config, using original times")
                limited_charge, limited_discharge = original_charge, original_discharge
            
            # Add limited times to schedule
            new_schedule.extend(self._times_to_schedule(limited_charge, f"{bat_device.name}_charge", horizon_start, slot_minutes))
            new_schedule.extend(self._times_to_schedule(limited_discharge, f"{bat_device.name}_discharge", horizon_start, slot_minutes))
        
        # Merge and save updated schedule
        new_schedule = merge_sequential_timeslots([new_schedule])
        schedule_doc['schedule'] = new_schedule
        schedule_doc['last_soc_recalc'] = datetime.now().isoformat()
        
        with TinyDB('db.json') as db:
            db.upsert(schedule_doc, Query().id == "schedule")
        
        logger.info(f"✅ Battery limits recalculated ({len(new_schedule)} entries)")
        await self.scheduler_instance.schedule_actions()

    async def _get_battery_soc(self, bat_device):
        """Get current battery SOC, return 50% as fallback."""
        if bat_device.battery_soc_entity:
            state = await self.get_state(bat_device.battery_soc_entity)
            if state and state.get('state') not in ('unknown', 'unavailable'):
                try:
                    soc = float(state['state'])
                    logger.info(f"🔋 {bat_device.name}: SOC {soc:.1f}%")
                    return soc
                except (ValueError, TypeError):
                    pass
        
        logger.warning(f"⚠️ {bat_device.name}: Using fallback SOC 50%")
        return 50.0

    def _extract_times(self, schedule, device_key, horizon_start, slot_minutes):
        """Extract future time slots for a device from schedule.
        
        Expands merged blocks back into individual slot times.
        """
        times = []
        now = datetime.now()
        
        for entry in schedule:
            if entry.get('device') == device_key:
                start = datetime.fromisoformat(entry['start'])
                stop = datetime.fromisoformat(entry['stop'])
                
                # Expand merged blocks into individual slots
                current = start
                slot_delta = timedelta(minutes=slot_minutes)
                
                while current < stop:
                    # Only add slots that haven't ended yet (includes in-progress slots)
                    slot_end = current + slot_delta
                    if slot_end > now:
                        # Convert to HH:MM format relative to horizon
                        minutes_from_horizon = int((current - horizon_start).total_seconds() / 60)
                        times.append(f"{minutes_from_horizon // 60:02d}:{minutes_from_horizon % 60:02d}")
                    current += slot_delta
        
        return times

    def _times_to_schedule(self, times, device_key, horizon_start, slot_minutes):
        """Convert time strings to ISO schedule entries."""
        return slots_to_iso_ranges(
            times, device_key, horizon_start.date(), horizon_start, block_minutes=slot_minutes
        ) if times else []

    async def _get_predicted_usage(self, slot_minutes):
        """Get predicted power usage interpolated to slot_minutes intervals."""
        try:
            access_token = self.ha_client.get_access_token()
            stats_loader = StatisticsLoader(access_token)
            weather = Weather(access_token)
            predictor = Prediction(stats_loader, weather, self.price_history_manager)
            
            predicted_df = await predictor.calculatePowerUsage()
            if predicted_df is not None and len(predicted_df) > 0:
                # Interpolate hourly predictions to slot_minutes intervals
                predicted_df = predicted_df.set_index('timestamp')
                predicted_df = predicted_df.resample(f'{slot_minutes}min').interpolate(method='linear')
                predicted_df = predicted_df.reset_index()
                
                # Convert from kWh per hour to kWh per slot
                predicted_df['predicted_kwh'] = predicted_df['predicted_kwh'] * (slot_minutes / 60)
                
                usage = predicted_df[['timestamp', 'predicted_kwh']].to_dict('records')
                logger.debug(f"🔋 Retrieved {len(usage)} {slot_minutes}-min slots of predicted power usage")
                return usage
        except Exception as e:
            logger.warning(f"⚠️ Could not get predicted power usage: {e}")
        return None