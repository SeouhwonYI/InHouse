# PostgreSQL / 배포 모드

v9부터 앱은 `DATABASE_URL`이 있으면 PostgreSQL을 사용하고, 없으면 기존처럼 로컬 SQLite(`data/inhouse_balancer.sqlite`)를 사용합니다.

## 1. 로컬 PostgreSQL 또는 호스팅 DB 준비

환경변수 또는 `.env`에 다음 값을 넣습니다.

```env
DATABASE_URL=postgresql://USER:PASSWORD@HOST:5432/DBNAME
RIOT_API_KEY=RGAPI-...
```

Streamlit Cloud, Render, Railway 등에서는 이 값을 플랫폼의 Secret / Environment Variables에 넣으면 됩니다.

## 2. 설치

```powershell
pip install -r requirements.txt
```

`requirements.txt`에는 PostgreSQL 접속용 `psycopg[binary]`와 드래그 보드용 `streamlit-sortables`가 포함되어 있습니다.

## 3. 실행

```powershell
streamlit run streamlit_app.py
```

`DATABASE_URL`이 설정되어 있으면 앱이 자동으로 PostgreSQL 테이블을 생성합니다.

## 4. 기존 SQLite DB를 PostgreSQL로 옮기기

기존 로컬 DB를 그대로 PostgreSQL에 옮기려면:

```powershell
python tools\migrate_sqlite_to_postgres.py data\inhouse_balancer.sqlite --reset
```

`--reset`은 PostgreSQL 쪽 기존 players/matches/ratings를 지우고 복사합니다. 운영 중인 DB에 사용할 때는 신중하게 사용하세요.

## 5. 운영 데이터 원칙

- PostgreSQL이 source of truth입니다.
- `players.csv`, `matches.csv`는 최초 import와 백업/복구용입니다.
- 새 경기 결과는 DB에 저장됩니다.
- CSV 로그는 보조 archive/export 용도입니다.

## 6. 드래그 수동 조정

Team Builder의 수동 세부 조정 패널은 `streamlit-sortables`가 설치되어 있으면 드래그 보드로 표시됩니다.

- BLUE/RED 컬럼 사이로 플레이어를 드래그하면 팀이 바뀝니다.
- 같은 컬럼 안에서 위아래로 드래그하면 포지션이 바뀝니다.
- 각 컬럼의 순서는 TOP / JG / MID / ADC / SUP입니다.
- 각 팀은 최종적으로 5명이어야 적용됩니다.
- 이후 `좌우 스왑만 최적화`를 누르면 현재 포지션을 유지한 채 각 라인의 BLUE↔RED 스왑 32가지만 비교합니다.
