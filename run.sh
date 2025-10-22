#!/bin/bash
set -e
CONFIG_PATH=/data/options.json
# Read variables from config.json
HA_URL=$(jq -r '.options.ha_url' config.json)

# Start Flask server in background
python3 -c "from web.server import run_server; run_server()" &

# Run the Python script with Home Assistant libraries
exec python3 /app/optimization_plan.py --HAUrl "${HA_URL}" --token "${SUPERVISOR_TOKEN}"