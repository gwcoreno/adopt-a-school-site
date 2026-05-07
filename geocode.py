"""
Geocode the Seedling schools CSV -> schools.json.

What it does, in order, for each school:
  1. Try Nominatim (OpenStreetMap) by full address.       <- ~88% of hits
  2. If that misses, try US Census Bureau geocoder.       <- catches some rural addresses
  3. If still missing, try Photon by full address, then by school name + city.
  4. Verify any step-2/3 result by reverse-geocoding its ZIP and checking
     that it matches the expected ZIP. Drop mismatches.
  5. For anything still missing, fall back to the ZIP centroid (~1 mile off).
     These entries are flagged with `"approximate": true`.

How to run:
    python3 geocode.py

Output: schools.json (in the same folder).

All geocoders used are free and require no API key. Nominatim asks for
~1 req/sec, hence the sleep calls. For 124 rows this takes ~3 minutes.
"""
import json
import csv
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
CSV = HERE / "Seedling_Partner_Schools_with_Addresses.csv"
OUT = HERE / "schools.json"

USER_AGENT = "seedling-schools-map/1.0 (one-time geocode)"
DEFAULT_STATUS = "Unfunded"
MIN_SECONDS_BETWEEN_CALLS = 1.0
RETRY_DELAY_ON_429 = 15.0
MAX_429_RETRIES = 5

# Bounding box used to reject obviously-wrong matches (greater Austin area).
LAT_MIN, LAT_MAX = 29.0, 31.5
LNG_MIN, LNG_MAX = -98.5, -96.5


_LAST_HTTP_CALL_AT = 0.0


def http_get_json(url: str, timeout: int = 20) -> dict:
    global _LAST_HTTP_CALL_AT
    retries = 0

    while True:
        # Keep at least one second between every outbound API call.
        elapsed = time.monotonic() - _LAST_HTTP_CALL_AT
        if elapsed < MIN_SECONDS_BETWEEN_CALLS:
            time.sleep(MIN_SECONDS_BETWEEN_CALLS - elapsed)

        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                _LAST_HTTP_CALL_AT = time.monotonic()
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            _LAST_HTTP_CALL_AT = time.monotonic()
            if e.code == 429 and retries < MAX_429_RETRIES:
                retries += 1
                print(f"    429 rate-limited; waiting {RETRY_DELAY_ON_429:.0f}s before retry {retries}/{MAX_429_RETRIES}")
                time.sleep(RETRY_DELAY_ON_429)
                continue
            raise


def in_bounds(lat: float, lng: float) -> bool:
    return LAT_MIN < lat < LAT_MAX and LNG_MIN < lng < LNG_MAX


def nominatim_address(full_address: str):
    q = urllib.parse.quote(full_address)
    data = http_get_json(
        f"https://nominatim.openstreetmap.org/search?q={q}&format=json&limit=1&countrycodes=us"
    )
    if not data:
        return None
    return float(data[0]["lat"]), float(data[0]["lon"])


def nominatim_name(name: str, city: str):
    q = urllib.parse.quote(f"{name} {city} Texas")
    data = http_get_json(
        f"https://nominatim.openstreetmap.org/search?q={q}&format=json&limit=1&countrycodes=us"
    )
    if not data:
        return None
    return float(data[0]["lat"]), float(data[0]["lon"])


def nominatim_reverse_zip(lat: float, lng: float) -> str:
    data = http_get_json(
        f"https://nominatim.openstreetmap.org/reverse?lat={lat}&lon={lng}&format=json"
    )
    return data.get("address", {}).get("postcode", "")


def census(full_address: str):
    q = urllib.parse.quote(full_address)
    data = http_get_json(
        "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"
        f"?address={q}&benchmark=Public_AR_Current&format=json"
    )
    matches = data.get("result", {}).get("addressMatches", [])
    if not matches:
        return None
    c = matches[0]["coordinates"]
    return float(c["y"]), float(c["x"])


def photon_address(full_address: str):
    q = urllib.parse.quote(full_address)
    data = http_get_json(f"https://photon.komoot.io/api/?q={q}&limit=1")
    feats = data.get("features", [])
    if not feats:
        return None
    coords = feats[0]["geometry"]["coordinates"]  # [lng, lat]
    return coords[1], coords[0]


def photon_name(name: str, city: str):
    q = urllib.parse.quote(f"{name} {city} Texas")
    data = http_get_json(f"https://photon.komoot.io/api/?q={q}&limit=1")
    feats = data.get("features", [])
    if not feats:
        return None
    coords = feats[0]["geometry"]["coordinates"]
    return coords[1], coords[0]


def zip_centroid(zip_code: str):
    data = http_get_json(f"https://api.zippopotam.us/us/{zip_code}")
    place = data["places"][0]
    return float(place["latitude"]), float(place["longitude"])


def try_with_zip_check(coords, expected_zip: str) -> bool:
    """Verify a coord pair lands in the expected ZIP. Sleeps 1.1s after."""
    if not coords:
        return False
    lat, lng = coords
    if not in_bounds(lat, lng):
        return False
    try:
        got = nominatim_reverse_zip(lat, lng)
    except Exception:
        got = ""
    time.sleep(1.1)
    return got == expected_zip


def geocode_one(name, street, city, state, zip_code, full_address):
    """Return (lat, lng, approximate_bool, method_str) or raises if it can't find anything."""

    # 1) Nominatim full address — high-quality match, no ZIP check needed.
    try:
        coords = nominatim_address(full_address)
        if coords and in_bounds(*coords):
            return (*coords, False, "nominatim-address")
    except Exception as e:
        print(f"    nominatim-address error: {e}")
    time.sleep(1.1)

    # 2) Census — also high-quality. Verify ZIP for safety.
    try:
        coords = census(full_address)
        if try_with_zip_check(coords, zip_code):
            return (*coords, False, "census")
    except Exception as e:
        print(f"    census error: {e}")
    time.sleep(0.3)

    # 3) Photon by address — looser; ZIP-verify.
    try:
        coords = photon_address(full_address)
        if try_with_zip_check(coords, zip_code):
            return (*coords, False, "photon-address")
    except Exception as e:
        print(f"    photon-address error: {e}")
    time.sleep(0.5)

    # 4) Photon by school name + city — looser still; ZIP-verify.
    try:
        coords = photon_name(name, city)
        if try_with_zip_check(coords, zip_code):
            return (*coords, False, "photon-name")
    except Exception as e:
        print(f"    photon-name error: {e}")
    time.sleep(0.5)

    # 5) ZIP centroid — guaranteed but approximate (~1 mile off).
    coords = zip_centroid(zip_code)
    return (*coords, True, "zip-centroid")


def parse_address(full_address: str):
    """
    Parse an address like:
      "515 Vargas Road, Austin, TX 78741"
    into (street, city, state, zip_code).
    """
    parts = [p.strip() for p in full_address.split(",")]
    if len(parts) < 3:
        raise ValueError(f"Unexpected address format: {full_address!r}")

    street = parts[0]
    city = parts[1]
    state_zip = parts[2]
    m = re.match(r"^([A-Za-z]{2})\s+(\d{5})(?:-\d{4})?$", state_zip)
    if not m:
        raise ValueError(f"Could not parse state/zip from: {full_address!r}")
    state = m.group(1)
    zip_code = m.group(2)
    return street, city, state, zip_code


def normalize_district(raw_district: str) -> str:
    # Input values like "AISD Elementary" -> "AISD".
    return str(raw_district).strip().split()[0]


def main():
    rows = []
    with open(CSV, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            full = (row.get("Address") or "").strip()
            if not full:
                continue
            name = (row.get("School Name") or "").strip()
            district = normalize_district(row.get("District") or "")
            street, city, state, zip_code = parse_address(full)
            rows.append((name, street, city, state, zip_code, district, full))

    schools = []
    for i, row in enumerate(rows, 1):
        name, street, city, state, zip_code, district, full = row
        zip_code = str(zip_code)
        try:
            lat, lng, approx, method = geocode_one(name, street, city, state, zip_code, full)
            entry = {
                "name": name,
                "status": DEFAULT_STATUS,
                "district": district,
                "address": full,
                "lat": lat,
                "lng": lng,
            }
            if approx:
                entry["approximate"] = True
            schools.append(entry)
            tag = "APPROX" if approx else "OK"
            print(f"[{i}/{len(rows)}] {tag:6s} {name}  ({method})")
        except Exception as e:
            print(f"[{i}/{len(rows)}] FAIL   {name}: {e}")

    with open(OUT, "w") as f:
        json.dump({"schools": schools}, f, indent=2)

    approx_count = sum(1 for s in schools if s.get("approximate"))
    print(f"\nWrote {OUT}")
    print(f"Total: {len(schools)}/{len(rows)}  ({approx_count} approximate, {len(schools) - approx_count} exact)")


if __name__ == "__main__":
    main()
