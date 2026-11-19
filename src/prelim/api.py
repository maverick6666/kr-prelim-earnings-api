"""잠정실적 API.

    GET /health
    GET /v1/filings                              최근 잠정실적 (최초 공개 시각 순)
    GET /v1/filings/{rcept_no}                   공시 한 건: 숫자 · 증감 · 검증 · 근거 · 깜짝 판정 · 버전
    GET /v1/companies/{stock_code}/earnings      한 회사의 기간별 현재 값
    GET /v1/companies/{stock_code}/earnings/{period}   기간 하나(예: 2024Q2 · 2023FY · 2016M04)

데이터는 scripts/build_store.py 가 만든 build/store/*.parquet 를 읽는다(PRELIM_STORE_DIR 로 바꿀 수 있음).
실행: PYTHONPATH=src uvicorn prelim.api:app --port 8100
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
from fastapi import FastAPI, HTTPException, Query

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

    # ── 응답 조각 ──
    def surprise(self, f: pd.Series) -> dict | None:
        if pd.isna(f.get("consensus_krw")):
            return None
        out = _row(f, ["surprise_label", "surprise_pct", "surprise_hold_reason", "consensus_krw",
                       "consensus_published_at", "consensus_age_days", "consensus_source", "consensus_sentence"])
        out["basis"] = "영업이익 · 첫 공시 전에 나온 기사 속 시장 예상치(에프앤가이드 등 인용)"
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
                       "version_kind", "version_no", "versions_in_chain", "first_published_at", "published_at",
                       "time_cat", "document_parsed"])
        out["period_inferred"] = out.get("period_key") == "UNKNOWN"
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


def create_app(store: Store | None = None) -> FastAPI:
    app = FastAPI(title="잠정실적 속보 API", version="0.2.0",
                  description="DART 잠정실적 공시를 숫자 · 증감 · 깜짝 여부 · 근거로 정리해 준다.")
    state: dict[str, Store] = {}

    def db() -> Store:
        if "store" not in state:
            state["store"] = store if store is not None else Store.load()
        return state["store"]

    @app.get("/health")
    def health() -> dict:
        s = db()
        return {"status": "ok", "filings": int(len(s.filings)), "metric_rows": int(len(s.metrics))}

    @app.get("/v1/filings")
    def list_filings(date: str | None = Query(None, description="최초 공개일 YYYY-MM-DD"),
                     limit: int = Query(20, ge=1, le=200), offset: int = Query(0, ge=0)) -> dict:
        f = db().filings
        f = f[f["document_parsed"] & ~f["is_subsidiary_major_matter"].astype(bool)]
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
        for _period, g in sorted(f.groupby("period_key"), key=lambda t: t[0], reverse=True)[:limit]:
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
            known = sorted(f.loc[f["period_key"] != "UNKNOWN", "period_key"].unique())
            # 경계 시나리오: 아직 공시하지 않은 기간은 오류가 아니라 '아직 없음'
            return {"status": "NOT_YET", "stock_code": stock_code, "period": period,
                    "latest_available_period": known[-1] if known else None}
        g = g if scope else pick_scope(g)
        chain_id = g.sort_values("rcept_no").iloc[-1]["chain_id"]
        out = period_view(g[g["chain_id"] == chain_id])
        return {"stock_code": stock_code, "period": period} | out

    return app


app = create_app()
