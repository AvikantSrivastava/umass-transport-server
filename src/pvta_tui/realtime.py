"""GTFS-RT realtime feed integration."""

from dataclasses import dataclass
from datetime import datetime

import httpx
from google.transit import gtfs_realtime_pb2

TRIP_UPDATES_URL = "https://gtfs-cache.admin.umass.edu/gtfs-rt/trip-updates"
ALERTS_URL = "https://gtfs-cache.admin.umass.edu/gtfs-rt/alerts"


@dataclass
class StopTimeUpdate:
    """Realtime update for a single stop."""
    stop_id: str
    arrival_time: int | None  # Unix timestamp
    arrival_delay: int  # seconds
    departure_time: int | None
    departure_delay: int


@dataclass
class TripUpdate:
    """Realtime update for a trip."""
    trip_id: str
    route_id: str
    direction_id: int
    stop_times: dict[str, StopTimeUpdate]  # stop_id -> update


@dataclass
class Alert:
    """Service alert."""
    header: str
    description: str
    route_ids: list[str]


@dataclass
class RealtimeData:
    """Container for realtime data."""
    trip_updates: dict[str, TripUpdate]  # trip_id -> update
    alerts: list[Alert]
    timestamp: datetime


async def fetch_trip_updates(client: httpx.AsyncClient) -> dict[str, TripUpdate]:
    """Fetch and parse trip updates feed."""
    response = await client.get(TRIP_UPDATES_URL)
    response.raise_for_status()

    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(response.content)

    updates = {}
    for entity in feed.entity:
        if entity.HasField('trip_update'):
            tu = entity.trip_update
            trip = tu.trip

            stop_times = {}
            for stu in tu.stop_time_update:
                stop_times[stu.stop_id] = StopTimeUpdate(
                    stop_id=stu.stop_id,
                    arrival_time=stu.arrival.time if stu.HasField('arrival') and stu.arrival.time else None,
                    arrival_delay=stu.arrival.delay if stu.HasField('arrival') else 0,
                    departure_time=stu.departure.time if stu.HasField('departure') and stu.departure.time else None,
                    departure_delay=stu.departure.delay if stu.HasField('departure') else 0,
                )

            updates[trip.trip_id] = TripUpdate(
                trip_id=trip.trip_id,
                route_id=trip.route_id,
                direction_id=trip.direction_id,
                stop_times=stop_times,
            )

    return updates


async def fetch_alerts(client: httpx.AsyncClient) -> list[Alert]:
    """Fetch and parse alerts feed."""
    try:
        response = await client.get(ALERTS_URL)
        response.raise_for_status()
    except httpx.HTTPError:
        return []

    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(response.content)

    alerts = []
    for entity in feed.entity:
        if entity.HasField('alert'):
            alert = entity.alert
            route_ids = []
            for selector in alert.informed_entity:
                if selector.route_id:
                    route_ids.append(selector.route_id)

            # Get header and description from translations
            header = ""
            description = ""
            if alert.header_text.translation:
                header = alert.header_text.translation[0].text
            if alert.description_text.translation:
                description = alert.description_text.translation[0].text

            alerts.append(Alert(
                header=header,
                description=description,
                route_ids=route_ids,
            ))

    return alerts


async def fetch_realtime() -> RealtimeData:
    """Fetch all realtime data."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        updates = await fetch_trip_updates(client)
        alerts = await fetch_alerts(client)

    return RealtimeData(
        trip_updates=updates,
        alerts=alerts,
        timestamp=datetime.now(),
    )
