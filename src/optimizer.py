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

    def _extract_prices(self, raw_prices, slot_minutes):
        """Extract and normalize price data."""
        if isinstance(raw_prices[0], dict):
            prices = [float(item.get("value")) for item in raw_prices if "value" in item]
        else:
            prices = [float(v) for v in raw_prices]
        if len(prices) == 24:
            prices = [p for p in prices for _ in range(int(60 / slot_minutes))]
        return prices

    async def run_optimization(self):
        """Main optimization logic."""
        NORDPOOL_SENSOR = "sensor.nordpool"
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
        EV_MAX_PRICE = 0.1  # 10 cents per kWh

        logger.info("🔎 Starting energy optimization using Nordpool prices...")

        state = await self.get_state(NORDPOOL_SENSOR)
        if not state or "attributes" not in state:
            logger.error(f"⚠️ No attributes found for {NORDPOOL_SENSOR}")
            return

        attrs = state["attributes"]
        price_sets = {}
        for label, keys in [("today", ["raw_today", "today"]), ("tomorrow", ["raw_tomorrow", "tomorrow"])]:
            raw_prices = None
            for k in keys:
                raw_prices = attrs.get(k)
                if raw_prices:
                    break
            if raw_prices:
                price_sets[label] = self._extract_prices(raw_prices, SLOT_MINUTES)

        if not price_sets:
            logger.error("⚠️ No valid price data for today or tomorrow.")
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
        db = TinyDB('db.json')
        db.upsert({"id": "schedule", "schedule": iso_times_merged}, Query().id == "schedule")
        logger.info(f"✅ Optimization complete. Schedule saved to TinyDB.")
        # Schedule actions from database
        await self.scheduler_instance.schedule_actions()