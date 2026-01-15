from __future__ import annotations

import logging
import time
from typing import Any, List, Dict

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from pyezbeq.models import SearchRequest

from .coordinator import EzBEQCoordinator
from .devices import async_refresh_devices_sensor  # unchanged import

_LOGGER = logging.getLogger(__name__)

CATALOG_URL = "https://beqcatalogue.readthedocs.io/en/latest/database.json"
CATALOG_CACHE_TTL = 7 * 24 * 3600  # 1 week

STATUS_SENSOR_ID = "sensor.ezbeq_load_status"
STATUS_FRIENDLY_NAME = "ezBEQ Load Status"

# ---------------------------------------------------------------------------
# Substitution rules (ordered). Users can edit these lists directly.
# Each rule: enabled, inputs (incoming codec matches any), outputs (try in order).
# Normalization: lowercase string equality.
# ---------------------------------------------------------------------------
SUBSTITUTION_RULES: List[Dict[str, Any]] = [
    {
        "enabled": True,
        "inputs": ["Atmos"],
        "outputs": ["TrueHD 7.1", "TrueHD Atmos", "TrueHD 5.1", "DD+ Atmos"],
    },
    {
        "enabled": True,
        "inputs": ["dts-hd ma 7.1"],
        "outputs": ["dts-x", "dts:x", "dts-x hr", "dts-hd ma 5.1", "dts.hd ma 5.1"],
    },
    {
        "enabled": True,
        "inputs": ["DD+ 5.1", "DD+ 7.1", "DD+ 2.0", "DD+ 2.1"],
        "outputs": ["DD+", "DD+ Atmos", "DD+ 5.1 Atmos"],
    },
    {
        "enabled": True,
        "inputs": ["DTS 5.1", "DTS 6.1"],
        "outputs": ["DTS-HD MA 5.1", "DTS-HD MA 7.1", "DTS-ES 5.1", "DTS-ES 6.1", "DTS-EX 5.1"],
    },
    {
        "enabled": True,
        "inputs": ["DTS-HD MA 5.1", "DTS-HD MA 7.1"],
        "outputs": ["DTS-HD MA 5.1", "DTS-HD MA 7.1"],
    },
    {
        "enabled": True,
        "inputs": ["PCM"],
        "outputs": ["LPCM 5.1", "LPCM 7.1", "LPCM 2.0", "LPCM 1.0"],
    },
    # Add more rules here as needed.
]


async def async_setup_services(
    hass: HomeAssistant, coordinator: EzBEQCoordinator, domain: str
) -> None:
    """Set up the EzBEQ services."""

    # ---------- Status helper ----------
    def _utc_timestamp() -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    def _set_status(state: str, **attrs: Any) -> None:
        """Create/update the status sensor with fixed entity_id."""
        base_attrs = {
            "friendly_name": STATUS_FRIENDLY_NAME,
            "last_changed": _utc_timestamp(),
            "stage": state,
        }
        base_attrs.update({k: v for k, v in attrs.items() if v is not None})
        hass.states.async_set(STATUS_SENSOR_ID, state, base_attrs)
        _LOGGER.debug("STATUS -> %s | manual_load=%s | attrs=%s", state, attrs.get("manual_load"), attrs)

    # Initialize the status sensor
    _set_status("idle")

    # ---------- Catalogue fetcher (shared) ----------
    async def _get_catalog_items() -> list[dict] | None:
        domain_cache = hass.data.setdefault(domain, {})
        cache = domain_cache.get("catalog_cache")
        now = time.time()
        items = None
        if cache and (now - cache["ts"] < CATALOG_CACHE_TTL):
            items = cache["items"]

        if items is None:
            session = async_get_clientsession(hass)
            try:
                async with session.get(CATALOG_URL, timeout=15) as resp:
                    resp.raise_for_status()
                    data = await resp.json(content_type=None)
            except Exception as e:
                _LOGGER.warning("Could not fetch BEQ catalogue: %s", e)
                return None

            if isinstance(data, list):
                items = data
            elif isinstance(data, dict):
                items = data.get("titles") or list(data.values())
            else:
                return None

            domain_cache["catalog_cache"] = {"ts": now, "items": items}
        return items

    # ---------- Catalogue match helpers ----------
    def _normalize_codec(value: str | None) -> str:
        return (value or "").strip().lower()

    def _match_catalog_item(
        items: list[dict],
        tmdb: str,
        codec: str,
        edition: str,
        year: int,
        title: str,
    ) -> dict | None:
        tmdb_str = str(tmdb).strip()
        codec_norm = _normalize_codec(codec)
        edition_norm = (edition or "").strip().lower()
        year_str = str(year).strip()
        title_norm = (title or "").strip().lower()

        def _edition_matches(item: dict) -> bool:
            if not edition_norm:
                return True
            return (item.get("edition", "") or "").strip().lower() == edition_norm

        def _codec_matches(item: dict) -> bool:
            audio_types = item.get("audioTypes") or []
            if isinstance(audio_types, str):
                audio_types = [audio_types]
            return any((a or "").strip().lower() == codec_norm for a in audio_types)

        for item in items:
            if str(item.get("theMovieDB", "")).strip() == tmdb_str and _codec_matches(item) and _edition_matches(item):
                return item

        for item in items:
            if (
                str(item.get("year", "")).strip() == year_str
                and (item.get("title", "") or "").strip().lower() == title_norm
                and _codec_matches(item)
                and _edition_matches(item)
            ):
                return item
        return None

    def _author_matches(item_author: str | list[str], target: str) -> bool:
        if not target:
            return True
        target_norm = target.strip().lower()
        if isinstance(item_author, list):
            return any(str(a).strip().lower() == target_norm for a in item_author)
        return str(item_author).strip().lower() == target_norm

    def _match_catalog_item_preferring_author(
        items: list[dict],
        tmdb: str,
        codec: str,
        edition: str,
        year: int,
        title: str,
        preferred_author: str,
    ) -> dict | None:
        """First try to match tmdb+codec+edition with preferred_author; fall back later."""
        tmdb_str = str(tmdb).strip()
        codec_norm = _normalize_codec(codec)
        edition_norm = (edition or "").strip().lower()
        preferred = preferred_author.strip().lower()

        def _edition_matches(item: dict) -> bool:
            if not edition_norm:
                return True
            return (item.get("edition", "") or "").strip().lower() == edition_norm

        def _codec_matches(item: dict) -> bool:
            audio_types = item.get("audioTypes") or []
            if isinstance(audio_types, str):
                audio_types = [audio_types]
            return any((a or "").strip().lower() == codec_norm for a in audio_types)

        for item in items:
            if str(item.get("theMovieDB", "")).strip() != tmdb_str:
                continue
            if not _codec_matches(item) or not _edition_matches(item):
                continue
            if not preferred:
                return item
            item_author = item.get("author") or item.get("authors") or ""
            if _author_matches(item_author, preferred):
                return item
        return None

    def _extract_author(item: dict | None) -> str:
        if not item:
            return ""
        author = item.get("author") or item.get("authors") or ""
        if isinstance(author, list):
            return ", ".join(str(a) for a in author if a)
        return str(author)

    def _extract_extra_fields(item: dict | None) -> Dict[str, Any]:
        """Pull additional fields for the status sensor; safe defaults if missing."""
        if not item:
            return {}
        imgs = item.get("images") or []
        if isinstance(imgs, str):
            imgs = [imgs]
        runtime_raw = item.get("runtime")
        try:
            runtime_minutes = int(runtime_raw) if runtime_raw is not None else None
        except (TypeError, ValueError):
            runtime_minutes = None
        return {
            "tmdb_id": item.get("theMovieDB") or "",
            "title": item.get("title") or "",
            "alt_title": item.get("altTitle") or "",
            "source": item.get("source") or "",
            "content_type": item.get("content_type") or "",
            "language": item.get("language") or "",
            "mv_offset": float(item.get("mv")) if str(item.get("mv")).strip() not in ("", "None", "null") else None,
            "audio_types": item.get("audioTypes") or [],
            "warning": item.get("warning") or "",
            "note": item.get("note") or "",
            "image1": imgs[0] if len(imgs) >= 1 else "",
            "image2": imgs[1] if len(imgs) >= 2 else "",
            "runtime_minutes": runtime_minutes,
            "genres": item.get("genres") or [],
            "created_at": item.get("created_at"),
        }

    # ---------- Substitution helpers ----------
    def _rule_applies(rule: Dict[str, Any], original_codec_norm: str) -> bool:
        if not rule.get("enabled", False):
            return False
        inputs = [_normalize_codec(x) for x in rule.get("inputs", [])]
        return original_codec_norm in inputs

    def _catalog_has_codec(
        items: list[dict], tmdb: str, edition: str, candidate_codec_norm: str
    ) -> bool:
        tmdb_str = str(tmdb).strip()
        edition_norm = (edition or "").strip().lower()

        def _edition_matches(item: dict) -> bool:
            if not edition_norm:
                return True
            return (item.get("edition", "") or "").strip().lower() == edition_norm

        for item in items:
            if str(item.get("theMovieDB", "")).strip() != tmdb_str:
                continue
            if not _edition_matches(item):
                continue
            audio_types = item.get("audioTypes") or []
            if isinstance(audio_types, str):
                audio_types = [audio_types]
            audio_types_norm = [(a or "").strip().lower() for a in audio_types]
            if candidate_codec_norm in audio_types_norm:
                return True
        return False

    # ---------- Service: load_beq_profile ----------
    async def load_beq_profile(call: ServiceCall) -> None:
        """Load a BEQ profile."""
        enable_audio_codec_subs = bool(call.data.get("enable_audio_codec_substitutions", False))
        manual_load = bool(call.data.get("manual_load", False))

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

        # Remember whether a preferred author was supplied; blank if not
        preferred_supplied = bool(search_request.preferred_author.strip())
        if not preferred_supplied:
            search_request.preferred_author = ""

        used_codec = search_request.codec  # Track the codec actually loaded
        author = ""
        catalog_items: list[dict] | None = None
        matched_item: dict | None = None  # keep the match for extra attrs

        # Pre-match to inject author if missing (aligns automatic load with manual determinism)
        catalog_items = await _get_catalog_items()
        if catalog_items:
            matched_item = _match_catalog_item_preferring_author(
                catalog_items,
                search_request.tmdb,
                search_request.codec,
                search_request.edition,
                search_request.year,
                search_request.title or "",
                search_request.preferred_author,
            ) or _match_catalog_item(
                catalog_items,
                search_request.tmdb,
                search_request.codec,
                search_request.edition,
                search_request.year,
                search_request.title or "",
            )
            if matched_item and not preferred_supplied:
                search_request.preferred_author = _extract_author(matched_item)

        _set_status(
            "loading_primary",
            profile=search_request.title,
            codec=used_codec,
            edition=search_request.edition,
            slots=search_request.slots,
            manual_load=manual_load,
        )

        try:
            await coordinator.client.load_beq_profile(search_request)
            _LOGGER.info("Successfully loaded BEQ profile")
            if catalog_items and matched_item:
                author = _extract_author(matched_item) or ""
            elif catalog_items:
                matched_item = _match_catalog_item_preferring_author(
                    catalog_items,
                    search_request.tmdb,
                    used_codec,
                    search_request.edition,
                    search_request.year,
                    search_request.title or "",
                    search_request.preferred_author,
                ) or _match_catalog_item(
                    catalog_items,
                    search_request.tmdb,
                    used_codec,
                    search_request.edition,
                    search_request.year,
                    search_request.title or "",
                )
                author = _extract_author(matched_item) or ""
        except Exception as e:
            _LOGGER.warning("Primary load failed for codec '%s': %s", search_request.codec, e)
            if not enable_audio_codec_subs:
                _set_status(
                    "load_fail",
                    reason=str(e),
                    profile=search_request.title,
                    codec=used_codec,
                    edition=search_request.edition,
                    slots=search_request.slots,
                    manual_load=manual_load,
                )
                raise HomeAssistantError(f"Failed to load BEQ profile: {e}") from e

            catalog_items = catalog_items or await _get_catalog_items()
            if not catalog_items:
                _set_status(
                    "load_fail",
                    reason=f"No catalogue for substitutions: {e}",
                    profile=search_request.title,
                    codec=used_codec,
                    edition=search_request.edition,
                    slots=search_request.slots,
                    manual_load=manual_load,
                )
                raise HomeAssistantError(
                    f"Failed to load BEQ profile (no catalogue for substitutions): {e}"
                ) from e

            original_codec_norm = _normalize_codec(search_request.codec)
            substitute_found = False

            for rule in SUBSTITUTION_RULES:
                if not _rule_applies(rule, original_codec_norm):
                    continue
                outputs = [_normalize_codec(x) for x in rule.get("outputs", [])]
                for cand in outputs:
                    if cand == original_codec_norm:
                        continue  # skip identical
                    if not _catalog_has_codec(catalog_items, search_request.tmdb, search_request.edition, cand):
                        continue
                    _LOGGER.info("Retrying load with substitute codec '%s'", cand)
                    search_request.codec = cand
                    used_codec = cand
                    _set_status(
                        "loading_secondary",
                        profile=search_request.title,
                        codec=used_codec,
                        edition=search_request.edition,
                        slots=search_request.slots,
                        manual_load=manual_load,
                    )
                    try:
                        await coordinator.client.load_beq_profile(search_request)
                        _LOGGER.info("Successfully loaded BEQ profile after substitution")
                        matched_item = _match_catalog_item_preferring_author(
                            catalog_items,
                            search_request.tmdb,
                            used_codec,
                            search_request.edition,
                            search_request.year,
                            search_request.title or "",
                            search_request.preferred_author,
                        ) or _match_catalog_item(
                            catalog_items,
                            search_request.tmdb,
                            used_codec,
                            search_request.edition,
                            search_request.year,
                            search_request.title or "",
                        )
                        author = _extract_author(matched_item) or ""
                        substitute_found = True
                        break
                    except Exception as e2:
                        _LOGGER.warning("Substitution load with codec '%s' failed: %s", cand, e2)
                        continue
                if substitute_found:
                    break

            if not substitute_found:
                _set_status(
                    "load_fail",
                    reason=str(e),
                    profile=search_request.title,
                    codec=used_codec,
                    edition=search_request.edition,
                    slots=search_request.slots,
                    manual_load=manual_load,
                )
                raise HomeAssistantError(f"Failed to load BEQ profile after substitutions: {e}") from e

        extra_attrs = _extract_extra_fields(matched_item)

        _set_status(
            "load_success",
            profile=search_request.title,
            codec=used_codec,
            edition=search_request.edition,
            slots=search_request.slots,
            author=author,
            manual_load=manual_load,
            **extra_attrs,
        )

        # MiniDSP may have changed: refresh snapshot (fire-and-forget)
        hass.async_create_task(async_refresh_devices_sensor(hass, coordinator, domain))

    # ---------- Service: unload_beq_profile ----------
    async def unload_beq_profile(call: ServiceCall) -> None:
        """Unload the BEQ profile."""
        slots = call.data.get("slots", [1])
        manual_load = bool(call.data.get("manual_load", False))  # keep flag for consistency
        _set_status("unloading", slots=slots, manual_load=manual_load)
        try:
            search_request = SearchRequest(
                preferred_author="",
                edition="",
                tmdb="",  # Not used for unloading, but required by the model
                year=0,
                codec="",
                slots=slots,
            )
            await coordinator.client.unload_beq_profile(search_request)
            _LOGGER.info("Successfully unloaded BEQ profile")
            _set_status("unload_success", slots=slots, manual_load=manual_load)
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
            _set_status("unload_fail", reason=str(e), slots=slots, manual_load=manual_load)
            raise HomeAssistantError(f"Failed to unload BEQ profile: {e}") from e
        finally:
            hass.async_create_task(async_refresh_devices_sensor(hass, coordinator, domain))

    hass.services.async_register(domain, "load_beq_profile", load_beq_profile)
    hass.services.async_register(domain, "unload_beq_profile", unload_beq_profile)


async def async_unload_services(hass: HomeAssistant, domain: str) -> None:
    """Unload EzBEQ services."""
    hass.services.async_remove(domain, "load_beq_profile")
    hass.services.async_remove(domain, "unload_beq_profile")
