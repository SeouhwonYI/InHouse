from __future__ import annotations

import html
import csv
import io
import json
import textwrap
from dataclasses import asdict, is_dataclass
from pathlib import Path
import os
from typing import Iterable

import pandas as pd
import streamlit as st

try:
    from dotenv import load_dotenv
    # 명시적으로 .env만 읽습니다. .env.example은 템플릿 파일로만 남깁니다.
    load_dotenv(dotenv_path=Path(".env"), override=True)
except Exception:  # noqa: BLE001
    pass

try:
    from streamlit_sortables import sort_items
except Exception:  # noqa: BLE001
    sort_items = None

from inhouse_balancer.balancer import candidate_from_assignments, generate_balanced_teams, make_assignment, optimize_side_swaps
from inhouse_balancer.constants import DEFAULT_CONFIG, LANE_IMPACT_LABELS, ROLES
from inhouse_balancer.rating import compute_rating_preview, rating_to_tier_label
from inhouse_balancer.importers import bootstrap_from_records, import_players_csv, import_matches_csv
from inhouse_balancer.riot_client import RiotClient
from inhouse_balancer.riot_sync import refresh_all_players_from_riot
from inhouse_balancer.pool_sort import (
    DEFAULT_POOL_SORT,
    NAME_POOL_SORT,
    POOL_SORT_OPTIONS,
    sorted_players_for_pool,
)
from inhouse_balancer.exports import DEFAULT_MATCH_LOG_PATH, append_match_csv_log
from inhouse_balancer.storage import (
    connect,
    count_players,
    create_player,
    delete_all_data,
    export_matches_rows,
    export_players_rows,
    init_db,
    list_match_participants,
    list_matches,
    list_player_match_history,
    list_players,
    record_match_and_update,
)

DB_PATH = Path("data/inhouse_balancer.sqlite")


def env_value(key: str, default: str = "") -> str:
    """Read configuration from the local .env/environment only.

    .env.example is intentionally never loaded. In local development, values in .env
    override shell environment variables because load_dotenv(..., override=True) is used above.
    """
    return os.getenv(key, default)


ROLE_META = {
    "TOP": {"label": "탑", "icon": "⬟", "class": "role-top"},
    "JG": {"label": "정글", "icon": "◆", "class": "role-jg"},
    "MID": {"label": "미드", "icon": "✦", "class": "role-mid"},
    "ADC": {"label": "원딜", "icon": "◈", "class": "role-adc"},
    "SUP": {"label": "서폿", "icon": "✚", "class": "role-sup"},
}


@st.cache_resource
def get_conn():
    storage_mode = env_value("INHOUSE_STORAGE", "").strip().lower()
    database_url_from_env = env_value("DATABASE_URL", "").strip()

    # .env에 DATABASE_URL이 있으면 기본적으로 DB를 사용합니다.
    # CSV/SQLite를 강제로 쓰고 싶을 때만 INHOUSE_STORAGE=csv 또는 sqlite를 명시하세요.
    if storage_mode in {"csv", "file", "files", "sqlite"}:
        database_url = ""
    elif storage_mode in {"postgres", "postgresql", "db"} or database_url_from_env:
        database_url = database_url_from_env
    else:
        database_url = ""

    conn = connect(DB_PATH, database_url=database_url)
    init_db(conn)
    # Real-data mode: do not auto-generate sample players.
    return conn


def e(value: object) -> str:
    return html.escape(str(value), quote=True)


def clean_html(markup: str) -> str:
    """Return HTML/CSS with leading indentation removed from every line.

    Streamlit Markdown treats lines that start with four spaces as code blocks.
    Without this, custom <div> snippets can appear as raw source code in the UI.
    """
    return "\n".join(
        line.strip()
        for line in textwrap.dedent(str(markup)).splitlines()
        if line.strip()
    )


def render_html(markup: str, *, container=st) -> None:
    """Render HTML/CSS without letting Markdown turn indented tags into code."""
    cleaned = clean_html(markup)
    if hasattr(container, "html"):
        container.html(cleaned)
    else:
        container.markdown(cleaned, unsafe_allow_html=True)


def rating_color_class(rating: float) -> str:
    if rating >= 85:
        return "rating-god"
    if rating >= 75:
        return "rating-high"
    if rating >= 65:
        return "rating-mid"
    if rating >= 55:
        return "rating-low"
    return "rating-weak"


def team_class(team: str) -> str:
    return "team-blue" if team.upper() == "BLUE" else "team-red"


def inject_css() -> None:
    render_html(
        """
        <style>
        :root {
            --bg-0: #070b16;
            --bg-1: #0b1020;
            --bg-2: #10182e;
            --panel: rgba(17, 25, 46, 0.82);
            --panel-strong: rgba(20, 31, 58, 0.95);
            --line: rgba(132, 153, 255, 0.22);
            --line-strong: rgba(132, 153, 255, 0.38);
            --text: #e8ecff;
            --muted: #9aa6c7;
            --blue: #5aa7ff;
            --blue-strong: #2f80ff;
            --red: #ff657f;
            --purple: #9b7cff;
            --green: #42d78b;
            --gold: #ffc766;
        }

        html, body, [data-testid="stAppViewContainer"] {
            background:
                radial-gradient(circle at top left, rgba(73, 125, 255, .18), transparent 32rem),
                radial-gradient(circle at top right, rgba(155, 124, 255, .16), transparent 34rem),
                linear-gradient(135deg, #060916 0%, #0b1020 52%, #090c18 100%) !important;
            color: var(--text) !important;
        }
        [data-testid="stHeader"] { background: rgba(6, 9, 22, .35) !important; backdrop-filter: blur(10px); }
        .main .block-container { max-width: 1500px; padding: 1.35rem 1.8rem 2.5rem; }

        section[data-testid="stSidebar"] {
            background: linear-gradient(180deg, rgba(8, 13, 27, .98), rgba(12, 18, 36, .98)) !important;
            border-right: 1px solid rgba(116, 139, 255, .18);
        }
        section[data-testid="stSidebar"] * { color: #dbe3ff !important; }
        section[data-testid="stSidebar"] [data-testid="stRadio"] label { color: #9aa6c7 !important; }
        section[data-testid="stSidebar"] [role="radiogroup"] label {
            border-radius: 12px !important;
            padding: .4rem .55rem !important;
            margin: .12rem 0 !important;
        }

        h1, h2, h3 { color: var(--text) !important; letter-spacing: -0.025em; }
        p, span, label { color: inherit; }
        .stCaption, [data-testid="stCaptionContainer"] { color: var(--muted) !important; }
        .stSelectbox, .stMultiSelect, .stNumberInput, .stTextInput, .stSlider { color: var(--text) !important; }
        div[data-baseweb="select"] > div,
        input,
        textarea {
            background: rgba(10, 16, 31, .9) !important;
            border-color: rgba(115, 137, 255, .24) !important;
            color: var(--text) !important;
            border-radius: 12px !important;
        }
        .stButton > button {
            border: 1px solid rgba(118, 143, 255, .35) !important;
            border-radius: 14px !important;
            color: #eef3ff !important;
            background: linear-gradient(135deg, rgba(47,128,255,.96), rgba(155,124,255,.96)) !important;
            box-shadow: 0 14px 30px rgba(65, 99, 255, .20);
            min-height: 44px;
            font-weight: 800 !important;
            letter-spacing: -.01em;
        }
        .stButton > button:hover {
            border-color: rgba(193, 206, 255, .8) !important;
            box-shadow: 0 16px 36px rgba(65, 99, 255, .30);
            transform: translateY(-1px);
        }
        div[data-testid="stMetric"] {
            background: rgba(14, 22, 42, .75);
            border: 1px solid rgba(122, 145, 255, .18);
            border-radius: 16px;
            padding: .9rem 1rem;
        }
        div[data-testid="stMetricLabel"] { color: var(--muted) !important; }
        div[data-testid="stMetricValue"] { color: #f4f7ff !important; }
        div[data-testid="stDataFrame"] {
            border: 1px solid rgba(122, 145, 255, .18);
            border-radius: 16px;
            overflow: hidden;
        }

        .app-logo {
            display:flex; align-items:center; gap: .7rem; margin: .35rem 0 1.1rem 0;
        }
        .logo-mark {
            width: 40px; height: 40px; border-radius: 14px;
            display:flex; align-items:center; justify-content:center;
            background: linear-gradient(135deg, #2f80ff, #9b7cff);
            box-shadow: 0 12px 28px rgba(47,128,255,.25);
            font-size: 1.2rem;
        }
        .logo-text-main { font-weight: 900; line-height: 1; letter-spacing: .02em; color: #f4f7ff; }
        .logo-text-sub { margin-top: .22rem; color: #8fa2d8; font-size: .78rem; }

        .hero {
            border: 1px solid rgba(122, 145, 255, .23);
            border-radius: 24px;
            background:
                radial-gradient(circle at 12% 0%, rgba(47,128,255,.26), transparent 26rem),
                radial-gradient(circle at 78% 10%, rgba(155,124,255,.20), transparent 22rem),
                linear-gradient(135deg, rgba(18,28,55,.86), rgba(10,15,31,.86));
            padding: 1.25rem 1.35rem;
            margin-bottom: 1.05rem;
            box-shadow: 0 20px 60px rgba(0,0,0,.24);
        }
        .hero-row { display:flex; justify-content:space-between; gap: 1rem; align-items:flex-start; }
        .eyebrow { color: #8da3ff; font-size: .78rem; font-weight: 800; letter-spacing: .11em; text-transform: uppercase; }
        .hero-title { margin-top: .2rem; color: #f8faff; font-weight: 900; font-size: 2.0rem; letter-spacing: -.04em; }
        .hero-subtitle { margin-top: .25rem; color: var(--muted); font-size: .98rem; max-width: 760px; }
        .hero-actions { display:flex; gap: .55rem; flex-wrap:wrap; justify-content:flex-end; }

        .panel {
            border: 1px solid rgba(122, 145, 255, .22);
            border-radius: 22px;
            background: linear-gradient(180deg, rgba(18, 28, 52, .86), rgba(10, 15, 29, .86));
            padding: 1rem 1.05rem;
            box-shadow: 0 16px 44px rgba(0,0,0,.20);
            margin-bottom: 1rem;
        }
        .panel.tight { padding: .78rem .85rem; }
        .panel-title-row { display:flex; align-items:center; justify-content:space-between; gap: .75rem; margin-bottom: .8rem; }
        .panel-title { font-size: 1.02rem; font-weight: 900; color: #f2f5ff; letter-spacing: -.02em; }
        .panel-subtitle { color: var(--muted); font-size: .82rem; margin-top: .2rem; }
        .subtle-tag {
            display:inline-flex; align-items:center; gap:.32rem;
            padding: .32rem .55rem; border-radius: 999px;
            background: rgba(120,145,255,.10); border: 1px solid rgba(120,145,255,.20);
            color: #b9c6ff; font-size: .76rem; font-weight: 800;
        }
        .sort-hint {
            display:flex; align-items:center; justify-content:space-between; gap:.7rem;
            margin: .2rem 0 .62rem; color:#8fa0d0; font-size:.78rem;
        }
        .sort-hint b { color:#dce6ff; }
        .sort-head { display:inline-flex; align-items:center; gap:.22rem; }
        .sort-head.active { color:#eef5ff; }
        .sort-arrow { color:#77a7ff; font-weight:950; }

        .metric-grid { display:grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap:.78rem; }
        .metric-card {
            border: 1px solid rgba(122, 145, 255, .18);
            border-radius: 18px;
            background: rgba(10, 16, 31, .72);
            padding: .82rem .9rem;
        }
        .metric-label { color: var(--muted); font-size:.78rem; font-weight:700; }
        .metric-value { color:#f8faff; font-size:1.32rem; font-weight:900; margin-top:.1rem; letter-spacing:-.03em; }
        .metric-hint { color:#8794bb; font-size:.74rem; margin-top:.1rem; }

        .player-table { width: 100%; border-collapse: separate; border-spacing: 0 .46rem; }
        .player-table th { color: #8390b8; font-size: .73rem; font-weight: 800; text-align: left; padding: 0 .55rem .1rem; }
        .player-table th.sort-active {
            color: #f5f8ff;
            text-shadow: 0 0 16px rgba(90, 167, 255, .35);
        }
        .player-table td {
            background: rgba(9, 15, 29, .78);
            border-top: 1px solid rgba(110, 132, 255, .14);
            border-bottom: 1px solid rgba(110, 132, 255, .14);
            padding: .58rem .55rem;
            vertical-align: middle;
            color: #e9edff;
        }
        .player-table tr td:first-child { border-left: 1px solid rgba(110,132,255,.14); border-radius: 14px 0 0 14px; }
        .player-table tr td:last-child { border-right: 1px solid rgba(110,132,255,.14); border-radius: 0 14px 14px 0; }
        .player-name { font-weight: 850; color:#f3f6ff; white-space: nowrap; }
        .player-id { color:#7f8bb0; font-size:.72rem; margin-top:.1rem; }
        .avatar {
            width: 30px; height: 30px; border-radius: 11px; display:inline-flex; align-items:center; justify-content:center;
            margin-right:.55rem; background: linear-gradient(135deg, rgba(47,128,255,.45), rgba(155,124,255,.45));
            border: 1px solid rgba(179, 194, 255, .25); box-shadow: inset 0 0 18px rgba(255,255,255,.08);
            font-size:.82rem; color:#f6f8ff;
        }
        .player-cell { display:flex; align-items:center; }
        .tier-badge, .role-chip, .pref-pill, .status-pill {
            display:inline-flex; align-items:center; justify-content:center;
            border-radius: 999px; font-weight: 850; white-space: nowrap;
        }
        .tier-badge { padding: .26rem .48rem; font-size:.72rem; color:#dbe8ff; background: rgba(95, 130, 255, .15); border:1px solid rgba(116,145,255,.26); }
        .role-chip { min-width: 44px; gap:.18rem; padding: .25rem .38rem; font-size:.72rem; margin-right:.22rem; border:1px solid rgba(134,154,255,.16); background: rgba(255,255,255,.035); }
        .role-chip b { font-weight: 900; }
        .pref-pill { padding:.24rem .42rem; font-size:.68rem; margin: .08rem .12rem .08rem 0; color:#cdd8ff; background:rgba(124,146,255,.12); border:1px solid rgba(124,146,255,.18); }
        .rating-god { color:#ffcf74; }
        .rating-high { color:#6ee7b7; }
        .rating-mid { color:#93c5fd; }
        .rating-low { color:#c4b5fd; }
        .rating-weak { color:#fca5a5; }

        .team-card {
            border-radius: 24px; padding: 1rem; min-height: 440px;
            border: 1px solid rgba(126,148,255,.24);
            background: linear-gradient(180deg, rgba(17, 26, 48, .92), rgba(8, 13, 26, .92));
            box-shadow: 0 20px 50px rgba(0,0,0,.22);
        }
        .team-blue { border-color: rgba(90,167,255,.34); box-shadow: 0 20px 55px rgba(31, 108, 255, .12); }
        .team-red { border-color: rgba(255,101,127,.34); box-shadow: 0 20px 55px rgba(255, 74, 104, .10); }
        .team-head { display:flex; align-items:flex-start; justify-content:space-between; gap: 1rem; margin-bottom:.8rem; }
        .team-name { font-size: 1.08rem; font-weight: 950; letter-spacing:.06em; }
        .team-blue .team-name { color: var(--blue); }
        .team-red .team-name { color: var(--red); }
        .team-total { text-align:right; }
        .team-total .label { color: var(--muted); font-size:.72rem; font-weight:750; }
        .team-total .value { color:#f7f9ff; font-size:1.42rem; font-weight:950; line-height:1.05; }
        .strength-bar { height: 7px; width: 120px; border-radius:999px; margin-top:.34rem; background: rgba(255,255,255,.06); overflow:hidden; }
        .strength-fill { height:100%; border-radius:999px; }
        .team-blue .strength-fill { background: linear-gradient(90deg, #2f80ff, #7cc6ff); }
        .team-red .strength-fill { background: linear-gradient(90deg, #ff4c6a, #ff9a76); }
        .slot-row {
            display:grid; grid-template-columns: 74px 1fr 74px 70px; gap:.45rem; align-items:center;
            padding: .62rem .6rem; margin-bottom:.42rem; border-radius: 16px;
            background: rgba(7, 13, 27, .72); border: 1px solid rgba(121,145,255,.14);
        }
        .slot-role { display:flex; align-items:center; gap:.38rem; font-size:.76rem; color:#cbd6ff; font-weight:900; }
        .slot-player { font-weight:900; color:#f3f6ff; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
        .slot-tier { color:#9aa6c7; font-size:.72rem; margin-top:.08rem; }
        .slot-champs { display:flex; gap:.28rem; flex-wrap:wrap; margin-top:.32rem; }
        .champ-pill {
            display:inline-flex; align-items:center; gap:.16rem;
            padding:.18rem .38rem; border-radius:999px;
            background:rgba(90,167,255,.11); border:1px solid rgba(90,167,255,.18);
            color:#cfe1ff; font-size:.68rem; font-weight:800;
        }
        .champ-pill.empty { color:#8794bb; background:rgba(255,255,255,.035); border-color:rgba(255,255,255,.055); }
        .slot-rating { text-align:right; font-weight:950; color:#f7faff; }
        .slot-rating small { display:block; color:#8b96ba; font-size:.66rem; font-weight:700; }
        .status-pill { padding:.22rem .38rem; font-size:.68rem; }
        .status-ok { color:#8af3bd; background:rgba(66,215,139,.12); border:1px solid rgba(66,215,139,.20); }
        .status-warn { color:#ffc766; background:rgba(255,199,102,.12); border:1px solid rgba(255,199,102,.20); }

        .balance-card { border-radius: 24px; padding: 1rem; background: linear-gradient(180deg, rgba(20,29,52,.92), rgba(10,14,28,.92)); border:1px solid rgba(155,124,255,.26); min-height: 440px; }
        .versus { text-align:center; margin: .35rem 0 .75rem; }
        .vs-mark { display:inline-flex; width: 62px; height: 62px; align-items:center; justify-content:center; border-radius:22px; background:linear-gradient(135deg, rgba(47,128,255,.30), rgba(255,101,127,.22)); color:#f8faff; font-weight:950; font-size:1.25rem; border:1px solid rgba(180,194,255,.25); }
        .winrate-line { display:flex; align-items:center; justify-content:space-between; font-size:.82rem; color:#aab5d7; margin-bottom:.45rem; }
        .winrate-bar { height: 12px; border-radius: 999px; overflow:hidden; display:flex; background:rgba(255,255,255,.06); border: 1px solid rgba(255,255,255,.05); }
        .win-blue { background: linear-gradient(90deg, #2f80ff, #73baff); }
        .win-red { background: linear-gradient(90deg, #ff6b85, #ff9a76); }
        .summary-list { margin-top:1rem; display:grid; gap:.55rem; }
        .summary-item { display:flex; justify-content:space-between; align-items:center; padding:.62rem .66rem; border-radius:15px; background:rgba(8,14,28,.75); border:1px solid rgba(122,145,255,.13); }
        .summary-item span:first-child { color:#9ca8ca; font-size:.78rem; font-weight:750; }
        .summary-item span:last-child { color:#f4f7ff; font-weight:950; }

        .info-strip { border:1px solid rgba(90,167,255,.22); border-radius:17px; padding:.72rem .82rem; background:rgba(47,128,255,.08); color:#b8c7f6; font-size:.84rem; margin-top:.5rem; }
        .blue-text { color: var(--blue); }
        .red-text { color: var(--red); }
        .green-text { color: var(--green); }
        .gold-text { color: var(--gold); }

        .mvp-grid { display:grid; grid-template-columns: repeat(5, minmax(0,1fr)); gap:.55rem; margin-top:.7rem; }
        .mvp-card { border-radius: 18px; padding:.72rem .62rem; background:rgba(8,14,28,.75); border:1px solid rgba(122,145,255,.16); text-align:center; }
        .mvp-card .name { color:#f3f6ff; font-weight:900; font-size:.86rem; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
        .mvp-card .role { color:#9aa6c7; font-size:.72rem; margin-top:.12rem; }
        .change-grid { display:grid; grid-template-columns: repeat(5, minmax(0,1fr)); gap:.55rem; }
        .change-card { border-radius:17px; padding:.62rem; background:rgba(8,14,28,.75); border:1px solid rgba(122,145,255,.14); }
        .change-role { font-weight:950; color:#f4f7ff; margin-bottom:.45rem; }
        .change-row { display:flex; justify-content:space-between; align-items:center; font-size:.76rem; padding:.32rem 0; border-top:1px solid rgba(255,255,255,.055); }
        .delta-pos { color:#66eaa2; font-weight:950; }
        .delta-neg { color:#ff7c93; font-weight:950; }

        .profile-head { display:grid; grid-template-columns: auto 1fr auto auto; gap:1rem; align-items:center; }
        .profile-avatar { width:78px; height:78px; border-radius: 26px; background:linear-gradient(135deg, rgba(47,128,255,.40), rgba(155,124,255,.45)); border:1px solid rgba(185,198,255,.25); display:flex; align-items:center; justify-content:center; font-size:2rem; box-shadow: inset 0 0 28px rgba(255,255,255,.08); }
        .profile-name { font-size:1.6rem; font-weight:950; color:#f8faff; letter-spacing:-.03em; }
        .profile-meta { color:var(--muted); font-size:.84rem; margin-top:.16rem; }
        .role-bar-row { display:grid; grid-template-columns: 58px 1fr 78px; gap:.5rem; align-items:center; margin-bottom:.62rem; }
        .role-bar-label { color:#d9e1ff; font-weight:900; font-size:.78rem; }
        .role-bar-track { height:12px; border-radius:999px; background:rgba(255,255,255,.06); overflow:hidden; border:1px solid rgba(255,255,255,.05); }
        .role-bar-fill { height:100%; border-radius:999px; background:linear-gradient(90deg, #2f80ff, #9b7cff); }
        .role-bar-value { text-align:right; color:#f6f8ff; font-weight:950; font-size:.82rem; }

        @media (max-width: 1100px) {
            .metric-grid, .mvp-grid, .change-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
            .slot-row { grid-template-columns: 64px 1fr 60px; }
            .slot-row .status-pill { display:none; }
            .hero-row, .team-head, .profile-head { flex-direction:column; display:flex; }
        }

        .dashboard-kpi-grid {
            display:grid;
            grid-template-columns: repeat(4, minmax(0, 1fr));
            gap:.8rem;
            margin-bottom: 1rem;
        }
        .dashboard-kpi-card {
            border: 1px solid rgba(122, 145, 255, .18);
            border-radius: 18px;
            background: linear-gradient(180deg, rgba(16, 24, 46, .92), rgba(9, 14, 28, .92));
            padding: .95rem 1rem;
            box-shadow: 0 12px 28px rgba(0,0,0,.18);
        }
        .dashboard-kpi-label {
            color: var(--muted);
            font-size: .78rem;
            font-weight: 700;
        }
        .dashboard-kpi-value {
            color: #f8faff;
            font-size: 1.45rem;
            font-weight: 950;
            margin-top: .18rem;
            letter-spacing: -.03em;
        }
        .dashboard-kpi-sub {
            color: #8a98bf;
            font-size: .74rem;
            margin-top: .12rem;
        }

        .dashboard-top-grid {
            display:grid;
            grid-template-columns: repeat(3, minmax(0, 1fr));
            gap:.8rem;
            margin-bottom: 1rem;
        }
        .dashboard-top-card {
            border-radius: 20px;
            padding: 1rem;
            border: 1px solid rgba(122,145,255,.18);
            background: linear-gradient(180deg, rgba(17, 26, 48, .94), rgba(8, 13, 26, .94));
            box-shadow: 0 16px 34px rgba(0,0,0,.20);
        }
        .dashboard-top-rank {
            font-size: .78rem;
            font-weight: 900;
            letter-spacing: .08em;
            color: #9fb0e8;
            margin-bottom: .35rem;
        }
        .dashboard-top-name {
            color:#f8fbff;
            font-size:1.15rem;
            font-weight:950;
            margin-bottom:.25rem;
        }
        .dashboard-top-meta {
            color:#9aa6c7;
            font-size:.82rem;
            margin-bottom:.7rem;
        }
        .dashboard-top-stats {
            display:grid;
            grid-template-columns: repeat(3, minmax(0,1fr));
            gap:.45rem;
        }
        .dashboard-stat-box {
            border-radius: 14px;
            padding: .6rem .55rem;
            background: rgba(255,255,255,.04);
            border: 1px solid rgba(255,255,255,.05);
        }
        .dashboard-stat-box .label {
            color:#8b98bc;
            font-size:.68rem;
            font-weight:700;
        }
        .dashboard-stat-box .value {
            color:#f5f8ff;
            font-size:.95rem;
            font-weight:900;
            margin-top:.1rem;
        }

        .dashboard-rank-table {
            width:100%;
            border-collapse: separate;
            border-spacing: 0 .48rem;
        }
        .dashboard-rank-table th {
            color:#8c99bf;
            font-size:.73rem;
            font-weight:800;
            text-align:left;
            padding: 0 .65rem .15rem;
        }
        .dashboard-rank-table td {
            background: rgba(9, 15, 29, .78);
            border-top: 1px solid rgba(110, 132, 255, .14);
            border-bottom: 1px solid rgba(110, 132, 255, .14);
            padding: .68rem .65rem;
            color:#e9edff;
            vertical-align: middle;
        }
        .dashboard-rank-table tr td:first-child {
            border-left: 1px solid rgba(110,132,255,.14);
            border-radius: 14px 0 0 14px;
        }
        .dashboard-rank-table tr td:last-child {
            border-right: 1px solid rgba(110,132,255,.14);
            border-radius: 0 14px 14px 0;
        }
        .rank-badge {
            display:inline-flex;
            align-items:center;
            justify-content:center;
            min-width:34px;
            height:28px;
            border-radius:999px;
            background: rgba(120,145,255,.12);
            border:1px solid rgba(120,145,255,.22);
            color:#eef3ff;
            font-size:.76rem;
            font-weight:900;
        }
        .player-strong {
            color:#f6f9ff;
            font-weight:900;
        }
        .winrate-track {
            width: 120px;
            height: 10px;
            border-radius: 999px;
            background: rgba(255,255,255,.06);
            border:1px solid rgba(255,255,255,.04);
            overflow:hidden;
            margin-top:.22rem;
        }
        .winrate-fill {
            height:100%;
            border-radius:999px;
            background: linear-gradient(90deg, #2f80ff, #73baff);
        }
        .lp-pos { color: #6ee7b7; font-weight: 900; }
        .lp-neg { color: #ff8aa0; font-weight: 900; }
        .lp-mid { color: #f3f6ff; font-weight: 900; }

        @media (max-width: 1100px) {
            .dashboard-kpi-grid,
            .dashboard-top-grid {
                grid-template-columns: repeat(2, minmax(0,1fr));
            }
        }
        </style>
        """
    )


def render_sidebar_brand() -> None:
    render_html(
        """
        <div class="app-logo">
            <div class="logo-mark">⚖</div>
            <div>
                <div class="logo-text-main">INHOUSE<br/>BALANCER</div>
                <div class="logo-text-sub">for League of Legends</div>
            </div>
        </div>
        """,
        container=st.sidebar,
    )


def render_hero(eyebrow: str, title: str, subtitle: str, right_html: str = "") -> None:
    render_html(
        f"""
        <div class="hero">
            <div class="hero-row">
                <div>
                    <div class="eyebrow">{e(eyebrow)}</div>
                    <div class="hero-title">{e(title)}</div>
                    <div class="hero-subtitle">{e(subtitle)}</div>
                </div>
                <div class="hero-actions">{right_html}</div>
            </div>
        </div>
        """
    )


def panel_start(title: str, subtitle: str | None = None, tag: str | None = None, tight: bool = False) -> None:
    """Render a closed section header card.

    Earlier versions tried to open a <div> here and close it later. Streamlit
    renders each markdown/html call separately, so split open/close tags can leak
    as visible source code. Keep this HTML self-contained.
    """
    sub = f'<div class="panel-subtitle">{e(subtitle)}</div>' if subtitle else ""
    tag_html = f'<div class="subtle-tag">{e(tag)}</div>' if tag else ""
    klass = "panel tight section-head" if tight else "panel section-head"
    render_html(
        f"""
        <div class="{klass}">
            <div class="panel-title-row">
                <div>
                    <div class="panel-title">{e(title)}</div>
                    {sub}
                </div>
                {tag_html}
            </div>
        </div>
        """
    )


def panel_end() -> None:
    # Intentionally no-op. See panel_start docstring.
    return None


def metric_cards(metrics: list[tuple[str, str, str]]) -> str:
    cards = []
    for label, value, hint in metrics:
        cards.append(
            f"""
            <div class="metric-card">
                <div class="metric-label">{e(label)}</div>
                <div class="metric-value">{e(value)}</div>
                <div class="metric-hint">{e(hint)}</div>
            </div>
            """
        )
    return f'<div class="metric-grid">{"".join(cards)}</div>'


def role_chip(role: str, rating: float | None = None) -> str:
    meta = ROLE_META[role]
    if rating is None:
        return f'<span class="role-chip {meta["class"]}">{meta["icon"]} {role}</span>'
    return (
        f'<span class="role-chip {meta["class"]}">'
        f'<span>{meta["icon"]}</span><span>{role}</span> <b class="{rating_color_class(rating)}">{rating:.0f}</b>'
        f'</span>'
    )


def pref_pills(preferred_roles: Iterable[str]) -> str:
    roles = list(preferred_roles)
    if not roles:
        return '<span class="pref-pill">상관없음</span>'
    return "".join(f'<span class="pref-pill">{e(role)}</span>' for role in roles)


def players_to_df(players) -> pd.DataFrame:
    return pd.DataFrame([p.to_row() for p in players])


def team_df(assignment) -> pd.DataFrame:
    rows = []
    for role in ROLES:
        p = assignment.slots[role]
        rating = p.rating_for(role)
        rows.append(
            {
                "포지션": role,
                "소환사": p.name,
                "실력치": round(rating, 1),
                "추정 티어": rating_to_tier_label(rating),
                "선호 일치": "일치" if p.preference_penalty(role) <= 2 else "오프롤",
            }
        )
    return pd.DataFrame(rows)


def change_df(changes) -> pd.DataFrame:
    rows = []
    for c in changes:
        if is_dataclass(c):
            item = asdict(c)
        else:
            item = c
        rows.append(
            {
                "팀": item["team"],
                "포지션": item["role"],
                "플레이어": item["player_name"],
                "변경 전": item["before"],
                "변경 후": item["after"],
                "변화량": item["delta"],
                "신뢰도": item["confidence_after"],
                "사유": item["reason"],
            }
        )
    return pd.DataFrame(rows)


def current_candidate(conn):
    candidate = st.session_state.get("latest_candidate")
    if candidate is not None:
        return candidate
    players = list_players(conn)[:10]
    if len(players) == 10:
        candidate = generate_balanced_teams(players, top_k=1)[0]
        st.session_state["latest_candidate"] = candidate
        return candidate
    return None


def player_pool_sorted(
    players,
    *,
    sort_key: str = "DEFAULT",
    descending: bool = True,
) -> list:
    """Return players in the order used by the player-pool table.

    DEFAULT keeps the DB/list order. Role sort keys order by the selected role
    rating, with player label as a deterministic tie-breaker.
    """
    if sort_key not in {"DEFAULT", *ROLES}:
        sort_key = "DEFAULT"
    if sort_key == "DEFAULT":
        return list(players)

    def key_fn(player):
        rating = float(player.rating_for(sort_key))
        primary = -rating if descending else rating
        return (primary, player.label_name.casefold(), player.name.casefold())

    return sorted(players, key=key_fn)


def render_player_pool_sort_controls() -> tuple[str, bool]:
    """Render compact role-sort controls for the player pool."""
    sort_key = st.session_state.get("player_pool_sort_key", "DEFAULT")
    descending = bool(st.session_state.get("player_pool_sort_desc", True))

    st.caption("플레이어 풀 정렬 · TOP/JG/MID/ADC/SUP 버튼을 누르면 해당 라인 점수 기준으로 정렬됩니다. 같은 버튼을 다시 누르면 높은순/낮은순이 전환됩니다.")
    cols = st.columns([0.9, 0.8, 0.8, 0.8, 0.8, 0.8, 1.15], gap="small")
    options = [("기본", "DEFAULT"), *[(role, role) for role in ROLES]]

    for col, (label, key) in zip(cols[:6], options):
        active = sort_key == key
        suffix = ""
        if active and key in ROLES:
            suffix = " ↓" if descending else " ↑"
        button_label = f"✓ {label}{suffix}" if active else label
        with col:
            if st.button(button_label, key=f"player_pool_sort_{key}", use_container_width=True):
                if key == "DEFAULT":
                    st.session_state["player_pool_sort_key"] = "DEFAULT"
                    st.session_state["player_pool_sort_desc"] = True
                elif sort_key == key:
                    st.session_state["player_pool_sort_desc"] = not descending
                else:
                    st.session_state["player_pool_sort_key"] = key
                    st.session_state["player_pool_sort_desc"] = True
                st.rerun()

    with cols[6]:
        current = "기본 순서" if sort_key == "DEFAULT" else f"{sort_key} {'높은순' if descending else '낮은순'}"
        st.markdown(f"<div style='padding:.75rem .1rem; color:#9fb0e8; font-weight:800; font-size:.82rem;'>현재: {e(current)}</div>", unsafe_allow_html=True)

    return str(st.session_state.get("player_pool_sort_key", "DEFAULT")), bool(st.session_state.get("player_pool_sort_desc", True))


def _pool_sort_state() -> tuple[str, bool]:
    sort_key = st.session_state.get("player_pool_sort_key", DEFAULT_POOL_SORT)
    descending = bool(st.session_state.get("player_pool_sort_desc", True))
    if sort_key not in POOL_SORT_OPTIONS:
        sort_key = DEFAULT_POOL_SORT
    return str(sort_key), descending


def _sort_button_label(option: str, current: str, descending: bool) -> str:
    if option != current or option == DEFAULT_POOL_SORT:
        return option
    return f"{option} {'↓' if descending else '↑'}"


def render_player_pool_sort_controls() -> tuple[str, bool]:
    current, descending = _pool_sort_state()

    cols = st.columns([0.9, 0.72, 0.72, 0.72, 0.72, 0.72, 0.98, 0.82])
    changed = False
    for col, option in zip(cols, POOL_SORT_OPTIONS, strict=True):
        active = option == current
        with col:
            clicked = st.button(
                _sort_button_label(option, current, descending),
                key=f"player_pool_sort_btn_{option}",
                use_container_width=True,
                type="primary" if active and option != DEFAULT_POOL_SORT else "secondary",
                help=(
                    "기본 등록 순서로 표시합니다."
                    if option == DEFAULT_POOL_SORT
                    else f"{option} 기준으로 {'내림차순/오름차순을 전환' if active else '높은 순서 정렬'}합니다."
                ),
            )
        if clicked:
            if option == DEFAULT_POOL_SORT:
                st.session_state["player_pool_sort_key"] = DEFAULT_POOL_SORT
                st.session_state["player_pool_sort_desc"] = True
            elif option == current:
                st.session_state["player_pool_sort_desc"] = not descending
            else:
                st.session_state["player_pool_sort_key"] = option
                st.session_state["player_pool_sort_desc"] = option != NAME_POOL_SORT
            changed = True

    if changed:
        st.rerun()

    return _pool_sort_state()


def render_player_pool(
    players,
    selected_names: set[str] | None = None,
    sort_key: str | None = None,
    sort_desc: bool | None = None,
) -> None:
    """Render the Team Builder player pool table.

    v11 originally rendered the sort controls outside this function but the
    function signature was not updated, which caused:
    TypeError: render_player_pool() got an unexpected keyword argument 'sort_key'.

    If sort_key/sort_desc are provided by the caller, use them directly and do
    not render a second copy of the sort controls. If they are omitted, keep the
    function usable as a standalone renderer by rendering controls here.
    """
    selected_names = selected_names or set()
    if sort_key is None:
        sort_key, descending = render_player_pool_sort_controls()
    else:
        descending = True if sort_desc is None else bool(sort_desc)
    visible_players = sorted_players_for_pool(players, sort_key, descending=descending)

    rows = []
    for idx, p in enumerate(visible_players, start=1):
        selected = "✓" if p.name in selected_names else str(idx)
        role_cells = "".join(f"<td>{role_chip(role, p.rating_for(role))}</td>" for role in ROLES)
        tier = f"{p.solo_tier} {p.solo_rank}".strip()
        flex_tier = f"{getattr(p, 'flex_tier', 'UNRANKED')} {getattr(p, 'flex_rank', '')}".strip()
        rows.append(
            f"""
            <tr>
                <td style="width:42px; text-align:center; color:#9fb0e8; font-weight:900;">{e(selected)}</td>
                <td>
                    <div class="player-cell">
                        <span class="avatar">{e(p.label_name[:1])}</span>
                        <div>
                            <div class="player-name">{e(p.label_name)}</div>
                            <div class="player-id">{e(p.name)} · ID {e(p.id)} · {e(p.riot_id)}</div>
                        </div>
                    </div>
                </td>
                <td>
                    <span class="tier-badge">Solo {e(tier)}</span>
                    <div class="player-id">Flex {e(flex_tier)}</div>
                </td>
                {role_cells}
                <td>{pref_pills(p.preferred_roles)}</td>
            </tr>
            """
        )

    def role_header(role: str) -> str:
        active = sort_key == role
        arrow = "↓" if active and descending else "↑" if active else "↕"
        return f'<span class="sort-head {"active" if active else ""}">{role}<span class="sort-arrow">{arrow}</span></span>'

    sort_text = "등록 순서" if sort_key == DEFAULT_POOL_SORT else f"{sort_key} {'높은 순' if descending else '낮은 순'}"

    render_html(
        f"""
        <div class="panel">
            <div class="panel-title-row">
                <div>
                    <div class="panel-title">플레이어 풀</div>
                    <div class="panel-subtitle">라인별 실력치는 솔로랭크와 별도로 추정됩니다.</div>
                </div>
                <div class="subtle-tag">{len(selected_names) if selected_names else len(players)} / 10 selected</div>
            </div>
            <div class="sort-hint">
                <div>위 정렬 버튼에서 <b>TOP/JG/MID/ADC/SUP</b>을 누르면 해당 라인 점수 기준으로 정렬됩니다.</div>
                <div class="subtle-tag">정렬: {e(sort_text)}</div>
            </div>
            <table class="player-table">
                <thead>
                    <tr>
                        <th>#</th><th>이름 / 소환사</th><th>솔로랭크</th>
                        <th>{role_header('TOP')}</th><th>{role_header('JG')}</th><th>{role_header('MID')}</th><th>{role_header('ADC')}</th><th>{role_header('SUP')}</th><th>선호 포지션</th>
                    </tr>
                </thead>
                <tbody>{''.join(rows)}</tbody>
            </table>
        </div>
        """
    )


def champion_pills_html(player, role: str) -> str:
    picks = (getattr(player, "lane_champions", {}) or {}).get(role, [])[:3]
    if not picks:
        return '<span class="champ-pill empty">Most 기록 없음</span>'

    pills = []
    for item in picks:
        champion = item.get("champion", "") if isinstance(item, dict) else str(item)
        games = item.get("games", 0) if isinstance(item, dict) else 0
        label = f"{champion} {games}판" if games else champion
        pills.append(f'<span class="champ-pill">{e(label)}</span>')
    return "".join(pills)


def team_card_html(assignment) -> str:
    total = float(assignment.total_rating)
    fill = max(6, min(100, total / 5.0))
    team = assignment.team.upper()
    rows = []
    for role in ROLES:
        p = assignment.slots[role]
        rating = p.rating_for(role)
        tier = rating_to_tier_label(rating)
        ok = p.preference_penalty(role) <= 2.0
        status_class = "status-ok" if ok else "status-warn"
        status_text = "일치" if ok else "오프롤"
        meta = ROLE_META[role]
        champ_pills = champion_pills_html(p, role)
        rows.append(
            f"""
            <div class="slot-row">
                <div class="slot-role"><span>{meta['icon']}</span><span>{role}</span></div>
                <div>
                    <div class="slot-player">{e(p.label_name)}</div>
                    <div class="slot-tier">{e(p.name)} · {e(meta['label'])} · {e(tier)} · 선호 {', '.join(p.preferred_roles) if p.preferred_roles else '상관없음'}</div>
                    <div class="slot-champs">{champ_pills}</div>
                </div>
                <div class="slot-rating"><span class="{rating_color_class(rating)}">{rating:.1f}</span><small>{e(tier)}</small></div>
                <div><span class="status-pill {status_class}">{status_text}</span></div>
            </div>
            """
        )
    return f"""
    <div class="team-card {team_class(team)}">
        <div class="team-head">
            <div>
                <div class="team-name">{team} TEAM</div>
                <div class="panel-subtitle">라인별 배정 결과</div>
            </div>
            <div class="team-total">
                <div class="label">팀 총 실력치</div>
                <div class="value">{total:.1f}</div>
                <div class="strength-bar"><div class="strength-fill" style="width:{fill:.1f}%"></div></div>
            </div>
        </div>
        {''.join(rows)}
    </div>
    """


def balance_summary_html(candidate) -> str:
    blue_pct = candidate.expected_blue_win * 100.0
    red_pct = 100.0 - blue_pct
    blue_width = max(2, min(98, blue_pct))
    red_width = max(2, min(98, red_pct))
    return f"""
    <div class="balance-card">
        <div class="panel-title-row">
            <div>
                <div class="panel-title">밸런스 요약</div>
                <div class="panel-subtitle">예상 승률이 50:50에 가까울수록 좋습니다.</div>
            </div>
            <div class="subtle-tag">gap {candidate.rating_gap:.2f}</div>
        </div>
        <div class="versus"><span class="vs-mark">VS</span></div>
        <div class="winrate-line"><span class="blue-text">BLUE {blue_pct:.1f}%</span><span class="red-text">RED {red_pct:.1f}%</span></div>
        <div class="winrate-bar">
            <div class="win-blue" style="width:{blue_width:.1f}%"></div>
            <div class="win-red" style="width:{red_width:.1f}%"></div>
        </div>
        <div class="summary-list">
            <div class="summary-item"><span>평균 팀 실력 차이</span><span>{candidate.rating_gap:.2f}</span></div>
            <div class="summary-item"><span>선호 포지션 페널티</span><span>{candidate.preference_penalty:.1f}</span></div>
            <div class="summary-item"><span>목적함수 점수</span><span>{candidate.objective:.2f}</span></div>
            <div class="summary-item"><span>추천 판단</span><span class="green-text">{'매우 근소' if candidate.rating_gap < 4 else '확인 필요'}</span></div>
        </div>
        <div class="info-strip">ⓘ 오프롤 배정이 많으면 실력 차이가 작아도 실제 체감 밸런스가 나빠질 수 있습니다.</div>
    </div>
    """


def render_team_cards(candidate) -> None:
    c1, c2, c3 = st.columns([1.25, 0.82, 1.25], gap="large")
    with c1:
        render_html(team_card_html(candidate.blue))
    with c2:
        render_html(balance_summary_html(candidate))
    with c3:
        render_html(team_card_html(candidate.red))


def roster_compact_html(assignment, title: str) -> str:
    rows = []
    for role in ROLES:
        p = assignment.slots[role]
        rows.append(
            f"""
            <div class="slot-row" style="grid-template-columns:70px 1fr 72px; min-height:52px;">
                <div class="slot-role">{ROLE_META[role]['icon']} {role}</div>
                <div><div class="slot-player">{e(p.label_name)}</div><div class="slot-tier">{e(p.name)}</div></div>
                <div class="slot-rating"><span class="{rating_color_class(p.rating_for(role))}">{p.rating_for(role):.0f}</span></div>
            </div>
            """
        )
    return f"""
    <div class="panel tight">
        <div class="panel-title-row"><div class="panel-title">{e(title)}</div><div class="subtle-tag">{assignment.total_rating:.1f}</div></div>
        {''.join(rows)}
    </div>
    """


def mvp_preview_html(assignment) -> str:
    cards = []
    for role in ROLES:
        p = assignment.slots[role]
        cards.append(
            f"""
            <div class="mvp-card">
                <div class="avatar" style="margin:0 auto .45rem;">{e(p.label_name[:1])}</div>
                <div class="name">{e(p.label_name)}</div>
                <div class="role">{ROLE_META[role]['icon']} {role} · {p.rating_for(role):.0f}</div>
            </div>
            """
        )
    return f'<div class="mvp-grid">{"".join(cards)}</div>'


def change_preview_html(changes) -> str:
    by_role: dict[str, list[dict]] = {role: [] for role in ROLES}
    for c in changes:
        item = asdict(c) if is_dataclass(c) else c
        by_role[item["role"]].append(item)

    role_cards = []
    for role in ROLES:
        rows = []
        for item in sorted(by_role[role], key=lambda x: 0 if x["team"] == "BLUE" else 1):
            delta = float(item["delta"])
            klass = "delta-pos" if delta >= 0 else "delta-neg"
            sign = "+" if delta >= 0 else ""
            rows.append(
                f"""
                <div class="change-row">
                    <span class="{'blue-text' if item['team'] == 'BLUE' else 'red-text'}">{e(item['team'])}</span>
                    <span title="{e(item['reason'])}">{e(item['player_name'])}</span>
                </div>
                <div class="change-row">
                    <span>{item['before']:.1f} → {item['after']:.1f}</span>
                    <span class="{klass}">{sign}{delta:.1f}</span>
                </div>
                """
            )
        role_cards.append(
            f"""
            <div class="change-card">
                <div class="change-role">{ROLE_META[role]['icon']} {role}</div>
                {''.join(rows)}
            </div>
            """
        )
    return f'<div class="change-grid">{"".join(role_cards)}</div>'


def role_bars_html(player) -> str:
    rows = []
    for role in ROLES:
        rating = player.rating_for(role)
        fill = max(3, min(100, rating))
        rows.append(
            f"""
            <div class="role-bar-row">
                <div class="role-bar-label">{ROLE_META[role]['icon']} {role}</div>
                <div class="role-bar-track"><div class="role-bar-fill" style="width:{fill:.1f}%"></div></div>
                <div class="role-bar-value"><span class="{rating_color_class(rating)}">{rating:.1f}</span> <span style="color:#8c98bc">{rating_to_tier_label(rating)}</span></div>
            </div>
            """
        )
    return "".join(rows)


def page_dashboard(conn) -> None:
    players = list_players(conn)
    matches = list_matches(conn, limit=5000)

    render_hero(
        "Dashboard",
        "내전 대시보드",
        "플레이어별 누적 성적, 승률, 내전 LP를 한눈에 확인합니다.",
    )

    if not players:
        st.info("등록된 플레이어가 없습니다.")
        return

    rows = []
    for p in players:
        history = list_player_match_history(conn, int(p.id), limit=10000)

        wins = 0
        losses = 0
        for h in history:
            is_win = (
                (h["team"] == "BLUE" and h["blue_win"])
                or (h["team"] == "RED" and not h["blue_win"])
            )
            if is_win:
                wins += 1
            else:
                losses += 1

        total_games = wins + losses
        winrate = (wins / total_games * 100.0) if total_games else 0.0
        inhouse_lp = int(round((wins - losses) * 15 + winrate))

        rows.append(
            {
                "플레이어": p.label_name,
                "승": wins,
                "패": losses,
                "승률_num": winrate,
                "승률": f"{winrate:.1f}%",
                "내전 LP": inhouse_lp,
                "경기 수": total_games,
            }
        )

    df = pd.DataFrame(rows).sort_values(
        by=["내전 LP", "승률_num", "승"],
        ascending=[False, False, False],
    ).reset_index(drop=True)

    total_matches = len(matches)
    total_players = len(players)

    top_winrate_player = df.loc[df["승률_num"].idxmax(), "플레이어"] if not df.empty else "-"
    top_lp_player = df.loc[df["내전 LP"].idxmax(), "플레이어"] if not df.empty else "-"

    render_html(
        f"""
        <div class="dashboard-kpi-grid">
            <div class="dashboard-kpi-card">
                <div class="dashboard-kpi-label">등록 플레이어</div>
                <div class="dashboard-kpi-value">{total_players}</div>
                <div class="dashboard-kpi-sub">현재 내전 참가 풀</div>
            </div>
            <div class="dashboard-kpi-card">
                <div class="dashboard-kpi-label">누적 경기 수</div>
                <div class="dashboard-kpi-value">{total_matches}</div>
                <div class="dashboard-kpi-sub">저장된 전체 경기</div>
            </div>
            <div class="dashboard-kpi-card">
                <div class="dashboard-kpi-label">최고 승률 플레이어</div>
                <div class="dashboard-kpi-value">{e(top_winrate_player)}</div>
                <div class="dashboard-kpi-sub">승률 기준 리더</div>
            </div>
            <div class="dashboard-kpi-card">
                <div class="dashboard-kpi-label">최고 내전 LP</div>
                <div class="dashboard-kpi-value">{e(top_lp_player)}</div>
                <div class="dashboard-kpi-sub">현재 랭킹 1위</div>
            </div>
        </div>
        """
    )

    top3 = df.head(3).to_dict("records")
    top_cards = []
    medals = ["🥇", "🥈", "🥉"]
    for i, row in enumerate(top3):
        top_cards.append(
            f"""
            <div class="dashboard-top-card">
                <div class="dashboard-top-rank">{medals[i]} TOP {i+1}</div>
                <div class="dashboard-top-name">{e(row["플레이어"])}</div>
                <div class="dashboard-top-meta">{row["경기 수"]} games · {row["승"]}승 {row["패"]}패</div>
                <div class="dashboard-top-stats">
                    <div class="dashboard-stat-box">
                        <div class="label">승률</div>
                        <div class="value">{row["승률"]}</div>
                    </div>
                    <div class="dashboard-stat-box">
                        <div class="label">내전 LP</div>
                        <div class="value">{row["내전 LP"]}</div>
                    </div>
                    <div class="dashboard-stat-box">
                        <div class="label">순위</div>
                        <div class="value">#{i+1}</div>
                    </div>
                </div>
            </div>
            """
        )

    render_html(
        f"""
        <div class="panel">
            <div class="panel-title-row">
                <div>
                    <div class="panel-title">TOP 3 플레이어</div>
                    <div class="panel-subtitle">현재 내전 성적 기준 상위 플레이어입니다.</div>
                </div>
            </div>
            <div class="dashboard-top-grid">
                {''.join(top_cards)}
            </div>
        </div>
        """
    )

    table_rows = []
    for idx, row in df.iterrows():
        winrate = float(row["승률_num"])
        lp = int(row["내전 LP"])
        lp_class = "lp-pos" if lp > 0 else "lp-neg" if lp < 0 else "lp-mid"

        table_rows.append(
            f"""
            <tr>
                <td><span class="rank-badge">#{idx+1}</span></td>
                <td><span class="player-strong">{e(row["플레이어"])}</span></td>
                <td>{row["승"]}</td>
                <td>{row["패"]}</td>
                <td>
                    <div>{row["승률"]}</div>
                    <div class="winrate-track">
                        <div class="winrate-fill" style="width:{winrate:.1f}%"></div>
                    </div>
                </td>
                <td><span class="{lp_class}">{lp}</span></td>
                <td style="color:#9aa6c7;">{row["경기 수"]}</td>
            </tr>
            """
        )

    render_html(
        f"""
        <div class="panel">
            <div class="panel-title-row">
                <div>
                    <div class="panel-title">내전 랭킹</div>
                    <div class="panel-subtitle">내전 LP, 승률, 승수를 기준으로 정렬됩니다.</div>
                </div>
                <div class="subtle-tag">Ranking Table</div>
            </div>

            <table class="dashboard-rank-table">
                <thead>
                    <tr>
                        <th style="width:70px;">순위</th>
                        <th>플레이어</th>
                        <th>승</th>
                        <th>패</th>
                        <th>승률</th>
                        <th>내전 LP</th>
                        <th>경기 수</th>
                    </tr>
                </thead>
                <tbody>
                    {''.join(table_rows)}
                </tbody>
            </table>
        </div>
        """
    )

def _candidate_editor_version() -> int:
    return int(st.session_state.get("assignment_editor_version", 0))


def _bump_candidate_editor_version() -> None:
    st.session_state["assignment_editor_version"] = _candidate_editor_version() + 1


def assignment_editor_df(candidate) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"포지션": role, "BLUE": candidate.blue.slots[role].name, "RED": candidate.red.slots[role].name}
            for role in ROLES
        ]
    )


def candidate_from_editor_df(editor_df: pd.DataFrame, players, *, config: dict[str, float] | None = None):
    lookup = {p.name: p for p in players}
    blue_slots = {}
    red_slots = {}
    selected_names: list[str] = []
    for _, row in editor_df.iterrows():
        role = str(row["포지션"]).strip().upper()
        if role not in ROLES:
            raise ValueError(f"알 수 없는 포지션: {role}")
        blue_name = str(row["BLUE"]).strip()
        red_name = str(row["RED"]).strip()
        if blue_name not in lookup:
            raise ValueError(f"BLUE {role} 플레이어를 찾을 수 없습니다: {blue_name}")
        if red_name not in lookup:
            raise ValueError(f"RED {role} 플레이어를 찾을 수 없습니다: {red_name}")
        blue_slots[role] = lookup[blue_name]
        red_slots[role] = lookup[red_name]
        selected_names.extend([blue_name, red_name])

    duplicates = sorted({name for name in selected_names if selected_names.count(name) > 1})
    if duplicates:
        raise ValueError("한 플레이어가 여러 슬롯에 들어가 있습니다: " + ", ".join(duplicates))
    if len(selected_names) != 10:
        raise ValueError("정확히 10개 슬롯이 필요합니다.")
    if len(set(selected_names)) != 10:
        raise ValueError("10개 슬롯에는 서로 다른 플레이어 10명이 들어가야 합니다.")

    return candidate_from_assignments(
        make_assignment("BLUE", blue_slots),
        make_assignment("RED", red_slots),
        config=config,
    )


def sortable_label(player) -> str:
    label = getattr(player, "label_name", None) or player.name
    if label != player.name:
        return f"{label} · {player.name}"
    return player.name


def assignment_sortable_containers(candidate) -> list[dict[str, object]]:
    return [
        {"header": "BLUE", "items": [sortable_label(candidate.blue.slots[role]) for role in ROLES]},
        {"header": "RED", "items": [sortable_label(candidate.red.slots[role]) for role in ROLES]},
    ]


DRAG_BOARD_STYLE = """
.sortable-component.vertical {
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
    gap: 18px;
    align-items: stretch;
    background: transparent;
    padding: 2px 0 0 0;
}

.sortable-component.vertical .sortable-container {
    min-width: 0;
    margin: 0;
    padding: 0;
    border-radius: 18px;
    overflow: hidden;
    border: 1px solid rgba(255, 91, 118, .42);
    background: rgba(10, 18, 37, .84);
}

/* 실제 화면상 BLUE에 먹는 selector */
.sortable-component.vertical .sortable-container:nth-child(2) {
    border-color: rgba(74, 143, 255, .45);
}

/* 기본 헤더 = RED */
.sortable-container-header {
    margin: 0;
    padding: 12px 16px;
    color: #f8fbff;
    font-weight: 900;
    letter-spacing: .08em;
    font-size: 15px;
    background: linear-gradient(135deg, rgba(193, 50, 78, .52), rgba(16, 24, 50, .85));
}

/* 실제 화면상 BLUE 헤더에 먹는 selector */
.sortable-container:nth-child(2) .sortable-container-header {
    background: linear-gradient(135deg, rgba(45, 94, 189, .52), rgba(16, 24, 50, .85));
}

.sortable-container-header::after {
    content: "  ·  위에서부터 TOP / JG / MID / ADC / SUP";
    color: #93a7df;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: .02em;
}

.sortable-container-body {
    margin: 0;
    padding: 10px;
    width: 100%;
    min-height: 286px;
    border-radius: 0;
    background: rgba(5, 10, 23, .38);
    counter-reset: slot;
}

/* 기본 아이템 = RED */
.sortable-item,
.sortable-item:hover {
    display: flex !important;
    align-items: center;
    min-height: 42px;
    margin: 8px 0;
    padding: 9px 12px;
    border-radius: 12px;
    background: rgba(239, 88, 112, .86);
    color: #ffffff;
    font-size: 14px;
    font-weight: 800;
    line-height: 1.25;
    box-shadow: 0 10px 22px rgba(0, 0, 0, .24);
}

/* 실제 화면상 BLUE 아이템에 먹는 selector */
.sortable-container:nth-child(2) .sortable-item,
.sortable-container:nth-child(2) .sortable-item:hover {
    background: rgba(65, 140, 255, .86);
}

.sortable-item::before {
    counter-increment: slot;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-width: 48px;
    height: 24px;
    margin-right: 10px;
    border-radius: 999px;
    background: rgba(1, 8, 24, .34);
    color: #ffffff;
    font-size: 11px;
    font-weight: 950;
    letter-spacing: .05em;
}

.sortable-item:nth-child(1)::before { content: "TOP"; }
.sortable-item:nth-child(2)::before { content: "JG"; }
.sortable-item:nth-child(3)::before { content: "MID"; }
.sortable-item:nth-child(4)::before { content: "ADC"; }
.sortable-item:nth-child(5)::before { content: "SUP"; }

.sortable-item:active {
    cursor: grabbing;
    transform: scale(.99);
}

.active {
    opacity: .62;
}

@media (max-width: 760px) {
    .sortable-component.vertical {
        grid-template-columns: 1fr;
    }
}
"""


def candidate_from_sortable_containers(containers, players, *, config: dict[str, float] | None = None):
    label_to_player = {sortable_label(p): p for p in players}
    if not isinstance(containers, list) or len(containers) < 2:
        raise ValueError("드래그 보드 결과를 읽을 수 없습니다.")

    blue_items = list(containers[0].get("items", []))
    red_items = list(containers[1].get("items", []))
    if len(blue_items) != 5 or len(red_items) != 5:
        raise ValueError(f"BLUE와 RED는 각각 5명이어야 합니다. 현재 BLUE {len(blue_items)}명, RED {len(red_items)}명입니다.")

    all_items = blue_items + red_items
    duplicates = sorted({item for item in all_items if all_items.count(item) > 1})
    if duplicates:
        raise ValueError("한 플레이어가 여러 슬롯에 들어가 있습니다: " + ", ".join(duplicates))
    if len(set(all_items)) != 10:
        raise ValueError("10개 슬롯에는 서로 다른 플레이어 10명이 들어가야 합니다.")

    unknown = [item for item in all_items if item not in label_to_player]
    if unknown:
        raise ValueError("알 수 없는 플레이어 라벨입니다: " + ", ".join(unknown))

    blue_slots = {role: label_to_player[item] for role, item in zip(ROLES, blue_items, strict=True)}
    red_slots = {role: label_to_player[item] for role, item in zip(ROLES, red_items, strict=True)}
    return candidate_from_assignments(
        make_assignment("BLUE", blue_slots),
        make_assignment("RED", red_slots),
        config=config,
    )


def team_copy_text(candidate, mode: str = "two_col_display") -> str:
    def display(player):
        return getattr(player, "label_name", None) or player.name

    return "\n".join(
        f"{display(candidate.blue.slots[role])}\t{display(candidate.red.slots[role])}"
        for role in ROLES
    )

def render_copy_export_panel(candidate) -> None:
    panel_start("팀 복붙", "위에서 아래 순서는 TOP / JG / MID / ADC / SUP입니다. 표시이름은 players.csv의 display_name/이름 컬럼을 사용합니다.", tag="Copy")
    text_value = team_copy_text(candidate, "two_col_display")
    col1, col2 = st.columns([0.85, 0.15])
    with col1:
        st.text_area(
            "복사 내용",
            value=text_value,
            height=150,
            disabled=True,
        )
    with col2:
        st.write("")
        st.write("")
        if st.button("📋 복사", use_container_width=True):
            import pyperclip
            try:
                pyperclip.copy(text_value)
                st.success("클립보드에 복사됨!")
            except Exception:
                st.error("복사 실패 (pyperclip 미설치)")
    panel_end()


def render_manual_adjustment_panel(candidate, players, *, config: dict[str, float] | None = None) -> None:
    panel_start(
        "수동 세부 조정",
        "BLUE와 RED를 좌우 2열로 놓고 드래그로 직접 조정합니다. 위에서 아래 순서는 TOP/JG/MID/ADC/SUP입니다.",
        tag="Drag balance",
    )
    st.caption(
        "왼쪽은 BLUE, 오른쪽은 RED입니다. 같은 팀 안에서 위아래로 옮기면 포지션이 바뀌고, BLUE↔RED 사이로 옮기면 팀이 바뀝니다. "
        "각 팀은 최종적으로 5명씩이어야 합니다."
    )

    use_drag = sort_items is not None
    sorted_containers = None

    if use_drag:
        render_html(
            """
            <div class="info-strip">↔ BLUE/RED가 좌우 2열로 표시됩니다. 같은 팀 안에서는 위아래로 포지션을 바꾸고, 양 팀 사이로 끌면 팀을 바꿀 수 있습니다.</div>
            """
        )
        sorted_containers = sort_items(
            assignment_sortable_containers(candidate),
            multi_containers=True,
            direction="vertical",
            custom_style=DRAG_BOARD_STYLE,
            key=f"assignment_sortable_{_candidate_editor_version()}",
        )
    else:
        st.warning(
            "streamlit-sortables 패키지가 설치되지 않아 드래그 보드를 사용할 수 없습니다. "
            "`pip install -r requirements.txt` 후 다시 실행하면 드래그 모드가 활성화됩니다."
        )
        names = [p.name for p in players]
        sorted_df = st.data_editor(
            assignment_editor_df(candidate),
            use_container_width=True,
            hide_index=True,
            num_rows="fixed",
            key=f"assignment_editor_{_candidate_editor_version()}",
            column_config={
                "포지션": st.column_config.TextColumn("포지션", disabled=True, width="small"),
                "BLUE": st.column_config.SelectboxColumn("BLUE", options=names, required=True),
                "RED": st.column_config.SelectboxColumn("RED", options=names, required=True),
            },
        )

    c1, c2, c3 = st.columns([0.34, 0.34, 0.32])
    with c1:
        apply_clicked = st.button("드래그 배정 적용" if use_drag else "수동 배정 적용", use_container_width=True)
    with c2:
        side_swap_clicked = st.button("좌우 스왑만 최적화", use_container_width=True)
    with c3:
        reset_clicked = st.button("자동 생성 결과로 되돌리기", use_container_width=True)

    if apply_clicked or side_swap_clicked:
        try:
            if use_drag:
                manual_candidate = candidate_from_sortable_containers(sorted_containers, players, config=config)
            else:
                manual_candidate = candidate_from_editor_df(sorted_df, players, config=config)

            if side_swap_clicked:
                optimized = optimize_side_swaps(
                    manual_candidate.blue,
                    manual_candidate.red,
                    config=config,
                )
                st.session_state["latest_candidate"] = optimized
                st.session_state["candidate_list"] = [optimized]
                _bump_candidate_editor_version()
                st.success(
                    f"좌우 스왑 최적화 완료: gap {manual_candidate.rating_gap:.2f} → {optimized.rating_gap:.2f}"
                )
                st.rerun()
            else:
                st.session_state["latest_candidate"] = manual_candidate
                st.session_state["candidate_list"] = [manual_candidate]
                _bump_candidate_editor_version()
                st.success(f"배정 적용 완료: gap={manual_candidate.rating_gap:.2f}")
                st.rerun()
        except Exception as exc:  # noqa: BLE001
            st.error(str(exc))

    if reset_clicked:
        candidates = st.session_state.get("last_auto_candidate_list")
        if candidates:
            st.session_state["candidate_list"] = candidates
            st.session_state["latest_candidate"] = candidates[0]
            _bump_candidate_editor_version()
            st.success("마지막 자동 생성 결과로 되돌렸습니다.")
            st.rerun()
        else:
            st.info("되돌릴 자동 생성 결과가 없습니다. 먼저 팀을 생성하세요.")

    st.info(
        "좌우 스왑 최적화는 현재 보드에서 정한 포지션 배치를 고정한 뒤, 각 라인별 BLUE↔RED 교환 32가지 경우만 비교합니다."
    )
    panel_end()

def page_team_builder(conn) -> None:
    players = list_players(conn)
    render_hero(
        "Team Builder",
        "팀 생성",
        "10명의 플레이어로 포지션별 실력치와 선호 포지션을 함께 고려한 5v5 팀을 생성합니다.",
        metric_cards([
            ("등록 인원", str(len(players)), "Player pool"),
            ("필요 인원", "10", "5v5"),
            ("업데이트 방식", "Role-specific", "라인별 레이팅"),
        ]),
    )

    if len(players) < 10:
        st.warning("팀 생성을 위해 최소 10명의 플레이어가 필요합니다.")
        return

    names = [p.name for p in players]
    default_names = names[:10]

    panel_start("팀 생성 설정", "참가자 10명과 선호 포지션 반영 강도를 선택하세요.", tag="Balance config", tight=True)
    selected_names = st.multiselect(
        "내전에 참가할 10명 선택",
        options=names,
        default=default_names,
        max_selections=10,
    )
    col_a, col_b = st.columns([0.58, 0.42])
    with col_a:
        preference_weight = st.slider(
            "선호 포지션 반영 강도",
            min_value=0.0,
            max_value=3.0,
            value=float(DEFAULT_CONFIG["preference_penalty_weight"]),
            step=0.1,
            help="높을수록 오프롤 배정을 더 강하게 피합니다.",
        )
    with col_b:
        st.write("")
        st.write("")
        generate_clicked = st.button("⚖️ 균형 잡힌 팀 생성", type="primary", use_container_width=True)
    top_k = 1  # 항상 1개의 최적 후보만 생성
    panel_end()

    selected = [p for p in players if p.name in selected_names]
    sort_key, sort_desc = render_player_pool_sort_controls()
    render_player_pool(
        player_pool_sorted(players, sort_key=sort_key, descending=sort_desc),
        selected_names=set(selected_names),
        sort_key=sort_key,
        sort_desc=sort_desc,
    )

    config = {"preference_penalty_weight": preference_weight}

    if generate_clicked:
        if len(selected) != 10:
            st.error("정확히 10명을 선택해야 합니다.")
            return
        candidates = generate_balanced_teams(
            selected,
            config=config,
            top_k=top_k,
        )
        st.session_state["latest_candidate"] = candidates[0]
        st.session_state["candidate_list"] = candidates
        st.session_state["last_auto_candidate_list"] = candidates
        _bump_candidate_editor_version()

    candidates = st.session_state.get("candidate_list")
    if candidates:
        if len(candidates) > 1:
            choice = st.selectbox(
                "팀 후보 선택",
                range(len(candidates)),
                format_func=lambda i: f"후보 {i+1} · gap={candidates[i].rating_gap:.2f}, penalty={candidates[i].preference_penalty:.1f}",
            )
            st.session_state["latest_candidate"] = candidates[choice]
            candidate = candidates[choice]
        else:
            candidate = candidates[0]
    else:
        candidate = current_candidate(conn)

    if candidate:
        render_team_cards(candidate)
        render_copy_export_panel(candidate)
        render_manual_adjustment_panel(candidate, selected if len(selected) == 10 else list(candidate.blue.slots.values()) + list(candidate.red.slots.values()), config=config)
        render_html(
            """
            <div class="info-strip">ⓘ 라인별 실력치는 솔로랭크 티어와 별개로 업데이트됩니다. 경기 결과 입력에서 캐리 선수와 라인 영향도를 반영하면 이후 팀 생성이 점점 더 현실적인 밸런스로 조정됩니다.</div>
            """
        )

def page_match_result(conn) -> None:
    candidate = current_candidate(conn)
    render_hero(
        "Match Result",
        "경기 결과 입력",
        "승패뿐 아니라 승리팀 캐리 선수, MVP, 라인별 영향도를 반영해서 포지션별 레이팅을 업데이트합니다.",
        "" if candidate is None else metric_cards([
            ("Blue 예상 승률", f"{candidate.expected_blue_win * 100:.1f}%", "Before match"),
            ("Blue 총합", f"{candidate.blue.total_rating:.1f}", "Team strength"),
            ("Red 총합", f"{candidate.red.total_rating:.1f}", "Team strength"),
        ]),
    )

    if candidate is None:
        st.warning("먼저 팀을 생성하세요.")
        return

    c1, c2, c3 = st.columns([1, 0.7, 1], gap="large")
    with c1:
        render_html(roster_compact_html(candidate.blue, "BLUE TEAM"))
    with c2:
        render_html(
            f"""
            <div class="balance-card" style="min-height:auto;">
                <div class="versus"><span class="vs-mark">VS</span></div>
                <div class="winrate-line"><span class="blue-text">BLUE {candidate.expected_blue_win*100:.1f}%</span><span class="red-text">RED {(1-candidate.expected_blue_win)*100:.1f}%</span></div>
                <div class="winrate-bar"><div class="win-blue" style="width:{candidate.expected_blue_win*100:.1f}%"></div><div class="win-red" style="width:{(1-candidate.expected_blue_win)*100:.1f}%"></div></div>
                <div class="summary-list">
                    <div class="summary-item"><span>실력치 차이</span><span>{candidate.rating_gap:.2f}</span></div>
                    <div class="summary-item"><span>선호 페널티</span><span>{candidate.preference_penalty:.1f}</span></div>
                </div>
            </div>
            """
        )
    with c3:
        render_html(roster_compact_html(candidate.red, "RED TEAM"))

    panel_start("결과 정보", "승리팀, 스코어, 메모를 입력합니다.", tag="Outcome")
    result_cols = st.columns([0.35, 0.65])
    with result_cols[0]:
        winner = st.radio("승리팀", options=["BLUE", "RED"], horizontal=True)
    with result_cols[1]:
        notes = st.text_input("메모", placeholder="예: 바텀 스노우볼")
    panel_end()
    blue_score = 1 if winner == "BLUE" else 0
    red_score = 0 if winner == "BLUE" else 1

    winning_assignment = candidate.blue if winner == "BLUE" else candidate.red
    blue_win = winner == "BLUE"

    panel_start(
        "게임 캐리 / 잘한 플레이어 선택",
        "승리팀에서 1~5명 선택하세요. 선택된 선수는 플레이한 포지션에 더 큰 긍정 업데이트를 받습니다.",
        tag=f"{winner} only",
    )
    render_html(mvp_preview_html(winning_assignment))
    carry_options = []
    label_to_id = {}
    for role in ROLES:
        p = winning_assignment.slots[role]
        label = f"{p.name} · {role}"
        carry_options.append(label)
        label_to_id[label] = int(p.id)

    selected_labels = st.multiselect("캐리/잘한 플레이어", carry_options, default=carry_options[:1])
    if len(selected_labels) > 5:
        st.warning("최대 5명까지만 반영합니다. 앞의 5명만 사용됩니다.")
        selected_labels = selected_labels[:5]
    carry_ids = [label_to_id[label] for label in selected_labels]

    mvp_label = None
    if selected_labels:
        mvp_label = st.selectbox("MVP 선택(선택 사항)", options=["없음", *selected_labels])
    mvp_id = None if mvp_label in (None, "없음") else label_to_id[mvp_label]
    panel_end()

    panel_start("라인별 영향력 평가", "승리팀 기준으로 각 라인이 얼마나 우세했는지 선택합니다.", tag="Lane impact")
    lane_impacts = {}
    lane_cols = st.columns(5)
    for idx, role in enumerate(ROLES):
        with lane_cols[idx]:
            lane_impacts[role] = st.selectbox(
                role,
                options=list(LANE_IMPACT_LABELS),
                index=2,
                help="승리팀 기준입니다. 예: BLUE 승리에서 TOP=열세면 Blue TOP은 라인에서 밀렸지만 게임은 이긴 것으로 처리됩니다.",
            )
    panel_end()

    changes = compute_rating_preview(
        candidate.blue,
        candidate.red,
        blue_win=blue_win,
        carry_player_ids=carry_ids,
        mvp_player_id=mvp_id,
        lane_impacts=lane_impacts,
    )
    panel_start("레이팅 업데이트 미리보기", "선택한 결과가 저장되기 전에 포지션별 변화량을 확인합니다.", tag="Preview")
    render_html(change_preview_html(changes))
    with st.expander("상세 업데이트 사유 보기"):
        st.dataframe(change_df(changes), use_container_width=True, hide_index=True)
    panel_end()

    if st.button("✅ 결과 저장 및 반영", type="primary", use_container_width=True):
        match_id, saved_changes = record_match_and_update(
            conn,
            blue=candidate.blue,
            red=candidate.red,
            blue_win=blue_win,
            blue_score=int(blue_score),
            red_score=int(red_score),
            carry_player_ids=carry_ids,
            mvp_player_id=mvp_id,
            lane_impacts=lane_impacts,
            notes=notes,
            append_csv_log=False,
        )
        csv_log_path = append_match_csv_log(
            conn,
            match_id=match_id,
            blue=candidate.blue,
            red=candidate.red,
            blue_win=blue_win,
            blue_score=int(blue_score),
            red_score=int(red_score),
            carry_player_ids=carry_ids,
            mvp_player_id=mvp_id,
            lane_impacts=lane_impacts,
            notes=notes,
        )
        st.success(f"경기 #{match_id} 저장 완료. CSV 저장 + append 로그 완료: {csv_log_path}")
        st.session_state.pop("latest_candidate", None)
        st.session_state.pop("candidate_list", None)
        st.dataframe(change_df(saved_changes), use_container_width=True, hide_index=True)


def page_players(conn) -> None:
    players = list_players(conn)
    render_hero(
        "Players",
        "플레이어 분석",
        "한 명의 포지션별 추정 실력, 신뢰도, 최근 내전 기록을 확인합니다.",
        metric_cards([
            ("등록 플레이어", str(len(players)), "Players"),
            ("역할", "5", "TOP/JG/MID/ADC/SUP"),
            ("기록 방식", "Role history", "포지션별 누적"),
        ]),
    )
    if not players:
        st.warning("등록된 플레이어가 없습니다.")
        return

    selected_name = st.selectbox("플레이어 선택", [p.name for p in players])
    player = next(p for p in players if p.name == selected_name)
    avg_conf = sum(player.confidence_for(role) for role in ROLES) / len(ROLES)
    main_role = player.preferred_roles[0] if player.preferred_roles else "-"
    sub_role = player.preferred_roles[1] if len(player.preferred_roles) > 1 else "-"

    render_html(
        f"""
        <div class="panel">
            <div class="profile-head">
                <div class="profile-avatar">{e(player.name[:1])}</div>
                <div>
                    <div class="profile-name">{e(player.name)}</div>
                    <div class="profile-meta">{e(player.riot_id)} · 선호 {e(', '.join(player.preferred_roles) if player.preferred_roles else '상관없음')}</div>
                </div>
                <div class="metric-card"><div class="metric-label">솔로랭크</div><div class="metric-value">{e((player.solo_tier + ' ' + player.solo_rank).strip())}</div><div class="metric-hint">{player.league_points} LP</div></div>
                <div class="metric-card"><div class="metric-label">신뢰도</div><div class="metric-value green-text">{avg_conf*100:.0f}%</div><div class="metric-hint">평균 confidence</div></div>
            </div>
        </div>
        """
    )

    left, right = st.columns([0.5, 0.5], gap="large")
    with left:
        panel_start("라인별 실력 추정", "0~100 내부 스케일과 추정 티어입니다.")
        render_html(role_bars_html(player))
        panel_end()
    with right:
        role_rows = []
        for role in ROLES:
            rating = player.rating_for(role)
            role_rows.append(
                {
                    "포지션": role,
                    "추정 실력치": rating,
                    "추정 티어": rating_to_tier_label(rating),
                    "게임 수": player.games_for(role),
                    "신뢰도": player.confidence_for(role),
                }
            )
        role_df = pd.DataFrame(role_rows)
        panel_start("포지션별 게임 수 & 신뢰도", f"주 포지션 {main_role} · 부 포지션 {sub_role}")
        st.dataframe(role_df, use_container_width=True, hide_index=True)
        panel_end()

    panel_start("최근 내전 기록", "선택받은 캐리/MVP 여부와 변화량을 함께 표시합니다.")
    history = list_player_match_history(conn, int(player.id), limit=30)
    if history:
        rows = []
        for h in history:
            carry_ids = set(json.loads(h["carry_player_ids_json"] or "[]"))
            win = (h["team"] == "BLUE" and h["blue_win"]) or (h["team"] == "RED" and not h["blue_win"])
            rows.append(
                {
                    "경기": h["match_id"],
                    "일시": h["played_at"],
                    "팀": h["team"],
                    "포지션": h["role"],
                    "결과": "승리" if win else "패배",
                    "캐리 선정": "MVP" if h["mvp_player_id"] == player.id else ("캐리" if player.id in carry_ids else "-"),
                    "변화량": h["delta"],
                    "사유": h["reason"],
                }
            )
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    else:
        st.info("아직 이 플레이어의 내전 기록이 없습니다.")
    panel_end()

    panel_start("Rating Update Logic", "승패, 캐리 선정, MVP, 라인 영향도를 모두 반영합니다.")
    render_html(
        """
        <div class="summary-list">
            <div class="summary-item"><span>기본 승패 보정</span><span>예상 승률 대비 결과</span></div>
            <div class="summary-item"><span>캐리 선정</span><span class="green-text">해당 포지션 추가 상승</span></div>
            <div class="summary-item"><span>MVP</span><span class="gold-text">추가 보너스</span></div>
            <div class="summary-item"><span>라인 열세 후 승리</span><span>과대 상승 방지</span></div>
            <div class="summary-item"><span>게임 수 증가</span><span>신뢰도 상승, 변동폭 완화</span></div>
        </div>
        """
    )
    panel_end()


def page_history(conn) -> None:
    render_hero("History", "경기 히스토리", "저장된 모든 경기와 참가자별 업데이트 내역을 확인합니다.")
    matches = list_matches(conn, limit=100)
    if not matches:
        st.info("아직 저장된 경기가 없습니다.")
        return

    panel_start("경기 목록", "최근 100개 경기")
    rows = []
    for m in matches:
        winner = "BLUE" if m["blue_win"] else "RED"
        blue_expected_win = round(m["expected_blue_win"] * 100, 1)
        rows.append(
            {
                "id": m["id"],
                "일시": m["played_at"],
                "승리팀": winner,
                "BLUE 예상 승률": blue_expected_win,
                "메모": m["notes"],
            }
        )
    
    # 승리팀 색상화를 위한 HTML 테이블 렌더링
    table_rows = []
    for row in rows:
        winner_color = "var(--blue)" if row["승리팀"] == "BLUE" else "var(--red)"
        winrate = float(row["BLUE 예상 승률"])
        winrate_color = "var(--blue)" if winrate >= 50.0 else "var(--red)"

        table_rows.append(
            f"""
            <tr>
                <td>{row['id']}</td>
                <td>{row['일시']}</td>
                <td style="color: {winner_color}; font-weight: 900;">{row['승리팀']}</td>
                <td style="color: {winrate_color}; font-weight: 400;">{winrate:.1f}%</td>
                <td>{row['메모']}</td>
            </tr>
            """
        )
    
    render_html(
        f"""
        <div class="panel">
            <table class="player-table">
                <thead>
                    <tr>
                        <th style="width: 60px;">경기 ID</th>
                        <th>일시</th>
                        <th>승리팀</th>
                        <th>BLUE 예상승률</th>
                        <th>메모</th>
                    </tr>
                </thead>
                <tbody>
                    {''.join(table_rows)}
                </tbody>
            </table>
        </div>
        """
    )
    panel_end()

    panel_start("상세 경기", "선택한 경기의 참가자별 변화량")
    match_id = st.selectbox("상세 경기", [m["id"] for m in matches])
    participants = list_match_participants(conn, int(match_id))
    st.dataframe(pd.DataFrame(participants), use_container_width=True, hide_index=True)
    panel_end()


def _save_uploaded_csv(uploaded_file, target_dir: Path, filename: str) -> Path:
    target_dir.mkdir(parents=True, exist_ok=True)
    suffix = Path(getattr(uploaded_file, "name", "")).suffix.lower() or Path(filename).suffix or ".csv"
    stem = Path(filename).stem
    path = target_dir / f"{stem}{suffix}"
    path.write_bytes(uploaded_file.getvalue())
    return path


def _report_to_frame(report) -> pd.DataFrame:
    rows = [{"구분": "가져오기 성공", "개수": report.imported}, {"구분": "스킵/오류", "개수": report.skipped}]
    return pd.DataFrame(rows)


def _csv_download_bytes(rows: list[dict], columns: list[str]) -> bytes:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({column: row.get(column, "") for column in columns})
    return buffer.getvalue().encode("utf-8-sig")


def page_import_sync(conn) -> None:
    render_hero(
        "Import / Riot Sync",
        "데이터 가져오기 & Riot 갱신",
        "기존 내전 기록 CSV를 리플레이하고, Riot API로 솔로랭크와 최근 포지션 prior를 갱신합니다.",
        metric_cards([
            ("가져오기", "CSV", "Players / Matches"),
            ("Riot API", "Rank", "Solo queue prior"),
            ("업데이트", "Replay", "시간순 경기 반영"),
        ]),
    )

    panel_start("권장 순서", "처음 실제 데이터를 넣을 때는 아래 순서를 추천합니다.", tag="Workflow")
    st.markdown(
        """
        1. 기존 DB 백업: `copy data\\inhouse_balancer.sqlite data\\inhouse_balancer.backup.sqlite`  
        2. 플레이어 CSV/XLSX 가져오기  
        3. Riot API로 솔로랭크/최근 포지션 갱신  
        4. 과거 내전 CSV/XLSX를 시간순으로 리플레이  
        5. Players / History / Team Builder에서 결과 확인  
        6. 이후 앱에서 저장한 새 경기는 SQLite와 `data/records/match_results.csv`에 함께 기록
        """
    )
    panel_end()

    panel_start(
        "초기 세팅: 실제 players + 과거 matches로 DB 재구성",
        "기존 데이터를 지우고, 업로드한 players만 등록한 뒤 과거 경기를 시간순으로 리플레이합니다.",
        tag="Initial bootstrap",
    )
    st.warning("이 버튼은 현재 DB의 플레이어/경기/레이팅을 초기화할 수 있습니다. 실행 전에 data\\inhouse_balancer.sqlite 백업을 권장합니다.")
    b_col1, b_col2 = st.columns(2, gap="large")
    with b_col1:
        bootstrap_players_file = st.file_uploader(
            "초기 세팅용 players CSV/XLSX",
            type=["csv", "xlsx", "xls"],
            key="bootstrap_players_csv",
        )
    with b_col2:
        bootstrap_matches_file = st.file_uploader(
            "초기 세팅용 matches CSV/XLSX",
            type=["csv", "xlsx", "xls"],
            key="bootstrap_matches_csv",
        )

    opt_col1, opt_col2, opt_col3 = st.columns([0.34, 0.46, 0.20])
    with opt_col1:
        bootstrap_reset_all = st.checkbox(
            "기존 DB 완전 초기화",
            value=True,
            help="체크하면 기존 플레이어/레이팅/경기 기록을 모두 지운 뒤 다시 구성합니다.",
        )
    with opt_col2:
        bootstrap_infer_prefs = st.checkbox(
            "비어있는 선호 포지션을 matches 기록에서 추정",
            value=True,
            help="players.csv의 preferred_roles가 비어 있으면 과거 경기에서 많이 플레이한 라인을 주/부 포지션으로 사용합니다.",
        )
    with opt_col3:
        inferred_slots = st.number_input("추정 포지션 수", min_value=1, max_value=3, value=2, step=1)

    bootstrap_clicked = st.button(
        "DB 초기화 + players 등록 + matches 리플레이",
        type="primary",
        use_container_width=True,
        disabled=bootstrap_players_file is None,
    )
    if bootstrap_clicked:
        players_path = _save_uploaded_csv(bootstrap_players_file, Path("data/import_uploads"), "bootstrap_players.csv")
        matches_path = None
        if bootstrap_matches_file is not None:
            matches_path = _save_uploaded_csv(bootstrap_matches_file, Path("data/import_uploads"), "bootstrap_matches.csv")
        result = bootstrap_from_records(
            conn,
            players_path,
            matches_path,
            reset_all=bool(bootstrap_reset_all),
            infer_missing_preferences=bool(bootstrap_infer_prefs),
            inferred_preference_slots=int(inferred_slots),
        )
        players_report = result["players"]
        matches_report = result.get("matches")
        st.success(
            f"초기 세팅 완료: 플레이어 {players_report.imported}명, "
            f"경기 {matches_report.imported if matches_report else 0}개 리플레이"
        )
        summary_rows = [
            {"구분": "플레이어 가져오기", "성공": players_report.imported, "스킵/오류": players_report.skipped},
        ]
        if matches_report is not None:
            summary_rows.append({"구분": "과거 경기 리플레이", "성공": matches_report.imported, "스킵/오류": matches_report.skipped})
        st.dataframe(pd.DataFrame(summary_rows), use_container_width=True, hide_index=True)
        inferred = result.get("inferred_preferences") or {}
        if inferred:
            with st.expander(f"matches에서 추정해 채운 선호 포지션 {len(inferred)}명"):
                st.dataframe(
                    pd.DataFrame([
                        {"플레이어": name, "추정 선호 포지션": " | ".join(roles)}
                        for name, roles in inferred.items()
                    ]),
                    use_container_width=True,
                    hide_index=True,
                )
        all_errors = []
        if players_report.errors:
            all_errors.extend(players_report.errors)
        if matches_report is not None and matches_report.errors:
            all_errors.extend(matches_report.errors)
        if all_errors:
            with st.expander("오류/스킵 행 보기"):
                st.write(all_errors)
        st.session_state.pop("latest_candidate", None)
        st.session_state.pop("candidate_list", None)
    panel_end()

    left, right = st.columns([0.5, 0.5], gap="large")

    with left:
        panel_start("플레이어 CSV 가져오기", "name, Riot ID, 솔로랭크, 선호 포지션, 선택적 role rating을 가져옵니다.", tag="Players CSV")
        players_file = st.file_uploader("players CSV/XLSX 업로드", type=["csv", "xlsx", "xls"], key="players_csv")
        if st.button("플레이어 CSV 반영", use_container_width=True, disabled=players_file is None):
            path = _save_uploaded_csv(players_file, Path("data/import_uploads"), "players_upload.csv")
            report = import_players_csv(conn, path)
            st.success(f"플레이어 {report.imported}명 가져오기/업데이트 완료")
            st.dataframe(_report_to_frame(report), use_container_width=True, hide_index=True)
            if report.errors:
                with st.expander("오류/스킵 행 보기"):
                    st.write(report.errors)
        panel_end()

    with right:
        panel_start(
            "Riot API 초기화",
            "Riot ID 기준으로 솔랭/자유랭크, 최근 포지션, 라인별 Top 3 챔피언을 갱신합니다.",
            tag="Riot",
        )
        api_key = env_value("RIOT_API_KEY", "").strip()
        st.caption("API 키는 `.env`의 RIOT_API_KEY만 사용합니다. 입력창으로 받지 않습니다.")
        infer_roles_count = st.slider("최근 매치 기반 선호 포지션 추정 개수", min_value=0, max_value=80, value=30, step=5)
        champion_pool_count = st.slider("라인별 Top 3 챔피언 계산용 최근 매치 수", min_value=0, max_value=100, value=40, step=5)
        solo_weight = st.slider("초기 점수에서 솔랭 반영 비중", min_value=0.0, max_value=1.0, value=0.75, step=0.05)
        reset_role_ratings = st.checkbox(
            "역할별 레이팅을 Riot prior로 재초기화",
            value=True,
            help="초기 세팅 시에는 켜는 것을 추천합니다. 이미 내전 기록을 충분히 쌓은 뒤에는 끄는 것이 좋습니다.",
        )
        if st.button("Riot API로 초기 점수/챔피언 갱신", use_container_width=True):
            client = RiotClient(api_key=api_key or None)
            if not client.enabled:
                st.error(".env 파일에 RIOT_API_KEY가 필요합니다.")
            else:
                with st.spinner("Riot API 갱신 중입니다. 플레이어 수와 최근 매치 개수에 따라 시간이 걸릴 수 있습니다."):
                    results = refresh_all_players_from_riot(
                        conn,
                        client,
                        reset_role_ratings=reset_role_ratings,
                        infer_roles_count=int(infer_roles_count),
                        champion_pool_count=int(champion_pool_count),
                        solo_weight=float(solo_weight),
                    )
                df = pd.DataFrame([
                    {
                        "플레이어": r.player_name,
                        "상태": r.status,
                        "메시지": r.message,
                        "솔랭": (f"{r.solo_tier or ''} {r.solo_rank or ''}".strip()),
                        "솔랭 LP": r.league_points,
                        "자유랭크": (f"{r.flex_tier or ''} {r.flex_rank or ''}".strip()),
                        "자유랭크 LP": r.flex_league_points,
                        "추정 포지션": ", ".join(r.inferred_roles or []),
                    }
                    for r in results
                ])
                st.dataframe(df, use_container_width=True, hide_index=True)
                st.session_state.pop("latest_candidate", None)
                st.session_state.pop("candidate_list", None)
        panel_end()

    panel_start("과거 내전 CSV 리플레이", "승패, 팀 배정, 캐리/MVP, 라인 영향도를 시간순으로 반영합니다.", tag="Matches CSV")
    st.caption("핵심 컬럼: blue_top~blue_sup, red_top~red_sup, blue_win, carry_players, mvp_player, lane_top~lane_sup")
    matches_file = st.file_uploader("matches CSV/XLSX 업로드", type=["csv", "xlsx", "xls"], key="matches_csv")
    col_a, col_b = st.columns([0.72, 0.28])
    with col_a:
        st.info("과거 경기는 `played_at` 오름차순으로 리플레이됩니다. 플레이어 이름은 DB의 name, Riot ID, display_name 중 하나와 맞으면 됩니다.")
    with col_b:
        import_matches_clicked = st.button("과거 경기 리플레이", type="primary", use_container_width=True, disabled=matches_file is None)
    if import_matches_clicked:
        path = _save_uploaded_csv(matches_file, Path("data/import_uploads"), "matches_upload.csv")
        report = import_matches_csv(conn, path)
        st.success(f"경기 {report.imported}개 리플레이 완료")
        st.dataframe(_report_to_frame(report), use_container_width=True, hide_index=True)
        if report.errors:
            with st.expander("오류/스킵 행 보기"):
                st.write(report.errors)
    panel_end()

    panel_start("저장 구조", "새 경기 결과가 어디에 저장되는지 확인합니다.", tag="Storage")
    st.markdown(
        f"""
        - 기본 저장소: `data/inhouse_balancer.sqlite`  
        - 새 경기 CSV append 로그: `{DEFAULT_MATCH_LOG_PATH}`  
        - 과거 matches import/replay는 기존 기록을 학습용으로 반영하지만, append 로그에 중복 기록하지 않습니다.
        """
    )
    player_rows = export_players_rows(conn)
    match_rows = export_matches_rows(conn)
    dl_cols = st.columns(3)
    with dl_cols[0]:
        st.download_button(
            "players.csv 다운로드",
            data=_csv_download_bytes(
                player_rows,
                [
                    "name", "display_name", "riot_game_name", "riot_tag_line",
                    "solo_tier", "solo_rank", "league_points",
                    "flex_tier", "flex_rank", "flex_league_points",
                    "preferred_roles", "top_champions_json", *ROLES,
                ],
            ),
            file_name="players.csv",
            mime="text/csv",
            use_container_width=True,
        )
    with dl_cols[1]:
        st.download_button(
            "matches.csv 다운로드",
            data=_csv_download_bytes(
                match_rows,
                [
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
                ],
            ),
            file_name="matches.csv",
            mime="text/csv",
            use_container_width=True,
        )
    with dl_cols[2]:
        participant_path = getattr(conn, "participants_path", None)
        if participant_path and Path(participant_path).exists():
            st.download_button(
                "match_participants.csv 다운로드",
                data=Path(participant_path).read_bytes(),
                file_name="match_participants.csv",
                mime="text/csv",
                use_container_width=True,
            )
    if Path(DEFAULT_MATCH_LOG_PATH).exists():
        st.download_button(
            "match_results.csv 다운로드",
            data=Path(DEFAULT_MATCH_LOG_PATH).read_bytes(),
            file_name="match_results.csv",
            mime="text/csv",
            use_container_width=True,
        )
    else:
        st.info("아직 앱에서 새로 저장한 경기 로그 CSV가 없습니다. Match Result에서 결과를 저장하면 생성됩니다.")
    panel_end()

    panel_start("CSV 컬럼 요약", "기존 기록을 아래 형태로 맞추면 가장 안정적으로 가져올 수 있습니다.")
    st.code(
        """players.csv:
name,display_name,riot_game_name,riot_tag_line,solo_tier,solo_rank,league_points,preferred_roles,TOP,JG,MID,ADC,SUP

matches.csv:
played_at,blue_win,blue_score,red_score,
blue_top,blue_jg,blue_mid,blue_adc,blue_sup,
red_top,red_jg,red_mid,red_adc,red_sup,
carry_players,mvp_player,lane_top,lane_jg,lane_mid,lane_adc,lane_sup,notes""",
        language="text",
    )
    panel_end()


def page_settings(conn) -> None:
    render_hero("Settings", "설정", "업데이트 파라미터, 실제 데이터 초기화, 수동 플레이어 추가를 관리합니다.")
    panel_start("현재 업데이트 파라미터", "팀 생성과 레이팅 업데이트에 사용되는 값입니다.")
    st.json(DEFAULT_CONFIG)
    panel_end()

    panel_start("데이터 초기화", "실제 데이터로 새로 시작할 때 사용합니다.")
    st.caption("데모 플레이어는 더 이상 자동 생성되지 않습니다. 실제 초기 세팅은 Import / Riot Sync의 초기 세팅 패널을 사용하세요.")
    if st.button("모든 데이터 비우기", use_container_width=True):
        delete_all_data(conn)
        st.session_state.pop("latest_candidate", None)
        st.session_state.pop("candidate_list", None)
        st.success("모든 플레이어/경기/레이팅 데이터를 삭제했습니다.")
    panel_end()

    panel_start("플레이어 추가", "수동으로 플레이어를 등록하거나 업데이트합니다. name은 고유 식별자, display_name은 복붙용 이름입니다.")
    with st.form("add-player"):
        display_name = st.text_input("표시 이름", placeholder="예: 철수")
        riot_id = st.text_input("Riot ID / 고유 name", placeholder="예: Hide on bush#KR1")
        tier = st.selectbox("솔로랭크 티어", ["UNRANKED", "IRON", "BRONZE", "SILVER", "GOLD", "PLATINUM", "EMERALD", "DIAMOND", "MASTER", "GRANDMASTER", "CHALLENGER"], index=0)
        rank = st.selectbox("랭크", ["", "IV", "III", "II", "I"], index=0)
        lp = st.number_input("LP", min_value=0, max_value=2000, value=0)
        pref = st.multiselect("선호 포지션", ROLES, default=[])
        submitted = st.form_submit_button("추가/업데이트")
        if submitted:
            canonical_name = riot_id.strip() or display_name.strip()
            if not canonical_name:
                st.error("표시 이름 또는 Riot ID를 입력하세요.")
            else:
                riot_game_name, riot_tag_line = None, None
                if "#" in canonical_name:
                    riot_game_name, riot_tag_line = [x.strip() for x in canonical_name.rsplit("#", 1)]
                create_player(
                    conn,
                    name=canonical_name,
                    display_name=display_name.strip() or canonical_name,
                    preferred_roles=pref,
                    solo_tier=tier,
                    solo_rank=rank,
                    league_points=int(lp),
                    riot_game_name=riot_game_name,
                    riot_tag_line=riot_tag_line,
                )
                st.success(f"{display_name.strip() or canonical_name} 저장 완료")
    panel_end()

def main() -> None:
    st.set_page_config(page_title="Inhouse Balancer", page_icon="⚖️", layout="wide")
    inject_css()
    conn = get_conn()

    render_sidebar_brand()
    page = st.sidebar.radio(
        "Menu",
        ["Dashboard", "Team Builder", "Match Result", "Players", "History", "Import / Riot Sync", "Settings"],
        index=1,
    )
    st.sidebar.markdown("---")
    render_html(
        """
        <div class="panel tight">
            <div class="panel-title">현재 룰셋</div>
            <div class="panel-subtitle">기본 룰셋 · 캐리/MVP 보정 활성화</div>
        </div>
        """,
        container=st.sidebar,
    )

    if page == "Dashboard":
        page_dashboard(conn)
    elif page == "Team Builder":
        page_team_builder(conn)
    elif page == "Match Result":
        page_match_result(conn)
    elif page == "Players":
        page_players(conn)
    elif page == "History":
        page_history(conn)
    elif page == "Import / Riot Sync":
        page_import_sync(conn)
    else:
        page_settings(conn)


if __name__ == "__main__":
    main()
