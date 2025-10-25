#!/usr/bin/env python3

import argparse
import asyncio
import logging
from src.optimizer import HeatpumpOptimizer
from apscheduler.schedulers.asyncio import AsyncIOScheduler
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

    # Run initial optimization
    await optimizer.run_optimization()

    # Test WS API 
    fetcher = HAEnergyDashboardFetcher(args.token)
    await fetcher.fetch_energy_dashboard_config()

    # Schedule daily optimization at 16:05
    async def scheduled_optimization():
        try:
            await optimizer.run_optimization()
        except Exception as e:
            logger.error(f"Error during scheduled optimization: {e}", exc_info=True)

    scheduler.add_job(scheduled_optimization, 'cron', hour=16, minute=5)

    logger.info("Starting optimization APScheduler...")

    # Keep the script running
    try:
        while True:
            await asyncio.sleep(60)
    except Exception as e:
        logger.error(f"Error in main loop: {e}", exc_info=True)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)