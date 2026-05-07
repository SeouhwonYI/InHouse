"""Sorting helpers for the Team Builder player pool."""
from __future__ import annotations

from collections.abc import Iterable

from .constants import ROLES
from .models import Player

DEFAULT_POOL_SORT = "기본"
NAME_POOL_SORT = "이름"
SOLO_POOL_SORT = "솔로랭크"
POOL_SORT_OPTIONS = [DEFAULT_POOL_SORT, *ROLES, SOLO_POOL_SORT, NAME_POOL_SORT]


def normalize_pool_sort_key(sort_key: str | None) -> str:
    """Normalize a UI sort key into one of the supported player-pool sort options."""
    if not sort_key:
        return DEFAULT_POOL_SORT
    key = str(sort_key).strip()
    upper_key = key.upper()
    if upper_key in ROLES:
        return upper_key
    if key in {DEFAULT_POOL_SORT, NAME_POOL_SORT, SOLO_POOL_SORT}:
        return key
    if upper_key in {"NAME", "DISPLAY_NAME"}:
        return NAME_POOL_SORT
    if upper_key in {"SOLO", "RANK", "BASE", "BASE_RATING"}:
        return SOLO_POOL_SORT
    return DEFAULT_POOL_SORT


def sorted_players_for_pool(
    players: Iterable[Player],
    sort_key: str | None = DEFAULT_POOL_SORT,
    *,
    descending: bool = True,
) -> list[Player]:
    """Return players sorted for the Team Builder player pool.

    Role sort keys sort by that role's estimated rating. The secondary key lightly
    prefers players who are less off-role for that role, then falls back to display name.
    """
    normalized = normalize_pool_sort_key(sort_key)
    items = list(players)

    if normalized == DEFAULT_POOL_SORT:
        return items

    if normalized in ROLES:
        role = normalized
        return sorted(
            items,
            key=lambda p: (
                p.rating_for(role),
                -p.preference_penalty(role),
                p.base_rating,
                (p.label_name or p.name).casefold(),
            ),
            reverse=descending,
        )

    if normalized == SOLO_POOL_SORT:
        return sorted(
            items,
            key=lambda p: (
                p.base_rating,
                p.league_points,
                (p.solo_tier or "").casefold(),
                (p.label_name or p.name).casefold(),
            ),
            reverse=descending,
        )

    if normalized == NAME_POOL_SORT:
        return sorted(
            items,
            key=lambda p: ((p.label_name or p.name).casefold(), p.name.casefold()),
            reverse=descending,
        )

    return items
