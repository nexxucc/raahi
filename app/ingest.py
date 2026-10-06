"""Command-line GTFS validator/ingester for an already-acquired local ZIP."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys

from app.gtfs import FeedValidationError, load_gtfs_zip
from app.storage import ingest_feed


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate and import a local OTD bus GTFS ZIP")
    parser.add_argument("archive", help="Path to a GTFS ZIP obtained through an authorized source flow")
    parser.add_argument("--database", default="data/transit.sqlite3", help="SQLite database path")
    parser.add_argument("--source-name", default="Delhi Open Transit Data (OTD) bus GTFS")
    parser.add_argument("--source-url", default="https://otd.delhi.gov.in/data/static/")
    parser.add_argument("--attribution", default="Delhi Open Transit Data (OTD)")
    args = parser.parse_args()

    try:
        feed = load_gtfs_zip(
            args.archive,
            source_name=args.source_name,
            source_url=args.source_url,
            attribution=args.attribution,
        )
        feed_id = ingest_feed(feed, args.database)
    except (FeedValidationError, OSError, ValueError, sqlite3.Error) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(json.dumps({
        "status": "imported",
        "feed_id": feed_id,
        "source": feed.metadata.source_name,
        "retrieved_at": feed.metadata.retrieved_at.isoformat(),
        "sha256": feed.metadata.archive_sha256,
        "counts": {
            "agencies": len(feed.agencies),
            "routes": len(feed.routes),
            "stops": len(feed.stops),
            "shapes": len(feed.shapes),
            "trips": len(feed.trips),
            "calendars": len(feed.calendars),
            "calendar_dates": len(feed.calendar_dates),
            "stop_times": len(feed.stop_times),
        },
        "attribution": feed.metadata.attribution,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
