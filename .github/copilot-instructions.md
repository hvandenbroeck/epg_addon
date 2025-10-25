# Copilot Instructions for EPG Addon

## Project Overview
This project is a Home Assistant addon for optimizing energy usage of heat pumps, hot water, and batteries. It integrates with Home Assistant via its API and MQTT, schedules device actions based on Nordpool electricity prices, and exposes a web UI for visualization.

## Architecture
- **Main Entrypoint:** `optimization_plan.py` orchestrates the optimization, scheduling, and Home Assistant API integration.
- **Core Logic:**
  - `src/optimizer.py`: Main optimization logic, Home Assistant API/service calls, and device scheduling.
  - `src/optimization.py`: Mathematical optimization routines (using PuLP/GLPK).
  - `src/devices.py`: Device action abstraction (MQTT/entity service calls).
  - `src/config.py`: Device action configuration (topics, entities, payloads).
  - `src/utils.py`: Helper functions for time and slot calculations.
- **Web UI:**
  - `web/server.py`: Flask server exposing `/` (UI) and `/api/results` (TinyDB-backed schedule results).
  - `web/templates/index.html`: Timeline visualization using ApexCharts.
- **Data Storage:**
  - TinyDB (`db.json`) for storing and serving optimization results.

## Developer Workflows
- **Build & Run:**
  - Use the provided `Dockerfile` and `run.sh` for containerized execution.
  - Entrypoint: `run.sh` (starts Flask server and optimization script).
  - Home Assistant URL and token are read from `config.json` (see `run.sh`).
- **Testing:**
  - No tests are present by default (`tests/` is empty). Add tests as needed.
- **Web UI:**
  - Access via port 8099. Results are fetched from `/api/results`.

## Project Conventions
- **Device Actions:**
  - All device actions (MQTT/entity) are defined in `src/config.py` and referenced by device type and action label.
  - Use `Devices.execute_device_action()` for all device control logic.
- **Scheduling:**
  - APScheduler is used for periodic optimization (see `optimization_plan.py`).
  - Optimization runs daily at 16:05 by default.
- **Optimization:**
  - All optimization routines use PuLP with GLPK solver.
  - Slot size is 15 minutes by default.
- **API Integration:**
  - Home Assistant API calls use bearer token from config.
  - MQTT actions are proxied via Home Assistant's `mqtt/publish` service.

## Key Files & Directories
- `optimization_plan.py`: Entrypoint, scheduler, argument parsing
- `src/`: Core logic (optimizer, devices, optimization, config, utils)
- `web/`: Flask server and UI
- `config.json`: Home Assistant connection config
- `run.sh`: Entrypoint script

## External Dependencies
- Python: `homeassistant`, `requests`, `apscheduler`, `pulp`, `flask`, `flask-cors`, `tinydb`
- System: `jq`, `glpk-utils` (for optimization)

## Example: Adding a New Device Action
1. Add action config to `src/config.py` under the appropriate device.
2. Reference the new action in `Devices.execute_device_action()` logic if needed.
3. Ensure the optimizer schedules the new action as required.

---
For further details, see the referenced files. If any section is unclear or incomplete, please provide feedback for improvement.
