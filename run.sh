#!/bin/bash
set -e
CONFIG_PATH=/data/options.json
# Read variables from config.json
HA_URL=$(jq -r '.options.ha_url' config.json)
HA_WS_Url=$(jq -r '.options.ha_ws_url' config.json)

# Deploy the Home Assistant package shipped with the add-on (helpers, script, automation and
# sensors behind the EV deadline dashboard) into <HA config>/packages/. The HA config dir is
# mounted at /homeassistant via `map: homeassistant_config:rw` in config.yaml (older
# Supervisors mount `config:rw` at /config, hence the fallback). Only copied when it changed,
# and never fatal: the optimizer must still start if the mount is missing or read-only.
PKG_SRC=/app/homeassistant/packages/ev_deadline.yaml
PKG_NAME=$(basename "$PKG_SRC")
for HA_DIR in /homeassistant /config; do
  if [ -d "$HA_DIR" ] && [ -w "$HA_DIR" ]; then
    if mkdir -p "$HA_DIR/packages" 2>/dev/null; then
      if cmp -s "$PKG_SRC" "$HA_DIR/packages/$PKG_NAME"; then
        echo "HA package $HA_DIR/packages/$PKG_NAME is up to date"
      elif cp "$PKG_SRC" "$HA_DIR/packages/$PKG_NAME" 2>/dev/null; then
        echo "Deployed HA package to $HA_DIR/packages/$PKG_NAME - restart Home Assistant (or reload YAML) to apply"
      else
        echo "WARNING: could not write $HA_DIR/packages/$PKG_NAME"
      fi
    fi
    break
  fi
done
if [ ! -d /homeassistant ] && [ ! -d /config ]; then
  echo "WARNING: Home Assistant config dir not mounted; HA package not deployed (check 'map' in config.yaml)"
fi

# Start Flask server in background
python3 -c "from web.server import run_server; run_server()" &

# Run the Python script with Home Assistant libraries
exec python3 /app/optimization_plan.py --token "${SUPERVISOR_TOKEN}"