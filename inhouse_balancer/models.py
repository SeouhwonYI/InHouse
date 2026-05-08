"""Dataclasses used by the balancer and rating engine."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .constants import ROLES


@dataclass(slots=True)
class RoleRating:
    role: str
    rating: float
    games_played: int = 0
    confidence: float = 0.45


@dataclass(slots=True)
class Player:
    id: int | None
    name: str
    display_name: str | None = None
    riot_game_name: str | None = None
    riot_tag_line: str | None = None
    solo_tier: str = "UNRANKED"
    solo_rank: str = ""
    league_points: int = 0
    flex_tier: str = "UNRANKED"
    flex_rank: str = ""
    flex_league_points: int = 0
    base_rating: float = 50.0
    preferred_roles: list[str] = field(default_factory=list)
    role_ratings: dict[str, RoleRating] = field(default_factory=dict)
    lane_champions: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        normalized: list[str] = []
        for role in self.preferred_roles:
            role = role.upper()
            if role in ROLES and role not in normalized:
                normalized.append(role)
        self.preferred_roles = normalized

        for role in ROLES:
            self.role_ratings.setdefault(
                role,
                RoleRating(role=role, rating=self.base_rating, games_played=0, confidence=0.45),
            )
            self.lane_champions.setdefault(role, [])

    def rating_for(self, role: str) -> float:
        return float(self.role_ratings[role].rating)

    def games_for(self, role: str) -> int:
        return int(self.role_ratings[role].games_played)

    def confidence_for(self, role: str) -> float:
        return float(self.role_ratings[role].confidence)

    def preference_penalty(self, role: str) -> float:
        """Small human-facing penalty for assigning a player away from preferred roles.

        This is intentionally in the same rough unit as the 0~100 role rating. A penalty
        of 6~8 is enough to avoid ugly off-role assignments unless needed for balance.
        """
        role = role.upper()
        if not self.preferred_roles:
            return 2.5
        if role == self.preferred_roles[0]:
            return 0.0
        if len(self.preferred_roles) >= 2 and role == self.preferred_roles[1]:
            return 2.0
        if role in self.preferred_roles:
            return 3.0
        return 7.0

    @property
    def label_name(self) -> str:
        return self.display_name or self.name

    @property
    def riot_id(self) -> str:
        if self.riot_game_name and self.riot_tag_line:
            return f"{self.riot_game_name}#{self.riot_tag_line}"
        return self.name

    def to_row(self) -> dict[str, Any]:
        row: dict[str, Any] = {
            "id": self.id,
            "이름": self.label_name,
            "소환사": self.name,
            "솔로랭크": f"{self.solo_tier} {self.solo_rank}".strip(),
            "자유랭크": f"{self.flex_tier} {self.flex_rank}".strip(),
            "선호": ", ".join(self.preferred_roles) or "-",
        }
        for role in ROLES:
            row[role] = round(self.rating_for(role), 1)
        return row


@dataclass(slots=True)
class TeamAssignment:
    team: str
    slots: dict[str, Player]
    total_rating: float
    preference_penalty: float

    def player_ids(self) -> set[int]:
        return {p.id for p in self.slots.values() if p.id is not None}

    def as_rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for role in ROLES:
            player = self.slots[role]
            rows.append(
                {
                    "포지션": role,
                    "플레이어": player.name,
                    "배정 실력치": round(player.rating_for(role), 1),
                    "선호 일치": "일치" if player.preference_penalty(role) <= 2.0 else "오프롤",
                }
            )
        return rows


@dataclass(slots=True)
class BalanceCandidate:
    blue: TeamAssignment
    red: TeamAssignment
    objective: float
    rating_gap: float
    preference_penalty: float
    expected_blue_win: float

    def summary(self) -> dict[str, Any]:
        return {
            "Blue 총합": round(self.blue.total_rating, 1),
            "Red 총합": round(self.red.total_rating, 1),
            "실력치 차이": round(self.rating_gap, 2),
            "선호 포지션 페널티": round(self.preference_penalty, 2),
            "Blue 예상 승률": round(self.expected_blue_win * 100, 1),
        }


@dataclass(slots=True)
class RatingChange:
    player_id: int
    player_name: str
    team: str
    role: str
    before: float
    after: float
    delta: float
    games_before: int
    games_after: int
    confidence_after: float
    reason: str
