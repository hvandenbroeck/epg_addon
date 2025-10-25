import aiohttp
import pandas as pd
from datetime import datetime, timedelta, timezone
from .HAConfig import HAEnergyDashboardFetcher
from .config import CONFIG

class StatisticsLoader:
    """
    Loads statistics from Home Assistant API and returns a pandas DataFrame.
    """
    def __init__(self, access_token):
        self.access_token = access_token
        self.headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

    async def fetch_statistics(self):
        """
        Fetches statistics for the past 14 days and returns a pandas DataFrame.
        """

        # First, fetch energy dashboard config to identify relevant entities
        fetcher = HAEnergyDashboardFetcher(self.access_token)
        energy_dashboard_config = await fetcher.fetch_energy_dashboard_config()

        # Extract entities from energy dashboard config
        entities = self.extract_energy_entities_for_usage(energy_dashboard_config)

        # Build list of statistic IDs to fetch
        statistic_ids = []
        for sensors in entities.values():
            statistic_ids.extend(sensors)
        

        end_dt = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        start_dt = end_dt - timedelta(days=365)
        payload = {
            "start_time": start_dt.isoformat(),
            "end_time": end_dt.isoformat(),
            "statistic_ids": statistic_ids,
            "period": "hour",
            "types": ["mean", "min", "max", "sum", "state"]
        }
        
        url = f"{CONFIG['options']['ha_url']}/api/services/recorder/get_statistics?return_response=1"
        
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=self.headers, json=payload) as response:
                if response.status != 200:
                    raise Exception(f"Failed to fetch statistics: {response.status}")
                data = await response.json()
        stats = data.get("service_response", {}).get("statistics", {})

        # Prepare mapping for all entities and their relevant value type
        # Use 'state' for energy sensors, 'mean' for temperature sensors, fallback to 'state' if unknown
        entity_value_map = {}
        for entity in statistic_ids:
            if "temperature" in entity:
                value_type = "mean"
            else:
                value_type = "state"
            entity_value_map[entity] = value_type

        # Extract mapping for each entity
        entity_maps = {}
        for entity, value_type in entity_value_map.items():
            entity_maps[entity] = {entry["start"]: entry.get(value_type) for entry in stats.get(entity, []) if value_type in entry}

        # Union of all start times
        all_times = set()
        for m in entity_maps.values():
            all_times |= set(m)
        all_times = sorted(all_times)

        # Build DataFrame columns for each entity
        data = {"timestamp": pd.to_datetime(all_times)}
        for entity, m in entity_maps.items():
            data[entity] = [m.get(t) for t in all_times]

        df = pd.DataFrame(data)
        df = df.reset_index(drop=True)

        # Calculate hourly energy usage using entities dict
        # energy_used_per_hour = grid_import + battery_discharge + solar_production - grid_export - battery_charge
        cols_plus = [col for col in [
            *(entities.get("grid_import", [])),
            *(entities.get("battery_discharge", [])),
            *(entities.get("solar_production", []))
        ] if col in df.columns]
        cols_minus = [col for col in [
            *(entities.get("grid_export", [])),
            *(entities.get("battery_charge", []))
        ] if col in df.columns]

        # Sum positive contributors
        df["energy_used_per_hour"] = 0.0
        for col in cols_plus:
            df["energy_used_per_hour"] += df[col].diff()
        # Subtract negative contributors
        for col in cols_minus:
            df["energy_used_per_hour"] -= df[col].diff()

        return df

    @staticmethod
    def extract_energy_entities_for_usage(energy_config):
        """
        Extracts entities needed to calculate true total energy used by home.
        Returns a dict of relevant sensors grouped by their role.
        """
        result = energy_config.get("result", {})
        entities = {
            "grid_import": [],
            "solar_production": [],
            "battery_discharge": [],
            "grid_export": [],
            "battery_charge": [],
        }

        for source in result.get("energy_sources", []):
            source_type = source.get("type")
            if source_type == "grid":
                for flow in source.get("flow_from", []):
                    sensor = flow.get("stat_energy_from")
                    if sensor:
                        entities["grid_import"].append(sensor)
                for flow in source.get("flow_to", []):
                    sensor = flow.get("stat_energy_to")
                    if sensor:
                        entities["grid_export"].append(sensor)
            elif source_type == "solar":
                sensor = source.get("stat_energy_from")
                if sensor:
                    entities["solar_production"].append(sensor)
            elif source_type == "battery":
                sensor_discharge = source.get("stat_energy_from")
                sensor_charge = source.get("stat_energy_to")
                if sensor_discharge:
                    entities["battery_discharge"].append(sensor_discharge)
                if sensor_charge:
                    entities["battery_charge"].append(sensor_charge)
        return entities   