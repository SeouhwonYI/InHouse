from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import argparse

from inhouse_balancer.importers import import_players_csv
from inhouse_balancer.storage import connect, delete_all_data, init_db


def main() -> None:
    parser = argparse.ArgumentParser(description="Import/update players from a CSV file.")
    parser.add_argument("csv_path", help="Path to players CSV")
    parser.add_argument("--db", default="data/inhouse_balancer.sqlite", help="SQLite DB path")
    parser.add_argument("--reset-all", action="store_true", help="DANGER: delete all players/matches before import")
    args = parser.parse_args()

    conn = connect(Path(args.db))
    init_db(conn)
    if args.reset_all:
        delete_all_data(conn)
        init_db(conn)
        print("Deleted all existing players and matches.")

    report = import_players_csv(conn, args.csv_path)
    print(f"Imported/updated players: {report.imported}")
    print(f"Skipped rows: {report.skipped}")
    for err in report.errors[:30]:
        print(f"- {err}")
    if report.errors and len(report.errors) > 30:
        print(f"... {len(report.errors) - 30} more errors")


if __name__ == "__main__":
    main()
