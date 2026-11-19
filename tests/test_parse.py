"""세 가지 원문 양식(2026 UTF-8 · 2016 CP949 · 2010 '증감액(증감율)' 합친 칸)으로 파서와 검증을 확인한다."""

from pathlib import Path

import pytest

from prelim.checks import (
    CONVENTION,
    FAIL,
    NA,
    NEAR,
    PASS,
    SOURCE_TYPO,
    check_change,
    check_pct,
    check_row,
)
from prelim.parse import (
    Amount,
    MetricRow,
    PeriodInfo,
    column_role,
    fill_year_from_receipt,
    find_current_period,
    find_unit_override,
    normalize_annual,
    parse_amount,
    parse_change_and_pct,
    parse_html,
    period_from_label,
    period_from_month_pair,
    period_from_text,
    resolve_single,
)
from prelim.source import decode

FIXTURES = Path(__file__).parent / "fixtures"


def load(rcept_no: str):
    html, encoding = decode((FIXTURES / f"{rcept_no}.xml").read_bytes())
    return parse_html(rcept_no, html, encoding)


def metric(parsed, code, span="PERIOD"):
    return next(m for m in parsed.metrics if m.metric == code and m.span == span)


@pytest.mark.parametrize(
    ("text", "value", "decimals"),
    [("1,338,734", 1338734, 0), ("-695", -695, 0), ("△695", -695, 0), ("(1,234)", -1234, 0),
     ("+8.02", 8.02, 2), ("42.67%", 42.67, 2), ("-", None, 0)],
)
def test_parse_amount(text, value, decimals):
    a = parse_amount(text)
    assert a is not None and a.value == value and a.decimals == decimals


def test_parse_amount_rejects_words():
    assert parse_amount("흑자전환") is None


def test_combined_change_and_pct():
    change, pct, turn = parse_change_and_pct("1,212 (적자지속)")
    assert change.value == 1212 and pct is None and turn == "적자지속"
    change, pct, turn = parse_change_and_pct("-357(-23.69)")
    assert change.value == -357 and pct.value == -23.69 and turn is None


@pytest.mark.parametrize(
    ("label", "role"),
    [("당기실적 ('26.1Q)", "CURRENT"), ("전기실적 ('25.4Q)", "PRIOR_PERIOD"), ("전년동기실적", "PRIOR_YEAR"),
     ("전기대비 증감율(%)", "QOQ_PCT"), ("전기대비 흑자적자전환여부", "QOQ_TURN"),
     ("전년동기대비 증감율(%)", "YOY_PCT"), ("전년동기대비증감액(증감율)", "YOY_CHANGE_PCT"), ("구분", None)],
)
def test_column_role(label, role):
    assert column_role(label) == role


def test_2026_samsung_utf8_eok_won():
    p = load("20260430800083")
    assert p.status == "OK" and p.encoding == "utf-8"
    assert p.unit_raw == "억원" and p.unit_multiplier == 100_000_000
    assert p.period_current == ("2026-01-01", "2026-03-31")
    rev = metric(p, "REVENUE")
    assert (rev.current.value, rev.prior_period.value, rev.prior_year.value) == (1338734, 938374, 791405)
    assert (rev.qoq_pct.value, rev.yoy_pct.value) == (42.67, 69.16)
    assert check_row(rev).verdict == PASS
    op = metric(p, "OPERATING_INCOME")
    assert op.current.value == 572328 and check_row(op).yoy_pct == PASS
    assert {m.metric for m in p.metrics} == {
        "REVENUE", "OPERATING_INCOME", "PRETAX_INCOME", "NET_INCOME", "NET_INCOME_CONTROLLING"}


def test_2016_cp949():
    p = load("20161028800116")
    assert p.status == "OK" and p.encoding == "cp949" and p.unit_multiplier == 1_000_000
    rev = metric(p, "REVENUE")
    assert (rev.current.value, rev.prior_year.value, rev.yoy_pct.value) == (35572, 37173, -4.31)
    assert check_row(rev).verdict == PASS
    cum = metric(p, "NET_INCOME", "CUMULATIVE")
    assert (cum.current.value, cum.prior_year.value) == (14534, 16170)


def test_2010_combined_change_cells():
    p = load("20100506900368")
    assert p.status == "OK" and p.encoding == "cp949"
    rev = metric(p, "REVENUE")
    assert (rev.qoq_change.value, rev.qoq_pct.value) == (549, 20.15)
    assert (rev.yoy_change.value, rev.yoy_pct.value) == (2005, 158.12)
    assert check_row(rev).verdict == PASS
    op = metric(p, "OPERATING_INCOME")
    assert op.current.value == -695 and op.qoq_turn == "적자지속"
    assert check_row(op).qoq_change == PASS


@pytest.mark.parametrize(
    ("label", "kind", "year", "quarter", "month"),
    [("당기실적 ('26.1Q)", "QUARTER", 2026, 1, None), ("당기실적 (16년 4월)", "MONTHLY", 2016, None, 4),
     ("당기실적 2016년3분기", "QUARTER", 2016, 3, None), ("당기실적 (2016년 1/4분기)", "QUARTER", 2016, 1, None),
     ("당기실적 (2025년 상반기)", "HALF", 2025, 2, None), ("당기실적 (2015년 연간)", "ANNUAL", 2015, 4, None),
     ("당기실적 2025.01.01 ~ 2025.03.31", "QUARTER", 2025, 1, None), ("당기실적", "UNKNOWN", None, None, None),
     ("당기실적 (2023년)", "ANNUAL", 2023, 4, None), ("당기실적 (2023)", "ANNUAL", 2023, 4, None),
     ("당기실적 (제59기)", "ANNUAL", None, 4, None), ("당기실적 ('23.4Q )", "QUARTER", 2023, 4, None),
     # 실제 공시에서 놓치던 표기들
     ("당기실적 ('21.1분기)", "QUARTER", 2021, 1, None), ("당기실적 (2016년 3Q)", "QUARTER", 2016, 3, None),
     ("당기실적 (17년3Q)", "QUARTER", 2017, 3, None), ("당기실적 ('13 3Q)", "QUARTER", 2013, 3, None),
     ("당기실적 (1Q24)", "QUARTER", 2024, 1, None), ("당기실적 ('25.Q1)", "QUARTER", 2025, 1, None),
     ("당기실적 ('08.2/4 )", "QUARTER", 2008, 2, None), ("당기실적 (091Q)", "QUARTER", 2009, 1, None),
     ("당기실적 (20193Q)", "QUARTER", 2019, 3, None), ("당기실적 (18.6월)", "MONTHLY", 2018, None, 6),
     ("당기실적 (09.07.01~09.09.30)", "QUARTER", 2009, 3, None),
     ("당기실적(2006.01.01~03.31)", "QUARTER", 2006, 1, None),
     ("당기실적 (`08.1.1~`08.3.31 )", "QUARTER", 2008, 1, None),
     ("당기실적 (2009.2.1.~2009.2.28)", "MONTHLY", 2009, None, 2),
     ("당기실적 2019년 (1.1~12.31)", "ANNUAL", 2019, 4, None), ("당기실적 (21.01~21.03)", "QUARTER", 2021, 1, None),
     ("당기실적 (13년4월-6월)", "QUARTER", 2013, 2, None), ("당기실적 (2015.04~06)", "QUARTER", 2015, 2, None),
     ("당기실적 (FY2021)", "ANNUAL", 2021, 4, None), ("당기실적 (2007年)", "ANNUAL", 2007, 4, None),
     ("당기실적 ('09 상반기)", "HALF", 2009, 2, None), ("당기(2003)실적", "ANNUAL", 2003, 4, None),
     # 옛 판독은 범위의 첫 달을 월별로 읽었다
     ("당기실적(2004년10월1일~12월31일 )", "QUARTER", 2004, 4, None),
     ("당기실적 (2014년 01월~2014년 12월)", "ANNUAL", 2014, 4, None),
     # 표준 기간이 아니면 정하지 않는다
     ("당기실적 (2013년 3월~12월)", "UNKNOWN", 2013, None, None),
     # 연도 없는 분기 · 기수(연도는 parse_html 이 접수일로 채움)
     ("당기실적 (3분기)", "QUARTER", None, 3, None), ("당기실적 (2Q)", "QUARTER", None, 2, None),
     ("당기실적 제54기반기", "HALF", None, 2, None), ("당기실적 (2009.03)", "UNKNOWN", None, None, None)],
)
def test_period_from_label(label, kind, year, quarter, month):
    p = period_from_label(label)
    assert (p.kind, p.year, p.quarter, p.month) == (kind, year, quarter, month)


@pytest.mark.parametrize(
    ("text", "expected"),
    [("1. 실적내용 해당 기간 당 기 : 2012년 1월 1일부터 2012년 3월 31일까지 전 기 : 2011년 10월 1일부터",
      ("2012-01-01", "2012-03-31")),
     ("해당 기간 당 기 : 2010년 01월01일 ~ 2010년 03월31일(10년1분기) 전 기 : 2009년 10월01일 ~ 2009년 12월31일",
      ("2010-01-01", "2010-03-31")),
     # 당기 칸이 비어 있으면 뒤의 전기 기간을 당기로 읽지 않는다
     ("구분 당기실적 (-) 전기실적 2025.10.01 ~ 2025.12.31", None)],
)
def test_current_period_from_body_text(text, expected):
    assert find_current_period(text) == expected


def test_period_from_body_field_without_dates():
    p = period_from_text("1. 실적 내용 해당 기간 당 기: 2008년 1분기 전 기: 2007년 4분기")
    assert (p.kind, p.year, p.quarter, p.source) == ("QUARTER", 2008, 1, "text")
    # 쌍점 없는 설명 문장 속 연 · 월은 기간으로 읽지 않는다
    assert period_from_text("당기 실적은 2023년 3월 합병 영향으로 늘었다").kind == "UNKNOWN"


@pytest.mark.parametrize(
    ("current", "prior", "expected"),
    [("당기실적 (2009.03)", "전기실적 (2009.02)", ("MONTHLY", 2009, None, 3)),
     ("당기실적 (2009.03)", "전기실적 (2008.12)", ("QUARTER", 2009, 1, None)),
     ("당기실적 ('13.01)", "전기실적 ('12.12)", ("MONTHLY", 2013, None, 1)),
     ("당기실적 (2009.03)", None, ("UNKNOWN", None, None, None)),
     ("당기실적 (2009.03)", "전년동기 (2008.03)", ("UNKNOWN", None, None, None))],
)
def test_bare_year_month_needs_prior_label(current, prior, expected):
    p = period_from_month_pair(current, prior)
    assert (p.kind, p.year, p.quarter, p.month) == expected


@pytest.mark.parametrize(
    ("period", "receipt", "year"),
    [(PeriodInfo("QUARTER", None, quarter=3), "20231114", 2023),
     (PeriodInfo("QUARTER", None, quarter=4), "20240131", 2023),
     (PeriodInfo("MONTHLY", None, month=12), "20090115", 2008),
     (PeriodInfo("HALF", None, quarter=2), "20150728", 2015),
     (PeriodInfo("ANNUAL", None, quarter=4), "20240205", 2023),
     (PeriodInfo("ANNUAL", None, quarter=4), "20240820", None)],
)
def test_year_filled_from_receipt_date(period, receipt, year):
    filled = fill_year_from_receipt(period, receipt)
    assert filled.year == year
    assert filled.source.endswith("+receipt") == (year is not None)


def test_annual_filing_moves_prior_period_to_prior_year():
    rows = [MetricRow(metric="REVENUE", metric_label="매출액", span="PERIOD", current=parse_amount("382,911"),
                      prior_period=parse_amount("355,390"), qoq_pct=parse_amount("7.7"))]
    assert normalize_annual(rows) == 1
    r = rows[0]
    assert r.prior_year.value == 355390 and r.yoy_pct.value == 7.7 and r.prior_period is None
    assert check_row(r).yoy_pct == PASS


def test_unit_override_in_notes():
    assert find_unit_override("4. 기타 - 상기 금액은 조원 단위이며, 연결기준") == "조원"


def test_check_pct_rounding_and_failures():
    # 금액이 정수로 반올림돼 있으면(±0.5) 105/100 의 증감률은 약 3.98~6.03% 어디든 될 수 있다
    assert check_pct(Amount("", 105.0), Amount("", 100.0), Amount("", 5.4, 1)) == PASS
    a = Amount("", 105.0, 2)  # 소수 둘째 자리까지 적힌 금액(±0.005)
    b = Amount("", 100.0, 2)
    assert check_pct(a, b, Amount("", 5.0, 2)) == PASS
    assert check_pct(a, b, Amount("", 5.08, 2)) == NEAR
    assert check_pct(a, b, Amount("", 50.0, 0)) == FAIL
    assert check_pct(a, None, Amount("", 5.0, 0)) == NA


def test_check_pct_negative_prior_absolute_denominator():
    # 손실 -100 → 이익 50: 절댓값 분모면 +150%
    assert check_pct(Amount("", 50.0), Amount("", -100.0), Amount("", 150.0, 0)) == PASS


def test_check_change_sign_written_positive():
    assert check_change(Amount("", 90.0), Amount("", 100.0), Amount("", -10.0)) == PASS
    assert check_change(Amount("", 90.0), Amount("", 100.0), Amount("", 10.0)) == CONVENTION


def test_check_pct_truncation_convention():
    # 523 / 2974 = 17.59% 감소인데 '-17' 로 버림해 적은 경우
    assert check_pct(Amount("", 2451.0), Amount("", 2974.0), Amount("", -17.0, 0)) == CONVENTION


def test_spaced_change_and_pct():
    change, pct, _ = parse_change_and_pct("1,041 44.8%")
    assert change.value == 1041 and pct.value == 44.8


def test_resolve_single_number_in_combined_column():
    # '증감액(증감율)' 열인데 증감률만 적힌 경우: 48.18 은 (371563-250751)/250751 = 48.18%
    change, pct = resolve_single(Amount("", 371563.0), Amount("", 250751.0), Amount("48.18", 48.18, 2), None)
    assert change is None and pct.value == 48.18


def test_source_typo_dot_used_as_thousands_separator():
    # 원문이 매출 17,227 을 '17.227' 로 적은 경우 → 증감률이 맞지 않다가, 고치면 맞는다
    row = MetricRow(metric="REVENUE", metric_label="매출액", span="PERIOD",
                    current=parse_amount("17.227"), prior_period=parse_amount("16,445"),
                    prior_year=parse_amount("13,232"), qoq_pct=parse_amount("4.8"), yoy_pct=parse_amount("30.2"))
    chk = check_row(row)
    assert chk.verdict == SOURCE_TYPO and chk.fix == ("current", "17.227", 17227.0)


def test_format_typo_without_growth_rate_to_check():
    # 누계 행이라 증감률이 없어도, 다른 금액이 정수인 표에서 '102.834' 는 102,834 의 표기 오기로 본다
    row = MetricRow(metric="NET_INCOME", metric_label="당기순이익", span="CUMULATIVE",
                    current=parse_amount("102.834"), prior_year=parse_amount("48,519"))
    chk = check_row(row, unit_multiplier=1_000_000)
    assert chk.verdict == SOURCE_TYPO and chk.fix == ("current", "102.834", 102834.0)
    # 조원 표는 소수를 실제로 쓰므로 오기로 보지 않는다
    assert check_row(row, unit_multiplier=1_000_000_000_000).verdict == NA


def test_source_typo_comma_used_as_decimal_in_pct():
    row = MetricRow(metric="OPERATING_INCOME", metric_label="영업이익", span="PERIOD",
                    current=parse_amount("307"), prior_year=parse_amount("159"), yoy_pct=parse_amount("93,1"))
    chk = check_row(row)
    assert chk.verdict == SOURCE_TYPO and chk.fix == ("yoy_pct", "93,1", 93.1)
