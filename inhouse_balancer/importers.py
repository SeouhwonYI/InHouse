"""CSV import helpers for historical in-house data.

The importer intentionally uses the same rating engine as the Streamlit app.  This
means historical matches should be imported in chronological order so that each game
updates the player-role ratings before the next game is replayed.
"""
from __future__ import annotations

import csv
import io
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .constants import LANE_IMPACT_LABELS, ROLES
from .models import Player, TeamAssignment
from .rating import initialize_role_ratings, tier_to_rating
from .storage import create_player, delete_all_data, list_players, record_match_and_update


ROLE_ALIASES: dict[str, str] = {
    "TOP": "TOP",
    "TOPLANE": "TOP",
    "TOP_LANE": "TOP",
    "탑": "TOP",
    "JG": "JG",
    "JGL": "JG",
    "JUNGLE": "JG",
    "정글": "JG",
    "MID": "MID",
    "MIDDLE": "MID",
    "미드": "MID",
    "ADC": "ADC",
    "BOT": "ADC",
    "BOTTOM": "ADC",
    "원딜": "ADC",
    "바텀": "ADC",
    "SUP": "SUP",
    "SUPPORT": "SUP",
    "UTILITY": "SUP",
    "서폿": "SUP",
    "서포터": "SUP",
}

LANE_IMPACT_ALIASES: dict[str, str] = {
    "압승": "압승",
    "HARD_WIN": "압승",
    "STOMP": "압승",
    "우세": "우세",
    "WIN": "우세",
    "AHEAD": "우세",
    "비등": "비등",
    "EVEN": "비등",
    "DRAW": "비등",
    "열세": "열세",
    "LOSE": "열세",
    "BEHIND": "열세",
}

TRUE_VALUES = {"1", "TRUE", "T", "Y", "YES", "WIN", "BLUE", "BLUE_WIN", "승", "승리", "블루", "BLUE TEAM"}
FALSE_VALUES = {"0", "FALSE", "F", "N", "NO", "LOSE", "LOSS", "RED", "RED_WIN", "패", "패배", "레드", "RED TEAM"}


@dataclass(slots=True)
class ImportReport:
    imported: int = 0
    skipped: int = 0
    errors: list[str] | None = None

    def __post_init__(self) -> None:
        if self.errors is None:
            self.errors = []

    def add_error(self, message: str) -> None:
        self.skipped += 1
        self.errors.append(message)


def _get(row: dict[str, Any], *keys: str, default: str = "") -> str:
    """Fetch a CSV value from one of several possible header names."""
    for key in keys:
        if key in row and row[key] is not None:
            return str(row[key]).strip()
    lower = {str(k).strip().lower(): v for k, v in row.items()}
    for key in keys:
        value = lower.get(key.lower())
        if value is not None:
            return str(value).strip()
    return default


def parse_role(value: str) -> str:
    role = ROLE_ALIASES.get(str(value or "").strip().upper())
    if role not in ROLES:
        raise ValueError(f"unknown role: {value!r}")
    return role


def parse_roles(value: str) -> list[str]:
    if not value:
        return []
    tokens = [x.strip() for x in value.replace("/", "|").replace(",", "|").split("|")]
    roles: list[str] = []
    for token in tokens:
        if not token:
            continue
        role = parse_role(token)
        if role not in roles:
            roles.append(role)
    return roles


def parse_bool(value: str) -> bool:
    token = str(value or "").strip().upper()
    if token in TRUE_VALUES:
        return True
    if token in FALSE_VALUES:
        return False
    raise ValueError(f"cannot parse boolean/winner value: {value!r}")


def parse_lane_impact(value: str) -> str:
    if not value:
        return "비등"
    label = LANE_IMPACT_ALIASES.get(str(value).strip().upper(), str(value).strip())
    if label not in LANE_IMPACT_LABELS:
        raise ValueError(f"unknown lane impact: {value!r}")
    return label


def split_names(value: str) -> list[str]:
    if not value:
        return []
    normalized = value.replace(";", "|").replace(",", "|").replace("/", "|")
    return [x.strip() for x in normalized.split("|") if x.strip()]


def parse_riot_id(value: str) -> tuple[str | None, str | None]:
    if not value or "#" not in value:
        return None, None
    game_name, tag_line = value.rsplit("#", 1)
    game_name = game_name.strip()
    tag_line = tag_line.strip()
    if not game_name or not tag_line:
        return None, None
    return game_name, tag_line


def _clean_header(value: Any) -> str:
    return str(value or "").replace("\ufeff", "").strip()


def _clean_cell(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    # pandas may read empty spreadsheet cells as the string "nan" when dtype is not strict.
    if text.lower() == "nan":
        return ""
    return text


def _normalize_rows(rows: Iterable[dict[Any, Any]]) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    for row in rows:
        normalized.append({_clean_header(k): _clean_cell(v) for k, v in row.items() if _clean_header(k)})
    return normalized


def _decode_text_with_fallback(raw: bytes, csv_path: str | Path) -> tuple[str, str]:
    """Decode uploaded CSV bytes.

    Excel on Korean Windows often saves "CSV (Comma delimited)" as CP949/EUC-KR,
    while "CSV UTF-8" uses UTF-8 with BOM.  Supporting both avoids the common
    `UnicodeDecodeError: utf-8 codec can't decode byte 0xc0` import failure.
    """
    candidates = ("utf-8-sig", "utf-8", "cp949", "euc-kr")
    last_error: UnicodeDecodeError | None = None
    for encoding in candidates:
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError as exc:
            last_error = exc
    raise UnicodeDecodeError(
        last_error.encoding if last_error else "unknown",
        last_error.object if last_error else raw,
        last_error.start if last_error else 0,
        last_error.end if last_error else 1,
        f"Could not decode {csv_path}. Save as CSV UTF-8 or use .xlsx.",
    )


def load_csv_rows(csv_path: str | Path) -> list[dict[str, str]]:
    """Load CSV/XLSX rows with Korean Excel-friendly encoding fallback.

    Despite the historical name, this function also accepts .xlsx/.xls files.
    CSV files are decoded as UTF-8 first, then CP949/EUC-KR for Excel exports
    from Korean Windows.
    """
    path = Path(csv_path)
    suffix = path.suffix.lower()

    if suffix in {".xlsx", ".xls"}:
        try:
            import pandas as pd
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError("Excel import requires pandas and openpyxl. Run: pip install -r requirements.txt") from exc
        df = pd.read_excel(path, dtype=str, keep_default_na=False)
        df.columns = [_clean_header(c) for c in df.columns]
        rows = df.to_dict(orient="records")
        if not list(df.columns):
            raise ValueError(f"Spreadsheet has no header row: {csv_path}")
        return _normalize_rows(rows)

    raw = path.read_bytes()
    text, _encoding = _decode_text_with_fallback(raw, csv_path)
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;")
    except csv.Error:
        dialect = csv.excel

    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    if not reader.fieldnames:
        raise ValueError(f"CSV has no header row: {csv_path}")
    return _normalize_rows(dict(row) for row in reader)



def infer_preferred_roles_from_match_rows(
    match_rows: Iterable[dict[str, Any]],
    *,
    max_roles: int = 2,
) -> dict[str, list[str]]:
    """Infer player preferred roles from historical match slot usage.

    This is useful for the very first real-data bootstrap: if `players.csv` has an
    empty `preferred_roles` cell, the app can use prior in-house records to set a
    sensible initial main/sub role before replaying match results.
    """
    counts: dict[str, Counter[str]] = defaultdict(Counter)
    for row in match_rows:
        for team in ("BLUE", "RED"):
            for role in ROLES:
                player_name = _get(
                    row,
                    f"{team.lower()}_{role.lower()}",
                    f"{team}_{role}",
                    f"{team.lower()}{role.lower()}",
                )
                if player_name:
                    counts[player_name][role] += 1

    inferred: dict[str, list[str]] = {}
    for name, counter in counts.items():
        ranked = sorted(counter.items(), key=lambda item: (-item[1], ROLES.index(item[0])))
        inferred[name] = [role for role, _count in ranked[:max(1, int(max_roles))]]
    return inferred


def _import_player_rows(conn, rows: list[dict[str, str]], *, source_label: str = "players CSV") -> ImportReport:
    report = ImportReport()

    for line_no, row in enumerate(rows, start=2):
        try:
            name = _get(row, "name", "riot_id", "riot id", "riotID", "summoner", "소환사", "플레이어")
            if not name:
                raise ValueError("missing name")
            display_name = _get(row, "display_name", "display", "alias", "nickname", "이름", "별명") or name

            riot_id = _get(row, "riot_id", "riot id", "riotID")
            riot_game_name = _get(row, "riot_game_name", "game_name", "gameName")
            riot_tag_line = _get(row, "riot_tag_line", "tag_line", "tagLine", "tag")
            if not (riot_game_name and riot_tag_line):
                parsed_game_name, parsed_tag_line = parse_riot_id(riot_id or name)
                riot_game_name = riot_game_name or parsed_game_name or None
                riot_tag_line = riot_tag_line or parsed_tag_line or None

            preferred_roles = parse_roles(_get(row, "preferred_roles", "preferred", "roles", "선호포지션", "선호"))
            solo_tier = _get(row, "solo_tier", "tier", "티어", default="UNRANKED").upper() or "UNRANKED"
            solo_rank = _get(row, "solo_rank", "rank", "랭크", default="").upper()
            lp_text = _get(row, "league_points", "lp", "LP", default="0") or "0"
            league_points = int(float(lp_text))

            solo_rating = tier_to_rating(solo_tier, solo_rank, league_points)
            role_ratings = initialize_role_ratings(solo_rating, preferred_roles)
            any_explicit_rating = False
            for role in ROLES:
                value = _get(row, role, role.lower(), default="")
                if value != "":
                    role_ratings[role] = float(value)
                    any_explicit_rating = True

            create_player(
                conn,
                name=name,
                preferred_roles=preferred_roles,
                solo_tier=solo_tier,
                solo_rank=solo_rank,
                league_points=league_points,
                role_ratings=role_ratings if any_explicit_rating else None,
                display_name=display_name,
                riot_game_name=riot_game_name,
                riot_tag_line=riot_tag_line,
            )
            report.imported += 1
        except Exception as exc:  # noqa: BLE001 - report every bad CSV row without aborting the whole import
            report.add_error(f"{source_label} line {line_no}: {exc}")

    return report


def import_players_csv(conn, csv_path: str | Path) -> ImportReport:
    """Import or update players from CSV/XLSX.

    Required: `name`
    Optional: `display_name`, `riot_game_name`, `riot_tag_line`, `riot_id`, `solo_tier`, `solo_rank`,
    `league_points`, `preferred_roles`, and role rating columns: `TOP`, `JG`, `MID`,
    `ADC`, `SUP`.
    """
    return _import_player_rows(conn, load_csv_rows(csv_path))


def bootstrap_from_records(
    conn,
    players_path: str | Path,
    matches_path: str | Path | None = None,
    *,
    reset_all: bool = True,
    infer_missing_preferences: bool = True,
    inferred_preference_slots: int = 2,
) -> dict[str, Any]:
    """Create a clean initial DB from real players and historical matches.

    Flow:
      1. Optionally delete all current DB data, including demo players.
      2. Import players from `players_path`.
      3. If `matches_path` is supplied, replay the historical matches in chronological
         order using the same rating engine as the app.

    If a player's `preferred_roles` cell is empty and `infer_missing_preferences=True`,
    the function uses the historical match slots to infer that player's main/sub role
    before importing players. Existing preferred_roles values are never overwritten.
    """
    if reset_all:
        delete_all_data(conn)

    player_rows = load_csv_rows(players_path)
    match_rows = load_csv_rows(matches_path) if matches_path else []
    inferred: dict[str, list[str]] = {}
    applied_inferred: dict[str, list[str]] = {}

    if infer_missing_preferences and match_rows:
        inferred = infer_preferred_roles_from_match_rows(
            match_rows,
            max_roles=max(1, int(inferred_preference_slots)),
        )
        for row in player_rows:
            name = _get(row, "name", "riot_id", "riot id", "riotID", "summoner", "소환사", "플레이어")
            current_pref = _get(row, "preferred_roles", "preferred", "roles", "선호포지션", "선호")
            if name and not current_pref and inferred.get(name):
                row["preferred_roles"] = "|".join(inferred[name])
                applied_inferred[name] = inferred[name]

    players_report = _import_player_rows(conn, player_rows, source_label="players bootstrap")
    matches_report = None
    if matches_path:
        matches_report = import_matches_csv(conn, matches_path)

    return {
        "players": players_report,
        "matches": matches_report,
        "inferred_preferences": applied_inferred,
        "reset_all": reset_all,
    }

def _players_by_name(conn) -> dict[str, Player]:
    players = list_players(conn)
    lookup: dict[str, Player] = {}
    for player in players:
        lookup[player.name] = player
        lookup[player.name.lower()] = player
        if player.display_name:
            lookup[player.display_name] = player
            lookup[player.display_name.lower()] = player
        lookup[player.riot_id] = player
        lookup[player.riot_id.lower()] = player
    return lookup


def _require_player(lookup: dict[str, Player], name: str) -> Player:
    key = str(name or "").strip()
    if not key:
        raise ValueError("empty player name")
    player = lookup.get(key) or lookup.get(key.lower())
    if player is None:
        raise ValueError(f"player not found in DB: {name!r}")
    return player


def _team_assignment_from_row(row: dict[str, str], lookup: dict[str, Player], team: str) -> TeamAssignment:
    slots: dict[str, Player] = {}
    for role in ROLES:
        col = f"{team.lower()}_{role.lower()}"
        alt_col = f"{team}_{role}"
        player_name = _get(row, col, alt_col, f"{team.lower()}{role.lower()}")
        slots[role] = _require_player(lookup, player_name)

    total_rating = sum(slots[role].rating_for(role) for role in ROLES)
    preference_penalty = sum(slots[role].preference_penalty(role) for role in ROLES)
    return TeamAssignment(team=team.upper(), slots=slots, total_rating=total_rating, preference_penalty=preference_penalty)


def _ids_for_names(lookup: dict[str, Player], names: Iterable[str]) -> list[int]:
    ids: list[int] = []
    for name in names:
        player = _require_player(lookup, name)
        if player.id is None:
            raise ValueError(f"player has no DB id: {name}")
        if int(player.id) not in ids:
            ids.append(int(player.id))
    return ids


def import_matches_csv(conn, csv_path: str | Path) -> ImportReport:
    """Replay historical matches from CSV and update ratings.

    Required columns:
    `blue_win`, `blue_top`, `blue_jg`, `blue_mid`, `blue_adc`, `blue_sup`,
    `red_top`, `red_jg`, `red_mid`, `red_adc`, `red_sup`.

    Optional columns:
    `played_at`, `blue_score`, `red_score`, `carry_players`, `mvp_player`,
    `lane_top`, `lane_jg`, `lane_mid`, `lane_adc`, `lane_sup`, `notes`.
    """
    rows = load_csv_rows(csv_path)

    # Historical replay should be chronological. Empty dates remain in file order after
    # dated rows because Python's sort is stable.
    rows_with_index = list(enumerate(rows, start=2))
    rows_with_index.sort(key=lambda item: (_get(item[1], "played_at", "date", "일시", default=""), item[0]))

    report = ImportReport()
    for line_no, row in rows_with_index:
        try:
            lookup = _players_by_name(conn)
            blue = _team_assignment_from_row(row, lookup, "BLUE")
            red = _team_assignment_from_row(row, lookup, "RED")

            blue_win = parse_bool(_get(row, "blue_win", "winner", "승리팀", "result"))
            blue_score = int(float(_get(row, "blue_score", "blueScore", default="1" if blue_win else "0") or 0))
            red_score = int(float(_get(row, "red_score", "redScore", default="0" if blue_win else "1") or 0))
            carry_player_names = split_names(_get(row, "carry_players", "carries", "carry", "캐리", "잘한플레이어"))[:5]
            carry_ids = _ids_for_names(lookup, carry_player_names)
            mvp_name = _get(row, "mvp_player", "mvp", "MVP")
            mvp_id = _ids_for_names(lookup, [mvp_name])[0] if mvp_name else None
            lane_impacts = {
                role: parse_lane_impact(_get(row, f"lane_{role.lower()}", f"{role}_lane", f"라인_{role}", default="비등"))
                for role in ROLES
            }
            notes = _get(row, "notes", "memo", "메모")

            match_id, _changes = record_match_and_update(
                conn,
                blue=blue,
                red=red,
                blue_win=blue_win,
                blue_score=blue_score,
                red_score=red_score,
                carry_player_ids=carry_ids,
                mvp_player_id=mvp_id,
                lane_impacts=lane_impacts,
                notes=notes,
                append_csv_log=False,
            )

            played_at = _get(row, "played_at", "date", "일시")
            if played_at:
                conn.execute("UPDATE matches SET played_at = ? WHERE id = ?", (played_at, int(match_id)))
                conn.commit()

            report.imported += 1
        except Exception as exc:  # noqa: BLE001
            report.add_error(f"matches CSV line {line_no}: {exc}")

    return report
