"""Current weather for the clock card: one fixed place, read-only, two exact hosts.

The place is the operator's explicit choice (Beşiktaş, İstanbul, Türkiye) -
never IP, device, browser or time-zone location. It is geocoded once through
Open-Meteo, checked to really be that district, and cached; every refresh then
asks Tomorrow.io's realtime endpoint for those coordinates.

Nothing here reaches a model or a provider, reads a file other than its own
coordinate cache, or follows a redirect. The Tomorrow.io key travels only in
the request header; it is never stored, returned, logged or put in a URL.
Failures become one short code; no value is ever invented.
"""

from __future__ import annotations

import json
import math
import socket
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

KEY_ENV = "TOMORROW_API_KEY"
LOCATION = {"country": "Türkiye", "country_code": "TR", "province": "İstanbul", "district": "Beşiktaş"}
GEOCODER_URL = "https://geocoding-api.open-meteo.com/v1/search"
WEATHER_URL = "https://api.tomorrow.io/v4/weather/realtime"
APPROVED_HOSTS = frozenset({"geocoding-api.open-meteo.com", "api.tomorrow.io"})
TIMEOUT_SECONDS = 10
REFRESH_SECONDS = 5 * 60
#: A failed call waits this long before trying again (rate limit, bad key, no geocoder).
HOLD_SECONDS = 30 * 60
#: Up to this age a reading is current; after it, it is shown only as "last reading".
FRESH_SECONDS = 20 * 60
#: Past this age a reading is not shown at all.
EXPIRED_SECONDS = 2 * 60 * 60

#: Tomorrow.io's documented base ``weatherCode`` values -> JARVIS condition keys.
#: 0 ("Unknown") and anything undocumented map to no condition at all.
CONDITIONS = {
    1000: "clear", 1100: "mostly_clear", 1101: "partly_cloudy", 1102: "mostly_cloudy", 1001: "cloudy",
    2000: "fog", 2100: "light_fog",
    4000: "drizzle", 4001: "rain", 4200: "light_rain", 4201: "heavy_rain",
    5000: "snow", 5001: "flurries", 5100: "light_snow", 5101: "heavy_snow",
    6000: "freezing_drizzle", 6001: "freezing_rain", 6200: "light_freezing_rain", 6201: "heavy_freezing_rain",
    7000: "ice_pellets", 7101: "heavy_ice_pellets", 7102: "light_ice_pellets",
    8000: "thunderstorm",
}


class WeatherError(Exception):
    """A failure reduced to one code: no_key, timeout, network, rate_limited, auth, http, malformed, not_found."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:   # a 3xx becomes an HTTPError
        return None


_OPENER = build_opener(_NoRedirect)


def fetch_json(url: str, params: dict[str, str], headers: dict[str, str] | None = None) -> Any:
    """GET one of the two approved endpoints; no cookies, no redirects, bounded time."""
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname not in APPROVED_HOSTS or url not in (GEOCODER_URL, WEATHER_URL):
        raise WeatherError("not_allowed")
    request = Request(url + "?" + urlencode(params), headers={"Accept": "application/json", **(headers or {})})
    try:
        with _OPENER.open(request, timeout=TIMEOUT_SECONDS) as response:
            body = response.read(256_000)
    except HTTPError as error:
        raise WeatherError("rate_limited" if error.code == 429 else "auth" if error.code in (401, 403) else "http") from None
    except (TimeoutError, socket.timeout):
        raise WeatherError("timeout") from None
    except URLError as error:
        raise WeatherError("timeout" if isinstance(error.reason, (TimeoutError, socket.timeout)) else "network") from None
    except OSError:
        raise WeatherError("network") from None
    try:
        return json.loads(body)
    except (ValueError, UnicodeDecodeError):
        raise WeatherError("malformed") from None


def fold(text: Any) -> str:
    """Compare place names without case or Turkish diacritics (İstanbul == istanbul, Beşiktaş == besiktas)."""
    decomposed = unicodedata.normalize("NFKD", str(text or "").replace("ı", "i"))
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).casefold().strip()


def pick_location(payload: Any, wanted: dict[str, str] = LOCATION) -> dict[str, Any]:
    """The geocoder result that really is the wanted district - never a same-named place elsewhere."""
    results = payload.get("results") if isinstance(payload, dict) else None
    for result in results if isinstance(results, list) else []:
        if not isinstance(result, dict):
            continue
        if (str(result.get("country_code", "")).upper() != wanted["country_code"]
                or fold(result.get("admin1")) != fold(wanted["province"])
                or fold(wanted["district"]) not in (fold(result.get("name")), fold(result.get("admin2")))):
            continue
        lat, lon = result.get("latitude"), result.get("longitude")
        if valid_coordinates(lat, lon):
            return {**wanted, "display_name": f"{wanted['district']}, {wanted['province']}, {wanted['country']}",
                    "latitude": float(lat), "longitude": float(lon), "resolved_at": int(time.time())}
    raise WeatherError("not_found")


def valid_coordinates(lat: Any, lon: Any) -> bool:
    return (all(type(v) in (int, float) and math.isfinite(v) for v in (lat, lon))
            and -90 <= lat <= 90 and -180 <= lon <= 180)


def resolve(wanted: dict[str, str] = LOCATION, fetch: Callable[..., Any] = fetch_json) -> dict[str, Any]:
    payload = fetch(GEOCODER_URL, {"name": wanted["district"], "count": "10", "language": "tr", "format": "json"})
    return pick_location(payload, wanted)


def load_cached(path: Path, wanted: dict[str, str] = LOCATION) -> dict[str, Any] | None:
    """The cached coordinates, only if they are for the place still wanted."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if (not isinstance(data, dict) or any(data.get(k) != v for k, v in wanted.items())
            or not valid_coordinates(data.get("latitude"), data.get("longitude"))):
        return None
    return data


def save_cached(path: Path, location: dict[str, Any]) -> None:
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(location, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass            # the next start simply geocodes again


def parse_realtime(payload: Any, fetched_at: float | None = None) -> dict[str, Any]:
    """The few fields the card needs from a realtime answer; anything implausible is refused whole."""
    data = payload.get("data") if isinstance(payload, dict) else None
    values = data.get("values") if isinstance(data, dict) else None
    if not isinstance(values, dict):
        raise WeatherError("malformed")
    temperature = values.get("temperature")
    if type(temperature) not in (int, float) or not math.isfinite(temperature) or not -90 <= temperature <= 70:
        raise WeatherError("malformed")
    try:
        stamp = datetime.fromisoformat(str(data.get("time", "")))
    except ValueError:
        raise WeatherError("malformed") from None
    if stamp.tzinfo is None:            # a time without a zone could be anyone's
        raise WeatherError("malformed")
    observed = stamp.timestamp()
    fetched = time.time() if fetched_at is None else fetched_at
    if observed > fetched + 10 * 60:
        raise WeatherError("malformed")
    code = values.get("weatherCode")
    reading = {"temperature": float(temperature), "condition": CONDITIONS.get(code, "") if type(code) is int else "",
               "observed_at": observed, "fetched_at": fetched}
    apparent = values.get("temperatureApparent")
    if type(apparent) in (int, float) and math.isfinite(apparent) and -90 <= apparent <= 70:
        reading["feels_like"] = float(apparent)
    chance = values.get("precipitationProbability")
    if type(chance) in (int, float) and 0 <= chance <= 100:
        reading["precipitation_probability"] = float(chance)
    return reading


def current(location: dict[str, Any], key: str, fetch: Callable[..., Any] = fetch_json) -> dict[str, Any]:
    if not key:
        raise WeatherError("no_key")
    payload = fetch(WEATHER_URL, {"location": f"{location['latitude']:.5f},{location['longitude']:.5f}", "units": "metric"},
                    {"apikey": key})
    return parse_realtime(payload)


def refresh(cache: Path, key: str, fetch: Callable[..., Any] = fetch_json,
            location: dict[str, Any] | None = None) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str]:
    """One refresh: (location, reading, error code). Geocodes only when no valid cached place exists."""
    if not key:
        return location, None, "no_key"
    try:
        if location is None:
            location = load_cached(cache)
        if location is None:
            location = resolve(fetch=fetch)
            save_cached(cache, location)
        return location, current(location, key, fetch), ""
    except WeatherError as error:
        return location, None, error.code


def project(location: dict[str, Any] | None, reading: dict[str, Any] | None, now: float | None = None) -> dict[str, Any]:
    """What the card may show: fresh, stale ("last reading") or nothing. Never a key, never a default value."""
    now = time.time() if now is None else now
    age = now - reading["observed_at"] if reading else None
    if location is None or reading is None or age is None or age > EXPIRED_SECONDS:
        return {"available": False, "state": "unavailable"}
    return {"available": True, "state": "fresh" if age <= FRESH_SECONDS else "stale",
            "district": location["district"], "temperature": round(reading["temperature"]),
            "condition": reading["condition"], "observed_at": int(reading["observed_at"]),
            "fetched_at": int(reading["fetched_at"])}
