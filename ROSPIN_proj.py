import gzip
import json
import os
import re
import sqlite3
import tarfile
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple


LAT_MIN, LAT_MAX = 43.0, 48.3
LON_MIN, LON_MAX = 20.2, 32.5
GRID_SIZE_DEGREES = 0.1
LOW_NIC_THRESHOLD = 7
PERSISTENT_LOW_NIC_RATIO = 0.2
MIN_AIRCRAFT_PER_CELL = 3

DB_NAME = "romania_black_sea_adsb.db"
ARCHIVE_DIR = "archives"

ARCHIVE_SPLIT_RE = re.compile(r"^(?P<base>.+\.tar)\.(?P<part>[a-z]{2})$", re.IGNORECASE)


def setup_database() -> None:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS flights (
            archive_date TEXT,
            timestamp INTEGER,
            hex TEXT,
            flight TEXT,
            lat REAL,
            lon REAL,
            alt_baro INTEGER,
            gs REAL,
            track REAL,
            trace_flags INTEGER,
            source_type TEXT,
            nic INTEGER,
            rc REAL,
            nic_baro INTEGER,
            nac_p INTEGER,
            nac_v INTEGER,
            sil INTEGER,
            sil_type TEXT,
            sda INTEGER,
            adsb_version INTEGER,
            ias REAL,
            roll REAL
        )
        """
    )
    existing_columns = {
        row[1] for row in cursor.execute("PRAGMA table_info(flights)").fetchall()
    }
    required_columns = {
        "archive_date": "TEXT",
        "trace_flags": "INTEGER",
        "source_type": "TEXT",
        "nic": "INTEGER",
        "rc": "REAL",
        "nic_baro": "INTEGER",
        "nac_p": "INTEGER",
        "nac_v": "INTEGER",
        "sil": "INTEGER",
        "sil_type": "TEXT",
        "sda": "INTEGER",
        "adsb_version": "INTEGER",
        "ias": "REAL",
        "roll": "REAL",
    }
    for column_name, column_type in required_columns.items():
        if column_name not in existing_columns:
            cursor.execute(f"ALTER TABLE flights ADD COLUMN {column_name} {column_type}")

    for legacy_column in ("alt_geom", "geom_rate"):
        if legacy_column in existing_columns:
            cursor.execute(f"ALTER TABLE flights DROP COLUMN {legacy_column}")

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS processed_archives (
            archive_key TEXT PRIMARY KEY,
            archive_name TEXT NOT NULL,
            processed_at TEXT NOT NULL
        )
        """
    )
    cursor.execute(
        "DROP INDEX IF EXISTS idx_unique_flight_row;"
    )
    cursor.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_flight_row ON flights(archive_date, timestamp, hex, flight, lat, lon, alt_baro, gs, track);"
    )
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_time ON flights(timestamp);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_hex ON flights(hex);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_archive_date ON flights(archive_date);")
    conn.commit()
    conn.close()


def setup_grid_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS aircraft_cell_daily (
            archive_date TEXT NOT NULL,
            grid_row INTEGER NOT NULL,
            grid_col INTEGER NOT NULL,
            cell_lat_min REAL NOT NULL,
            cell_lon_min REAL NOT NULL,
            hex TEXT NOT NULL,
            observation_count INTEGER NOT NULL,
            valid_nic_observations INTEGER NOT NULL,
            low_nic_observations INTEGER NOT NULL,
            low_nic_ratio REAL,
            persistent_low_nic INTEGER NOT NULL,
            adsb_observations INTEGER NOT NULL,
            mlat_observations INTEGER NOT NULL,
            PRIMARY KEY (archive_date, grid_row, grid_col, hex)
        );

        CREATE TABLE IF NOT EXISTS daily_grid (
            archive_date TEXT NOT NULL,
            grid_row INTEGER NOT NULL,
            grid_col INTEGER NOT NULL,
            cell_lat_min REAL NOT NULL,
            cell_lon_min REAL NOT NULL,
            distinct_aircraft INTEGER NOT NULL,
            aircraft_with_nic INTEGER NOT NULL,
            low_nic_aircraft INTEGER NOT NULL,
            low_nic_aircraft_ratio REAL,
            observation_count INTEGER NOT NULL,
            valid_nic_observations INTEGER NOT NULL,
            low_nic_observations INTEGER NOT NULL,
            adsb_observations INTEGER NOT NULL,
            mlat_observations INTEGER NOT NULL,
            sufficient_sample INTEGER NOT NULL,
            PRIMARY KEY (archive_date, grid_row, grid_col)
        );

        CREATE INDEX IF NOT EXISTS idx_aircraft_cell_date
            ON aircraft_cell_daily(archive_date);
        CREATE INDEX IF NOT EXISTS idx_daily_grid_date
            ON daily_grid(archive_date);
        """
    )


def _is_archive_file(name: str) -> bool:
    lower_name = name.lower()
    return (
        lower_name.endswith(".tar")
        or lower_name.endswith(".tar.gz")
        or lower_name.endswith(".tgz")
        or ARCHIVE_SPLIT_RE.match(lower_name) is not None
    )


class _ConcatenatedParts:
    def __init__(self, paths: List[str]) -> None:
        self._handles = [open(path, "rb") for path in paths]
        self._index = 0

    def read(self, size: int = -1) -> bytes:
        if self._index >= len(self._handles):
            return b""

        if size == -1:
            chunks = [handle.read() for handle in self._handles[self._index:]]
            self._index = len(self._handles)
            return b"".join(chunks)

        chunks = []
        remaining = size
        while remaining > 0 and self._index < len(self._handles):
            chunk = self._handles[self._index].read(remaining)
            if chunk:
                chunks.append(chunk)
                remaining -= len(chunk)
            else:
                self._handles[self._index].close()
                self._index += 1
        return b"".join(chunks)

    def close(self) -> None:
        for handle in self._handles[self._index:]:
            handle.close()
        self._index = len(self._handles)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()


def _extract_points_from_trace(
    trace_data: Dict[str, Any], archive_date: str
) -> List[Tuple[Any, ...]]:
    rows: List[Tuple[Any, ...]] = []
    base_timestamp = float(trace_data.get("timestamp", trace_data.get("now", 0)) or 0)
    hex_code = (trace_data.get("hex") or trace_data.get("icao") or "").strip()
    flight = (trace_data.get("flight") or "").strip()

    for point in trace_data.get("trace", []):
        if len(point) < 6:
            continue

        try:
            offset = float(point[0] or 0)
            lat = float(point[1])
            lon = float(point[2])
        except (TypeError, ValueError):
            continue

        if not (LAT_MIN <= lat <= LAT_MAX and LON_MIN <= lon <= LON_MAX):
            continue

        trace_flags = point[6] if len(point) > 6 else None
        metadata = point[8] if len(point) > 8 and isinstance(point[8], dict) else {}
        source_type = point[9] if len(point) > 9 else None

        rows.append(
            (
                archive_date,
                base_timestamp + offset,
                hex_code,
                flight,
                lat,
                lon,
                point[3] if len(point) > 3 else None,
                point[4] if len(point) > 4 else None,
                point[5] if len(point) > 5 else None,
                trace_flags,
                source_type,
                metadata.get("nic"),
                metadata.get("rc"),
                metadata.get("nic_baro"),
                metadata.get("nac_p"),
                metadata.get("nac_v"),
                metadata.get("sil"),
                metadata.get("sil_type"),
                metadata.get("sda"),
                metadata.get("version"),
                point[12] if len(point) > 12 else metadata.get("ias"),
                point[13] if len(point) > 13 else metadata.get("roll"),
            )
        )

    return rows


def _load_trace_objects(gz_handle: Any, member_name: str) -> List[Dict[str, Any]]:
    payload = gz_handle.read()
    if payload.startswith(b"\x1f\x8b"):
        payload = gzip.decompress(payload)
    text = payload.decode("utf-8")
    decoder = json.JSONDecoder()
    objects: List[Dict[str, Any]] = []
    position = 0

    while position < len(text):
        while position < len(text) and text[position].isspace():
            position += 1
        if position >= len(text):
            break

        try:
            trace_data, next_position = decoder.raw_decode(text, position)
        except json.JSONDecodeError as error:
            raise ValueError(
                f"invalid JSON in {member_name} at character {error.pos}"
            ) from error

        if not isinstance(trace_data, dict):
            raise ValueError(f"expected a JSON object in {member_name}")

        objects.append(trace_data)
        position = next_position

    if len(objects) > 1:
        print(
            f"Warning: {member_name} contains {len(objects)} concatenated JSON objects; processing all of them",
            flush=True,
        )

    return objects


def _process_archive_file(
    archive_paths: List[str], conn: sqlite3.Connection, label: str, archive_date: str
) -> None:
    cursor = conn.cursor()
    batch: List[Tuple[Any, ...]] = []

    if len(archive_paths) == 1:
        archive_stream = open(archive_paths[0], "rb")
    else:
        archive_stream = _ConcatenatedParts(archive_paths)

    try:
        with tarfile.open(fileobj=archive_stream, mode="r|*") as tar:
            for member in tar:
                if not member.isfile() or not member.name.startswith("./traces/"):
                    continue
                if not member.name.endswith(".json"):
                    continue

                extracted = tar.extractfile(member)
                if extracted is None:
                    continue

                try:
                    trace_objects = _load_trace_objects(extracted, member.name)
                except (OSError, UnicodeDecodeError, ValueError) as error:
                    print(f"Warning: skipping {member.name}: {error}", flush=True)
                    continue

                for trace_data in trace_objects:
                    batch.extend(_extract_points_from_trace(trace_data, archive_date))

                if len(batch) >= 10000:
                    cursor.executemany(
                        """
                        INSERT OR IGNORE INTO flights
                        (archive_date, timestamp, hex, flight, lat, lon, alt_baro, gs, track,
                         trace_flags, source_type, nic, rc, nic_baro, nac_p, nac_v, sil,
                         sil_type, sda, adsb_version, ias, roll)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        batch,
                    )
                    conn.commit()
                    batch.clear()
    finally:
        archive_stream.close()

    if batch:
        cursor.executemany(
            """
            INSERT OR IGNORE INTO flights
            (archive_date, timestamp, hex, flight, lat, lon, alt_baro, gs, track,
             trace_flags, source_type, nic, rc, nic_baro, nac_p, nac_v, sil,
             sil_type, sda, adsb_version, ias, roll)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            batch,
        )
        conn.commit()

    print(f"[{label}] processed successfully")


def _local_archive_groups(archive_dir: str) -> List[Tuple[str, List[str]]]:
    grouped_files: Dict[str, List[str]] = {}
    for name in os.listdir(archive_dir):
        if not _is_archive_file(name):
            continue

        split_match = ARCHIVE_SPLIT_RE.match(name.lower())
        base_name = split_match.group("base") if split_match else name
        grouped_files.setdefault(base_name, []).append(os.path.join(archive_dir, name))

    return sorted(
        (base_name, sorted(paths)) for base_name, paths in grouped_files.items()
    )


def _archive_key(paths: List[str]) -> str:
    parts = []
    for path in paths:
        stat = os.stat(path)
        parts.append(f"{os.path.basename(path)}:{stat.st_size}:{stat.st_mtime_ns}")
    return "|".join(parts)


def _archive_date_from_name(archive_name: str) -> str | None:
    date_match = re.search(r"(?<!\d)(\d{4}\.\d{2}\.\d{2})(?!\d)", archive_name)
    if not date_match:
        return None
    try:
        return datetime.strptime(date_match.group(1), "%Y.%m.%d").date().isoformat()
    except ValueError:
        return None


def _grid_coordinates(lat: float, lon: float) -> Tuple[int, int, float, float]:
    grid_row = int((lat - LAT_MIN) // GRID_SIZE_DEGREES)
    grid_col = int((lon - LON_MIN) // GRID_SIZE_DEGREES)
    cell_lat_min = LAT_MIN + grid_row * GRID_SIZE_DEGREES
    cell_lon_min = LON_MIN + grid_col * GRID_SIZE_DEGREES
    return grid_row, grid_col, cell_lat_min, cell_lon_min


def build_grid_summaries() -> None:
    setup_database()
    conn = sqlite3.connect(DB_NAME)
    try:
        setup_grid_tables(conn)
        conn.execute("DELETE FROM aircraft_cell_daily")
        conn.execute("DELETE FROM daily_grid")

        conn.execute(
            """
            INSERT INTO aircraft_cell_daily (
                archive_date, grid_row, grid_col, cell_lat_min, cell_lon_min, hex,
                observation_count, valid_nic_observations, low_nic_observations,
                low_nic_ratio, persistent_low_nic, adsb_observations, mlat_observations
            )
            SELECT
                archive_date,
                CAST((lat - ?) / ? AS INTEGER),
                CAST((lon - ?) / ? AS INTEGER),
                ? + CAST((lat - ?) / ? AS INTEGER) * ?,
                ? + CAST((lon - ?) / ? AS INTEGER) * ?,
                hex,
                COUNT(*),
                SUM(CASE WHEN source_type LIKE 'adsb%' AND nic IS NOT NULL THEN 1 ELSE 0 END),
                SUM(CASE WHEN source_type LIKE 'adsb%' AND nic IS NOT NULL AND nic < ? THEN 1 ELSE 0 END),
                CAST(SUM(CASE WHEN source_type LIKE 'adsb%' AND nic IS NOT NULL AND nic < ? THEN 1 ELSE 0 END) AS REAL)
                    / NULLIF(SUM(CASE WHEN source_type LIKE 'adsb%' AND nic IS NOT NULL THEN 1 ELSE 0 END), 0),
                CASE
                    WHEN CAST(SUM(CASE WHEN source_type LIKE 'adsb%' AND nic IS NOT NULL AND nic < ? THEN 1 ELSE 0 END) AS REAL)
                        / NULLIF(SUM(CASE WHEN source_type LIKE 'adsb%' AND nic IS NOT NULL THEN 1 ELSE 0 END), 0) >= ?
                    THEN 1 ELSE 0
                END,
                SUM(CASE WHEN source_type LIKE 'adsb%' THEN 1 ELSE 0 END),
                SUM(CASE WHEN source_type = 'mlat' THEN 1 ELSE 0 END)
            FROM flights
            WHERE archive_date IS NOT NULL
              AND hex IS NOT NULL
              AND lat IS NOT NULL
              AND lon IS NOT NULL
            GROUP BY
                archive_date,
                CAST((lat - ?) / ? AS INTEGER),
                CAST((lon - ?) / ? AS INTEGER),
                ? + CAST((lat - ?) / ? AS INTEGER) * ?,
                ? + CAST((lon - ?) / ? AS INTEGER) * ?,
                hex
            """,
            (
                LAT_MIN, GRID_SIZE_DEGREES, LON_MIN, GRID_SIZE_DEGREES,
                LAT_MIN, LAT_MIN, GRID_SIZE_DEGREES, GRID_SIZE_DEGREES,
                LON_MIN, LON_MIN, GRID_SIZE_DEGREES, GRID_SIZE_DEGREES,
                LOW_NIC_THRESHOLD, LOW_NIC_THRESHOLD, LOW_NIC_THRESHOLD,
                PERSISTENT_LOW_NIC_RATIO,
                LAT_MIN, GRID_SIZE_DEGREES, LON_MIN, GRID_SIZE_DEGREES,
                LAT_MIN, LAT_MIN, GRID_SIZE_DEGREES, GRID_SIZE_DEGREES,
                LON_MIN, LON_MIN, GRID_SIZE_DEGREES, GRID_SIZE_DEGREES,
            ),
        )

        conn.execute(
            """
            INSERT INTO daily_grid (
                archive_date, grid_row, grid_col, cell_lat_min, cell_lon_min,
                distinct_aircraft, aircraft_with_nic, low_nic_aircraft,
                low_nic_aircraft_ratio, observation_count, valid_nic_observations,
                low_nic_observations, adsb_observations, mlat_observations,
                sufficient_sample
            )
            SELECT
                archive_date,
                grid_row,
                grid_col,
                cell_lat_min,
                cell_lon_min,
                COUNT(*),
                SUM(CASE WHEN valid_nic_observations > 0 THEN 1 ELSE 0 END),
                SUM(CASE WHEN persistent_low_nic = 1 THEN 1 ELSE 0 END),
                CAST(SUM(CASE WHEN persistent_low_nic = 1 THEN 1 ELSE 0 END) AS REAL)
                    / NULLIF(SUM(CASE WHEN valid_nic_observations > 0 THEN 1 ELSE 0 END), 0),
                SUM(observation_count),
                SUM(valid_nic_observations),
                SUM(low_nic_observations),
                SUM(adsb_observations),
                SUM(mlat_observations),
                CASE WHEN SUM(CASE WHEN valid_nic_observations > 0 THEN 1 ELSE 0 END) >= ? THEN 1 ELSE 0 END
            FROM aircraft_cell_daily
            GROUP BY archive_date, grid_row, grid_col, cell_lat_min, cell_lon_min
            """,
            (MIN_AIRCRAFT_PER_CELL,),
        )
        conn.commit()
    finally:
        conn.close()


def process_local_archives(archive_dir: str = ARCHIVE_DIR) -> None:
    setup_database()
    if not os.path.isdir(archive_dir):
        os.makedirs(archive_dir)
        print(f"Created '{archive_dir}'. Put downloaded archives there and run the script again.")
        return

    archive_groups = _local_archive_groups(archive_dir)
    if not archive_groups:
        print(f"No archives found in '{archive_dir}'.")
        return

    conn = sqlite3.connect(DB_NAME)
    try:
        for archive_name, paths in archive_groups:
            archive_date = _archive_date_from_name(archive_name)
            if archive_date is None:
                print(
                    f"Skipping {archive_name}: expected a date like YYYY.MM.DD in the filename",
                    flush=True,
                )
                continue

            archive_key = _archive_key(paths)
            if conn.execute(
                "SELECT 1 FROM processed_archives WHERE archive_key = ?",
                (archive_key,),
            ).fetchone():
                print(f"Skipping already processed {archive_name}", flush=True)
                continue

            try:
                print(f"Processing {archive_name} without joining parts...", flush=True)
                _process_archive_file(paths, conn, archive_name, archive_date)
                conn.execute(
                    "INSERT INTO processed_archives VALUES (?, ?, ?)",
                    (archive_key, archive_name, datetime.now(timezone.utc).isoformat()),
                )
                conn.commit()
            finally:
                pass
    finally:
        conn.close()


def run_past_week_job() -> None:
    process_local_archives()
    build_grid_summaries()


if __name__ == "__main__":
    run_past_week_job()