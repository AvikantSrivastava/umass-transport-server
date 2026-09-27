"""GTFS data fetcher - downloads and caches GTFS data in /tmp."""

import zipfile
from pathlib import Path

import httpx

GTFS_URL = "https://gtfs-cache.admin.umass.edu/gtfs.zip"
DATA_DIR = Path("/tmp/umass-transit")


def get_data_dir() -> Path:
    """Get the data directory, downloading GTFS if needed."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Check if we have the required files
    required_files = ["stops.txt", "trips.txt", "stop_times.txt", "calendar.txt"]
    if all((DATA_DIR / f).exists() for f in required_files):
        return DATA_DIR

    # Download and extract
    print("Downloading GTFS data...")
    zip_path = DATA_DIR / "gtfs.zip"

    with httpx.Client(timeout=30.0, follow_redirects=True) as client:
        response = client.get(GTFS_URL)
        response.raise_for_status()
        zip_path.write_bytes(response.content)

    print("Extracting GTFS data...")
    with zipfile.ZipFile(zip_path, 'r') as zf:
        zf.extractall(DATA_DIR)

    zip_path.unlink()  # Clean up zip file
    print(f"GTFS data ready in {DATA_DIR}")

    return DATA_DIR
