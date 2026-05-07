"""Role-specific rating initialization and match update logic."""
from __future__ import annotations

import math
from typing import Iterable

from .constants import (
    DEFAULT_CONFIG,
    LANE_IMPACT_SCORES,
    RANK_OFFSET,
    ROLES,
    TIER_BASE_RATING,
)
from .models import RatingChange, TeamAssignment


def clamp(value: float, lower: float = 1.0, upper: float = 100.0) -> float:
    return max(lower, min(upper, value))


def tier_to_rating(tier: str, rank: str = "", league_points: int = 0) -> float:
    """Convert public ranked tier into the MVP's internal 0~100 prior.

    This is not Riot MMR. It is just a prior used before enough in-house data exists.
    """
    tier = (tier or "UNRANKED").upper()
    rank = (rank or "").upper()
    base = TIER_BASE_RATING.get(tier, TIER_BASE_RATING["UNRANKED"])
    offset = RANK_OFFSET.get(rank, 0.0)
    lp_offset = max(0, min(100, int(league_points or 0))) / 100.0

    if tier in {"MASTER", "GRANDMASTER", "CHALLENGER"}:
        # High-tier LP can be large. Compress it aggressively to avoid runaway priors.
        lp_offset = min(max(0, int(league_points or 0)), 1200) / 240.0

    return clamp(base + offset + lp_offset)


def rating_to_tier_label(rating: float) -> str:
    """Approximate display label for a 0~100 rating."""
    rating = clamp(rating)
    if rating >= 97:
        return "C"
    if rating >= 93:
        return "GM"
    if rating >= 88:
        return "M"
    if rating >= 83:
        return "D1"
    if rating >= 80:
        return "D2"
    if rating >= 77:
        return "D3"
    if rating >= 74:
        return "D4"
    if rating >= 71:
        return "E1"
    if rating >= 68:
        return "E2"
    if rating >= 65:
        return "P1"
    if rating >= 61:
        return "P2"
    if rating >= 57:
        return "G1"
    if rating >= 53:
        return "G2"
    if rating >= 48:
        return "S"
    if rating >= 38:
        return "B"
    return "I"


def initialize_role_ratings(
    solo_rating: float,
    preferred_roles: Iterable[str],
    *,
    unknown_offrole_penalty: float = 8.0,
    secondary_penalty: float = 4.0,
) -> dict[str, float]:
    """Create role priors from solo queue tier and declared preferred roles."""
    preferred = [r.upper() for r in preferred_roles if r.upper() in ROLES]
    ratings: dict[str, float] = {}
    for role in ROLES:
        if preferred and role == preferred[0]:
            penalty = 0.0
        elif role in preferred:
            penalty = secondary_penalty
        else:
            penalty = unknown_offrole_penalty
        ratings[role] = clamp(float(solo_rating) - penalty)
    return ratings


def expected_win_probability(blue_total: float, red_total: float, *, scale: float | None = None) -> float:
    """Expected Blue win probability from summed role ratings.

    The formula is Elo-like, but the input ratings are a 0~100 internal skill estimate.
    """
    scale = float(scale or DEFAULT_CONFIG["winrate_scale"])
    return 1.0 / (1.0 + 10.0 ** (-(blue_total - red_total) / scale))


def games_to_confidence(games_played: int) -> float:
    """Confidence grows quickly in the first few games and then saturates."""
    games_played = max(0, games_played)
    return round(min(0.95, 0.35 + 0.13 * math.sqrt(games_played)), 3)


def personal_k_factor(k_factor: float, games_played: int) -> float:
    """Reduce update size as a player-role pair accumulates evidence."""
    games_played = max(0, games_played)
    return k_factor / math.sqrt(1.0 + games_played / 8.0)


def compute_rating_preview(
    blue: TeamAssignment,
    red: TeamAssignment,
    *,
    blue_win: bool,
    carry_player_ids: Iterable[int] | None = None,
    mvp_player_id: int | None = None,
    lane_impacts: dict[str, str] | None = None,
    config: dict[str, float] | None = None,
) -> list[RatingChange]:
    """Compute per-player role-rating changes without mutating the players.

    `lane_impacts` is interpreted from the winning team's perspective:
    - 압승/우세: winning team's player in that role gets an extra positive update.
    - 열세: winning team's player receives a small negative lane adjustment, and the
      losing team's opposite role receives a partial positive correction.
    """
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    carry_ids = {int(x) for x in (carry_player_ids or []) if x is not None}
    lane_impacts = lane_impacts or {role: "비등" for role in ROLES}

    p_blue = expected_win_probability(
        blue.total_rating,
        red.total_rating,
        scale=cfg["winrate_scale"],
    )
    winner_team = "BLUE" if blue_win else "RED"
    changes: list[RatingChange] = []

    for assignment in (blue, red):
        team = assignment.team.upper()
        team_expected = p_blue if team == "BLUE" else 1.0 - p_blue
        actual = 1.0 if team == winner_team else 0.0
        outcome_error = actual - team_expected

        for role in ROLES:
            player = assignment.slots[role]
            if player.id is None:
                raise ValueError("All players must have database ids before rating updates.")

            before = player.rating_for(role)
            games_before = player.games_for(role)
            k = personal_k_factor(cfg["k_factor"], games_before)
            delta = k * outcome_error
            reason_parts: list[str] = ["승패 보정"]

            if team == winner_team and player.id in carry_ids:
                delta += cfg["carry_bonus"]
                reason_parts.append("캐리 선정")

            if team == winner_team and mvp_player_id is not None and player.id == int(mvp_player_id):
                delta += cfg["mvp_bonus"]
                reason_parts.append("MVP 보너스")

            lane_label = lane_impacts.get(role, "비등")
            lane_value = LANE_IMPACT_SCORES.get(lane_label, 0.0) * cfg["lane_impact_weight"]
            if lane_value:
                if team == winner_team:
                    delta += lane_value
                    reason_parts.append(f"라인 {lane_label}")
                else:
                    # Opposite side gets a partial mirrored adjustment. If the winning
                    # team's lane was "열세", the losing player receives positive credit.
                    delta -= lane_value * 0.85
                    reason_parts.append(f"상대 라인 {lane_label}")

            max_delta = float(cfg["max_single_game_delta"])
            delta = max(-max_delta, min(max_delta, delta))
            after = clamp(before + delta)
            games_after = games_before + 1

            changes.append(
                RatingChange(
                    player_id=int(player.id),
                    player_name=player.name,
                    team=team,
                    role=role,
                    before=round(before, 3),
                    after=round(after, 3),
                    delta=round(after - before, 3),
                    games_before=games_before,
                    games_after=games_after,
                    confidence_after=games_to_confidence(games_after),
                    reason=" + ".join(reason_parts),
                )
            )

    return changes
