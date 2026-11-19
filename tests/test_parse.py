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
    column_role,
    find_unit_override,
    normalize_annual,
    parse_amount,
    parse_change_and_pct,
    parse_html,
    period_from_label,
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
     ("당기실적 (제59기)", "ANNUAL", None, 4, None), ("당기실적 ('23.4Q )", "QUARTER", 2023, 4, None)],
)
def test_period_from_label(label, kind, year, quarter, month):
    p = period_from_label(label)
    assert (p.kind, p.year, p.quarter, p.month) == (kind, year, quarter, month)


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
