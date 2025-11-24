import asyncio
import websockets
import json
import logging
from urllib.parse import urlparse
from ..config import CONFIG

class HAEnergyDashboardFetcher:
    def __init__(self, access_token):
        self.ws_url = CONFIG['options']['ha_ws_url']
        self.access_token = access_token


    async def fetch_energy_dashboard_config(self):
        async with websockets.connect(self.ws_url) as ws:
            # Wait for 'auth_required'
            msg = await ws.recv()
            logging.info(f"Received: {msg}")

            # Send authentication
            await ws.send(json.dumps({
                "type": "auth",
                "access_token": self.access_token
            }))
            msg = await ws.recv()
            logging.info(f"Received: {msg}")

            # Send the energy/get_prefs command
            await ws.send(json.dumps({
                "id": 1,
                "type": "energy/get_prefs"
            }))
            msg = await ws.recv()
            logging.info(f"Energy Dashboard Config: {msg}")

        return json.loads(msg)
