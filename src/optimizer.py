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
from .optimization import optimize_wp, optimize_hw, plan_battery_lp, plan_ev_deadline_charge, read_ev_deadline_inputs
from .optimization import plan_thermal_setpoints, build_comfort_floor
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

        logger.info("🔎 Starting energy optimization using ENTSO-E prices (rolling horizon)...")

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

        # Battery charge/discharge slots are chosen by the whole-horizon LP in
        # recalculate_battery_limits() (which has the SOC + usage/solar predictions it needs);
        # the daily run here only computes the price-based grid-export block below.
        results = {}
        
        # ===== HEAT PUMP OPTIMIZATION (iterate over all WP devices) =====
        wp_devices = devices_config.get_devices_by_type('wp')
        runtime_calc = RuntimeCalculator()
        
        for wp_device in wp_devices:
            device_name = wp_device.name
            # Thermal pre-heating devices are planned by the setpoint LP in
            # recalculate_thermal_setpoints() (which has the measured room temp + predictions it
            # needs), not by the block MILP here — exactly as battery is handled.
            if wp_device.wp_preheat_enabled:
                logger.info(f"🔥 {device_name}: wp_preheat_enabled=True, planned by recalculate_thermal_setpoints()")
                continue
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

        # ===== BATTERY GRID-EXPORT BLOCKING (iterate over all battery devices) =====
        # Charge/discharge slots are planned by the LP in recalculate_battery_limits(); the only
        # price-based decision made here (no predictions needed) is when to block grid export.
        battery_devices = devices_config.get_devices_by_type('battery')
        for bat_device in battery_devices:
            device_name = bat_device.name

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

        # Save schedule to TinyDB. Battery charge/discharge entries (and the
        # original_battery_schedule display reference) are added by recalculate_battery_limits(),
        # called below once predictions are cached.
        with TinyDB('db.json') as db:
            db.upsert({
                "id": "schedule",
                "schedule": iso_times_merged,  # grid-export blocks now; battery charge/discharge added by recalc
                "original_battery_schedule": [],  # populated by the daily recalc (LP plan reference)
                "horizon_start": horizon_start.isoformat(),
                "horizon_end": horizon_end.isoformat(),
                "prices": prices,  # Store prices for recalculation
                "slot_minutes": slot_minutes,
                "updated_at": datetime.now().isoformat(),
            }, Query().id == "schedule")
        
        logger.info(f"✅ Optimization complete. Schedule saved to TinyDB.")
        
        # Calculate and cache production & consumption predictions (full ML run)
        await self._calculate_and_cache_predictions()
        
        # Run initial EV deadline-charging plan based on current SOC/target/deadline.
        # This runs BEFORE the battery LP so any scheduled EV energy is on record.
        await self.recalculate_ev_deadline_plans()

        # Plan heat-pump setpoints (thermal LP) before the battery LP so the battery recalc's
        # trailing schedule_actions() schedules the heat-pump entries too.
        await self.recalculate_thermal_setpoints(is_daily=True)

        # Run the battery LP for the first time (is_daily=True also records the plan reference).
        await self.recalculate_battery_limits(is_daily=True)

        # Schedule actions from database
        await self.scheduler_instance.schedule_actions()

    async def recalculate_battery_limits(self, is_daily: bool = False):
        """Plan battery charge/discharge for every battery device with a whole-horizon LP.

        Called at the end of the daily optimization (``is_daily=True``, which additionally
        records the plan as the dashboard's "planned" reference) and every 15 minutes
        (``is_daily=False``) to re-solve against the latest measured SOC.

        ``optimization/battery_lp.py`` is the single source of truth. One linear program over
        the whole price horizon decides each slot's mode, so there is no separate percentile
        selection, no greedy "cycle limiting", and no whole-day solar-only switch: the LP
        reserves cheap and solar energy for the dearest hours by construction, and seasonal
        behaviour (store midday solar in summer, arbitrage from the grid in winter) falls out
        of the per-slot load/solar/price inputs. A slot the LP grid-charges becomes a
        ``_charge`` entry, a slot it discharges to cover load becomes a ``_discharge`` entry,
        and every other slot is left to the inverter's hold state (which still absorbs solar
        surplus) — exactly the actuation the scheduler already drives.
        """
        logger.info("🔋 Planning battery charge/discharge (whole-horizon LP)...")

        with TinyDB('db.json') as db:
            schedule_doc = db.get(Query().id == "schedule")

        if not schedule_doc or not schedule_doc.get('horizon_start') or not schedule_doc.get('prices'):
            logger.warning("⚠️ No valid schedule found, skipping recalculation")
            return

        horizon_start = datetime.fromisoformat(schedule_doc['horizon_start'])
        horizon_end = datetime.fromisoformat(schedule_doc['horizon_end'])

        if datetime.now() >= horizon_end:
            logger.info("📅 Horizon expired, skipping recalculation")
            return

        prices = schedule_doc['prices']
        slot_minutes = schedule_doc.get('slot_minutes', 15)
        current_schedule = schedule_doc.get('schedule', [])

        predicted_usage = Prediction.get_cached_usage(slot_minutes)
        predicted_solar = Prediction.get_cached_solar(slot_minutes)

        round_trip_efficiency = CONFIG['options'].get('battery_round_trip_efficiency', 0.9)
        cycle_cost_per_kwh = CONFIG['options'].get('battery_cycle_cost_per_kwh', 0.0)

        # Keep every non-battery entry (grid-export blocks, EV, thermal); the LP rebuilds only
        # the battery charge/discharge/solar_only entries below.
        new_schedule = [
            entry for entry in current_schedule
            if not (entry.get('device', '').endswith('_charge') or
                    entry.get('device', '').endswith('_discharge') or
                    entry.get('device', '').endswith('_solar_only'))
        ]

        battery_soc_predictions: dict[str, list[dict]] = {}
        plan_summary_devices: dict[str, dict] = {}
        planned_reference_entries: list[dict] = []  # daily-only: the Gantt "plan" rows

        for bat_device in devices_config.get_devices_by_type('battery'):
            name = bat_device.name

            if not (bat_device.battery_capacity_kwh and bat_device.battery_charge_speed_kw):
                logger.warning(f"⚠️ {name}: battery_capacity_kwh / battery_charge_speed_kw not set — "
                               f"skipping battery planning for this device")
                continue

            current_soc = await self._get_battery_soc(bat_device)

            # Preserve already-committed past slots; the measured SOC already reflects them.
            prev_charge = self._extract_times(current_schedule, f"{name}_charge", horizon_start, slot_minutes)
            prev_discharge = self._extract_times(current_schedule, f"{name}_discharge", horizon_start, slot_minutes)

            # Exported solar earns the day-ahead price, except where this device blocks export
            # (near-zero / negative injection), where it earns nothing.
            export_block_threshold = (
                bat_device.grid_export_block_threshold if bat_device.price_based_solar_grid_export else 0.0
            )

            plan = plan_battery_lp(
                prices=prices,
                slot_minutes=slot_minutes,
                horizon_start=horizon_start,
                current_soc=current_soc,
                battery_capacity_kwh=bat_device.battery_capacity_kwh,
                battery_charge_speed_kw=bat_device.battery_charge_speed_kw,
                min_soc_percent=bat_device.battery_min_soc_percent or 20.0,
                max_soc_percent=bat_device.battery_max_soc_percent or 80.0,
                predicted_power_usage=predicted_usage,
                predicted_solar=predicted_solar,
                round_trip_efficiency=round_trip_efficiency,
                cycle_cost_per_kwh=cycle_cost_per_kwh,
                export_block_threshold=export_block_threshold,
                previous_charge_times=prev_charge,
                previous_discharge_times=prev_discharge,
                device_name=name,
            )
            charge_times, discharge_times = plan['charge_times'], plan['discharge_times']
            plan_summary_devices[name] = plan.get('economics', {})

            new_schedule.extend(self._times_to_schedule(charge_times, f"{name}_charge", horizon_start, slot_minutes))
            new_schedule.extend(self._times_to_schedule(discharge_times, f"{name}_discharge", horizon_start, slot_minutes))

            if is_daily:
                planned_reference_entries.extend(
                    self._times_to_schedule(charge_times, f"{name}_charge_planned", horizon_start, slot_minutes))
                planned_reference_entries.extend(
                    self._times_to_schedule(discharge_times, f"{name}_discharge_planned", horizon_start, slot_minutes))

            # Compute battery SOC prediction for this device
            try:
                soc_prediction = predict_battery_soc(
                    charge_times=charge_times,
                    discharge_times=discharge_times,
                    slot_minutes=slot_minutes,
                    horizon_start=horizon_start,
                    horizon_end=horizon_end,
                    current_soc=current_soc,
                    battery_capacity_kwh=bat_device.battery_capacity_kwh,
                    battery_charge_speed_kw=bat_device.battery_charge_speed_kw,
                    min_soc_percent=bat_device.battery_min_soc_percent or 20.0,
                    max_soc_percent=bat_device.battery_max_soc_percent or 80.0,
                    device_name=name,
                    predicted_power_usage=predicted_usage,
                    predicted_solar=predicted_solar,
                )
                battery_soc_predictions[name] = soc_prediction
            except Exception as e:
                logger.warning(f"⚠️ {name}: Could not compute SOC prediction: {e}")

        # Merge and save updated schedule
        new_schedule = merge_sequential_timeslots([new_schedule])
        schedule_doc['schedule'] = new_schedule
        schedule_doc['last_soc_recalc'] = datetime.now().isoformat()
        schedule_doc['solar_only_mode'] = False  # the LP handles seasons per-slot; no whole-day flip
        schedule_doc['battery_plan_summary'] = {
            'method': 'lp',
            'round_trip_efficiency': round_trip_efficiency,
            'cycle_cost_per_kwh': cycle_cost_per_kwh,
            'export_pricing': 'dynamic_day_ahead',
            'devices': plan_summary_devices,
            'evaluated_at': datetime.now().isoformat(),
        }
        # Drop the obsolete percentile-era keys so stale values never resurface in the UI.
        schedule_doc.pop('battery_price_thresholds', None)
        schedule_doc.pop('discharge_price_context', None)
        schedule_doc.pop('solar_only_evaluation', None)
        if is_daily:
            schedule_doc['original_battery_schedule'] = merge_sequential_timeslots([planned_reference_entries])

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

        logger.info(f"✅ Battery plan updated ({len(new_schedule)} entries)")
        await self.scheduler_instance.schedule_actions()

    async def _build_outside_temp_forecast(self, horizon_start, horizon_end, slot_minutes, n_slots, fallback_sensor=None):
        """Return a per-slot outside-temperature forecast for the horizon (°C).

        Fetches Open-Meteo hourly temperature (same source the solar forecast uses) and maps each
        slot to its hour. On any failure falls back to a flat series read from ``fallback_sensor``
        (the device's ``outside_temp_sensor``), or 10 °C if that is unreadable. The result is cached
        in the schedule doc by the daily run so the 15-minute recalculation needs no API call.
        """
        flat = 10.0
        if fallback_sensor:
            st = await self.get_state(fallback_sensor)
            if st and st.get('state') not in ('unknown', 'unavailable', None):
                try:
                    flat = float(st['state'])
                except (ValueError, TypeError):
                    pass
        try:
            weather = Weather(self.ha_client.get_access_token())
            df = await weather.getUpcomingHourlyWeather()
            # Map each forecast hour (naive local timestamp) to an outside temp.
            by_hour = {}
            for _, row in df.iterrows():
                ts = row['timestamp']
                ts = ts.to_pydatetime() if hasattr(ts, 'to_pydatetime') else ts
                by_hour[ts.replace(minute=0, second=0, microsecond=0, tzinfo=None)] = float(row['temperature'])
            series = []
            for i in range(n_slots):
                slot_dt = horizon_start + timedelta(minutes=i * slot_minutes)
                hour_key = slot_dt.replace(minute=0, second=0, microsecond=0)
                series.append(by_hour.get(hour_key, flat))
            # Fill any leading/trailing gaps with the nearest known value.
            if any(v is None for v in series):
                series = [v if v is not None else flat for v in series]
            logger.info(f"🌡️ Outside-temp forecast built for {n_slots} slots "
                        f"({min(series):.1f}–{max(series):.1f} °C)")
            return series
        except Exception as e:
            logger.warning(f"⚠️ Could not fetch outside-temp forecast ({e}); using flat {flat:.1f} °C")
            return [flat] * n_slots

    async def recalculate_thermal_setpoints(self, is_daily: bool = False):
        """Plan a room-temperature setpoint trajectory for every pre-heating-enabled heat pump.

        The thermal-mass counterpart of ``recalculate_battery_limits``: one whole-horizon LP
        (``optimization/thermal_preheat.py``) decides a setpoint per slot that pre-charges the floor
        slab in cheap/solar hours and coasts through price peaks down to the time-of-day comfort
        floor. Called at the end of the daily optimization (``is_daily=True``) and every 15 minutes
        to re-solve against the latest *measured* room temperature — the thermal analogue of the
        battery re-solving against measured SOC. Runs before the battery recalc in both sequences so
        that recalc's trailing ``schedule_actions()`` picks up the heat-pump entries too.
        """
        preheat_devices = [d for d in devices_config.get_devices_by_type('wp') if d.wp_preheat_enabled]
        if not preheat_devices:
            return

        logger.info("🔥 Planning heat-pump setpoints (whole-horizon thermal LP)...")

        with TinyDB('db.json') as db:
            schedule_doc = db.get(Query().id == "schedule")

        if not schedule_doc or not schedule_doc.get('horizon_start') or not schedule_doc.get('prices'):
            logger.warning("⚠️ No valid schedule found, skipping thermal recalculation")
            return

        horizon_start = datetime.fromisoformat(schedule_doc['horizon_start'])
        horizon_end = datetime.fromisoformat(schedule_doc['horizon_end'])
        if datetime.now() >= horizon_end:
            logger.info("📅 Horizon expired, skipping thermal recalculation")
            return

        prices = schedule_doc['prices']
        slot_minutes = schedule_doc.get('slot_minutes', 15)
        n_slots = len(prices)
        current_schedule = schedule_doc.get('schedule', [])

        predicted_usage = Prediction.get_cached_usage(slot_minutes)
        predicted_solar = Prediction.get_cached_solar(slot_minutes)

        # Outside-temp forecast: fetched & cached by the daily run, reused by the 15-min recalc.
        outside_temp = schedule_doc.get('outside_temp_forecast')
        if is_daily or not outside_temp or len(outside_temp) < n_slots:
            fallback_sensor = next((d.outside_temp_sensor for d in preheat_devices if d.outside_temp_sensor), None)
            outside_temp = await self._build_outside_temp_forecast(
                horizon_start, horizon_end, slot_minutes, n_slots, fallback_sensor)
            schedule_doc['outside_temp_forecast'] = outside_temp

        # Rebuild only this feature's heat-pump entries; keep everything else (battery, EV, grid
        # export, hot water) untouched.
        preheat_names = {d.name for d in preheat_devices}
        new_schedule = [e for e in current_schedule if e.get('device') not in preheat_names]

        def _carry_forward(device_name):
            """Keep a device's previously committed plan when it cannot be re-planned this cycle.

            A transient sensor read failure must not wipe the heat pump's schedule (which would
            cancel its already-scheduled setpoint jobs and leave the zone uncontrolled); the prior
            ranges — still future-valid and carrying their setpoints — are preserved instead.
            """
            new_schedule.extend(e for e in current_schedule if e.get('device') == device_name)

        thermal_summary = {}
        thermal_temp_predictions = {}
        thermal_temp_actual_collected = {}  # device -> (daily backfill series, current measured temp)
        for wp_device in preheat_devices:
            name = wp_device.name

            if not wp_device.inside_temp_sensor:
                logger.warning(f"⚠️ {name}: wp_preheat_enabled but inside_temp_sensor not set — keeping previous plan")
                _carry_forward(name)
                continue

            current_temp = None
            st = await self.get_state(wp_device.inside_temp_sensor)
            if st and st.get('state') not in ('unknown', 'unavailable', None):
                try:
                    current_temp = float(st['state'])
                except (ValueError, TypeError):
                    pass
            if current_temp is None:
                logger.warning(f"⚠️ {name}: room temperature ({wp_device.inside_temp_sensor}) unreadable — keeping previous plan")
                _carry_forward(name)
                continue

            comfort_floor = build_comfort_floor(
                horizon_start, n_slots, slot_minutes,
                [w.model_dump() for w in wp_device.preheat_comfort_windows],
                wp_device.preheat_comfort_default_min_temp,
            )

            # Setpoint currently commanded for the slot in progress — seeds the planner's
            # anti-chatter hysteresis so the live command stays put across 15-min re-solves.
            now_dt = datetime.now()
            previous_setpoint = next(
                (e['setpoint'] for e in current_schedule
                 if e.get('device') == name and e.get('setpoint') is not None
                 and datetime.fromisoformat(e['start']) <= now_dt < datetime.fromisoformat(e['stop'])),
                None,
            )

            plan = plan_thermal_setpoints(
                prices=prices,
                slot_minutes=slot_minutes,
                horizon_start=horizon_start,
                current_temp=current_temp,
                outside_temp=outside_temp,
                comfort_floor=comfort_floor,
                max_setpoint=wp_device.preheat_max_setpoint,
                ua_kw_per_c=wp_device.thermal_heat_loss_kw_per_c,
                heat_capacity_kwh_per_c=wp_device.thermal_heat_capacity_kwh_per_c,
                max_heat_kw=wp_device.thermal_max_heat_kw,
                predicted_solar=predicted_solar,
                predicted_power_usage=predicted_usage,
                cop_ref=wp_device.thermal_cop_ref,
                cop_slope=wp_device.thermal_cop_slope,
                tout_ref=wp_device.thermal_cop_tout_ref,
                preheat_penalty_per_c=wp_device.preheat_penalty_per_c,
                setpoint_resolution=wp_device.preheat_setpoint_resolution,
                previous_setpoint=previous_setpoint,
                device_name=name,
            )

            new_schedule.extend(self._setpoints_to_schedule(
                plan['setpoints'], name, horizon_start, horizon_end, slot_minutes))
            thermal_summary[name] = plan.get('economics', {})
            if plan.get('temps'):
                thermal_temp_predictions[name] = [
                    {'timestamp': (horizon_start + timedelta(minutes=i * slot_minutes)).isoformat(),
                     'temp': t}
                    for i, t in enumerate(plan['temps']) if t is not None
                ]

            # Measured room temperature, for actual-vs-predicted on the UI and for periodic reviews.
            # Backfill the elapsed part of the horizon from HA history every cycle (not just daily)
            # so the measured line fills in promptly — including right after a restart, when the
            # horizon starts near "now" and there is no accumulated log yet. The current reading is
            # also appended below so there is always a fresh point even between sparse sensor logs.
            backfill = []
            try:
                hours_back = min(36.0, max(1.0, (datetime.now() - horizon_start).total_seconds() / 3600 + 0.5))
                hist = await self.ha_client.get_history_series(wp_device.inside_temp_sensor, hours_back=hours_back)
                backfill = [{'timestamp': t.isoformat(), 'temp': round(v, 3)}
                            for t, v in hist if t >= horizon_start]
            except Exception as e:
                logger.warning(f"⚠️ {name}: could not backfill measured temperature history: {e}")
            thermal_temp_actual_collected[name] = (backfill, current_temp)

        schedule_doc['schedule'] = merge_sequential_timeslots([new_schedule])
        schedule_doc['last_thermal_recalc'] = datetime.now().isoformat()
        schedule_doc['thermal_plan_summary'] = {
            'method': 'lp',
            'devices': thermal_summary,
            'evaluated_at': datetime.now().isoformat(),
        }
        with TinyDB('db.json') as db:
            db.upsert(schedule_doc, Query().id == "schedule")

        with TinyDB('db.json') as db:
            existing = db.get(Query().id == 'predictions') or {}
            existing_actual = dict(existing.get('thermal_temp_actual') or {})
            now_iso = datetime.now().isoformat()
            for name, (backfill, cur_temp) in thermal_temp_actual_collected.items():
                # Merge the fresh HA backfill with the accumulated rolling log (the appends below
                # are denser than the sparse sensor history, so keep both), deduped by timestamp.
                merged = {p['timestamp']: p for p in existing_actual.get(name, [])}
                for p in backfill:
                    merged.setdefault(p['timestamp'], p)
                log = sorted(merged.values(), key=lambda p: p['timestamp'])
                # Append the current reading (deduped to ~1 point / 5 min).
                if cur_temp is not None and (
                    not log or datetime.fromisoformat(log[-1]['timestamp']) < datetime.now() - timedelta(minutes=5)
                ):
                    log.append({'timestamp': now_iso, 'temp': round(cur_temp, 3)})
                # Keep only points within the current horizon.
                existing_actual[name] = [
                    p for p in log if datetime.fromisoformat(p['timestamp']) >= horizon_start
                ]
            update = {'id': 'predictions', 'thermal_temp_actual': existing_actual,
                      'updated_at': now_iso}
            if thermal_temp_predictions:
                update['thermal_temp'] = thermal_temp_predictions
            existing.update(update)
            db.upsert(existing, Query().id == 'predictions')

        logger.info(f"✅ Heat-pump setpoint plan updated for {len(thermal_summary)} device(s)")

    def _setpoints_to_schedule(self, setpoints, device_key, horizon_start, horizon_end, slot_minutes):
        """Group a per-slot setpoint array into schedule-doc ranges carrying a ``setpoint`` value.

        Only future slots (end after "now") are emitted; consecutive slots with the same setpoint
        are merged into one range. Each entry is ``{device, start, stop, setpoint}``; the scheduler
        writes the setpoint via the device's ``start`` action with ``{setpoint}`` in the payload.
        """
        if not setpoints:
            return []
        now = datetime.now()
        entries = []
        run_start = None
        run_value = None
        n = len(setpoints)
        for i in range(n):
            v = setpoints[i]
            if run_value is None:
                run_start, run_value = i, v
            elif v != run_value:
                entries.append((run_start, i, run_value))
                run_start, run_value = i, v
        entries.append((run_start, n, run_value))

        out = []
        for start_i, stop_i, value in entries:
            start_dt = horizon_start + timedelta(minutes=start_i * slot_minutes)
            stop_dt = min(horizon_start + timedelta(minutes=stop_i * slot_minutes), horizon_end)
            if stop_dt <= now:
                continue  # wholly in the past
            out.append({
                'device': device_key,
                'start': start_dt.isoformat(),
                'stop': stop_dt.isoformat(),
                'setpoint': value,
            })
        return out

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
        elif status == 'deadline_passed':
            headline = 'Deadline passed'
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
        Returns all slots (past and future); the battery LP keeps the past ones and re-solves
        the future from the current measured SOC.
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