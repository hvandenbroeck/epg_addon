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

            # Handle battery typed entries: suffix identifies action type and config attributes
            # Order matters — longer suffixes must be checked before shorter overlapping ones
            BATTERY_SUFFIXES = {
                '_block_grid_export': ('block_grid_export', 'block_grid_export_start', 'block_grid_export_stop'),
                '_solar_only':        ('solar_only',        'solar_only_start',        'solar_only_stop'),
                '_discharge':         ('discharge',         'discharge_start',         'discharge_stop'),
                '_charge':            ('charge',            'charge_start',            'charge_stop'),
            }
            matched_suffix = next((s for s in BATTERY_SUFFIXES if device.endswith(s)), None)

            if matched_suffix:
                action_type, start_attr, stop_attr = BATTERY_SUFFIXES[matched_suffix]
                base_device_name = device[:-len(matched_suffix)]
                cfg = self.devices.get_device_config(base_device_name)
                if not cfg:
                    logger.warning(f"⚠️ No config for battery device '{base_device_name}'")
                    continue
                start_actions = getattr(cfg, start_attr, None)
                start_actions = start_actions.model_dump(exclude_none=True) if start_actions else {}
                stop_actions = getattr(cfg, stop_attr, None)
                stop_actions = stop_actions.model_dump(exclude_none=True) if stop_actions else {}
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

        logger.info(f"✅ Total actions scheduled: {scheduled_count}")
