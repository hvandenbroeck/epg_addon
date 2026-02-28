# EPG Addon Documentation

Welcome to the EPG Addon documentation. The EPG (Energy Price Guidance) Addon optimizes the energy usage of heat pumps, hot water systems, batteries, and EV chargers based on Nordpool electricity prices.

## Getting Started

| Guide | Description |
|-------|-------------|
| [Quick Start](quick_start.md) | Set up your device configuration and get the addon running |

## Configuration

| Guide | Description |
|-------|-------------|
| [Device Configuration](device_configuration.md) | Complete reference for configuring devices (`/data/options.json`) |
| [Configuration Examples](configuration_examples.md) | Real-world device configuration patterns and recipes |

## Features

| Guide | Description |
|-------|-------------|
| [Expressions](expressions.md) | Use mathematical expressions in `value` and `payload` fields |
| [Battery Optimization](battery_optimization.md) | How battery charge/discharge thresholds are calculated from price history |
| [Heat Pump Runtime](heat_pump_runtime.md) | Automatic daily runtime calculation from historical sensor data |
| [Debug Logs](debug_logs.md) | Web-based debug log viewer: filtering, search, and real-time updates |

## Overview

The addon integrates with Home Assistant via its REST API and MQTT. Each day at 16:05 it:
1. Fetches upcoming electricity prices from ENTSO-E
2. Runs optimization algorithms (MILP for thermal devices, percentile-based for batteries)
3. Schedules device start/stop actions through the Home Assistant scheduler

Supported device types:
- **`wp`** – Heat pump
- **`hw`** – Hot water
- **`battery`** – Battery (separate charge / discharge actions)
- **`ev`** – Electric vehicle charger

The web UI is available on port **8099** and shows the scheduled optimization plan and debug logs.
