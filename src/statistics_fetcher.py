import logging
import aiohttp
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


class StatisticsFetcher:
    """Fetches historical statistics from Home Assistant for energy consumption sensors."""

    def __init__(self, ha_url, access_token):
        """Initialize StatisticsFetcher with Home Assistant connection details.
        
        Args:
            ha_url: Home Assistant base URL
            access_token: Home Assistant authentication token
        """
        self.ha_url = ha_url
        self.headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }
        self.sensor_ids = [
            "sensor.energy_consumption_tarif_1",
            "sensor.energy_consumption_tarif_2"
        ]

    async def fetch_hourly_statistics(self, entity_id, start_time, end_time):
        """Fetch hourly statistics from Home Assistant statistics table.
        
        Args:
            entity_id: The sensor entity ID to fetch statistics for
            start_time: Start datetime for the statistics query
            end_time: End datetime for the statistics query
            
        Returns:
            dict: Hourly statistics data
        """
        # Use the statistics API endpoint for long-term statistics
        start_iso = start_time.isoformat()
        end_iso = end_time.isoformat()
        
        # Add filter_entity_id to the query string and remove payload
        url = f"{self.ha_url}/api/history/period/{start_iso}?end_time={end_iso}&filter_entity_id={entity_id}"
        logger.info(f"Fetching hourly statistics for {entity_id} from {start_iso} to {end_iso}")
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=self.headers) as response:
                    if response.status == 200:
                        data = await response.json()
                        logger.debug(f"Successfully fetched statistics for {entity_id}")
                        # The response is a list of lists, one per entity
                        if data and isinstance(data, list) and len(data) > 0:
                            return data[0]  # Return the list for this entity
                        else:
                            logger.warning(f"No data returned for {entity_id}")
                            return []
                    else:
                        logger.error(f"Failed to fetch statistics for {entity_id}: HTTP {response.status}")
                        return []
        except Exception as e:
            logger.error(f"Error fetching statistics for {entity_id}: {str(e)}")
            return []

    async def initialize(self):
        """Initialize the statistics fetcher and fetch last week's data.
        
        This method should be called at application startup.
        """
        logger.info("Initializing StatisticsFetcher...")
        
        # Calculate time range for the last week
        end_time = datetime.now() - timedelta(days=2)
        start_time = datetime.now() - timedelta(days=20)
        
        logger.info("=" * 80)
        logger.info("Fetching energy consumption data (hourly resolution) for the range:")
        logger.info(f"Time range: {start_time.strftime('%Y-%m-%d %H:%M:%S')} to {end_time.strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info("=" * 80)
        
        for sensor_id in self.sensor_ids:
            logger.info(f"\nProcessing sensor: {sensor_id}")
            logger.info("-" * 80)
            
            # Fetch hourly statistics
            stats_data = await self.fetch_hourly_statistics(sensor_id, start_time, end_time)
            
            if stats_data and sensor_id in stats_data:
                statistics = stats_data[sensor_id]
                logger.info(f"Retrieved {len(statistics)} hourly data points for {sensor_id}")
                
                # Log the statistics data
                if len(statistics) > 0:
                    logger.info(f"\nSample of data for {sensor_id}:")
                    # Log first 5 entries as examples
                    for i, stat in enumerate(statistics[:5]):
                        timestamp = stat.get('start', 'N/A')
                        mean = stat.get('mean', 'N/A')
                        state = stat.get('state', 'N/A')
                        sum_val = stat.get('sum', 'N/A')
                        logger.info(f"  [{i+1}] Time: {timestamp}, Mean: {mean}, State: {state}, Sum: {sum_val}")
                    
                    if len(statistics) > 5:
                        logger.info(f"  ... and {len(statistics) - 5} more entries")
                    
                    # Log summary statistics
                    try:
                        total_sum = sum(float(s.get('sum', 0)) for s in statistics if s.get('sum') is not None)
                        avg_mean = sum(float(s.get('mean', 0)) for s in statistics if s.get('mean') is not None) / len(statistics)
                        logger.info(f"\nSummary for {sensor_id}:")
                        logger.info(f"  Total sum: {total_sum:.2f}")
                        logger.info(f"  Average mean: {avg_mean:.2f}")
                    except (ValueError, ZeroDivisionError) as e:
                        logger.warning(f"Could not calculate summary statistics: {str(e)}")
                else:
                    logger.warning(f"No data points found for {sensor_id}")
            else:
                logger.error(f"No statistics data available for {sensor_id}")
            
            logger.info("-" * 80)
        
        logger.info("=" * 80)
        logger.info("Completed fetching energy consumption data")
        logger.info("=" * 80)
        logger.info("StatisticsFetcher initialization complete")
