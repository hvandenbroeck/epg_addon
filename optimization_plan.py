#!/usr/bin/env python3

import argparse
import asyncio
import logging
from src.optimizer import HeatpumpOptimizer
from src.load_watcher import LoadWatcher
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.events import EVENT_JOB_EXECUTED, EVENT_JOB_ERROR
from src.statistics_loader import StatisticsLoader
from src.weather import Weather
from src.prediction import Prediction
from src.HAConfig import HAEnergyDashboardFetcher
from src.config import CONFIG

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

async def main():
    """Main function to run the optimization."""
    parser = argparse.ArgumentParser(description='Home Assistant Heat Pump Optimizer')
    parser.add_argument('--token', required=True, help='Home Assistant Long-Lived Access Token')
    args = parser.parse_args()


    # --- Run prediction at addon start (inlined) ---
    statistics_loader = StatisticsLoader(args.token)
    weather = Weather(args.token)
    prediction = Prediction(statistics_loader, weather)
    await prediction.calculateTomorrowsPowerUsage()

    # Create APScheduler instance
    scheduler = AsyncIOScheduler()
    scheduler.start()

    # Create optimizer instance with scheduler
    # logger.info(args.HAUrl + "----" + args.token)
    optimizer = HeatpumpOptimizer(args.token, scheduler=scheduler)

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