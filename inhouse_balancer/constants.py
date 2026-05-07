"""Shared constants for the in-house balancer MVP."""
from __future__ import annotations

ROLES: tuple[str, ...] = ("TOP", "JG", "MID", "ADC", "SUP")

ROLE_LABELS: dict[str, str] = {
    "TOP": "탑",
    "JG": "정글",
    "MID": "미드",
    "ADC": "원딜",
    "SUP": "서포터",
}

ROLE_SORT_ORDER: dict[str, int] = {role: idx for idx, role in enumerate(ROLES)}

LANE_IMPACT_LABELS: tuple[str, ...] = ("압승", "우세", "비등", "열세")

# Winning team's lane state, from the perspective of the selected winning team.
# Positive means the winning team also won that lane; negative means they won the game
# despite this lane being behind.
LANE_IMPACT_SCORES: dict[str, float] = {
    "압승": 1.40,
    "우세": 0.70,
    "비등": 0.00,
    "열세": -0.70,
}

# Rating is intentionally a human-readable 0~100 scale, not Riot MMR.
TIER_BASE_RATING: dict[str, float] = {
    "UNRANKED": 50.0,
    "IRON": 25.0,
    "BRONZE": 35.0,
    "SILVER": 45.0,
    "GOLD": 55.0,
    "PLATINUM": 65.0,
    "EMERALD": 72.0,
    "DIAMOND": 80.0,
    "MASTER": 88.0,
    "GRANDMASTER": 93.0,
    "CHALLENGER": 97.0,
}

# Riot ranks ascend from IV -> I. The offsets are deliberately small because LP already
# provides a fine-grained signal.
RANK_OFFSET: dict[str, float] = {
    "IV": 0.0,
    "III": 1.0,
    "II": 2.0,
    "I": 3.0,
    "": 0.0,
}

DEFAULT_CONFIG: dict[str, float] = {
    # Team building objective.
    "team_gap_weight": 1.0,
    "preference_penalty_weight": 1.2,

    # Expected win-rate curve. Larger => win-rate estimates are less extreme.
    "winrate_scale": 200.0,

    # Rating update. K=4 means a 50:50 win gives roughly +2 before bonuses.
    "k_factor": 4.0,
    "carry_bonus": 1.15,
    "mvp_bonus": 0.85,
    "lane_impact_weight": 1.0,
    "max_single_game_delta": 6.0,

    # Base rating is only a helper feature for profile summaries.
    "base_rating_share": 0.22,
}
