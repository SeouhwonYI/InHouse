from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import argparse

from inhouse_balancer.importers import import_matches_csv
from inhouse_balancer.storage import connect, init_db


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay historical in-house matches from CSV.")
    parser.add_argument("csv_path", help="Path to matches CSV")
    parser.add_argument("--db", default="data/inhouse_balancer.sqlite", help="SQLite DB path")
    args = parser.parse_args()

    conn = connect(Path(args.db))
    init_db(conn)
    report = import_matches_csv(conn, args.csv_path)
    print(f"Imported historical matches: {report.imported}")
    print(f"Skipped rows: {report.skipped}")
    for err in report.errors[:50]:
        print(f"- {err}")
    if report.errors and len(report.errors) > 50:
        print(f"... {len(report.errors) - 50} more errors")


if __name__ == "__main__":
    main()
