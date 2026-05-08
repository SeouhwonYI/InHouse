"""Persistence layer for the in-house balancer.

The app defaults to a CSV-only store for small private in-house groups.  SQLite is
still available as a local fallback by setting ``INHOUSE_STORAGE=sqlite``, and
production deployments can use PostgreSQL by setting DATABASE_URL, e.g.

    DATABASE_URL=postgresql://user:password@host:5432/dbname

The public functions intentionally keep the old sqlite-like `conn.execute(sql, params)`
interface so the rest of the MVP code can run on either backend.
"""
from __future__ import annotations

import csv
import json
import os
import sqlite3
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .constants import DEFAULT_CONFIG, ROLES
from .exports import DEFAULT_MATCH_LOG_PATH, append_match_csv_log
from .models import Player, RoleRating, TeamAssignment
from .rating import clamp, compute_rating_preview, initialize_role_ratings, tier_to_rating

DB_PATH = Path("data/inhouse_balancer.sqlite")
CSV_DATA_DIR = Path("data/csv")

CSV_PLAYER_COLUMNS = [
    "id",
    "name",
    "display_name",
    "riot_game_name",
    "riot_tag_line",
    "solo_tier",
    "solo_rank",
    "league_points",
    "flex_tier",
    "flex_rank",
    "flex_league_points",
    "base_rating",
    "preferred_roles",
    *ROLES,
    *[f"{role}_games" for role in ROLES],
    *[f"{role}_confidence" for role in ROLES],
    "puuid",
    "summoner_id",
    "profile_icon_id",
    "summoner_level",
    "top_champions_json",
    "created_at",
    "updated_at",
]

CSV_MATCH_COLUMNS = [
    "id",
    "played_at",
    "blue_win",
    "blue_score",
    "red_score",
    "blue_rating_before",
    "red_rating_before",
    "expected_blue_win",
    "carry_player_ids",
    "mvp_player_id",
    "lane_impacts",
    "notes",
    "created_at",
]

CSV_PARTICIPANT_COLUMNS = [
    "match_id",
    "player_id",
    "team",
    "role",
    "rating_before",
    "rating_after",
    "delta",
    "reason",
]


class CsvStorage:
    """Small file-backed store used by the app when no DB is configured."""

    is_csv = True

    def __init__(self, data_dir: str | Path = CSV_DATA_DIR):
        self.data_dir = Path(data_dir)
        self.players_path = self.data_dir / "players.csv"
        self.matches_path = self.data_dir / "matches.csv"
        self.participants_path = self.data_dir / "match_participants.csv"

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None

    def close(self) -> None:
        return None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def get_match_row(self, match_id: int) -> dict[str, Any] | None:
        for row in _csv_read_dicts(self.matches_path):
            if int(row.get("id") or 0) == int(match_id):
                return row
        return None


class PostgresConnection:
    """Tiny psycopg wrapper exposing a sqlite-like subset used by the app."""

    is_postgres = True

    def __init__(self, raw):
        self.raw = raw

    @staticmethod
    def _convert_placeholders(sql: str) -> str:
        # All MVP queries use sqlite-style `?` placeholders.  The SQL snippets do not
        # contain literal question marks, so this direct replacement is sufficient.
        return sql.replace("?", "%s")

    def execute(self, sql: str, params: Iterable[Any] | None = None):
        return self.raw.execute(self._convert_placeholders(sql), tuple(params or ()))

    def executescript(self, script: str) -> None:
        for statement in script.split(";"):
            statement = statement.strip()
            if statement:
                self.execute(statement)

    def commit(self) -> None:
        self.raw.commit()

    def rollback(self) -> None:
        self.raw.rollback()

    def close(self) -> None:
        self.raw.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        return False


def is_postgres_conn(conn) -> bool:
    return bool(getattr(conn, "is_postgres", False))


def is_csv_conn(conn) -> bool:
    return bool(getattr(conn, "is_csv", False))


def connect(
    db_path: str | Path = DB_PATH,
    *,
    database_url: str | None = None,
):
    """Open CSV, PostgreSQL, or SQLite.

    `database_url=None` means "read DATABASE_URL from the environment".  Passing an
    empty string disables PostgreSQL and forces SQLite, which is useful for tests and
    migration scripts.
    """
    if database_url is None:
        database_url = os.getenv("DATABASE_URL", "").strip()
    else:
        database_url = database_url.strip()

    if database_url:
        try:
            import psycopg
            from psycopg.rows import dict_row
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                "PostgreSQL mode requires psycopg. Run: pip install -r requirements.txt"
            ) from exc

        raw = psycopg.connect(database_url, row_factory=dict_row, autocommit=False)
        return PostgresConnection(raw)

    if str(db_path) == ":memory:":
        storage_backend = "sqlite"
    else:
        storage_backend = os.getenv("INHOUSE_STORAGE", "csv").strip().lower()
    if storage_backend in {"csv", "file", "files"}:
        csv_dir = os.getenv("CSV_DATA_DIR", "").strip()
        return CsvStorage(csv_dir or CSV_DATA_DIR)

    path = Path(db_path)
    if str(path) != ":memory":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn) -> None:
    if is_csv_conn(conn):
        _init_csv_store(conn)
        return
    if is_postgres_conn(conn):
        _init_postgres_db(conn)
    else:
        _init_sqlite_db(conn)
    ensure_column(conn, "players", "display_name", "TEXT")
    ensure_column(conn, "players", "flex_tier", "TEXT DEFAULT 'UNRANKED'")
    ensure_column(conn, "players", "flex_rank", "TEXT DEFAULT ''")
    ensure_column(conn, "players", "flex_league_points", "INTEGER DEFAULT 0")
    ensure_column(conn, "players", "top_champions_json", "TEXT NOT NULL DEFAULT '{}'")
    conn.commit()


def _now_text() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _csv_read_dicts(path: Path) -> list[dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return [dict(row) for row in csv.DictReader(f)]


def _csv_write_dicts(path: Path, columns: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in columns})


def _csv_append_dict(path: Path, columns: list[str], row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    file_exists = path.exists() and path.stat().st_size > 0
    with path.open("a", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        if not file_exists:
            writer.writeheader()
        writer.writerow({column: row.get(column, "") for column in columns})


def _init_csv_store(conn: CsvStorage) -> None:
    conn.data_dir.mkdir(parents=True, exist_ok=True)
    if not conn.players_path.exists():
        _csv_write_dicts(conn.players_path, CSV_PLAYER_COLUMNS, [])
    if not conn.matches_path.exists():
        _csv_write_dicts(conn.matches_path, CSV_MATCH_COLUMNS, [])
    if not conn.participants_path.exists():
        _csv_write_dicts(conn.participants_path, CSV_PARTICIPANT_COLUMNS, [])


def _csv_next_id(rows: list[dict[str, str]]) -> int:
    ids = [int(row.get("id") or 0) for row in rows if str(row.get("id") or "").strip()]
    return (max(ids) + 1) if ids else 1


def _csv_player_from_row(row: dict[str, str]) -> Player:
    ratings: dict[str, RoleRating] = {}
    base = float(row.get("base_rating") or 50.0)
    for role in ROLES:
        ratings[role] = RoleRating(
            role=role,
            rating=float(row.get(role) or base),
            games_played=int(float(row.get(f"{role}_games") or 0)),
            confidence=float(row.get(f"{role}_confidence") or 0.45),
        )
    return Player(
        id=int(row["id"]) if str(row.get("id") or "").strip() else None,
        name=str(row.get("name") or ""),
        display_name=str(row.get("display_name") or "") or None,
        riot_game_name=str(row.get("riot_game_name") or "") or None,
        riot_tag_line=str(row.get("riot_tag_line") or "") or None,
        solo_tier=str(row.get("solo_tier") or "UNRANKED"),
        solo_rank=str(row.get("solo_rank") or ""),
        league_points=int(float(row.get("league_points") or 0)),
        flex_tier=str(row.get("flex_tier") or "UNRANKED"),
        flex_rank=str(row.get("flex_rank") or ""),
        flex_league_points=int(float(row.get("flex_league_points") or 0)),
        base_rating=base,
        preferred_roles=_json_loads_list(json.dumps(str(row.get("preferred_roles") or "").split("|"))),
        role_ratings=ratings,
        lane_champions=normalize_top_champions_by_role(row.get("top_champions_json")),
    )


def _csv_players_by_id(conn: CsvStorage) -> dict[int, Player]:
    return {int(player.id): player for player in list_players(conn) if player.id is not None}


def _init_sqlite_db(conn) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS players (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            display_name TEXT,
            riot_game_name TEXT,
            riot_tag_line TEXT,
            solo_tier TEXT DEFAULT 'UNRANKED',
            solo_rank TEXT DEFAULT '',
            league_points INTEGER DEFAULT 0,
            flex_tier TEXT DEFAULT 'UNRANKED',
            flex_rank TEXT DEFAULT '',
            flex_league_points INTEGER DEFAULT 0,
            base_rating REAL NOT NULL DEFAULT 50.0,
            preferred_roles_json TEXT NOT NULL DEFAULT '[]',
            top_champions_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS player_role_ratings (
            player_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            rating REAL NOT NULL,
            games_played INTEGER NOT NULL DEFAULT 0,
            confidence REAL NOT NULL DEFAULT 0.45,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (player_id, role),
            FOREIGN KEY (player_id) REFERENCES players(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS matches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            played_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            blue_win INTEGER NOT NULL,
            blue_score INTEGER DEFAULT 1,
            red_score INTEGER DEFAULT 0,
            blue_rating_before REAL NOT NULL,
            red_rating_before REAL NOT NULL,
            expected_blue_win REAL NOT NULL,
            carry_player_ids_json TEXT NOT NULL DEFAULT '[]',
            mvp_player_id INTEGER,
            lane_impacts_json TEXT NOT NULL DEFAULT '{}',
            notes TEXT DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS match_participants (
            match_id INTEGER NOT NULL,
            player_id INTEGER NOT NULL,
            team TEXT NOT NULL,
            role TEXT NOT NULL,
            rating_before REAL NOT NULL,
            rating_after REAL NOT NULL,
            delta REAL NOT NULL,
            reason TEXT NOT NULL,
            PRIMARY KEY (match_id, player_id),
            FOREIGN KEY (match_id) REFERENCES matches(id) ON DELETE CASCADE,
            FOREIGN KEY (player_id) REFERENCES players(id) ON DELETE CASCADE
        );
        """
    )


def _init_postgres_db(conn) -> None:
    statements = [
        """
        CREATE TABLE IF NOT EXISTS players (
            id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            display_name TEXT,
            riot_game_name TEXT,
            riot_tag_line TEXT,
            solo_tier TEXT DEFAULT 'UNRANKED',
            solo_rank TEXT DEFAULT '',
            league_points INTEGER DEFAULT 0,
            flex_tier TEXT DEFAULT 'UNRANKED',
            flex_rank TEXT DEFAULT '',
            flex_league_points INTEGER DEFAULT 0,
            base_rating DOUBLE PRECISION NOT NULL DEFAULT 50.0,
            preferred_roles_json TEXT NOT NULL DEFAULT '[]',
            top_champions_json TEXT NOT NULL DEFAULT '{}',
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS player_role_ratings (
            player_id INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
            role TEXT NOT NULL,
            rating DOUBLE PRECISION NOT NULL,
            games_played INTEGER NOT NULL DEFAULT 0,
            confidence DOUBLE PRECISION NOT NULL DEFAULT 0.45,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (player_id, role)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS matches (
            id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            played_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            blue_win INTEGER NOT NULL,
            blue_score INTEGER DEFAULT 1,
            red_score INTEGER DEFAULT 0,
            blue_rating_before DOUBLE PRECISION NOT NULL,
            red_rating_before DOUBLE PRECISION NOT NULL,
            expected_blue_win DOUBLE PRECISION NOT NULL,
            carry_player_ids_json TEXT NOT NULL DEFAULT '[]',
            mvp_player_id INTEGER,
            lane_impacts_json TEXT NOT NULL DEFAULT '{}',
            notes TEXT DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS match_participants (
            match_id INTEGER NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
            player_id INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
            team TEXT NOT NULL,
            role TEXT NOT NULL,
            rating_before DOUBLE PRECISION NOT NULL,
            rating_after DOUBLE PRECISION NOT NULL,
            delta DOUBLE PRECISION NOT NULL,
            reason TEXT NOT NULL,
            PRIMARY KEY (match_id, player_id)
        )
        """,
    ]
    for statement in statements:
        conn.execute(statement)


def ensure_column(conn, table: str, column: str, definition: str) -> None:
    """Add a nullable column when an older database is missing it."""
    if is_postgres_conn(conn):
        row = conn.execute(
            """
            SELECT 1 AS exists
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = ?
              AND column_name = ?
            """,
            (table, column),
        ).fetchone()
        exists = row is not None
    else:
        columns = {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        exists = column in columns
    if not exists:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


# Backward-compatible private alias.
_ensure_column = ensure_column


def _json_loads_list(value: str | None) -> list[str]:
    if not value:
        return []
    loaded = json.loads(value)
    if not isinstance(loaded, list):
        return []
    return [str(x) for x in loaded]


def _json_loads_dict(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        loaded = json.loads(value)
    except Exception:  # noqa: BLE001
        return {}
    return loaded if isinstance(loaded, dict) else {}


def normalize_top_champions_by_role(value: Any) -> dict[str, list[dict[str, Any]]]:
    """Normalize champion pools from Riot sync or hand-authored CSV input.

    Accepted shapes:
      {"ADC": [{"champion": "Sivir", "games": 4}]}
      {"ADC": ["Sivir", "Xayah", "Ashe"]}

    Empty champion names are ignored, and only known app roles are kept.
    """
    if value is None:
        return {role: [] for role in ROLES}
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return {role: [] for role in ROLES}
        try:
            value = json.loads(text)
        except Exception:  # noqa: BLE001
            return {role: [] for role in ROLES}
    if not isinstance(value, dict):
        return {role: [] for role in ROLES}

    normalized: dict[str, list[dict[str, Any]]] = {role: [] for role in ROLES}
    for raw_role, raw_items in value.items():
        role = str(raw_role or "").strip().upper()
        if role not in ROLES:
            continue
        if isinstance(raw_items, (str, dict)):
            items = [raw_items]
        elif isinstance(raw_items, list):
            items = raw_items
        else:
            items = []

        seen: set[str] = set()
        for item in items:
            games = 0
            if isinstance(item, dict):
                champion = str(item.get("champion") or item.get("name") or "").strip()
                try:
                    games = int(float(item.get("games") or 0))
                except Exception:  # noqa: BLE001
                    games = 0
            else:
                champion = str(item or "").strip()
            if not champion or champion in seen:
                continue
            seen.add(champion)
            normalized[role].append({"champion": champion, "games": max(0, games)})
    return normalized


def _player_from_row(row: sqlite3.Row, ratings: dict[str, RoleRating]) -> Player:
    return Player(
        id=int(row["id"]),
        name=str(row["name"]),
        display_name=row["display_name"] if "display_name" in row.keys() else None,
        riot_game_name=row["riot_game_name"],
        riot_tag_line=row["riot_tag_line"],
        solo_tier=str(row["solo_tier"] or "UNRANKED"),
        solo_rank=str(row["solo_rank"] or ""),
        league_points=int(row["league_points"] or 0),
        flex_tier=str(row["flex_tier"] or "UNRANKED") if "flex_tier" in row.keys() else "UNRANKED",
        flex_rank=str(row["flex_rank"] or "") if "flex_rank" in row.keys() else "",
        flex_league_points=int(row["flex_league_points"] or 0) if "flex_league_points" in row.keys() else 0,
        base_rating=float(row["base_rating"]),
        preferred_roles=_json_loads_list(row["preferred_roles_json"]),
        role_ratings=ratings,
        lane_champions=normalize_top_champions_by_role(
            row["top_champions_json"] if "top_champions_json" in row.keys() else "{}"
        ),
    )


def create_player(
    conn: sqlite3.Connection,
    *,
    name: str,
    preferred_roles: Iterable[str] = (),
    solo_tier: str = "UNRANKED",
    solo_rank: str = "",
    league_points: int = 0,
    flex_tier: str = "UNRANKED",
    flex_rank: str = "",
    flex_league_points: int = 0,
    role_ratings: dict[str, float] | None = None,
    display_name: str | None = None,
    riot_game_name: str | None = None,
    riot_tag_line: str | None = None,
    top_champions_by_role: dict[str, list[dict[str, Any]]] | None = None,
) -> int:
    preferred = [role.upper() for role in preferred_roles if role.upper() in ROLES]
    display_name = (display_name or name).strip()
    base_rating = tier_to_rating(solo_tier, solo_rank, league_points)
    top_champions_by_role = normalize_top_champions_by_role(top_champions_by_role)
    if role_ratings is None:
        role_ratings = initialize_role_ratings(base_rating, preferred)

    if is_csv_conn(conn):
        rows = _csv_read_dicts(conn.players_path)
        now = _now_text()
        existing = next((row for row in rows if str(row.get("name") or "") == name), None)
        if existing is None:
            existing = {
                "id": _csv_next_id(rows),
                "created_at": now,
            }
            rows.append(existing)
        existing.update(
            {
                "name": name,
                "display_name": display_name,
                "riot_game_name": riot_game_name or "",
                "riot_tag_line": riot_tag_line or "",
                "solo_tier": solo_tier.upper(),
                "solo_rank": solo_rank.upper(),
                "league_points": int(league_points),
                "flex_tier": flex_tier.upper(),
                "flex_rank": flex_rank.upper(),
                "flex_league_points": int(flex_league_points),
                "base_rating": float(base_rating),
                "preferred_roles": "|".join(preferred),
                "top_champions_json": json.dumps(top_champions_by_role, ensure_ascii=False),
                "updated_at": now,
            }
        )
        for role in ROLES:
            existing[role] = float(role_ratings[role])
            existing.setdefault(f"{role}_games", 0)
            existing.setdefault(f"{role}_confidence", 0.45)
        _csv_write_dicts(conn.players_path, CSV_PLAYER_COLUMNS, rows)
        return int(existing["id"])

    cur = conn.execute(
        """
        INSERT INTO players (
            name, display_name, riot_game_name, riot_tag_line, solo_tier, solo_rank,
            league_points, flex_tier, flex_rank, flex_league_points,
            base_rating, preferred_roles_json, top_champions_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET
            display_name = excluded.display_name,
            riot_game_name = excluded.riot_game_name,
            riot_tag_line = excluded.riot_tag_line,
            solo_tier = excluded.solo_tier,
            solo_rank = excluded.solo_rank,
            league_points = excluded.league_points,
            flex_tier = excluded.flex_tier,
            flex_rank = excluded.flex_rank,
            flex_league_points = excluded.flex_league_points,
            base_rating = excluded.base_rating,
            preferred_roles_json = excluded.preferred_roles_json,
            top_champions_json = excluded.top_champions_json,
            updated_at = CURRENT_TIMESTAMP
        RETURNING id
        """,
        (
            name,
            display_name,
            riot_game_name,
            riot_tag_line,
            solo_tier.upper(),
            solo_rank.upper(),
            int(league_points),
            flex_tier.upper(),
            flex_rank.upper(),
            int(flex_league_points),
            float(base_rating),
            json.dumps(preferred, ensure_ascii=False),
            json.dumps(top_champions_by_role, ensure_ascii=False),
        ),
    )
    player_id = int(cur.fetchone()["id"])

    for role in ROLES:
        conn.execute(
            """
            INSERT INTO player_role_ratings (player_id, role, rating, games_played, confidence)
            VALUES (?, ?, ?, 0, 0.45)
            ON CONFLICT(player_id, role) DO UPDATE SET
                rating = excluded.rating,
                updated_at = CURRENT_TIMESTAMP
            """,
            (player_id, role, float(role_ratings[role])),
        )
    conn.commit()
    return player_id


def update_player_riot_snapshot(
    conn,
    *,
    player_id: int,
    riot_game_name: str,
    riot_tag_line: str,
    puuid: str,
    summoner_id: str | None = None,
    profile_icon_id: int = 0,
    summoner_level: int = 0,
    solo_tier: str = "UNRANKED",
    solo_rank: str = "",
    league_points: int = 0,
    flex_tier: str = "UNRANKED",
    flex_rank: str = "",
    flex_league_points: int = 0,
    base_rating: float = 50.0,
    preferred_roles: Iterable[str] = (),
    role_priors: dict[str, float] | None = None,
    top_champions_by_role: dict[str, list[dict[str, Any]]] | None = None,
    reset_role_ratings: bool = False,
) -> None:
    """Persist Riot-ranked priors and recent lane champion pools for one player."""
    preferred = [role.upper() for role in preferred_roles if role.upper() in ROLES]
    role_priors = role_priors or initialize_role_ratings(base_rating, preferred)
    champions_json = json.dumps(normalize_top_champions_by_role(top_champions_by_role), ensure_ascii=False)

    if is_csv_conn(conn):
        rows = _csv_read_dicts(conn.players_path)
        target = next((row for row in rows if int(row.get("id") or 0) == int(player_id)), None)
        if target is None:
            raise ValueError(f"player_id를 찾을 수 없습니다: {player_id}")
        target.update(
            {
                "riot_game_name": riot_game_name,
                "riot_tag_line": riot_tag_line,
                "puuid": puuid,
                "summoner_id": summoner_id or "",
                "profile_icon_id": int(profile_icon_id or 0),
                "summoner_level": int(summoner_level or 0),
                "solo_tier": solo_tier.upper(),
                "solo_rank": solo_rank.upper(),
                "league_points": int(league_points),
                "flex_tier": flex_tier.upper(),
                "flex_rank": flex_rank.upper(),
                "flex_league_points": int(flex_league_points),
                "base_rating": float(base_rating),
                "preferred_roles": "|".join(preferred),
                "top_champions_json": champions_json,
                "updated_at": _now_text(),
            }
        )
        for role in ROLES:
            games_played = int(float(target.get(f"{role}_games") or 0))
            if reset_role_ratings or games_played == 0:
                target[role] = float(role_priors[role])
                if reset_role_ratings:
                    target[f"{role}_games"] = 0
                    target[f"{role}_confidence"] = 0.45
        _csv_write_dicts(conn.players_path, CSV_PLAYER_COLUMNS, rows)
        return

    with conn:
        conn.execute(
            """
            UPDATE players
            SET riot_game_name = ?, riot_tag_line = ?, puuid = ?, summoner_id = ?,
                profile_icon_id = ?, summoner_level = ?,
                solo_tier = ?, solo_rank = ?, league_points = ?,
                flex_tier = ?, flex_rank = ?, flex_league_points = ?,
                base_rating = ?, preferred_roles_json = ?, top_champions_json = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                riot_game_name,
                riot_tag_line,
                puuid,
                summoner_id,
                int(profile_icon_id or 0),
                int(summoner_level or 0),
                solo_tier.upper(),
                solo_rank.upper(),
                int(league_points),
                flex_tier.upper(),
                flex_rank.upper(),
                int(flex_league_points),
                float(base_rating),
                json.dumps(preferred, ensure_ascii=False),
                champions_json,
                int(player_id),
            ),
        )
        for role in ROLES:
            row = conn.execute(
                "SELECT games_played FROM player_role_ratings WHERE player_id = ? AND role = ?",
                (int(player_id), role),
            ).fetchone()
            games_played = int(row["games_played"] if row else 0)
            if reset_role_ratings or games_played == 0:
                conn.execute(
                    """
                    INSERT INTO player_role_ratings (player_id, role, rating, games_played, confidence)
                    VALUES (?, ?, ?, 0, 0.45)
                    ON CONFLICT(player_id, role) DO UPDATE SET
                        rating = excluded.rating,
                        games_played = CASE WHEN ? THEN 0 ELSE player_role_ratings.games_played END,
                        confidence = CASE WHEN ? THEN 0.45 ELSE player_role_ratings.confidence END,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (int(player_id), role, float(role_priors[role]), bool(reset_role_ratings), bool(reset_role_ratings)),
                )


def list_players(conn: sqlite3.Connection) -> list[Player]:
    if is_csv_conn(conn):
        return [
            _csv_player_from_row(row)
            for row in sorted(_csv_read_dicts(conn.players_path), key=lambda item: int(item.get("id") or 0))
            if str(row.get("name") or "").strip()
        ]

    rows = conn.execute("SELECT * FROM players ORDER BY id ASC").fetchall()
    if not rows:
        return []

    role_rows = conn.execute("SELECT * FROM player_role_ratings").fetchall()
    ratings_by_player: dict[int, dict[str, RoleRating]] = {}
    for rr in role_rows:
        ratings_by_player.setdefault(int(rr["player_id"]), {})[str(rr["role"])] = RoleRating(
            role=str(rr["role"]),
            rating=float(rr["rating"]),
            games_played=int(rr["games_played"]),
            confidence=float(rr["confidence"]),
        )

    players = []
    for row in rows:
        player_id = int(row["id"])
        players.append(_player_from_row(row, ratings_by_player.get(player_id, {})))
    return players


def get_player(conn: sqlite3.Connection, player_id: int) -> Player | None:
    if is_csv_conn(conn):
        for player in list_players(conn):
            if player.id is not None and int(player.id) == int(player_id):
                return player
        return None

    row = conn.execute("SELECT * FROM players WHERE id = ?", (int(player_id),)).fetchone()
    if row is None:
        return None
    role_rows = conn.execute(
        "SELECT * FROM player_role_ratings WHERE player_id = ?", (int(player_id),)
    ).fetchall()
    ratings = {
        str(rr["role"]): RoleRating(
            role=str(rr["role"]),
            rating=float(rr["rating"]),
            games_played=int(rr["games_played"]),
            confidence=float(rr["confidence"]),
        )
        for rr in role_rows
    }
    return _player_from_row(row, ratings)


def count_players(conn: sqlite3.Connection) -> int:
    if is_csv_conn(conn):
        return len(list_players(conn))
    return int(conn.execute("SELECT COUNT(*) AS c FROM players").fetchone()["c"])


def delete_all_data(conn) -> None:
    if is_csv_conn(conn):
        _csv_write_dicts(conn.players_path, CSV_PLAYER_COLUMNS, [])
        _csv_write_dicts(conn.matches_path, CSV_MATCH_COLUMNS, [])
        _csv_write_dicts(conn.participants_path, CSV_PARTICIPANT_COLUMNS, [])
        return

    if is_postgres_conn(conn):
        conn.execute(
            "TRUNCATE TABLE match_participants, matches, player_role_ratings, players RESTART IDENTITY CASCADE"
        )
    else:
        conn.executescript(
            """
            DELETE FROM match_participants;
            DELETE FROM matches;
            DELETE FROM player_role_ratings;
            DELETE FROM players;
            DELETE FROM sqlite_sequence WHERE name IN ('players', 'matches');
            """
        )
    conn.commit()


def record_match_and_update(
    conn: sqlite3.Connection,
    *,
    blue: TeamAssignment,
    red: TeamAssignment,
    blue_win: bool,
    blue_score: int = 1,
    red_score: int = 0,
    carry_player_ids: Iterable[int] | None = None,
    mvp_player_id: int | None = None,
    lane_impacts: dict[str, str] | None = None,
    notes: str = "",
    config: dict[str, float] | None = None,
    append_csv_log: bool = True,
    csv_log_path: str | Path = DEFAULT_MATCH_LOG_PATH,
) -> tuple[int, list[dict[str, Any]]]:
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    carry_ids = [int(x) for x in (carry_player_ids or [])]
    lane_impacts = lane_impacts or {role: "비등" for role in ROLES}

    changes = compute_rating_preview(
        blue,
        red,
        blue_win=blue_win,
        carry_player_ids=carry_ids,
        mvp_player_id=mvp_player_id,
        lane_impacts=lane_impacts,
        config=cfg,
    )

    expected_blue = float(
        1.0
        / (1.0 + 10.0 ** (-(blue.total_rating - red.total_rating) / cfg["winrate_scale"]))
    )

    if is_csv_conn(conn):
        match_rows = _csv_read_dicts(conn.matches_path)
        match_id = _csv_next_id(match_rows)
        now = _now_text()
        _csv_append_dict(
            conn.matches_path,
            CSV_MATCH_COLUMNS,
            {
                "id": match_id,
                "played_at": now,
                "blue_win": 1 if blue_win else 0,
                "blue_score": int(blue_score),
                "red_score": int(red_score),
                "blue_rating_before": float(blue.total_rating),
                "red_rating_before": float(red.total_rating),
                "expected_blue_win": float(expected_blue),
                "carry_player_ids": "|".join(str(x) for x in carry_ids),
                "mvp_player_id": int(mvp_player_id) if mvp_player_id is not None else "",
                "lane_impacts": json.dumps(lane_impacts, ensure_ascii=False),
                "notes": notes,
                "created_at": now,
            },
        )

        player_rows = _csv_read_dicts(conn.players_path)
        rows_by_id = {int(row.get("id") or 0): row for row in player_rows}
        for change in changes:
            row = rows_by_id.get(int(change.player_id))
            if row is not None:
                current_base = float(row.get("base_rating") or 50.0)
                row[change.role] = float(change.after)
                row[f"{change.role}_games"] = int(change.games_after)
                row[f"{change.role}_confidence"] = float(change.confidence_after)
                row["base_rating"] = float(clamp(current_base + change.delta * cfg["base_rating_share"]))
                row["updated_at"] = now
            _csv_append_dict(
                conn.participants_path,
                CSV_PARTICIPANT_COLUMNS,
                {
                    "match_id": match_id,
                    "player_id": int(change.player_id),
                    "team": change.team,
                    "role": change.role,
                    "rating_before": float(change.before),
                    "rating_after": float(change.after),
                    "delta": float(change.delta),
                    "reason": change.reason,
                },
            )
        _csv_write_dicts(conn.players_path, CSV_PLAYER_COLUMNS, player_rows)

        if append_csv_log:
            append_match_csv_log(
                conn,
                match_id=match_id,
                blue=blue,
                red=red,
                blue_win=blue_win,
                blue_score=blue_score,
                red_score=red_score,
                carry_player_ids=carry_ids,
                mvp_player_id=mvp_player_id,
                lane_impacts=lane_impacts,
                notes=notes,
                path=csv_log_path,
            )
        return match_id, [asdict(change) for change in changes]

    with conn:
        cur = conn.execute(
            """
            INSERT INTO matches (
                blue_win, blue_score, red_score, blue_rating_before, red_rating_before,
                expected_blue_win, carry_player_ids_json, mvp_player_id,
                lane_impacts_json, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            RETURNING id
            """,
            (
                1 if blue_win else 0,
                int(blue_score),
                int(red_score),
                float(blue.total_rating),
                float(red.total_rating),
                float(expected_blue),
                json.dumps(carry_ids, ensure_ascii=False),
                int(mvp_player_id) if mvp_player_id is not None else None,
                json.dumps(lane_impacts, ensure_ascii=False),
                notes,
            ),
        )
        match_id = int(cur.fetchone()["id"])

        for change in changes:
            conn.execute(
                """
                UPDATE player_role_ratings
                SET rating = ?, games_played = ?, confidence = ?, updated_at = CURRENT_TIMESTAMP
                WHERE player_id = ? AND role = ?
                """,
                (
                    float(change.after),
                    int(change.games_after),
                    float(change.confidence_after),
                    int(change.player_id),
                    change.role,
                ),
            )
            # Keep a slower-moving general base rating for profile summaries.
            conn.execute(
                """
                UPDATE players
                SET base_rating = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    float(
                        clamp(
                            conn.execute(
                                "SELECT base_rating FROM players WHERE id = ?",
                                (int(change.player_id),),
                            ).fetchone()["base_rating"]
                            + change.delta * cfg["base_rating_share"]
                        )
                    ),
                    int(change.player_id),
                ),
            )
            conn.execute(
                """
                INSERT INTO match_participants (
                    match_id, player_id, team, role, rating_before, rating_after, delta, reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    match_id,
                    int(change.player_id),
                    change.team,
                    change.role,
                    float(change.before),
                    float(change.after),
                    float(change.delta),
                    change.reason,
                ),
            )

    if append_csv_log:
        append_match_csv_log(
            conn,
            match_id=match_id,
            blue=blue,
            red=red,
            blue_win=blue_win,
            blue_score=blue_score,
            red_score=red_score,
            carry_player_ids=carry_ids,
            mvp_player_id=mvp_player_id,
            lane_impacts=lane_impacts,
            notes=notes,
            path=csv_log_path,
        )

    return match_id, [asdict(change) for change in changes]


def list_matches(conn: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
    if is_csv_conn(conn):
        rows: list[dict[str, Any]] = []
        for row in _csv_read_dicts(conn.matches_path):
            rows.append(
                {
                    "id": int(row.get("id") or 0),
                    "played_at": row.get("played_at") or "",
                    "blue_win": int(float(row.get("blue_win") or 0)),
                    "blue_score": int(float(row.get("blue_score") or 0)),
                    "red_score": int(float(row.get("red_score") or 0)),
                    "blue_rating_before": float(row.get("blue_rating_before") or 0),
                    "red_rating_before": float(row.get("red_rating_before") or 0),
                    "expected_blue_win": float(row.get("expected_blue_win") or 0),
                    "carry_player_ids_json": json.dumps(
                        [int(x) for x in str(row.get("carry_player_ids") or "").split("|") if x],
                        ensure_ascii=False,
                    ),
                    "mvp_player_id": int(row["mvp_player_id"]) if str(row.get("mvp_player_id") or "").strip() else None,
                    "lane_impacts_json": row.get("lane_impacts") or "{}",
                    "notes": row.get("notes") or "",
                    "created_at": row.get("created_at") or "",
                }
            )
        rows.sort(key=lambda item: (str(item["played_at"]), int(item["id"])), reverse=True)
        return rows[: int(limit)]

    rows = conn.execute(
        """
        SELECT * FROM matches
        ORDER BY played_at DESC, id DESC
        LIMIT ?
        """,
        (int(limit),),
    ).fetchall()
    return [dict(row) for row in rows]


def list_match_participants(conn: sqlite3.Connection, match_id: int) -> list[dict[str, Any]]:
    if is_csv_conn(conn):
        players = _csv_players_by_id(conn)
        rows: list[dict[str, Any]] = []
        for row in _csv_read_dicts(conn.participants_path):
            if int(row.get("match_id") or 0) != int(match_id):
                continue
            player = players.get(int(row.get("player_id") or 0))
            rows.append(
                {
                    "match_id": int(row.get("match_id") or 0),
                    "player_id": int(row.get("player_id") or 0),
                    "team": row.get("team") or "",
                    "role": row.get("role") or "",
                    "rating_before": float(row.get("rating_before") or 0),
                    "rating_after": float(row.get("rating_after") or 0),
                    "delta": float(row.get("delta") or 0),
                    "reason": row.get("reason") or "",
                    "name": player.name if player else "",
                }
            )
        rows.sort(key=lambda item: (item["team"], ROLES.index(item["role"]) if item["role"] in ROLES else 99))
        return rows

    rows = conn.execute(
        """
        SELECT mp.*, p.name
        FROM match_participants mp
        JOIN players p ON p.id = mp.player_id
        WHERE mp.match_id = ?
        ORDER BY mp.team ASC,
                 CASE mp.role
                    WHEN 'TOP' THEN 1
                    WHEN 'JG' THEN 2
                    WHEN 'MID' THEN 3
                    WHEN 'ADC' THEN 4
                    WHEN 'SUP' THEN 5
                    ELSE 99
                 END
        """,
        (int(match_id),),
    ).fetchall()
    return [dict(row) for row in rows]


def list_player_match_history(conn: sqlite3.Connection, player_id: int, limit: int = 20) -> list[dict[str, Any]]:
    if is_csv_conn(conn):
        matches = {int(row["id"]): row for row in list_matches(conn, limit=100000)}
        rows: list[dict[str, Any]] = []
        for row in _csv_read_dicts(conn.participants_path):
            if int(row.get("player_id") or 0) != int(player_id):
                continue
            match = matches.get(int(row.get("match_id") or 0))
            if not match:
                continue
            rows.append(
                {
                    "match_id": int(row.get("match_id") or 0),
                    "played_at": match["played_at"],
                    "blue_win": match["blue_win"],
                    "carry_player_ids_json": match["carry_player_ids_json"],
                    "mvp_player_id": match["mvp_player_id"],
                    "team": row.get("team") or "",
                    "role": row.get("role") or "",
                    "rating_before": float(row.get("rating_before") or 0),
                    "rating_after": float(row.get("rating_after") or 0),
                    "delta": float(row.get("delta") or 0),
                    "reason": row.get("reason") or "",
                }
            )
        rows.sort(key=lambda item: (str(item["played_at"]), int(item["match_id"])), reverse=True)
        return rows[: int(limit)]

    rows = conn.execute(
        """
        SELECT
            m.id AS match_id,
            m.played_at,
            m.blue_win,
            m.carry_player_ids_json,
            m.mvp_player_id,
            mp.team,
            mp.role,
            mp.rating_before,
            mp.rating_after,
            mp.delta,
            mp.reason
        FROM match_participants mp
        JOIN matches m ON m.id = mp.match_id
        WHERE mp.player_id = ?
        ORDER BY m.played_at DESC, m.id DESC
        LIMIT ?
        """,
        (int(player_id), int(limit)),
    ).fetchall()
    return [dict(row) for row in rows]


EXPORT_MATCH_COLUMNS = [
    "played_at",
    "blue_win",
    "blue_score",
    "red_score",
    "blue_top",
    "blue_jg",
    "blue_mid",
    "blue_adc",
    "blue_sup",
    "red_top",
    "red_jg",
    "red_mid",
    "red_adc",
    "red_sup",
    "carry_players",
    "mvp_player",
    "lane_top",
    "lane_jg",
    "lane_mid",
    "lane_adc",
    "lane_sup",
    "notes",
]

EXPORT_PLAYER_COLUMNS = [
    "name",
    "display_name",
    "riot_game_name",
    "riot_tag_line",
    "solo_tier",
    "solo_rank",
    "league_points",
    "preferred_roles",
    "TOP",
    "JG",
    "MID",
    "ADC",
    "SUP",
]


def export_players_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for player in list_players(conn):
        row: dict[str, Any] = {
            "name": player.name,
            "display_name": player.label_name,
            "riot_game_name": player.riot_game_name or "",
            "riot_tag_line": player.riot_tag_line or "",
            "solo_tier": player.solo_tier,
            "solo_rank": player.solo_rank,
            "league_points": player.league_points,
            "flex_tier": player.flex_tier,
            "flex_rank": player.flex_rank,
            "flex_league_points": player.flex_league_points,
            "preferred_roles": "|".join(player.preferred_roles),
            "top_champions_json": json.dumps(player.lane_champions, ensure_ascii=False),
        }
        for role in ROLES:
            row[role] = round(player.rating_for(role), 3)
        rows.append(row)
    return rows


def _names_for_ids(conn: sqlite3.Connection, player_ids: Iterable[int]) -> dict[int, str]:
    ids = [int(x) for x in player_ids]
    if not ids:
        return {}
    if is_csv_conn(conn):
        players = _csv_players_by_id(conn)
        return {pid: players[pid].name for pid in ids if pid in players}
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(f"SELECT id, name FROM players WHERE id IN ({placeholders})", ids).fetchall()
    return {int(row["id"]): str(row["name"]) for row in rows}


def match_export_row(conn: sqlite3.Connection, match_id: int) -> dict[str, Any]:
    if is_csv_conn(conn):
        matches = {int(row["id"]): row for row in list_matches(conn, limit=100000)}
        match = matches.get(int(match_id))
        if match is None:
            raise ValueError(f"match not found: {match_id}")
        players = _csv_players_by_id(conn)
        participants = list_match_participants(conn, int(match_id))
        by_slot = {
            (str(row["team"]).lower(), str(row["role"]).lower()): players.get(int(row["player_id"]))
            for row in participants
        }
        carry_ids = [int(x) for x in json.loads(match["carry_player_ids_json"] or "[]")]
        carry_names = _names_for_ids(conn, carry_ids)
        mvp_player = ""
        if match["mvp_player_id"] is not None:
            mvp_player = _names_for_ids(conn, [int(match["mvp_player_id"])]).get(int(match["mvp_player_id"]), "")
        lane_impacts = json.loads(match["lane_impacts_json"] or "{}")
        row: dict[str, Any] = {
            "played_at": match["played_at"],
            "blue_win": "BLUE" if bool(match["blue_win"]) else "RED",
            "blue_score": int(match["blue_score"] or 0),
            "red_score": int(match["red_score"] or 0),
            "carry_players": "|".join(carry_names.get(pid, "") for pid in carry_ids if carry_names.get(pid, "")),
            "mvp_player": mvp_player,
            "notes": match["notes"] or "",
        }
        for team in ("blue", "red"):
            for role in ROLES:
                player = by_slot.get((team, role.lower()))
                row[f"{team}_{role.lower()}"] = player.name if player else ""
        for role in ROLES:
            row[f"lane_{role.lower()}"] = lane_impacts.get(role, "鍮꾨벑")
        return {column: row.get(column, "") for column in EXPORT_MATCH_COLUMNS}

    match = conn.execute("SELECT * FROM matches WHERE id = ?", (int(match_id),)).fetchone()
    if match is None:
        raise ValueError(f"match not found: {match_id}")

    participants = conn.execute(
        """
        SELECT mp.team, mp.role, p.name
        FROM match_participants mp
        JOIN players p ON p.id = mp.player_id
        WHERE mp.match_id = ?
        """,
        (int(match_id),),
    ).fetchall()

    by_slot = {(str(row["team"]).lower(), str(row["role"]).lower()): str(row["name"]) for row in participants}
    carry_ids = [int(x) for x in json.loads(match["carry_player_ids_json"] or "[]")]
    carry_names = _names_for_ids(conn, carry_ids)
    mvp_player = ""
    if match["mvp_player_id"] is not None:
        mvp_player = _names_for_ids(conn, [int(match["mvp_player_id"])]).get(int(match["mvp_player_id"]), "")
    lane_impacts = json.loads(match["lane_impacts_json"] or "{}")

    row: dict[str, Any] = {
        "played_at": match["played_at"],
        "blue_win": "BLUE" if bool(match["blue_win"]) else "RED",
        "blue_score": int(match["blue_score"] or 0),
        "red_score": int(match["red_score"] or 0),
        "carry_players": "|".join(carry_names.get(pid, "") for pid in carry_ids if carry_names.get(pid, "")),
        "mvp_player": mvp_player,
        "notes": match["notes"] or "",
    }
    for team in ("blue", "red"):
        for role in ROLES:
            row[f"{team}_{role.lower()}"] = by_slot.get((team, role.lower()), "")
    for role in ROLES:
        row[f"lane_{role.lower()}"] = lane_impacts.get(role, "비등")
    return {column: row.get(column, "") for column in EXPORT_MATCH_COLUMNS}


def export_matches_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    if is_csv_conn(conn):
        rows = sorted(list_matches(conn, limit=100000), key=lambda item: (str(item["played_at"]), int(item["id"])))
        return [match_export_row(conn, int(row["id"])) for row in rows]
    rows = conn.execute("SELECT id FROM matches ORDER BY played_at ASC, id ASC").fetchall()
    return [match_export_row(conn, int(row["id"])) for row in rows]


def append_match_export_csv(
    conn: sqlite3.Connection,
    match_id: int,
    csv_path: str | Path = Path("data/records/matches_saved.csv"),
) -> Path:
    """Append one saved match to an import-compatible CSV mirror.

    SQLite remains the source of truth.  This CSV is a convenience backup/export so
    the user can copy the newly recorded records into an external sheet if needed.
    """
    path = Path(csv_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    row = match_export_row(conn, int(match_id))
    write_header = not path.exists() or path.stat().st_size == 0
    with path.open("a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=EXPORT_MATCH_COLUMNS)
        if write_header:
            writer.writeheader()
        writer.writerow(row)
    return path


def update_match_played_at(conn, match_id: int, played_at: str) -> None:
    if is_csv_conn(conn):
        rows = _csv_read_dicts(conn.matches_path)
        for row in rows:
            if int(row.get("id") or 0) == int(match_id):
                row["played_at"] = played_at
                break
        _csv_write_dicts(conn.matches_path, CSV_MATCH_COLUMNS, rows)
        return
    conn.execute("UPDATE matches SET played_at = ? WHERE id = ?", (played_at, int(match_id)))
    conn.commit()
