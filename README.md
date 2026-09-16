# ROSPIN ADS-B Archive Processor

This project processes ADS-B history archives downloaded manually from adsb.lol and stores only flight-position points inside a Romania and western Black Sea bounding box in SQLite.

The program does not download anything from the internet. You place the archive files in the `archives` folder, and the script performs the local extraction, filtering, and database insertion.

## Requirements

- Python 3.10 or newer
- No external Python packages are required by the current script

The project may use a local virtual environment at `.venv`, but the script itself uses only Python standard-library modules.

## Folder Layout

```text
ROSPIN_proj.py
README.md
archives/
romania_black_sea_adsb.db
```

The `archives` folder is created automatically if it does not exist.

## Getting Archives

Download archive assets manually from the GitHub releases of:

`https://github.com/adsblol/globe_history_2026/releases`

Place the downloaded files directly inside `archives`.

Supported formats include:

```text
archive.tar
archive.tar.gz
archive.tgz
archive.tar.aa
archive.tar.ab
archive.tar.ac
```

For a split archive, keep every part together in `archives` and keep the original filenames. For example:

```text
archives/
  v2026.09.14-planes-readsb-mlatonly-0.tar.aa
  v2026.09.14-planes-readsb-mlatonly-0.tar.ab
```

Do not extract the archive parts. The script joins split parts locally before reading them.

## Running the Script

From PowerShell in the project folder:

```powershell
& ".\.venv\Scripts\python.exe" ".\ROSPIN_proj.py"
```

If you are using a global Python installation instead:

```powershell
python .\ROSPIN_proj.py
```

The script scans every supported archive in `archives`.

## What the Script Does

1. Creates the SQLite database and required indexes if they do not exist.
2. Finds supported archive files in `archives`.
3. Groups split files by their shared archive name.
4. Streams split parts directly into the TAR reader without creating a joined copy.
5. Opens the TAR archive.
6. Reads uncompressed JSON members under `traces/`.
7. Parses each JSON trace.
8. Reads the aircraft identifier, callsign, timestamp, and position trace.
9. Keeps only points inside the configured latitude and longitude limits.
10. Inserts matching points into SQLite in batches of 10,000 rows.
11. Records completed archives in `processed_archives`.
12. Deletes temporary joined files.

No archive data is uploaded or downloaded by the script.

## Phase 1 Status

Phase 1 is the raw data ingestion layer. It is responsible for collecting and preserving the observations needed by later analysis:

- Every qualifying trace point is stored as a separate row.
- Repeated observations from the same aircraft are expected and are not collapsed during ingestion.
- `hex` identifies the aircraft so later analysis can group observations correctly.
- `archive_date` identifies the source day for daily comparisons and maps.
- `source_type` allows ADS-B and MLAT positions to be separated.
- NIC and related integrity fields are stored when present; missing source values remain `NULL`.
- Exact duplicate rows are rejected by the unique index.

The script does not yet create grid cells, calculate interference percentages, classify aircraft as degraded, or generate maps. Those belong to the analysis and visualization phases.

## Phase 2: Daily Square-Grid Summaries

Phase 2 is now implemented. After local archive processing, the script rebuilds two derived SQLite tables from the raw `flights` table:

```text
flights
  many observations per aircraft
        |
        v
aircraft_cell_daily
  one row per archive_date + grid cell + hex
        |
        v
daily_grid
  one row per archive_date + grid cell
```

The grid uses square cells of `0.1` degrees latitude by `0.1` degrees longitude. The size is configurable in `ROSPIN_proj.py`:

```python
GRID_SIZE_DEGREES = 0.1
```

The current analysis thresholds are also configurable:

```python
LOW_NIC_THRESHOLD = 7
PERSISTENT_LOW_NIC_RATIO = 0.2
MIN_AIRCRAFT_PER_CELL = 3
```

For each aircraft in each daily cell, `aircraft_cell_daily` records the observation count, valid ADS-B NIC count, low-NIC count, low-NIC ratio, whether the aircraft has persistent low NIC, and ADS-B versus MLAT observation counts.

For each daily cell, `daily_grid` records distinct aircraft, aircraft with usable NIC, persistent low-NIC aircraft, the low-NIC aircraft ratio, observation totals, and whether the cell has at least `MIN_AIRCRAFT_PER_CELL` distinct aircraft with valid ADS-B NIC data. MLAT-only aircraft do not make a NIC cell count as statistically sufficient.

The final low-NIC aircraft ratio counts aircraft, not raw observations:

```text
persistent low-NIC aircraft / aircraft with valid NIC
```

This prevents an aircraft with many reports from dominating a cell. MLAT observations are retained for coverage context but are not counted as aircraft-reported ADS-B NIC observations.

The derived tables are rebuilt from `flights` on each run. Raw observations are preserved. This makes the aggregation reproducible when thresholds or grid size change.

Query map-ready cells for one day:

```sql
SELECT
    cell_lat_min,
    cell_lon_min,
    distinct_aircraft,
    low_nic_aircraft_ratio,
    observation_count,
    sufficient_sample
FROM daily_grid
WHERE archive_date = '2026-09-14'
ORDER BY grid_row, grid_col;
```

## Geographic Filter

The current filter is a rectangular bounding box:

```text
Latitude:  43.0 through 48.3
Longitude: 20.2 through 32.5
```

This covers Romania and part of the western Black Sea, but it is not an exact country or coastline boundary. It may include points outside Romania and may include more sea area than intended.

To change it, edit these constants in `ROSPIN_proj.py`:

```python
LAT_MIN, LAT_MAX = 43.0, 48.3
LON_MIN, LON_MAX = 20.2, 32.5
```

## Database

The database file is:

`romania_black_sea_adsb.db`

The `flights` table contains:

| Column | Meaning |
|---|---|
| `archive_date` | Date represented by the source archive, stored as `YYYY-MM-DD` |
| `timestamp` | Position timestamp as a Unix timestamp, including fractional seconds when available |
| `hex` | Aircraft hexadecimal identifier |
| `flight` | Aircraft callsign, if available |
| `lat` | Latitude |
| `lon` | Longitude |
| `alt_baro` | Barometric altitude from the trace |
| `gs` | Ground speed |
| `track` | Track direction |
| `trace_flags` | Readsb trace flags, including stale-position and leg markers |
| `source_type` | Position source, such as `adsb_icao` or `mlat` |
| `nic` | Navigation Integrity Category |
| `rc` | Radius of containment in metres |
| `nic_baro` | Barometric altitude integrity category |
| `nac_p` | Navigation Accuracy Category for position |
| `nac_v` | Navigation Accuracy Category for velocity |
| `sil` | Source Integrity Level |
| `sil_type` | SIL interpretation, such as `perhour` or `persample` |
| `sda` | System Design Assurance |
| `adsb_version` | ADS-B version reported by the aircraft |
| `ias` | Indicated airspeed, when present |
| `roll` | Roll angle, when present |

The script also creates `processed_archives`, which stores the local archive signature and processing time.

The source archive date is stored separately from the point timestamp. It is extracted from filenames such as `v2026.09.14-planes-readsb-mlatonly-0.tar`, so every imported row can be grouped reliably by the day of the archive.

The optional integrity fields come from the aircraft metadata embedded in readsb trace points. They can be `NULL` when the source did not provide them. Keep `source_type` in analysis: aircraft-reported ADS-B integrity values should not be interpreted the same way as positions calculated by MLAT.

## Checking the Database

Count imported flight positions:

```powershell
& ".\.venv\Scripts\python.exe" -c "import sqlite3; c=sqlite3.connect('romania_black_sea_adsb.db'); print(c.execute('SELECT COUNT(*) FROM flights').fetchone()[0]); c.close()"
```

Show a few records:

```powershell
& ".\.venv\Scripts\python.exe" -c "import sqlite3; c=sqlite3.connect('romania_black_sea_adsb.db'); print(c.execute('SELECT * FROM flights LIMIT 5').fetchall()); c.close()"
```

Show how many points were imported for each archive day:

```powershell
& ".\.venv\Scripts\python.exe" -c "import sqlite3; c=sqlite3.connect('romania_black_sea_adsb.db'); print(c.execute('SELECT archive_date, COUNT(*) FROM flights GROUP BY archive_date ORDER BY archive_date').fetchall()); c.close()"
```

Select one day for a map:

```sql
SELECT timestamp, hex, flight, lat, lon, alt_baro, gs, track
FROM flights
WHERE archive_date = '2026-09-14'
ORDER BY timestamp;
```

See which archives were completed:

```powershell
& ".\.venv\Scripts\python.exe" -c "import sqlite3; c=sqlite3.connect('romania_black_sea_adsb.db'); print(c.execute('SELECT archive_name, processed_at FROM processed_archives').fetchall()); c.close()"
```

## Repeat Runs

A successfully processed archive is recorded in `processed_archives`. If you run the program again with the same files, it skips them instead of processing them again.

An archive is recorded only after processing completes successfully. If the program is interrupted or an archive fails, it will be attempted again on the next run.

The archive signature uses the filenames, file sizes, and modification times. If you replace or modify an archive, the script treats it as a new input.

The unique index on `flights` also prevents duplicate flight-position rows.

## Repeated Observations and Daily Aggregation

One aircraft broadcasts many position reports during a day. Therefore, one aircraft can contribute many rows, and potentially many NIC values, inside the same grid cell. The raw `flights` table intentionally preserves those observations.

The later map analysis must aggregate in two steps rather than treating every row as an independent aircraft:

```text
flights
  many observations per aircraft
        |
        v
aircraft_cell_daily
  one summary row per archive_date + grid cell + hex
        |
        v
daily_grid
  one summary row per archive_date + grid cell
```

The intermediate aircraft-cell summary can calculate, for example:

- number of observations;
- number of valid NIC observations;
- number and percentage of low-NIC observations;
- whether the aircraft has persistent degradation in that cell;
- ADS-B versus MLAT observation counts.

The final daily grid summary can then calculate:

```text
low-NIC aircraft / distinct aircraft observed in the cell
```

This avoids allowing an aircraft with an unusually dense or long track to dominate the map. It should not be implemented as one physical database table per flight; `hex` is the aircraft key.

Recommended classifications for the analysis phase are:

- `normal`: valid NIC values remain at or above the chosen threshold;
- `degraded`: a meaningful share of the aircraft's valid observations has low NIC;
- `unknown`: the aircraft has no usable NIC values.

Cells should also have minimum thresholds for distinct aircraft with valid NIC and, later, minimum observation counts. MLAT positions should be excluded from, or displayed separately from, aircraft-reported ADS-B integrity analysis.

## Planned Map Phase

Phase 3 now provides a local browser UI backed by the `daily_grid` table.

Start it from PowerShell in the project folder:

```powershell
& ".\.venv\Scripts\python.exe" ".\phase3_server.py"
```

Open:

`http://127.0.0.1:8000`

The interface provides:

- a satellite basemap focused on the configured bounding box;
- a `0.1` degree square grid;
- a day selector based on dates present in `daily_grid`;
- a flight-density lens;
- a NIC-reliability lens;
- a suspected-interference screening lens;
- an ADS-B versus MLAT coverage lens;
- cell popups with aircraft, observation, NIC, and sample-sufficiency details;
- an empty state when the database has not been populated yet.

The server is intentionally small and uses only Python's standard library. It exposes:

```text
/api/status
/api/dates
/api/grid?date=YYYY-MM-DD
```

Satellite tiles are supplied by Esri and require an internet connection in the browser. The local data API itself reads only the SQLite database and does not download archive data.

The planned map lenses are:

- flight density;
- NIC reliability percentage;
- suspected interference or degradation;
- ADS-B versus MLAT coverage;
- optional altitude layers.

The map should be described as a suspected GNSS interference indicator. NIC degradation alone does not prove jamming or spoofing, so the analysis should also consider `rc`, `nac_p`, `nac_v`, `sil`, `sda`, trajectory anomalies, missing data, traffic density, and source type.

## Important Storage Note

When processing split archives, the script streams the parts directly into the TAR reader. It does not create another multi-gigabyte combined archive, so the manually downloaded parts are the main storage requirement.

The archive is still read in full because the relevant trace files are distributed throughout the global archive. This change removes the extra full-disk copy and allows processing to begin while the parts are being read.

## Troubleshooting

### `No archives found in 'archives'.`

Put supported archive files directly in the `archives` folder and run the script again.

### The database has zero rows

Check that:

- the archive contains JSON members under `traces/`;
- the archive was downloaded completely;
- the archive contains trace points in the configured bounding box;
- all parts of a split archive are present.

### The script skips an archive because its date cannot be identified

Use the original adsb.lol filename containing a date such as `YYYY.MM.DD`. The date is required so the imported rows can be assigned to a daily dataset.

### An archive is processed again

The archive may have changed size or modification time, which creates a new archive signature. This is intentional so updated files are not silently skipped.
