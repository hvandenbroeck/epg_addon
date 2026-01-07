# Architecture Refactoring Proposal

This document proposes a better structure for the EPG Addon application, specifically addressing the complexity in `optimizer.py` and `optimization.py`.

## Current Issues

### 1. `optimizer.py` (~600 lines) - Too Many Responsibilities

The `HeatpumpOptimizer` class currently handles:

| Responsibility | Lines | Description |
|---------------|-------|-------------|
| HA API Integration | ~50 | `get_state()`, `call_service()` |
| Device State Persistence | ~100 | `_get_device_state()`, `_save_device_state()`, TinyDB operations |
| Gap/Lock Calculations | ~70 | `_calculate_initial_gap()`, `_get_locked_slots()` |
| Main Orchestration | ~200 | `run_optimization()` - loops through devices, calls optimizers |
| Battery SOC Recalculation | ~100 | `recalculate_battery_limits()` and helpers |
| Predicted Usage | ~30 | `_get_predicted_usage()` |

**Problems:**
- Violates Single Responsibility Principle
- Hard to test individual components
- Difficult to understand the flow
- Mixing infrastructure (TinyDB, HA API) with domain logic

### 2. `optimization.py` (~500 lines) - Better, But Could Be Modular

The optimization module has clearer responsibility but bundles different optimization algorithms:

| Algorithm | Lines | Purpose |
|-----------|-------|---------|
| `optimize_thermal_device()` | ~100 | MILP optimization for WP/HW |
| `optimize_wp()` / `optimize_hw()` | ~40 | Thin wrappers for thermal devices |
| `optimize_battery()` | ~70 | Price-based battery charging |
| `optimize_bat_discharge()` | ~70 | Price-based battery discharging |
| `optimize_ev()` | ~5 | Simple threshold-based EV charging |
| `limit_battery_cycles()` | ~160 | SOC-aware cycle limiting |

**Problems:**
- All algorithms in one file make it harder to navigate
- Different algorithms have different complexity levels
- Battery logic is split between this file and optimizer.py

---

## Proposed Structure

### New Directory Layout

```
src/
├── __init__.py
├── config.py                      # Global config (unchanged)
├── devices.py                     # Device action execution (unchanged)
├── devices_config.py              # Device configuration models (unchanged)
├── scheduler.py                   # APScheduler integration (unchanged)
├── utils.py                       # Helper functions (unchanged)
├── price_fetcher.py               # ENTSO-E price fetching (unchanged)
│
├── ha_client.py                   # NEW: Home Assistant API client
├── device_state_manager.py        # NEW: Device state persistence (TinyDB)
│
├── optimization/                  # NEW: Optimization module directory
│   ├── __init__.py               # Re-exports for convenience
│   ├── thermal.py                # Thermal device (WP/HW) optimization
│   ├── battery.py                # Battery charge/discharge optimization
│   ├── ev.py                     # EV charging optimization
│   └── battery_limiter.py        # SOC-aware battery cycle limiting
│
└── optimizer.py                   # REFACTORED: Orchestrator only
```

---

## Detailed Refactoring Plan

### Phase 1: Extract Home Assistant Client

Create `src/ha_client.py`:

```python
"""Home Assistant API Client.

Provides a clean interface for Home Assistant API interactions.
"""
import logging
import aiohttp
from .config import CONFIG

logger = logging.getLogger(__name__)


class HomeAssistantClient:
    """Async client for Home Assistant REST API."""

    def __init__(self, access_token: str):
        self.ha_url = CONFIG['options']['ha_url']
        self.headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

    async def get_state(self, entity_id: str) -> dict | None:
        """Get the state of an entity from Home Assistant.
        
        Args:
            entity_id: The entity ID to query
            
        Returns:
            Entity state dict or None if not found
        """
        url = f"{self.ha_url}/api/states/{entity_id}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=self.headers) as response:
                if response.status == 200:
                    return await response.json()
                return None

    async def call_service(self, service: str, **service_data) -> bool:
        """Call a Home Assistant service.
        
        Args:
            service: Service in format 'domain/service_name'
            **service_data: Service parameters
            
        Returns:
            True if service call was successful
        """
        domain, service_name = service.split('/')
        url = f"{self.ha_url}/api/services/{domain}/{service_name}"
        
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=self.headers, json=service_data) as response:
                return response.status == 200
```

**Benefits:**
- Single place for HA API interactions
- Can be easily mocked in tests
- Can add retry logic, connection pooling, etc.

---

### Phase 2: Extract Device State Manager

Create `src/device_state_manager.py`:

```python
"""Device State Manager.

Manages device state persistence using TinyDB for tracking:
- Last run times (for gap calculations)
- Locked/scheduled starts (for rescheduling protection)
"""
import logging
from datetime import datetime, timedelta
from tinydb import TinyDB, Query

logger = logging.getLogger(__name__)


class DeviceStateManager:
    """Manages device state persistence in TinyDB."""

    def __init__(self, db_path: str = 'db.json'):
        self.db_path = db_path

    def get_device_state(self, device: str) -> dict:
        """Get the last run state for a device.
        
        Returns:
            dict with 'last_run_end' (datetime or None) and 'locked_starts' (list of datetimes)
        """
        with TinyDB(self.db_path) as db:
            state_doc = db.get(Query().id == f"{device}_state")
        
        if not state_doc:
            return {'last_run_end': None, 'locked_starts': []}
        
        last_run_end = None
        if state_doc.get('last_run_end'):
            try:
                last_run_end = datetime.fromisoformat(state_doc['last_run_end'])
            except:
                pass
        
        locked_starts = []
        for start_str in state_doc.get('locked_starts', []):
            try:
                locked_starts.append(datetime.fromisoformat(start_str))
            except:
                pass
        
        return {'last_run_end': last_run_end, 'locked_starts': locked_starts}

    def save_device_state(self, device: str, last_run_end: datetime, scheduled_starts: list):
        """Save device state for the next optimization run."""
        with TinyDB(self.db_path) as db:
            state = {
                'id': f"{device}_state",
                'last_run_end': last_run_end.isoformat() if last_run_end else None,
                'locked_starts': [s.isoformat() for s in scheduled_starts],
                'updated_at': datetime.now().isoformat()
            }
            db.upsert(state, Query().id == f"{device}_state")
        logger.debug(f"💾 Saved {device} state: last_run_end={last_run_end}, locked_starts={len(scheduled_starts)}")

    def calculate_initial_gap(self, device: str, horizon_start: datetime, 
                               slot_minutes: int, block_hours: float) -> int:
        """Calculate how many slots since the device last ran.
        
        Returns:
            Number of slots since last run ended (0 if currently running or just ended)
        """
        state = self.get_device_state(device)
        last_run_end = state['last_run_end']
        
        if last_run_end is None:
            logger.info(f"📊 {device}: No previous run recorded, using default initial gap")
            return int(4 * 60 / slot_minutes)  # Assume 4 hours gap
        
        if last_run_end >= horizon_start:
            logger.info(f"📊 {device}: Last run ends at {last_run_end}, horizon starts at {horizon_start}")
            return 0
        
        gap_minutes = (horizon_start - last_run_end).total_seconds() / 60
        gap_slots = int(gap_minutes / slot_minutes)
        logger.info(f"📊 {device}: Last run ended {gap_minutes:.0f} min ago ({gap_slots} slots)")
        return gap_slots

    def get_locked_slots(self, device: str, horizon_start: datetime, 
                          lock_end_datetime: datetime, slot_minutes: int) -> set:
        """Get slot indices that are locked (already scheduled and shouldn't be changed)."""
        state = self.get_device_state(device)
        locked_starts = state['locked_starts']
        
        locked_slots = set()
        for start_dt in locked_starts:
            if start_dt >= horizon_start and start_dt < lock_end_datetime:
                slot_idx = int((start_dt - horizon_start).total_seconds() / 60 / slot_minutes)
                if slot_idx >= 0:
                    locked_slots.add(slot_idx)
                    logger.debug(f"🔒 {device}: Locked slot {slot_idx} (start at {start_dt})")
        
        return locked_slots
```

**Benefits:**
- All state persistence logic in one place
- Clear interface for state operations
- Easy to swap TinyDB for another storage backend
- Testable in isolation

---

### Phase 3: Split Optimization Algorithms

Create `src/optimization/` directory:

#### `src/optimization/__init__.py`
```python
"""Optimization algorithms for energy devices."""

from .thermal import optimize_thermal_device, optimize_wp, optimize_hw
from .battery import optimize_battery, optimize_bat_discharge
from .ev import optimize_ev
from .battery_limiter import limit_battery_cycles

__all__ = [
    'optimize_thermal_device',
    'optimize_wp', 
    'optimize_hw',
    'optimize_battery',
    'optimize_bat_discharge',
    'optimize_ev',
    'limit_battery_cycles',
]
```

#### `src/optimization/thermal.py` (~150 lines)
Contains `optimize_thermal_device()`, `optimize_wp()`, `optimize_hw()` - the MILP optimization for heat pumps and hot water.

#### `src/optimization/battery.py` (~150 lines)
Contains `optimize_battery()` and `optimize_bat_discharge()` - price-based battery optimization.

#### `src/optimization/ev.py` (~20 lines)
Contains `optimize_ev()` - simple threshold-based EV charging.

#### `src/optimization/battery_limiter.py` (~200 lines)
Contains `limit_battery_cycles()` - SOC-aware cycle limiting that simulates battery state over time.

**Benefits:**
- Each file has a clear, focused purpose
- Easier to find and modify specific algorithms
- Better for collaborative development
- Algorithms can evolve independently

---

### Phase 4: Simplify Optimizer (Orchestrator)

The refactored `optimizer.py` becomes a pure orchestrator:

```python
"""Energy Optimization Orchestrator.

Coordinates the optimization workflow:
1. Fetch prices
2. Get device states
3. Run optimization algorithms
4. Save results and schedule actions
"""
import logging
import json
from datetime import datetime, timedelta
from tinydb import TinyDB, Query

from .ha_client import HomeAssistantClient
from .device_state_manager import DeviceStateManager
from .devices import Devices
from .scheduler import Scheduler
from .optimization import (
    optimize_wp, optimize_hw, 
    optimize_battery, optimize_bat_discharge,
    optimize_ev, limit_battery_cycles
)
from .utils import slot_to_time, slots_to_iso_ranges, merge_sequential_timeslots, time_to_slot
from .config import CONFIG
from .price_fetcher import EntsoeePriceFetcher
from .devices_config import devices_config
from .forecasting.price_history import PriceHistoryManager
from .forecasting.statistics_loader import StatisticsLoader
from .forecasting.weather import Weather
from .forecasting.prediction import Prediction

logger = logging.getLogger(__name__)


class HeatpumpOptimizer:
    """Orchestrates energy optimization for heat pumps, batteries, and EVs."""

    def __init__(self, access_token: str, scheduler=None):
        self.ha_client = HomeAssistantClient(access_token)
        self.state_manager = DeviceStateManager()
        self.devices = Devices(access_token)
        self.scheduler_instance = Scheduler(scheduler, self.devices)
        
        # Initialize price fetching
        entsoe_token = CONFIG['options'].get('entsoe_api_token', '')
        entsoe_country = CONFIG['options'].get('entsoe_country_code', 'BE')
        self.price_fetcher = EntsoeePriceFetcher(entsoe_token, entsoe_country) if entsoe_token else None
        self.price_history_manager = PriceHistoryManager(entsoe_token, entsoe_country) if entsoe_token else None

    async def run_optimization(self):
        """Main optimization workflow."""
        # 1. Fetch prices
        horizon_data = self._fetch_prices()
        if not horizon_data:
            return
        
        # 2. Calculate price thresholds
        thresholds = await self._calculate_price_thresholds()
        
        # 3. Optimize each device type
        results = {}
        results.update(self._optimize_thermal_devices(horizon_data, 'wp'))
        results.update(self._optimize_thermal_devices(horizon_data, 'hw'))
        results.update(await self._optimize_batteries(horizon_data, thresholds))
        results.update(self._optimize_evs(horizon_data))
        
        # 4. Save schedule and schedule actions
        self._save_schedule(results, horizon_data)
        await self.scheduler_instance.schedule_actions()

    # Private methods handle specific sub-tasks...
```

**Benefits:**
- `run_optimization()` is now a clear, high-level workflow
- Each step is delegated to specialized components
- Easy to understand the overall flow
- Components can be tested independently

---

## Summary: Before vs After

### Before
```
optimizer.py (600 lines)
├── HA API calls
├── TinyDB state management  
├── Gap/lock calculations
├── Optimization orchestration
├── Battery SOC recalculation
└── Prediction helpers

optimization.py (500 lines)
├── Thermal device optimization
├── Battery charge optimization
├── Battery discharge optimization
├── EV optimization
└── Battery cycle limiting
```

### After
```
ha_client.py (~50 lines)
└── HomeAssistantClient

device_state_manager.py (~100 lines)
└── DeviceStateManager

optimization/
├── thermal.py (~150 lines)
├── battery.py (~150 lines)
├── ev.py (~20 lines)
└── battery_limiter.py (~200 lines)

optimizer.py (~250 lines)
└── HeatpumpOptimizer (orchestrator only)
```

---

## Implementation Priority

1. **Phase 1: Extract HA Client** - Low risk, immediate testability improvement
2. **Phase 2: Extract Device State Manager** - Medium effort, good separation
3. **Phase 3: Split Optimization** - Mostly moving code, low risk
4. **Phase 4: Simplify Optimizer** - Depends on phases 1-3

Each phase can be done independently, allowing incremental improvement.

---

## Testing Strategy

With this refactoring, testing becomes much easier:

```python
# Test HA Client
def test_ha_client_get_state():
    client = HomeAssistantClient("test_token")
    # Mock aiohttp and test

# Test State Manager
def test_device_state_persistence():
    manager = DeviceStateManager("test_db.json")
    manager.save_device_state("wp", datetime.now(), [])
    state = manager.get_device_state("wp")
    assert state['last_run_end'] is not None

# Test Optimization Algorithms
def test_thermal_optimization():
    prices = [0.10, 0.15, 0.12, 0.08, 0.20]
    result = optimize_thermal_device(
        prices=prices,
        slot_minutes=60,
        # ... other params
    )
    assert len(result) > 0
```

---

## Conclusion

This refactoring proposal addresses the main pain points:

1. **optimizer.py is too large** → Split into focused modules
2. **Hard to understand** → Clear orchestration pattern
3. **Difficult to test** → Isolated components with clean interfaces
4. **Mixing concerns** → Infrastructure separate from domain logic

The proposed structure follows established patterns (Clean Architecture, Single Responsibility) and makes the codebase more maintainable for future development.
