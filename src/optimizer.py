import logging
import json
from datetime import datetime, timedelta
import aiohttp
from tinydb import TinyDB, Query
import asyncio

from .devices import Devices
from .scheduler import Scheduler
from .optimization import optimize_wp, optimize_hw, optimize_battery, optimize_bat_discharge, optimize_ev
from .utils import slot_to_time, get_block_len, slots_to_iso_ranges, merge_sequential_timeslots
from .config import CONFIG
from .price_fetcher import EntsoeePriceFetcher

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

    async def run_optimization(self):
        """Main optimization logic."""
        SLOT_MINUTES = 15

        # Configurable blocks
        WP_BLOCK_HOURS = 2
        WP_BLOCKS = 4
        HW_BLOCK_HOURS = 1
        HW_BLOCKS = 2
        HW_MIN_GAP_HOURS = 10
        BAT_CHARGE_SLOTS = 10
        BAT_DISCHARGE_BLOCK_HOURS = 1
        BAT_DISCHARGE_BLOCKS = 10
        EV_MAX_PRICE = 0.10  # 10 cents per kWh

        logger.info("🔎 Starting energy optimization using ENTSO-E prices...")

        # Check if price fetcher is configured
        if not self.price_fetcher:
            logger.error("⚠️ ENTSO-E price fetcher not configured. Please set entsoe_api_token in config.json")
            return

        # Fetch prices from ENTSO-E
        price_sets = self.price_fetcher.get_prices()
        
        if not price_sets:
            logger.error("⚠️ Failed to fetch price data from ENTSO-E")
            return

        results = {}
        schedule_entries = []
        iso_times = []
        for label, prices in price_sets.items():
            logger.info(f"⚙️ Optimizing for {label}...")
            
            wp_times = optimize_wp(prices, SLOT_MINUTES, WP_BLOCK_HOURS, WP_BLOCKS, slot_to_time)
            hw_times = optimize_hw(prices, SLOT_MINUTES, HW_BLOCK_HOURS, HW_BLOCKS, HW_MIN_GAP_HOURS, slot_to_time)
            bat_charge_times = optimize_battery(prices, SLOT_MINUTES, BAT_CHARGE_SLOTS, slot_to_time)
            bat_discharge_times = optimize_bat_discharge(prices, SLOT_MINUTES, BAT_DISCHARGE_BLOCK_HOURS, BAT_DISCHARGE_BLOCKS, slot_to_time)
            ev_times = optimize_ev(prices, SLOT_MINUTES, EV_MAX_PRICE, slot_to_time)
            results[label] = {"wp": wp_times, "hw": hw_times, "bat_charge": bat_charge_times, "bat_discharge": bat_discharge_times, "ev": ev_times}
            logger.info(f"⚙️ Results: {json.dumps(results[label])}")

            # Prepare schedule entries for TinyDB
            target_date = datetime.now().date() if label == "today" else (datetime.now().date() + timedelta(days=1))
            for device, times in results[label].items():
                iso_times.append(slots_to_iso_ranges(times, device, target_date))

        iso_times_merged = []
        iso_times_merged = merge_sequential_timeslots(iso_times)

        # Save schedule to TinyDB
        with TinyDB('db.json') as db:
            db.upsert({"id": "schedule", "schedule": iso_times_merged}, Query().id == "schedule")
        logger.info(f"✅ Optimization complete. Schedule saved to TinyDB.")
        # Schedule actions from database
        await self.scheduler_instance.schedule_actions()