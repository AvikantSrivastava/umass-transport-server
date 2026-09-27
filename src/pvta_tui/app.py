"""PVTA Bus TUI Application."""

import asyncio
from datetime import datetime, date, timedelta
from pathlib import Path

from textual import on
from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, Vertical
from textual.widgets import Header, Footer, Static, Input, Label, DataTable, Button
from textual.binding import Binding
from textual.message import Message

from .gtfs_loader import GTFSData, load_gtfs, load_cache, save_cache
from .realtime import RealtimeData, fetch_realtime, Alert
from .departures import get_departures, format_minutes_until, Departure

# MVP Configuration
ROUTE_ID = "31"
ORIGIN_STOP_ID = "157"  # The Boulders Apts
DEST_STOP_ID = "72"     # Studio Arts Building
DIRECTION_ID = 0        # Sunderland-bound (passes through UMass)

DATA_DIR = Path(__file__).parent.parent.parent / "data"
CACHE_PATH = DATA_DIR / "gtfs_cache.pkl"

REFRESH_INTERVAL = 5  # seconds


class AlertBanner(Static):
    """Banner showing service alerts."""

    def update_alerts(self, alerts: list[Alert], route_id: str) -> None:
        relevant = [a for a in alerts if route_id in a.route_ids or not a.route_ids]
        if relevant:
            text = " | ".join(a.header for a in relevant[:3])
            self.update(f"⚠ {text}")
            self.add_class("visible")
        else:
            self.update("")
            self.remove_class("visible")


def format_date_label(d: date) -> str:
    """Format date as 'month day' (e.g., 'november 6')."""
    return d.strftime("%B %d").lower().replace(" 0", " ")


class DateSelector(Static):
    """Date selector for choosing query date."""

    def __init__(self):
        super().__init__()
        self.selected_date: date = date.today()

    def compose(self) -> ComposeResult:
        today = date.today()
        tomorrow = today + timedelta(days=1)
        yield Horizontal(
            Button(f"Today ({format_date_label(today)})", id="date-today", variant="primary"),
            Button(f"Tomorrow ({format_date_label(tomorrow)})", id="date-tomorrow"),
            Input(placeholder="or enter date: november 6", id="date-input"),
            classes="date-row",
        )

    def get_selected_date(self) -> date:
        return self.selected_date

    def _update_buttons(self, selected_id: str) -> None:
        """Update button variants to show selection."""
        for btn_id in ["date-today", "date-tomorrow"]:
            btn = self.query_one(f"#{btn_id}", Button)
            btn.variant = "primary" if btn_id == selected_id else "default"

    @on(Button.Pressed, "#date-today")
    def select_today(self) -> None:
        self.selected_date = date.today()
        self._update_buttons("date-today")
        self.post_message(self.DateChanged(self.selected_date))

    @on(Button.Pressed, "#date-tomorrow")
    def select_tomorrow(self) -> None:
        self.selected_date = date.today() + timedelta(days=1)
        self._update_buttons("date-tomorrow")
        self.post_message(self.DateChanged(self.selected_date))

    @on(Input.Submitted, "#date-input")
    def parse_custom_date(self, event: Input.Submitted) -> None:
        """Parse custom date input like 'november 6'."""
        text = event.value.strip().lower()
        if not text:
            return

        try:
            # Try parsing "month day" format
            current_year = date.today().year
            parsed = datetime.strptime(f"{text} {current_year}", "%B %d %Y").date()
            # If the date is in the past, assume next year
            if parsed < date.today():
                parsed = parsed.replace(year=current_year + 1)
            self.selected_date = parsed
            self._update_buttons("")  # Deselect both buttons
            self.post_message(self.DateChanged(self.selected_date))
        except ValueError:
            pass  # Invalid date format, ignore

    class DateChanged(Message):
        """Message sent when date selection changes."""
        def __init__(self, selected_date: date) -> None:
            super().__init__()
            self.selected_date = selected_date


class SearchBox(Static):
    """Search input for source/destination (placeholder for v1)."""

    def compose(self) -> ComposeResult:
        yield Horizontal(
            Input(placeholder="From: The Boulders Apts", id="from-input", disabled=True),
            Static("→", classes="arrow"),
            Input(placeholder="To: UMass (Studio Arts)", id="to-input", disabled=True),
            classes="search-row",
        )


class DepartureList(Static):
    """List of upcoming departures."""

    def compose(self) -> ComposeResult:
        yield DataTable(id="departures-table")

    def on_mount(self) -> None:
        table = self.query_one("#departures-table", DataTable)
        table.add_columns("Departs", "Status", "Headsign", "Arrives")
        table.cursor_type = "row"

    def update_departures(self, departures: list[Departure], now: datetime) -> None:
        table = self.query_one("#departures-table", DataTable)
        table.clear()

        if not departures:
            table.add_row("--", "No service", "Check schedule", "--")
            return

        for dep in departures[:10]:  # Show up to 10 departures
            # Departure time
            eff_dep = dep.predicted_departure or dep.scheduled_departure
            dep_mins = format_minutes_until(eff_dep, now)

            # Status badge
            if dep.is_live:
                if dep.predicted_departure:
                    delay = int((dep.predicted_departure - dep.scheduled_departure).total_seconds() / 60)
                    if delay > 2:
                        status = f"🔴 +{delay}m"
                    elif delay < -1:
                        status = f"🟢 {delay}m"
                    else:
                        status = "🟢 Live"
                else:
                    status = "🟢 Live"
            else:
                status = "📅 Sched"

            # Headsign
            headsign = dep.headsign or "Route 31"

            # Arrival time
            eff_arr = dep.predicted_arrival or dep.scheduled_arrival
            arr_time = eff_arr.strftime("%H:%M")

            table.add_row(dep_mins, status, headsign, arr_time)


class StatusBar(Static):
    """Status bar showing last update time."""

    def update_status(self, timestamp: datetime | None, error: str | None = None) -> None:
        if error:
            self.update(f"⚠ {error}")
        elif timestamp:
            self.update(f"Updated: {timestamp.strftime('%H:%M:%S')}")
        else:
            self.update("Loading...")


class PVTAApp(App):
    """PVTA Bus Arrivals TUI."""

    TITLE = "PVTA Bus - Route 31"
    CSS = """
    Screen {
        layout: vertical;
    }

    .search-row {
        height: 3;
        padding: 0 1;
    }

    .search-row Input {
        width: 1fr;
    }

    .search-row .arrow {
        width: 3;
        text-align: center;
        padding-top: 1;
    }

    .date-row {
        height: 3;
        padding: 0 1;
    }

    .date-row Button {
        margin-right: 1;
    }

    .date-row Input {
        width: 1fr;
    }

    AlertBanner {
        height: 0;
        background: $warning;
        color: $text;
        padding: 0 1;
        text-style: bold;
    }

    AlertBanner.visible {
        height: 1;
    }

    #main-container {
        height: 1fr;
        padding: 1;
    }

    #route-header {
        text-align: center;
        text-style: bold;
        background: $primary;
        padding: 0 1;
        margin-bottom: 1;
    }

    #departures-table {
        height: 1fr;
    }

    StatusBar {
        height: 1;
        dock: bottom;
        background: $surface;
        padding: 0 1;
    }

    DataTable > .datatable--header {
        text-style: bold;
        background: $primary-darken-2;
    }

    DataTable > .datatable--cursor {
        background: $primary-darken-1;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh", "Refresh"),
    ]

    def __init__(self):
        super().__init__()
        self.gtfs: GTFSData | None = None
        self.realtime: RealtimeData | None = None
        self._refresh_task: asyncio.Task | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield SearchBox()
        yield DateSelector()
        yield AlertBanner()
        yield Container(
            Label(f"Route 31: The Boulders → UMass", id="route-header"),
            DepartureList(),
            id="main-container",
        )
        yield StatusBar()
        yield Footer()

    async def on_mount(self) -> None:
        # Load GTFS data
        await self.load_gtfs_data()
        # Start refresh loop
        self._refresh_task = asyncio.create_task(self.refresh_loop())

    async def load_gtfs_data(self) -> None:
        """Load GTFS static data."""
        status = self.query_one(StatusBar)

        # Try cache first
        self.gtfs = load_cache(CACHE_PATH)
        if self.gtfs is None:
            status.update_status(None, None)
            self.gtfs = load_gtfs(DATA_DIR)
            save_cache(self.gtfs, CACHE_PATH)

        # Initial refresh
        await self.do_refresh()

    async def refresh_loop(self) -> None:
        """Periodic refresh loop."""
        while True:
            await asyncio.sleep(REFRESH_INTERVAL)
            await self.do_refresh()

    async def do_refresh(self) -> None:
        """Fetch realtime data and update display."""
        status = self.query_one(StatusBar)
        alert_banner = self.query_one(AlertBanner)
        dep_list = self.query_one(DepartureList)
        date_selector = self.query_one(DateSelector)

        try:
            self.realtime = await fetch_realtime()
            now = datetime.now()
            selected_date = date_selector.get_selected_date()

            # Build query time: use selected date with current time
            # For future dates, use start of day (midnight)
            if selected_date == date.today():
                query_time = now
            else:
                query_time = datetime.combine(selected_date, datetime.min.time())

            # Update alerts
            alert_banner.update_alerts(self.realtime.alerts, ROUTE_ID)

            # Get and display departures
            # For future dates, don't use realtime data
            realtime_data = self.realtime if selected_date == date.today() else None

            if self.gtfs:
                departures = get_departures(
                    self.gtfs,
                    realtime_data,
                    ROUTE_ID,
                    ORIGIN_STOP_ID,
                    DEST_STOP_ID,
                    DIRECTION_ID,
                    query_time,
                )
                dep_list.update_departures(departures, query_time)

            status.update_status(self.realtime.timestamp)

        except Exception as e:
            status.update_status(None, str(e)[:50])

    def action_refresh(self) -> None:
        """Manual refresh."""
        asyncio.create_task(self.do_refresh())

    @on(DateSelector.DateChanged)
    def on_date_changed(self, event: DateSelector.DateChanged) -> None:
        """Refresh when date selection changes."""
        asyncio.create_task(self.do_refresh())


def main():
    app = PVTAApp()
    app.run()


if __name__ == "__main__":
    main()
