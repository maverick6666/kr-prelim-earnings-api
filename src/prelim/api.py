"""잠정실적 API.

    GET /                                        데모 화면
    GET /health
    GET /v1/search?q=                            회사명 · 종목코드 찾기
    GET /v1/filings                              최근 잠정실적 (최초 공개 시각 순)
    GET /v1/filings/{rcept_no}                   공시 한 건: 숫자 · 증감 · 검증 · 근거 · 깜짝 판정 · 버전
    GET /v1/companies/{stock_code}/earnings      한 회사의 기간별 현재 값
    GET /v1/companies/{stock_code}/earnings/{period}   기간 하나(예: 2024Q2 · 2023FY · 2016M04)
        status: OK · CORRECTION_NOT_REFLECTED(원문 없는 정정공시가 있음) · NUMBERS_NOT_AVAILABLE
                · NOT_YET(자료 끝 기준 아직 나올 때가 안 됨) · NOT_FOUND(그 기간 공시를 못 찾음 — 이유를 note 에)

데이터는 scripts/build_store.py 가 만든 build/store/*.parquet 를 읽는다(PRELIM_STORE_DIR 로 바꿀 수 있음).
실행: PYTHONPATH=src uvicorn prelim.api:app --port 8100
"""

from __future__ import annotations

import calendar
import math
import os
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse

WEB_DIR = Path(__file__).resolve().parent / "web"
STORE_DIR = Path(os.environ.get("PRELIM_STORE_DIR", Path(__file__).resolve().parents[2] / "build" / "store"))
PERIOD_RE = re.compile(r"^\d{4}(Q[1-4]|FY|M(0[1-9]|1[0-2]))$")
METRIC_NAMES = {
    "REVENUE": "매출액", "OPERATING_INCOME": "영업이익", "PRETAX_INCOME": "세전이익",
    "NET_INCOME": "당기순이익", "NET_INCOME_CONTROLLING": "지배주주순이익",
}
DART_URL = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo={}"


def _clean(v):
    """JSON 으로 내보낼 값: NaN · NaT → None, numpy 수 → 파이썬 수."""
    if v is None:
        return None
    if isinstance(v, float) and math.isnan(v):
        return None
    if v is pd.NaT:
        return None
    if hasattr(v, "item"):
        v = v.item()
        if isinstance(v, float) and math.isnan(v):
            return None
    return v


def _row(series: pd.Series, cols: list[str]) -> dict:
    return {c: _clean(series.get(c)) for c in cols}


@dataclass
class Store:
    filings: pd.DataFrame
    metrics: pd.DataFrame

    @classmethod
    def load(cls, directory: Path = STORE_DIR) -> Store:
        return cls(pd.read_parquet(directory / "filings.parquet"), pd.read_parquet(directory / "metrics.parquet"))

    def __post_init__(self) -> None:
        self.filings = self.filings.set_index("rcept_no", drop=False).rename_axis(None)
        self._metrics_by_filing = {k: g for k, g in self.metrics.groupby("rcept_no")}
        # 자료 끝 = 가장 늦게 접수된 공시의 날짜(NOT_YET 판단 기준)
        self.data_end = pd.to_datetime(self.filings["rcept_dt"], format="%Y%m%d").max().date()

    # ── 응답 조각 ──
    def surprise(self, f: pd.Series) -> dict | None:
        if pd.isna(f.get("consensus_krw")):
            return None
        out = _row(f, ["surprise_label", "surprise_pct", "surprise_hold_reason", "consensus_krw",
                       "consensus_published_at", "consensus_age_days", "consensus_source", "consensus_sentence"])
        out["basis"] = "영업이익 · 첫 공시 전에 나온 기사 속 시장 예상치(에프앤가이드 등 인용)"
        # 예상치가 작으면 % 가 터진다(예상 1,199억 · 실제 −1조 7,334억 → −1545.7%). 금액 차이를 함께 준다.
        actual = (self.headline(f["rcept_no"]).get("operating_income") or {}).get("current_krw")
        out["actual_krw"] = actual
        out["gap_krw"] = None if actual is None else actual - out["consensus_krw"]
        return out

    def metric_rows(self, rcept_no: str) -> list[dict]:
        g = self._metrics_by_filing.get(rcept_no)
        if g is None:
            return []
        rows = []
        for _, m in g.iterrows():
            row = {
                "metric": m["metric"], "name": METRIC_NAMES.get(m["metric"], m["metric_label"]),
                "label_in_filing": m["metric_label"], "span": m["span"],
                "current_krw": _clean(m["current_krw"]), "prior_period_krw": _clean(m["prior_period_krw"]),
                "prior_year_krw": _clean(m["prior_year_krw"]),
                "yoy_pct": {"reported": _clean(m["yoy_pct"]), "calculated": _clean(m["yoy_pct_calc"])},
                "qoq_pct": {"reported": _clean(m["qoq_pct"]), "calculated": _clean(m["qoq_pct_calc"])},
                "yoy_turn": _clean(m["yoy_turn"]) or _clean(m["yoy_turn_calc"]),
                "check": {"verdict": m["chk"], "yoy_pct": m["chk_yoy_pct"], "qoq_pct": m["chk_qoq_pct"],
                          "yoy_change": m["chk_yoy_change"], "qoq_change": m["chk_qoq_change"]},
                "source_typo": None if pd.isna(m["fix_field"]) else {
                    "field": m["fix_field"], "as_written": m["fix_raw"], "corrected": _clean(m["fix_value"])},
                "evidence": m["evidence"],
            }
            rows.append(row)
        return rows

    def headline(self, rcept_no: str) -> dict:
        g = self._metrics_by_filing.get(rcept_no)
        out = {}
        if g is None:
            return out
        period = g[g["span"] == "PERIOD"].set_index("metric")
        for metric in ("REVENUE", "OPERATING_INCOME", "NET_INCOME"):
            if metric in period.index:
                m = period.loc[metric]
                m = m.iloc[0] if isinstance(m, pd.DataFrame) else m
                out[metric.lower()] = {"current_krw": _clean(m["current_krw"]),
                                       "yoy_pct": _clean(m["yoy_pct_calc"]) if not pd.isna(m["yoy_pct_calc"])
                                       else _clean(m["yoy_pct"]),
                                       "yoy_turn": _clean(m["yoy_turn"]) or _clean(m["yoy_turn_calc"])}
        return out

    def chain_surprise(self, chain_id: str) -> dict | None:
        """묶음의 깜짝 판정은 첫 버전(시장이 처음 숫자를 본 공시)에만 있다."""
        first = self.filings[(self.filings["chain_id"] == chain_id) & (self.filings["version_no"] == 1)]
        return None if first.empty else self.surprise(first.iloc[0])

    def versions(self, chain_id: str) -> list[dict]:
        chain = self.filings[self.filings["chain_id"] == chain_id].sort_values("version_no")
        return [_row(v, ["rcept_no", "version_no", "version_kind", "correction_kind", "report_nm",
                         "published_at", "rcept_dt", "document_parsed"]) | {"dart_url": DART_URL.format(v["rcept_no"])}
                for _, v in chain.iterrows()]

    def summary(self, f: pd.Series) -> dict:
        out = _row(f, ["rcept_no", "corp_name", "stock_code", "statement_scope", "period_key", "period_kind",
                       "period_source", "version_kind", "version_no", "versions_in_chain", "first_published_at",
                       "published_at", "time_cat", "document_parsed"])
        out["headline"] = self.headline(f["rcept_no"])
        out["surprise"] = self.surprise(f)
        out["dart_url"] = DART_URL.format(f["rcept_no"])
        return out

    def detail(self, f: pd.Series) -> dict:
        out = self.summary(f)
        out |= _row(f, ["corp_code", "report_nm", "rcept_dt", "correction_kind", "unit_raw", "chain_id",
                        "supersedes", "is_latest", "time_match", "notes"])
        out["metrics"] = self.metric_rows(f["rcept_no"])
        out["versions"] = self.versions(f["chain_id"])
        return out


_KIND_ORDER = {"M": 0, "Q": 1, "FY": 2}


def period_order(key: str) -> tuple[int, int, int]:
    """기간 키를 기간이 끝나는 때 순서로: 2024M05 < 2024Q2 < 2024M07 < 2024Q4 < 2024FY."""
    year = int(key[:4])
    if key.endswith("FY"):
        return year, 12, _KIND_ORDER["FY"]
    if key[4] == "M":
        return year, int(key[5:]), _KIND_ORDER["M"]
    return year, 3 * int(key[5]), _KIND_ORDER["Q"]


FILING_LAG_DAYS = 60  # 잠정실적은 보통 기간이 끝나고 두 달 안에 나온다


def period_end(key: str) -> date:
    """기간이 끝나는 날: 2024Q2 → 2024-06-30, 2023FY → 2023-12-31, 2016M04 → 2016-04-30."""
    year = int(key[:4])
    month = 12 if key.endswith("FY") else int(key[5:]) if key[4] == "M" else 3 * int(key[5])
    return date(year, month, calendar.monthrange(year, month)[1])


def not_yet(period: str, data_end: date, lag_days: int = FILING_LAG_DAYS) -> bool:
    """자료 끝 기준으로 그 기간의 잠정실적이 아직 나올 때가 안 됐나(기간 끝 + lag 가 자료 끝보다 뒤)."""
    return period_end(period) + timedelta(days=lag_days) > data_end


def create_app(store: Store | None = None) -> FastAPI:
    state: dict[str, Store] = {} if store is None else {"store": store}

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        db()  # 서버가 뜰 때 한 번 읽어 둔다(첫 요청이 수 초 걸리지 않게)
        yield

    app = FastAPI(title="잠정실적 속보 API", version="0.2.0", lifespan=lifespan,
                  description="DART 잠정실적 공시를 숫자 · 증감 · 깜짝 여부 · 근거로 정리해 준다.")

    def db() -> Store:
        if "store" not in state:
            state["store"] = Store.load()
        return state["store"]

    @app.get("/", include_in_schema=False)
    def demo_page() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/v1/search")
    def search(q: str = Query(..., min_length=1, max_length=40), limit: int = Query(10, ge=1, le=30)) -> dict:
        """회사명 일부나 종목코드로 찾는다. 회사마다 가장 최근 잠정실적 공시 하나를 함께 준다."""
        f = db().filings
        f = f[f["document_parsed"] & ~f["is_subsidiary_major_matter"].astype(bool)]
        hit = f[(f["stock_code"] == q) | f["corp_name"].str.contains(q, regex=False, na=False)]
        latest = hit.sort_values("rcept_no").groupby("stock_code").tail(1).sort_values("rcept_no", ascending=False)
        return {"items": [{"stock_code": r["stock_code"], "corp_name": r["corp_name"],
                           "latest_rcept_no": r["rcept_no"]} for _, r in latest.head(limit).iterrows()]}

    @app.get("/health")
    def health() -> dict:
        s = db()
        return {"status": "ok", "filings": int(len(s.filings)), "metric_rows": int(len(s.metrics))}

    @app.get("/v1/filings")
    def list_filings(date: str | None = Query(None, description="최초 공개일 YYYY-MM-DD"),
                     exclude_monthly: bool = Query(False, description="월별 매출 공시 빼기"),
                     limit: int = Query(20, ge=1, le=200), offset: int = Query(0, ge=0)) -> dict:
        f = db().filings
        f = f[f["document_parsed"] & ~f["is_subsidiary_major_matter"].astype(bool)]
        if exclude_monthly:
            f = f[f["period_kind"] != "MONTHLY"]
        if date:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
                raise HTTPException(422, "date 는 YYYY-MM-DD")
            f = f[f["first_receipt_date"] == date]
        order = pd.to_datetime(f["published_at"]).fillna(pd.to_datetime(f["rcept_dt"], format="%Y%m%d"))
        f = f.assign(_order=order).sort_values(["_order", "rcept_no"], ascending=False)
        page = f.iloc[offset:offset + limit]
        return {"total": int(len(f)), "items": [db().summary(r) for _, r in page.iterrows()]}

    @app.get("/v1/filings/{rcept_no}")
    def get_filing(rcept_no: str) -> dict:
        s = db()
        if not re.fullmatch(r"\d{14}", rcept_no):
            raise HTTPException(422, "접수번호는 14자리 숫자")
        if rcept_no not in s.filings.index:
            raise HTTPException(404, "잠정실적 공시가 아니거나 아직 없는 접수번호")
        return s.detail(s.filings.loc[rcept_no])

    def company_chains(stock_code: str, scope: str | None) -> pd.DataFrame:
        if not re.fullmatch(r"[0-9A-Z]{6}", stock_code):
            raise HTTPException(422, "종목코드는 6자리")
        f = db().filings
        f = f[(f["stock_code"] == stock_code) & ~f["is_subsidiary_major_matter"].astype(bool)]
        if scope:
            f = f[f["statement_scope"] == scope]
        return f

    def pick_scope(f: pd.DataFrame) -> pd.DataFrame:
        """연결이 있으면 연결, 없으면 별도(시장이 보는 기준)."""
        if (f["statement_scope"] == "CONSOLIDATED").any():
            return f[f["statement_scope"] == "CONSOLIDATED"]
        return f

    def period_view(f: pd.DataFrame) -> dict:
        """한 묶음의 현재 값: 마지막 버전의 원문을 읽었으면 그 숫자, 아니면 마지막으로 읽은 버전 + 경고."""
        chain = f.sort_values("version_no")
        latest = chain.iloc[-1]
        parsed = chain[chain["document_parsed"]]
        if parsed.empty:
            return {"status": "NUMBERS_NOT_AVAILABLE", "versions": db().versions(latest["chain_id"])}
        shown = parsed.iloc[-1]
        status = "OK" if bool(latest["document_parsed"]) else "CORRECTION_NOT_REFLECTED"
        out = {"status": status, "current": db().detail(shown),
               "surprise_at_first_release": db().chain_surprise(latest["chain_id"])}
        if status != "OK":
            out["warning"] = ("이 기간에는 원문을 읽지 못한 정정공시가 있어, 정정 전 숫자를 보여 준다: "
                              f"{latest['rcept_no']} ({latest['rcept_dt']})")
        return out

    @app.get("/v1/companies/{stock_code}/earnings")
    def company_earnings(stock_code: str, scope: str | None = Query(None, pattern="^(CONSOLIDATED|STANDALONE)$"),
                         limit: int = Query(12, ge=1, le=100)) -> dict:
        f = company_chains(stock_code, scope)
        if f.empty:
            raise HTTPException(404, "잠정실적 공시를 낸 적이 없는 종목코드")
        f = f[f["period_key"] != "UNKNOWN"]
        periods = []
        for _period, g in sorted(f.groupby("period_key"), key=lambda t: period_order(t[0]), reverse=True)[:limit]:
            g = g if scope else pick_scope(g)
            latest = g.sort_values("rcept_no").iloc[-1]
            chain = g[g["chain_id"] == latest["chain_id"]]
            shown = chain[chain["document_parsed"]].sort_values("version_no")
            if shown.empty:
                continue
            item = db().summary(shown.iloc[-1])
            item["correction_not_reflected"] = not bool(latest["document_parsed"])
            item["surprise_at_first_release"] = db().chain_surprise(latest["chain_id"])
            periods.append(item)
        return {"stock_code": stock_code, "corp_name": _clean(f["corp_name"].iloc[0]) if len(f) else None,
                "periods": periods}

    @app.get("/v1/companies/{stock_code}/earnings/{period}")
    def company_period(stock_code: str, period: str,
                       scope: str | None = Query(None, pattern="^(CONSOLIDATED|STANDALONE)$")) -> dict:
        if not PERIOD_RE.fullmatch(period):
            raise HTTPException(422, "기간은 2024Q2 · 2023FY · 2016M04 형식")
        f = company_chains(stock_code, scope)
        if f.empty:
            raise HTTPException(404, "잠정실적 공시를 낸 적이 없는 종목코드")
        g = f[f["period_key"] == period]
        if g.empty:
            known = sorted(f.loc[f["period_key"] != "UNKNOWN", "period_key"].unique(), key=period_order)
            latest = known[-1] if known else None
            out = {"stock_code": stock_code, "period": period, "latest_available_period": latest}
            # 경계 시나리오: 자료 끝 기준으로 아직 나올 때가 안 된 기간이면 '아직 없음'(오류 아님).
            # 회사의 마지막 공시 기간만 보면, 공시를 멈춘 회사(상장폐지 등)의 지난 기간까지 '아직' 이 된다.
            data_end = db().data_end
            out["data_end"] = data_end.isoformat()
            if not_yet(period, data_end):
                return {"status": "NOT_YET"} | out
            unknown = int((f["period_key"] == "UNKNOWN").sum())
            if latest is not None and period_order(period) > period_order(latest):
                out["note"] = (f"이 회사의 마지막 잠정실적은 {latest} 이다. 그 뒤로 잠정실적을 내지 않았다"
                               "(상장폐지 · 공시 중단 · 잠정실적 대신 정기보고서만 냄 등).")
            else:
                out["note"] = "이 기간의 잠정실적 공시를 찾지 못했다(잠정실적을 내지 않은 기간일 수 있다)."
            if unknown:
                out["note"] += f" 이 회사에는 기간 표시를 읽지 못한 공시가 {unknown}건 있다."
            return {"status": "NOT_FOUND"} | out
        g = g if scope else pick_scope(g)
        chain_id = g.sort_values("rcept_no").iloc[-1]["chain_id"]
        out = period_view(g[g["chain_id"] == chain_id])
        return {"stock_code": stock_code, "period": period} | out

    return app


app = create_app()
