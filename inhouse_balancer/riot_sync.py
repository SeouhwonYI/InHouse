"""Riot API synchronization utilities.

These helpers update only public ranked priors and optional recent-position priors.
They do not automatically import custom/in-house match histories from Riot.  Historical
in-house games should be replayed through the CSV importer so carry/MVP/lane-impact
signals can be preserved.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .constants import ROLES
from .importers import parse_riot_id
from .rating import initialize_role_ratings, tier_to_rating
from .riot_client import RiotAPIError, RiotClient
from .storage import ensure_column, list_players

RIOT_TO_APP_ROLE = {
    "TOP": "TOP",
    "JUNGLE": "JG",
    "MIDDLE": "MID",
    "MID": "MID",
    "BOTTOM": "ADC",
    "BOT": "ADC",
    "UTILITY": "SUP",
    "SUPPORT": "SUP",
}


@dataclass(slots=True)
class RiotSyncResult:
    player_name: str
    status: str
    message: str
    solo_tier: str | None = None
    solo_rank: str | None = None
    league_points: int | None = None
    inferred_roles: list[str] | None = None


def ensure_riot_columns(conn) -> None:
    """Add Riot metadata columns if the DB was created by an older MVP version."""
    ensure_column(conn, "players", "puuid", "TEXT")
    ensure_column(conn, "players", "summoner_id", "TEXT")
    ensure_column(conn, "players", "profile_icon_id", "INTEGER DEFAULT 0")
    ensure_column(conn, "players", "summoner_level", "INTEGER DEFAULT 0")
    conn.commit()


def solo_queue_entry(entries: list[dict[str, Any]]) -> dict[str, Any] | None:
    for entry in entries:
        if entry.get("queueType") == "RANKED_SOLO_5x5":
            return entry
    return None


def flex_queue_entry(entries: list[dict[str, Any]]) -> dict[str, Any] | None:
    for entry in entries:
        if entry.get("queueType") == "RANKED_FLEX_SR":
            return entry
    return None


def infer_recent_roles(client: RiotClient, puuid: str, *, count: int = 20) -> list[str]:
    """Infer top roles from recent Match-V5 participant position fields."""
    if count <= 0:
        return []

    role_counts = {role: 0 for role in ROLES}
    match_ids = client.get_recent_match_ids(puuid, count=min(100, int(count)))
    for match_id in match_ids:
        match = client.get_match(match_id)
        participants = match.get("info", {}).get("participants", [])
        participant = next((p for p in participants if p.get("puuid") == puuid), None)
        if not participant:
            continue
        raw_role = participant.get("teamPosition") or participant.get("individualPosition") or ""
        app_role = RIOT_TO_APP_ROLE.get(str(raw_role).upper())
        if app_role in role_counts:
            role_counts[app_role] += 1

    return [role for role, n in sorted(role_counts.items(), key=lambda kv: (-kv[1], ROLES.index(kv[0]))) if n > 0][:2]


def refresh_player_from_riot(
    conn,
    client: RiotClient,
    player,
    *,
    reset_role_ratings: bool = False,
    infer_roles_count: int = 0,
) -> RiotSyncResult:
    ensure_riot_columns(conn)

    game_name = player.riot_game_name
    tag_line = player.riot_tag_line
    if not (game_name and tag_line):
        parsed_game_name, parsed_tag_line = parse_riot_id(player.name)
        game_name = game_name or parsed_game_name
        tag_line = tag_line or parsed_tag_line

    if not (game_name and tag_line):
        return RiotSyncResult(player.name, "skipped", "Riot ID가 없습니다. 예: 이름#KR1")

    try:
        account = client.get_account_by_riot_id(game_name, tag_line)
        puuid = account["puuid"]
        summoner = client.get_summoner_by_puuid(puuid)
        entries = client.get_rank_entries_by_puuid(puuid)
        ranked = solo_queue_entry(entries) or flex_queue_entry(entries)

        if ranked is None:
            tier = "UNRANKED"
            rank = ""
            lp = 0
        else:
            tier = str(ranked.get("tier", "UNRANKED")).upper()
            rank = str(ranked.get("rank", "")).upper()
            lp = int(ranked.get("leaguePoints", 0) or 0)

        preferred_roles = list(player.preferred_roles)
        inferred_roles: list[str] = []
        if infer_roles_count > 0:
            inferred_roles = infer_recent_roles(client, puuid, count=infer_roles_count)
            if inferred_roles:
                preferred_roles = inferred_roles

        base_rating = tier_to_rating(tier, rank, lp)
        role_priors = initialize_role_ratings(base_rating, preferred_roles)

        with conn:
            conn.execute(
                """
                UPDATE players
                SET riot_game_name = ?, riot_tag_line = ?, puuid = ?, summoner_id = ?,
                    profile_icon_id = ?, summoner_level = ?, solo_tier = ?, solo_rank = ?,
                    league_points = ?, base_rating = ?, preferred_roles_json = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    game_name,
                    tag_line,
                    puuid,
                    summoner.get("id"),
                    int(summoner.get("profileIconId", 0) or 0),
                    int(summoner.get("summonerLevel", 0) or 0),
                    tier,
                    rank,
                    int(lp),
                    float(base_rating),
                    __import__("json").dumps(preferred_roles, ensure_ascii=False),
                    int(player.id),
                ),
            )
            for role in ROLES:
                # Preserve already-learned in-house evidence unless the user explicitly resets.
                row = conn.execute(
                    "SELECT games_played FROM player_role_ratings WHERE player_id = ? AND role = ?",
                    (int(player.id), role),
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
                        (int(player.id), role, float(role_priors[role]), bool(reset_role_ratings), bool(reset_role_ratings)),
                    )

        return RiotSyncResult(
            player.name,
            "updated",
            f"{tier} {rank} {lp}LP로 갱신",
            tier,
            rank,
            lp,
            inferred_roles,
        )
    except RiotAPIError as exc:
        return RiotSyncResult(player.name, "error", str(exc))
    except Exception as exc:  # noqa: BLE001
        return RiotSyncResult(player.name, "error", f"unexpected error: {exc}")


def refresh_all_players_from_riot(
    conn,
    client: RiotClient,
    *,
    reset_role_ratings: bool = False,
    infer_roles_count: int = 0,
    only_names: set[str] | None = None,
) -> list[RiotSyncResult]:
    players = list_players(conn)
    results: list[RiotSyncResult] = []
    for player in players:
        if only_names and player.name not in only_names and player.riot_id not in only_names:
            continue
        results.append(
            refresh_player_from_riot(
                conn,
                client,
                player,
                reset_role_ratings=reset_role_ratings,
                infer_roles_count=infer_roles_count,
            )
        )
    return results
