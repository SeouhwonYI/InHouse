# 실제 데이터 초기화 + Riot API 갱신 가이드

이 앱은 SQLite DB를 메인 저장소로 사용합니다. 기존 내전 기록은 `players.csv/xlsx`와 `matches.csv/xlsx`로 가져와서 시간순으로 replay하고, 이후 앱에서 저장하는 새 경기만 `data/records/match_results.csv`에 append 로그로 남깁니다.

## 권장 순서

1. 기존 DB 백업
2. `players.csv/xlsx` 가져오기
3. 선택적으로 Riot API로 솔로랭크/최근 포지션 prior 갱신
4. `matches.csv/xlsx`를 시간순 replay
5. Team Builder / Players / History 확인

## DB 백업

```powershell
copy data\inhouse_balancer.sqlite data\inhouse_balancer.backup.sqlite
```

## players 파일

필수 컬럼:

```text
name
```

권장 컬럼:

```text
name,display_name,riot_game_name,riot_tag_line,solo_tier,solo_rank,league_points,preferred_roles,TOP,JG,MID,ADC,SUP
```

- `name`: 고유 식별자. Riot ID 전체 문자열을 추천합니다. 예: `그냥롤#KR1`
- `display_name`: 팀 복붙에 쓸 짧은 이름입니다. 예: `철수`
- `riot_game_name`: Riot ID의 `#` 앞부분
- `riot_tag_line`: Riot ID의 `#` 뒷부분
- `preferred_roles`: `TOP|JG`처럼 입력합니다.
- `TOP/JG/MID/ADC/SUP`: 직접 알고 있는 초기 점수가 있으면 입력합니다. 비워두면 solo tier prior를 사용합니다.

CLI import:

```powershell
.\.venv\Scripts\python.exe tools\import_players_csv.py players.csv
```

전체 초기화 후 가져오기:

```powershell
.\.venv\Scripts\python.exe tools\import_players_csv.py players.csv --reset-all
```

## Riot API 갱신

`.env` 파일에 API key를 넣을 수 있습니다.

```text
RIOT_API_KEY=RGAPI-...
```

전체 플레이어의 랭크 갱신:

```powershell
.\.venv\Scripts\python.exe tools\refresh_riot_players.py
```

최근 10게임 기준 선호 포지션 추정:

```powershell
.\.venv\Scripts\python.exe tools\refresh_riot_players.py --infer-roles 10
```

역할별 레이팅을 Riot prior로 재초기화:

```powershell
.\.venv\Scripts\python.exe tools\refresh_riot_players.py --reset-role-ratings --infer-roles 10
```

이미 과거 내전 기록을 replay한 뒤에는 `--reset-role-ratings`를 쓰지 않는 편이 좋습니다.

## matches 파일

필수 컬럼:

```text
blue_win,
blue_top,blue_jg,blue_mid,blue_adc,blue_sup,
red_top,red_jg,red_mid,red_adc,red_sup
```

권장 컬럼:

```text
played_at,blue_score,red_score,
carry_players,mvp_player,
lane_top,lane_jg,lane_mid,lane_adc,lane_sup,
notes
```

- `blue_win`: `BLUE` 또는 `RED`
- 슬롯 컬럼은 `players.name`, Riot ID, 또는 `display_name`과 매칭됩니다.
- `carry_players`: `이름1|이름2|이름3`처럼 `|`로 구분합니다.
- `lane_*`: 승리팀 기준 `압승 / 우세 / 비등 / 열세`입니다.

CLI replay:

```powershell
.\.venv\Scripts\python.exe tools\import_matches_csv.py matches.csv
```

과거 경기는 `played_at` 기준 오름차순으로 replay됩니다.

## 실제 데이터로 한 번에 초기화

UI에서는 `Import / Riot Sync` → `초기 세팅: 실제 players + 과거 matches로 DB 재구성`을 사용하면 됩니다.

CLI로는:

```powershell
.\.venv\Scripts\python.exe tools\bootstrap_initial_data.py players.csv matches.csv
```

이 명령은 기본적으로 기존 DB를 비우고, players를 등록한 뒤, matches를 오래된 경기부터 순서대로 replay합니다.

권장 흐름:

```text
players import → Riot API prior 갱신 → matches replay
```

## 저장 구조

```text
data/inhouse_balancer.sqlite          # 메인 DB
data/records/match_results.csv       # 앱에서 새로 저장한 경기 append 로그
data/import_uploads/                 # 업로드 파일 임시 저장
```

과거 matches import/replay는 CSV 로그에 append하지 않습니다. 새로 실제 내전을 진행하고 `Match Result`에서 결과를 저장한 경기만 `data/records/match_results.csv`에 append됩니다.

## 한국어 Excel 인코딩

한국어 Windows Excel의 일반 CSV는 CP949/EUC-KR일 수 있습니다. importer는 UTF-8, UTF-8-BOM, CP949, EUC-KR을 자동 처리하고, `.xlsx`도 바로 업로드할 수 있습니다.
