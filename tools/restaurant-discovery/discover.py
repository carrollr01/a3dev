#!/usr/bin/env python3
"""
Discover restaurants in Towson, MD that use Tock, SevenRooms, resOS, Toast
(reservations/online ordering), or Square as their booking/ordering layer.

Pipeline
--------
1. Query OpenStreetMap Overpass for amenity=restaurant|cafe|bar|pub|fast_food
   within a radius of central Towson (no API key required).
2. For each result with a website, fetch the homepage plus a few likely
   reservation/order subpages and fingerprint the HTML for known platform
   signatures (script src, iframe src, anchor href, raw text).
3. Write a CSV with name, address, phone, website, detected platforms,
   direct booking/ordering URLs, and a confidence score.

Usage
-----
    python discover.py --out towson_restaurants.csv
    python discover.py --radius 4500 --workers 24 --out out.csv
    python discover.py --lat 39.4015 --lon -76.6019 --radius 5000

All flags are optional. Defaults target central Towson within ~4 km.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Iterable
from urllib.parse import urljoin, urlparse

import requests

# Towson, MD town center (Towson Circle area).
DEFAULT_LAT = 39.4015
DEFAULT_LON = -76.6019
DEFAULT_RADIUS_M = 4000

OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]

USER_AGENT = (
    "TowsonRestaurantDiscovery/1.0 "
    "(+https://a3dev.org; contact ryan@a3dev.org)"
)

# Pages we'll probe in addition to the homepage. Most restaurant sites put
# the booking widget on one of these.
RESERVATION_PATHS = [
    "/reservations",
    "/reservation",
    "/reserve",
    "/book",
    "/booking",
    "/order",
    "/order-online",
    "/menu",
    "/contact",
]

# Platform fingerprints. Each entry maps a platform name to a list of
# regexes; if any match the fetched HTML the platform is flagged. We keep
# patterns specific enough to avoid false positives from generic tracking.
PLATFORM_PATTERNS: dict[str, list[re.Pattern[str]]] = {
    "Tock": [
        re.compile(r"(?:www\.)?exploretock\.com", re.I),
        re.compile(r"widget\.exploretock\.com", re.I),
        re.compile(r"tock-?widget", re.I),
        re.compile(r"\btock\.com/(?:r|reservations)/", re.I),
    ],
    "SevenRooms": [
        re.compile(r"(?:www\.)?sevenrooms\.com", re.I),
        re.compile(r"widget\.sevenrooms\.com", re.I),
        re.compile(r"sevenrooms-widget", re.I),
        re.compile(r"data-sr-(?:venue|widget)", re.I),
    ],
    "resOS": [
        re.compile(r"(?:app|book|widget)\.resos\.com", re.I),
        re.compile(r"\bresos\.com/(?:book|widget|booking)", re.I),
        re.compile(r"resos-widget", re.I),
    ],
    "Toast": [
        # Toast online ordering / Toast Tables (reservations)
        re.compile(r"(?:order|book|www)\.toasttab\.com", re.I),
        re.compile(r"cdn\.toasttab\.com", re.I),
        re.compile(r"toast-?(?:tables|reservations|booking|order)", re.I),
        re.compile(r"order\.online/[a-z0-9-]+", re.I),  # Toast order.online
    ],
    "Square": [
        re.compile(r"squareup\.com/(?:appointments|gift|store|dashboard)", re.I),
        re.compile(r"\b[a-z0-9-]+\.square\.site", re.I),
        re.compile(r"app\.squareup\.com/appointments", re.I),
        re.compile(r"js\.squareupsandbox\.com|web\.squarecdn\.com", re.I),
    ],
}

# Patterns to extract direct booking/order URLs once a platform is detected.
URL_EXTRACTORS: dict[str, re.Pattern[str]] = {
    "Tock": re.compile(
        r"https?://(?:www\.)?exploretock\.com/[A-Za-z0-9_\-/]+", re.I
    ),
    "SevenRooms": re.compile(
        r"https?://(?:www\.)?sevenrooms\.com/[A-Za-z0-9_\-/]+", re.I
    ),
    "resOS": re.compile(
        r"https?://(?:app|book|widget)\.resos\.com/[A-Za-z0-9_\-/?=&]+", re.I
    ),
    "Toast": re.compile(
        r"https?://(?:order|book|www)\.toasttab\.com/[A-Za-z0-9_\-/?=&]+",
        re.I,
    ),
    "Square": re.compile(
        r"https?://(?:[a-z0-9-]+\.square\.site"
        r"|squareup\.com/(?:appointments|gift|store)"
        r"|app\.squareup\.com/appointments)[A-Za-z0-9_\-/?=&]*",
        re.I,
    ),
}

log = logging.getLogger("discover")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class Restaurant:
    osm_id: str
    name: str
    lat: float
    lon: float
    address: str = ""
    phone: str = ""
    website: str = ""
    cuisine: str = ""
    platforms: list[str] = field(default_factory=list)
    booking_urls: list[str] = field(default_factory=list)
    pages_checked: list[str] = field(default_factory=list)
    confidence: float = 0.0
    error: str = ""


# ---------------------------------------------------------------------------
# Step 1: Discovery via Overpass
# ---------------------------------------------------------------------------


def build_overpass_query(lat: float, lon: float, radius_m: int) -> str:
    """Overpass QL: restaurants/cafes/bars/etc. with a name within radius."""
    amenities = "restaurant|cafe|bar|pub|fast_food|food_court|biergarten"
    return f"""
    [out:json][timeout:60];
    (
      node["amenity"~"^({amenities})$"]["name"](around:{radius_m},{lat},{lon});
      way["amenity"~"^({amenities})$"]["name"](around:{radius_m},{lat},{lon});
      relation["amenity"~"^({amenities})$"]["name"](around:{radius_m},{lat},{lon});
    );
    out center tags;
    """.strip()


def fetch_overpass(query: str) -> dict:
    last_err: Exception | None = None
    for endpoint in OVERPASS_ENDPOINTS:
        try:
            log.info("Overpass: querying %s", endpoint)
            r = requests.post(
                endpoint,
                data={"data": query},
                headers={"User-Agent": USER_AGENT},
                timeout=90,
            )
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.warning("Overpass endpoint failed (%s): %s", endpoint, e)
            last_err = e
            time.sleep(2)
    raise RuntimeError(f"All Overpass endpoints failed: {last_err}")


def _format_address(tags: dict) -> str:
    parts = [
        tags.get("addr:housenumber", ""),
        tags.get("addr:street", ""),
    ]
    line1 = " ".join(p for p in parts if p).strip()
    line2_bits = [
        tags.get("addr:city", ""),
        tags.get("addr:state", ""),
        tags.get("addr:postcode", ""),
    ]
    line2 = ", ".join(p for p in line2_bits if p).strip(", ")
    return ", ".join(p for p in [line1, line2] if p)


def _normalize_url(raw: str) -> str:
    raw = raw.strip()
    if not raw:
        return ""
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    return raw


def parse_overpass(payload: dict) -> list[Restaurant]:
    out: list[Restaurant] = []
    for el in payload.get("elements", []):
        tags = el.get("tags", {}) or {}
        name = tags.get("name", "").strip()
        if not name:
            continue
        if el["type"] == "node":
            lat, lon = el.get("lat"), el.get("lon")
        else:
            center = el.get("center") or {}
            lat, lon = center.get("lat"), center.get("lon")
        if lat is None or lon is None:
            continue
        website = _normalize_url(
            tags.get("website")
            or tags.get("contact:website")
            or tags.get("url")
            or ""
        )
        out.append(
            Restaurant(
                osm_id=f"{el['type']}/{el['id']}",
                name=name,
                lat=lat,
                lon=lon,
                address=_format_address(tags),
                phone=tags.get("phone") or tags.get("contact:phone", ""),
                website=website,
                cuisine=tags.get("cuisine", ""),
            )
        )
    # Deduplicate by (name, address) — OSM sometimes has both a node and a way.
    seen: dict[tuple[str, str], Restaurant] = {}
    for r in out:
        key = (r.name.lower(), r.address.lower())
        if key not in seen or (not seen[key].website and r.website):
            seen[key] = r
    return list(seen.values())


# ---------------------------------------------------------------------------
# Step 2: Fingerprint each restaurant's website
# ---------------------------------------------------------------------------


def _safe_get(
    session: requests.Session, url: str, timeout: float = 10.0
) -> str:
    try:
        resp = session.get(
            url,
            timeout=timeout,
            allow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        )
        ctype = resp.headers.get("content-type", "")
        if resp.status_code >= 400 or "html" not in ctype.lower():
            return ""
        return resp.text[:1_500_000]  # cap to ~1.5 MB
    except requests.RequestException:
        return ""


def _candidate_pages(homepage: str, html: str) -> list[str]:
    """Pick likely reservation/order subpages from the homepage."""
    base = homepage.rstrip("/")
    candidates: set[str] = set()
    # 1. Static guesses
    for path in RESERVATION_PATHS:
        candidates.add(base + path)
    # 2. Anchor hrefs that look reservation/order-y (same-origin only)
    homepage_host = urlparse(homepage).netloc
    for href in re.findall(r'href="([^"#]+)"', html, flags=re.I):
        href_lower = href.lower()
        if not any(
            kw in href_lower
            for kw in (
                "reserv",
                "book",
                "order",
                "menu",
                "table",
            )
        ):
            continue
        absolute = urljoin(homepage, href)
        if urlparse(absolute).netloc == homepage_host:
            candidates.add(absolute.split("#")[0])
    # Cap the number of probed subpages so we don't hammer any one site.
    return list(candidates)[:8]


def detect_platforms(
    html: str,
) -> tuple[list[str], list[str]]:
    """Return (matched_platforms, extracted_booking_urls)."""
    platforms: list[str] = []
    urls: set[str] = set()
    for platform, patterns in PLATFORM_PATTERNS.items():
        if any(p.search(html) for p in patterns):
            platforms.append(platform)
            extractor = URL_EXTRACTORS.get(platform)
            if extractor:
                for m in extractor.finditer(html):
                    urls.add(m.group(0))
    return platforms, sorted(urls)


def fingerprint(restaurant: Restaurant, session: requests.Session) -> Restaurant:
    if not restaurant.website:
        restaurant.error = "no website"
        return restaurant

    pages_to_check = [restaurant.website]
    homepage_html = _safe_get(session, restaurant.website)
    restaurant.pages_checked.append(restaurant.website)

    if not homepage_html:
        restaurant.error = "homepage unreachable"
        # Still try a couple of static guesses in case homepage redirects oddly.
        homepage_html = ""

    pages_to_check.extend(_candidate_pages(restaurant.website, homepage_html))

    found_platforms: set[str] = set()
    found_urls: set[str] = set()
    aggregated_html = homepage_html

    for url in pages_to_check[1:]:
        if url == restaurant.website:
            continue
        sub_html = _safe_get(session, url)
        if not sub_html:
            continue
        restaurant.pages_checked.append(url)
        aggregated_html += "\n" + sub_html

    platforms, urls = detect_platforms(aggregated_html)
    found_platforms.update(platforms)
    found_urls.update(urls)

    restaurant.platforms = sorted(found_platforms)
    restaurant.booking_urls = sorted(found_urls)
    # Confidence: 1.0 if we got a direct booking URL, 0.6 if a fingerprint
    # matched without an explicit URL, 0 otherwise.
    if restaurant.booking_urls:
        restaurant.confidence = 1.0
    elif restaurant.platforms:
        restaurant.confidence = 0.6
    else:
        restaurant.confidence = 0.0
    if restaurant.platforms and restaurant.error == "homepage unreachable":
        # We salvaged a hit from a subpage; clear the error.
        restaurant.error = ""
    return restaurant


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def run(
    lat: float,
    lon: float,
    radius_m: int,
    workers: int,
    out_path: str,
    only_with_platform: bool,
    json_out: str | None,
) -> int:
    log.info(
        "Searching restaurants within %dm of (%.4f, %.4f)…",
        radius_m,
        lat,
        lon,
    )
    payload = fetch_overpass(build_overpass_query(lat, lon, radius_m))
    restaurants = parse_overpass(payload)
    log.info("Found %d named restaurants in OSM.", len(restaurants))

    with_site = [r for r in restaurants if r.website]
    log.info(
        "%d have a website tag; fingerprinting with %d workers…",
        len(with_site),
        workers,
    )

    session = requests.Session()
    completed: list[Restaurant] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(fingerprint, r, session): r for r in with_site
        }
        for i, fut in enumerate(as_completed(futures), 1):
            r = fut.result()
            completed.append(r)
            if r.platforms:
                log.info(
                    "[%d/%d] %s -> %s",
                    i,
                    len(with_site),
                    r.name,
                    ", ".join(r.platforms),
                )
            elif i % 10 == 0:
                log.info("[%d/%d] scanned…", i, len(with_site))

    # Restaurants without a website are still reported (no platform data).
    no_site = [r for r in restaurants if not r.website]
    for r in no_site:
        r.error = r.error or "no website tag in OSM"

    all_results = completed + no_site

    rows: Iterable[Restaurant]
    if only_with_platform:
        rows = [r for r in all_results if r.platforms]
    else:
        rows = all_results

    rows = sorted(rows, key=lambda r: (not r.platforms, r.name.lower()))

    write_csv(rows, out_path)
    log.info("Wrote %d rows to %s", sum(1 for _ in rows), out_path)

    if json_out:
        write_json(rows, json_out)
        log.info("Wrote JSON to %s", json_out)

    summary = summarize(all_results)
    print("\n=== Summary ===")
    for line in summary:
        print(line)
    return 0


def write_csv(rows: Iterable[Restaurant], path: str) -> None:
    fields = [
        "name",
        "address",
        "phone",
        "website",
        "cuisine",
        "platforms",
        "booking_urls",
        "confidence",
        "lat",
        "lon",
        "osm_id",
        "pages_checked",
        "error",
    ]
    rows = list(rows)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow(
                {
                    "name": r.name,
                    "address": r.address,
                    "phone": r.phone,
                    "website": r.website,
                    "cuisine": r.cuisine,
                    "platforms": "; ".join(r.platforms),
                    "booking_urls": "; ".join(r.booking_urls),
                    "confidence": f"{r.confidence:.2f}",
                    "lat": r.lat,
                    "lon": r.lon,
                    "osm_id": r.osm_id,
                    "pages_checked": "; ".join(r.pages_checked),
                    "error": r.error,
                }
            )


def write_json(rows: Iterable[Restaurant], path: str) -> None:
    rows = list(rows)
    payload = [r.__dict__ for r in rows]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


def summarize(rows: list[Restaurant]) -> list[str]:
    total = len(rows)
    with_site = sum(1 for r in rows if r.website)
    counts: dict[str, int] = {}
    for r in rows:
        for p in r.platforms:
            counts[p] = counts.get(p, 0) + 1
    lines = [
        f"Restaurants found:            {total}",
        f"  with website tag:           {with_site}",
        f"  with detected platform:     {sum(1 for r in rows if r.platforms)}",
        "Platform breakdown:",
    ]
    for p in ("Tock", "SevenRooms", "resOS", "Toast", "Square"):
        lines.append(f"  {p:<12} {counts.get(p, 0)}")
    return lines


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--lat", type=float, default=DEFAULT_LAT)
    ap.add_argument("--lon", type=float, default=DEFAULT_LON)
    ap.add_argument(
        "--radius",
        type=int,
        default=DEFAULT_RADIUS_M,
        help="Search radius in meters (default 4000).",
    )
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument(
        "--out",
        default="towson_restaurants.csv",
        help="CSV output path.",
    )
    ap.add_argument(
        "--json-out",
        default=None,
        help="Optional JSON output path.",
    )
    ap.add_argument(
        "--only-with-platform",
        action="store_true",
        help="Only include restaurants where a platform was detected.",
    )
    ap.add_argument(
        "--verbose", "-v", action="store_true", help="Verbose logging."
    )
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    return run(
        lat=args.lat,
        lon=args.lon,
        radius_m=args.radius,
        workers=args.workers,
        out_path=args.out,
        only_with_platform=args.only_with_platform,
        json_out=args.json_out,
    )


if __name__ == "__main__":
    sys.exit(main())
