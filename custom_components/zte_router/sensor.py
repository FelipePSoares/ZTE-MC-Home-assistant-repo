import json
import time
import logging
import subprocess
import asyncio
from datetime import datetime, timedelta
from homeassistant.helpers.entity import Entity, EntityCategory
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import PlatformNotReady
from .const import (
    DOMAIN, SENSOR_NAMES, MANUFACTURER, MODEL, UNITS,
    DISABLED_SENSORS_MC889, DISABLED_SENSORS_MC888, DISABLED_SENSORS_MC801A,
    DIAGNOSTICS_SENSORS,FLUX_KEYS, FLUX_ICON_MAP

)

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities):
    ip_entry = entry.data["router_ip"]
    password_entry = entry.data["router_password"]
    username_entry = entry.data.get("router_username") if entry.data.get("router_type") in ["MC888A", "MC889A"] else None
    ping_interval = entry.data.get("ping_interval", 60)
    sms_check_interval = entry.data.get("sms_check_interval", 100)
    router_type = entry.data.get("router_type", "MC801A")
    config = {**entry.data, **entry.options}
    enable_flux = config.get("enable_flux_sensors", True)



    _LOGGER.info(f"Router type selected: {router_type}")
    _LOGGER.info(f"Ping interval: {ping_interval} seconds")
    _LOGGER.info(f"SMS check interval: {sms_check_interval} seconds")

    if router_type == "MC889":
        disabled_sensors = DISABLED_SENSORS_MC889
    elif router_type == "MC888":
        disabled_sensors = DISABLED_SENSORS_MC888
    else:
        disabled_sensors = DISABLED_SENSORS_MC801A

    coordinator = ZTERouterDataUpdateCoordinator(hass, ip_entry, password_entry, username_entry, ping_interval)
    sms_coordinator = ZTERouterSMSUpdateCoordinator(hass, ip_entry, password_entry, username_entry, sms_check_interval)

    await coordinator.async_refresh()
    await sms_coordinator.async_refresh()

    if not coordinator.last_update_success or not sms_coordinator.last_update_success:
        _LOGGER.error("Coordinator(s) failed to refresh successfully.")
        raise PlatformNotReady

    sensors = []
    handled_keys = set()

    # Command 3 (dynamic data)
    dynamic_data = await hass.async_add_executor_job(
        coordinator.run_mc_script, ip_entry, password_entry, username_entry, 3
    )
    _LOGGER.debug(f"Data for command 3: {dynamic_data}")
    try:
        dynamic_data_json = extract_json(dynamic_data)
        dynamic_data = json.loads(dynamic_data_json)
    except json.JSONDecodeError as e:
        _LOGGER.error(f"Failed to parse JSON data for command 3: {e}")
        dynamic_data = {}
    coordinator._data.update(dynamic_data)

    # Command 6 (SMS data)
    additional_data = await hass.async_add_executor_job(
        coordinator.run_mc_script, ip_entry, password_entry, username_entry, 6
    )
    _LOGGER.debug(f"Data for command 6: {additional_data}")
    try:
        additional_data_json = extract_json(additional_data)
        additional_data = json.loads(additional_data_json)
    except json.JSONDecodeError as e:
        _LOGGER.error(f"Failed to parse JSON data for command 6: {e}")
        additional_data = {}
    coordinator._data.update(additional_data)

    # Add Last SMS sensor
    if "content" in additional_data:
        sensors.append(LastSMSSensor(sms_coordinator, additional_data, disabled_sensors.get("last_sms", False)))

    # Core custom sensors
    sensors.append(ConnectedBandsSensor(coordinator, disabled_sensors.get("connected_bands", False)))
    sensors.append(WiFiClientsSensor(coordinator))
    sensors.append(LANClientsSensor(coordinator))
    sensors.append(MonthlyUsageSensor(coordinator))
    sensors.append(monthly_tx_gb(coordinator))
    sensors.append(monthly_rx_gb(coordinator))
    sensors.append(DataLeftSensor(coordinator))
    sensors.append(ConnectionUptimeSensor(coordinator))

    # Command 16 (station list)
    station_list_data = await hass.async_add_executor_job(
        coordinator.run_mc_script, ip_entry, password_entry, username_entry, 16
    )
    _LOGGER.debug(f"Station list raw output: {station_list_data}")
    try:
        station_list_json = extract_json(station_list_data)
        station_list_parsed = json.loads(station_list_json)
    except json.JSONDecodeError as e:
        _LOGGER.error(f"Failed to parse JSON data for command 16 (station_list): {e}")
        station_list_parsed = {}
    coordinator._data.update(station_list_parsed)
    sensors.append(ConnectedDevicesSensor(coordinator, disabled_by_default=False))

    # --- FLUX sensors ---
    if enable_flux:
        for key in FLUX_KEYS:
            if key not in handled_keys:
                if key == "flux_total_usage":
                    _LOGGER.debug(f"[FLUX] Registering FLUX Total Usage Sensor: {key}")
                    sensors.append(ZTEFluxTotalUsageSensor(coordinator))
                else:
                    _LOGGER.debug(f"[FLUX] Registering FLUX sensor: {key}")
                    sensors.append(ZTEFluxSensor(coordinator, key))
                handled_keys.add(key)
    else:
        _LOGGER.info("FLUX sensors are disabled by user config. Skipping creation.")


    # --- Selected other formatted sensors ---
    formatted_keys = {"connection_uptime"}
    if not enable_flux:
        formatted_keys.add("date_month")

    # --- Fallback: all others as basic sensors ---
    _LOGGER.debug(f"Remaining unhandled keys: {[k for k in coordinator.data.keys() if k not in handled_keys]}")
    for key in coordinator.data.keys():
        if key in handled_keys or key in FLUX_KEYS:
            continue
        name = SENSOR_NAMES.get(key, key)
        sensors.append(ZTERouterSensor(coordinator, name, key, disabled_sensors.get(key, False)))
        handled_keys.add(key)


    _LOGGER.info(f"Sensors added: {[sensor.name for sensor in sensors]}")
    _LOGGER.info(f"Diagnostics sensors: {[sensor.name for sensor in sensors if sensor.is_diagnostics]}")

    async_add_entities(sensors, True)

def extract_json(output):
    """Extract the JSON data from the output."""
    try:
        _LOGGER.debug(f"Raw output before JSON extraction: {output}")
        json_data = output[output.index('{'):output.rindex('}')+1]
        return json_data
    except ValueError as e:
        _LOGGER.error(f"Failed to extract JSON: {e}")
        _LOGGER.debug(f"Raw output that caused the error: {output}")
        return "{}"


class ZTERouterDataUpdateCoordinator(DataUpdateCoordinator):
    def __init__(self, hass, ip_entry, password_entry, username_entry, ping_interval):
        self.ip_entry = ip_entry
        self.password_entry = password_entry
        self.username_entry = username_entry if username_entry else ""
        self._data = {}
        _LOGGER.info(f"Initializing DataUpdateCoordinator with ping interval: {ping_interval} seconds")
        super().__init__(
            hass,
            _LOGGER,
            name="zte_router",
            update_interval=timedelta(seconds=ping_interval),  # Use ping_interval
        )

    async def _async_update_data(self):
        _LOGGER.info("Starting _async_update_data in DataUpdateCoordinator at %s", datetime.now())
        try:
            await asyncio.sleep(4)  # Optional initial delay

            # Fetch main data (command 7)
            main_data = await self.hass.async_add_executor_job(
                self.run_mc_script, self.ip_entry, self.password_entry, self.username_entry, 7
            )
            _LOGGER.debug(f"Data received from command 7: {main_data}")
            main_json = extract_json(main_data)
            self._data.update(json.loads(main_json))

            # Wait 3 seconds before next command
            await asyncio.sleep(3)

            # Fetch station and LAN clients (command 16)
            clients_data = await self.hass.async_add_executor_job(
                self.run_mc_script, self.ip_entry, self.password_entry, self.username_entry, 16
            )
            _LOGGER.debug(f"Data received from command 16: {clients_data}")
            clients_json = extract_json(clients_data)
            self._data.update(json.loads(clients_json))

            return self._data

        except Exception as err:
            _LOGGER.error(f"Error during _async_update_data: {err}")
            return self._data


    def run_mc_script(self, ip, password, username, command, retries=3, delay=2):
        attempt = 0
        while attempt < retries:
            _LOGGER.info(f"Running mc.py script for command {command}, attempt {attempt + 1}")
            try:
                # Ensure username is a string; use an empty string if None
                username = username or ""
                cmd = [
                    "python3",
                    "/config/custom_components/zte_router/mc.py",
                    ip,
                    password,
                    str(command),
                    username  # Include username if applicable
                ]
                result = subprocess.run(cmd, capture_output=True, text=True, check=True)
                _LOGGER.debug(f"Output for command {command}: {result.stdout}")
                return result.stdout
            except subprocess.CalledProcessError as e:
                _LOGGER.warning(f"Attempt {attempt + 1} failed with error: {e}. Retrying in {delay} seconds...")
                attempt += 1
                if attempt < retries:
                    time.sleep(delay)
                    delay *= 2  # Double the delay time for the next retry
                else:
                    _LOGGER.error(f"All {retries} attempts failed for command {command}. Raising error.")
                    raise

class ZTERouterSMSUpdateCoordinator(DataUpdateCoordinator):
    def __init__(self, hass, ip_entry, password_entry, username_entry, sms_check_interval):
        self.ip_entry = ip_entry
        self.password_entry = password_entry
        self.username_entry = username_entry if username_entry else ""
        self._data = {}
        _LOGGER.info(f"Initializing SMSUpdateCoordinator with SMS check interval: {sms_check_interval} seconds")
        super().__init__(
            hass,
            _LOGGER,
            name="zte_router_sms",
            update_interval=timedelta(seconds=sms_check_interval),  # Use sms_check_interval
        )

    async def _async_update_data(self):
        _LOGGER.info("Starting _async_update_data in SMSUpdateCoordinator at %s", datetime.now())
        try:
            # Offload the blocking function to a thread
            data = await self.hass.async_add_executor_job(
                self.run_mc_script, self.ip_entry, self.password_entry, self.username_entry, 6
            )
            _LOGGER.debug(f"SMS data received from mc.py script: {data}")
            json_data = extract_json(data)
            self._data.update(json.loads(json_data))
            return self._data
        except Exception as err:
            _LOGGER.error(f"Error during _async_update_data (SMS): {err}")
            return self._data

    def run_mc_script(self, ip, password, username, command):
        _LOGGER.info(f"Running mc.py script for SMS command {command}")
        try:
            # Ensure username is a string; use an empty string if None
            username = username or ""
            cmd = [
                "python3",
                "/config/custom_components/zte_router/mc.py",
                ip,
                password,
                str(command),
                username  # Include username if applicable
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            _LOGGER.debug(f"Output for SMS command {command}: {result.stdout}")
            return result.stdout
        except subprocess.CalledProcessError as e:
            _LOGGER.error(f"Error running SMS command {command}: {e}")
            raise

class ZTERouterEntity(RestoreEntity, Entity):
    """Base class for ZTE Router sensors to ensure consistent MRO."""

    async def async_added_to_hass(self):
        _LOGGER.info(f"Entity {self.name} added to hass at {datetime.now()}")
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is not None:
            self._state = last_state.state
            if hasattr(self, "_attributes"):
                self._attributes.update(last_state.attributes)
            _LOGGER.debug(f"Restored state for {self.name}: {self._state}")
        self.async_on_remove(self.coordinator.async_add_listener(
            lambda: asyncio.ensure_future(self.async_handle_coordinator_update())
        ))
        await self.async_handle_coordinator_update()

class ZTERouterSensor(ZTERouterEntity):
    def __init__(self, coordinator, name, key, disabled_by_default=False):
        self.coordinator = coordinator
        self._name = name
        self._key = key
        self._state = None
        self.entity_registry_enabled_default = not disabled_by_default
        self._attr_is_diagnostics = key in DIAGNOSTICS_SENSORS
        self._attr_should_poll = False  # Disable default polling
        _LOGGER.info(f"Initializing sensor {self._name} with key {self._key}")

    @property
    def name(self):
        return self._name

    @property
    def state(self):
        return self._state

    @property
    def unique_id(self):
        return f"{DOMAIN}_{self.coordinator.ip_entry}_{self._key}"

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, f"{DOMAIN}_{self.coordinator.ip_entry}")},
            "name": self.coordinator.ip_entry,
            "manufacturer": MANUFACTURER,
            "model": MODEL,
            "sw_version": self.coordinator.data.get("wa_inner_version", "Unknown")
        }

    @property
    def available(self):
        """Return True if the entity is available."""
        return True

    @property
    def unit_of_measurement(self):
        return UNITS.get(self._key)

    @property
    def is_diagnostics(self):
        return self._attr_is_diagnostics

    @property
    def entity_category(self):
        if self.is_diagnostics:
            return EntityCategory.DIAGNOSTIC
        return None

    async def async_update(self):
        _LOGGER.info(f"Manual update requested for sensor {self._name} at {datetime.now()}")
        await self.coordinator.async_request_refresh()

    async def async_handle_coordinator_update(self):
        old_state = self._state
        state_changed = False

        if self.coordinator.data:
            new_state = self.coordinator.data.get(self._key, None)

            if isinstance(new_state, str):
                raw_state = new_state  # Preserve the raw incoming string
                display_state = new_state if new_state.strip() else "n/a"

                # Try to convert PCI from hex if applicable and not empty
                if "pci" in self._key.lower() and new_state.strip():
                    try:
                        raw_state = int(new_state, 16)
                        display_state = raw_state
                        _LOGGER.debug(
                            f"Converted hex PCI value to decimal for key '{self._key}': {raw_state}"
                        )
                    except (ValueError, TypeError):
                        _LOGGER.warning(
                            f"Failed to convert value for key '{self._key}' (expected hex string): {new_state}"
                        )
                        raw_state = new_state
                        display_state = new_state if new_state.strip() else "n/a"

                elif "ngbr_cell_info" in self._key.lower():
                    max_length = 255
                    if len(new_state) > max_length:
                        raw_state = new_state[:max_length]
                        display_state = raw_state
                        _LOGGER.debug(
                            f"Truncated 'ngbr_cell_info' to {max_length} characters for key '{self._key}'."
                        )

                # Compare raw values to detect change
                if raw_state != old_state:
                    self._state = raw_state
                    state_changed = True
                    _LOGGER.info(
                        f"Sensor '{self._name}' updated. Old state: {old_state}, New state: {self._state} (Displayed as: {display_state})"
                    )
                else:
                    _LOGGER.debug(
                        f"Sensor '{self._name}' state unchanged."
                    )

            else:
                _LOGGER.debug(
                    f"Invalid value type for key '{self._key}' in coordinator data: {type(new_state)}"
                )
        else:
            _LOGGER.warning(
                f"No coordinator data available for sensor '{self._name}'. Retaining last state."
            )

        if state_changed:
            # Optionally expose display value to HA
            self.async_write_ha_state()


class LastSMSSensor(ZTERouterEntity):
    def __init__(self, coordinator, sms_data, disabled_by_default=False):
        self.coordinator = coordinator
        self._name = "Last SMS"
        self._state = sms_data.get("id", "NO DATA")  # Set the state to the SMS ID
        self._attributes = {k: v for k, v in sms_data.items() if k != "id"}  # Store all other attributes except the ID
        self._attributes["content"] = sms_data.get("content", "NO CONTENT")  # Move content to attributes
        self.entity_registry_enabled_default = not disabled_by_default
        self._attr_should_poll = False  # Disable default polling
        _LOGGER.info(f"Initializing Last SMS sensor with state: {self._state} (SMS ID)")

        # Parse and format the date attribute
        if "date" in self._attributes:
            self._attributes["formatted_date"] = self.format_date(self._attributes["date"])

    @property
    def name(self):
        return self._name

    @property
    def state(self):
        return self._state  # Now returning the SMS ID as the state

    @property
    def unique_id(self):
        return f"{DOMAIN}_{self.coordinator.ip_entry}_last_sms"

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, f"{DOMAIN}_{self.coordinator.ip_entry}")},
            "name": self.coordinator.ip_entry,
            "manufacturer": MANUFACTURER,
            "model": MODEL,
            "sw_version": self.coordinator.data.get("wa_inner_version", "Unknown")
        }

    @property
    def available(self):
        """Return True if the entity is available."""
        return True

    @property
    def extra_state_attributes(self):
        return self._attributes  # Return the content and other attributes

    @property
    def is_diagnostics(self):
        return True  # LastSMS is a diagnostic sensor

    @property
    def entity_category(self):
        return EntityCategory.DIAGNOSTIC

    async def async_update(self):
        _LOGGER.info(f"Manual update requested for Last SMS sensor at {datetime.now()}")
        await self.coordinator.async_request_refresh()

    async def async_handle_coordinator_update(self):
        old_state = self._state
        _LOGGER.debug(f"Updating LastSMS sensor with new data: {self.coordinator.data}")
        data = self.coordinator.data
        if data and "id" in data:  # Make sure ID is in the data
            self._state = data.get("id", "NO DATA")  # Set the state to the SMS ID
            self._attributes = {k: v for k, v in data.items() if k != "id"}
            self._attributes["content"] = data.get("content", "NO CONTENT")  # Move content to attributes
            if "date" in self._attributes:
                self._attributes["formatted_date"] = self.format_date(self._attributes["date"])
            _LOGGER.info(f"Last SMS sensor updated. Old state: {old_state}, New state: {self._state} (SMS ID)")
        else:
            _LOGGER.warning(f"No new SMS data available. Retaining last state: {self._state}")
        self.async_write_ha_state()

    def format_date(self, date_str):
        try:
            # Extract the date parts from the string
            parts = date_str.split(',')
            if len(parts) == 7 and all(parts):
                year = int(parts[0]) + 2000  # Assuming the year is in the format 'YY'
                month = int(parts[1])
                day = int(parts[2])
                hour = int(parts[3])
                minute = int(parts[4])
                second = int(parts[5])
                timezone_offset = parts[6]

                # Create a datetime object
                dt = datetime(year, month, day, hour, minute, second)

                # Format the date to a more readable format
                formatted_date = dt.strftime("%Y-%m-%d %H:%M:%S")

                # Append timezone offset
                formatted_date += f" UTC{timezone_offset}"

                return formatted_date
            else:
                return date_str  # Return the original string if it doesn't match the expected format
        except ValueError as e:
            _LOGGER.error(f"Error parsing date string {date_str}: {e}")
            return date_str
#fixed indent outside of a class
def format_ca_bands(ca_bands, nr5g_action_band):
    _LOGGER.debug(f"Raw ca_bands input: {ca_bands}")
    _LOGGER.debug(f"Raw nr5g_action_band input: {nr5g_action_band}")

    if not ca_bands:
        _LOGGER.debug("No CA bands provided. Returning 'No CA'")
        return "No CA"

    ca_bands_formatted = []

    for band in ca_bands.strip(';').split(';'):
        if not band:
            continue  # Skip empty strings after split

        band_info = band.split(',')
        _LOGGER.debug(f"Parsing CA band string: {band} -> split: {band_info}")

        if len(band_info) >= 6:
            try:
                band_id = band_info[3]
                bandwidth = band_info[5]
                formatted_band = f"B{band_id}@{bandwidth}MHz"
                ca_bands_formatted.append(formatted_band)
                _LOGGER.debug(f"Formatted band: {formatted_band}")
            except Exception as e:
                _LOGGER.warning(f"Failed to format band '{band}' due to: {e}")
        else:
            _LOGGER.warning(f"Band info has insufficient parts: {band_info}")

    if nr5g_action_band:
        ca_bands_formatted.append(nr5g_action_band)
        _LOGGER.debug(f"Appended NR5G action band: {nr5g_action_band}")

    formatted_result = "+".join(ca_bands_formatted)
    _LOGGER.debug(f"Final formatted CA bands string: {formatted_result}")
    return formatted_result

class ConnectedBandsSensor(ZTERouterEntity):
    def __init__(self, coordinator, disabled_by_default=False):
        self.coordinator = coordinator
        self._name = "Connected Bands"
        self._state = None
        self._attributes = {}
        self.entity_registry_enabled_default = not disabled_by_default
        self._attr_is_diagnostics = True  # Ensure ConnectedBands is marked as diagnostics
        self._attr_should_poll = False  # Disable default polling
        _LOGGER.info(f"Initializing Connected Bands sensor")

    @property
    def name(self):
        return self._name

    @property
    def state(self):
        return self._state

    @property
    def unique_id(self):
        return f"{DOMAIN}_{self.coordinator.ip_entry}_connected_bands"

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, f"{DOMAIN}_{self.coordinator.ip_entry}")},
            "name": self.coordinator.ip_entry,
            "manufacturer": MANUFACTURER,
            "model": MODEL,
            "sw_version": self.coordinator.data.get("wa_inner_version", "Unknown")
        }

    @property
    def available(self):
        """Return True if the entity is available."""
        return True

    @property
    def extra_state_attributes(self):
        return self._attributes

    @property
    def is_diagnostics(self):
        return self._attr_is_diagnostics

    @property
    def entity_category(self):
        if self.is_diagnostics:
            return EntityCategory.DIAGNOSTIC
        return None

    async def async_update(self):
        _LOGGER.info(f"Manual update requested for Connected Bands sensor at {datetime.now()}")
        await self.coordinator.async_request_refresh()

    async def async_handle_coordinator_update(self):
        old_state = self._state
        if self.coordinator.data:
            data = self.coordinator.data
            rmcc = data.get("rmcc", "")
            rmnc = data.get("rmnc", "")
            cell_id = data.get("cell_id", "")
            wan_ip = data.get("wan_ipaddr", "")
            main_band = data.get("lte_ca_pcell_band", "")
            main_bandwidth = data.get("lte_ca_pcell_bandwidth", "")
            ca_bands = data.get("lte_multi_ca_scell_info", "")
            ca_bands_formatted = format_ca_bands(ca_bands, data.get("nr5g_action_band", ""))

            # Calculate enbid
            try:
                enb_id = int(cell_id, 16) // 256 if cell_id else ""
            except ValueError:
                _LOGGER.error(f"Invalid cell_id for conversion to int: {cell_id}")
                enb_id = ""

            self._state = f"MAIN:B{main_band}@{main_bandwidth}MHz CA:{ca_bands_formatted}"
            self._attributes = {
                "rmcc": rmcc,
                "rmnc": rmnc,
                "cell_id": cell_id,
                "wan_ip": wan_ip,
                "main_band": main_band,
                "main_bandwidth": main_bandwidth,
                "ca_bands": ca_bands,
                "enb_id": enb_id,
            }
            _LOGGER.info(f"Connected Bands sensor updated. Old state: {old_state}, New state: {self._state}")
        else:
            _LOGGER.warning(f"No data available for Connected Bands sensor. Retaining last state: {self._state}")
        self.async_write_ha_state()

class MonthlyUsageSensor(ZTERouterEntity):
    def __init__(self, coordinator):
        self.coordinator = coordinator
        self._name = "Monthly Usage"
        self._state = None
        self.entity_registry_enabled_default = True  # Set to True, enabled by default
        self._attr_should_poll = False  # Disable default polling
        _LOGGER.info(f"Initializing Monthly Usage sensor")

    @property
    def name(self):
        return self._name

    @property
    def state(self):
        return self._state

    @property
    def unique_id(self):
        return f"{DOMAIN}_{self.coordinator.ip_entry}_monthly_usage"

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, f"{DOMAIN}_{self.coordinator.ip_entry}")},
            "name": self.coordinator.ip_entry,
            "manufacturer": MANUFACTURER,
            "model": MODEL,
            "sw_version": self.coordinator.data.get("wa_inner_version", "Unknown")
        }

    @property
    def available(self):
        """Return True if the entity is available."""
        return True

    @property
    def unit_of_measurement(self):
        return "GB"

    @property
    def is_diagnostics(self):
        return False  # MonthlyUsage is not a diagnostic sensor

    @property
    def entity_category(self):
        return None

    async def async_update(self):
        _LOGGER.info(f"Manual update requested for Monthly Usage sensor at {datetime.now()}")
        await self.coordinator.async_request_refresh()

    async def async_handle_coordinator_update(self):
        old_state = self._state
        if self.coordinator.data:
            data = self.coordinator.data
            monthly_tx_bytes = float(data.get("monthly_tx_bytes", 0) or 0)
            monthly_rx_bytes = float(data.get("monthly_rx_bytes", 0) or 0)
            monthly_usage_gb = (monthly_tx_bytes + monthly_rx_bytes) / 1024 / 1024 / 1024
            self._state = round(monthly_usage_gb, 2)
            _LOGGER.info(f"Monthly Usage sensor updated. Old state: {old_state}, New state: {self._state}")
        else:
            _LOGGER.warning(f"No data available for Monthly Usage sensor. Retaining last state: {self._state}")
        self.async_write_ha_state()

#define GB TX sensor
class monthly_tx_gb(ZTERouterEntity):
    def __init__(self, coordinator):
        self.coordinator = coordinator
        self._name = "Monthly TX GB"
        self._state = None
        self.entity_registry_enabled_default = True  # Set to True, enabled by default
        self._attr_should_poll = False  # Disable default polling
        _LOGGER.info(f"Initializing Monthly TX GB sensor")

    @property
    def name(self):
        return self._name

    @property
    def state(self):
        return self._state

    @property
    def unique_id(self):
        return f"{DOMAIN}_{self.coordinator.ip_entry}_monthly_tx_gb"

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, f"{DOMAIN}_{self.coordinator.ip_entry}")},
            "name": self.coordinator.ip_entry,
            "manufacturer": MANUFACTURER,
            "model": MODEL,
            "sw_version": self.coordinator.data.get("wa_inner_version", "Unknown")
        }

    @property
    def available(self):
        """Return True if the entity is available."""
        return True

    @property
    def unit_of_measurement(self):
        return "GB"

    @property
    def is_diagnostics(self):
        return False  #Monthly GB Sensor is not a diagnostic sensor

    @property
    def entity_category(self):
        return None

    async def async_update(self):
        _LOGGER.info(f"Manual update requested for Monthly TX GB sensor at {datetime.now()}")
        await self.coordinator.async_request_refresh()

    async def async_handle_coordinator_update(self):
        old_state = self._state
        if self.coordinator.data:
            data = self.coordinator.data
            monthly_tx_bytes = float(data.get("monthly_tx_bytes", 0) or 0)
            monthly_tx_gb = monthly_tx_bytes / 1024 / 1024 / 1024
            self._state = round(monthly_tx_gb, 2)
            _LOGGER.info(f"Monthly TX GB sensor updated. Old state: {old_state}, New state: {self._state}")
        else:
            _LOGGER.warning(f"No data available for Monthly TX GB sensor. Retaining last state: {self._state}")
        self.async_write_ha_state()

#define GB RX sensor
class monthly_rx_gb(ZTERouterEntity):
    def __init__(self, coordinator):
        self.coordinator = coordinator
        self._name = "Monthly RX GB"
        self._state = None
        self.entity_registry_enabled_default = True  # Set to True, enabled by default
        self._attr_should_poll = False  # Disable default polling
        _LOGGER.info(f"Initializing Monthly RX GB sensor")

    @property
    def name(self):
        return self._name

    @property
    def state(self):
        return self._state

    @property
    def unique_id(self):
        return f"{DOMAIN}_{self.coordinator.ip_entry}_monthly_rx_gb"

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, f"{DOMAIN}_{self.coordinator.ip_entry}")},
            "name": self.coordinator.ip_entry,
            "manufacturer": MANUFACTURER,
            "model": MODEL,
            "sw_version": self.coordinator.data.get("wa_inner_version", "Unknown")
        }

    @property
    def available(self):
        """Return True if the entity is available."""
        return True

    @property
    def unit_of_measurement(self):
        return "GB"

    @property
    def is_diagnostics(self):
        return False  #Monthly GB Sensor is not a diagnostic sensor

    @property
    def entity_category(self):
        return None

    async def async_update(self):
        _LOGGER.info(f"Manual update requested for Monthly RX GB sensor at {datetime.now()}")
        await self.coordinator.async_request_refresh()

    async def async_handle_coordinator_update(self):
        old_state = self._state
        if self.coordinator.data:
            data = self.coordinator.data
            monthly_rx_bytes = float(data.get("monthly_rx_bytes", 0) or 0)
            monthly_rx_gb = monthly_rx_bytes / 1024 / 1024 / 1024
            self._state = round(monthly_rx_gb, 2)
            _LOGGER.info(f"Monthly RX GB sensor updated. Old state: {old_state}, New state: {self._state}")
        else:
            _LOGGER.warning(f"No data available for Monthly RX GB sensor. Retaining last state: {self._state}")
        self.async_write_ha_state()

#define DataLeftSensor
class DataLeftSensor(ZTERouterEntity):
    def __init__(self, coordinator):
        self.coordinator = coordinator
        self._name = "Data Left"
        self._state = None
        self.entity_registry_enabled_default = True  # Set to True, enabled by default
        self._attr_should_poll = False  # Disable default polling
        _LOGGER.info(f"Initializing Data Left sensor")

    @property
    def name(self):
        return self._name

    @property
    def state(self):
        return self._state

    @property
    def unique_id(self):
        return f"{DOMAIN}_{self.coordinator.ip_entry}_data_left"

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, f"{DOMAIN}_{self.coordinator.ip_entry}")},
            "name": self.coordinator.ip_entry,
            "manufacturer": MANUFACTURER,
            "model": MODEL,
            "sw_version": self.coordinator.data.get("wa_inner_version", "Unknown")
        }

    @property
    def available(self):
        """Return True if the entity is available."""
        return True

    @property
    def unit_of_measurement(self):
        return "GB"

    @property
    def is_diagnostics(self):
        return False  # DataLeft is not a diagnostic sensor

    @property
    def entity_category(self):
        return None

    async def async_update(self):
        _LOGGER.info(f"Manual update requested for Data Left sensor at {datetime.now()}")
        await self.coordinator.async_request_refresh()

    async def async_handle_coordinator_update(self):
        old_state = self._state
        if self.coordinator.data:
            monthly_usage = float(self.hass.states.get("sensor.monthly_usage").state or 0)
            # Use the configurable threshold from the options
            threshold = self.coordinator.config_entry.options.get("monthly_usage_threshold", 200)
            data_left = threshold - monthly_usage if monthly_usage < threshold else 50 - (monthly_usage % 50)
            self._state = round(data_left, 2)
            _LOGGER.info(f"Data Left sensor updated. Old state: {old_state}, New state: {self._state}")
        else:
            _LOGGER.warning(f"No data available for Data Left sensor. Retaining last state: {self._state}")
        self.async_write_ha_state()

class ConnectionUptimeSensor(ZTERouterEntity):
    def __init__(self, coordinator):
        self.coordinator = coordinator
        self._name = "Connection Uptime"
        self._state = None
        self.entity_registry_enabled_default = True  # Set to True, enabled by default
        self._attr_should_poll = False  # Disable default polling
        _LOGGER.info(f"Initializing Connection Uptime sensor")

    @property
    def name(self):
        return self._name

    @property
    def state(self):
        return self._state

    @property
    def unique_id(self):
        return f"{DOMAIN}_{self.coordinator.ip_entry}_connection_uptime"

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, f"{DOMAIN}_{self.coordinator.ip_entry}")},
            "name": self.coordinator.ip_entry,
            "manufacturer": MANUFACTURER,
            "model": MODEL,
            "sw_version": self.coordinator.data.get("wa_inner_version", "Unknown")
        }

    @property
    def available(self):
        """Return True if the entity is available."""
        return True

    @property
    def unit_of_measurement(self):
        return UNITS.get("connection_uptime")

    @property
    def is_diagnostics(self):
        return True  # ConnectionUptime is a diagnostic sensor

    @property
    def entity_category(self):
        return EntityCategory.DIAGNOSTIC

    async def async_update(self):
        _LOGGER.info(f"Manual update requested for Connection Uptime sensor at {datetime.now()}")
        await self.coordinator.async_request_refresh()

    async def async_handle_coordinator_update(self):
        old_state = self._state
        if self.coordinator.data:
            realtime_time = float(self.coordinator.data.get("realtime_time", 0) or 0)
            uptime_hours = realtime_time / 3600
            self._state = round(uptime_hours, 2)
            _LOGGER.info(f"Connection Uptime sensor updated. Old state: {old_state}, New state: {self._state}")
        else:
            _LOGGER.warning(f"No data available for Connection Uptime sensor. Retaining last state: {self._state}")
        self.async_write_ha_state()

class ConnectedDevicesSensor(ZTERouterEntity):
    def __init__(self, coordinator, disabled_by_default=False):
        self.coordinator = coordinator
        self._name = "Connected Devices"
        self._state = None
        self._attributes = {}
        self.entity_registry_enabled_default = not disabled_by_default
        self._attr_should_poll = False
        _LOGGER.info("Initializing Connected Devices sensor")

    @property
    def name(self):
        return self._name

    @property
    def state(self):
        return len(self._attributes.get("station_list", []))

    @property
    def unique_id(self):
        return f"{DOMAIN}_{self.coordinator.ip_entry}_connected_devices"

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, f"{DOMAIN}_{self.coordinator.ip_entry}")},
            "name": self.coordinator.ip_entry,
            "manufacturer": MANUFACTURER,
            "model": MODEL,
            "sw_version": self.coordinator.data.get("wa_inner_version", "Unknown")
        }

    @property
    def available(self):
        return True

    @property
    def extra_state_attributes(self):
        return self._attributes

    @property
    def is_diagnostics(self):
        return False  # ✅ This fixes the crash

    async def async_update(self):
        _LOGGER.info(f"Manual update requested for Connected Devices sensor at {datetime.now()}")
        await self.coordinator.async_request_refresh()

    async def async_handle_coordinator_update(self):
        if self.coordinator.data:
            station_list = self.coordinator.data.get("station_list", [])
            self._attributes["station_list"] = station_list
            _LOGGER.info(f"Connected Devices updated: {len(station_list)} devices")
        else:
            _LOGGER.warning("No data available for Connected Devices")
        self.async_write_ha_state()

class WiFiClientsSensor(ZTERouterEntity):
    def __init__(self, coordinator, disabled_by_default=False):
        self.coordinator = coordinator
        self._name = "WiFi Clients"
        self._state = None
        self._attributes = {}
        self.entity_registry_enabled_default = not disabled_by_default
        self._attr_should_poll = False
        _LOGGER.info("Initializing WiFi Clients sensor")

    @property
    def name(self):
        return self._name

    @property
    def state(self):
        return len(self._attributes.get("wifi_clients", []))

    @property
    def unique_id(self):
        return f"{DOMAIN}_{self.coordinator.ip_entry}_wifi_clients"

    @property
    def extra_state_attributes(self):
        return self._attributes

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, f"{DOMAIN}_{self.coordinator.ip_entry}")},
            "name": self.coordinator.ip_entry,
            "manufacturer": MANUFACTURER,
            "model": MODEL,
            "sw_version": self.coordinator.data.get("wa_inner_version", "Unknown")
        }

    @property
    def is_diagnostics(self):
        return False

    async def async_update(self):
        await self.coordinator.async_request_refresh()

    async def async_handle_coordinator_update(self):
        if self.coordinator.data:
            wifi_clients = self.coordinator.data.get("station_list", [])
            self._state = len(wifi_clients)

            formatted_clients = []
            for client in wifi_clients:
                    formatted_clients.append({
                        "Hostname": client.get("hostname", "--"),
                        "MAC Address": client.get("mac_addr", "--"),
                        "IP Address": client.get("ip_addr", "--"),
                        "Speed": f"{client.get('agreed_rate', '--')} Mbps",
                        "Connected": format_seconds(client.get("connect_time", 0)),
                        "Address Type": client.get("addr_type", "--"),
                        "Type": client.get("type", "--"),
                    })



            self._attributes["wifi_clients"] = formatted_clients
            _LOGGER.info(f"WiFi Clients sensor updated: {self._state} devices")
        else:
            _LOGGER.warning("No data for WiFi Clients")
        self.async_write_ha_state()



class LANClientsSensor(ZTERouterEntity):
    def __init__(self, coordinator, disabled_by_default=False):
        self.coordinator = coordinator
        self._name = "LAN Clients"
        self._state = None
        self._attributes = {}
        self.entity_registry_enabled_default = not disabled_by_default
        self._attr_should_poll = False
        _LOGGER.info("Initializing LAN Clients sensor")

    @property
    def name(self):
        return self._name

    @property
    def state(self):
        return len(self._attributes.get("lan_clients", []))

    @property
    def unique_id(self):
        return f"{DOMAIN}_{self.coordinator.ip_entry}_lan_clients"

    @property
    def extra_state_attributes(self):
        return self._attributes

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, f"{DOMAIN}_{self.coordinator.ip_entry}")},
            "name": self.coordinator.ip_entry,
            "manufacturer": MANUFACTURER,
            "model": MODEL,
            "sw_version": self.coordinator.data.get("wa_inner_version", "Unknown")
        }

    @property
    def is_diagnostics(self):
        return False

    async def async_update(self):
        await self.coordinator.async_request_refresh()

    async def async_handle_coordinator_update(self):
        if self.coordinator.data:
            lan_clients = self.coordinator.data.get("lan_station_list", [])
            self._state = len(lan_clients)

            formatted_clients = []
            for client in lan_clients:
                formatted_clients.append({
                    "Hostname": client.get("hostname", "--"),
                    "MAC Address": client.get("mac_addr", "--"),
                    "IP Address": client.get("ip_addr", "--"),
                    "Speed": f"{client.get('agreed_rate', '--')} Mbps",
                    "Connected": format_seconds(client.get("connect_time", 0)),
                    "Address Type": client.get("addr_type", "--"),
                    "Type": client.get("type", "--"),
                })


            self._attributes["lan_clients"] = formatted_clients
            _LOGGER.info(f"LAN Clients sensor updated: {self._state} devices")
        else:
            _LOGGER.warning("No data for LAN Clients")
        self.async_write_ha_state()

def format_seconds(seconds):
    try:
        seconds = int(seconds)
        hours, remainder = divmod(seconds, 3600)
        minutes, sec = divmod(remainder, 60)

        parts = []
        if hours:
            parts.append(f"{hours}h")
        if minutes:
            parts.append(f"{minutes}m")
        if sec or not parts:
            parts.append(f"{sec}s")

        return " ".join(parts)
    except (TypeError, ValueError):
        return "--"

BYTE_KEYS = {
    "flux_realtime_tx_bytes",
    "flux_realtime_rx_bytes",
    "flux_monthly_tx_bytes",
    "flux_monthly_rx_bytes",
}

THROUGHPUT_KEYS = {
    "flux_realtime_tx_thrpt",
    "flux_realtime_rx_thrpt",
}

class ZTEDataStatisticsSensor(ZTERouterEntity):
    def __init__(self, coordinator, key):
        self.coordinator = coordinator
        self._key = key
        self._name = SENSOR_NAMES.get(key, key)
        self._unit = UNITS.get(key)
        self._state = None
        self.entity_registry_enabled_default = True
        self._attr_should_poll = False
        self._attr_is_diagnostics = key in FLUX_KEYS
        _LOGGER.debug(f"[FLUX] Initialized ZTEDataStatisticsSensor: {self._name} | Diagnostic: {self._attr_is_diagnostics}")

    @property
    def name(self):
        return self._name

    @property
    def state(self):
        raw = self.coordinator.data.get(self._key)
        _LOGGER.debug(f"[FLUX] {self._name}: Raw value = {repr(raw)}")

        if raw in [None, "", "null"]:
            _LOGGER.warning(f"[FLUX] {self._name}: Missing or empty value")
            return "N/A"

        try:
            clean_raw = str(raw).strip()
            value = int(float(clean_raw))
            _LOGGER.debug(f"[FLUX] {self._name}: Parsed value = {value}")

            if self._key in BYTE_KEYS:
                gb_value = value / 1024 / 1024 / 1024
                self._unit = "GB"
                if gb_value >= 1024:
                    self._unit = "TB"
                    result = round(gb_value / 1024, 2)
                else:
                    result = round(gb_value, 2)
                _LOGGER.info(f"[FLUX] {self._name}: {value} bytes => {result} {self._unit}")
                return result

            elif self._key.endswith("_time"):
                formatted = self.format_seconds(value)
                _LOGGER.info(f"[FLUX] {self._name}: {value} seconds => {formatted}")
                return formatted

            elif self._key in THROUGHPUT_KEYS:
                formatted = self.format_throughput(value)
                _LOGGER.info(f"[FLUX] {self._name}: {value} bps => {formatted}")
                return formatted

            elif self._key == "date_month":
                result = f"{clean_raw[:4]}-{clean_raw[4:6]}" if len(clean_raw) == 8 else clean_raw
                _LOGGER.info(f"[FLUX] {self._name}: Parsed date => {result}")
                return result

            _LOGGER.info(f"[FLUX] {self._name}: Raw int value returned = {value}")
            return value

        except (ValueError, TypeError) as e:
            _LOGGER.warning(f"[FLUX] {self._name}: Failed to convert value '{raw}' - {e}")
            return "N/A"

    @property
    def unit_of_measurement(self):
        if self._key in BYTE_KEYS:
            return self._unit
        elif self._key in THROUGHPUT_KEYS:
            return None
        return self._unit

    @property
    def unique_id(self):
        return f"{DOMAIN}_{self.coordinator.ip_entry}_stat_{self._key}"

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, f"{DOMAIN}_{self.coordinator.ip_entry}")},
            "name": self.coordinator.ip_entry,
            "manufacturer": "ZTE",
            "model": "MC Series",
        }

    @property
    def is_diagnostics(self):
        return self._attr_is_diagnostics

    @property
    def entity_category(self):
        return EntityCategory.DIAGNOSTIC if self.is_diagnostics else None

    async def async_update(self):
        _LOGGER.debug(f"[FLUX] Manual update requested for {self._name}")
        await self.coordinator.async_request_refresh()

    async def async_handle_coordinator_update(self):
        _LOGGER.debug(f"[FLUX] Coordinator update triggered for {self._name}")
        self.async_write_ha_state()

    def format_seconds(self, seconds):
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        sec = seconds % 60
        return f"{hours}h {minutes}m {sec}s"

    def format_throughput(self, bps):
        if bps >= 1_000_000:
            return f"{bps / 1_000_000:.2f} Mbps"
        elif bps >= 1_000:
            return f"{bps / 1_000:.2f} Kbps"
        return f"{bps} bps"

class ZTEFluxSensor(ZTEDataStatisticsSensor):
    def __init__(self, coordinator, key):
        super().__init__(coordinator, key)
        self._attr_is_diagnostics = True
        self._attr_should_poll = False
        _LOGGER.debug(f"[FLUX] Initialized ZTEFluxSensor: {self._name}")

    @property
    def icon(self):
        return FLUX_ICON_MAP.get(self._key, "mdi:chart-bar")

    @property
    def entity_category(self):
        return EntityCategory.DIAGNOSTIC

class ZTEFluxTotalUsageSensor(ZTEFluxSensor):
    def __init__(self, coordinator):
        super().__init__(coordinator, "flux_total_usage")
        self._name = "FLUX Monthly Usage"
        self._unit = "GB"
        _LOGGER.debug(f"[FLUX] Initialized ZTEFluxTotalUsageSensor")

    @property
    def state(self):
        try:
            tx_raw = self.coordinator.data.get("flux_monthly_tx_bytes", "0")
            rx_raw = self.coordinator.data.get("flux_monthly_rx_bytes", "0")
            _LOGGER.debug(f"[FLUX] Total TX raw: {tx_raw} | RX raw: {rx_raw}")

            tx = int(float(tx_raw.strip())) if tx_raw else 0
            rx = int(float(rx_raw.strip())) if rx_raw else 0

            total_gb = (tx + rx) / 1024 / 1024 / 1024
            result = round(total_gb, 2)
            _LOGGER.info(f"[FLUX] Total Monthly Usage: TX={tx}, RX={rx}, Total={result} GB")
            return result
        except Exception as e:
            _LOGGER.warning(f"[FLUX] Total Usage: Failed to calculate - {e}")
            return "N/A"

    @property
    def unique_id(self):
        return f"{DOMAIN}_{self.coordinator.ip_entry}_stat_flux_total_usage"
