"""Separate NWS monitoring with bounded transport, typed records and independent feeds."""

from __future__ import annotations

import copy
import json
import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, NotRequired
from urllib.parse import urlparse

import httpx
from typing_extensions import TypedDict

JsonObject = dict[str, Any]
RecordKind = Literal["observation", "alert", "forecast"]
FeedState = Literal["available", "stale", "unavailable"]

NWS_BASE_URL = "https://api.weather.gov"
MAX_RESPONSE_BYTES = 2_000_000
MAX_CACHE_ENTRIES = 64
MAX_CACHE_TTL_SECONDS = 300
MAX_FORECAST_PERIODS = 14
DEFAULT_OBSERVATION_AGE_SECONDS = 3600
DEFAULT_FORECAST_AGE_SECONDS = 21600
HEAT_EVENTS = frozenset(
    {
        "Heat Advisory",
        "Excessive Heat Watch",
        "Excessive Heat Warning",
        "Extreme Heat Watch",
        "Extreme Heat Warning",
    }
)
_STALE_FLAGS = frozenset({"stale", "expired"})
_UNAVAILABLE_FLAGS = frozenset(
    {
        "missing_issue_time",
        "future_issue_time",
        "missing_temperature",
        "missing_validity",
    }
)
_PROVIDER_ERRORS = (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError)


class MonitoringRecord(TypedDict):
    kind: RecordKind
    observed_or_issued_at: str | None
    valid_from: str | None
    valid_until: str | None
    retrieved_at: str
    source: str
    quality_flags: list[str]
    data: JsonObject


class FeedResult(TypedDict):
    state: FeedState
    records: list[MonitoringRecord]
    retrieved_at: str | None
    source: NotRequired[str]
    attempted_at: NotRequired[str]
    reason: NotRequired[str]


class MonitoringSnapshot(TypedDict):
    historical_score_affected: bool
    point: dict[str, float]
    notice: str
    heatrisk: dict[str, str]
    observations: FeedResult
    alerts: FeedResult
    forecasts: FeedResult


@dataclass(frozen=True)
class _CachedResponse:
    cached_at: float
    retrieved_at: str
    payload: JsonObject


def timestamp(value: str | None) -> datetime | None:
    """Parse provider timestamps without assigning a guessed timezone."""
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Provider timestamp lacks timezone")
    return parsed


def _object(value: object) -> JsonObject:
    if not isinstance(value, dict):
        raise ValueError("Provider value must be a JSON object")
    return value


def _string(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Provider value must be a string")
    return value


def _feed_state(records: list[MonitoringRecord]) -> FeedState:
    flags = {flag for record in records for flag in record["quality_flags"]}
    if flags & _STALE_FLAGS:
        return "stale"
    if flags & _UNAVAILABLE_FLAGS:
        return "unavailable"
    return "available"


class NWSMonitor:
    """Keep acquisition separate from freshness interpretation and historical scoring.

    A supplied HTTP client provides deterministic offline tests. Successful cached payloads
    preserve retrieval time and are copied so callers cannot mutate future responses.
    """

    def __init__(
        self,
        client: httpx.Client | None = None,
        ttl: float = 120,
        max_entries: int = MAX_CACHE_ENTRIES,
    ) -> None:
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(5, connect=3),
            headers={
                "User-Agent": os.getenv("HRM_NWS_USER_AGENT", "human-resilience-map/0.1"),
                "Accept": "application/geo+json",
            },
            follow_redirects=False,
        )
        self.ttl = min(MAX_CACHE_TTL_SECONDS, max(1, ttl))
        self.max_entries = min(MAX_CACHE_ENTRIES, max(1, max_entries))
        self.cache: OrderedDict[str, _CachedResponse] = OrderedDict()
        self.lock = threading.Lock()

    def get(self, url: str) -> tuple[JsonObject, str]:
        """Fetch a bounded same-provider JSON object, caching successful responses only."""
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.netloc != "api.weather.gov":
            raise ValueError("Untrusted provider link")
        # Serialize fetches to bound outbound load and prevent duplicate cache fills.
        with self.lock:
            entry = self.cache.get(url)
            if entry and time.monotonic() - entry.cached_at < self.ttl:
                self.cache.move_to_end(url)
                return copy.deepcopy(entry.payload), entry.retrieved_at
            with self.client.stream("GET", url) as response:
                response.raise_for_status()
                chunks = bytearray()
                for chunk in response.iter_bytes():
                    chunks.extend(chunk)
                    if len(chunks) > MAX_RESPONSE_BYTES:
                        raise ValueError("Provider response exceeds 2 MB")
                data = _object(json.loads(chunks))
            retrieved = datetime.now(UTC).isoformat()
            self.cache[url] = _CachedResponse(time.monotonic(), retrieved, data)
            self.cache.move_to_end(url)
            while len(self.cache) > self.max_entries:
                self.cache.popitem(last=False)
            return copy.deepcopy(data), retrieved

    @staticmethod
    def record(
        kind: RecordKind,
        props: JsonObject,
        source: str,
        retrieved: str,
        max_age: int,
        now: datetime,
    ) -> MonitoringRecord:
        """Preserve provider values and explicitly describe missing or stale evidence."""
        issued_key = {"observation": "timestamp", "alert": "sent", "forecast": "updated"}[kind]
        issued = props.get(issued_key)
        start = props.get("effective") if kind == "alert" else props.get("startTime")
        end = (
            (props.get("ends") or props.get("expires")) if kind == "alert" else props.get("endTime")
        )
        flags: list[str] = []
        when = timestamp(issued)
        if when is None:
            flags.append("missing_issue_time")
        elif when > now:
            flags.append("future_issue_time")
        elif kind != "alert" and (now - when).total_seconds() > max_age:
            flags.append("stale")
        expiry = timestamp(end)
        if expiry is not None and expiry <= now:
            flags.append("expired")
        if kind in {"alert", "forecast"} and (not start or not end):
            flags.append("missing_validity")
        if kind == "observation":
            temperature = _object(props.get("temperature") or {})
            if temperature.get("value") is None:
                flags.append("missing_temperature")
            if temperature.get("qualityControl"):
                flags.append("provider_qc:" + str(temperature["qualityControl"]))
        return {
            "kind": kind,
            "observed_or_issued_at": issued,
            "valid_from": start,
            "valid_until": end,
            "retrieved_at": retrieved,
            "source": source,
            "quality_flags": flags,
            "data": props,
        }

    def _observations(self, properties: JsonObject, max_age: int, now: datetime) -> FeedResult:
        stations, _ = self.get(_string(properties["observationStations"]))
        station = _string(stations["features"][0]["id"])
        url = station.rstrip("/") + "/observations/latest"
        data, retrieved = self.get(url)
        records = [
            self.record("observation", _object(data["properties"]), url, retrieved, max_age, now)
        ]
        return self._available_feed(records, url, retrieved)

    def _alerts(self, lat: float, lon: float, max_age: int, now: datetime) -> FeedResult:
        url = f"{NWS_BASE_URL}/alerts/active?point={lat:.4f},{lon:.4f}"
        data, retrieved = self.get(url)
        records = []
        for feature in data["features"]:
            props = _object(feature["properties"])
            if props.get("event") in HEAT_EVENTS:
                records.append(self.record("alert", props, url, retrieved, max_age, now))
        # Expired/unknown-validity alerts remain flagged, never treated as an all-clear.
        return self._available_feed(records, url, retrieved)

    def _forecasts(self, properties: JsonObject, max_age: int, now: datetime) -> FeedResult:
        url = _string(properties["forecast"])
        data, retrieved = self.get(url)
        props = _object(data["properties"])
        records = [
            self.record(
                "forecast",
                {**_object(period), "updated": props.get("updated")},
                url,
                retrieved,
                max_age,
                now,
            )
            for period in props["periods"][:MAX_FORECAST_PERIODS]
        ]
        if not records:
            raise ValueError("Empty forecast")
        return self._available_feed(records, url, retrieved)

    @staticmethod
    def _available_feed(records: list[MonitoringRecord], url: str, retrieved: str) -> FeedResult:
        return {
            "state": _feed_state(records),
            "records": records,
            "retrieved_at": retrieved,
            "source": url,
        }

    @staticmethod
    def _unavailable_feed(now: datetime) -> FeedResult:
        return {
            "state": "unavailable",
            "records": [],
            "retrieved_at": None,
            "attempted_at": now.isoformat(),
            "reason": "Provider failed, incomplete or invalid response",
        }

    def read(
        self,
        lat: float,
        lon: float,
        observation_age: int = DEFAULT_OBSERVATION_AGE_SECONDS,
        forecast_age: int = DEFAULT_FORECAST_AGE_SECONDS,
    ) -> MonitoringSnapshot:
        """Evaluate independent feeds: one outage does not suppress other valid feeds."""
        now = datetime.now(UTC)
        properties = None
        try:
            point, _ = self.get(f"{NWS_BASE_URL}/points/{lat:.4f},{lon:.4f}")
            properties = _object(point["properties"])
        except _PROVIDER_ERRORS:
            properties = None
        try:
            observations = (
                self._observations(properties, observation_age, now)
                if properties is not None
                else self._unavailable_feed(now)
            )
        except _PROVIDER_ERRORS:
            observations = self._unavailable_feed(now)
        try:
            alerts = self._alerts(lat, lon, forecast_age, now)
        except _PROVIDER_ERRORS:
            alerts = self._unavailable_feed(now)
        try:
            forecasts = (
                self._forecasts(properties, forecast_age, now)
                if properties is not None
                else self._unavailable_feed(now)
            )
        except _PROVIDER_ERRORS:
            forecasts = self._unavailable_feed(now)
        return {
            "historical_score_affected": False,
            "point": {"lat": lat, "lon": lon},
            "notice": "Point weather/station context, not full selected-area coverage. Missing/stale data must never imply safe conditions.",
            "heatrisk": {
                "state": "unavailable",
                "reason": "Official download interface assessment documented; connector not enabled.",
            },
            "observations": observations,
            "alerts": alerts,
            "forecasts": forecasts,
        }


monitor = NWSMonitor()
