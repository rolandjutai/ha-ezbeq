from __future__ import annotations

import json
import logging
from typing import Any, List, Optional, Tuple

try:
    from httpx import Response  # type: ignore
except Exception:  # pragma: no cover
    Response = Any  # fallback typing if httpx import fails during type checking


def _coerce_number(value: Any, default: float = 0.0) -> float:
    """Return value if it's int/float; otherwise default (used for None)."""
    if isinstance(value, (int, float)):
        return float(value)
    return float(default)


def _normalize_and_override_gains_inplace(
    obj: Any,
    override_pair: Optional[Tuple[float, float]] = None,
) -> bool:
    """
    Recursively normalize/override the payload in-place.

    - If override_pair is provided (e.g., (0.0, 0.0)):
        * For any dict with key 'gains' where value is a list:
            - replace the list with fixed values based on override_pair.
              If the list length is 2, set exactly [g0, g1].
              If length is N != 2, set all N entries to g0 to preserve shape.
        * For dict-style gains ('gain1', 'gain2'), set them to g0/g1 respectively.
    - Regardless of override:
        * Coerce None/non-numeric entries in 'gains' to 0.0
        * Coerce 'gain1'/'gain2' None to 0.0

    Returns True if any change was made.
    """
    changed = False

    if isinstance(obj, dict):
        # List-style gains: "gains": [x, y, ...]
        if "gains" in obj and isinstance(obj["gains"], list):
            gains_list = obj["gains"]

            # Apply override first (if requested)
            if override_pair is not None:
                g0, g1 = float(override_pair[0]), float(override_pair[1])
                if len(gains_list) == 2:
                    new_list = [g0, g1]
                else:
                    # Preserve length; fill with first override value
                    new_list = [g0 for _ in gains_list]
                if new_list != gains_list:
                    obj["gains"] = new_list
                    gains_list = new_list
                    changed = True

            # Normalize any remaining None/non-number entries to 0.0
            normalized_list: List[float] = []
            mutated = False
            for v in gains_list:
                if v is None or not isinstance(v, (int, float)):
                    normalized_list.append(_coerce_number(v, 0.0))
                    mutated = True
                else:
                    normalized_list.append(float(v))
            if mutated:
                obj["gains"] = normalized_list
                changed = True

        # Dict-style gains: {"gain1": x, "gain2": y}
        g1_present = "gain1" in obj
        g2_present = "gain2" in obj
        if g1_present or g2_present:
            if override_pair is not None:
                g0, g1 = float(override_pair[0]), float(override_pair[1])
                if g1_present and obj.get("gain1") != g0:
                    obj["gain1"] = g0
                    changed = True
                if g2_present and obj.get("gain2") != g1:
                    obj["gain2"] = g1
                    changed = True
            # Normalize None to 0.0 (in case override not provided)
            if g1_present and obj.get("gain1") is None:
                obj["gain1"] = 0.0
                changed = True
            if g2_present and obj.get("gain2") is None:
                obj["gain2"] = 0.0
                changed = True

        # Recurse
        for _, v in list(obj.items()):
            if isinstance(v, (dict, list)):
                if _normalize_and_override_gains_inplace(v, override_pair):
                    changed = True

    elif isinstance(obj, list):
        for v in obj:
            if isinstance(v, (dict, list)):
                if _normalize_and_override_gains_inplace(v, override_pair):
                    changed = True

    return changed


class HttpxLogProxy:
    """
    Transparent proxy around an httpx.AsyncClient that:
      - Logs HTTP method, URL, and JSON payload (if any)
      - Normalizes payload (coerces None/non-numeric gains -> 0.0)
      - Optionally overrides all outgoing gains to a fixed pair (e.g., [0.0, 0.0])
      - Logs response status code and a short preview

    Behavior aside from normalization/override and logging is unchanged.
    """

    def __init__(
        self,
        inner: Any,
        logger: logging.Logger,
        max_preview: int = 1000,
        override_gains: bool = False,
        override_gains_values: Tuple[float, float] = (0.0, 0.0),
    ) -> None:
        self._inner = inner
        self._logger = logger
        self._max_preview = max_preview
        self._override_pair: Optional[Tuple[float, float]] = (
            (float(override_gains_values[0]), float(override_gains_values[1]))
            if override_gains
            else None
        )

    async def request(self, method: str, url: Any, *args: Any, **kwargs: Any) -> Response:
        # Log outgoing JSON payload, normalize, and optionally override gains
        js = kwargs.get("json", None)
        if isinstance(js, dict):
            try:
                original = json.dumps(js, ensure_ascii=False, sort_keys=True)
            except Exception:
                original = "<unserializable JSON dict>"

            changed = _normalize_and_override_gains_inplace(js, self._override_pair)

            try:
                modified = json.dumps(js, ensure_ascii=False, sort_keys=True)
            except Exception:
                modified = "<unserializable JSON dict>"

            if changed:
                self._logger.debug("ezBEQ HTTP %s %s json (modified): %s", method.upper(), url, modified)
                self._logger.debug("ezBEQ HTTP %s %s json (original): %s", method.upper(), url, original)
            else:
                self._logger.debug("ezBEQ HTTP %s %s json: %s", method.upper(), url, modified)
        else:
            if js is not None:
                self._logger.debug(
                    "ezBEQ HTTP %s %s (non-dict JSON payload type=%s)",
                    method.upper(),
                    url,
                    type(js).__name__,
                )
            else:
                self._logger.debug("ezBEQ HTTP %s %s", method.upper(), url)

        resp: Response = await self._inner.request(method, url, *args, **kwargs)

        # Log response code and a short preview
        try:
            text = getattr(resp, "text", "")
            preview = text[: self._max_preview] if isinstance(text, str) else "<non-text body>"
        except Exception:
            preview = "<unavailable>"
        self._logger.debug(
            "ezBEQ RESP %s %s -> %s: %s",
            method.upper(),
            url,
            getattr(resp, "status_code", "?"),
            preview,
        )
        return resp

    # Convenience methods delegate to request (fixed: no bad annotations in calls)
    async def post(self, url: Any, *args: Any, **kwargs: Any) -> Response:
        return await self.request("POST", url, *args, **kwargs)

    async def get(self, url: Any, *args: Any, **kwargs: Any) -> Response:
        return await self.request("GET", url, *args, **kwargs)

    async def put(self, url: Any, *args: Any, **kwargs: Any) -> Response:
        return await self.request("PUT", url, *args, **kwargs)

    async def patch(self, url: Any, *args: Any, **kwargs: Any) -> Response:
        return await self.request("PATCH", url, *args, **kwargs)

    async def delete(self, url: Any, *args: Any, **kwargs: Any) -> Response:
        return await self.request("DELETE", url, *args, **kwargs)

    # Forward everything else (including aclose, headers, cookies, etc.)
    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)
