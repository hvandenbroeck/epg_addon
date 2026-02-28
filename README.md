# EPG Addon

A Home Assistant addon that optimizes the energy usage of heat pumps, hot water systems, batteries, and EV chargers based on Nordpool electricity prices.

## Features

- **Price-based scheduling** – Fetches upcoming electricity prices from ENTSO-E and schedules device start/stop actions in the cheapest available slots.
- **Battery arbitrage** – Charges batteries during low-price slots and discharges during high-price slots using historical percentile thresholds.
- **Heat pump runtime** – Calculates expected daily runtime from sensor history and adjusts the schedule accordingly.
- **Load management** – Dynamically limits device power draw to avoid grid overload.
- **Expressions** – Use mathematical expressions (e.g. watt-to-amp conversion) directly in device configuration values.
- **Web UI** – Visualizes the optimization schedule and provides a filterable debug log viewer on port **8099**.

## Documentation

See the **[documentation/](documentation/index.md)** folder for all guides:

| Guide | Description |
|-------|-------------|
| [Quick Start](documentation/quick_start.md) | Set up your configuration and get started |
| [Device Configuration](documentation/device_configuration.md) | Full reference for `/data/options.json` |
| [Configuration Examples](documentation/configuration_examples.md) | Real-world configuration patterns |
| [Expressions](documentation/expressions.md) | Mathematical expressions in `value`/`payload` fields |
| [Battery Optimization](documentation/battery_optimization.md) | Percentile-based charge/discharge strategy |
| [Heat Pump Runtime](documentation/heat_pump_runtime.md) | Sensor-based daily runtime calculation |
| [Debug Logs](documentation/debug_logs.md) | Web-based log viewer |

## Quick Setup

1. Copy `DEVICES_CONFIG_EXAMPLE.json` as a reference and create `/data/options.json` with your device configuration.
2. Set your Home Assistant URL, access token, and ENTSO-E API token in `config.json`.
3. Build and start the container (see `Dockerfile` and `run.sh`).
4. The addon runs the optimization daily at **16:05** and exposes the web UI on port **8099**.
