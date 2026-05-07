# Inhouse Balancer

League of Legends 5v5 내전 팀 밸런싱 도구입니다. 데모 데이터는 자동 생성되지 않으며, 사용자가 올린 `players.csv/xlsx`와 `matches.csv/xlsx`를 기준으로 초기 레이팅을 만들고 이후 경기 결과를 누적합니다.

## 주요 기능

- 10명 기반 5v5 자동 팀 생성
- 플레이어별 `TOP / JG / MID / ADC / SUP` 라인별 추정 실력치 관리
- 선호 포지션과 오프롤 페널티 반영
- 과거 내전 기록을 시간순으로 replay하여 초기 라인별 점수 튜닝
- Riot API를 통한 솔로랭크 prior 갱신
- 경기 결과 저장 시 SQLite 반영 + CSV append 로그 생성
- 팀 생성 결과 복붙: 2열 표시이름, 2열 Riot ID, 팀별 목록
- 수동 세부 조정: 표에서 슬롯 수정 후 적용
- 라인별 Blue↔Red 좌우 스왑만 고려하는 추가 최적화

## 설치 및 실행

PowerShell 기준:

```powershell
python -m venv .venv
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope Process
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
streamlit run streamlit_app.py
```

가상환경 활성화가 불편하면:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m streamlit run streamlit_app.py
```

## 권장 데이터 흐름

1. `Import / Riot Sync`에서 실제 데이터 초기화 패널을 사용합니다.
2. `players.csv/xlsx`를 업로드합니다.
3. 선택적으로 Riot API prior를 갱신합니다.
4. `matches.csv/xlsx`를 업로드하여 과거 경기를 replay합니다.
5. `Team Builder`에서 팀을 생성합니다.
6. 필요한 경우 `수동 세부 조정`에서 포지션/팀을 조정합니다.
7. `좌우 스왑만 최적화`로 라인별 좌우 교환만 비교해 밸런스를 다시 맞춥니다.
8. `Match Result`에서 결과를 저장합니다.

## players 파일 컬럼

필수:

```text
name
```

권장:

```text
name,display_name,riot_game_name,riot_tag_line,solo_tier,solo_rank,league_points,preferred_roles,TOP,JG,MID,ADC,SUP
```

- `name`: 고유 식별자. Riot ID 전체 문자열을 추천합니다. 예: `Hide on bush#KR1`
- `display_name`: 복붙용 표시 이름. 예: `철수`
- `riot_game_name`: `#` 앞부분
- `riot_tag_line`: `#` 뒷부분
- `preferred_roles`: `TOP|JG`처럼 `|`로 구분
- `TOP/JG/MID/ADC/SUP`: 선택 입력. 비우면 솔로랭크 prior로 초기화됩니다.

## matches 파일 컬럼

```text
played_at,blue_win,blue_score,red_score,
blue_top,blue_jg,blue_mid,blue_adc,blue_sup,
red_top,red_jg,red_mid,red_adc,red_sup,
carry_players,mvp_player,lane_top,lane_jg,lane_mid,lane_adc,lane_sup,notes
```

- `blue_win`: `BLUE` 또는 `RED`
- 각 슬롯 컬럼: `players.name`, Riot ID, 또는 `display_name` 중 하나와 매칭 가능
- `carry_players`: `A|B|C`처럼 최대 5명
- `mvp_player`: 한 명
- `lane_*`: 승리팀 기준 `압승 / 우세 / 비등 / 열세`

## 저장 구조

```text
data/inhouse_balancer.sqlite          # 메인 DB, source of truth
data/records/match_results.csv       # 앱에서 새로 저장한 경기 append 로그
data/import_uploads/                 # 업로드 파일 임시 보관
```

과거 `matches.csv`를 import/replay할 때는 중복을 피하기 위해 append 로그에 쓰지 않습니다. 앱에서 새로 `Match Result`를 저장한 경기만 `data/records/match_results.csv`에 append됩니다.

## 폴더 구조

```text
inhouse-balancer-mvp/
├── streamlit_app.py
├── requirements.txt
├── .env.example
├── README.md
├── README_IMPORT_AND_RIOT.md
├── .streamlit/
│   └── config.toml
├── inhouse_balancer/
│   ├── balancer.py
│   ├── constants.py
│   ├── exports.py
│   ├── importers.py
│   ├── models.py
│   ├── rating.py
│   ├── riot_client.py
│   ├── riot_sync.py
│   └── storage.py
├── tools/
│   ├── bootstrap_initial_data.py
│   ├── import_matches_csv.py
│   ├── import_players_csv.py
│   └── refresh_riot_players.py
├── tests/
│   └── test_core.py
└── data/
    ├── .gitkeep
    └── records/
        └── .gitkeep
```

`templates/`, `seed_data.py`, `__pycache__/`, `.pytest_cache/`는 정리했습니다.

## 테스트

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

현재 핵심 테스트는 팀 생성, 캐리 보정, 라인 영향도 보정, CP949 CSV 읽기, CSV append 로그, 좌우 스왑 최적화를 확인합니다.


### v11: 플레이어 풀 정렬

Team Builder의 플레이어 풀 위에 `TOP / JG / MID / ADC / SUP / 솔로랭크 / 이름` 정렬 버튼이 추가되었습니다.
같은 버튼을 한 번 더 누르면 내림차순/오름차순이 전환됩니다. 기본적으로 라인 버튼은 해당 라인 점수가 높은 플레이어부터 표시합니다.

