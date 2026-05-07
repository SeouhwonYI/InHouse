from __future__ import annotations

from inhouse_balancer.balancer import generate_balanced_teams, optimize_side_swaps
from inhouse_balancer.constants import ROLES
from inhouse_balancer.importers import load_csv_rows
from inhouse_balancer.rating import compute_rating_preview
from inhouse_balancer.exports import append_match_csv_log
from inhouse_balancer.storage import connect, create_player, init_db, list_players, record_match_and_update


def make_conn():
    conn = connect(":memory:", database_url="")
    init_db(conn)
    seed_realistic_players(conn)
    return conn


def seed_realistic_players(conn) -> None:
    specs = [
        ("A", ["TOP"], {"TOP": 82, "JG": 70, "MID": 70, "ADC": 63, "SUP": 66}),
        ("B", ["MID"], {"TOP": 62, "JG": 64, "MID": 78, "ADC": 67, "SUP": 62}),
        ("C", ["JG"], {"TOP": 63, "JG": 81, "MID": 65, "ADC": 61, "SUP": 66}),
        ("D", ["ADC"], {"TOP": 59, "JG": 60, "MID": 63, "ADC": 84, "SUP": 68}),
        ("E", ["SUP"], {"TOP": 58, "JG": 62, "MID": 60, "ADC": 65, "SUP": 80}),
        ("F", ["TOP"], {"TOP": 76, "JG": 62, "MID": 61, "ADC": 57, "SUP": 60}),
        ("G", ["JG"], {"TOP": 61, "JG": 77, "MID": 64, "ADC": 59, "SUP": 62}),
        ("H", ["MID"], {"TOP": 60, "JG": 63, "MID": 75, "ADC": 62, "SUP": 58}),
        ("I", ["ADC"], {"TOP": 57, "JG": 58, "MID": 60, "ADC": 79, "SUP": 63}),
        ("J", ["SUP"], {"TOP": 56, "JG": 60, "MID": 59, "ADC": 62, "SUP": 77}),
    ]
    for name, prefs, ratings in specs:
        create_player(conn, name=name, preferred_roles=prefs, role_ratings=ratings)


def test_generate_balanced_teams_has_exact_roles_and_players():
    conn = make_conn()
    players = list_players(conn)
    candidate = generate_balanced_teams(players, top_k=1)[0]

    assert set(candidate.blue.slots.keys()) == set(ROLES)
    assert set(candidate.red.slots.keys()) == set(ROLES)
    assert len(candidate.blue.player_ids()) == 5
    assert len(candidate.red.player_ids()) == 5
    assert candidate.blue.player_ids().isdisjoint(candidate.red.player_ids())


def test_carry_selection_gives_stronger_positive_update_than_unselected_winner():
    conn = make_conn()
    players = list_players(conn)
    candidate = generate_balanced_teams(players, top_k=1)[0]

    carry_player = candidate.blue.slots["MID"]
    changes = compute_rating_preview(
        candidate.blue,
        candidate.red,
        blue_win=True,
        carry_player_ids=[carry_player.id],
        mvp_player_id=carry_player.id,
        lane_impacts={role: "비등" for role in ROLES},
    )

    carry_change = next(c for c in changes if c.player_id == carry_player.id)
    normal_winner_change = next(
        c
        for c in changes
        if c.team == "BLUE" and c.player_id != carry_player.id
    )

    assert carry_change.delta > normal_winner_change.delta


def test_losing_lane_on_winning_team_can_reduce_that_role_bonus():
    conn = make_conn()
    players = list_players(conn)
    candidate = generate_balanced_teams(players, top_k=1)[0]

    neutral = compute_rating_preview(
        candidate.blue,
        candidate.red,
        blue_win=True,
        carry_player_ids=[],
        lane_impacts={role: "비등" for role in ROLES},
    )
    with_losing_top = compute_rating_preview(
        candidate.blue,
        candidate.red,
        blue_win=True,
        carry_player_ids=[],
        lane_impacts={**{role: "비등" for role in ROLES}, "TOP": "열세"},
    )

    blue_top_neutral = next(c for c in neutral if c.team == "BLUE" and c.role == "TOP")
    blue_top_losing = next(c for c in with_losing_top if c.team == "BLUE" and c.role == "TOP")
    red_top_losing = next(c for c in with_losing_top if c.team == "RED" and c.role == "TOP")
    red_top_neutral = next(c for c in neutral if c.team == "RED" and c.role == "TOP")

    assert blue_top_losing.delta < blue_top_neutral.delta
    assert red_top_losing.delta > red_top_neutral.delta


def test_load_csv_rows_accepts_cp949_korean_excel_csv(tmp_path):
    csv_path = tmp_path / "players_cp949.csv"
    text = "name,riot_game_name,riot_tag_line,preferred_roles\n잘ㅋ멋진놈,잘ㅋ멋진놈,KR1,TOP|JG\n"
    csv_path.write_bytes(text.encode("cp949"))

    rows = load_csv_rows(csv_path)

    assert rows[0]["name"] == "잘ㅋ멋진놈"
    assert rows[0]["riot_game_name"] == "잘ㅋ멋진놈"
    assert rows[0]["preferred_roles"] == "TOP|JG"


def test_record_match_can_append_csv_log(tmp_path):
    conn = make_conn()
    players = list_players(conn)
    candidate = generate_balanced_teams(players, top_k=1)[0]
    log_path = tmp_path / "records" / "match_results.csv"

    match_id, _changes = record_match_and_update(
        conn,
        blue=candidate.blue,
        red=candidate.red,
        blue_win=True,
        carry_player_ids=[candidate.blue.slots["MID"].id],
        mvp_player_id=candidate.blue.slots["MID"].id,
        lane_impacts={role: "비등" for role in ROLES},
    )
    append_match_csv_log(
        conn,
        match_id=match_id,
        blue=candidate.blue,
        red=candidate.red,
        blue_win=True,
        blue_score=1,
        red_score=0,
        carry_player_ids=[candidate.blue.slots["MID"].id],
        mvp_player_id=candidate.blue.slots["MID"].id,
        lane_impacts={role: "비등" for role in ROLES},
        path=log_path,
    )

    assert log_path.exists()
    content = log_path.read_text(encoding="utf-8-sig")
    assert "match_id" in content
    assert "BLUE" in content


def test_side_swap_optimizer_considers_only_rolewise_swaps():
    conn = make_conn()
    players = list_players(conn)
    candidate = generate_balanced_teams(players, top_k=1)[0]
    optimized = optimize_side_swaps(candidate.blue, candidate.red)

    assert set(optimized.blue.slots) == set(ROLES)
    assert set(optimized.red.slots) == set(ROLES)
    # For each role, the optimized slot must be one of the original two same-role players.
    for role in ROLES:
        original_pair = {candidate.blue.slots[role].name, candidate.red.slots[role].name}
        assert optimized.blue.slots[role].name in original_pair
        assert optimized.red.slots[role].name in original_pair


def test_player_pool_sort_by_role_descending():
    from inhouse_balancer.pool_sort import sorted_players_for_pool

    conn = make_conn()
    players = list_players(conn)
    sorted_by_jg = sorted_players_for_pool(players, "JG", descending=True)

    assert sorted_by_jg[0].rating_for("JG") >= sorted_by_jg[1].rating_for("JG")
    assert sorted_by_jg == sorted(players, key=lambda p: (p.rating_for("JG"), -p.preference_penalty("JG"), p.base_rating, p.label_name.casefold()), reverse=True)


def test_player_pool_sort_by_name_ascending():
    from inhouse_balancer.pool_sort import sorted_players_for_pool

    conn = make_conn()
    players = list_players(conn)
    sorted_by_name = sorted_players_for_pool(players, "이름", descending=False)

    assert [p.label_name for p in sorted_by_name] == sorted(p.label_name for p in players)
