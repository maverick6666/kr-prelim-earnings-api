"""정답 없이 하는 검증: 공시 안에 함께 적힌 증감률 · 증감액과, 뽑은 숫자로 다시 계산한 값을 비교한다.

잠정실적 표에는 당기 · 전기 · 전년동기 숫자와 '증감율(%)'(옛 양식은 '증감액(증감율)')이 같이 적힌다.
뽑은 두 숫자로 다시 계산한 증감률이 공시에 적힌 증감률과 맞으면, 두 숫자를 맞게 읽었다는 강한 증거다.

반올림 범위로 비교한다.
- 표에 적힌 금액은 표시 단위로 반올림된 값이라, 실제 값은 ±0.5 표시 단위 안에 있다.
- 적힌 증감률도 소수 자릿수만큼 반올림된 값이다.
- 두 범위가 겹치면 PASS, 겹치지 않지만 상대 오차 1% 또는 0.1%p 안이면 NEAR, 그 밖은 FAIL.

한계: 비율 검사라서 단위(억원 · 백만원)가 틀려도 통과한다. 단위는 다른 방법(정답 대조 · 확정 재무와 대조)으로 본다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

from .parse import Amount, MetricRow, squash

PASS, CONVENTION, NEAR, FAIL, NA = "PASS", "PASS_CONVENTION", "NEAR", "FAIL", "NA"
SOURCE_TYPO = "SOURCE_TYPO"


@dataclass
class RowCheck:
    yoy_pct: str = NA
    qoq_pct: str = NA
    yoy_change: str = NA
    qoq_change: str = NA
    # 원문 오기로 보이는 칸을 고쳐서 맞았을 때: (칸 이름, 원문 글자, 고친 값)
    fix: tuple[str, str, float] | None = None

    @property
    def verdict(self) -> str:
        if self.fix is not None:
            return SOURCE_TYPO
        results = [self.yoy_pct, self.qoq_pct, self.yoy_change, self.qoq_change]
        for level in (FAIL, PASS, CONVENTION, NEAR):
            if level in results:
                return level
        return NA


def _half(a: Amount) -> float:
    return 0.5 * 10 ** (-a.decimals)


def growth_interval(cur: Amount, prior: Amount, absolute_denominator: bool) -> tuple[float, float] | None:
    """반올림 범위 안의 당기 · 비교 값 조합으로 가능한 증감률(%)의 최소 · 최대."""
    hc, hp = _half(cur), _half(prior)
    values = []
    for c in (cur.value - hc, cur.value + hc):
        for p in (prior.value - hp, prior.value + hp):
            if p == 0 or (p > 0) != (prior.value > 0):
                return None  # 비교 값이 0 근처면 증감률이 정의되지 않는다
            denom = abs(p) if absolute_denominator else p
            values.append((c - p) / denom * 100)
    return min(values), max(values)


def check_pct(cur: Amount | None, prior: Amount | None, reported: Amount | None) -> str:
    if not cur or not prior or not reported or None in (cur.value, prior.value, reported.value):
        return NA
    if prior.value == 0:
        return NA
    r, hr = reported.value, _half(reported)
    step = 2 * hr  # 마지막 자리 한 칸
    near = convention = False
    # 비교 값이 음수(손실)면 회사마다 분모를 그대로 쓰기도, 절댓값으로 쓰기도 한다. 둘 다 본다.
    for absolute in (False, True) if prior.value < 0 else (False,):
        interval = growth_interval(cur, prior, absolute)
        if interval is None:
            return NA
        lo, hi = interval
        if lo - hr <= r <= hi + hr:
            return PASS
        # 관행 차이: (1) 감소를 부호 없이 적음 (2) 반올림 대신 버림 (3) 둘 다
        for target in (r, -r):
            if target != r and lo - hr <= target <= hi + hr:
                convention = True
            trunc_lo, trunc_hi = (target, target + step) if target >= 0 else (target - step, target)
            if hi >= trunc_lo and lo <= trunc_hi:
                convention = True
        gap = abs((lo + hi) / 2 - r)
        if gap <= 0.1 or gap <= 0.01 * abs((lo + hi) / 2):
            near = True
    if convention:
        return CONVENTION
    return NEAR if near else FAIL


def check_change(cur: Amount | None, prior: Amount | None, reported: Amount | None) -> str:
    if not cur or not prior or not reported or None in (cur.value, prior.value, reported.value):
        return NA
    diff = cur.value - prior.value
    tolerance = _half(cur) + _half(prior) + _half(reported)
    if abs(diff - reported.value) <= tolerance + 1e-9:
        return PASS
    if abs(abs(diff) - abs(reported.value)) <= tolerance + 1e-9:
        return CONVENTION  # 부호만 다르게 적은 경우(감소를 양수로 적음)
    return FAIL


def _checks(row: MetricRow) -> RowCheck:
    return RowCheck(
        yoy_pct=check_pct(row.current, row.prior_year, row.yoy_pct),
        qoq_pct=check_pct(row.current, row.prior_period, row.qoq_pct),
        yoy_change=check_change(row.current, row.prior_year, row.yoy_change),
        qoq_change=check_change(row.current, row.prior_period, row.qoq_change),
    )


# ── 원문 오기 후보 ───────────────────────────────────────────────────────
_DOT_AS_THOUSANDS = re.compile(r"^[+\-−△]?\d{1,3}(\.\d{3})+$")  # 17.227 → 17,227
_BAD_GROUPING = re.compile(r",(?!\d{3}(?:[,.]|$))")  # 쉼표 뒤가 정확히 세 자리가 아님: 18,4931
_COMMA_AS_DECIMAL = re.compile(r"^[+\-−△]?\d+,\d{1,2}$")  # 93,1 → 93.1


def typo_candidates(a: Amount | None, is_pct: bool) -> list[Amount]:
    if a is None or a.value is None:
        return []
    raw = squash(a.raw).replace("%", "")
    sign = -1.0 if a.value < 0 else 1.0
    digits = re.sub(r"\D", "", raw)
    out = []
    if is_pct:
        if _COMMA_AS_DECIMAL.match(raw):
            whole, frac = re.sub(r"[+\-−△]", "", raw).split(",")
            out.append(Amount(a.raw, sign * float(f"{whole}.{frac}"), len(frac)))
    else:
        if _DOT_AS_THOUSANDS.match(raw) or (_BAD_GROUPING.search(raw) and "." not in raw):
            out.append(Amount(a.raw, sign * float(digits), 0))
    return out


def check_row(row: MetricRow) -> RowCheck:
    """증감률 · 증감액으로 검증하고, 실패하면 흔한 원문 오기를 고쳐 다시 맞춰 본다.

    고친 값으로 모든 검사가 통과하면 SOURCE_TYPO(원문 오기 의심)로 표시하고 고친 값을 남긴다.
    원래 값은 그대로 두고(덮어쓰지 않음), 고친 값은 후보로만 제시한다.
    """
    base = _checks(row)
    if base.verdict != FAIL:
        return base
    fields = ("current", "prior_year", "prior_period", "yoy_pct", "qoq_pct", "yoy_change", "qoq_change")
    for name in fields:
        original = getattr(row, name)
        for candidate in typo_candidates(original, is_pct=name.endswith("_pct")):
            trial = _checks(replace(row, **{name: candidate}))
            if trial.verdict in (PASS, CONVENTION):
                trial.fix = (name, original.raw, candidate.value)
                return trial
    return base
