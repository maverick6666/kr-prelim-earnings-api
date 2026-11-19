"""API 대표 시나리오: 정상 조회 · 아직 공시 안 한 기간 · 원문을 못 읽은 정정공시, 그리고 입력 오류."""

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from prelim.api import Store, create_app, not_yet, period_end, period_order

NA = float("nan")


def filing(rcept_no, chain_id, version_no, version_kind, *, parsed=True, period="2024Q2", stock="005930",
           published="2024-07-05 08:44", correction="INITIAL", consensus=NA, label=None, pct=NA):
    return {
        "rcept_no": rcept_no, "chain_id": chain_id, "version_no": version_no, "version_kind": version_kind,
        "correction_kind": correction, "report_nm": "연결재무제표기준영업(잠정)실적(공정공시)",
        "published_at": published, "rcept_dt": rcept_no[:8], "document_parsed": parsed, "corp_name": "삼성전자",
        "corp_code": "00126380", "stock_code": stock, "statement_scope": "CONSOLIDATED", "period_key": period,
        "period_kind": "QUARTER", "versions_in_chain": 3, "first_published_at": "2024-07-05 08:44",
        "first_receipt_date": "2024-07-05", "time_cat": "장 전", "consensus_krw": consensus,
        "surprise_label": label, "surprise_pct": pct, "surprise_hold_reason": None,
        "consensus_published_at": "2024-06-21 15:58:00" if label else None, "consensus_age_days": 13 if label else NA,
        "consensus_source": "newsway.co.kr" if label else None, "consensus_sentence": "컨센서스(8조2000억원)",
        "unit_raw": "억원", "supersedes": None, "is_latest": version_kind == "correction", "time_match": "exact",
        "notes": None, "is_subsidiary_major_matter": False,
    }


def metric(rcept_no, name, current, prior_year, yoy_pct):
    return {
        "rcept_no": rcept_no, "metric": name, "metric_label": "영업이익", "span": "PERIOD",
        "current_krw": current, "prior_period_krw": NA, "prior_year_krw": prior_year, "yoy_pct": yoy_pct,
        "yoy_pct_calc": round((current - prior_year) / prior_year * 100, 2), "qoq_pct": NA, "qoq_pct_calc": NA,
        "yoy_turn": None, "yoy_turn_calc": None, "chk": "PASS", "chk_yoy_pct": "PASS", "chk_qoq_pct": "NA",
        "chk_yoy_change": "NA", "chk_qoq_change": "NA", "fix_field": None, "fix_raw": None, "fix_value": NA,
        "evidence": "영업이익 | 당해실적 | 104,000 | ...",
    }


@pytest.fixture()
def client():
    filings = pd.DataFrame([
        filing("20240705800058", "20240705800058", 1, "initial", consensus=8.2e12, label="어닝 서프라이즈", pct=26.8),
        filing("20240731800039", "20240705800058", 2, "repeat", published="2024-07-31 08:40"),
        filing("20240731800047", "20240705800058", 3, "correction", parsed=False, correction="CONTENT_CORRECTION",
               published="2024-07-31 09:03"),
        filing("20240405800040", "20240405800040", 1, "initial", period="2024Q1", published="2024-04-05 08:34"),
    ])
    filings.loc[3, "is_latest"] = True
    metrics = pd.DataFrame([
        metric("20240705800058", "OPERATING_INCOME", 10.4e12, 0.67e12, 1452.24),
        metric("20240731800039", "OPERATING_INCOME", 10.44e12, 0.67e12, 1458.21),
        metric("20240405800040", "OPERATING_INCOME", 6.6e12, 0.64e12, 931.25),
    ])
    return TestClient(create_app(Store(filings, metrics)))


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["filings"] == 4


def test_filing_detail_has_numbers_evidence_versions_and_surprise(client):
    body = client.get("/v1/filings/20240705800058").json()
    assert body["headline"]["operating_income"]["current_krw"] == 10.4e12
    assert body["metrics"][0]["evidence"].startswith("영업이익")
    assert [v["version_kind"] for v in body["versions"]] == ["initial", "repeat", "correction"]
    assert body["surprise"]["surprise_label"] == "어닝 서프라이즈"
    assert body["surprise"]["gap_krw"] == 10.4e12 - 8.2e12  # 실제 − 예상(원)
    assert body["dart_url"].endswith("20240705800058")


def test_period_with_unparsed_correction_warns_instead_of_pretending(client):
    body = client.get("/v1/companies/005930/earnings/2024Q2").json()
    assert body["status"] == "CORRECTION_NOT_REFLECTED"
    assert body["current"]["rcept_no"] == "20240731800039"  # 원문을 읽은 마지막 버전
    assert "20240731800047" in body["warning"]
    # 깜짝 판정은 첫 공시(7/5) 기준으로 함께 보인다
    assert body["surprise_at_first_release"]["surprise_label"] == "어닝 서프라이즈"
    assert body["current"]["surprise"] is None


def test_period_not_yet_disclosed_is_not_an_error(client):
    r = client.get("/v1/companies/005930/earnings/2026Q4")
    assert r.status_code == 200
    assert r.json() == {"status": "NOT_YET", "stock_code": "005930", "period": "2026Q4",
                        "latest_available_period": "2024Q2", "data_end": "2024-07-31"}


def test_past_period_without_filing_is_not_called_not_yet(client):
    body = client.get("/v1/companies/005930/earnings/2023Q4").json()
    assert body["status"] == "NOT_FOUND" and body["latest_available_period"] == "2024Q2"


def test_period_order_follows_period_end_not_text():
    keys = ["2024Q2", "2024M12", "2024FY", "2024Q4", "2024M05", "2023FY"]
    assert sorted(keys, key=period_order) == ["2023FY", "2024M05", "2024Q2", "2024M12", "2024Q4", "2024FY"]


def test_company_earnings_lists_latest_periods_first(client):
    body = client.get("/v1/companies/005930/earnings").json()
    assert [p["period_key"] for p in body["periods"]] == ["2024Q2", "2024Q1"]
    assert body["periods"][0]["correction_not_reflected"] is True


def test_feed_is_ordered_by_publish_time_and_skips_unparsed(client):
    items = client.get("/v1/filings?limit=10").json()["items"]
    assert [i["rcept_no"] for i in items] == ["20240731800039", "20240705800058", "20240405800040"]


def test_search_and_demo_page(client):
    items = client.get("/v1/search?q=삼성").json()["items"]
    assert items == [{"stock_code": "005930", "corp_name": "삼성전자", "latest_rcept_no": "20240731800039"}]
    page = client.get("/")
    assert page.status_code == 200 and "잠정실적 속보" in page.text


@pytest.mark.parametrize(("path", "code"), [
    ("/v1/companies/005930/earnings/2024Q5", 422),
    ("/v1/companies/12/earnings", 422),
    ("/v1/companies/999999/earnings", 404),
    ("/v1/filings/123", 422),
    ("/v1/filings/20991231000000", 404),
    ("/v1/filings?date=2024/07/05", 422),
])
def test_bad_inputs(client, path, code):
    assert client.get(path).status_code == code


def test_not_yet_depends_on_data_end_not_on_the_company():
    from datetime import date

    assert period_end("2024Q2") == date(2024, 6, 30) and period_end("2016M02") == date(2016, 2, 29)
    assert period_end("2023FY") == date(2023, 12, 31)
    data_end = date(2024, 7, 31)
    assert not_yet("2024Q2", data_end) and not_yet("2024Q3", data_end)  # 끝난 지 두 달이 안 됐거나 아직 안 끝남
    assert not not_yet("2024Q1", data_end)  # 지난 기간: 공시가 없으면 '아직' 이 아니라 '없음'


def test_company_that_stopped_filing_gets_not_found_with_reason(client):
    body = client.get("/v1/companies/005930/earnings/2024Q1").json()
    assert body["status"] == "OK"  # 있는 기간은 그대로
    stale = client.get("/v1/companies/005930/earnings/2023Q3").json()
    assert stale["status"] == "NOT_FOUND" and "찾지 못했다" in stale["note"]
