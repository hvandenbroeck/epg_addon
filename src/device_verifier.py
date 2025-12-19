"""
Device State Verifier

This module provides functionality to verify device states and retry actions if needed.
- Periodic verification every 5 minutes to ensure devices are in the correct state
- Post-action verification: 6 checks every 30 seconds for 3 minutes after each start/stop action
  (jobs are dynamically scheduled only when actions are executed)
"""

import logging
import asyncio
import aiohttp
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
from tinydb import TinyDB, Query

from .devices_config import devices_config
from .utils import ensure_list, evaluate_expression
from .config import CONFIG

logger = logging.getLogger(__name__)


class DeviceVerifier:
    """Verifies device states and retries actions if devices are not in the expected state."""

    # Tracking post-action verification
    _pending_verifications: Dict[str, Dict[str, Any]] = {}

    def __init__(self, devices_instance, scheduler=None):
        """Initialize the DeviceVerifier.
        
        Args:
            devices_instance: Devices instance for executing actions
            scheduler: APScheduler instance for scheduling verification jobs
        """
        self.devices = devices_instance
        self.scheduler = scheduler
        self.ha_url = CONFIG['options']['ha_url']
        self.headers = {
            "Authorization": f"Bearer {devices_instance.headers['Authorization'].split(' ')[1]}",
            "Content-Type": "application/json",
        }
        self.devices_config = devices_config
        # Track pending verifications: {device_name: {action_label, end_time, verification_count}}
        self._pending_verifications = {}

    async def get_entity_state(self, entity_id: str) -> Optional[Dict[str, Any]]:
        """Get the state of an entity from Home Assistant.
        
        Args:
            entity_id: The entity ID to get state for
            
        Returns:
            dict: Entity state data or None if failed
        """
        url = f"{self.ha_url}/api/states/{entity_id}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=self.headers) as response:
                    if response.status == 200:
                        return await response.json()
                    logger.warning(f"Failed to get state for {entity_id}: HTTP {response.status}")
                    return None
        except Exception as e:
            logger.error(f"Error getting state for {entity_id}: {e}")
            return None

    async def get_mqtt_value(self, topic_get: str) -> Optional[str]:
        """Get the current value from an MQTT topic via Home Assistant.
        
        For MQTT topics, we need to read from a sensor entity that subscribes to the topic.
        This assumes there's a corresponding sensor entity or we use MQTT subscription.
        
        Args:
            topic_get: The MQTT topic to get value from
            
        Returns:
            str: The current value or None if failed
        """
        # Try to find a sensor entity that corresponds to this MQTT topic
        # Common pattern: sensor.topic_name where topic is transformed
        # For ebusd topics like "ebusd/700/HwcTempDesired/get", sensor might be "sensor.ebusd_700_hwctempdesired"
        
        # Transform topic to potential entity_id
        # Remove /get suffix and transform to entity format
        topic_base = topic_get.replace('/get', '').replace('/set', '')
        entity_id = "sensor." + topic_base.replace('/', '_').lower()
        
        state = await self.get_entity_state(entity_id)
        if state:
            return state.get('state')
        
        # Alternative: try without sensor prefix transformations
        # Some setups might have different naming conventions
        logger.debug(f"Could not find entity for MQTT topic {topic_get}, tried {entity_id}")
        return None

    async def verify_mqtt_action(self, mqtt_config: Dict[str, Any], context: Optional[Dict] = None) -> bool:
        """Verify an MQTT action was successful.
        
        Args:
            mqtt_config: MQTT action configuration with topic, payload, topic_get, payload_check
            context: Optional context for expression evaluation
            
        Returns:
            bool: True if verification passed, False otherwise
        """
        topic = mqtt_config.get("topic", "")
        payload = mqtt_config.get("payload")
        
        # Get the topic to read from (defaults to replacing /set with /get)
        topic_get = mqtt_config.get("topic_get")
        if not topic_get:
            topic_get = topic.replace('/set', '/get')
        
        # Get the expected value (defaults to payload)
        expected_value = mqtt_config.get("payload_check", payload)
        
        # Evaluate expressions if needed
        if context:
            expected_value = str(evaluate_expression(expected_value, context))
        else:
            expected_value = str(expected_value)
        
        # Get current value
        current_value = await self.get_mqtt_value(topic_get)
        
        if current_value is None:
            logger.warning(f"Could not read MQTT value from {topic_get}")
            return False
        
        # Compare values (convert both to string for comparison)
        is_match = str(current_value).strip() == expected_value.strip()
        
        if not is_match:
            logger.warning(f"MQTT verification failed for {topic}: expected '{expected_value}', got '{current_value}'")
        else:
            logger.debug(f"MQTT verification passed for {topic}: '{current_value}'")
        
        return is_match

    async def verify_entity_action(self, entity_config: Dict[str, Any], context: Optional[Dict] = None) -> bool:
        """Verify an entity action was successful.
        
        Args:
            entity_config: Entity action configuration with entity_id, value/option, value_check, state_attribute
            context: Optional context for expression evaluation
            
        Returns:
            bool: True if verification passed, False otherwise
        """
        entity_id = entity_config.get("entity_id")
        if not entity_id:
            logger.warning("No entity_id in entity config, skipping verification")
            return True
        
        # Get the service name to infer expected state for certain services
        service = entity_config.get("service", "")
        
        # Get the expected value
        # Priority: value_check > option > value > inferred from service
        expected_value = entity_config.get("value_check")
        if expected_value is None:
            expected_value = entity_config.get("option")
        if expected_value is None:
            expected_value = entity_config.get("value")
        
        # Infer expected value from service name for switch/input_boolean services
        if expected_value is None:
            if service in ("switch/turn_on", "input_boolean/turn_on", "light/turn_on", "fan/turn_on"):
                expected_value = "on"
            elif service in ("switch/turn_off", "input_boolean/turn_off", "light/turn_off", "fan/turn_off"):
                expected_value = "off"
            elif service == "homeassistant/turn_on":
                expected_value = "on"
            elif service == "homeassistant/turn_off":
                expected_value = "off"
        
        if expected_value is None:
            logger.warning(f"No expected value for entity {entity_id}, skipping verification")
            return True
        
        # Evaluate expressions if needed
        if context:
            expected_value = evaluate_expression(expected_value, context)
        
        # Get current state
        state_data = await self.get_entity_state(entity_id)
        if state_data is None:
            logger.warning(f"Could not read state for entity {entity_id}")
            return False
        
        # Get the value to compare (state or specific attribute)
        state_attribute = entity_config.get("state_attribute")
        if state_attribute:
            current_value = state_data.get("attributes", {}).get(state_attribute)
        else:
            current_value = state_data.get("state")
        
        # Compare values (handle type conversion)
        try:
            # Try numeric comparison first
            if isinstance(expected_value, (int, float)) or (isinstance(expected_value, str) and expected_value.replace('.', '').replace('-', '').isdigit()):
                is_match = float(current_value) == float(expected_value)
            else:
                # String comparison
                is_match = str(current_value).strip().lower() == str(expected_value).strip().lower()
        except (ValueError, TypeError):
            # Fall back to string comparison
            is_match = str(current_value).strip().lower() == str(expected_value).strip().lower()
        
        if not is_match:
            logger.warning(f"Entity verification failed for {entity_id}: expected '{expected_value}', got '{current_value}'")
        else:
            logger.debug(f"Entity verification passed for {entity_id}: '{current_value}'")
        
        return is_match

    async def verify_device_action(self, device: str, action_label: str, context: Optional[Dict] = None) -> bool:
        """Verify all actions for a device are in the expected state.
        
        Args:
            device: Device name (e.g., 'wp', 'hw', 'ev1')
            action_label: Action to verify ('start' or 'stop')
            context: Optional context for expression evaluation
            
        Returns:
            bool: True if all verifications passed, False if any failed
        """
        device_obj = self.devices_config.get_device_by_name(device)
        if not device_obj:
            logger.warning(f"No config found for device '{device}'")
            return True
        
        # Get the action (start or stop)
        action_set = device_obj.start if action_label == "start" else device_obj.stop
        all_passed = True
        
        # Verify MQTT actions
        for mqtt_action in action_set.mqtt:
            mqtt_config = mqtt_action.model_dump(exclude_none=True)
            if not await self.verify_mqtt_action(mqtt_config, context):
                all_passed = False
        
        # Verify entity actions
        for entity_action in action_set.entity:
            entity_config = entity_action.model_dump(exclude_none=True)
            if not await self.verify_entity_action(entity_config, context):
                all_passed = False
        
        return all_passed

    def register_action(self, device: str, action_label: str, context: Optional[Dict] = None):
        """Register a device action for post-action verification.
        
        This schedules verification jobs every 30 seconds for 3 minutes.
        
        Args:
            device: Device identifier
            action_label: Action label ('start' or 'stop')
            context: Optional context for expression evaluation
        """
        if not self.scheduler:
            logger.warning("No scheduler available for post-action verification")
            return
        
        # Remove any existing verification jobs for this device
        self._cancel_verification_jobs(device)
        
        # Schedule 6 verification jobs (every 30 seconds for 3 minutes)
        now = datetime.now()
        job_ids = []
        
        for i in range(6):
            run_time = now + timedelta(seconds=30 * (i + 1))
            job_id = f"verify_{device}_{i}_{now.timestamp()}"
            job_ids.append(job_id)
            
            self.scheduler.add_job(
                self._run_single_verification,
                'date',
                run_date=run_time,
                args=[device, action_label, context, i + 1],
                id=job_id,
                replace_existing=True,
                misfire_grace_time=15
            )
        
        self._pending_verifications[device] = {
            "action_label": action_label,
            "job_ids": job_ids,
            "context": context,
            "end_time": now + timedelta(minutes=3),
            "checks_scheduled": 6
        }
        
        logger.info(f"📋 Scheduled 6 verification checks for {device} {action_label} over next 3 minutes")

    def _cancel_verification_jobs(self, device: str):
        """Cancel any pending verification jobs for a device.
        
        Args:
            device: Device identifier
        """
        if device in self._pending_verifications:
            job_ids = self._pending_verifications[device].get("job_ids", [])
            for job_id in job_ids:
                try:
                    self.scheduler.remove_job(job_id)
                except Exception:
                    pass  # Job may have already run or been removed
            del self._pending_verifications[device]

    async def _run_single_verification(self, device: str, action_label: str, context: Optional[Dict], check_number: int):
        """Run a single verification check for a device.
        
        Args:
            device: Device identifier
            action_label: Action label ('start' or 'stop')
            context: Optional context for expression evaluation
            check_number: Which verification check this is (1-6)
        """
        logger.debug(f"🔍 Running post-action verification #{check_number} for {device} {action_label}")
        
        is_correct = await self.verify_device_action(device, action_label, context)
        
        if not is_correct:
            logger.warning(f"⚠️ Device {device} not in expected {action_label} state, retrying action...")
            device_obj = self.devices_config.get_device_by_name(device)
            if device_obj:
                action_set = device_obj.start if action_label == "start" else device_obj.stop
                action_config = action_set.model_dump(exclude_none=True)
                # Execute without re-registering to avoid infinite loop
                await self.devices.execute_device_action(device, action_config, action_label, context=context, skip_verification=True)
        else:
            logger.info(f"✅ Device {device} verified in correct {action_label} state (check #{check_number}), cancelling remaining checks")
            # Cancel remaining verification jobs for this device
            self._cancel_verification_jobs(device)

    async def run_periodic_verification(self):
        """Run periodic verification for all scheduled devices.
        
        This checks the current schedule and verifies each device is in the correct state.
        Should be called every 5 minutes.
        """
        logger.info("🔍 Running periodic device state verification...")
        
        # Load current schedule from TinyDB
        try:
            with TinyDB('db.json') as db:
                schedule_doc = db.get(Query().id == "schedule")
        except Exception as e:
            logger.error(f"Failed to load schedule from TinyDB: {e}")
            return
        
        if not schedule_doc or "schedule" not in schedule_doc:
            logger.debug("No schedule found, skipping periodic verification")
            return
        
        now = datetime.now()
        
        # Group schedule entries by device and determine expected state
        # A device should be "start" if ANY of its slots is currently active
        device_states: Dict[str, str] = {}  # device -> expected action ("start" or "stop")
        
        for entry in schedule_doc["schedule"]:
            device = entry.get("device")
            start_str = entry.get("start")
            stop_str = entry.get("stop")
            
            if not device or not start_str or not stop_str:
                continue
            
            try:
                start_time = datetime.fromisoformat(start_str)
                end_time = datetime.fromisoformat(stop_str)
            except Exception as e:
                logger.error(f"Invalid datetime in schedule entry: {e}")
                continue
            
            device_obj = self.devices_config.get_device_by_name(device)
            if not device_obj:
                continue
            
            # Check if this slot is currently active
            if start_time <= now < end_time:
                # Device should be running - this takes priority
                device_states[device] = "start"
            elif device not in device_states:
                # Device has a schedule entry but no active slot yet
                # Default to "stop" - device should be off outside scheduled windows
                device_states[device] = "stop"
        
        # Now verify each device's expected state
        for device, expected_action in device_states.items():
            device_obj = self.devices_config.get_device_by_name(device)
            if not device_obj:
                continue
            
            # Verify device state
            is_correct = await self.verify_device_action(device, expected_action)
            
            if not is_correct:
                logger.warning(f"⚠️ Device {device} not in expected {expected_action} state during periodic check, executing action...")
                action_set = device_obj.start if expected_action == "start" else device_obj.stop
                action_config = action_set.model_dump(exclude_none=True)
                await self.devices.execute_device_action(device, action_config, expected_action)
                # Register for post-action verification
                self.register_action(device, expected_action)
            else:
                logger.debug(f"✅ Device {device} verified in correct {expected_action} state")

    def get_verification_status(self) -> Dict[str, Any]:
        """Get the current verification status for all pending verifications.
        
        Returns:
            dict: Dictionary of device -> verification info
        """
        status = {}
        for device, verification in self._pending_verifications.items():
            remaining_jobs = len(verification.get("job_ids", []))
            end_time = verification.get("end_time", datetime.now())
            status[device] = {
                "action": verification["action_label"],
                "remaining_time": max(0, (end_time - datetime.now()).total_seconds()),
                "remaining_checks": remaining_jobs
            }
        return status
