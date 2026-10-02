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
from .optimization import optimize_wp, optimize_hw, optimize_battery, optimize_bat_discharge, limit_battery_cycles, plan_ev_deadline_charge, read_ev_deadline_inputs
from .conditions import evaluate_condition_group
from .ev_charging_state import get_ev_charging_state
from .utils import slot_to_time, slots_to_iso_ranges, merge_sequential_timeslots, time_to_slot
from .config import CONFIG
from .price_fetcher import EntsoeePriceFetcher
from .devices_config import devices_config
from .forecasting.price_history import PriceHistoryManager
from .forecasting.curtailment_history import CurtailmentHistoryManager
from .forecasting.statistics_loader import StatisticsLoader
from .forecasting.weather import Weather
from .forecasting.prediction import Prediction
from .forecasting.battery_soc_prediction import predict_battery_soc
from .forecasting.ev_soc_prediction import predict_ev_soc
from .runtime_calculator import RuntimeCalculator

logger = logging.getLogger(__name__)


def _compose_ev_headline(plan_headline, charging, charging_power_w):
    """One headline for the EV deadline card: charging state first, plan status second.

    An actively charging car takes the front of the line — "waiting for the car to be ready"
    or "scheduled" next to a charger that is already delivering power is exactly what read
    as confusing before.
    """
    if not charging:
        return plan_headline
    label = f" – {charging_power_w / 1000:.1f} kW" if charging_power_w is not None else ""
    charging_headline = f"Charging now{label}"
    return f"{charging_headline} · {plan_headline}" if plan_headline else charging_headline


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
        retry_interval = CONFIG['options'].get('price_fetch_retry_interval_minutes', 5)
        retry_max_hours = CONFIG['options'].get('price_fetch_retry_max_hours', 2)
        self.price_fetcher = EntsoeePriceFetcher(
            entsoe_token, entsoe_country, retry_interval, retry_max_hours
        ) if entsoe_token else None
        
        # Initialize Price History Manager for percentile calculations
        self.price_history_manager = PriceHistoryManager(entsoe_token, entsoe_country) if entsoe_token else None

        # Records curtailed hours (grid export blocked) so they can be excluded from solar training
        self.curtailment_history_manager = CurtailmentHistoryManager()

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
        SLOT_MINUTES = CONFIG['options'].get('slot_minutes', 15)
        LOCK_HOURS = CONFIG['options'].get('lock_hours', 2)  # Don't reschedule actions within 2 hours

        # Battery percentile configuration (for dynamic price thresholds)
        BAT_PRICE_HISTORY_DAYS = CONFIG['options'].get('battery_price_history_days', 14)
        BAT_CHARGE_PERCENTILE = CONFIG['options'].get('battery_charge_percentile', 30)
        BAT_DISCHARGE_PERCENTILE = CONFIG['options'].get('battery_discharge_percentile', 70)
        BAT_PRICE_DIFF_THRESHOLD = CONFIG['options'].get('battery_price_difference_threshold', 0.10)

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
        # Full day's min price for discharge threshold calculations (includes prices before current slot)
        full_day_min_price = horizon_data.get('full_day_min_price')
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
        runtime_calc = RuntimeCalculator()
        
        for wp_device in wp_devices:
            device_name = wp_device.name
            # Use device-specific config with fallback defaults
            WP_BLOCK_HOURS = wp_device.block_hours if wp_device.block_hours is not None else 1.0
            WP_MIN_GAP_HOURS = wp_device.min_gap_hours if wp_device.min_gap_hours is not None else 3.0
            WP_MAX_GAP_HOURS = wp_device.max_gap_hours if wp_device.max_gap_hours is not None else 8.0

            # ── Temperature-based optimization disable check ──────────────────────
            if (wp_device.disable_optimization_above_avg_temp is not None
                    and wp_device.outside_temp_sensor):
                avg_temp = await self.ha_client.get_avg_temperature_48h(wp_device.outside_temp_sensor)
                if avg_temp is not None and avg_temp > wp_device.disable_optimization_above_avg_temp:
                    logger.info(
                        f"🌡️ {device_name}: Skipping optimization — 48h average outside temperature "
                        f"({avg_temp:.1f}°C) exceeds threshold ({wp_device.disable_optimization_above_avg_temp}°C)"
                    )
                    results[device_name] = []
                    continue
                elif avg_temp is not None:
                    logger.info(
                        f"🌡️ {device_name}: 48h average outside temperature {avg_temp:.1f}°C "
                        f"— below threshold ({wp_device.disable_optimization_above_avg_temp}°C), optimization enabled"
                    )
            # ─────────────────────────────────────────────────────────────────────
            
            # Calculate expected daily runtime from historical data
            expected_daily_runtime = None
            if (wp_device.inside_temp_sensor and 
                wp_device.outside_temp_sensor and 
                wp_device.heatpump_status_sensor):
                
                # Use RuntimeCalculator to load history, calculate, and store runtime
                expected_daily_runtime = await runtime_calc.calculate_and_store_daily_runtime(
                    ha_url=self.ha_client.ha_url,
                    access_token=self.ha_client.get_access_token(),
                    device_name=device_name,
                    inside_temp_sensor=wp_device.inside_temp_sensor,
                    outside_temp_sensor=wp_device.outside_temp_sensor,
                    heatpump_status_sensor=wp_device.heatpump_status_sensor,
                    days_back=10
                )
            else:
                logger.debug(f"ℹ️ {device_name}: Runtime sensors not configured, skipping runtime calculation")
            
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
                slot_to_time=slot_to_time,
                expected_daily_runtime=expected_daily_runtime
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
            # Use device-specific config with fallback defaults
            HW_BLOCK_HOURS = hw_device.block_hours if hw_device.block_hours is not None else 1.0
            HW_MIN_GAP_HOURS = hw_device.min_gap_hours if hw_device.min_gap_hours is not None else 6.0
            HW_MAX_GAP_HOURS = hw_device.max_gap_hours if hw_device.max_gap_hours is not None else 12.0
            
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
        discharge_price_context = {}  # Store price context for preserving discharge decisions
        
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
            # Note: optimize_bat_discharge returns a tuple (times, price_context)
            # Pass full_day_min_price to preserve discharge decisions even when low prices have passed
            bat_discharge_times, bat_price_context = optimize_bat_discharge(
                prices=prices,
                slot_minutes=slot_minutes,
                slot_to_time=slot_to_time,
                min_discharge_price=min_discharge_price,
                price_difference_threshold=BAT_PRICE_DIFF_THRESHOLD,
                reference_min_price=full_day_min_price
            )
            
            # Store price context for this device (used to preserve decisions on recalculation)
            discharge_price_context[device_name] = bat_price_context
            
            # Store ORIGINAL times (before SOC limiting) for informative display
            original_battery_times[f"{device_name}_charge_planned"] = list(bat_charge_times) if bat_charge_times else []
            original_battery_times[f"{device_name}_discharge_planned"] = list(bat_discharge_times) if bat_discharge_times else []
            
            # Store in results (will be overwritten with limited times below)
            results[f"{device_name}_charge"] = bat_charge_times
            results[f"{device_name}_discharge"] = bat_discharge_times

            # Compute export-blocking slots for negative-price periods
            if bat_device.price_based_solar_grid_export:
                threshold = bat_device.grid_export_block_threshold
                block_grid_export_times = [
                    slot_to_time(i, slot_minutes)
                    for i, price in enumerate(prices)
                    if price < threshold
                ]
                results[f"{device_name}_block_grid_export"] = block_grid_export_times
                logger.info(f"☀️ {device_name}: {len(block_grid_export_times)} slot(s) below {threshold} €/kWh → grid export will be blocked")

        # ===== EV OPTIMIZATION (iterate over all EV devices) =====
        ev_devices = devices_config.get_devices_by_type('ev')
        for ev_device in ev_devices:
            device_name = ev_device.name
            if ev_device.solar_charge_only:
                logger.info(f"🚗 {device_name}: solar_charge_only=True, handled by the EV solar charge controller")
                continue
            if ev_device.ev_deadline_charge_enabled:
                logger.info(f"🚗 {device_name}: ev_deadline_charge_enabled=True, plan computed by recalculate_ev_deadline_plans()")
                continue
            logger.warning(
                f"🚗 {device_name}: neither solar_charge_only nor ev_deadline_charge_enabled is set — "
                "no charging schedule will be created for this device"
            )

        logger.info(f"⚙️ Optimization Results (before SOC limiting): {json.dumps(results)}")

        # Convert results to ISO time ranges for scheduling
        # Build device_block_minutes dynamically based on device type
        device_block_minutes = {}
        for device in devices_config.devices:
            if device.type == 'wp':
                block_hours = device.block_hours if device.block_hours is not None else 1.0
                device_block_minutes[device.name] = int(block_hours * 60)
            elif device.type == 'hw':
                block_hours = device.block_hours if device.block_hours is not None else 1.0
                device_block_minutes[device.name] = int(block_hours * 60)
            elif device.type == 'battery':
                # Battery has separate charge and discharge entries
                device_block_minutes[f"{device.name}_charge"] = slot_minutes  # Single slot
                device_block_minutes[f"{device.name}_discharge"] = slot_minutes  # Single slot
                if device.price_based_solar_grid_export:
                    device_block_minutes[f"{device.name}_block_grid_export"] = slot_minutes  # Single slot
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
                },
                "discharge_price_context": discharge_price_context  # Preserve min price for discharge threshold calculations
            }, Query().id == "schedule")
        
        logger.info(f"✅ Optimization complete. Schedule saved to TinyDB.")
        
        # Calculate and cache production & consumption predictions (full ML run)
        await self._calculate_and_cache_predictions()
        
        # Run initial EV deadline-charging plan based on current SOC/target/deadline.
        # This runs BEFORE battery limiting: the scheduled EV energy is added to the usage
        # prediction that decides whether solar-only mode is enabled.
        await self.recalculate_ev_deadline_plans()

        # Run initial battery cycle limiting based on current SOC
        await self.recalculate_battery_limits()

        # Schedule actions from database
        await self.scheduler_instance.schedule_actions()

    async def recalculate_battery_limits(self):
        """Recalculate battery cycle limits based on current SOC.
        
        Called after optimization and every 15 minutes to adapt to actual SOC changes.

        Solar-only mode is evaluated globally: when total predicted solar production
        exceeds total predicted power usage, battery charging and discharging are
        disabled entirely and a solar_only schedule entry is added instead.
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

        # ===== SOLAR-ONLY STATE EVALUATION =====
        # Compare total predicted solar production vs total predicted power usage.
        # When solar exceeds usage, there is enough renewable energy to cover all needs,
        # so battery grid-charging and discharging should be suspended.
        predicted_usage = Prediction.get_cached_usage(slot_minutes)
        predicted_solar = Prediction.get_cached_solar(slot_minutes)

        solar_only_mode = False
        solar_only_evaluation = None
        if predicted_solar and predicted_usage:
            total_solar_kwh = sum(p['predicted_kwh'] for p in predicted_solar)
            base_usage_kwh = sum(p['predicted_kwh'] for p in predicted_usage)
            # An EV that is ready to charge and has deadline slots scheduled is a real,
            # known load on top of the ML usage prediction — count it, otherwise a sunny
            # day could switch the battery to solar-only while the car quietly eats the
            # surplus. Provisional (on-hold) plans are NOT counted.
            ev_deadline_kwh = self._scheduled_ev_deadline_kwh()
            total_usage_kwh = base_usage_kwh + ev_deadline_kwh
            logger.info(
                f"☀️ Solar prediction: {total_solar_kwh:.2f} kWh | "
                f"Usage prediction: {base_usage_kwh:.2f} kWh"
                + (f" + scheduled EV charge {ev_deadline_kwh:.2f} kWh = {total_usage_kwh:.2f} kWh"
                   if ev_deadline_kwh else "")
            )
            if total_solar_kwh > total_usage_kwh:
                solar_only_mode = True
                logger.info(
                    f"☀️ Solar-only mode ACTIVATED "
                    f"(solar {total_solar_kwh:.2f} kWh > usage {total_usage_kwh:.2f} kWh) – "
                    "battery charge/discharge suspended"
                )
            else:
                logger.info("☀️ Solar-only mode not active – proceeding with normal battery optimization")
            solar_only_evaluation = {
                'solar_kwh': round(total_solar_kwh, 3),
                'usage_kwh': round(base_usage_kwh, 3),
                'ev_deadline_kwh': round(ev_deadline_kwh, 3),
                'total_usage_kwh': round(total_usage_kwh, 3),
                'solar_only_mode': solar_only_mode,
                'evaluated_at': datetime.now().isoformat(),
            }
        else:
            logger.info("☀️ Insufficient solar/usage predictions – proceeding with normal battery optimization")
        
        # Keep all non-battery entries from current schedule
        new_schedule = [
            entry for entry in schedule_doc.get('schedule', [])
            if not (entry.get('device', '').endswith('_charge') or 
                   entry.get('device', '').endswith('_discharge') or
                   entry.get('device', '').endswith('_solar_only'))
        ]

        # Accumulate SOC predictions for all battery devices
        battery_soc_predictions: dict[str, list[dict]] = {}
        
        # Process each battery device
        for bat_device in devices_config.get_devices_by_type('battery'):

            if solar_only_mode:
                # Solar-only mode: skip charge/discharge entirely and add a solar_only entry
                # spanning the full optimization horizon so the inverter can be configured
                # to allow passive solar charging.
                new_schedule.append({
                    "device": f"{bat_device.name}_solar_only",
                    "start": horizon_start.isoformat(),
                    "stop": horizon_end.isoformat(),
                })
                logger.info(
                    f"☀️ {bat_device.name}: Added solar_only entry "
                    f"({horizon_start} → {horizon_end})"
                )
                continue

            # Normal mode: apply SOC-based cycle limiting
            # Get current SOC (or use 50% as fallback)
            current_soc = await self._get_battery_soc(bat_device)
            
            # Extract original times from stored schedule (already filtered for future slots)
            original_charge = self._extract_times(original_battery_schedule, f"{bat_device.name}_charge_planned", horizon_start, slot_minutes)
            original_discharge = self._extract_times(original_battery_schedule, f"{bat_device.name}_discharge_planned", horizon_start, slot_minutes)

            # Extract previously limited times from current schedule to preserve past planned slots
            current_schedule = schedule_doc.get('schedule', [])
            prev_limited_charge = self._extract_times(current_schedule, f"{bat_device.name}_charge", horizon_start, slot_minutes)
            prev_limited_discharge = self._extract_times(current_schedule, f"{bat_device.name}_discharge", horizon_start, slot_minutes)

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
                    predicted_solar=predicted_solar,
                    device_name=bat_device.name,
                    previous_limited_charge_times=prev_limited_charge,
                    previous_limited_discharge_times=prev_limited_discharge,
                )
            else:
                # Use original times if battery not fully configured
                logger.warning(f"⚠️ {bat_device.name}: Missing battery config, using original times")
                limited_charge, limited_discharge = original_charge, original_discharge
            
            # Add limited times to schedule
            new_schedule.extend(self._times_to_schedule(limited_charge, f"{bat_device.name}_charge", horizon_start, slot_minutes))
            new_schedule.extend(self._times_to_schedule(limited_discharge, f"{bat_device.name}_discharge", horizon_start, slot_minutes))
        
            # Compute battery SOC prediction for this device
            if bat_device.battery_capacity_kwh and bat_device.battery_charge_speed_kw:
                try:
                    soc_prediction = predict_battery_soc(
                        charge_times=limited_charge,
                        discharge_times=limited_discharge,
                        slot_minutes=slot_minutes,
                        horizon_start=horizon_start,
                        horizon_end=horizon_end,
                        current_soc=current_soc,
                        battery_capacity_kwh=bat_device.battery_capacity_kwh,
                        battery_charge_speed_kw=bat_device.battery_charge_speed_kw,
                        min_soc_percent=bat_device.battery_min_soc_percent or 20.0,
                        max_soc_percent=bat_device.battery_max_soc_percent or 80.0,
                        device_name=bat_device.name,
                        predicted_power_usage=predicted_usage,
                        predicted_solar=predicted_solar,
                    )
                    battery_soc_predictions[bat_device.name] = soc_prediction
                except Exception as e:
                    logger.warning(f"⚠️ {bat_device.name}: Could not compute SOC prediction: {e}")

        # Merge and save updated schedule
        new_schedule = merge_sequential_timeslots([new_schedule])
        schedule_doc['schedule'] = new_schedule
        schedule_doc['last_soc_recalc'] = datetime.now().isoformat()
        schedule_doc['solar_only_mode'] = solar_only_mode
        schedule_doc['solar_only_evaluation'] = solar_only_evaluation
        
        with TinyDB('db.json') as db:
            db.upsert(schedule_doc, Query().id == "schedule")

        # Save battery SOC predictions alongside usage/solar predictions
        if battery_soc_predictions:
            with TinyDB('db.json') as db:
                existing = db.get(Query().id == 'predictions') or {}
                existing.update({
                    'id': 'predictions',
                    'battery_soc': battery_soc_predictions,
                    'updated_at': datetime.now().isoformat()
                })
                db.upsert(existing, Query().id == 'predictions')
            total_points = sum(len(v) for v in battery_soc_predictions.values())
            logger.info(f"🔋 Saved battery SOC predictions for {len(battery_soc_predictions)} device(s) ({total_points} data points)")
        
        logger.info(f"✅ Battery limits recalculated ({len(new_schedule)} entries)")
        await self.scheduler_instance.schedule_actions()

    async def recalculate_ev_deadline_plans(self, full_replan: bool = True):
        """Recalculate EV deadline-charging plans based on current SOC, target, and deadline.

        Called after the daily optimization, on the dashboard "Recalculate now" button (both
        ``full_replan=True``, the only two things that actually pick new charge slots), and
        every 15 minutes in the background (``full_replan=False``, mirrors
        recalculate_battery_limits) purely to re-apply the EV-readiness gate below — a car that
        is charging slower than predicted must NOT cause more slots to be added outside of an
        explicit re-plan, so the background cycle carries the previously committed plan forward
        unchanged (see ``lock_plan`` in optimization/ev_deadline.py) instead of re-running slot
        selection against the live SOC.

        The plan is always *computed* (so the UI can show what would be charged), but it is
        only *scheduled* when the EV is ready to charge according to its
        ``ev_ready_to_charge_condition`` (e.g. plugged in). When the EV is not ready, only the
        slot currently in progress (if any) stays in the live schedule so its stop action still
        fires; everything else is held back until the car is ready. Devices without a
        readiness condition are scheduled unconditionally, as before.

        Returns the summary written to the ``ev_deadline`` TinyDB doc (or None when skipped),
        so the manual-trigger endpoint can report readiness back to the dashboard.
        """
        ev_deadline_devices = [
            d for d in devices_config.get_devices_by_type('ev') if d.ev_deadline_charge_enabled
        ]
        if not ev_deadline_devices:
            return None

        logger.info("🚗 Recalculating EV deadline-charging plans...")

        with TinyDB('db.json') as db:
            schedule_doc = db.get(Query().id == "schedule")

        if not schedule_doc or not schedule_doc.get('horizon_start') or not schedule_doc.get('prices'):
            logger.warning("⚠️ No valid schedule found, skipping EV deadline recalculation")
            return None

        horizon_start = datetime.fromisoformat(schedule_doc['horizon_start'])
        horizon_end = datetime.fromisoformat(schedule_doc['horizon_end'])

        if datetime.now() >= horizon_end:
            logger.info("📅 Horizon expired, skipping EV deadline recalculation")
            return None

        prices = schedule_doc['prices']
        slot_minutes = schedule_doc.get('slot_minutes', 15)
        current_schedule = schedule_doc.get('schedule', [])
        now = datetime.now().replace(tzinfo=None)

        ev_device_names = {d.name for d in ev_deadline_devices}
        new_schedule = [
            entry for entry in current_schedule
            if entry.get('device') not in ev_device_names
        ]

        ev_soc_predictions = {}
        device_summaries = {}

        for ev_device in ev_deadline_devices:
            previous_charge_times = self._extract_times(current_schedule, ev_device.name, horizon_start, slot_minutes)

            # Observed charging state (ev_charging_condition, else the charger's power meter).
            # Independent of readiness: a car that is already charging must not still be
            # announced as merely "ready to charge" in the UI/dashboard.
            charging_state = await get_ev_charging_state(self.get_state, ev_device)

            if not ev_device.ev_battery_capacity_kwh:
                logger.warning(f"⚠️ {ev_device.name}: ev_battery_capacity_kwh not configured, skipping deadline planning")
                new_schedule.extend(self._times_to_schedule(previous_charge_times, ev_device.name, horizon_start, slot_minutes))
                device_summaries[ev_device.name] = self._ev_deadline_summary(
                    ev_device, ready=True, ready_configured=False, scheduled=bool(previous_charge_times),
                    status='not_configured',
                    message='ev_battery_capacity_kwh is not configured for this device; deadline planning is skipped.',
                    charging_state=charging_state,
                )
                continue

            current_soc, target_soc, deadline = await read_ev_deadline_inputs(self.get_state, ev_device, now)

            charge_power_kw = ev_device.ev_deadline_charge_power_kw
            if not charge_power_kw:
                charge_power_kw = ev_device.ev_max_current_limit * 230 / 1000
                logger.debug(
                    f"🚗 {ev_device.name}: ev_deadline_charge_power_kw not set, estimated "
                    f"{charge_power_kw:.2f} kW from ev_max_current_limit at 230V"
                )

            plan = plan_ev_deadline_charge(
                prices=prices,
                slot_minutes=slot_minutes,
                horizon_start=horizon_start,
                current_soc=current_soc,
                target_soc=target_soc,
                deadline=deadline,
                ev_battery_capacity_kwh=ev_device.ev_battery_capacity_kwh,
                charge_power_kw=charge_power_kw,
                device_name=ev_device.name,
                previous_charge_times=previous_charge_times,
                lock_plan=not full_replan,
            )

            # Readiness gate: None = no condition configured (always schedule), otherwise
            # the condition must be certainly TRUE (unknown/unavailable counts as not ready,
            # see conditions.evaluate_condition_group).
            ready = await self._ev_ready_state(ev_device)
            ready_configured = ready is not None
            if ready is False:
                scheduled_times = plan.in_progress_times
                held = [t for t in plan.charge_times if t not in plan.in_progress_times]
                logger.info(
                    f"🔌 {ev_device.name}: EV not ready to charge (ev_ready_to_charge_condition is not "
                    f"satisfied) — {len(held)} planned slot(s) held back until the car is ready"
                    + (f"; keeping the in-progress slot {plan.in_progress_times}" if plan.in_progress_times else "")
                )
            else:
                scheduled_times = plan.charge_times

            new_schedule.extend(self._times_to_schedule(scheduled_times, ev_device.name, horizon_start, slot_minutes))

            device_summaries[ev_device.name] = self._ev_deadline_summary(
                ev_device,
                ready=ready if ready_configured else True,
                ready_configured=ready_configured,
                scheduled=bool(scheduled_times),
                status=plan.status,
                message=plan.message,
                plan=plan,
                scheduled_times=scheduled_times,
                current_soc=current_soc,
                target_soc=target_soc,
                deadline=deadline,
                charge_power_kw=charge_power_kw,
                horizon_start=horizon_start,
                slot_minutes=slot_minutes,
                charging_state=charging_state,
            )

            if current_soc is not None:
                try:
                    ev_soc_predictions[ev_device.name] = predict_ev_soc(
                        charge_times=scheduled_times,
                        slot_minutes=slot_minutes,
                        horizon_start=horizon_start,
                        horizon_end=horizon_end,
                        current_soc=current_soc,
                        ev_battery_capacity_kwh=ev_device.ev_battery_capacity_kwh,
                        charge_power_kw=charge_power_kw,
                        device_name=ev_device.name,
                    )
                except Exception as e:
                    logger.warning(f"⚠️ {ev_device.name}: could not compute EV SOC prediction: {e}")

        new_schedule = merge_sequential_timeslots([new_schedule])
        schedule_doc['schedule'] = new_schedule
        schedule_doc['last_ev_deadline_recalc'] = datetime.now().isoformat()

        with TinyDB('db.json') as db:
            db.upsert(schedule_doc, Query().id == "schedule")

        if ev_soc_predictions:
            with TinyDB('db.json') as db:
                existing = db.get(Query().id == 'predictions') or {}
                existing.update({
                    'id': 'predictions',
                    'ev_soc': ev_soc_predictions,
                    'updated_at': datetime.now().isoformat()
                })
                db.upsert(existing, Query().id == 'predictions')

        # Persist the plan summaries (scheduled AND held-back plans) for the web UI info box,
        # the /api/ev_deadline endpoint polled by the HA package, and the solar-only decision.
        first_name = next(iter(device_summaries), None)
        ev_deadline_doc = {
            'id': 'ev_deadline',
            'updated_at': datetime.now().isoformat(),
            'devices': device_summaries,
            'primary': device_summaries[first_name] if first_name else None,
        }
        with TinyDB('db.json') as db:
            db.upsert(ev_deadline_doc, Query().id == 'ev_deadline')

        logger.info(f"✅ EV deadline plans recalculated ({len(new_schedule)} entries)")
        await self.scheduler_instance.schedule_actions()
        return {k: v for k, v in ev_deadline_doc.items() if k != 'id'}

    async def _ev_ready_state(self, ev_device):
        """True/False from ``ev_ready_to_charge_condition``; None when no condition is configured."""
        cond = getattr(ev_device, 'ev_ready_to_charge_condition', None)
        if not cond or not cond.conditions:
            return None
        return await evaluate_condition_group(cond, self.get_state)

    async def refresh_ev_charging_state(self):
        """Re-read each deadline EV's live charging state into the ``ev_deadline`` doc.

        The full plan is only recalculated every 15 minutes, which is far too slow for a
        "is the car charging right now?" badge — so this runs on the load-watcher interval
        and rewrites just the charging fields (and the charging half of the headline) of the
        summaries already stored. It never touches the plan or the schedule.

        No-op until recalculate_ev_deadline_plans() has written the doc at least once.
        """
        ev_deadline_devices = [
            d for d in devices_config.get_devices_by_type('ev') if d.ev_deadline_charge_enabled
        ]
        if not ev_deadline_devices:
            return None

        with TinyDB('db.json') as db:
            doc = db.get(Query().id == 'ev_deadline')
        if not doc or not doc.get('devices'):
            return None

        summaries = doc['devices']
        changed = False
        for ev_device in ev_deadline_devices:
            summary = summaries.get(ev_device.name)
            if summary is None:
                continue
            state = await get_ev_charging_state(self.get_state, ev_device)
            charging = state['charging'] is True
            power_w = state['power_w']
            # A doc written before plan_headline existed only has the combined headline; adopt
            # it as the plan half once, so the next refresh doesn't prefix "Charging now" twice.
            plan_headline = summary.get('plan_headline') or summary.get('headline')
            updated = {
                'plan_headline': plan_headline,
                'charging': charging,
                'charging_power_w': round(power_w, 1) if power_w is not None else None,
                'charging_power_kw': round(power_w / 1000, 3) if power_w is not None else None,
                'charging_source': state['source'],
                'charging_condition_configured': state['condition_configured'],
                'charging_load_entity': state['load_entity'],
                'headline': _compose_ev_headline(plan_headline, charging, power_w),
            }
            if any(summary.get(k) != v for k, v in updated.items()):
                if summary.get('charging') != charging:
                    logger.info(
                        f"🚗 {ev_device.name}: charging state -> {'charging' if charging else 'not charging'}"
                        + (f" ({power_w / 1000:.2f} kW)" if power_w is not None else '')
                    )
                summary.update(updated)
                changed = True

        if not changed:
            return doc.get('devices')

        first_name = next(iter(summaries), None)
        doc['devices'] = summaries
        doc['primary'] = summaries[first_name] if first_name else None
        doc['charging_updated_at'] = datetime.now().isoformat()
        with TinyDB('db.json') as db:
            db.upsert(doc, Query().id == 'ev_deadline')
        return summaries

    def _ev_deadline_summary(self, ev_device, ready, ready_configured, scheduled, status, message,
                             plan=None, scheduled_times=None, current_soc=None, target_soc=None,
                             deadline=None, charge_power_kw=None, horizon_start=None, slot_minutes=15,
                             charging_state=None):
        """Build the per-device dict stored in the ``ev_deadline`` TinyDB doc.

        ``slots`` is the full computed plan (merged ISO ranges) even when it is on hold, so the
        UI can show what will be charged once the EV is ready; ``scheduled`` says whether that
        plan is currently in the live schedule.

        ``charging_state`` is the live observation from
        :func:`src.ev_charging_state.get_ev_charging_state`; when it says the car is drawing
        power that wins the headline, because "ready to charge" reads as "not charging yet"
        to anyone looking at a car that is already charging.
        """
        def _ranges(times):
            if not times or horizon_start is None:
                return []
            merged = merge_sequential_timeslots([
                self._times_to_schedule(times, ev_device.name, horizon_start, slot_minutes)
            ])
            return [{'start': e['start'], 'stop': e['stop']} for e in merged]

        planned_slots = _ranges(plan.charge_times if plan else [])
        scheduled_slots = _ranges(scheduled_times or [])

        charging_state = charging_state or {}
        charging = charging_state.get('charging') is True
        charging_power_w = charging_state.get('power_w')

        if status == 'not_configured':
            headline = 'Device not configured for deadline charging'
        elif not ready:
            headline = 'Waiting for the car to be ready'
            if plan and plan.charge_times:
                headline = f"On hold: {len(plan.charge_times)} slot(s) ready to schedule"
        elif status == 'planned':
            headline = f"Scheduled: {len(scheduled_times or [])} slot(s), ~{plan.energy_planned_kwh:.1f} kWh"
        elif status == 'locked':
            headline = f"Scheduled (fixed): {len(scheduled_times or [])} slot(s), ~{plan.energy_planned_kwh:.1f} kWh"
        elif status == 'infeasible':
            headline = 'Scheduled: every remaining slot (deadline too close)'
        elif status == 'partial_horizon':
            headline = f"Scheduled: {len(scheduled_times or [])} slot(s) so far (more prices pending)"
        elif status == 'target_reached':
            headline = 'Target SOC reached'
        elif status == 'missing_inputs':
            headline = 'Inputs unavailable'
        elif status == 'no_slots':
            headline = 'No slots left before the deadline'
        else:
            headline = status.replace('_', ' ').capitalize()

        # An actively charging car takes the headline: "waiting for the car to be ready" or
        # "scheduled" next to a charger that is already delivering power is what confused
        # people in the first place. The plan headline is kept as the second half.
        plan_headline = headline
        headline = _compose_ev_headline(plan_headline, charging, charging_power_w)

        return {
            'device': ev_device.name,
            'ready': ready,
            'ready_condition_configured': ready_configured,
            'charging': charging,
            'charging_power_w': round(charging_power_w, 1) if charging_power_w is not None else None,
            'charging_power_kw': round(charging_power_w / 1000, 3) if charging_power_w is not None else None,
            'charging_source': charging_state.get('source'),
            'charging_condition_configured': charging_state.get('condition_configured', False),
            'charging_load_entity': charging_state.get('load_entity'),
            'scheduled': scheduled,
            'status': status,
            'headline': headline,
            # The plan half of the headline on its own, so refresh_ev_charging_state() can
            # rebuild the combined headline without re-planning.
            'plan_headline': plan_headline,
            'message': message,
            'current_soc': current_soc,
            'target_soc': target_soc,
            'deadline': deadline.isoformat() if deadline else None,
            'charge_power_kw': charge_power_kw,
            'energy_needed_kwh': round(plan.energy_needed_kwh, 3) if plan else 0.0,
            'energy_planned_kwh': round(plan.energy_planned_kwh, 3) if plan else 0.0,
            'energy_scheduled_kwh': round(len(scheduled_times or []) * (charge_power_kw or 0) * slot_minutes / 60, 3),
            'avg_price': round(plan.avg_price, 4) if plan and plan.avg_price is not None else None,
            'slot_count': len(plan.charge_times) if plan else 0,
            'scheduled_slot_count': len(scheduled_times or []),
            'slots': planned_slots,
            'scheduled_slots': scheduled_slots,
            'first_slot_start': planned_slots[0]['start'] if planned_slots else None,
            'last_slot_stop': planned_slots[-1]['stop'] if planned_slots else None,
            'updated_at': datetime.now().isoformat(),
        }

    def _scheduled_ev_deadline_kwh(self) -> float:
        """Energy (kWh) of EV deadline slots that are actually in the live schedule.

        Read from the ``ev_deadline`` doc written by recalculate_ev_deadline_plans(); only
        devices whose plan is scheduled (EV ready) count. Provisional plans are ignored.
        """
        try:
            with TinyDB('db.json') as db:
                doc = db.get(Query().id == 'ev_deadline')
        except Exception as e:
            logger.warning(f"⚠️ Could not read EV deadline plans: {e}")
            return 0.0
        if not doc:
            return 0.0
        total = 0.0
        for name, info in (doc.get('devices') or {}).items():
            if info.get('scheduled'):
                total += float(info.get('energy_scheduled_kwh') or 0.0)
        return total

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
        """Extract time slots for a device from schedule.
        
        Expands merged blocks back into individual slot times.
        Returns all slots (past and future) - filtering is done by limit_battery_cycles.
        """
        times = []
        
        for entry in schedule:
            if entry.get('device') == device_key:
                start = datetime.fromisoformat(entry['start'])
                stop = datetime.fromisoformat(entry['stop'])
                
                # Expand merged blocks into individual slots
                current = start
                slot_delta = timedelta(minutes=slot_minutes)
                
                while current < stop:
                    # Convert to HH:MM format relative to horizon
                    minutes_from_horizon = int((current - horizon_start).total_seconds() / 60)
                    if minutes_from_horizon >= 0:  # Only include slots within the horizon
                        times.append(f"{minutes_from_horizon // 60:02d}:{minutes_from_horizon % 60:02d}")
                    current += slot_delta
        
        return times

    def _times_to_schedule(self, times, device_key, horizon_start, slot_minutes):
        """Convert time strings to ISO schedule entries."""
        return slots_to_iso_ranges(
            times, device_key, horizon_start.date(), horizon_start, block_minutes=slot_minutes
        ) if times else []

    async def _calculate_and_cache_predictions(self):
        """Run full ML predictions for power usage and solar production, writing results to TinyDB.
        
        Called by run_optimization() on startup and at 16:05. The resulting cache is then
        consumed by _get_predicted_usage() and _get_predicted_solar() during subsequent
        recalculate_battery_limits() calls, avoiding repeated API and ML overhead.
        """
        logger.info("🤖 Calculating and caching production & consumption predictions...")
        
        access_token = self.ha_client.get_access_token()
        stats_loader = StatisticsLoader(access_token)
        weather = Weather(access_token)
        predictor = Prediction(stats_loader, weather, self.price_history_manager,
                               self.curtailment_history_manager)
        
        try:
            await predictor.calculatePowerUsage()
            logger.info("✅ Power usage predictions cached")
        except Exception as e:
            logger.warning(f"⚠️ Could not calculate power usage predictions: {e}")
        
        try:
            await predictor.calculateSolarProduction()
            logger.info("✅ Solar production predictions cached")
        except Exception as e:
            logger.warning(f"⚠️ Could not calculate solar production predictions: {e}")