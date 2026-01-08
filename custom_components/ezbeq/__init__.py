"""The ezbeq Profile Loader integration."""

from __future__ import annotations

import logging

from pyezbeq.ezbeq import EzbeqClient
from pyezbeq.models import SearchRequest  # kept import (may be unused)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import HomeAssistant, ServiceCall  # kept import (may be unused)
from homeassistant.helpers import device_registry as dr
from homeassistant.exceptions import HomeAssistantError  # kept import (may be unused)

from .services import async_setup_services, async_unload_services
from .const import DOMAIN
from .coordinator import EzBEQCoordinator

# Lightweight HTTP proxy to log outbound requests and optionally override gains
from ._http_log_proxy import HttpxLogProxy  # type: ignore[attr-defined]

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]

# Toggle to force all outgoing per-channel gains to a fixed pair.
# Set OVERRIDE_GAINS to True to always send OVERRIDE_GAINS_VALUES (e.g., (0.0, 0.0)).
OVERRIDE_GAINS: bool = True
OVERRIDE_GAINS_VALUES = (0.0, 0.0)

type EzBEQConfigEntry = ConfigEntry[EzBEQCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: EzBEQConfigEntry) -> bool:
    """Set up ezbeq Profile Loader from a config entry."""
    host = entry.data[CONF_HOST]
    port = entry.data[CONF_PORT]

    client = EzbeqClient(host=host, port=port, logger=_LOGGER)

    # Wrap the underlying HTTP client so we log exactly what gets sent to ezBEQ
    # and (optionally) override gains with a fixed pair.
    client.client = HttpxLogProxy(
        client.client,
        _LOGGER,
        override_gains=OVERRIDE_GAINS,
        override_gains_values=OVERRIDE_GAINS_VALUES,
    )

    coordinator = EzBEQCoordinator(hass, client)

    # Hard-disable Main Volume (MV) changes from this integration.
    setattr(coordinator, "disable_mv", True)

    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator

    # create a device for the server
    device_registry = dr.async_get(hass)
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={
            (
                DOMAIN,
                f"{coordinator.config_entry.entry_id}_{DOMAIN}",
            )
        },
        name="EzBEQ",
        manufacturer="EzBEQ",
        sw_version=coordinator.client.version,
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await async_setup_services(hass, coordinator, domain=DOMAIN)
    _LOGGER.debug(
        "Finished setting up ezbeq from a config entry (override_gains=%s, values=%s)",
        OVERRIDE_GAINS,
        OVERRIDE_GAINS_VALUES,
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: EzBEQConfigEntry) -> bool:
    """Unload a config entry."""
    _LOGGER.debug("Unloading ezbeq config entry")
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator = entry.runtime_data
        await coordinator.client.client.aclose()

    await async_unload_services(hass, DOMAIN)
    return unload_ok
