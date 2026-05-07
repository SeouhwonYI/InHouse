"""Migrate a local SQLite MVP database into PostgreSQL.

Usage:
    python tools/migrate_sqlite_to_postgres.py data/inhouse_balancer.sqlite --reset

Set DATABASE_URL in .env/environment or pass --database-url.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Iterable

from inhouse_balancer.storage import connect, delete_all_data, init_db, ensure_column, is_postgres_conn

TABLES = [
    "players",
    "player_role_ratings",
    "matches",
    "match_participants",
]

OPTIONAL_PLAYER_COLUMNS = [
    ("puuid", "TEXT"),
    ("summoner_id", "TEXT"),
    ("profile_icon_id", "INTEGER DEFAULT 0"),
    ("summoner_level", "INTEGER DEFAULT 0"),
]


def table_columns(conn, table: str) -> list[str]:
    if is_postgres_conn(conn):
        rows = conn.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = current_schema() AND table_name = ?
            ORDER BY ordinal_position
            """,
            (table,),
        ).fetchall()
        return [str(row["column_name"]) for row in rows]
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return [str(row["name"]) for row in rows]


def copy_table(src, dest, table: str) -> int:
    src_columns = table_columns(src, table)
    dest_columns = table_columns(dest, table)
    columns = [column for column in src_columns if column in dest_columns]
    if not columns:
        return 0

    rows = src.execute(f"SELECT {', '.join(columns)} FROM {table}").fetchall()
    if not rows:
        return 0

    placeholders = ", ".join("?" for _ in columns)
    column_sql = ", ".join(columns)
    insert_sql = f"INSERT INTO {table} ({column_sql}) VALUES ({placeholders})"
    count = 0
    for row in rows:
        dest.execute(insert_sql, tuple(row[column] for column in columns))
        count += 1
    return count


def reset_identity(dest, table: str) -> None:
    if not is_postgres_conn(dest):
        return
    dest.execute(
        f"""
        SELECT setval(
            pg_get_serial_sequence('{table}', 'id'),
            COALESCE((SELECT MAX(id) FROM {table}), 1),
            (SELECT COUNT(*) FROM {table}) > 0
        )
        """
    )


def migrate(sqlite_path: Path, *, database_url: str | None = None, reset: bool = False) -> dict[str, int]:
    src = connect(sqlite_path, database_url="")
    dest = connect(database_url=database_url or os.getenv("DATABASE_URL", ""))
    if not is_postgres_conn(dest):
        raise RuntimeError("Destination is not PostgreSQL. Set DATABASE_URL or pass --database-url.")

    init_db(dest)
    for column, definition in OPTIONAL_PLAYER_COLUMNS:
        ensure_column(dest, "players", column, definition)
    dest.commit()

    if reset:
        delete_all_data(dest)

    counts: dict[str, int] = {}
    with dest:
        for table in TABLES:
            counts[table] = copy_table(src, dest, table)
        reset_identity(dest, "players")
        reset_identity(dest, "matches")
    return counts


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("sqlite_path", nargs="?", default="data/inhouse_balancer.sqlite")
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--reset", action="store_true", help="Clear PostgreSQL tables before copying.")
    args = parser.parse_args()

    counts = migrate(Path(args.sqlite_path), database_url=args.database_url, reset=args.reset)
    print("Migration complete")
    for table, count in counts.items():
        print(f"- {table}: {count}")


if __name__ == "__main__":
    main()
