from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from inhouse_balancer.importers import bootstrap_from_records
from inhouse_balancer.storage import connect, init_db


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Reset DB, import real players, and replay historical matches in chronological order."
    )
    parser.add_argument("players_path", help="Path to players CSV/XLSX")
    parser.add_argument("matches_path", nargs="?", default=None, help="Optional path to matches CSV/XLSX")
    parser.add_argument("--db", default="data/inhouse_balancer.sqlite", help="SQLite DB path")
    parser.add_argument(
        "--no-reset-all",
        action="store_true",
        help="Do not delete existing DB data before import. Default is to reset all data.",
    )
    parser.add_argument(
        "--no-infer-preferences",
        action="store_true",
        help="Do not fill empty preferred_roles from historical match slot usage.",
    )
    parser.add_argument(
        "--inferred-slots",
        type=int,
        default=2,
        help="Number of preferred roles to infer for players with empty preferred_roles.",
    )
    args = parser.parse_args()

    conn = connect(Path(args.db))
    init_db(conn)

    result = bootstrap_from_records(
        conn,
        args.players_path,
        args.matches_path,
        reset_all=not args.no_reset_all,
        infer_missing_preferences=not args.no_infer_preferences,
        inferred_preference_slots=args.inferred_slots,
    )
    players = result["players"]
    matches = result.get("matches")
    inferred = result.get("inferred_preferences") or {}

    print(f"Imported/updated players: {players.imported}")
    print(f"Skipped player rows: {players.skipped}")
    if matches is not None:
        print(f"Replayed historical matches: {matches.imported}")
        print(f"Skipped match rows: {matches.skipped}")
    if inferred:
        print("Inferred preferred roles from match history:")
        for name, roles in inferred.items():
            print(f"- {name}: {'|'.join(roles)}")

    errors = []
    errors.extend(players.errors or [])
    if matches is not None:
        errors.extend(matches.errors or [])
    if errors:
        print("Errors:")
        for err in errors[:80]:
            print(f"- {err}")
        if len(errors) > 80:
            print(f"... {len(errors) - 80} more errors")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
