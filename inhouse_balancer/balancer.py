"""Team balancing helpers for 5v5 in-house games."""
from __future__ import annotations

from itertools import combinations, permutations, product

from .constants import DEFAULT_CONFIG, ROLES
from .models import BalanceCandidate, Player, TeamAssignment
from .rating import expected_win_probability


def _assignment_metrics(slots: dict[str, Player]) -> tuple[float, float]:
    total_rating = sum(slots[role].rating_for(role) for role in ROLES)
    preference_penalty = sum(slots[role].preference_penalty(role) for role in ROLES)
    return round(total_rating, 3), round(preference_penalty, 3)


def make_assignment(team: str, slots: dict[str, Player]) -> TeamAssignment:
    """Build a TeamAssignment from explicit role -> player slots."""
    missing = [role for role in ROLES if role not in slots]
    if missing:
        raise ValueError(f"missing roles for {team}: {missing}")
    normalized = {role: slots[role] for role in ROLES}
    total_rating, preference_penalty = _assignment_metrics(normalized)
    return TeamAssignment(
        team=team.upper(),
        slots=normalized,
        total_rating=total_rating,
        preference_penalty=preference_penalty,
    )


# Backward-compatible alias for older callers.
make_assignment_from_slots = make_assignment


def _make_assignment(team: str, permuted_players: tuple[Player, ...]) -> TeamAssignment:
    return make_assignment(team, dict(zip(ROLES, permuted_players, strict=True)))


def enumerate_role_assignments(team: str, players: list[Player]) -> list[TeamAssignment]:
    if len(players) != 5:
        raise ValueError("A role assignment requires exactly 5 players.")
    return [_make_assignment(team, perm) for perm in permutations(players)]


def candidate_from_assignments(
    blue: TeamAssignment,
    red: TeamAssignment,
    *,
    config: dict[str, float] | None = None,
) -> BalanceCandidate:
    """Create a BalanceCandidate from already-fixed Blue/Red assignments."""
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    rating_gap = abs(blue.total_rating - red.total_rating)
    preference_penalty = blue.preference_penalty + red.preference_penalty
    objective = cfg["team_gap_weight"] * rating_gap + cfg["preference_penalty_weight"] * preference_penalty
    expected_blue = expected_win_probability(
        blue.total_rating,
        red.total_rating,
        scale=cfg["winrate_scale"],
    )
    return BalanceCandidate(
        blue=blue,
        red=red,
        objective=round(objective, 6),
        rating_gap=round(rating_gap, 6),
        preference_penalty=round(preference_penalty, 6),
        expected_blue_win=round(expected_blue, 6),
    )


def _insert_top_candidate(
    top: list[BalanceCandidate],
    candidate: BalanceCandidate,
    top_k: int,
) -> None:
    """Maintain a tiny sorted list of top candidates."""
    top.append(candidate)
    top.sort(key=lambda c: (c.objective, c.rating_gap, c.preference_penalty))
    if len(top) > top_k:
        top.pop()


def generate_balanced_teams(
    players: list[Player],
    *,
    config: dict[str, float] | None = None,
    top_k: int = 1,
) -> list[BalanceCandidate]:
    """Return one or more balanced team candidates.

    The first player is forced onto Blue while enumerating team splits to remove mirror
    duplicates. This does not reduce solution quality because Blue/Red labels are symmetric.
    """
    if len(players) != 10:
        raise ValueError(f"Exactly 10 players are required, got {len(players)}.")

    cfg = {**DEFAULT_CONFIG, **(config or {})}
    top_k = max(1, int(top_k))
    indexed_players = list(enumerate(players))
    top: list[BalanceCandidate] = []
    worst_objective = float("inf")

    for blue_indices_tuple in combinations(range(10), 5):
        if 0 not in blue_indices_tuple:
            continue

        blue_indices = set(blue_indices_tuple)
        blue_players = [player for idx, player in indexed_players if idx in blue_indices]
        red_players = [player for idx, player in indexed_players if idx not in blue_indices]

        blue_assignments = enumerate_role_assignments("BLUE", blue_players)
        red_assignments = enumerate_role_assignments("RED", red_players)

        for blue in blue_assignments:
            b_total = blue.total_rating
            b_pen = blue.preference_penalty
            for red in red_assignments:
                rating_gap = abs(b_total - red.total_rating)
                preference_penalty = b_pen + red.preference_penalty
                objective = (
                    cfg["team_gap_weight"] * rating_gap
                    + cfg["preference_penalty_weight"] * preference_penalty
                )

                if len(top) >= top_k and objective > worst_objective:
                    continue

                expected_blue = expected_win_probability(
                    b_total,
                    red.total_rating,
                    scale=cfg["winrate_scale"],
                )
                candidate = BalanceCandidate(
                    blue=blue,
                    red=red,
                    objective=round(objective, 6),
                    rating_gap=round(rating_gap, 6),
                    preference_penalty=round(preference_penalty, 6),
                    expected_blue_win=round(expected_blue, 6),
                )
                _insert_top_candidate(top, candidate, top_k)
                worst_objective = top[-1].objective

    return top


def optimize_side_swaps(
    blue: TeamAssignment,
    red: TeamAssignment,
    *,
    config: dict[str, float] | None = None,
    prefer_fewer_swaps: float = 0.05,
) -> BalanceCandidate:
    """Optimize only same-role Blue<->Red swaps after a human manual edit.

    This preserves the role row order and each player's assigned role. For every role,
    it considers either keeping the current left/right placement or swapping that role's
    Blue and Red players. There are only 2^5 = 32 possibilities.
    """
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    best: tuple[float, int, BalanceCandidate] | None = None

    for mask in product((False, True), repeat=len(ROLES)):
        blue_slots: dict[str, Player] = {}
        red_slots: dict[str, Player] = {}
        swaps = 0
        for role, swap in zip(ROLES, mask, strict=True):
            if swap:
                swaps += 1
                blue_slots[role] = red.slots[role]
                red_slots[role] = blue.slots[role]
            else:
                blue_slots[role] = blue.slots[role]
                red_slots[role] = red.slots[role]

        candidate = candidate_from_assignments(
            make_assignment("BLUE", blue_slots),
            make_assignment("RED", red_slots),
            config=cfg,
        )
        # Main objective remains rating gap + off-role penalty; a tiny tie-breaker avoids
        # unnecessary swaps when two arrangements are effectively equivalent.
        score = candidate.objective + swaps * float(prefer_fewer_swaps)
        item = (score, swaps, candidate)
        if best is None or (item[0], item[1], item[2].rating_gap) < (best[0], best[1], best[2].rating_gap):
            best = item

    assert best is not None
    return best[2]


# Backward-compatible semantic alias.
optimize_lane_side_swaps = optimize_side_swaps
