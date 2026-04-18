import logging
import json
from datetime import datetime
import asyncio
from tinydb import TinyDB, Query
from apscheduler.triggers.date import DateTrigger

logger = logging.getLogger(__name__)


class Scheduler:
    """Manages scheduling of device actions using APScheduler and TinyDB."""

    def __init__(self, scheduler, devices):
        """Initialize the Scheduler.
        
        Args:
            scheduler: APScheduler AsyncIOScheduler instance
            devices: Devices instance for executing actions
        """
        self.scheduler = scheduler
        self.devices = devices

    def remove_device_jobs(self):
        """Remove only device-related scheduled jobs (jobs with '_device_' in their ID).
        This preserves other jobs like scheduled_optimization and scheduled_load_watcher.
        """
        if not self.scheduler:
            return
        
        for job in self.scheduler.get_jobs():
            if '_device_' in job.id:
                self.scheduler.remove_job(job.id)
                logger.debug(f"🗑️ Removed job: {job.id}")

    async def schedule_actions(self):
        """Schedule device actions based on TinyDB schedule using APScheduler."""
        with TinyDB('db.json') as db:
            schedule_doc = db.get(Query().id == "schedule")
        
        if not schedule_doc or "schedule" not in schedule_doc:
            logger.warning("⚠️ No schedule found in TinyDB.")
            return

        # Load per-slot discharge min SOC lookup: device_name -> {ISO_start: min_soc_percent}
        battery_discharge_min_soc = schedule_doc.get('battery_discharge_min_soc', {})

        # Clear only device-related jobs in the scheduler
        self.remove_device_jobs()

        logger.info(f"⚙️ Results-inDB2: {json.dumps(schedule_doc)}")

        now = datetime.now()
        scheduled_count = 0

        for entry in schedule_doc["schedule"]:
            logger.info(f"⚙️ Results-entry: {json.dumps(entry)}")
            device = entry.get("device")
            start_str = entry.get("start")
            stop_str = entry.get("stop")
            if not device or not start_str or not stop_str:
                continue
            try:
                start_time = datetime.fromisoformat(start_str)
                end_time = datetime.fromisoformat(stop_str)
            except Exception as e:
                logger.error(f"❌ Invalid datetime in schedule: {e}")
                continue

            # Handle battery charge/discharge/solar_only entries
            # e.g., "battery_charge", "battery_discharge", "battery_solar_only"
            is_battery_charge = device.endswith('_charge')
            is_battery_discharge = device.endswith('_discharge')
            is_battery_solar_only = device.endswith('_solar_only')
            
            min_soc_percent = None  # Only set for discharge entries that have a min SOC mapping
            
            if is_battery_charge or is_battery_discharge or is_battery_solar_only:
                # Extract the actual device name (e.g., "battery" from "battery_charge")
                base_device_name = device.rsplit('_', 1)[0]
                cfg = self.devices.get_device_config(base_device_name)
                if not cfg:
                    logger.warning(f"⚠️ No config for battery device '{base_device_name}'")
                    continue
                
                # Use battery-specific actions
                if is_battery_charge:
                    start_actions = cfg.charge_start.model_dump(exclude_none=True) if cfg.charge_start else {}
                    stop_actions = cfg.charge_stop.model_dump(exclude_none=True) if cfg.charge_stop else {}
                    action_type = "charge"
                elif is_battery_solar_only:
                    start_actions = cfg.solar_only_start.model_dump(exclude_none=True) if cfg.solar_only_start else {}
                    stop_actions = cfg.solar_only_stop.model_dump(exclude_none=True) if cfg.solar_only_stop else {}
                    action_type = "solar_only"
                else:
                    start_actions = cfg.discharge_start.model_dump(exclude_none=True) if cfg.discharge_start else {}
                    stop_actions = cfg.discharge_stop.model_dump(exclude_none=True) if cfg.discharge_stop else {}
                    action_type = "discharge"
                    # Look up the min SOC for this discharge block (keyed by ISO start time)
                    device_min_soc_map = battery_discharge_min_soc.get(base_device_name, {})
                    min_soc_percent = device_min_soc_map.get(start_str)
            else:
                # Standard device handling (wp, hw, ev)
                cfg = self.devices.get_device_config(device)
                if not cfg:
                    logger.warning(f"⚠️ No config for '{device}'")
                    continue
                start_actions = cfg.start.model_dump(exclude_none=True)
                stop_actions = cfg.stop.model_dump(exclude_none=True)
                action_type = None
                base_device_name = device

            if end_time <= now:
                logger.info(f"⏩ Skipping past event {device} ({start_time})")
                continue

            # Schedule start action if in the future
            if start_time > now and self.scheduler:
                action_label = f"{action_type}_start" if action_type else "start"
                self.scheduler.add_job(
                    self.devices.execute_device_action,
                    trigger=DateTrigger(run_date=start_time),
                    args=[base_device_name, start_actions, action_label, start_time],
                    id=f"{device}_start_device_{start_time.isoformat()}",
                    replace_existing=True
                )
                logger.info(f"📅 Scheduled {device.upper()} START at {start_time.strftime('%Y-%m-%d %H:%M')}")

                # For discharge blocks: also schedule set_min_soc_start when configured
                if is_battery_discharge and min_soc_percent is not None and cfg.set_min_soc_start:
                    set_min_soc_start_actions = cfg.set_min_soc_start.model_dump(exclude_none=True)
                    soc_context = {'min_soc_percent': min_soc_percent}
                    self.scheduler.add_job(
                        self.devices.execute_device_action,
                        trigger=DateTrigger(run_date=start_time),
                        args=[base_device_name, set_min_soc_start_actions, 'set_min_soc_start', start_time, soc_context],
                        id=f"{device}_set_min_soc_start_device_{start_time.isoformat()}",
                        replace_existing=True
                    )
                    logger.info(
                        f"📅 Scheduled {device.upper()} SET_MIN_SOC_START "
                        f"({min_soc_percent:.1f}%) at {start_time.strftime('%Y-%m-%d %H:%M')}"
                    )

            # Schedule stop action if in the future
            if end_time > now and self.scheduler:
                action_label = f"{action_type}_stop" if action_type else "stop"
                self.scheduler.add_job(
                    self.devices.execute_device_action,
                    trigger=DateTrigger(run_date=end_time),
                    args=[base_device_name, stop_actions, action_label, end_time],
                    id=f"{device}_stop_device_{end_time.isoformat()}",
                    replace_existing=True
                )
                scheduled_count += 1
                logger.info(f"📅 Scheduled {device.upper()} STOP at {end_time.strftime('%Y-%m-%d %H:%M')}")

                # For discharge blocks: also schedule set_min_soc_stop when configured
                if is_battery_discharge and cfg.set_min_soc_stop:
                    set_min_soc_stop_actions = cfg.set_min_soc_stop.model_dump(exclude_none=True)
                    self.scheduler.add_job(
                        self.devices.execute_device_action,
                        trigger=DateTrigger(run_date=end_time),
                        args=[base_device_name, set_min_soc_stop_actions, 'set_min_soc_stop', end_time],
                        id=f"{device}_set_min_soc_stop_device_{end_time.isoformat()}",
                        replace_existing=True
                    )
                    logger.info(f"📅 Scheduled {device.upper()} SET_MIN_SOC_STOP at {end_time.strftime('%Y-%m-%d %H:%M')}")

        logger.info(f"✅ Total actions scheduled: {scheduled_count}")
