"""Departure calculation combining static and realtime data."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from .gtfs_loader import GTFSData, gtfs_time_to_datetime
from .realtime import RealtimeData


@dataclass
class Departure:
    """A scheduled or realtime departure."""
    trip_id: str
    route_id: str
    headsign: str
    origin_stop_id: str
    origin_stop_name: str
    dest_stop_id: str
    dest_stop_name: str
    scheduled_departure: datetime
    scheduled_arrival: datetime
    predicted_departure: datetime | None  # None if no realtime
    predicted_arrival: datetime | None
    is_live: bool


def _get_departures_for_service_date(
    gtfs: GTFSData,
    realtime: RealtimeData | None,
    route_id: str,
    origin_stop_id: str,
    dest_stop_id: str,
    direction_id: int,
    service_date: date,
    query_time: datetime,
) -> list[Departure]:
    """Get departures for a specific service date."""
    active_services = gtfs.get_active_services(service_date)
    departures = []

    for trip_id, trip in gtfs.trips.items():
        # Filter by route and direction
        if trip.route_id != route_id:
            continue
        if trip.direction_id != direction_id:
            continue
        if trip.service_id not in active_services:
            continue

        stop_times = gtfs.stop_times.get(trip_id, [])
        if not stop_times:
            continue

        # Find origin and destination in stop sequence
        origin_st = None
        dest_st = None
        for st in stop_times:
            if st.stop_id == origin_stop_id:
                origin_st = st
            elif st.stop_id == dest_stop_id and origin_st is not None:
                # Destination must come after origin
                dest_st = st
                break

        if origin_st is None or dest_st is None:
            continue

        # Convert to actual times
        scheduled_dep = gtfs_time_to_datetime(origin_st.departure_time, service_date)
        scheduled_arr = gtfs_time_to_datetime(dest_st.arrival_time, service_date)

        # Skip if already departed (with some buffer for realtime delays)
        if scheduled_dep < query_time - timedelta(minutes=5):
            continue

        # Check for realtime updates
        predicted_dep = None
        predicted_arr = None
        is_live = False

        if realtime and trip_id in realtime.trip_updates:
            update = realtime.trip_updates[trip_id]
            is_live = True

            # Get origin stop update
            if origin_stop_id in update.stop_times:
                stu = update.stop_times[origin_stop_id]
                if stu.departure_time:
                    predicted_dep = datetime.fromtimestamp(stu.departure_time)
                elif stu.departure_delay:
                    predicted_dep = scheduled_dep + timedelta(seconds=stu.departure_delay)

            # Get destination stop update
            if dest_stop_id in update.stop_times:
                stu = update.stop_times[dest_stop_id]
                if stu.arrival_time:
                    predicted_arr = datetime.fromtimestamp(stu.arrival_time)
                elif stu.arrival_delay:
                    predicted_arr = scheduled_arr + timedelta(seconds=stu.arrival_delay)

        departures.append(Departure(
            trip_id=trip_id,
            route_id=route_id,
            headsign=trip.headsign,
            origin_stop_id=origin_stop_id,
            origin_stop_name=gtfs.stops[origin_stop_id].stop_name,
            dest_stop_id=dest_stop_id,
            dest_stop_name=gtfs.stops[dest_stop_id].stop_name,
            scheduled_departure=scheduled_dep,
            scheduled_arrival=scheduled_arr,
            predicted_departure=predicted_dep,
            predicted_arrival=predicted_arr,
            is_live=is_live,
        ))

    return departures


def get_departures(
    gtfs: GTFSData,
    realtime: RealtimeData | None,
    route_id: str,
    origin_stop_id: str,
    dest_stop_id: str,
    direction_id: int,
    query_time: datetime | None = None,
) -> list[Departure]:
    """Get upcoming departures from origin to destination.

    Args:
        gtfs: Static GTFS data
        realtime: Realtime data (optional)
        route_id: Route to filter by
        origin_stop_id: Starting stop
        dest_stop_id: Ending stop
        direction_id: Direction to filter (0 or 1)
        query_time: Time to query from (defaults to now)

    Returns:
        List of departures sorted by scheduled departure time
    """
    if query_time is None:
        query_time = datetime.now()

    departures = []

    # If before 3am, check both yesterday's late-night service AND today's service
    if query_time.hour < 3:
        yesterday = query_time.date() - timedelta(days=1)
        today = query_time.date()

        # Get late-night trips from yesterday's service (times > 24:00)
        departures.extend(_get_departures_for_service_date(
            gtfs, realtime, route_id, origin_stop_id, dest_stop_id,
            direction_id, yesterday, query_time
        ))

        # Also get today's service trips
        departures.extend(_get_departures_for_service_date(
            gtfs, realtime, route_id, origin_stop_id, dest_stop_id,
            direction_id, today, query_time
        ))
    else:
        # Normal case: just use today's service
        departures.extend(_get_departures_for_service_date(
            gtfs, realtime, route_id, origin_stop_id, dest_stop_id,
            direction_id, query_time.date(), query_time
        ))

    # Sort by effective departure time (predicted if available, else scheduled)
    departures.sort(key=lambda d: d.predicted_departure or d.scheduled_departure)

    return departures


def format_minutes_until(dt: datetime, now: datetime) -> str:
    """Format time difference as minutes until."""
    diff = dt - now
    minutes = int(diff.total_seconds() / 60)

    if minutes < 0:
        return "Due"
    elif minutes == 0:
        return "Now"
    elif minutes < 60:
        return f"{minutes} min"
    else:
        hours = minutes // 60
        mins = minutes % 60
        if mins == 0:
            return f"{hours}h"
        return f"{hours}h {mins}m"
