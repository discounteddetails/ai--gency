"""Google Geocoding + Routes (PRD §9.5). Driving distance/time with a 24h in-process cache."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol

import httpx

from app.services.sheets import UpstreamError

METERS_PER_MILE = 1609.344


class AddressNotFound(Exception):
    pass


@dataclass
class Drive:
    miles: float
    minutes: float


class Maps(Protocol):
    async def geocode(self, address: str) -> str: ...
    async def drive(self, origin: str, destination: str) -> Drive: ...


class GoogleMaps:
    GEOCODE = "https://maps.googleapis.com/maps/api/geocode/json"
    ROUTES = "https://routes.googleapis.com/directions/v2:computeRoutes"
    TTL = 24 * 3600

    def __init__(self, api_key: str, client: httpx.AsyncClient | None = None):
        self._key = api_key
        self._http = client or httpx.AsyncClient(timeout=10)
        self._cache: dict[tuple, tuple[float, object]] = {}

    def _get(self, k):
        hit = self._cache.get(k)
        return hit[1] if hit and time.monotonic() - hit[0] < self.TTL else None

    async def _request(self, method: str, url: str, **kw) -> httpx.Response:
        last: Exception | None = None
        for _ in range(3):
            try:
                r = await self._http.request(method, url, **kw)
                if r.status_code >= 500:
                    last = UpstreamError(f"{url} {r.status_code}")
                    continue
                return r
            except httpx.HTTPError as e:
                last = e
        raise UpstreamError(f"Maps request failed: {last}")

    async def geocode(self, address: str) -> str:
        k = ("geo", address.strip().lower())
        if (hit := self._get(k)) is not None:
            return hit  # type: ignore[return-value]
        r = await self._request("GET", self.GEOCODE,
                                params={"address": address, "components": "country:US", "key": self._key})
        data = r.json()
        status = data.get("status")
        if status == "ZERO_RESULTS" or not data.get("results"):
            raise AddressNotFound(address)
        if status != "OK":
            raise UpstreamError(f"Geocoding status {status}")
        res = data["results"][0]
        if set(res.get("types", [])) & {"administrative_area_level_1", "country"}:
            raise AddressNotFound(f"Too vague: {address}")
        formatted = res["formatted_address"]
        self._cache[k] = (time.monotonic(), formatted)
        return formatted

    async def drive(self, origin: str, destination: str) -> Drive:
        k = ("drive", origin.strip().lower(), destination.strip().lower())
        if (hit := self._get(k)) is not None:
            return hit  # type: ignore[return-value]
        r = await self._request(
            "POST", self.ROUTES,
            headers={"X-Goog-Api-Key": self._key, "X-Goog-FieldMask": "routes.distanceMeters,routes.duration"},
            json={"origin": {"address": origin}, "destination": {"address": destination}, "travelMode": "DRIVE"},
        )
        if r.status_code >= 400:
            raise UpstreamError(f"Routes {r.status_code}: {r.text[:200]}")
        routes = r.json().get("routes") or []
        if not routes:
            raise AddressNotFound(destination)
        d = Drive(miles=routes[0]["distanceMeters"] / METERS_PER_MILE,
                  minutes=float(str(routes[0]["duration"]).rstrip("s")) / 60)
        self._cache[k] = (time.monotonic(), d)
        return d


async def check_service_area(maps: Maps, address: str, settings: dict) -> dict:
    formatted = await maps.geocode(address)
    d = await maps.drive(str(settings.get("service_area_origin", "Kyle, TX 78640")), formatted)
    free = float(settings.get("travel_free_miles", 25))
    import math

    fee = int(float(settings.get("travel_fee_per_mile", 2)) * math.ceil(round(max(0.0, d.miles - free), 6)))
    return {"formatted_address": formatted, "miles": round(d.miles, 1), "drive_minutes": round(d.minutes),
            "in_service_area": d.miles <= free, "travel_fee": fee}
