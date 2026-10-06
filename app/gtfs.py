"""GTFS ZIP parsing and structural validation (no network access)."""

from __future__ import annotations

import csv
import hashlib
import io
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import PurePosixPath
from typing import Any

REQUIRED_FILES = {"agency.txt", "routes.txt", "stops.txt", "trips.txt", "stop_times.txt"}


class FeedValidationError(ValueError):
    """Raised when a GTFS archive is malformed or internally inconsistent."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("GTFS validation failed:\n- " + "\n- ".join(errors))


@dataclass(slots=True)
class FeedMetadata:
    source_name: str
    source_url: str | None
    retrieved_at: datetime
    attribution: str
    archive_sha256: str
    feed_version: str | None = None
    feed_start_date: str | None = None
    feed_end_date: str | None = None


@dataclass(slots=True)
class TransitFeed:
    metadata: FeedMetadata
    agencies: list[dict[str, Any]] = field(default_factory=list)
    routes: list[dict[str, Any]] = field(default_factory=list)
    stops: list[dict[str, Any]] = field(default_factory=list)
    shapes: list[dict[str, Any]] = field(default_factory=list)
    trips: list[dict[str, Any]] = field(default_factory=list)
    calendars: list[dict[str, Any]] = field(default_factory=list)
    calendar_dates: list[dict[str, Any]] = field(default_factory=list)
    stop_times: list[dict[str, Any]] = field(default_factory=list)


def parse_gtfs_time(value: str, *, field_name: str = "time") -> int:
    """Return seconds since service-day midnight; hours may exceed 24."""
    parts = value.strip().split(":")
    if len(parts) != 3:
        raise ValueError(f"{field_name} must use HH:MM:SS")
    try:
        hours, minutes, seconds = (int(part) for part in parts)
    except ValueError as exc:
        raise ValueError(f"{field_name} must contain numeric HH:MM:SS") from exc
    if hours < 0 or not (0 <= minutes < 60) or not (0 <= seconds < 60):
        raise ValueError(f"{field_name} has an invalid clock value")
    return hours * 3600 + minutes * 60 + seconds


def _read_csv(archive: zipfile.ZipFile, filename: str, errors: list[str]) -> list[dict[str, str]]:
    candidates = [name for name in archive.namelist() if PurePosixPath(name).name == filename]
    if len(candidates) != 1:
        errors.append(f"Expected exactly one {filename}; found {len(candidates)}")
        return []
    try:
        raw = archive.read(candidates[0]).decode("utf-8-sig")
    except (UnicodeDecodeError, KeyError) as exc:
        errors.append(f"Could not decode {filename} as UTF-8: {exc}")
        return []

    reader = csv.DictReader(io.StringIO(raw, newline=""))
    if not reader.fieldnames:
        errors.append(f"{filename} has no header row")
        return []
    headers = [header.strip() if header else "" for header in reader.fieldnames]
    if len(headers) != len(set(headers)):
        errors.append(f"{filename} has duplicate column names")
        return []

    rows: list[dict[str, str]] = []
    for line_number, row in enumerate(reader, start=2):
        normalized: dict[str, str] = {}
        for key, value in row.items():
            if key is not None:
                normalized[key.strip()] = (value or "").strip()
        if None in row:
            errors.append(f"{filename}:{line_number} has more values than columns")
        rows.append(normalized)
    return rows


def _require_columns(
    filename: str,
    rows: list[dict[str, str]],
    required: set[str],
    errors: list[str],
) -> None:
    if not rows:
        return
    actual = set(rows[0])
    missing = sorted(required - actual)
    if missing:
        errors.append(f"{filename} is missing required columns: {', '.join(missing)}")


def _check_ids(
    filename: str,
    rows: list[dict[str, str]],
    id_column: str,
    errors: list[str],
) -> set[str]:
    seen: set[str] = set()
    for line_number, row in enumerate(rows, start=2):
        identifier = row.get(id_column, "")
        if not identifier:
            errors.append(f"{filename}:{line_number} has an empty {id_column}")
        elif identifier in seen:
            errors.append(f"{filename}:{line_number} duplicates {id_column}={identifier}")
        seen.add(identifier)
    return seen


def _required_value(
    filename: str,
    rows: list[dict[str, str]],
    column: str,
    errors: list[str],
) -> None:
    for line_number, row in enumerate(rows, start=2):
        if not row.get(column, ""):
            errors.append(f"{filename}:{line_number} has an empty {column}")


def _parse_float(value: str, *, filename: str, line: int, column: str, errors: list[str]) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        errors.append(f"{filename}:{line} has invalid {column}={value!r}")
        return None


def _parse_int(value: str, *, filename: str, line: int, column: str, errors: list[str]) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        errors.append(f"{filename}:{line} has invalid {column}={value!r}")
        return None


def _valid_gtfs_date(value: str) -> bool:
    if len(value) != 8 or not value.isdigit():
        return False
    try:
        date.fromisoformat(f"{value[:4]}-{value[4:6]}-{value[6:8]}")
    except ValueError:
        return False
    return True


def load_gtfs_zip(
    path: str,
    *,
    source_name: str = "Delhi Open Transit Data (OTD) bus GTFS",
    source_url: str | None = "https://otd.delhi.gov.in/data/static/",
    attribution: str = "Delhi Open Transit Data (OTD)",
    retrieved_at: datetime | None = None,
) -> TransitFeed:
    """Load an authorized local GTFS ZIP and validate its core relationships."""
    errors: list[str] = []
    try:
        with open(path, "rb") as source_file:
            payload = source_file.read()
    except OSError as exc:
        raise FeedValidationError([f"Cannot read archive: {exc}"]) from exc
    digest = hashlib.sha256(payload).hexdigest()

    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
    except (zipfile.BadZipFile, OSError) as exc:
        raise FeedValidationError([f"Not a valid GTFS ZIP archive: {exc}"]) from exc

    with archive:
        file_basenames = [PurePosixPath(name).name for name in archive.namelist() if not name.endswith("/")]
        for required_file in sorted(REQUIRED_FILES - set(file_basenames)):
            errors.append(f"Missing required file: {required_file}")
        calendar_present = "calendar.txt" in file_basenames
        calendar_dates_present = "calendar_dates.txt" in file_basenames
        if not calendar_present and not calendar_dates_present:
            errors.append("Feed must include calendar.txt and/or calendar_dates.txt")

        files: dict[str, list[dict[str, str]]] = {}
        supported = {
            "agency.txt", "routes.txt", "stops.txt", "shapes.txt", "trips.txt",
            "calendar.txt", "calendar_dates.txt", "stop_times.txt", "feed_info.txt",
        }
        for filename in supported:
            if filename in file_basenames:
                files[filename] = _read_csv(archive, filename, errors)

    agencies = files.get("agency.txt", [])
    routes = files.get("routes.txt", [])
    stops = files.get("stops.txt", [])
    shapes = files.get("shapes.txt", [])
    trips = files.get("trips.txt", [])
    calendars = files.get("calendar.txt", [])
    calendar_dates = files.get("calendar_dates.txt", [])
    stop_times = files.get("stop_times.txt", [])
    feed_info = files.get("feed_info.txt", [])

    for filename, rows in (
        ("agency.txt", agencies),
        ("routes.txt", routes),
        ("stops.txt", stops),
        ("trips.txt", trips),
        ("stop_times.txt", stop_times),
    ):
        if filename in file_basenames and not rows:
            errors.append(f"{filename} must contain at least one data row")

    _require_columns("agency.txt", agencies, {"agency_name", "agency_url", "agency_timezone"}, errors)
    _require_columns("routes.txt", routes, {"route_id", "route_type"}, errors)
    _require_columns("stops.txt", stops, {"stop_id", "stop_name", "stop_lat", "stop_lon"}, errors)
    _require_columns("trips.txt", trips, {"route_id", "service_id", "trip_id"}, errors)
    _require_columns("stop_times.txt", stop_times, {"trip_id", "stop_id", "stop_sequence", "arrival_time", "departure_time"}, errors)
    if calendars:
        _require_columns("calendar.txt", calendars, {"service_id", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "start_date", "end_date"}, errors)
    if calendar_dates:
        _require_columns("calendar_dates.txt", calendar_dates, {"service_id", "date", "exception_type"}, errors)
    if shapes:
        _require_columns("shapes.txt", shapes, {"shape_id", "shape_pt_lat", "shape_pt_lon", "shape_pt_sequence"}, errors)

    agency_ids = _check_ids("agency.txt", agencies, "agency_id", errors) if "agency_id" in (agencies[0] if agencies else {}) else set()
    route_ids = _check_ids("routes.txt", routes, "route_id", errors)
    stop_ids = _check_ids("stops.txt", stops, "stop_id", errors)
    trip_ids = _check_ids("trips.txt", trips, "trip_id", errors)
    service_ids = _check_ids("calendar.txt", calendars, "service_id", errors) if calendars else set()
    # A service_id intentionally repeats across calendar_dates rows; uniqueness is
    # the service/date pair, not service_id alone.
    service_ids.update(row.get("service_id", "") for row in calendar_dates)
    seen_calendar_dates: set[tuple[str, str]] = set()
    for line, row in enumerate(calendar_dates, start=2):
        key = (row.get("service_id", ""), row.get("date", ""))
        if key in seen_calendar_dates:
            errors.append(f"calendar_dates.txt:{line} duplicates service_id/date={key}")
        seen_calendar_dates.add(key)

    if len(agencies) > 1 and any(not row.get("agency_id") for row in agencies):
        errors.append("agency.txt must provide agency_id when the feed has multiple agencies")

    for line, row in enumerate(routes, start=2):
        agency_id = row.get("agency_id", "")
        if agency_id and agency_id not in agency_ids:
            errors.append(f"routes.txt:{line} references unknown agency_id={agency_id}")
        _parse_int(row.get("route_type", ""), filename="routes.txt", line=line, column="route_type", errors=errors)
    for line, row in enumerate(stops, start=2):
        lat = _parse_float(row.get("stop_lat", ""), filename="stops.txt", line=line, column="stop_lat", errors=errors)
        lon = _parse_float(row.get("stop_lon", ""), filename="stops.txt", line=line, column="stop_lon", errors=errors)
        if lat is not None and not -90 <= lat <= 90:
            errors.append(f"stops.txt:{line} stop_lat is outside [-90, 90]")
        if lon is not None and not -180 <= lon <= 180:
            errors.append(f"stops.txt:{line} stop_lon is outside [-180, 180]")
        parent = row.get("parent_station", "")
        if parent and parent not in stop_ids:
            errors.append(f"stops.txt:{line} references unknown parent_station={parent}")
    for line, row in enumerate(trips, start=2):
        if row.get("route_id") not in route_ids:
            errors.append(f"trips.txt:{line} references unknown route_id={row.get('route_id')}")
        if row.get("service_id") not in service_ids:
            errors.append(f"trips.txt:{line} references unknown service_id={row.get('service_id')}")
    seen_sequences: set[tuple[str, int]] = set()
    last_sequence_by_trip: dict[str, int] = {}
    for line, row in enumerate(stop_times, start=2):
        if row.get("trip_id") not in trip_ids:
            errors.append(f"stop_times.txt:{line} references unknown trip_id={row.get('trip_id')}")
        if row.get("stop_id") not in stop_ids:
            errors.append(f"stop_times.txt:{line} references unknown stop_id={row.get('stop_id')}")
        sequence = _parse_int(row.get("stop_sequence", ""), filename="stop_times.txt", line=line, column="stop_sequence", errors=errors)
        if sequence is not None:
            key = (row.get("trip_id", ""), sequence)
            if key in seen_sequences:
                errors.append(f"stop_times.txt:{line} duplicates stop_sequence={sequence} for trip_id={key[0]}")
            previous = last_sequence_by_trip.get(key[0])
            if previous is not None and sequence <= previous:
                errors.append(f"stop_times.txt:{line} stop_sequence is not increasing for trip_id={key[0]}")
            seen_sequences.add(key)
            last_sequence_by_trip[key[0]] = sequence
        for time_column in ("arrival_time", "departure_time"):
            try:
                parse_gtfs_time(row.get(time_column, ""), field_name=f"stop_times.txt:{line} {time_column}")
            except ValueError as exc:
                errors.append(str(exc))
    for line, row in enumerate(calendars, start=2):
        for day in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"):
            if row.get(day) not in {"0", "1"}:
                errors.append(f"calendar.txt:{line} {day} must be 0 or 1")
        for date_column in ("start_date", "end_date"):
            if not _valid_gtfs_date(row.get(date_column, "")):
                errors.append(f"calendar.txt:{line} has invalid {date_column}={row.get(date_column)!r}")
        if (
            _valid_gtfs_date(row.get("start_date", ""))
            and _valid_gtfs_date(row.get("end_date", ""))
            and row["start_date"] > row["end_date"]
        ):
            errors.append(f"calendar.txt:{line} start_date is after end_date")
    for line, row in enumerate(calendar_dates, start=2):
        if row.get("exception_type") not in {"1", "2"}:
            errors.append(f"calendar_dates.txt:{line} exception_type must be 1 or 2")
        if not _valid_gtfs_date(row.get("date", "")):
            errors.append(f"calendar_dates.txt:{line} has invalid date={row.get('date')!r}")
    seen_shape_sequences: set[tuple[str, int]] = set()
    for line, row in enumerate(shapes, start=2):
        lat = _parse_float(row.get("shape_pt_lat", ""), filename="shapes.txt", line=line, column="shape_pt_lat", errors=errors)
        lon = _parse_float(row.get("shape_pt_lon", ""), filename="shapes.txt", line=line, column="shape_pt_lon", errors=errors)
        sequence = _parse_int(row.get("shape_pt_sequence", ""), filename="shapes.txt", line=line, column="shape_pt_sequence", errors=errors)
        shape_id = row.get("shape_id", "")
        if not shape_id:
            errors.append(f"shapes.txt:{line} has an empty shape_id")
        if sequence is not None:
            key = (shape_id, sequence)
            if key in seen_shape_sequences:
                errors.append(f"shapes.txt:{line} duplicates shape point sequence={sequence} for shape_id={shape_id}")
            seen_shape_sequences.add(key)
        if lat is not None and not -90 <= lat <= 90:
            errors.append(f"shapes.txt:{line} shape_pt_lat is outside [-90, 90]")
        if lon is not None and not -180 <= lon <= 180:
            errors.append(f"shapes.txt:{line} shape_pt_lon is outside [-180, 180]")
    for line, row in enumerate(trips, start=2):
        shape_id = row.get("shape_id", "")
        if shape_id and shapes and shape_id not in {point.get("shape_id") for point in shapes}:
            errors.append(f"trips.txt:{line} references unknown shape_id={shape_id}")

    if errors:
        raise FeedValidationError(errors)

    info = feed_info[0] if feed_info else {}
    metadata = FeedMetadata(
        source_name=source_name,
        source_url=source_url,
        retrieved_at=retrieved_at or datetime.now(UTC),
        attribution=attribution,
        archive_sha256=digest,
        feed_version=info.get("feed_version") or None,
        feed_start_date=info.get("feed_start_date") or None,
        feed_end_date=info.get("feed_end_date") or None,
    )
    return TransitFeed(
        metadata=metadata,
        agencies=agencies,
        routes=routes,
        stops=stops,
        shapes=shapes,
        trips=trips,
        calendars=calendars,
        calendar_dates=calendar_dates,
        stop_times=stop_times,
    )
