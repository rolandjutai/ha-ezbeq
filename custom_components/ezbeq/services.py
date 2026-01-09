from __future__ import annotations

import logging
import time  # NEW
from typing import Any

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from pyezbeq.models import SearchRequest

from .coordinator import EzBEQCoordinator

_LOGGER = logging.getLogger(__name__)

CATALOG_URL = "https://beqcatalogue.readthedocs.io/en/latest/database.json"
CATALOG_CACHE_TTL = 7 * 24 * 3600  # 1 week


async def async_setup_services(
    hass: HomeAssistant, coordinator: EzBEQCoordinator, domain: str
) -> None:
    """Set up the EzBEQ services."""

    async def fetch_first_image_url(
        tmdb: str, codec: str, edition: str, year: int, title: str
    ) -> str | None:
        """Fetch the first image URL from the BEQ catalogue."""
        # --- cache lookup ---
        domain_cache = hass.data.setdefault(domain, {})
        cache = domain_cache.get("catalog_cache")
        now = time.time()
        items = None
        if cache and (now - cache["ts"] < CATALOG_CACHE_TTL):
            items = cache["items"]

        # --- fetch if cache miss/expired ---
        if items is None:
            session = async_get_clientsession(hass)
            try:
                async with session.get(CATALOG_URL, timeout=15) as resp:
                    resp.raise_for_status()
                    data = await resp.json(content_type=None)
            except Exception as e:
                _LOGGER.warning("Could not fetch BEQ catalogue images: %s", e)
                return None

            # The catalogue is a list; keep a fallback if it changes shape
            if isinstance(data, list):
                items = data
            elif isinstance(data, dict):
                items = data.get("titles") or list(data.values())
            else:
                return None

            # store in cache
            domain_cache["catalog_cache"] = {"ts": now, "items": items}

        tmdb_str = str(tmdb).strip()
        codec_norm = (codec or "").strip().lower()
        edition_norm = (edition or "").strip().lower()
        year_str = str(year).strip()
        title_norm = (title or "").strip().lower()

        def _get_images(item: dict) -> list[str]:
            imgs = item.get("images") or []
            if isinstance(imgs, str):
                imgs = [imgs]
            return imgs

        def _codec_matches(item: dict) -> bool:
            audio_types = item.get("audioTypes") or []
            if isinstance(audio_types, str):
                audio_types = [audio_types]
            return any((a or "").strip().lower() == codec_norm for a in audio_types)

        def _edition_matches(item: dict) -> bool:
            if not edition_norm:
                return True  # ignore edition if empty
            return (item.get("edition", "") or "").strip().lower() == edition_norm

        # Match by TMDB id (field is "theMovieDB" in this catalogue), codec, and edition (if provided)
        for item in items:
            if str(item.get("theMovieDB", "")).strip() == tmdb_str and _codec_matches(item) and _edition_matches(item):
                imgs = _get_images(item)
                if imgs:
                    return imgs[0]

        # Fallback: match by title + year + codec + edition
        for item in items:
            if (
                str(item.get("year", "")).strip() == year_str
                and (item.get("title", "") or "").strip().lower() == title_norm
                and _codec_matches(item)
                and _edition_matches(item)
            ):
                imgs = _get_images(item)
                if imgs:
                    return imgs[0]

        return None

    async def load_beq_profile(call: ServiceCall) -> None:
        """Load a BEQ profile."""

        def get_sensor_state(entity_id: str) -> Any:
            """Get the state of a sensor entity."""
            state = hass.states.get(entity_id)
            if state is None:
                raise HomeAssistantError(f"Sensor {entity_id} not found")
            return state.state

        try:
            search_request = SearchRequest(
                tmdb=get_sensor_state(call.data["tmdb_sensor"]),
                year=int(get_sensor_state(call.data["year_sensor"])),
                codec=get_sensor_state(call.data["codec_sensor"]),
                preferred_author=call.data.get("preferred_author", ""),
                edition=(
                    get_sensor_state(call.data["edition_sensor"])
                    if "edition_sensor" in call.data
                    else ""
                ),
                slots=call.data.get("slots", [1]),
                title=(
                    get_sensor_state(call.data["title_sensor"])
                    if "title_sensor" in call.data
                    else ""
                ),
            )
        except ValueError as e:
            raise HomeAssistantError(f"Invalid sensor data: {e}") from e

        try:
            await coordinator.client.load_beq_profile(search_request)
            _LOGGER.info("Successfully loaded BEQ profile")
        except Exception as e:
            # Surface HTTP details if available (non-breaking)
            resp = getattr(e, "response", None)
            if resp is not None:
                try:
                    _LOGGER.error(
                        "Failed to load BEQ profile: %s (status=%s, body=%s)",
                        e,
                        getattr(resp, "status_code", "?"),
                        getattr(resp, "text", "")[:800],
                    )
                except Exception:
                    _LOGGER.error("Failed to load BEQ profile: %s (response present but unreadable)", e)
            else:
                _LOGGER.error("Failed to load BEQ profile: %s", e)
            raise HomeAssistantError(f"Failed to load BEQ profile: {e}") from e

        # Populate image sensor with first image URL (optional)
        image_sensor = call.data.get("image_sensor")
        if image_sensor:
            image_url = await fetch_first_image_url(
                tmdb=search_request.tmdb,
                codec=search_request.codec,
                edition=search_request.edition,
                year=search_request.year,
                title=search_request.title or "",
            )
            hass.states.async_set(
                image_sensor,
                image_url or "Not Found",
                {"source": "beq_catalogue", "tmdb": search_request.tmdb},
            )
            if not image_url:
                _LOGGER.info("No image found in BEQ catalogue for tmdb=%s", search_request.tmdb)

    async def unload_beq_profile(call: ServiceCall) -> None:
        """Unload the BEQ profile."""
        try:
            slots = call.data.get("slots", [1])
            search_request = SearchRequest(
                preferred_author="",
                edition="",
                tmdb="",  # These fields are not used for unloading, but are required by the SearchRequest model
                year=0,
                codec="",
                slots=slots,
            )
            await coordinator.client.unload_beq_profile(search_request)
            _LOGGER.info("Successfully unloaded BEQ profile")
        except Exception as e:
            resp = getattr(e, "response", None)
            if resp is not None:
                try:
                    _LOGGER.error(
                        "Failed to unload BEQ profile: %s (status=%s, body=%s)",
                        e,
                        getattr(resp, "status_code", "?"),
                        getattr(resp, "text", "")[:800],
                    )
                except Exception:
                    _LOGGER.error("Failed to unload BEQ profile: %s (response present but unreadable)", e)
            else:
                _LOGGER.error("Failed to unload BEQ profile: %s", e)
            raise HomeAssistantError(f"Failed to unload BEQ profile: {e}") from e
        # NEW: clear the image sensor, if provided
        image_sensor = call.data.get("image_sensor")
        if image_sensor:
            hass.states.async_set(
                image_sensor,
                "",  # empty state to indicate cleared
                {"source": "beq_catalogue", "tmdb": ""},
            )

    hass.services.async_register(domain, "load_beq_profile", load_beq_profile)
    hass.services.async_register(domain, "unload_beq_profile", unload_beq_profile)


async def async_unload_services(hass: HomeAssistant, domain: str) -> None:
    """Unload EzBEQ services."""
    hass.services.async_remove(domain, "load_beq_profile")
