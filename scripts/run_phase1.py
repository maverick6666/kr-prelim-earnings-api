"""1단계: 잠정실적 공시 전체를 파싱하고, 정답 없이 세 가지 방법으로 정확도를 잰다.

1) 내부 검증: 공시에 함께 적힌 증감률 · 증감액 ↔ 뽑은 숫자로 다시 계산한 값 (checks.py)
2) 확정 재무 대조: 같은 회사 · 같은 분기의 OpenDART 정기보고서 숫자(원 단위)와 비교
   - '전년동기' 값은 이미 확정된 숫자라 거의 정확히 같아야 한다 → 숫자와 단위를 함께 검증
   - '당기' 값은 잠정치라 확정치와 조금 다를 수 있다 → 잠정 ↔ 확정 차이 분포
3) AI 대리 정답 240건(매출, 사람 검수 아님)과 대조

실행: PYTHONPATH=src python scripts/run_phase1.py
결과: build/ (큰 표, 깃에 안 올림) · reports/phase1_summary.json
"""

from __future__ import annotations

import json
import math
import sqlite3
import sys
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pandas as pd

from prelim.checks import check_row
from prelim.parse import parse_html
from prelim.source import DATA_ROOT, load_inventory, read_document

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build"
REPORTS = ROOT / "reports"
GOLD_DIR = DATA_ROOT / "derived/kr_disclosure_gold_labels/run_id=oef_v1_kr_20100101_20251231_d1d439e2bc_2607f8ebdc"
DART_DB = DATA_ROOT / "raw/opendart/dart_kr.sqlite"

FINAL_ACCOUNTS = {
    "REVENUE": ("매출액",),
    "OPERATING_INCOME": ("영업이익", "영업이익(손실)"),
    "PRETAX_INCOME": ("법인세차감전 순이익",),
    "NET_INCOME": ("당기순이익(손실)", "당기순이익"),
}


def _val(a):
    return None if a is None else a.value


def parse_one(rcept_no: str) -> tuple[dict, list[dict]]:
    doc = read_document(rcept_no)
    if doc is None:
        return {"rcept_no": rcept_no, "status": "NO_DOCUMENT"}, []
    p = parse_html(rcept_no, doc.html, doc.encoding)
    filing = {
        "rcept_no": rcept_no, "status": p.status, "encoding": p.encoding, "unit_raw": p.unit_raw,
        "unit_multiplier": p.unit_multiplier, "results_tables": p.results_tables,
        "period_start": p.period_current[0] if p.period_current else None,
        "period_end": p.period_current[1] if p.period_current else None,
        "label_current": p.period_labels.get("CURRENT"), "roles": json.dumps(p.column_roles),
        "period_kind": p.period.kind, "period_year": p.period.year, "period_quarter": p.period.quarter,
        "period_month": p.period.month, "period_source": p.period.source,
        "notes": " / ".join(p.notes) or None, "n_metric_rows": len(p.metrics),
    }
    rows = []
    for m in p.metrics:
        chk = check_row(m, p.unit_multiplier)
        rows.append({
            "rcept_no": rcept_no, "metric": m.metric, "metric_label": m.metric_label, "span": m.span,
            "current": _val(m.current), "prior_period": _val(m.prior_period), "prior_year": _val(m.prior_year),
            "qoq_pct": _val(m.qoq_pct), "yoy_pct": _val(m.yoy_pct),
            "qoq_change": _val(m.qoq_change), "yoy_change": _val(m.yoy_change),
            "qoq_turn": m.qoq_turn, "yoy_turn": m.yoy_turn,
            "chk_yoy_pct": chk.yoy_pct, "chk_qoq_pct": chk.qoq_pct,
            "chk_yoy_change": chk.yoy_change, "chk_qoq_change": chk.qoq_change, "chk": chk.verdict,
            "fix_field": chk.fix[0] if chk.fix else None, "fix_raw": chk.fix[1] if chk.fix else None,
            "fix_value": chk.fix[2] if chk.fix else None,
            "evidence": m.evidence[:400],
        })
    return filing, rows


REPRT_BY_QUARTER = {1: "11013", 2: "11012", 3: "11014", 4: "11011"}


def report_key(kind, year, quarter, receipt_date: pd.Timestamp) -> tuple[str | None, str | None, str]:
    """확정 재무와 맞출 (사업연도, 보고서 코드, 근거).

    원문에서 읽은 분기를 쓴다. 월별 공시는 분기 보고서와 맞출 수 없어 뺀다.
    기간을 못 읽었으면 접수일 직전 분기 말(12월 결산 가정)로 추정하고 근거를 'inferred' 로 남긴다.
    """
    if kind == "MONTHLY":
        return None, None, "monthly"
    if kind in ("QUARTER", "HALF", "ANNUAL") and not pd.isna(year) and not pd.isna(quarter):
        return str(int(year)), REPRT_BY_QUARTER.get(int(quarter)), "parsed"
    d = receipt_date - pd.Timedelta(days=1)
    q = (d.month - 1) // 3  # 접수 직전 분기
    if q == 0:
        return str(d.year - 1), REPRT_BY_QUARTER[4], "inferred"
    return str(d.year), REPRT_BY_QUARTER[q], "inferred"


def load_final_financials() -> pd.DataFrame:
    names = sorted({n for v in FINAL_ACCOUNTS.values() for n in v})
    placeholders = ",".join("?" * len(names))
    q = (
        "select corp_code, bsns_year, reprt_code, fs_div, account_nm, thstrm_amount, thstrm_add_amount, "
        f"frmtrm_amount, frmtrm_add_amount from fin_rows where sj_div='IS' and account_nm in ({placeholders})"
    )
    with sqlite3.connect(f"file:{DART_DB}?mode=ro", uri=True) as con:
        df = pd.read_sql_query(q, con, params=names)
    for c in ("thstrm_amount", "thstrm_add_amount", "frmtrm_amount", "frmtrm_add_amount"):
        df[c] = pd.to_numeric(df[c].str.replace(",", "", regex=False), errors="coerce")
    inv_names = {n: k for k, v in FINAL_ACCOUNTS.items() for n in v}
    df["metric"] = df["account_nm"].map(inv_names)
    return df.drop_duplicates(["corp_code", "bsns_year", "reprt_code", "fs_div", "metric"])


def _missing(x: float | None) -> bool:
    return x is None or (isinstance(x, float) and math.isnan(x))


def ratio_class(prelim: float | None, final: float | None) -> str:
    if _missing(prelim):
        return "NO_PRELIM"
    if _missing(final):
        return "NO_FINAL"
    if final == 0 or prelim == 0:
        return "ZERO"
    r = prelim / final
    if r < 0:
        return "SIGN_FLIP"
    if abs(r - 1) <= 0.005:
        return "EXACT_0.5%"
    if abs(r - 1) <= 0.05:
        return "WITHIN_5%"
    if abs(r - 1) <= 0.3:
        return "WITHIN_30%"
    k = round(math.log10(r))
    if k != 0 and abs(r / 10**k - 1) <= 0.1:
        return f"UNIT_LIKE_10^{k}"
    return "FAR"


def diagnose_far(x: pd.DataFrame, fin: pd.DataFrame) -> dict:
    """확정치와 크게 다른 행을 진단한다.

    같은 회사 · 같은 항목의 다른 분기 · 연도 · 연결/별도 확정치 중 0.5% 안에서 맞는 것이 있는지 본다.

    있으면 기간이나 기준을 잘못 맞춘 것(PERIOD_OR_SCOPE_MISMATCH), 어디에도 없으면 원문 숫자나 확정 재무 쪽 문제
    (NOT_FOUND_ANYWHERE)로 본다. 어떤 키에서 맞았는지도 몇 가지 센다.
    """
    pool: dict[tuple[str, str], list[tuple[tuple, float]]] = {}
    for row in fin.itertuples(index=False):
        for col in ("thstrm_amount", "thstrm_add_amount", "frmtrm_amount", "frmtrm_add_amount"):
            v = getattr(row, col)
            if v is not None and not (isinstance(v, float) and math.isnan(v)) and v != 0:
                pool.setdefault((row.corp_code, row.metric), []).append(
                    ((row.bsns_year, row.reprt_code, row.fs_div, col), float(v)))
    result = Counter()
    where = Counter()
    for what, col in (("prior_year", "cls_prior_year"), ("current", "cls_current")):
        far = x[x[col] == "FAR"]
        for row in far.itertuples(index=False):
            value = getattr(row, f"prelim_{what}_krw")
            hits = [k for k, v in pool.get((row.corp_code, row.metric), []) if abs(value / v - 1) <= 0.005]
            if hits:
                result[f"{what}/PERIOD_OR_SCOPE_MISMATCH"] += 1
                year, reprt, fs, _ = hits[0]
                same_scope = fs == row.fs_div
                same_year = year == row.bsns_year
                where["다른 분기(같은 해 · 같은 기준)" if same_year and same_scope and reprt != row.reprt_code else
                      "다른 해" if not same_year else "연결/별도 반대" if not same_scope else "같은 키의 다른 열"] += 1
            else:
                result[f"{what}/NOT_FOUND_ANYWHERE"] += 1
            result[f"{what}/basis_{row.period_basis}"] += 1
    return {"counts": dict(result), "mismatch_where": dict(where)}


def final_crosscheck(metrics: pd.DataFrame, filings: pd.DataFrame, inv: pd.DataFrame,
                     fin: pd.DataFrame) -> pd.DataFrame:
    f = filings.merge(inv[["rcept_no", "corp_code", "receipt_date"]], on="rcept_no")
    keys = [report_key(k, y, q, rd) for k, y, q, rd in
            zip(f["period_kind"], f["period_year"], f["period_quarter"], f["receipt_date"], strict=False)]
    f["bsns_year"], f["reprt_code"], f["period_basis"] = zip(*keys, strict=False)
    f["fs_div"] = f["statement_scope"].map({"CONSOLIDATED": "CFS", "STANDALONE": "OFS"})
    f["alt_fs_div"] = f["fs_div"].map({"CFS": "OFS", "OFS": "CFS"})
    cols = ["rcept_no", "corp_code", "bsns_year", "reprt_code", "fs_div", "alt_fs_div", "unit_multiplier",
            "period_kind", "period_basis"]
    m = metrics[metrics["metric"].isin(FINAL_ACCOUNTS)].merge(f[cols], on="rcept_no")
    key = ["corp_code", "bsns_year", "reprt_code", "metric"]
    value_cols = ["thstrm_amount", "thstrm_add_amount", "frmtrm_amount", "frmtrm_add_amount"]
    m = m.merge(fin[key + ["fs_div"] + value_cols], on=key + ["fs_div"], how="left")
    renames = {"fs_div": "alt_fs_div", **{c: "alt_" + c for c in value_cols}}
    alt = fin[key + ["fs_div"] + value_cols].rename(columns=renames)
    m = m.merge(alt, on=key + ["alt_fs_div"], how="left")

    annual = m["reprt_code"] == "11011"
    cumulative = m["span"] == "CUMULATIVE"
    use_period_value = ~cumulative | annual | (m["reprt_code"] == "11013")
    # 4분기 3개월 값은 사업보고서에 없고, 반기 공시의 '당해' 는 6개월이라 분기 값과 맞출 수 없다.
    # 연간 공시의 '당해' 는 1년치라 사업보고서 연간 값과 바로 맞춘다.
    is_annual_filing = m["period_kind"].eq("ANNUAL")
    blank = (annual & ~cumulative & ~is_annual_filing) | (m["period_kind"].eq("HALF") & ~cumulative)

    def finals(prefix: str) -> tuple[pd.Series, pd.Series]:
        cur = m[prefix + "thstrm_amount"].where(use_period_value, m[prefix + "thstrm_add_amount"]).mask(blank)
        prior = m[prefix + "frmtrm_amount"].where(use_period_value, m[prefix + "frmtrm_add_amount"]).mask(blank)
        return cur, prior

    m["final_current"], m["final_prior_year"] = finals("")
    m["alt_final_current"], m["alt_final_prior_year"] = finals("alt_")
    mult = m["unit_multiplier"]
    # 원문 오기로 표시된 칸은 고친 값으로 대조한다(원래 값은 parsed_metrics 에 그대로 남아 있다)
    m["prelim_current_krw"] = m["current"].where(m["fix_field"] != "current", m["fix_value"]) * mult
    m["prelim_prior_year_krw"] = m["prior_year"].where(m["fix_field"] != "prior_year", m["fix_value"]) * mult
    for what in ("prior_year", "current"):
        prelim, final, alt_final = m[f"prelim_{what}_krw"], m[f"final_{what}"], m[f"alt_final_{what}"]
        cls = [ratio_class(a, b) for a, b in zip(prelim, final, strict=False)]
        alt_cls = [ratio_class(a, b) for a, b in zip(prelim, alt_final, strict=False)]
        # 제목은 별도인데 숫자는 연결(또는 반대)인 공시: 반대 기준과는 정확히 맞는 경우
        m[f"cls_{what}"] = ["SCOPE_MISMATCH" if c not in ("EXACT_0.5%",) and a == "EXACT_0.5%" else c
                            for c, a in zip(cls, alt_cls, strict=False)]
    return m.drop(columns=["alt_" + c for c in value_cols])


def _same_krw(display_value: float | None, multiplier: float, gold_krw: float | None) -> bool:
    if _missing(display_value) or _missing(gold_krw):
        return False
    return abs(display_value * multiplier - gold_krw) < 1


def gold_compare(metrics: pd.DataFrame, filings: pd.DataFrame) -> dict:
    packets = {}
    for line in (GOLD_DIR / "gold_packet_price_blind.jsonl").open(encoding="utf-8"):
        p = json.loads(line)
        packets[p["packet_id"]] = p["source_evidence"][0]["external_id"]
    mult = filings.set_index("rcept_no")["unit_multiplier"]
    by_key = metrics.set_index(["rcept_no", "metric", "span"])
    results = Counter()
    misses = []
    for line in (GOLD_DIR / "codex_proxy_gold.jsonl").open(encoding="utf-8"):
        g = json.loads(line)
        rcept = packets.get(g["packet_id"])
        for pair in g.get("numeric_pairs") or []:
            span = {"REVENUE": "PERIOD", "REVENUE_CUMULATIVE": "CUMULATIVE"}.get(pair["metric"])
            if span is None or not pair["period"].startswith("CURRENT"):
                continue
            key = (rcept, "REVENUE", span)
            if key not in by_key.index or rcept not in mult.index or pd.isna(mult[rcept]):
                results["NOT_PARSED"] += 1
                misses.append((rcept, span, "NOT_PARSED"))
                continue
            row = by_key.loc[key]
            if isinstance(row, pd.DataFrame):
                row = row.iloc[0]
            ok_cur = _same_krw(row["current"], mult[rcept], pair.get("actual"))
            # 정답도 우리도 비교값이 없으면(예: 첫 결산) 일치로 본다
            ok_prev = _same_krw(row["prior_year"], mult[rcept], pair.get("expected")) or (
                _missing(pair.get("expected")) and _missing(row["prior_year"]))
            verdict = "BOTH_MATCH" if ok_cur and ok_prev else ("CURRENT_ONLY" if ok_cur else "MISMATCH")
            results[verdict] += 1
            if verdict != "BOTH_MATCH":
                misses.append((rcept, span, verdict))
    return {"counts": dict(results), "misses": misses[:30]}


def main() -> None:
    BUILD.mkdir(exist_ok=True)
    REPORTS.mkdir(exist_ok=True)
    t0 = time.time()
    inv = load_inventory()
    target = inv[inv["has_document"] & ~inv["is_monthly_report"]]
    print(f"목록 {len(inv):,}건 · 원문 있음 {int(inv['has_document'].sum()):,} · 월별 제외 대상 {len(target):,}",
          flush=True)

    if "--reuse" in sys.argv and (BUILD / "parsed_metrics.parquet").exists():
        filings_df = pd.read_parquet(BUILD / "parsed_filings.parquet")
        metrics_df = pd.read_parquet(BUILD / "parsed_metrics.parquet")
        print("저장된 파싱 결과를 다시 씀(--reuse)", flush=True)
    else:
        filings, rows = [], []
        with ProcessPoolExecutor(max_workers=6) as pool:
            for i, (f, r) in enumerate(pool.map(parse_one, target["rcept_no"], chunksize=200)):
                filings.append(f)
                rows.extend(r)
                if (i + 1) % 5000 == 0:
                    print(f"  {i + 1:,}건 · {time.time() - t0:.0f}초", flush=True)
        filings_df = pd.DataFrame(filings).merge(
            target[["rcept_no", "receipt_year", "correction_kind", "statement_scope", "corp_name"]], on="rcept_no")
        metrics_df = pd.DataFrame(rows)
        filings_df.to_parquet(BUILD / "parsed_filings.parquet", index=False)
        metrics_df.to_parquet(BUILD / "parsed_metrics.parquet", index=False)
        print(f"파싱 끝 {time.time() - t0:.0f}초 · 행 {len(metrics_df):,}", flush=True)

    era = pd.cut(filings_df["receipt_year"], [2001, 2007, 2026], labels=["2002-2007", "2008-2026"])
    summary: dict = {
        "inventory_filings": int(len(inv)),
        "with_document": int(inv["has_document"].sum()),
        "monthly_reports_excluded": int((inv["has_document"] & inv["is_monthly_report"]).sum()),
        "parsed_target": int(len(target)),
        "status_by_era": {
            str(k): v.value_counts().to_dict() for k, v in filings_df.groupby(era, observed=True)["status"]
        },
        "encoding": filings_df["encoding"].value_counts().to_dict(),
        "unit": filings_df["unit_raw"].value_counts().head(10).to_dict(),
    }

    m = metrics_df.merge(filings_df[["rcept_no", "receipt_year", "status"]], on="rcept_no")
    m = m[(m["receipt_year"] >= 2008) & (m["status"] == "OK")]
    ok_filings = filings_df[(filings_df["receipt_year"] >= 2008) & (filings_df["status"] == "OK")]
    coverage = {}
    for metric in ("REVENUE", "OPERATING_INCOME", "PRETAX_INCOME", "NET_INCOME", "NET_INCOME_CONTROLLING"):
        have = m[(m["metric"] == metric) & (m["span"] == "PERIOD") & m["current"].notna()]["rcept_no"].nunique()
        coverage[metric] = round(have / max(1, len(ok_filings)), 4)
    summary["metric_coverage_2008plus"] = coverage
    summary["self_check_2008plus"] = {
        f"{metric}/{span}": g["chk"].value_counts().to_dict()
        for (metric, span), g in m.groupby(["metric", "span"])
    }
    per_check = {}
    for col in ("chk_yoy_pct", "chk_qoq_pct", "chk_yoy_change", "chk_qoq_change"):
        per_check[col] = m[col].value_counts().to_dict()
    summary["self_check_by_type_2008plus"] = per_check

    core = m[m["metric"].isin(["REVENUE", "OPERATING_INCOME", "NET_INCOME"]) & (m["span"] == "PERIOD")]
    piv = core.pivot_table(index="rcept_no", columns="metric", values="chk", aggfunc="first")
    all_pass = (piv == "PASS").all(axis=1)
    any_fail = (piv == "FAIL").any(axis=1)
    summary["filings_core3_all_pass"] = {"n": int(len(piv)), "all_pass": int(all_pass.sum()),
                                         "any_fail": int(any_fail.sum())}

    print("확정 재무 대조 중…", flush=True)
    fin = load_final_financials()
    x = final_crosscheck(metrics_df, filings_df[filings_df["status"] == "OK"], inv, fin)
    x.drop(columns=["evidence"]).to_parquet(BUILD / "final_crosscheck.parquet", index=False)
    unmatched = ("NO_FINAL", "NO_PRELIM")
    summary["final_crosscheck"] = {
        "prior_year_vs_final": {f"{k[0]}/{k[1]}": v.value_counts().to_dict()
                                for k, v in x.groupby(["metric", "span"])["cls_prior_year"]},
        "current_vs_final": {f"{k[0]}/{k[1]}": v.value_counts().to_dict()
                             for k, v in x.groupby(["metric", "span"])["cls_current"]},
        # 비율(%): 확정 재무와 맞춰진 행만 분모로
        "rates_by_basis": {
            f"{what}/{basis}": (g[~g[col].isin(unmatched)][col].value_counts(normalize=True) * 100).round(2).to_dict()
            for what, col in (("prior_year", "cls_prior_year"), ("current", "cls_current"))
            for basis, g in x.groupby("period_basis")
        },
        "matched_rows": {what: int((~x[col].isin(unmatched)).sum())
                         for what, col in (("prior_year", "cls_prior_year"), ("current", "cls_current"))},
    }
    # 10의 거듭제곱 배 차이: 공시 전체가 같은 배수면 표 단위 오기 · 확정 재무 단위 문제, 일부 칸이면 자릿수 오기
    unit_rows = x[x["cls_prior_year"].str.startswith("UNIT") | x["cls_current"].str.startswith("UNIT")]
    kinds = Counter()
    for r in unit_rows["rcept_no"].unique():
        rows = x[x["rcept_no"] == r]
        classes = (set(rows["cls_prior_year"]) | set(rows["cls_current"])) - set(unmatched)
        kinds["WHOLE_FILING_SAME_FACTOR" if len(classes) == 1 else "SOME_CELLS"] += 1
    summary["final_crosscheck"]["unit_like_filings"] = dict(kinds)
    summary["final_crosscheck"]["far_diagnosis"] = diagnose_far(x, fin)

    summary["proxy_gold_revenue"] = gold_compare(metrics_df, filings_df)
    summary["elapsed_sec"] = round(time.time() - t0)
    (REPORTS / "phase1_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1,
                                                            default=str), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=1, default=str)[:6000])


if __name__ == "__main__":
    sys.exit(main())
