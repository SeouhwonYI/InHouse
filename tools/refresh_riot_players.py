from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import argparse
import os

from inhouse_balancer.riot_client import RiotClient
from inhouse_balancer.riot_sync import refresh_all_players_from_riot
from inhouse_balancer.storage import connect, init_db


def load_dotenv_if_exists(path: str = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def main() -> None:
    parser = argparse.ArgumentParser(description="Refresh Riot ID, solo queue rank, and optional recent-role priors.")
    parser.add_argument("--db", default="data/inhouse_balancer.sqlite", help="SQLite DB path")
    parser.add_argument("--api-key", default=None, help="Riot API key. Defaults to RIOT_API_KEY env var or .env")
    parser.add_argument("--reset-role-ratings", action="store_true", help="Reset all role ratings to Riot rank priors before historical replay")
    parser.add_argument("--infer-roles", type=int, default=0, help="Fetch N recent matches to infer preferred roles. Example: 20")
    parser.add_argument("--only", nargs="*", default=None, help="Only refresh these player names or Riot IDs")
    parser.add_argument("--min-interval", type=float, default=1.25, help="Seconds between Riot API calls")
    args = parser.parse_args()

    load_dotenv_if_exists()
    conn = connect(Path(args.db))
    init_db(conn)
    client = RiotClient(api_key=args.api_key, min_interval_seconds=args.min_interval)
    if not client.enabled:
        raise SystemExit("RIOT_API_KEY is not set. Use --api-key or create .env with RIOT_API_KEY=RGAPI-...")

    results = refresh_all_players_from_riot(
        conn,
        client,
        reset_role_ratings=args.reset_role_ratings,
        infer_roles_count=args.infer_roles,
        only_names=set(args.only) if args.only else None,
    )
    for r in results:
        inferred = f" · inferred roles={','.join(r.inferred_roles)}" if r.inferred_roles else ""
        print(f"[{r.status}] {r.player_name}: {r.message}{inferred}")
    print(f"Done. updated={sum(1 for r in results if r.status == 'updated')}, skipped={sum(1 for r in results if r.status == 'skipped')}, error={sum(1 for r in results if r.status == 'error')}")


if __name__ == "__main__":
    main()
