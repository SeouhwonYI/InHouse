"""CSV export/archive helpers.

SQLite is the source of truth.  The CSV log is a human-readable append-only archive
for matches that are saved through the app after team generation.
"""
from __future__ import annotations

import csv
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Iterable

from .constants import ROLES
from .models import TeamAssignment

DEFAULT_MATCH_LOG_PATH = Path("data/records/match_results.csv")

MATCH_LOG_COLUMNS = [
    "recorded_at",
    "match_id",
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
    "blue_rating_before",
    "red_rating_before",
    "expected_blue_win",
]


def _name_for_id_from_assignments(player_id: int | None, assignments: Iterable[TeamAssignment]) -> str:
    if player_id is None:
        return ""
    target = int(player_id)
    for assignment in assignments:
        for player in assignment.slots.values():
            if player.id is not None and int(player.id) == target:
                return player.name
    return ""


def append_match_csv_log(
    conn: sqlite3.Connection,
    *,
    match_id: int,
    blue: TeamAssignment,
    red: TeamAssignment,
    blue_win: bool,
    blue_score: int,
    red_score: int,
    carry_player_ids: Iterable[int] | None = None,
    mvp_player_id: int | None = None,
    lane_impacts: dict[str, str] | None = None,
    notes: str = "",
    path: str | Path = DEFAULT_MATCH_LOG_PATH,
) -> Path:
    """Append one saved match to data/records/match_results.csv.

    Historical replay imports should normally call `record_match_and_update(...,
    append_csv_log=False)` to avoid duplicating the original historical CSV. New
    matches saved through the UI use this append log by default.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    if hasattr(conn, "get_match_row"):
        match_row = conn.get_match_row(int(match_id))
    else:
        match_row = conn.execute(
            """
            SELECT played_at, blue_rating_before, red_rating_before, expected_blue_win
            FROM matches
            WHERE id = ?
            """,
            (int(match_id),),
        ).fetchone()
    played_at = match_row["played_at"] if match_row is not None else ""
    blue_rating_before = match_row["blue_rating_before"] if match_row is not None else blue.total_rating
    red_rating_before = match_row["red_rating_before"] if match_row is not None else red.total_rating
    expected_blue_win = match_row["expected_blue_win"] if match_row is not None else ""

    carry_names = [
        _name_for_id_from_assignments(pid, (blue, red))
        for pid in (carry_player_ids or [])
    ]
    carry_names = [name for name in carry_names if name]
    mvp_name = _name_for_id_from_assignments(mvp_player_id, (blue, red))
    lane_impacts = lane_impacts or {role: "비등" for role in ROLES}

    row: dict[str, object] = {
        "recorded_at": datetime.now().isoformat(timespec="seconds"),
        "match_id": int(match_id),
        "played_at": played_at,
        "blue_win": "BLUE" if blue_win else "RED",
        "blue_score": int(blue_score),
        "red_score": int(red_score),
        "carry_players": "|".join(carry_names),
        "mvp_player": mvp_name,
        "notes": notes,
        "blue_rating_before": round(float(blue_rating_before), 3),
        "red_rating_before": round(float(red_rating_before), 3),
        "expected_blue_win": round(float(expected_blue_win), 6) if expected_blue_win != "" else "",
    }
    for role in ROLES:
        row[f"blue_{role.lower()}"] = blue.slots[role].name
        row[f"red_{role.lower()}"] = red.slots[role].name
        row[f"lane_{role.lower()}"] = lane_impacts.get(role, "비등")

    file_exists = target.exists() and target.stat().st_size > 0
    with target.open("a", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=MATCH_LOG_COLUMNS, extrasaction="ignore")
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)
    return target
