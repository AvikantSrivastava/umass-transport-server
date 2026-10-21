"""PVTA Bus TUI Application."""

import asyncio
from datetime import datetime, date, timedelta
from pathlib import Path

from textual import on
from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, Vertical
from textual.widgets import Header, Footer, Static, Input, Label, DataTable, Button, OptionList, Digits
from textual.widgets.option_list import Option
from textual.binding import Binding
from textual.message import Message
from textual.screen import ModalScreen
from pyfiglet import Figlet

from .gtfs_loader import GTFSData, Stop, load_gtfs, load_cache, save_cache
from .realtime import RealtimeData, fetch_realtime, Alert
from .departures import get_departures, format_minutes_until, Departure
from .data_fetcher import get_data_dir, DATA_DIR

# MVP Configuration
ROUTE_ID = "31"
DEFAULT_ORIGIN_STOP_ID = "157"  # The Boulders Apts
DEFAULT_DEST_STOP_ID = "72"     # Studio Arts Building
# Either direction: trips are kept only if destination comes after origin
DIRECTION_ID = None

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


def format_date_label(d: date, include_day: bool = True) -> str:
    """Format date as 'monday, november 6' or 'november 6'."""
    if include_day:
        return d.strftime("%A, %B %d").lower().replace(" 0", " ")
    return d.strftime("%B %d").lower().replace(" 0", " ")


def get_date_label(d: date) -> str:
    """Get display label for a date (Today, Tomorrow, or weekday name)."""
    today = date.today()
    if d == today:
        return f"Today - {format_date_label(d)}"
    elif d == today + timedelta(days=1):
        return f"Tomorrow - {format_date_label(d)}"
    else:
        return format_date_label(d).title()


class DateSelector(Static):
    """Scrollable date selector."""

    NUM_DAYS = 14  # Show 2 weeks of dates

    def __init__(self):
        super().__init__()
        self.selected_date: date = date.today()
        self._dates: list[date] = []

    def compose(self) -> ComposeResult:
        today = date.today()
        self._dates = [today + timedelta(days=i) for i in range(self.NUM_DAYS)]

        options = [
            Option(get_date_label(d), id=d.isoformat())
            for d in self._dates
        ]

        yield OptionList(*options, id="date-picker")

    def on_mount(self) -> None:
        picker = self.query_one("#date-picker", OptionList)
        picker.highlighted = 0  # Start with today highlighted

    def get_selected_date(self) -> date:
        return self.selected_date

    @on(OptionList.OptionSelected, "#date-picker")
    def on_date_selected(self, event: OptionList.OptionSelected) -> None:
        """Handle date selection from the list."""
        if event.option.id:
            self.selected_date = date.fromisoformat(event.option.id)
            self.post_message(self.DateChanged(self.selected_date))

    class DateChanged(Message):
        """Message sent when date selection changes."""
        def __init__(self, selected_date: date) -> None:
            super().__init__()
            self.selected_date = selected_date


class SearchBox(Static):
    """Shows the selected source/destination; press s / d to change them."""

    def compose(self) -> ComposeResult:
        yield Horizontal(
            Input(placeholder="From: The Boulders Apts  [s]", id="from-input", disabled=True),
            Static("→", classes="arrow"),
            Input(placeholder="To: UMass (Studio Arts)  [d]", id="to-input", disabled=True),
            classes="search-row",
        )

    def set_stops(self, origin_name: str, dest_name: str) -> None:
        self.query_one("#from-input", Input).value = f"From: {origin_name}  [s]"
        self.query_one("#to-input", Input).value = f"To: {dest_name}  [d]"


class StopPickerScreen(ModalScreen[str | None]):
    """Large modal window for picking a stop. Returns the stop_id, or None."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("down", "cursor_down", show=False),
        Binding("up", "cursor_up", show=False),
    ]

    def __init__(self, title: str, stops: list[Stop], current_stop_id: str):
        super().__init__()
        self.picker_title = title
        self.stops = stops
        self.current_stop_id = current_stop_id

    def compose(self) -> ComposeResult:
        # Terminals can't change font size, so render the title as ASCII art
        width = max(self.app.size.width - 16, 40)
        banner = Figlet(font="small", width=width).renderText(self.picker_title).rstrip()
        with Vertical(id="picker-dialog"):
            yield Static(banner, id="picker-title")
            yield Input(placeholder="Type to filter stops…", id="picker-filter")
            yield OptionList(id="picker-list")
            yield Static("↑/↓ move   Enter select   Esc cancel", id="picker-hint")

    def on_mount(self) -> None:
        self._populate("")
        self.query_one("#picker-filter", Input).focus()

    def _populate(self, query: str) -> None:
        picker = self.query_one("#picker-list", OptionList)
        picker.clear_options()
        query = query.strip().lower()
        matches = [s for s in self.stops if query in s.stop_name.lower()]
        # Blank lines around each name make every row three lines tall
        picker.add_options(
            Option(
                f"\n  ● {s.stop_name}\n" if s.stop_id == self.current_stop_id else f"\n    {s.stop_name}\n",
                id=s.stop_id,
            )
            for s in matches
        )
        if matches:
            ids = [s.stop_id for s in matches]
            picker.highlighted = ids.index(self.current_stop_id) if self.current_stop_id in ids else 0

    @on(Input.Changed, "#picker-filter")
    def on_filter_changed(self, event: Input.Changed) -> None:
        self._populate(event.value)

    @on(Input.Submitted, "#picker-filter")
    def on_filter_submitted(self) -> None:
        picker = self.query_one("#picker-list", OptionList)
        if picker.highlighted is not None:
            self.dismiss(picker.get_option_at_index(picker.highlighted).id)

    @on(OptionList.OptionSelected, "#picker-list")
    def on_stop_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)

    def action_cursor_down(self) -> None:
        self.query_one("#picker-list", OptionList).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#picker-list", OptionList).action_cursor_up()

    def action_cancel(self) -> None:
        self.dismiss(None)


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


class Clock(Digits):
    """Large digital clock, ticking every second."""

    def on_mount(self) -> None:
        self.tick()
        self.set_interval(1, self.tick)

    def tick(self) -> None:
        self.update(datetime.now().strftime("%H:%M:%S"))


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

    Clock {
        width: 100%;
        text-align: center;
        color: $accent;
        text-style: bold;
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

    DateSelector {
        height: 5;
        padding: 0 1;
    }

    #date-picker {
        height: 100%;
        scrollbar-size: 1 1;
    }

    #date-picker > .option-list--option-highlighted {
        background: $primary;
    }

    #date-picker > .option-list--option-selected {
        background: $primary-darken-1;
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

    StopPickerScreen {
        align: center middle;
        background: $background 60%;
    }

    #picker-dialog {
        width: 90%;
        height: 90%;
        border: thick $primary;
        background: $surface;
        padding: 1 2;
    }

    #picker-title {
        width: 100%;
        height: auto;
        content-align: center middle;
        text-align: center;
        color: $accent;
        text-style: bold;
        margin-bottom: 1;
    }

    #picker-filter {
        margin-bottom: 1;
    }

    #picker-list {
        height: 1fr;
    }

    #picker-list > .option-list--option {
        text-style: bold;
    }

    #picker-hint {
        height: 1;
        text-align: center;
        color: $text-muted;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh", "Refresh"),
        Binding("s", "select_origin", "Origin"),
        Binding("d", "select_destination", "Destination"),
    ]

    def __init__(self):
        super().__init__()
        self.gtfs: GTFSData | None = None
        self.realtime: RealtimeData | None = None
        self._refresh_task: asyncio.Task | None = None
        self.origin_stop_id = DEFAULT_ORIGIN_STOP_ID
        self.dest_stop_id = DEFAULT_DEST_STOP_ID

    def compose(self) -> ComposeResult:
        yield Header()
        yield Clock()
        yield SearchBox()
        yield DateSelector()
        yield AlertBanner()
        yield Container(
            Label(f"Route {ROUTE_ID}: The Boulders → UMass", id="route-header"),
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
            # Fetch data if needed, then load
            data_dir = get_data_dir()
            self.gtfs = load_gtfs(data_dir)
            save_cache(self.gtfs, CACHE_PATH)

        self.update_signboard()

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
                    self.origin_stop_id,
                    self.dest_stop_id,
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

    def update_signboard(self) -> None:
        """Show the selected origin/destination in the header and search box."""
        if not self.gtfs:
            return
        origin = self.gtfs.stops[self.origin_stop_id].stop_name
        dest = self.gtfs.stops[self.dest_stop_id].stop_name
        self.query_one("#route-header", Label).update(f"Route {ROUTE_ID}: {origin} → {dest}")
        self.query_one(SearchBox).set_stops(origin, dest)

    def _open_stop_picker(self, title: str, current_stop_id: str, is_origin: bool) -> None:
        if not self.gtfs or isinstance(self.screen, StopPickerScreen):
            return

        def on_picked(stop_id: str | None) -> None:
            if stop_id is None:
                return
            if is_origin:
                # Picking the current destination as origin swaps the two
                if stop_id == self.dest_stop_id:
                    self.dest_stop_id = self.origin_stop_id
                self.origin_stop_id = stop_id
            else:
                if stop_id == self.origin_stop_id:
                    self.origin_stop_id = self.dest_stop_id
                self.dest_stop_id = stop_id
            self.update_signboard()
            asyncio.create_task(self.do_refresh())

        stops = self.gtfs.get_route_stops(ROUTE_ID)
        self.push_screen(StopPickerScreen(title, stops, current_stop_id), on_picked)

    def action_select_origin(self) -> None:
        self._open_stop_picker("Select Origin", self.origin_stop_id, is_origin=True)

    def action_select_destination(self) -> None:
        self._open_stop_picker("Select Destination", self.dest_stop_id, is_origin=False)

    @on(DateSelector.DateChanged)
    def on_date_changed(self, event: DateSelector.DateChanged) -> None:
        """Refresh when date selection changes."""
        asyncio.create_task(self.do_refresh())


def main():
    app = PVTAApp()
    app.run()


if __name__ == "__main__":
    main()
