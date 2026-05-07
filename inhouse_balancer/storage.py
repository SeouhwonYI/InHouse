"""Persistence layer for the in-house balancer.

The app keeps SQLite as a local-development fallback, but production deployments can
use PostgreSQL by setting DATABASE_URL, e.g.

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
from pathlib import Path
from typing import Any, Iterable

from .constants import DEFAULT_CONFIG, ROLES
from .exports import DEFAULT_MATCH_LOG_PATH, append_match_csv_log
from .models import Player, RoleRating, TeamAssignment
from .rating import clamp, compute_rating_preview, initialize_role_ratings, tier_to_rating

DB_PATH = Path("data/inhouse_balancer.sqlite")


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


def connect(
    db_path: str | Path = DB_PATH,
    *,
    database_url: str | None = None,
):
    """Open either PostgreSQL or SQLite.

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

    path = Path(db_path)
    if str(path) != ":memory":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn) -> None:
    if is_postgres_conn(conn):
        _init_postgres_db(conn)
    else:
        _init_sqlite_db(conn)
    ensure_column(conn, "players", "display_name", "TEXT")
    conn.commit()


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
            base_rating REAL NOT NULL DEFAULT 50.0,
            preferred_roles_json TEXT NOT NULL DEFAULT '[]',
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
            base_rating DOUBLE PRECISION NOT NULL DEFAULT 50.0,
            preferred_roles_json TEXT NOT NULL DEFAULT '[]',
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
        base_rating=float(row["base_rating"]),
        preferred_roles=_json_loads_list(row["preferred_roles_json"]),
        role_ratings=ratings,
    )


def create_player(
    conn: sqlite3.Connection,
    *,
    name: str,
    preferred_roles: Iterable[str] = (),
    solo_tier: str = "UNRANKED",
    solo_rank: str = "",
    league_points: int = 0,
    role_ratings: dict[str, float] | None = None,
    display_name: str | None = None,
    riot_game_name: str | None = None,
    riot_tag_line: str | None = None,
) -> int:
    preferred = [role.upper() for role in preferred_roles if role.upper() in ROLES]
    display_name = (display_name or name).strip()
    base_rating = tier_to_rating(solo_tier, solo_rank, league_points)
    if role_ratings is None:
        role_ratings = initialize_role_ratings(base_rating, preferred)

    cur = conn.execute(
        """
        INSERT INTO players (
            name, display_name, riot_game_name, riot_tag_line, solo_tier, solo_rank,
            league_points, base_rating, preferred_roles_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET
            display_name = excluded.display_name,
            riot_game_name = excluded.riot_game_name,
            riot_tag_line = excluded.riot_tag_line,
            solo_tier = excluded.solo_tier,
            solo_rank = excluded.solo_rank,
            league_points = excluded.league_points,
            base_rating = excluded.base_rating,
            preferred_roles_json = excluded.preferred_roles_json,
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
            float(base_rating),
            json.dumps(preferred, ensure_ascii=False),
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


def list_players(conn: sqlite3.Connection) -> list[Player]:
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
    return int(conn.execute("SELECT COUNT(*) AS c FROM players").fetchone()["c"])


def delete_all_data(conn) -> None:
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
            "preferred_roles": "|".join(player.preferred_roles),
        }
        for role in ROLES:
            row[role] = round(player.rating_for(role), 3)
        rows.append(row)
    return rows


def _names_for_ids(conn: sqlite3.Connection, player_ids: Iterable[int]) -> dict[int, str]:
    ids = [int(x) for x in player_ids]
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(f"SELECT id, name FROM players WHERE id IN ({placeholders})", ids).fetchall()
    return {int(row["id"]): str(row["name"]) for row in rows}


def match_export_row(conn: sqlite3.Connection, match_id: int) -> dict[str, Any]:
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
