import gzip
import json
import os
import re
import sqlite3
import tarfile
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Tuple

import requests


LAT_MIN, LAT_MAX = 43.5, 48.3
LON_MIN, LON_MAX = 20.2, 31.5

DB_NAME = "romania_black_sea_adsb.db"
GITHUB_REPO = "adsblol/globe_history_2026"
GITHUB_API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases"

ARCHIVE_SPLIT_RE = re.compile(r"^(?P<base>.+\.tar)\.(?P<part>[a-z]{2})$", re.IGNORECASE)


def setup_database() -> None:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS flights (
            timestamp INTEGER,
            hex TEXT,
            flight TEXT,
            lat REAL,
            lon REAL,
            alt_baro INTEGER,
            gs REAL,
            track REAL
        )
        """
    )
    cursor.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_flight_row ON flights(timestamp, hex, flight, lat, lon, alt_baro, gs, track);"
    )
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_time ON flights(timestamp);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_hex ON flights(hex);")
    conn.commit()
    conn.close()


def _github_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "Accept": "application/vnd.github+json",
            "User-Agent": "ROSPIN-flight-importer/1.0",
        }
    )
    token = os.getenv("GITHUB_TOKEN")
    if token:
        session.headers["Authorization"] = f"Bearer {token}"
    return session


def _http_get_json(session: requests.Session, url: str, params: Dict[str, Any] | None = None) -> Any:
    response = session.get(url, params=params, timeout=60)
    response.raise_for_status()
    return response.json()


def _download_url_to_path(session: requests.Session, url: str, target_path: str) -> None:
    with session.get(url, stream=True, timeout=120) as response:
        response.raise_for_status()
        with open(target_path, "wb") as handle:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    handle.write(chunk)


def _parse_github_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _is_archive_asset(name: str) -> bool:
    lower_name = name.lower()
    return (
        lower_name.endswith(".tar")
        or lower_name.endswith(".tar.gz")
        or lower_name.endswith(".tgz")
        or ARCHIVE_SPLIT_RE.match(lower_name) is not None
    )


def _combine_split_assets(assets: List[Dict[str, Any]], session: requests.Session, target_path: str) -> None:
    ordered_assets = sorted(
        assets,
        key=lambda asset: ARCHIVE_SPLIT_RE.match(asset["name"].lower()).group("part"),
    )

    with open(target_path, "wb") as output_handle:
        for asset in ordered_assets:
            with session.get(asset["browser_download_url"], stream=True, timeout=120) as response:
                response.raise_for_status()
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        output_handle.write(chunk)


def _extract_points_from_trace(trace_data: Dict[str, Any]) -> List[Tuple[Any, ...]]:
    rows: List[Tuple[Any, ...]] = []
    base_timestamp = int(trace_data.get("now", 0) or 0)
    hex_code = (trace_data.get("hex") or "").strip()
    flight = (trace_data.get("flight") or "").strip()

    for point in trace_data.get("trace", []):
        if len(point) < 6:
            continue

        try:
            offset = int(point[0] or 0)
            lat = float(point[1])
            lon = float(point[2])
        except (TypeError, ValueError):
            continue

        if not (LAT_MIN <= lat <= LAT_MAX and LON_MIN <= lon <= LON_MAX):
            continue

        rows.append(
            (
                base_timestamp + offset,
                hex_code,
                flight,
                lat,
                lon,
                point[3] if len(point) > 3 else None,
                point[4] if len(point) > 4 else None,
                point[5] if len(point) > 5 else None,
            )
        )

    return rows


def _process_archive_file(archive_path: str, conn: sqlite3.Connection, label: str) -> None:
    cursor = conn.cursor()
    batch: List[Tuple[Any, ...]] = []

    with tarfile.open(archive_path, mode="r:*") as tar:
        for member in tar.getmembers():
            if not member.isfile() or not member.name.endswith(".json.gz"):
                continue

            extracted = tar.extractfile(member)
            if extracted is None:
                continue

            with gzip.GzipFile(fileobj=extracted) as gz_handle:
                trace_data = json.load(gz_handle)

            batch.extend(_extract_points_from_trace(trace_data))

            if len(batch) >= 10000:
                cursor.executemany(
                    "INSERT OR IGNORE INTO flights VALUES (?,?,?,?,?,?,?,?)",
                    batch,
                )
                conn.commit()
                batch.clear()

    if batch:
        cursor.executemany(
            "INSERT OR IGNORE INTO flights VALUES (?,?,?,?,?,?,?,?)",
            batch,
        )
        conn.commit()

    print(f"[{label}] processed successfully")


def _recent_releases(session: requests.Session, days: int = 7) -> List[Dict[str, Any]]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    releases: List[Dict[str, Any]] = []
    page = 1

    while True:
        page_releases = _http_get_json(session, GITHUB_API_URL, params={"per_page": 100, "page": page})

        if not page_releases:
            break

        for release in page_releases:
            published_at = release.get("published_at")
            if not published_at:
                continue

            published_dt = _parse_github_datetime(published_at)
            if published_dt < cutoff:
                return releases

            releases.append(release)

        page += 1

    return releases


def _release_archive_groups(release: Dict[str, Any]) -> List[Tuple[str, List[Dict[str, Any]]]]:
    grouped_assets: Dict[str, List[Dict[str, Any]]] = {}

    for asset in release.get("assets", []):
        asset_name = asset.get("name", "")
        if not asset_name or not _is_archive_asset(asset_name):
            continue

        split_match = ARCHIVE_SPLIT_RE.match(asset_name.lower())
        base_name = split_match.group("base") if split_match else asset_name
        grouped_assets.setdefault(base_name, []).append(asset)

    return list(grouped_assets.items())


def process_recent_releases(days: int = 7) -> None:
    setup_database()
    session = _github_session()
    releases = _recent_releases(session, days=days)

    if not releases:
        print(f"No releases found in the last {days} days.")
        return

    conn = sqlite3.connect(DB_NAME)
    try:
        for release in releases:
            release_name = release.get("name") or release.get("tag_name") or "unknown-release"
            published_at = release.get("published_at", "unknown-time")
            archive_groups = _release_archive_groups(release)

            if not archive_groups:
                print(f"[{release_name}] no archive assets found")
                continue

            for archive_name, assets in archive_groups:
                with tempfile.NamedTemporaryFile(delete=False, suffix=".tar") as temp_file:
                    temp_path = temp_file.name

                try:
                    if len(assets) == 1 and not ARCHIVE_SPLIT_RE.match(assets[0]["name"].lower()):
                        _download_url_to_path(session, assets[0]["browser_download_url"], temp_path)
                    else:
                        _combine_split_assets(assets, session, temp_path)

                    label = f"{published_at} | {release_name} | {archive_name}"
                    _process_archive_file(temp_path, conn, label)
                finally:
                    try:
                        os.remove(temp_path)
                    except OSError:
                        pass
    finally:
        conn.close()


def run_past_week_job() -> None:
    process_recent_releases(days=7)


if __name__ == "__main__":
    run_past_week_job()