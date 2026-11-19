"""API 가 읽는 정리된 표를 만든다: 공시(버전 · 공개 시각) · 실적 행(원 단위 · 근거 · 검증) · 깜짝 판정.

입력은 1단계 결과(build/parsed_*.parquet)와 공시 목록, KIND 공시 시각, 기사에서 뽑은 시장 예상치다.

지켜야 할 규칙(불변조건) — validate() 가 검사한다
1. 숫자는 근거(원문 행 글자)와 함께만 내보낸다.
2. 금액은 원(KRW) 단위다. 단위를 못 읽은 공시의 금액은 비운다.
3. 정정공시는 이전 버전을 덮어쓰지 않는다. 한 묶음(회사 · 연결/별도 · 기간)에 '현재 값' 은 하나다.
4. 최초 공개 시각은 묶음의 첫 공시 시각이고, 정정으로 바뀌지 않는다.
5. 깜짝 판정에 쓰는 시장 예상치는 그 공시가 나오기 전에 나온 기사의 것만 쓴다.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from .source import DATA_ROOT, load_inventory

KIND_DISCLOSURES = Path("derived/kr_earnings_events/disclosures.parquet")
CONSENSUS = Path("derived/consensus_kr/consensus_checked.parquet")
CONSENSUS_OK = ("정상", "큰 차이·확인됨")
CORRECTION_LINK_DAYS = 120
CONSENSUS_MAX_AGE_DAYS = 45
SIGN_WORDS = r"마이너스|적자|영업손실|손실 규모"


# ── 1. 공개 시각: DART 공시 ↔ KIND 공시(분 단위) ─────────────────────────
def _kind_scope(title: str) -> str:
    return "CONSOLIDATED" if "연결" in title else "STANDALONE"


def attach_publish_time(filings: pd.DataFrame, kind: pd.DataFrame) -> pd.DataFrame:
    """종목코드 · 날짜 · 정정 여부 · 연결/별도 · 자회사 여부가 같은 공시끼리, 같은 날 안의 순서로 맞춘다.

    DART 접수번호와 KIND 접수번호는 체계가 달라 바로 잇지 못한다. 같은 묶음 안에서 공시 수가 다르면
    순서 맞춤이 틀릴 수 있어 time_match 를 'ambiguous' 로 남긴다.
    """
    k = kind[kind["kind"] == "prelim"].copy()
    k["is_corr"] = k["is_corr"].astype(bool)
    k["scope"] = k["title"].map(_kind_scope)
    k["sub"] = k["title"].str.contains("자회사", regex=False)
    keys = ["stock_code", "day", "is_corr", "scope", "sub"]
    k = k.rename(columns={"code": "stock_code"}).sort_values(["time_kst", "acptno"])
    k["rank"] = k.groupby(keys).cumcount()
    k["group_n"] = k.groupby(keys)["acptno"].transform("size")

    f = filings.copy()
    f["day"] = pd.to_datetime(f["rcept_dt"], format="%Y%m%d").dt.strftime("%Y-%m-%d")
    f["is_corr"] = f["correction_kind"] != "INITIAL"
    f["scope"] = f["statement_scope"]
    f["sub"] = f["is_subsidiary_major_matter"].astype(bool)
    f = f.sort_values("rcept_no")
    f["rank"] = f.groupby(keys).cumcount()
    f["group_n_dart"] = f.groupby(keys)["rcept_no"].transform("size")
    out = f.merge(k[keys + ["rank", "time_kst", "time_cat", "group_n"]], on=keys + ["rank"], how="left")
    out["time_match"] = np.select(
        [out["time_kst"].isna(), out["group_n"] != out["group_n_dart"]], ["none", "ambiguous"], "exact")
    out["published_at"] = out["time_kst"].where(out["time_match"] == "exact")
    return out.drop(columns=["day", "is_corr", "scope", "sub", "rank", "group_n", "group_n_dart", "time_kst"])


# ── 2. 버전 묶음: 원 공시 + 정정공시 ────────────────────────────────────
def period_key(row) -> str:
    """버전 묶음에 쓰는 기간 표시: 2026Q1 · 2023FY · 2016M04 · UNKNOWN."""
    kind, year = row["period_kind"], row["period_year"]
    if pd.isna(year):
        return "UNKNOWN"
    y = int(year)
    if kind == "MONTHLY" and not pd.isna(row["period_month"]):
        return f"{y}M{int(row['period_month']):02d}"
    if kind == "ANNUAL":
        return f"{y}FY"
    if kind in ("QUARTER", "HALF") and not pd.isna(row["period_quarter"]):
        return f"{y}Q{int(row['period_quarter'])}"
    return "UNKNOWN"


def build_versions(filings: pd.DataFrame) -> pd.DataFrame:
    """같은 실적(회사 · 연결/별도 · 자회사 여부 · 기간)을 다룬 공시를 한 묶음으로 잇고 버전을 붙인다.

    - 기간을 읽은 원 공시는 같은 기간끼리 한 묶음이다. 삼성전자처럼 한 분기에 잠정실적을 두 번 내는 회사가 있다
      (먼저 요약, 뒤에 상세). 뒤의 것은 'repeat' 버전이다.
    - 정정공시는 같은 회사 · 기준의 원 공시 중 기간이 같은(둘 다 읽혔을 때) 가장 최근 것, 기간을 모르면
      120일 안의 가장 최근 원 공시에 잇는다. 이을 곳이 없으면 그 정정공시가 묶음의 처음이 된다.
    """
    f = filings.sort_values("rcept_no").copy()
    f["period_key"] = f.apply(period_key, axis=1)
    f["receipt_date"] = pd.to_datetime(f["rcept_dt"], format="%Y%m%d")
    f["chain_id"] = f["rcept_no"]
    base = ["corp_code", "statement_scope", "is_subsidiary_major_matter"]
    known = (f["correction_kind"] == "INITIAL") & (f["period_key"] != "UNKNOWN")
    f.loc[known, "chain_id"] = f[known].groupby(base + ["period_key"])["rcept_no"].transform("first")
    initials = f[f["correction_kind"] == "INITIAL"]
    by_base = {k: g for k, g in initials.groupby(base)}
    chain_of: dict[str, str] = {}
    for idx, row in f[f["correction_kind"] != "INITIAL"].iterrows():
        cands = by_base.get(tuple(row[c] for c in base))
        if cands is None:
            continue
        cands = cands[cands["rcept_no"] < row["rcept_no"]]
        if row["period_key"] != "UNKNOWN":
            same = cands[cands["period_key"] == row["period_key"]]
            cands = same if len(same) else cands[cands["period_key"] == "UNKNOWN"]
        cands = cands[(row["receipt_date"] - cands["receipt_date"]).dt.days <= CORRECTION_LINK_DAYS]
        if len(cands):
            target = cands.iloc[-1]
            f.at[idx, "chain_id"] = chain_of.get(target["rcept_no"], target["chain_id"])
            chain_of[row["rcept_no"]] = f.at[idx, "chain_id"]
            if row["period_key"] == "UNKNOWN":  # 원 공시의 기간을 물려받는다
                f.at[idx, "period_key"] = target["period_key"]
    f = f.sort_values(["chain_id", "rcept_no"])
    f["version_no"] = f.groupby("chain_id").cumcount() + 1
    f["version_kind"] = np.where(f["correction_kind"] != "INITIAL", "correction",
                                 np.where(f["version_no"] == 1, "initial", "repeat"))
    f["supersedes"] = f.groupby("chain_id")["rcept_no"].shift(1)
    f["is_latest"] = f.groupby("chain_id")["rcept_no"].transform("max") == f["rcept_no"]
    f["versions_in_chain"] = f.groupby("chain_id")["rcept_no"].transform("size")
    if "document_parsed" in f:
        # 현재 값을 숫자로 내줄 수 있는가: 묶음의 마지막 버전 원문을 읽었을 때만
        latest_parsed = f[f["is_latest"]].set_index("chain_id")["document_parsed"]
        f["chain_latest_parsed"] = f["chain_id"].map(latest_parsed).astype(bool)
    # 규칙 4: 묶음의 '첫 버전' 값이다. groupby.first() 는 빈 값을 건너뛰어 정정공시 시각을 집을 수 있어 쓰지 않는다.
    first_rows = f.drop_duplicates("chain_id").set_index("chain_id")
    f["first_published_at"] = f["chain_id"].map(first_rows["published_at"])
    f["first_receipt_date"] = f["chain_id"].map(first_rows["receipt_date"]).dt.strftime("%Y-%m-%d")
    return f.drop(columns=["receipt_date"])


# ── 3. 깜짝 판정: 공시 전에 나온 시장 예상치(영업이익) ───────────────────
@dataclass(frozen=True)
class SurpriseBands:
    in_line: float = 5.0  # |차이| ≤ 5% → 부합
    big: float = 10.0  # |차이| > 10% → 깜짝(서프라이즈 · 쇼크)


DEFAULT_BANDS = SurpriseBands()


def surprise_label(pct: float | None, bands: SurpriseBands = DEFAULT_BANDS) -> str | None:
    if pct is None or pd.isna(pct):
        return None
    if abs(pct) <= bands.in_line:
        return "부합"
    if pct > bands.big:
        return "어닝 서프라이즈"
    if pct < -bands.big:
        return "어닝 쇼크"
    return "상회" if pct > 0 else "하회"


def attach_consensus(filings: pd.DataFrame, metrics: pd.DataFrame, consensus: pd.DataFrame) -> pd.DataFrame:
    """묶음의 첫 공시에 영업이익 시장 예상치(첫 공시보다 먼저 나온 기사 중 가장 최근)와 차이를 붙인다.

    - 분기 공시는 그 분기 예상치(Q1~Q4)와 당해 영업이익을, 연간 공시는 연간 예상치(FY)와 비교한다.
    - 연결 공시가 있는 회사 · 기간은 연결 공시에만 붙인다(예상치는 보통 연결 기준이다).
    - 깜짝 여부는 시장이 처음 숫자를 본 순간의 일이라 묶음의 첫 공시에만 판정한다. 같은 분기의 두 번째 잠정
      공시나 정정공시 뒤에 나온 기사는 이미 발표된 숫자를 '예상치' 처럼 적는 경우가 있어(예: "10조4000억원을
      발표하자") 기준 시각은 언제나 묶음의 최초 공개 시각이다(규칙 5).
    """
    c = consensus[consensus["status"].isin(CONSENSUS_OK)].copy()
    c["consensus_krw"] = c["op_consensus_eok"] * 1e8
    op = metrics[(metrics["metric"] == "OPERATING_INCOME") & (metrics["span"] == "PERIOD")]
    op = op[["rcept_no", "current_krw"]].rename(columns={"current_krw": "op_actual_krw"})
    f = filings.merge(op, on="rcept_no", how="left")
    f["consensus_period"] = np.where(f["period_key"].str.endswith("FY"), "FY",
                                     f["period_key"].str.extract(r"(Q[1-4])$", expand=False))
    f["consensus_year"] = pd.to_numeric(f["period_key"].str[:4], errors="coerce")
    has_cfs = f.groupby(["corp_code", "period_key"])["statement_scope"].transform(
        lambda s: (s == "CONSOLIDATED").any())
    eligible = ((f["statement_scope"] == "CONSOLIDATED") | ~has_cfs) & (f["version_no"] == 1)
    # 기준 시각: 묶음의 최초 공개 시각. KIND 분 단위가 없으면 첫 접수일 0시(그날 아침 기사도 쓰지 않는 보수적인 쪽)
    cutoff = pd.to_datetime(f["first_published_at"]).fillna(pd.to_datetime(f["first_receipt_date"]))
    picked = []
    groups = {k: g.sort_values("published_kst") for k, g in c.groupby(["stock_code", "year", "period"])}
    empty = (None, None, None, None)
    for i, row in f.iterrows():
        g = groups.get((row["stock_code"], row["consensus_year"], row["consensus_period"]))
        if not eligible[i] or g is None:
            picked.append(empty)
            continue
        before = g[g["published_kst"] < cutoff[i]]
        if before.empty:
            picked.append(empty)
            continue
        last = before.iloc[-1]
        picked.append((last["consensus_krw"], str(last["published_kst"]), last["publisher"], last["sentence"]))
    (f["consensus_krw"], f["consensus_published_at"], f["consensus_source"],
     f["consensus_sentence"]) = zip(*picked, strict=True)
    f["consensus_krw"] = pd.to_numeric(f["consensus_krw"])
    f["consensus_age_days"] = (cutoff - pd.to_datetime(f["consensus_published_at"])).dt.days
    f["surprise_pct"] = ((f["op_actual_krw"] - f["consensus_krw"]) / f["consensus_krw"].abs() * 100).round(2)
    # 판정을 보류하는 예상치: 분기의 절반(45일)보다 오래됐거나, 기사 문장은 손실을 말하는데 숫자가 양수
    stale = f["consensus_age_days"] > CONSENSUS_MAX_AGE_DAYS
    sign_suspect = f["consensus_sentence"].fillna("").str.contains(SIGN_WORDS) & (f["consensus_krw"] > 0)
    f["surprise_hold_reason"] = np.select([stale, sign_suspect], ["예상치가 45일보다 오래됨", "예상치 부호 의심"],
                                          None)
    f["surprise_label"] = f["surprise_pct"].map(surprise_label).where(f["surprise_hold_reason"].isna())
    return f.drop(columns=["op_actual_krw", "consensus_period", "consensus_year"])


# ── 4. 실적 행: 원 단위 · 증감 · 흑자/적자 ──────────────────────────────
def turn_label(cur: float | None, prior: float | None) -> str | None:
    if cur is None or prior is None or pd.isna(cur) or pd.isna(prior):
        return None
    if prior < 0 <= cur:
        return "흑자전환"
    if prior >= 0 > cur:
        return "적자전환"
    if prior < 0 and cur < 0:
        return "적자지속"
    return None


def build_metrics(metrics: pd.DataFrame, filings: pd.DataFrame) -> pd.DataFrame:
    m = metrics.merge(filings[["rcept_no", "unit_multiplier"]], on="rcept_no")
    m = m[m["evidence"].fillna("") != ""].copy()  # 규칙 1: 근거 없는 숫자는 내보내지 않는다
    mult = m["unit_multiplier"]
    for col in ("current", "prior_period", "prior_year"):
        fixed = m[col].where(m["fix_field"] != col, m["fix_value"])  # 원문 오기로 표시된 칸은 고친 값
        m[f"{col}_krw"] = fixed * mult
    for name, ref in (("yoy", "prior_year_krw"), ("qoq", "prior_period_krw")):
        valid = m[ref].notna() & (m[ref] > 0) & m["current_krw"].notna()
        m[f"{name}_pct_calc"] = ((m["current_krw"] - m[ref]) / m[ref] * 100).where(valid).round(2)
        m[f"{name}_turn_calc"] = [turn_label(c, p) for c, p in zip(m["current_krw"], m[ref], strict=False)]
    return m.drop(columns=["unit_multiplier"])


# ── 전체 ────────────────────────────────────────────────────────────────
def build_store(parsed_filings: pd.DataFrame, parsed_metrics: pd.DataFrame, root: Path = DATA_ROOT,
                min_year: int = 2008) -> tuple[pd.DataFrame, pd.DataFrame]:
    inv = load_inventory(root)
    cols = ["rcept_no", "corp_code", "stock_code", "corp_name", "report_nm", "rcept_dt", "correction_kind",
            "statement_scope", "is_subsidiary_major_matter"]
    f = parsed_filings[(parsed_filings["status"] == "OK") & (parsed_filings["receipt_year"] >= min_year)]
    f = f.drop(columns=["corp_name", "correction_kind", "statement_scope"], errors="ignore")
    f = f.merge(inv[cols], on="rcept_no")
    f["document_parsed"] = True
    # 정정공시는 목록에만 있고 원문이 모이지 않았다 → 숫자 없는 버전으로 묶음에 넣어 '정정이 있었다' 는 사실은 남긴다
    corr = inv[(inv["correction_kind"] != "INITIAL") & (inv["receipt_year"] >= min_year)
               & ~inv["rcept_no"].isin(f["rcept_no"])][cols + ["receipt_year"]].copy()
    corr["status"] = "NO_DOCUMENT"
    corr["document_parsed"] = False
    corr["period_kind"] = "UNKNOWN"
    f = pd.concat([f, corr], ignore_index=True)
    f = attach_publish_time(f, pd.read_parquet(root / KIND_DISCLOSURES))
    f = build_versions(f)
    m = build_metrics(parsed_metrics[parsed_metrics["rcept_no"].isin(f["rcept_no"])], f)
    f = attach_consensus(f, m, pd.read_parquet(root / CONSENSUS))
    return f.reset_index(drop=True), m.reset_index(drop=True)


def validate(filings: pd.DataFrame, metrics: pd.DataFrame) -> dict[str, int]:
    """불변조건 위반 수. 모두 0 이어야 한다."""
    latest_per_chain = filings.groupby("chain_id")["is_latest"].sum()
    first = filings.groupby("chain_id")["first_published_at"].nunique(dropna=True)
    pub = pd.to_datetime(filings["first_published_at"]).fillna(pd.to_datetime(filings["first_receipt_date"]))
    cons = pd.to_datetime(filings["consensus_published_at"])
    return {
        "metric_without_evidence": int((metrics["evidence"].fillna("") == "").sum()),
        "chain_without_exactly_one_latest": int((latest_per_chain != 1).sum()),
        "chain_with_changing_first_publish_time": int((first > 1).sum()),
        "version_published_before_first": int(
            (pd.to_datetime(filings["published_at"]) < pd.to_datetime(filings["first_published_at"])).sum()),
        "consensus_not_before_first_publication": int((cons.notna() & (cons >= pub)).sum()),
        "surprise_on_later_version": int((filings["surprise_label"].notna() & (filings["version_no"] > 1)).sum()),
        # 원문에 숫자가 있는데 원 단위 값이 비었다 = 단위를 모르는 금액이 섞였다
        "amount_without_krw": int((metrics["current"].notna() & metrics["current_krw"].isna()).sum()),
    }
