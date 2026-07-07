"""
GPS location extraction and reverse geocoding helper.

Provides:
  - extract_gps_coords(image_source) -> (lat, lon) | (None, None)
  - reverse_geocode(lat, lon)        -> "City, Country" | None
  - get_location_tag(image_source)   -> "city, country" (lowercased) | None

image_source can be a file path (str) or a seekable file-like object (BytesIO / file stream).
After calling extract_gps_coords on a stream, the caller is responsible for seeking back
to position 0 if the stream will be read again.
"""

import time
import requests
from PIL import Image
from PIL.ExifTags import TAGS, GPSTAGS

# Nominatim free reverse-geocoding endpoint (OpenStreetMap)
_NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
_USER_AGENT = "BackupMyMedia/1.0"

# Simple in-process cache: (rounded_lat, rounded_lon) -> location string
# Coords are rounded to 2 decimal places (~1 km) to avoid redundant lookups
# for photos taken at the same general location.
_geocode_cache: dict[tuple[float, float], str | None] = {}

# Nominatim asks for max 1 request/second from any single app
_RATE_LIMIT_SECONDS = 1.1
_last_request_time: float = 0.0


def _dms_to_decimal(dms, ref: str) -> float:
    """Convert degrees/minutes/seconds tuple to a signed decimal degree float."""
    degrees, minutes, seconds = dms
    # Pillow may return IFDRational objects; convert to float
    decimal = float(degrees) + float(minutes) / 60 + float(seconds) / 3600
    if ref in ("S", "W"):
        decimal = -decimal
    return decimal


def extract_gps_coords(image_source) -> tuple[float | None, float | None]:
    """
    Open an image and return (latitude, longitude) as decimal degrees.
    Returns (None, None) if no GPS data is present or on any error.

    image_source: file path string, or a seekable binary file-like object.
    """
    try:
        img = Image.open(image_source)
        exif_raw = img._getexif()
        if not exif_raw:
            return None, None

        # Tag ID 34853 is GPSInfo
        gps_raw = exif_raw.get(34853)
        if not gps_raw:
            return None, None

        gps = {GPSTAGS.get(k, k): v for k, v in gps_raw.items()}

        lat_dms = gps.get("GPSLatitude")
        lat_ref = gps.get("GPSLatitudeRef")
        lon_dms = gps.get("GPSLongitude")
        lon_ref = gps.get("GPSLongitudeRef")

        if not (lat_dms and lat_ref and lon_dms and lon_ref):
            return None, None

        lat = _dms_to_decimal(lat_dms, lat_ref)
        lon = _dms_to_decimal(lon_dms, lon_ref)
        return lat, lon

    except Exception:
        return None, None


def reverse_geocode(lat: float, lon: float) -> str | None:
    """
    Convert decimal-degree coordinates to a human-readable location string
    using the Nominatim (OpenStreetMap) API.

    Returns a string like "San Francisco, United States" or None on failure.
    Caches results to avoid redundant API calls.
    """
    global _last_request_time

    # Round to ~1 km precision for cache key
    cache_key = (round(lat, 2), round(lon, 2))
    if cache_key in _geocode_cache:
        return _geocode_cache[cache_key]

    # Enforce Nominatim rate limit
    elapsed = time.monotonic() - _last_request_time
    if elapsed < _RATE_LIMIT_SECONDS:
        time.sleep(_RATE_LIMIT_SECONDS - elapsed)

    try:
        resp = requests.get(
            _NOMINATIM_URL,
            params={"lat": lat, "lon": lon, "format": "json", "zoom": 10},
            headers={"User-Agent": _USER_AGENT},
            timeout=10,
        )
        _last_request_time = time.monotonic()

        if resp.status_code != 200:
            _geocode_cache[cache_key] = None
            return None

        data = resp.json()
        address = data.get("address", {})

        # Build a "City, Country" string from the most specific place name available.
        city = (
            address.get("city")
            or address.get("town")
            or address.get("village")
            or address.get("county")
            or address.get("state")
        )
        country = address.get("country")

        if city and country:
            location = f"{city}, {country}"
        elif city:
            location = city
        elif country:
            location = country
        else:
            # Fall back to the full display name if address fields are absent
            location = data.get("display_name")

        _geocode_cache[cache_key] = location
        return location

    except Exception:
        _last_request_time = time.monotonic()
        _geocode_cache[cache_key] = None
        return None


def get_location_tag(image_source) -> str | None:
    """
    High-level helper: open image_source, extract GPS coords, reverse-geocode,
    and return a lowercased tag string suitable for storing in Firestore
    (e.g. "san francisco, united states"), or None if unavailable.

    Also returns the raw (lat, lon) alongside the tag so callers can store
    the coordinates too if desired.

    Returns: (tag_string | None, lat | None, lon | None)
    """
    lat, lon = extract_gps_coords(image_source)
    if lat is None or lon is None:
        return None, None, None

    location = reverse_geocode(lat, lon)
    tag = location.lower() if location else None
    return tag, lat, lon
