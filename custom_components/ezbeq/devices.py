# devices.py
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Callable
from datetime import timedelta

from homeassistant.core import HomeAssistant, ServiceCall, CALLBACK_TYPE
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval

from .coordinator import EzBEQCoordinator

_LOGGER = logging.getLogger(__name__)

DEVICES_SENSOR_ID = "sensor.ezbeq_devices"
DEVICES_FRIENDLY_NAME = "ezBEQ Devices"
DEFAULT_REFRESH_INTERVAL_SECS = 120  # configurable when you call async_setup_devices


# ---------- small utilities ----------
def _utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _safe_base_url(hass: HomeAssistant, domain: str) -> Optional[str]:
    """Resolve base_url strictly from hass.data set in __init__.py."""
    base_url = (hass.data.get(domain) or {}).get("base_url")
    if not base_url:
        return None
    return str(base_url).rstrip("/")


# ---------- attribute flatteners ----------
def _flatten_slots(slots: List[dict]) -> Dict[str, Any]:
    flat: Dict[str, Any] = {}
    for slot in slots or []:
        sid = str(slot.get("id") or "")
        prefix = f"slot{sid}_"
        flat[prefix + "active"] = bool(slot.get("active", False))
        flat[prefix + "title"] = slot.get("last", "")
        flat[prefix + "author"] = slot.get("author") or ""
        flat[prefix + "can_activate"] = slot.get("canActivate")
        flat[prefix + "inputs"] = slot.get("inputs")
        flat[prefix + "outputs"] = slot.get("outputs")

        for g in slot.get("gains") or []:
            gid = str(g.get("id"))
            flat[f"{prefix}input{gid}_gain"] = g.get("value")
        for m in slot.get("mutes") or []:
            mid = str(m.get("id"))
            flat[f"{prefix}input{mid}_mute"] = m.get("value")
    return flat


def _active_slot(slots: List[dict]) -> Optional[dict]:
    for slot in slots or []:
        if slot.get("active"):
            return slot
    return None


# ---------- core refresh routine (exported) ----------
async def async_refresh_devices_sensor(
    hass: HomeAssistant, coordinator: EzBEQCoordinator, domain: str
) -> None:
    """Fetch /api/1/devices and update the devices sensor."""
    base_url = _safe_base_url(hass, domain)
    if not base_url:
        _LOGGER.warning("Cannot refresh devices sensor: base_url not found")
        hass.states.async_set(
            DEVICES_SENSOR_ID,
            "unreachable",
            {
                "friendly_name": DEVICES_FRIENDLY_NAME,
                "last_refreshed": _utc_timestamp(),
                "reason": "base_url not available",
            },
        )
        return

    url = f"{base_url}/api/1/devices"
    session = async_get_clientsession(hass)
    try:
        async with session.get(url, timeout=10) as resp:
            resp.raise_for_status()
            data = await resp.json(content_type=None)
    except Exception as e:
        _LOGGER.warning("Failed to fetch MiniDSP devices state: %s", e)
        hass.states.async_set(
            DEVICES_SENSOR_ID,
            "unreachable",
            {
                "friendly_name": DEVICES_FRIENDLY_NAME,
                "last_refreshed": _utc_timestamp(),
                "reason": str(e),
            },
        )
        return

    slots = data.get("slots") or []
    flat = _flatten_slots(slots)
    active = _active_slot(slots)

    attrs: Dict[str, Any] = {
        "friendly_name": DEVICES_FRIENDLY_NAME,
        "last_refreshed": _utc_timestamp(),
        "device_type": data.get("type"),
        "device_name": data.get("name"),
        "master_volume": data.get("masterVolume"),
        "mute": data.get("mute"),
        "serials": data.get("serials") or [],
        "slots_count": len(slots),
        "active_slot_id": active.get("id") if active else "",
        "active_slot_title": active.get("last") if active else "",
        "active_slot_author": active.get("author") if active else "",
        "active_slot_can_activate": active.get("canActivate") if active else None,
        "active_slot_inputs": active.get("inputs") if active else None,
        "active_slot_outputs": active.get("outputs") if active else None,
    }

    if active:
        for g in active.get("gains") or []:
            gid = str(g.get("id"))
            attrs[f"active_slot_input{gid}_gain"] = g.get("value")
        for m in active.get("mutes") or []:
            mid = str(m.get("id"))
            attrs[f"active_slot_input{mid}_mute"] = m.get("value")

    attrs.update(flat)
    attrs["slots_raw"] = slots  # optional full payload

    state = data.get("name") or "online"
    hass.states.async_set(DEVICES_SENSOR_ID, state, attrs)


# ---------- setup / teardown ----------
async def async_setup_devices(
    hass: HomeAssistant,
    coordinator: EzBEQCoordinator,
    domain: str,
    update_interval_secs: int = DEFAULT_REFRESH_INTERVAL_SECS,
) -> Callable[[], None]:
    """
    Register manual refresh service and start periodic refresh.
    Returns a cleanup function to cancel listeners/services.
    """
    refresh_task: List[CALLBACK_TYPE] = []

    async def _manual_refresh_service(call: ServiceCall) -> None:
        await async_refresh_devices_sensor(hass, coordinator, domain)

    hass.services.async_register(domain, "refresh_devices_snapshot", _manual_refresh_service)

    await async_refresh_devices_sensor(hass, coordinator, domain)

    if update_interval_secs and update_interval_secs > 0:
        refresh_task.append(
            async_track_time_interval(
                hass,
                lambda now: hass.async_create_task(
                    async_refresh_devices_sensor(hass, coordinator, domain)
                ),
                timedelta(seconds=update_interval_secs),
            )
        )

    def _unload() -> None:
        hass.services.async_remove(domain, "refresh_devices_snapshot")
        for cancel in refresh_task:
            cancel()

    return _unload
