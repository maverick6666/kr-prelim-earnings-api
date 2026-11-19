# 잠정실적 속보 API (작업 중)

DART 잠정실적 공시를 받아 **매출 · 영업이익 · 순이익, 전년 · 전분기 대비 증감, 시장 예상치 대비 깜짝 여부, 원문 근거**를 구조화된 데이터로 내주는 프로젝트입니다.

> 지금은 **2단계(버전 · 공개 시각 · 깜짝 판정 · API)** 까지 끝난 상태입니다. 데모 화면과 공개는 3단계에서 합니다.

## 지금까지 결과

**1단계 — 파싱과 정확도** ([보고서](reports/phase1_accuracy.md))

2008~2026년 잠정실적 공시 32,581건 중 **32,569건(99.96%)** 을 읽었고, 정답 없이 쓸 수 있는 세 가지 방법으로 정확도를 쟀습니다.

| 방법 | 결과 |
|---|---|
| 공시에 함께 적힌 증감률과 다시 계산한 값 대조 | 99.5% 일치 (228,991행) |
| '전년동기' 값 ↔ 정기보고서 확정 재무(단위까지 검증) | 97.3% 가 0.5% 안에서 같음 (118,048행) |
| AI 대리 정답(사람 검수 아님) 457쌍 | 당기 매출 100%, 비교값까지 99.3% 일치 |

검증이 틀리는 곳의 상당수는 원문 쪽 오기였고, 414행은 고친 값까지 자동으로 찾았습니다(예: `17.227` → 17,227).

**2단계 — 버전 · 공개 시각 · 깜짝 판정 · API** ([보고서](reports/phase2_store_api.md))

- 같은 실적을 다룬 공시(원 공시 · 같은 분기 재공시 · 정정공시)를 한 묶음으로 잇고, 이전 버전을 덮어쓰지 않습니다.
- 97.5% 에 KIND 기준 **분 단위 최초 공개 시각**과 장중 · 장 마감 뒤 구분을 붙였습니다.
- 시장 예상치가 있는 236건에 **어닝 서프라이즈 · 쇼크** 판정을 달았습니다. 예상치는 첫 공시보다 먼저 나온 기사의 것만 씁니다.
- 정한 불변조건 7가지(근거 없는 숫자 금지 · 현재 값 하나 · 최초 공개 시각 고정 · 미래 정보 금지 등)를 만들 때마다 검사하고, 위반은 0건입니다.

## API

| 요청 | 하는 일 |
|---|---|
| `GET /v1/filings` | 최근 잠정실적 목록 (공개 시각 순) |
| `GET /v1/filings/{rcept_no}` | 공시 한 건: 숫자 · 증감 · 검증 · 원문 오기 후보 · 근거 · 버전 · 깜짝 판정 |
| `GET /v1/companies/{stock_code}/earnings` | 한 회사의 기간별 현재 값 |
| `GET /v1/companies/{stock_code}/earnings/{period}` | 기간 하나(`2024Q2` · `2023FY` · `2016M04`). 아직 공시 전이면 `NOT_YET` |

```text
GET /v1/companies/005930/earnings/2024Q2

{"status": "CORRECTION_NOT_REFLECTED",
 "warning": "이 기간에는 원문을 읽지 못한 정정공시가 있어, 정정 전 숫자를 보여 준다: 20240731800047 (20240731)",
 "surprise_at_first_release": {"surprise_label": "어닝 서프라이즈", "surprise_pct": 26.83,
                               "consensus_krw": 8200000000000, "consensus_published_at": "2024-06-21 15:58:00", ...},
 "current": {"rcept_no": "20240731800039", "first_published_at": "2024-07-05 08:44", "time_cat": "장 전",
             "headline": {"operating_income": {"current_krw": 10443900000000, ...}, ...},
             "metrics": [ ... 항목별 숫자 · 증감률(공시 · 재계산) · 검증 · 근거 ... ],
             "versions": [initial 07-05 → repeat 07-31 → correction 07-31]}}
```

## 구조

```text
src/prelim/
  source.py    공시 목록 · 원문 ZIP 읽기, 인코딩(UTF-8/CP949) 판별
  parse.py     HTML 표 → 격자 → 열 역할 · 항목 · 단위 · 기간
  checks.py    증감률 · 증감액 반올림 범위 검증, 원문 오기 후보
  store.py     버전 묶음 · KIND 공개 시각 · 시장 예상치 대비 깜짝 판정 · 불변조건 검사
  api.py       FastAPI
scripts/run_phase1.py    전체 파싱 + 세 가지 정확도 측정
scripts/build_store.py   API 가 읽는 표 만들기 + 불변조건 검사
tests/                   파서 · 검증 43개, API 시나리오 12개
```

## 실행

```bash
pip install -e ".[api,dev]"
python -m pytest
PRELIM_DATA_ROOT=<데이터 폴더> PYTHONPATH=src python scripts/run_phase1.py
PRELIM_DATA_ROOT=<데이터 폴더> PYTHONPATH=src python scripts/build_store.py
PYTHONPATH=src uvicorn prelim.api:app --port 8100
```

원문 데이터(DART 공시 원문 ZIP · OpenDART 재무 · KIND 공시 목록 · 기사 예상치)는 이 저장소에 들어 있지 않습니다.
