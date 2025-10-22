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

    async def schedule_actions(self):
        """Schedule device actions based on TinyDB schedule using APScheduler."""
        db = TinyDB('db.json')
        schedule_doc = db.get(Query().id == "schedule")
        if not schedule_doc or "schedule" not in schedule_doc:
            logger.warning("⚠️ No schedule found in TinyDB.")
            return

        # Clear all jobs in the scheduler
        if self.scheduler:
            self.scheduler.remove_all_jobs()

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

            cfg = self.devices.get_device_config(device)
            if not cfg:
                logger.warning(f"⚠️ No config for '{device}'")
                continue

            if end_time <= now:
                logger.info(f"⏩ Skipping past event {device} ({start_time})")
                continue

            # Schedule start action if in the future
            if start_time > now and self.scheduler:
                self.scheduler.add_job(
                    self.devices.execute_device_action,
                    trigger=DateTrigger(run_date=start_time),
                    args=[device, cfg.get("start", {}), "start", start_time],
                    id=f"{device}_start_{start_time.isoformat()}",
                    replace_existing=True
                )
                logger.info(f"📅 Scheduled {device.upper()} START at {start_time.strftime('%Y-%m-%d %H:%M')}")

            # Schedule stop action if in the future
            if end_time > now and self.scheduler:
                self.scheduler.add_job(
                    self.devices.execute_device_action,
                    trigger=DateTrigger(run_date=end_time),
                    args=[device, cfg.get("stop", {}), "stop", end_time],
                    id=f"{device}_stop_{end_time.isoformat()}",
                    replace_existing=True
                )
                scheduled_count += 1
                logger.info(f"📅 Scheduled {device.upper()} STOP at {end_time.strftime('%Y-%m-%d %H:%M')}")

        logger.info(f"✅ Total actions scheduled: {scheduled_count}")
