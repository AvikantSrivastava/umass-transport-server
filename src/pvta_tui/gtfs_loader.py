"""GTFS static data loader."""

import csv
import pickle
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import NamedTuple


class Stop(NamedTuple):
    stop_id: str
    stop_name: str
    lat: float
    lon: float


class Trip(NamedTuple):
    trip_id: str
    route_id: str
    service_id: str
    headsign: str
    direction_id: int


class StopTime(NamedTuple):
    trip_id: str
    stop_id: str
    arrival_time: str  # HH:MM:SS, can be >24:00:00
    departure_time: str
    stop_sequence: int


class Service(NamedTuple):
    service_id: str
    days: tuple[bool, ...]  # mon-sun
    start_date: date
    end_date: date


@dataclass
class GTFSData:
    """Container for loaded GTFS data."""
    stops: dict[str, Stop]
    trips: dict[str, Trip]
    stop_times: dict[str, list[StopTime]]  # trip_id -> sorted stop_times
    services: dict[str, Service]
    service_exceptions: dict[str, dict[date, int]]  # service_id -> {date: type}

    def get_active_services(self, query_date: date) -> set[str]:
        """Get service_ids active on a given date."""
        day_of_week = query_date.weekday()  # 0=Monday
        active = set()

        for service_id, service in self.services.items():
            if service.start_date <= query_date <= service.end_date:
                if service.days[day_of_week]:
                    active.add(service_id)

        # Apply exceptions
        for service_id, exceptions in self.service_exceptions.items():
            if query_date in exceptions:
                exc_type = exceptions[query_date]
                if exc_type == 1:  # Added
                    active.add(service_id)
                elif exc_type == 2:  # Removed
                    active.discard(service_id)

        return active

    def get_route_stops(self, route_id: str) -> list[Stop]:
        """Get stops served by a route, in travel order.

        Walks the longest trip in each direction (0 first, then 1), so stops
        appear in the order a rider would pass them.
        """
        longest: dict[int, list[StopTime]] = {}
        for trip_id, trip in self.trips.items():
            if trip.route_id != route_id:
                continue
            sts = self.stop_times.get(trip_id, [])
            if len(sts) > len(longest.get(trip.direction_id, [])):
                longest[trip.direction_id] = sts

        ordered: list[Stop] = []
        seen: set[str] = set()
        for direction_id in sorted(longest):
            for st in longest[direction_id]:
                if st.stop_id not in seen and st.stop_id in self.stops:
                    seen.add(st.stop_id)
                    ordered.append(self.stops[st.stop_id])
        return ordered


def parse_gtfs_time(time_str: str) -> timedelta:
    """Parse GTFS time (can exceed 24:00:00) to timedelta from midnight."""
    h, m, s = map(int, time_str.strip().split(':'))
    return timedelta(hours=h, minutes=m, seconds=s)


def gtfs_time_to_datetime(time_str: str, service_date: date) -> datetime:
    """Convert GTFS time to actual datetime, handling times > 24:00."""
    td = parse_gtfs_time(time_str)
    midnight = datetime.combine(service_date, datetime.min.time())
    return midnight + td


def load_gtfs(data_dir: Path) -> GTFSData:
    """Load GTFS static data from extracted files."""
    # Load stops
    stops = {}
    with open(data_dir / 'stops.txt', newline='') as f:
        for row in csv.DictReader(f):
            stops[row['stop_id']] = Stop(
                stop_id=row['stop_id'],
                stop_name=row['stop_name'],
                lat=float(row['stop_lat']),
                lon=float(row['stop_lon']),
            )

    # Load trips
    trips = {}
    with open(data_dir / 'trips.txt', newline='') as f:
        for row in csv.DictReader(f):
            trips[row['trip_id']] = Trip(
                trip_id=row['trip_id'],
                route_id=row['route_id'],
                service_id=row['service_id'],
                headsign=row.get('trip_headsign', ''),
                direction_id=int(row.get('direction_id', 0)),
            )

    # Load stop_times grouped by trip
    stop_times: dict[str, list[StopTime]] = {}
    with open(data_dir / 'stop_times.txt', newline='') as f:
        for row in csv.DictReader(f):
            trip_id = row['trip_id']
            st = StopTime(
                trip_id=trip_id,
                stop_id=row['stop_id'],
                arrival_time=row['arrival_time'],
                departure_time=row['departure_time'],
                stop_sequence=int(row['stop_sequence']),
            )
            stop_times.setdefault(trip_id, []).append(st)

    # Sort by stop_sequence
    for trip_id in stop_times:
        stop_times[trip_id].sort(key=lambda x: x.stop_sequence)

    # Load calendar
    services = {}
    with open(data_dir / 'calendar.txt', newline='') as f:
        for row in csv.DictReader(f):
            days = (
                row['monday'] == '1',
                row['tuesday'] == '1',
                row['wednesday'] == '1',
                row['thursday'] == '1',
                row['friday'] == '1',
                row['saturday'] == '1',
                row['sunday'] == '1',
            )
            services[row['service_id']] = Service(
                service_id=row['service_id'],
                days=days,
                start_date=date.fromisoformat(row['start_date']),
                end_date=date.fromisoformat(row['end_date']),
            )

    # Load calendar_dates (exceptions)
    service_exceptions: dict[str, dict[date, int]] = {}
    with open(data_dir / 'calendar_dates.txt', newline='') as f:
        for row in csv.DictReader(f):
            service_id = row['service_id']
            exc_date = date.fromisoformat(row['date'])
            exc_type = int(row['exception_type'])
            service_exceptions.setdefault(service_id, {})[exc_date] = exc_type

    return GTFSData(
        stops=stops,
        trips=trips,
        stop_times=stop_times,
        services=services,
        service_exceptions=service_exceptions,
    )


def save_cache(data: GTFSData, cache_path: Path) -> None:
    """Save GTFS data to pickle cache."""
    with open(cache_path, 'wb') as f:
        pickle.dump(data, f)


def load_cache(cache_path: Path) -> GTFSData | None:
    """Load GTFS data from pickle cache."""
    if not cache_path.exists():
        return None
    with open(cache_path, 'rb') as f:
        return pickle.load(f)
