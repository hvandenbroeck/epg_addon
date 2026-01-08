#!/usr/bin/env python3

import argparse
import asyncio
import logging
from logging.handlers import RotatingFileHandler
import os
from src.optimizer import HeatpumpOptimizer
from src.load_watcher import LoadWatcher
from src.device_verifier import DeviceVerifier
from src.devices import Devices
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.events import EVENT_JOB_EXECUTED, EVENT_JOB_ERROR
from src.forecasting import StatisticsLoader, Weather, Prediction, HAEnergyDashboardFetcher, PriceHistoryManager
from src.config import CONFIG

# Configure logging with both file and console output
log_format = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
log_dir = '/data/logs'
os.makedirs(log_dir, exist_ok=True)

# Root logger configuration
root_logger = logging.getLogger()
root_logger.setLevel(logging.DEBUG)

# File handler (rotating, keeps 10 files of 10MB each = 100MB total history)
file_handler = RotatingFileHandler(
    f'{log_dir}/epg_addon.log',
    maxBytes=10*1024*1024,  # 10MB per file
    backupCount=10          # Keep 10 backup files
)
file_handler.setLevel(logging.DEBUG)
file_handler.setFormatter(logging.Formatter(log_format))
root_logger.addHandler(file_handler)

# Console handler (stdout)
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.DEBUG)
console_handler.setFormatter(logging.Formatter(log_format))
root_logger.addHandler(console_handler)

logger = logging.getLogger(__name__)

async def main():
    """Main function to run the optimization."""
    parser = argparse.ArgumentParser(description='Home Assistant Heat Pump Optimizer')
    parser.add_argument('--token', required=True, help='Home Assistant Long-Lived Access Token')
    args = parser.parse_args()


    # --- Run prediction at addon start (inlined) ---
    statistics_loader = StatisticsLoader(args.token)
    weather = Weather(args.token)
    
    # Initialize price history manager if ENTSO-E API is configured
    entsoe_token = CONFIG['options'].get('entsoe_api_token', '')
    entsoe_country = CONFIG['options'].get('entsoe_country_code', 'BE')
    price_history_manager = PriceHistoryManager(entsoe_token, entsoe_country) if entsoe_token else None
    
    prediction = Prediction(statistics_loader, weather, price_history_manager)
    await prediction.calculatePowerUsage()

    # Create APScheduler instance
    scheduler = AsyncIOScheduler()
    scheduler.start()

    # Create optimizer instance with scheduler
    # logger.info(args.HAUrl + "----" + args.token)
    optimizer = HeatpumpOptimizer(args.token, scheduler=scheduler)

    # Create device verifier and link it to devices
    device_verifier = DeviceVerifier(optimizer.devices, scheduler)
    Devices.set_verifier(device_verifier)

    # Create load watcher instance
    load_watcher = LoadWatcher(args.token)

    # Run initial optimization
    await optimizer.run_optimization()

    # Run initial load watcher
    await load_watcher.run()

    # Test WS API 
    fetcher = HAEnergyDashboardFetcher(args.token)
    await fetcher.fetch_energy_dashboard_config()

    # Schedule daily optimization at 16:05
    async def scheduled_optimization():
        logger.info("🕐 Starting scheduled optimization at 16:05")
        try:
            await optimizer.run_optimization()
            logger.info("✅ Scheduled optimization completed successfully")
        except Exception as e:
            logger.error(f"❌ Error during scheduled optimization: {e}", exc_info=True)


    scheduler.add_job(
        scheduled_optimization, 
        'cron', 
        hour=16, 
        minute=5,
        timezone='Europe/Brussels',  # Set your timezone
        coalesce=True,
        max_instances=1,
        misfire_grace_time=300,  # Allow 5 min grace period if system is busy
        id='daily_optimization'
    )
    logger.info("Daily optimization scheduled for 16:05 Europe/Brussels")

    # Schedule battery SOC recalculation every 15 minutes
    async def scheduled_battery_recalc():
        logger.info("🔋 Running scheduled battery SOC recalculation...")
        try:
            await optimizer.recalculate_battery_limits()
            logger.info("✅ Battery SOC recalculation completed successfully")
        except Exception as e:
            logger.error(f"❌ Error during battery SOC recalculation: {e}", exc_info=True)

    scheduler.add_job(
        scheduled_battery_recalc,
        'cron',
        minute='*/15',
        timezone='Europe/Brussels',
        coalesce=True,
        max_instances=1,
        misfire_grace_time=60,
        id='battery_soc_recalc'
    )
    logger.info("Battery SOC recalculation scheduled every 15 minutes (Europe/Brussels)")

    # Schedule load watcher to run every N minutes on the N-minute marks
    load_watcher_interval = CONFIG["options"].get("load_watcher_interval_minutes", 5)

    async def scheduled_load_watcher():
        logger.info("⚡ Running scheduled load watcher...")
        try:
            await load_watcher.run()
        except Exception as e:
            logger.error(f"❌ Error during load watcher: {e}", exc_info=True)

    scheduler.add_job(
        scheduled_load_watcher,
        'cron',
        minute=f'*/{load_watcher_interval}',
        timezone='Europe/Brussels',
        coalesce=True,
        max_instances=1,
        misfire_grace_time=60,
        id='load_watcher'
    )
    logger.info(f"Load watcher scheduled to run every {load_watcher_interval} minutes on the {load_watcher_interval}-minute marks (Europe/Brussels)")

    # Schedule device verification - periodic check every 5 minutes if enabled in config
    if CONFIG["options"].get("periodic_verification_enabled", True):
        async def scheduled_periodic_verification():
            logger.info("🔍 Running scheduled periodic device verification...")
            try:
                await device_verifier.run_periodic_verification()
            except Exception as e:
                logger.error(f"❌ Error during periodic verification: {e}", exc_info=True)

        scheduler.add_job(
            scheduled_periodic_verification,
            'cron',
            minute='*/5',
            timezone='Europe/Brussels',
            coalesce=True,
            max_instances=1,
            misfire_grace_time=60,
            id='periodic_device_verification'
        )
        logger.info("Periodic device verification scheduled to run every 5 minutes (Europe/Brussels)")
    else:
        logger.info("Periodic device verification is DISABLED by config.")
    # Note: Post-action verification jobs are scheduled dynamically by DeviceVerifier
    # when device actions are executed (6 checks over 3 minutes per action)
    
    # Print all scheduled jobs for verification
    logger.info("Currently scheduled jobs:")
    for job in scheduler.get_jobs():
        logger.info(f"  - {job.id}: next run at {job.next_run_time}")

    # Keep the script running
    try:
        while True:
            await asyncio.sleep(60)
    except Exception as e:
        logger.error(f"Error in main loop: {e}", exc_info=True)
    finally:
        # Cleanup on shutdown
        logger.info("Cleaning up resources...")
        load_watcher.close()
        scheduler.shutdown(wait=False)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)