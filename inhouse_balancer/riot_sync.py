"""Riot API synchronization utilities.

These helpers update public ranked priors, optional recent-position priors,
and lane-specific top champion pools from recent Match-V5 data.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any

from .constants import ROLES
from .importers import parse_riot_id
from .rating import initialize_role_ratings, tier_to_rating
from .riot_client import RiotAPIError, RiotClient
from .storage import ensure_column, list_players, update_player_riot_snapshot

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

# Summoner's Rift queues where lane/champion data is meaningful for this app.
# 400 normal draft, 420 solo/duo, 430 blind, 440 flex, 490 quickplay.
SR_QUEUE_IDS = {400, 420, 430, 440, 490}


@dataclass(slots=True)
class RiotSyncResult:
    player_name: str
    status: str
    message: str
    solo_tier: str | None = None
    solo_rank: str | None = None
    league_points: int | None = None
    flex_tier: str | None = None
    flex_rank: str | None = None
    flex_league_points: int | None = None
    inferred_roles: list[str] | None = None
    top_champions_by_role: dict[str, list[dict[str, Any]]] | None = None


def ensure_riot_columns(conn) -> None:
    """Add Riot metadata columns if the DB was created by an older MVP version."""
    if getattr(conn, "is_csv", False):
        return
    ensure_column(conn, "players", "puuid", "TEXT")
    ensure_column(conn, "players", "summoner_id", "TEXT")
    ensure_column(conn, "players", "profile_icon_id", "INTEGER DEFAULT 0")
    ensure_column(conn, "players", "summoner_level", "INTEGER DEFAULT 0")
    ensure_column(conn, "players", "flex_tier", "TEXT DEFAULT 'UNRANKED'")
    ensure_column(conn, "players", "flex_rank", "TEXT DEFAULT ''")
    ensure_column(conn, "players", "flex_league_points", "INTEGER DEFAULT 0")
    ensure_column(conn, "players", "top_champions_json", "TEXT NOT NULL DEFAULT '{}'")
    conn.commit()


def queue_entry(entries: list[dict[str, Any]], queue_type: str) -> dict[str, Any] | None:
    for entry in entries:
        if entry.get("queueType") == queue_type:
            return entry
    return None


def solo_queue_entry(entries: list[dict[str, Any]]) -> dict[str, Any] | None:
    return queue_entry(entries, "RANKED_SOLO_5x5")


def flex_queue_entry(entries: list[dict[str, Any]]) -> dict[str, Any] | None:
    return queue_entry(entries, "RANKED_FLEX_SR")


def rank_parts(entry: dict[str, Any] | None) -> tuple[str, str, int]:
    if entry is None:
        return "UNRANKED", "", 0
    return (
        str(entry.get("tier", "UNRANKED") or "UNRANKED").upper(),
        str(entry.get("rank", "") or "").upper(),
        int(entry.get("leaguePoints", 0) or 0),
    )


def blended_rank_rating(
    solo: dict[str, Any] | None,
    flex: dict[str, Any] | None,
    *,
    solo_weight: float = 0.75,
) -> float:
    """Combine solo/flex public ranks into one 0~100 prior.

    This is an app prior, not Riot MMR. Solo queue is weighted higher because it is
    usually the cleaner individual-skill signal; flex still contributes if present.
    """
    solo_rating = tier_to_rating(*rank_parts(solo)) if solo is not None else None
    flex_rating = tier_to_rating(*rank_parts(flex)) if flex is not None else None
    if solo_rating is not None and flex_rating is not None:
        w = max(0.0, min(1.0, float(solo_weight)))
        return float(solo_rating) * w + float(flex_rating) * (1.0 - w)
    if solo_rating is not None:
        return float(solo_rating)
    if flex_rating is not None:
        return float(flex_rating)
    return tier_to_rating("UNRANKED", "", 0)


def _participant_for_puuid(match: dict[str, Any], puuid: str) -> dict[str, Any] | None:
    participants = match.get("info", {}).get("participants", [])
    return next((p for p in participants if p.get("puuid") == puuid), None)


def _participant_role(participant: dict[str, Any]) -> str | None:
    raw_role = participant.get("teamPosition") or participant.get("individualPosition") or ""
    app_role = RIOT_TO_APP_ROLE.get(str(raw_role).upper())
    return app_role if app_role in ROLES else None


def _is_summoners_rift_match(match: dict[str, Any]) -> bool:
    info = match.get("info", {})
    queue_id = int(info.get("queueId", 0) or 0)
    map_id = int(info.get("mapId", 0) or 0)
    game_mode = str(info.get("gameMode", "") or "").upper()
    return queue_id in SR_QUEUE_IDS or (map_id == 11 and game_mode == "CLASSIC")


def infer_recent_roles(client: RiotClient, puuid: str, *, count: int = 20) -> list[str]:
    """Infer top roles from recent Match-V5 participant position fields."""
    if count <= 0:
        return []

    role_counts = {role: 0 for role in ROLES}
    match_ids = client.get_recent_match_ids(puuid, count=min(100, int(count)))
    for match_id in match_ids:
        match = client.get_match(match_id)
        if not _is_summoners_rift_match(match):
            continue
        participant = _participant_for_puuid(match, puuid)
        if not participant:
            continue
        app_role = _participant_role(participant)
        if app_role in role_counts:
            role_counts[app_role] += 1

    return [role for role, n in sorted(role_counts.items(), key=lambda kv: (-kv[1], ROLES.index(kv[0]))) if n > 0][:2]


def collect_top_champions_by_role(
    client: RiotClient,
    puuid: str,
    *,
    count: int = 40,
    top_n: int = 3,
) -> dict[str, list[dict[str, Any]]]:
    """Return top-N champion picks per role from recent Summoner's Rift matches."""
    if count <= 0:
        return {role: [] for role in ROLES}

    role_champ_counts: dict[str, Counter[str]] = {role: Counter() for role in ROLES}
    match_ids = client.get_recent_match_ids(puuid, count=min(100, int(count)))
    for match_id in match_ids:
        match = client.get_match(match_id)
        if not _is_summoners_rift_match(match):
            continue
        participant = _participant_for_puuid(match, puuid)
        if not participant:
            continue
        app_role = _participant_role(participant)
        champion = str(participant.get("championName") or "").strip()
        if app_role in role_champ_counts and champion:
            role_champ_counts[app_role][champion] += 1

    return {
        role: [
            {"champion": champion, "games": int(games)}
            for champion, games in role_champ_counts[role].most_common(int(top_n))
        ]
        for role in ROLES
    }


def refresh_player_from_riot(
    conn,
    client: RiotClient,
    player,
    *,
    reset_role_ratings: bool = False,
    infer_roles_count: int = 0,
    champion_pool_count: int = 40,
    solo_weight: float = 0.75,
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
        solo = solo_queue_entry(entries)
        flex = flex_queue_entry(entries)

        solo_tier, solo_rank, solo_lp = rank_parts(solo)
        flex_tier, flex_rank, flex_lp = rank_parts(flex)

        preferred_roles = list(player.preferred_roles)
        inferred_roles: list[str] = []
        if infer_roles_count > 0:
            inferred_roles = infer_recent_roles(client, puuid, count=infer_roles_count)
            if inferred_roles:
                preferred_roles = inferred_roles

        base_rating = blended_rank_rating(solo, flex, solo_weight=solo_weight)
        role_priors = initialize_role_ratings(base_rating, preferred_roles)
        top_champions_by_role = collect_top_champions_by_role(
            client,
            puuid,
            count=champion_pool_count,
            top_n=3,
        )

        update_player_riot_snapshot(
            conn,
            player_id=int(player.id),
            riot_game_name=game_name,
            riot_tag_line=tag_line,
            puuid=puuid,
            summoner_id=summoner.get("id"),
            profile_icon_id=int(summoner.get("profileIconId", 0) or 0),
            summoner_level=int(summoner.get("summonerLevel", 0) or 0),
            solo_tier=solo_tier,
            solo_rank=solo_rank,
            league_points=solo_lp,
            flex_tier=flex_tier,
            flex_rank=flex_rank,
            flex_league_points=flex_lp,
            base_rating=base_rating,
            preferred_roles=preferred_roles,
            role_priors=role_priors,
            top_champions_by_role=top_champions_by_role,
            reset_role_ratings=reset_role_ratings,
        )

        return RiotSyncResult(
            player.name,
            "updated",
            f"솔랭 {solo_tier} {solo_rank} {solo_lp}LP / 자유랭크 {flex_tier} {flex_rank} {flex_lp}LP 반영",
            solo_tier,
            solo_rank,
            solo_lp,
            flex_tier,
            flex_rank,
            flex_lp,
            inferred_roles,
            top_champions_by_role,
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
    champion_pool_count: int = 40,
    solo_weight: float = 0.75,
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
                champion_pool_count=champion_pool_count,
                solo_weight=solo_weight,
            )
        )
    return results
