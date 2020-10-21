"""Support for Broadlink covers.

RM device gateway for user defined IR/RF covers.
"""
from datetime import timedelta
import logging

from broadlink.exceptions import BroadlinkException
import voluptuous as vol

from homeassistant.components.cover import (
    ATTR_CURRENT_POSITION,
    ATTR_CURRENT_TILT_POSITION,
    ATTR_POSITION,
    ATTR_TILT_POSITION,
    DEVICE_CLASSES_SCHEMA,
    PLATFORM_SCHEMA,
    STATE_CLOSED,
    STATE_CLOSING,
    STATE_OPENING,
    SUPPORT_CLOSE,
    SUPPORT_CLOSE_TILT,
    SUPPORT_OPEN,
    SUPPORT_OPEN_TILT,
    SUPPORT_SET_POSITION,
    SUPPORT_SET_TILT_POSITION,
    SUPPORT_STOP,
    SUPPORT_STOP_TILT,
    CoverEntity,
)
from homeassistant.const import (
    CONF_COMMAND_CLOSE,
    CONF_COMMAND_OPEN,
    CONF_COMMAND_STOP,
    CONF_COVERS,
    CONF_DEVICE_CLASS,
    CONF_HOST,
    CONF_MAC,
    CONF_NAME,
)
from homeassistant.core import callback
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.event import (
    async_track_point_in_utc_time,
    async_track_time_interval,
)
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util.dt import utcnow

from .const import COVER_DOMAIN, DOMAIN, DOMAINS_AND_TYPES
from .helpers import data_packet, import_device, mac_address

_LOGGER = logging.getLogger(__name__)

CONF_OPENING_TIME = "opening_time"
CONF_CLOSING_TIME = "closing_time"

CONF_TILT_COMMAND_OPEN = "tilt_command_open"
CONF_TILT_COMMAND_CLOSE = "tilt_command_close"
CONF_TILT_COMMAND_STOP = "tilt_command_stop"
CONF_TILT_OPENING_TIME = "tilt_opening_time"
CONF_TILT_CLOSING_TIME = "tilt_closing_time"

COVER_DEBUG = True
TRAVEL_TIME_MAX = 300
POSITION_MIN = 0
POSITION_MAX = 100

# Cover status
COVER_CLOSED = 1
COVER_CLOSING = 2
COVER_OPENED = 3
COVER_OPENING = 4

TRAVEL_TIME = vol.All(vol.Coerce(float), vol.Range(min=0, max=TRAVEL_TIME_MAX))

COVER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_NAME): cv.string,
        vol.Optional(CONF_DEVICE_CLASS): DEVICE_CLASSES_SCHEMA,
        vol.Optional(CONF_COMMAND_OPEN): data_packet,
        vol.Optional(CONF_COMMAND_CLOSE): data_packet,
        vol.Optional(CONF_COMMAND_STOP): data_packet,
        vol.Optional(CONF_OPENING_TIME, default=0.0): TRAVEL_TIME,
        vol.Optional(CONF_CLOSING_TIME, default=0.0): TRAVEL_TIME,
        vol.Optional(CONF_TILT_COMMAND_OPEN): data_packet,
        vol.Optional(CONF_TILT_COMMAND_CLOSE): data_packet,
        vol.Optional(CONF_TILT_COMMAND_STOP): data_packet,
        vol.Optional(CONF_TILT_OPENING_TIME, default=0.0): TRAVEL_TIME,
        vol.Optional(CONF_TILT_CLOSING_TIME, default=0.0): TRAVEL_TIME,
    }
)

PLATFORM_SCHEMA = vol.All(
    PLATFORM_SCHEMA.extend(
        {
            vol.Required(CONF_MAC): mac_address,
            vol.Optional(CONF_COVERS, default=[]): vol.All(
                cv.ensure_list, [COVER_SCHEMA]
            ),
        }
    ),
)


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    """Import the device and set up custom covers.

    This is for backward compatibility.
    Do not use this method.
    """
    mac_addr = config[CONF_MAC]
    host = config.get(CONF_HOST)
    covers = config.get(CONF_COVERS)

    if covers:
        platform_data = hass.data[DOMAIN].platforms.setdefault(COVER_DOMAIN, {})
        platform_data.setdefault(mac_addr, []).extend(covers)

    else:
        _LOGGER.warning(
            "The cover platform is deprecated, except for custom IR/RF "
            "covers. Please refer to the Broadlink documentation to "
            "catch up"
        )

    if host:
        import_device(hass, host)


async def async_setup_entry(hass, config_entry, async_add_entities):
    """Set up the Broadlink cover."""
    device = hass.data[DOMAIN].devices[config_entry.entry_id]

    for domain, types in DOMAINS_AND_TYPES:
        if domain == COVER_DOMAIN:
            device_types = types

    if device.api.type in device_types:
        platform_data = hass.data[DOMAIN].platforms.get(COVER_DOMAIN, {})
        user_defined_cover = platform_data.get(device.api.mac, {})
        covers = [BroadlinkCover(device, config) for config in user_defined_cover]

    else:
        covers = []

    async_add_entities(covers)


class BroadlinkCover(CoverEntity, RestoreEntity):
    """Representation of an Broadlink cover."""

    def __init__(self, device, config):
        """Initialize the cover."""
        self._device = device
        self._device_class = None
        self._name = config.get(CONF_NAME)
        self._device_class = config.get(CONF_DEVICE_CLASS)

        command_open = config.get(CONF_COMMAND_OPEN)
        command_close = config.get(CONF_COMMAND_CLOSE)
        command_stop = config.get(CONF_COMMAND_STOP)
        opening_time = config.get(CONF_OPENING_TIME)
        closing_time = config.get(CONF_CLOSING_TIME)

        tilt_command_open = config.get(CONF_TILT_COMMAND_OPEN)
        tilt_command_close = config.get(CONF_TILT_COMMAND_CLOSE)
        tilt_command_stop = config.get(CONF_TILT_COMMAND_STOP)
        tilt_opening_time = config.get(CONF_TILT_OPENING_TIME)
        tilt_closing_time = config.get(CONF_TILT_CLOSING_TIME)

        self._supported_features = 0
        if command_open:
            self._supported_features |= SUPPORT_OPEN
        if command_close:
            self._supported_features |= SUPPORT_CLOSE
        if command_stop:
            self._supported_features |= SUPPORT_STOP
        if (
            command_open
            and command_close
            and command_stop
            and (opening_time > 0)
            and (closing_time > 0)
        ):
            self._supported_features |= SUPPORT_SET_POSITION

        if self._supported_features | (SUPPORT_OPEN & SUPPORT_CLOSE & SUPPORT_STOP):
            self._main_enable = True
        else:
            self._main_enable = False

        if tilt_command_open:
            self._supported_features |= SUPPORT_OPEN_TILT
        if tilt_command_close:
            self._supported_features |= SUPPORT_CLOSE_TILT
        if tilt_command_stop:
            self._supported_features |= SUPPORT_STOP_TILT
        if (
            tilt_command_open
            and tilt_command_close
            and tilt_command_stop
            and (tilt_opening_time > 0)
            and (tilt_closing_time > 0)
        ):
            self._supported_features |= SUPPORT_SET_TILT_POSITION

        if self._supported_features | (
            SUPPORT_OPEN_TILT & SUPPORT_CLOSE_TILT & SUPPORT_STOP_TILT
        ):
            self._tilt_enable = True
        else:
            self._tilt_enable = False

        self._main = _subCover(
            self,
            self._device,
            self._name,
            command_open,
            command_close,
            command_stop,
            opening_time,
            closing_time,
            False,
        )

        self._tilt = _subCover(
            self,
            self._device,
            self._name,
            tilt_command_open,
            tilt_command_close,
            tilt_command_stop,
            tilt_opening_time,
            tilt_closing_time,
            True,
        )

        self._coordinator = device.update_manager.coordinator

        _LOGGER.debug("Init done %s", self._name)

    @property
    def unique_id(self):
        """Return the cover unique id."""
        return f"{self._device.unique_id}-{self._name}"

    @property
    def name(self):
        """Return the name of the cover."""
        return self._name

    @property
    def assumed_state(self):
        """Return True if unable to access real state of the cover."""
        return True

    @property
    def available(self):
        """Return True if the cover is available."""
        return self._device.update_manager.available

    @property
    def supported_features(self):
        """Flag of supported features."""
        return self._supported_features

    @property
    def should_poll(self):
        """No polling needed."""
        return False

    @property
    def device_class(self):
        """Return device class."""
        return self._device_class

    @property
    def device_info(self):
        """Return device info."""
        return {
            "identifiers": {(DOMAIN, self._device.unique_id)},
            "manufacturer": self._device.api.manufacturer,
            "model": self._device.api.model,
            "name": self._device.name,
            "sw_version": self._device.fw_version,
        }

    def _restore_state(self, state):
        """Return the state of the cover."""
        self._is_opening = False
        self._is_closing = False
        self._is_closed = False

        if state == STATE_OPENING:
            self._is_opening = True
            return

        if state == STATE_CLOSING:
            self._is_closing = True
            return

        if state == STATE_CLOSED:
            self._is_closed = True

    @callback
    def update_data(self):
        """Update data."""
        self.async_write_ha_state()

    async def async_added_to_hass(self):
        """Call when the cover is added to hass."""
        saved_state = await self.async_get_last_state()
        if saved_state:
            self._restore_state(saved_state.state)

            if self.current_cover_position is None:
                self._main.restore_position(
                    saved_state.attributes.get(ATTR_CURRENT_POSITION)
                )

            if self.current_cover_tilt_position is None:
                self._tilt.restore_position(
                    saved_state.attributes.get(ATTR_CURRENT_TILT_POSITION)
                )

        self.async_on_remove(self._coordinator.async_add_listener(self.update_data))

    async def async_update(self):
        """Update the cover."""
        await self._coordinator.async_request_refresh()

    @property
    def current_cover_position(self):
        """Return the current position of the cover."""
        if self._supported_features & SUPPORT_SET_POSITION:
            return self._main.current_cover_position
        return None

    @property
    def current_cover_tilt_position(self):
        """Return the current tilt position of the cover."""
        if self._supported_features & SUPPORT_SET_TILT_POSITION:
            return self._tilt.current_cover_position
        return None

    @property
    def is_closed(self):
        """Return if the cover is closed."""
        return self._main.is_closed

    @property
    def is_closing(self):
        """Return if the cover is closing."""
        return self._main.is_closing

    @property
    def is_opening(self):
        """Return if the cover is opening."""
        return self._main.is_opening

    async def async_close_cover(self, **kwargs):
        """Close the cover."""
        if self._supported_features & SUPPORT_CLOSE:
            await self._main.async_close_cover(**kwargs)

    async def async_close_cover_tilt(self, **kwargs):
        """Close the cover tilt."""
        if self._supported_features & SUPPORT_CLOSE_TILT:
            await self._tilt.async_close_cover(**kwargs)

    async def async_open_cover(self, **kwargs):
        """Open the cover."""
        if self._supported_features & SUPPORT_OPEN:
            await self._main.async_open_cover(**kwargs)

    async def async_open_cover_tilt(self, **kwargs):
        """Open the cover tilt."""
        if self._supported_features & SUPPORT_OPEN_TILT:
            await self._tilt.async_open_cover(**kwargs)

    async def async_stop_cover(self, **kwargs):
        """Stop the cover on command."""
        if self._supported_features & SUPPORT_STOP:
            await self._main.async_stop_cover(**kwargs)

    async def async_stop_cover_tilt(self, **kwargs):
        """Stop the cover tilt."""
        if self._supported_features & SUPPORT_STOP_TILT:
            await self._tilt.async_stop_cover(**kwargs)

    async def async_set_cover_position(self, **kwargs):
        """Move the cover to a specific position."""
        if self._supported_features & SUPPORT_SET_POSITION:
            await self._main.async_set_cover_position(**kwargs)

    async def async_set_cover_tilt_position(self, **kwargs):
        """Move the cover til to a specific position."""
        if self._supported_features & SUPPORT_SET_TILT_POSITION:
            await self._tilt.async_set_cover_position(**kwargs)


class _subCover:
    """Half Broadlink cover - main or tilt."""

    def __init__(
        self,
        parent,
        device,
        name,
        command_open,
        command_close,
        command_stop,
        opening_time,
        closing_time,
        is_tilt,
    ):
        """Initialize the cover."""
        self._parent = parent
        self._device = device
        self._name = name + (" (tilt)" if is_tilt else "")
        self._is_tilt = is_tilt

        self._command_open = command_open
        self._command_close = command_close
        self._command_stop = command_stop
        self._opening_time = opening_time
        self._closing_time = closing_time

        self._status = None
        self._position = None
        self._position_set = self._position
        self._position_start = self._position

        self._unsub_tracking_interval_listener = None
        self._unsub_travel_duration_listener = None

        self._travel_time_start = None
        self._travel_time_stop = None

        if self._opening_time > 0:
            self._opening_speed = float(POSITION_MAX) / opening_time
        else:
            self._opening_speed = float(POSITION_MAX)

        if self._closing_time > 0:
            self._closing_speed = float(-POSITION_MAX) / closing_time
        else:
            self._closing_speed = float(-POSITION_MAX)

        self._speed = None

    @property
    def current_cover_position(self):
        """Return the current position of the cover."""
        return self._position

    @property
    def is_closed(self):
        """Return if the cover is closed."""
        return self._status == COVER_CLOSED

    @property
    def is_closing(self):
        """Return if the cover is closing."""
        return self._status == COVER_CLOSING

    @property
    def is_opening(self):
        """Return if the cover is opening."""
        return self._status == COVER_OPENING

    async def async_close_cover(self, **kwargs):
        """Close the cover on command."""
        _LOGGER.debug("%s is closing", self._name)

        if await self._async_send_packet(self._command_close):
            self._status = COVER_CLOSING
            self._position_set = POSITION_MIN
            self._listeners_start()
            self._parent.async_write_ha_state()

    async def async_open_cover(self, **kwargs):
        """Open the cover on command."""
        _LOGGER.debug("%s is opening", self._name)

        if await self._async_send_packet(self._command_open):
            self._status = COVER_OPENING
            self._position_set = POSITION_MAX
            self._listeners_start()
            self._parent.async_write_ha_state()

    async def async_stop_cover(self, **kwargs):
        """Stop the cover on command."""
        if await self._async_send_packet(self._command_stop):
            if self._listeners_stop():
                self._update_position(utcnow())

            self._status = (
                COVER_CLOSED if self._position == POSITION_MIN else COVER_OPENED
            )
            self._position_set = self._position
            self._position_start = None

            self._parent.async_write_ha_state()
            _LOGGER.debug("%s stopped at position ", self._name, self._position)

    async def async_set_cover_position(self, **kwargs):
        """Move the cover to a specific position."""
        position = (
            kwargs.get(ATTR_TILT_POSITION)
            if self._is_tilt
            else kwargs.get(ATTR_POSITION)
        )
        position = max(POSITION_MIN, min(round(position, 0), POSITION_MAX))

        if position == POSITION_MIN:
            return self.async_close_cover()

        if position == POSITION_MAX:
            return self.async_open_cover()

        if self._position in [None, position]:
            return self._parent.async_write_ha_state()

        _LOGGER.debug("%s setting position to: %i", self._name, position)

        self._position_set = position
        if self._position_set < self._position:
            command = self._command_close
            self._status = COVER_CLOSING
        else:
            command = self._command_open
            self._status = COVER_OPENING

        if await self._async_send_packet(command):
            self._listeners_start()
            self._parent.async_write_ha_state()

    def restore_position(self, position):
        """Restore cover position."""
        self.position = position

    def _listeners_start(self):
        """Start timer listeners."""
        now = utcnow()
        if self._listeners_stop():
            self._update_position(now)

        self._speed = (
            self._closing_speed
            if self._status == COVER_CLOSING
            else self._opening_speed
        )

        if self._position is None:
            # For unknown position use the full travel time from configuration
            self._position_start = (
                POSITION_MAX if self._status == COVER_CLOSING else POSITION_MIN
            )
            travel_duration = (
                self._closing_time
                if self._status == COVER_CLOSING
                else self._opening_time
            )
        else:
            self._position_start = self._position
            travel_duration = abs((self._position_set - self._position) / self._speed)

        self._travel_time_start = now
        self._travel_time_stop = self._travel_time_start + timedelta(
            seconds=travel_duration
        )

        # Start travel duration timer
        self._unsub_travel_duration_listener = async_track_point_in_utc_time(
            self._parent.hass, self._async_track_travel_duration, self._travel_time_stop
        )

        # Interval update of cover position
        if travel_duration > 2.0:
            self._unsub_tracking_interval_listener = async_track_time_interval(
                self._parent.hass,
                self._async_track_cover_position,
                timedelta(seconds=1.0),
            )

        _LOGGER.debug(
            "%s is moving from : %i, to: %i, in: %.3f",
            self._name,
            self._position_start,
            self._position_set,
            travel_duration,
        )

    def _listeners_stop(self):
        """Stop timer listeners."""
        if self._unsub_tracking_interval_listener is not None:
            self._unsub_tracking_interval_listener()
            self._unsub_tracking_interval_listener = None

            if self._unsub_travel_duration_listener is not None:
                self._unsub_travel_duration_listener()
                self._unsub_travel_duration_listener = None

            return True

        return False

    async def _async_track_travel_duration(self, now):
        """Stop cover travel after duration."""
        if self._position_set not in [POSITION_MIN, POSITION_MAX]:
            await self._async_send_packet(self._command_stop)

        self._listeners_stop()
        self._update_position(now)

        _LOGGER.debug(
            "%s travel ended after : %.3f",
            self._name,
            (now - self._travel_time_start).total_seconds(),
        )

        if self._position_set == POSITION_MIN:
            self._position = POSITION_MIN
        elif self._position_set == POSITION_MAX:
            self._position = POSITION_MAX

        self._position_start = None

        self._status = COVER_CLOSED if self._position == POSITION_MIN else COVER_OPENED

        self._parent.async_write_ha_state()

    async def _async_track_cover_position(self, now):
        """Track cover position over time."""
        self._update_position(now)
        self._parent.async_write_ha_state()

    def _update_position(self, now):
        """Compute actual position based on travelling time."""
        if self._position_start is None or self._travel_time_start is None:
            return

        travel_time = now - self._travel_time_start
        travel_pos = round(travel_time.total_seconds() * self._speed)
        position = self._position_start + travel_pos
        self._position = max(POSITION_MIN, min(position, POSITION_MAX))
        self._closed = self._position <= POSITION_MIN

        _LOGGER.debug("%s position is: %i ", self._name, self._position)

    async def _async_send_packet(self, packet):
        """Send a packet to the device."""
        if packet is None:
            return True

        if COVER_DEBUG:
            return True

        try:
            await self._device.async_request(self._device.api.send_data, packet)
        except (BroadlinkException, OSError) as err:
            _LOGGER.error("Failed to send packet: %s", err)
            return False
        return True
