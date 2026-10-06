from __future__ import annotations

import sqlite3
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from app.gtfs import FeedValidationError, load_gtfs_zip, parse_gtfs_time
from app.storage import connect_database, replace_feed

FIXTURE_FILES = {
    "agency.txt": "agency_id,agency_name,agency_url,agency_timezone\na1,Demo Transit,https://example.test,Asia/Kolkata\n",
    "routes.txt": "route_id,agency_id,route_short_name,route_long_name,route_type\nr1,a1,10,Sample Route,3\n",
    "stops.txt": "stop_id,stop_name,stop_lat,stop_lon\ns1,Origin,28.6000,77.2000\ns2,Destination,28.6100,77.2100\n",
    "trips.txt": "route_id,service_id,trip_id,shape_id\nr1,weekday,t1,sh1\n",
    "stop_times.txt": "trip_id,arrival_time,departure_time,stop_id,stop_sequence\nt1,25:05:00,25:05:30,s1,1\nt1,25:25:00,25:25:30,s2,2\n",
    "calendar.txt": "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\nweekday,1,1,1,1,1,0,0,20260101,20261231\n",
    "shapes.txt": "shape_id,shape_pt_lat,shape_pt_lon,shape_pt_sequence\nsh1,28.6000,77.2000,1\nsh1,28.6100,77.2100,2\n",
    "feed_info.txt": "feed_publisher_name,feed_publisher_url,feed_lang,feed_version,feed_start_date,feed_end_date\nDemo,https://example.test,en,fixture-v1,20260101,20261231\n",
}


def write_zip(directory: Path, files: dict[str, str] | None = None) -> Path:
    path = directory / "feed.zip"
    with zipfile.ZipFile(path, "w") as archive:
        for filename, contents in (files or FIXTURE_FILES).items():
            archive.writestr(filename, contents)
    return path


class GtfsParsingTests(unittest.TestCase):
    def test_parses_feed_and_preserves_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            archive_path = write_zip(Path(temp))
            feed = load_gtfs_zip(str(archive_path), source_name="fixture", source_url=None)

        self.assertEqual(len(feed.routes), 1)
        self.assertEqual(len(feed.stops), 2)
        self.assertEqual(len(feed.shapes), 2)
        self.assertEqual(len(feed.trips), 1)
        self.assertEqual(feed.metadata.source_name, "fixture")
        self.assertEqual(feed.metadata.feed_version, "fixture-v1")
        self.assertEqual(len(feed.metadata.archive_sha256), 64)

    def test_supports_after_midnight_service_times(self) -> None:
        self.assertEqual(parse_gtfs_time("25:05:30"), 90_330)
        with self.assertRaises(ValueError):
            parse_gtfs_time("25:60:00")

    def test_rejects_invalid_archive_and_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            bad = Path(temp) / "bad.zip"
            bad.write_text("not a zip")
            with self.assertRaisesRegex(FeedValidationError, "Not a valid GTFS ZIP"):
                load_gtfs_zip(str(bad))

            incomplete_files = dict(FIXTURE_FILES)
            del incomplete_files["trips.txt"]
            incomplete = write_zip(Path(temp), incomplete_files)
            with self.assertRaisesRegex(FeedValidationError, "Missing required file: trips.txt"):
                load_gtfs_zip(str(incomplete))

    def test_rejects_bad_reference_and_coordinates(self) -> None:
        invalid_files = dict(FIXTURE_FILES)
        invalid_files["stops.txt"] = (
            "stop_id,stop_name,stop_lat,stop_lon\ns1,Origin,128.0,77.2\n"
        )
        invalid_files["trips.txt"] = "route_id,service_id,trip_id,shape_id\nmissing,weekday,t1,sh1\n"
        with tempfile.TemporaryDirectory() as temp:
            archive_path = write_zip(Path(temp), invalid_files)
            with self.assertRaises(FeedValidationError) as raised:
                load_gtfs_zip(str(archive_path))
        message = str(raised.exception)
        self.assertIn("stop_lat is outside", message)
        self.assertIn("unknown route_id", message)

    def test_requires_calendar_or_calendar_dates(self) -> None:
        files = dict(FIXTURE_FILES)
        del files["calendar.txt"]
        with tempfile.TemporaryDirectory() as temp:
            archive_path = write_zip(Path(temp), files)
            with self.assertRaisesRegex(FeedValidationError, "calendar.txt and/or calendar_dates.txt"):
                load_gtfs_zip(str(archive_path))


class GtfsStorageTests(unittest.TestCase):
    def test_cli_imports_fixture_and_reports_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            archive_path = write_zip(Path(temp))
            database_path = Path(temp) / "nested" / "feed.sqlite3"
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "app.ingest",
                    str(archive_path),
                    "--database",
                    str(database_path),
                    "--source-name",
                    "Test GTFS",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('"status": "imported"', result.stdout)
            self.assertIn('"source": "Test GTFS"', result.stdout)
            connection = sqlite3.connect(database_path)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM stops").fetchone()[0], 2)
            connection.close()

    def test_import_is_repeatable_and_rolls_back_failed_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            archive_path = write_zip(Path(temp))
            feed = load_gtfs_zip(str(archive_path))
            connection = connect_database(":memory:")
            feed_id = replace_feed(connection, feed)
            replace_feed(connection, feed)
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM routes WHERE feed_id=?", (feed_id,)).fetchone()[0],
                1,
            )

            broken_replacement = load_gtfs_zip(str(archive_path))
            broken_replacement.routes.append(dict(broken_replacement.routes[0]))
            with self.assertRaises(sqlite3.IntegrityError):
                replace_feed(connection, broken_replacement)

            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM routes WHERE feed_id=?", (feed_id,)).fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM stop_times WHERE feed_id=?", (feed_id,)).fetchone()[0],
                2,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT arrival_seconds FROM stop_times WHERE feed_id=? AND stop_sequence=1",
                    (feed_id,),
                ).fetchone()[0],
                90_300,
            )
            connection.close()


if __name__ == "__main__":
    unittest.main()
