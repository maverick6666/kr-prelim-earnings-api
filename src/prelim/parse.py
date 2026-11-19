"""잠정실적 공시 원문(HTML) → 구조화된 숫자.

방식
1. 원문의 표를 rowspan · colspan 까지 펼친 격자(grid)로 만든다.
2. 실적 항목(매출액 · 영업이익 · …) 행이 가장 많은 표를 실적 표로 고른다.
3. 실적 행 위쪽 머리글을 열마다 이어 붙여 열의 역할(당기 · 전기 · 전년동기 · 증감률 · 흑자/적자 전환)을 정한다.
4. 행마다 항목 · 당해/누계 · 값을 읽고, 표 앞의 '단위' 로 원(KRW) 단위로 바꾼다.

숫자는 표시 단위 값(display)과 원 단위 값(krw)을 함께 남긴다. 검증(checks.py)은 표시 단위 값으로
반올림 범위를 계산하기 때문이다. 근거로 행 전체 글자(evidence)를 남긴다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from lxml import html as lxml_html

# ── 항목 이름 → 표준 코드 ────────────────────────────────────────────────
# 순서가 중요하다: '지배기업 소유주지분 순이익' 을 '순이익' 보다 먼저, '법인세…' 를 '당기순이익' 보다 먼저 본다.
METRIC_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("NET_INCOME_CONTROLLING", re.compile(r"지배.*(소유주|주주).*순이익|지배주주순이익")),
    ("PRETAX_INCOME", re.compile(r"법인세.*(차감전|비용차감전)|세전.*이익|계속사업이익")),
    ("OPERATING_INCOME", re.compile(r"^영업이익|^영업손익|^영업이익\(손실\)")),
    ("NET_INCOME", re.compile(r"^(당기|분기|반기)?순이익|^(당기|분기|반기)순손익|^당기순이익\(손실\)")),
    ("REVENUE", re.compile(r"^매출액|^매출$|^영업수익|^순영업수익|^수익\(매출액\)|^매출수익")),
]

SPAN_PERIOD = "PERIOD"  # 당해(그 분기 · 그 기간)
SPAN_CUMULATIVE = "CUMULATIVE"  # 누계(그해 처음부터)

UNIT_MULTIPLIERS = {
    "원": 1,
    "천원": 1_000,
    "천": 1_000,
    "백만원": 1_000_000,
    "백만": 1_000_000,
    "십억원": 1_000_000_000,
    "억원": 100_000_000,
    "억": 100_000_000,
    "조원": 1_000_000_000_000,
}
FOREIGN_UNIT = re.compile(r"(천|백만|십억)?\s*(달러|USD|US\$|엔|위안|유로)")

TURN_WORDS = ("흑자전환", "적자전환", "적자지속", "흑자지속", "적자확대", "적자축소", "흑자확대", "흑자축소")


@dataclass
class Amount:
    """표시 단위 숫자 하나. raw 는 원문 칸 글자 그대로, decimals 는 소수 자릿수(반올림 검증용)."""

    raw: str
    value: float | None
    decimals: int = 0


@dataclass
class MetricRow:
    metric: str
    metric_label: str
    span: str
    current: Amount | None = None
    prior_period: Amount | None = None
    prior_year: Amount | None = None
    qoq_change: Amount | None = None
    qoq_pct: Amount | None = None
    qoq_turn: str | None = None
    yoy_change: Amount | None = None
    yoy_pct: Amount | None = None
    yoy_turn: str | None = None
    evidence: str = ""

    def krw(self, amount: Amount | None, multiplier: int | None) -> float | None:
        if amount is None or amount.value is None or multiplier is None:
            return None
        return amount.value * multiplier


@dataclass
class ParsedFiling:
    rcept_no: str
    status: str  # OK | NO_RESULTS_TABLE | NO_UNIT | FOREIGN_CURRENCY | UNREADABLE | CORRUPTED_SOURCE
    encoding: str = ""
    unit_raw: str | None = None
    unit_multiplier: int | None = None
    column_roles: dict[int, str] = field(default_factory=dict)
    column_labels: dict[int, str] = field(default_factory=dict)
    period_labels: dict[str, str] = field(default_factory=dict)
    period_current: tuple[str, str] | None = None
    period: PeriodInfo = field(default_factory=lambda: PeriodInfo())
    results_tables: int = 0
    metrics: list[MetricRow] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


# ── 글자 · 숫자 ─────────────────────────────────────────────────────────
_WS = re.compile(r"\s+")


def clean(text: str) -> str:
    return _WS.sub(" ", text.replace("\xa0", " ")).strip()


def squash(text: str) -> str:
    """비교용: 공백을 모두 지운 글자."""
    return _WS.sub("", text)


_NUMBER = re.compile(r"^(?P<sign>[+\-−△▲▼]?)(?P<num>\d[\d,]*(?:\.\d+)?|\.\d+)$")


def parse_amount(text: str) -> Amount | None:
    """'1,338,734' · '-695' · '△695' · '(695)' · '+8.02' · '-' 를 읽는다. 숫자가 아니면 None."""
    raw = clean(text)
    s = squash(raw).replace("%", "")
    if s in ("", "-", "－", "―", "N/A", "n/a", "해당없음", "해당사항없음"):
        return Amount(raw=raw, value=None)
    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative, s = True, s[1:-1]
    m = _NUMBER.match(s)
    if not m:
        return None
    num = m.group("num").replace(",", "")
    if m.group("sign") in ("-", "−", "△", "▼"):
        negative = True
    value = float(num)
    decimals = len(num.split(".")[1]) if "." in num else 0
    return Amount(raw=raw, value=-value if negative else value, decimals=decimals)


_COMBINED = re.compile(r"^(?P<a>[^()]*)\((?P<b>[^()]*)\)$")
_SPACED_PAIR = re.compile(r"^(?P<a>[+\-−△▲▼]?[\d,]+(?:\.\d+)?)\s+(?P<b>[+\-−△▲▼]?[\d,]*\.?\d+\s*%?)$")


def parse_change_and_pct(text: str) -> tuple[Amount | None, Amount | None, str | None]:
    """옛 양식의 '증감액(증감율)' 한 칸.

    '549 (20.15)' · '-357(-23.69)' · '1,212 (흑자전환)' 처럼 괄호로 묶거나, '1,041 44.8%' 처럼 띄어 쓴다.
    숫자가 하나뿐이면 '%' 가 붙었으면 증감률, 아니면 증감액으로 둔다(이후 숫자 맞춤으로 다시 판단: resolve_single).
    """
    raw = clean(text)
    spaced = _SPACED_PAIR.match(raw)
    if spaced:
        return parse_amount(spaced.group("a")), parse_amount(spaced.group("b")), None
    s = squash(raw)
    m = _COMBINED.match(s)
    if not m:
        amt = parse_amount(s)
        if amt is not None and "%" in s:
            return None, amt, turn_word(s)
        return amt, None, turn_word(s)
    change = parse_amount(m.group("a"))
    inner = m.group("b")
    return change, parse_amount(inner), turn_word(inner)


def resolve_single(cur: Amount | None, ref: Amount | None, change: Amount | None,
                   pct: Amount | None) -> tuple[Amount | None, Amount | None]:
    """'증감액(증감율)' 열에 숫자가 하나만 있을 때, 증감액인지 증감률인지 숫자로 판단한다.

    당기 - 비교 값과 맞으면 증감액, (당기 - 비교 값) / 비교 값 × 100 과 맞으면 증감률.
    둘 다 아니면 처음 판단(‘%’ 유무)을 그대로 둔다.
    """
    single = change if pct is None else (pct if change is None else None)
    if single is None or single.value is None or cur is None or ref is None or None in (cur.value, ref.value):
        return change, pct
    diff = cur.value - ref.value
    as_change = abs(diff - single.value) <= 1.0
    as_pct = ref.value != 0 and abs(diff / abs(ref.value) * 100 - single.value) <= max(0.15, 0.01 * abs(single.value))
    if as_pct and not as_change:
        return None, single
    if as_change and not as_pct:
        return single, None
    return change, pct


def turn_word(text: str) -> str | None:
    s = squash(text)
    return next((w for w in TURN_WORDS if w in s), None)


def classify_metric(label: str) -> str | None:
    s = squash(label)
    for code, pattern in METRIC_PATTERNS:
        if pattern.search(s):
            return code
    return None


def classify_span(label: str) -> str | None:
    s = squash(label)
    if "누계" in s or "누적" in s:
        return SPAN_CUMULATIVE
    if "당해" in s or s in ("당기", "분기", "해당기간"):
        return SPAN_PERIOD
    return None


# ── 표 → 격자 ───────────────────────────────────────────────────────────
def _span(value: str | None) -> int:
    try:
        return max(1, min(int(str(value).strip()), 50))
    except (TypeError, ValueError):
        return 1


def table_rows(table) -> list:
    """이 표에 직접 속한 행만(안쪽에 끼운 표의 행은 빼고)."""
    rows = []
    for tr in table.iter("tr"):
        owner = next(tr.iterancestors("table"), None)
        if owner is table:
            rows.append(tr)
    return rows


def table_grid(table) -> list[list[str]]:
    """rowspan · colspan 을 펼쳐, 칸마다 원래 칸의 글자를 채운 2차원 격자."""
    grid: list[list[str]] = []
    carried: dict[tuple[int, int], str] = {}
    for r, tr in enumerate(table_rows(table)):
        cells = [c for c in tr if isinstance(c.tag, str) and c.tag.lower() in ("td", "th")]
        row: list[str] = []
        col = 0
        i = 0
        while i < len(cells) or (r, col) in carried:
            if (r, col) in carried:
                row.append(carried.pop((r, col)))
                col += 1
                continue
            cell = cells[i]
            i += 1
            text = clean(cell.text_content())
            rowspan, colspan = _span(cell.get("rowspan")), _span(cell.get("colspan"))
            for _ in range(colspan):
                row.append(text)
                for dr in range(1, rowspan):
                    carried[(r + dr, col)] = text
                col += 1
        grid.append(row)
    return grid


# ── 머리글 → 열 역할 ────────────────────────────────────────────────────
def column_role(label: str) -> str | None:
    s = squash(label)
    if not s or s in ("구분",):
        return None
    if "전년동기대비" in s or ("전년동기" in s and ("증감" in s or "흑자" in s)):
        prefix = "YOY"
    elif "전기대비" in s or ("전기" in s and ("증감" in s or "흑자" in s)):
        prefix = "QOQ"
    elif "전년동기" in s:
        return "PRIOR_YEAR"
    elif "전기" in s:
        return "PRIOR_PERIOD"
    elif "당기" in s:
        return "CURRENT"
    else:
        return None
    has_amt, has_pct, has_turn = "증감액" in s, ("증감율" in s or "증감률" in s or "%" in s), "흑자" in s or "적자" in s
    if has_amt and has_pct:
        return f"{prefix}_CHANGE_PCT"
    if has_turn and not has_pct:
        return f"{prefix}_TURN"
    if has_amt:
        return f"{prefix}_CHANGE"
    return f"{prefix}_PCT"


def _first_metric_row(grid: list[list[str]]) -> int | None:
    for r, row in enumerate(grid):
        if any(classify_metric(c) for c in row[:3]):
            return r
    return None


def _is_header_row(row: list[str]) -> bool:
    return any(squash(c).startswith("구분") for c in row[:2])


def header_start(grid: list[list[str]], start: int) -> int:
    """첫 실적 행 바로 위의 머리글 묶음이 시작하는 행.

    실적 표 위에는 안내 문구 · 실적기간 · 단위 행이 같은 표 안에 붙어 있는 경우가 많아,
    표 맨 위부터 머리글로 읽으면 열 의미가 섞인다. '구분' 으로 시작하는 연속 행만 머리글로 본다.
    """
    for r in range(start - 1, max(-1, start - 6), -1):
        if _is_header_row(grid[r]):
            while r - 1 >= 0 and _is_header_row(grid[r - 1]):
                r -= 1
            return r
    for r in range(start - 1, max(-1, start - 6), -1):
        if any("당기" in squash(c) for c in grid[r]):
            return r
    return max(0, start - 1)


def _header_labels(grid: list[list[str]], upto: int) -> dict[int, str]:
    labels: dict[int, list[str]] = {}
    for row in grid[header_start(grid, upto):upto]:
        for c, text in enumerate(row):
            if text and (not labels.get(c) or labels[c][-1] != text):
                labels.setdefault(c, []).append(text)
    return {c: " ".join(parts) for c, parts in labels.items()}


def find_results_tables(doc) -> list[tuple[list[list[str]], int]]:
    """실적 항목 행이 있는 표들(격자, 첫 실적 행 위치). 실적 행이 많은 순."""
    found = []
    for table in doc.iter("table"):
        grid = table_grid(table)
        start = _first_metric_row(grid)
        if start is None:
            continue
        n_metric = sum(1 for row in grid[start:] if any(classify_metric(c) for c in row[:3]))
        found.append((n_metric, grid, start))
    found.sort(key=lambda t: -t[0])
    return [(g, s) for _, g, s in found]


# ── 단위 · 기간 ─────────────────────────────────────────────────────────
_UNIT = re.compile(r"단위\s*[:：]?\s*\(?\s*([^\s,%)/]+)")
_DATE = r"(\d{4})[.\-년 ]\s*(\d{1,2})[.\-월 ]\s*(\d{1,2})일?"
_DATE_RANGE = re.compile(_DATE + r"\s*[~∼-]\s*" + _DATE)


def find_unit(text: str) -> str | None:
    """실적 항목이 처음 나오기 전의 가장 가까운 '단위' 를 쓴다.

    '단위당 판매가' 처럼 다른 뜻의 '단위' 도 있어서, 실제 단위 낱말(원 · 천원 · 백만원 · 억원 · 외화)로
    읽힌 것만 쓴다. 그런 것이 하나도 없으면 처음 읽힌 글자를 돌려줘 NO_UNIT 사유로 남긴다.
    """
    anchors = [i for i in (text.find("매출액"), text.find("영업이익"), text.find("영업수익")) if i >= 0]
    first_metric = min(anchors, default=len(text))
    found = list(_UNIT.finditer(text))
    valid = [m for m in found if m.group(1) in UNIT_MULTIPLIERS or FOREIGN_UNIT.search(m.group(1))]
    before = [m for m in valid if m.start() < first_metric]
    if before:
        return before[-1].group(1)
    if valid:
        return valid[0].group(1)
    return found[0].group(1) if found else None


_UNIT_NOTE = re.compile(r"(?:상기|위|본|표의?)\s*(?:금액|수치|실적)\S{0,3}\s*(조원|십억원|억원|백만원|천원)\s*단위")


def find_unit_override(text: str) -> str | None:
    """'상기 금액은 조원 단위이며' 처럼 주석에서 표 서식의 기본 단위를 덮어쓴 경우."""
    m = _UNIT_NOTE.search(text)
    return m.group(1) if m else None


@dataclass
class PeriodInfo:
    kind: str = "UNKNOWN"  # QUARTER | HALF | ANNUAL | MONTHLY | UNKNOWN
    year: int | None = None
    quarter: int | None = None
    month: int | None = None
    source: str = ""  # dates | label


def _year(y: str) -> int:
    v = int(y)
    return v + 2000 if v < 100 else v


def period_from_dates(start: str, end: str) -> PeriodInfo:
    y1, m1 = int(start[:4]), int(start[5:7])
    y2, m2 = int(end[:4]), int(end[5:7])
    months = (y2 - y1) * 12 + (m2 - m1) + 1
    if months == 1:
        return PeriodInfo("MONTHLY", y2, month=m2, source="dates")
    if months == 3 and m2 % 3 == 0:
        return PeriodInfo("QUARTER", y2, quarter=m2 // 3, source="dates")
    if months == 6 and m2 % 3 == 0:
        return PeriodInfo("HALF", y2, quarter=m2 // 3, source="dates")
    if months == 12 and m2 % 3 == 0:
        return PeriodInfo("ANNUAL", y2, quarter=4, source="dates")
    return PeriodInfo("UNKNOWN", y2, source="dates")


_LABEL_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("MONTHLY", re.compile(r"(\d{2,4})\s*년\s*(\d{1,2})\s*월")),
    ("QUARTER", re.compile(r"'?(\d{2})\s*\.\s*([1-4])\s*Q", re.I)),
    ("QUARTER", re.compile(r"(\d{4})\s*\.?\s*([1-4])\s*Q", re.I)),
    ("QUARTER", re.compile(r"(\d{2,4})\s*년\s*(?:도\s*)?([1-4])\s*(?:/\s*4)?\s*분기")),
    ("HALF", re.compile(r"(\d{2,4})\s*년\s*(?:도\s*)?(?:상반기|반기)")),
    ("ANNUAL", re.compile(r"(\d{2,4})\s*년\s*(?:도\s*)?(?:연간|년간|결산|전체)")),
]


def period_from_label(label: str) -> PeriodInfo:
    """머리글의 기간 표시: ('26.1Q) · 2016.3Q · 2016년 3분기 · 16년 1/4분기 · 16년 4월 · 2016년 상반기 · 연간."""
    m = _DATE_RANGE.search(label)
    if m:
        y1, m1, d1, y2, m2, d2 = (int(x) for x in m.groups())
        return period_from_dates(f"{y1:04d}-{m1:02d}-{d1:02d}", f"{y2:04d}-{m2:02d}-{d2:02d}")
    for kind, pattern in _LABEL_PATTERNS:
        m = pattern.search(label)
        if not m:
            continue
        year = _year(m.group(1))
        if kind == "MONTHLY":
            return PeriodInfo(kind, year, month=int(m.group(2)), source="label")
        if kind == "QUARTER":
            return PeriodInfo(kind, year, quarter=int(m.group(2)), source="label")
        return PeriodInfo(kind, year, quarter=2 if kind == "HALF" else 4, source="label")
    # 분기 · 월 표시 없이 연도나 기수만 있으면 연간 실적: '(2023년)' · '(2023)' · '(제59기)'
    core = re.sub(r"당기\s*(실적)?|[()\[\]\s]", "", label)
    m = re.fullmatch(r"'?(\d{2}|\d{4})년?도?", core)
    if m:
        return PeriodInfo("ANNUAL", _year(m.group(1)), quarter=4, source="label")
    if re.fullmatch(r"제\d+기", core):
        return PeriodInfo("ANNUAL", None, quarter=4, source="label")
    return PeriodInfo()


def normalize_annual(rows: list[MetricRow]) -> int:
    """연간 실적 공시는 '전기' 가 곧 전년이다. 전년동기 칸이 비어 있으면 전기 값을 전년 비교값으로 옮긴다.

    옮긴 행 수를 돌려준다. 4분기 공시(당기 = 4분기 3개월)는 전기가 3분기라 옮기지 않는다(연간일 때만 호출).
    """
    moved = 0
    for r in rows:
        if (r.prior_year is None or r.prior_year.value is None) and r.prior_period and r.prior_period.value is not None:
            r.prior_year, r.yoy_pct, r.yoy_change, r.yoy_turn = r.prior_period, r.qoq_pct, r.qoq_change, r.qoq_turn
            r.prior_period = r.qoq_pct = r.qoq_change = None
            r.qoq_turn = None
            moved += 1
    return moved


def find_current_period(text: str) -> tuple[str, str] | None:
    """'실적기간 당기실적 2026-01-01 ~ 2026-03-31' 이나 '해당기간 : 2010.1.1~2010.3.31' 에서 당기 기간."""
    for anchor in ("당기실적", "해당기간", "당기"):
        i = text.find(anchor)
        if i < 0:
            continue
        m = _DATE_RANGE.search(text, i, i + 80)
        if m:
            y1, m1, d1, y2, m2, d2 = (int(x) for x in m.groups())
            return f"{y1:04d}-{m1:02d}-{d1:02d}", f"{y2:04d}-{m2:02d}-{d2:02d}"
    return None


# ── 한 건 파싱 ──────────────────────────────────────────────────────────
_PROLOG = re.compile(r"^\s*<\?xml[^>]*\?>", re.I)


def parse_html(rcept_no: str, html: str, encoding: str = "") -> ParsedFiling:
    # 수집 단계에서 이미 글자가 깨진 채(U+FFFD) 저장된 원문은 항목 이름을 되살릴 수 없다
    if html.count("�") > 50:
        return ParsedFiling(rcept_no=rcept_no, status="CORRUPTED_SOURCE", encoding=encoding)
    try:
        doc = lxml_html.fromstring(_PROLOG.sub("", html) or "<html></html>")
    except Exception:  # lxml 은 여러 종류의 파싱 오류를 낸다(ParserError, ValueError 등)
        return ParsedFiling(rcept_no=rcept_no, status="UNREADABLE", encoding=encoding)

    for bad in doc.xpath("//style|//script"):
        bad.drop_tree()
    text = clean(doc.text_content())
    out = ParsedFiling(rcept_no=rcept_no, status="OK", encoding=encoding)

    tables = find_results_tables(doc)
    out.results_tables = len(tables)
    if not tables:
        out.status = "NO_RESULTS_TABLE"
        return out
    grid, start = tables[0]

    unit = find_unit(text)
    override = find_unit_override(text)
    if override and override != unit:
        out.notes.append(f"주석이 단위를 덮어씀: 표 {unit!r} → 주석 {override!r}")
        unit = override
    out.unit_raw = unit
    if unit and FOREIGN_UNIT.search(unit):
        out.status = "FOREIGN_CURRENCY"
    elif unit in UNIT_MULTIPLIERS:
        out.unit_multiplier = UNIT_MULTIPLIERS[unit]
    else:
        out.status = "NO_UNIT"
        out.notes.append(f"단위를 못 읽음: {unit!r}")

    labels = _header_labels(grid, start)
    out.column_labels = labels
    roles = {c: role for c, lab in labels.items() if (role := column_role(lab))}
    out.column_roles = roles
    for c, role in roles.items():
        if role in ("CURRENT", "PRIOR_PERIOD", "PRIOR_YEAR"):
            out.period_labels[role] = labels[c]
    out.period_current = find_current_period(text)
    if out.period_current:
        out.period = period_from_dates(*out.period_current)
    if out.period.kind == "UNKNOWN" and "CURRENT" in out.period_labels:
        out.period = period_from_label(out.period_labels["CURRENT"])

    value_cols = sorted(roles)
    first_value_col = value_cols[0] if value_cols else 2
    current_metric: tuple[str, str] | None = None
    seen: set[tuple[str, str]] = set()
    for row in grid[start:]:
        label_cells = row[:first_value_col]
        metric_label = next((c for c in label_cells if classify_metric(c)), None)
        if metric_label:
            current_metric = (classify_metric(metric_label), metric_label)  # type: ignore[assignment]
        elif not any(classify_span(c) for c in label_cells):
            current_metric = None
        if current_metric is None:
            continue
        span = next((s for c in label_cells if (s := classify_span(c))), SPAN_PERIOD)
        key = (current_metric[0], span)
        if key in seen:  # 같은 항목 · 같은 범위가 또 나오면(예: 별도 표 이어 붙임) 첫 것만
            continue
        seen.add(key)
        mrow = MetricRow(metric=current_metric[0], metric_label=current_metric[1], span=span,
                         evidence=" | ".join(row))
        for c in value_cols:
            if c >= len(row):
                continue
            cell, role = row[c], roles[c]
            if role == "CURRENT":
                mrow.current = parse_amount(cell)
            elif role == "PRIOR_PERIOD":
                mrow.prior_period = parse_amount(cell)
            elif role == "PRIOR_YEAR":
                mrow.prior_year = parse_amount(cell)
            elif role.endswith("_CHANGE_PCT"):
                change, pct, turn = parse_change_and_pct(cell)
                setattr(mrow, role[:3].lower() + "_change", change)
                setattr(mrow, role[:3].lower() + "_pct", pct)
                if turn:
                    setattr(mrow, role[:3].lower() + "_turn", turn)
            elif role.endswith("_CHANGE"):
                setattr(mrow, role[:3].lower() + "_change", parse_amount(cell))
            elif role.endswith("_PCT"):
                setattr(mrow, role[:3].lower() + "_pct", parse_amount(cell))
                if turn := turn_word(cell):
                    setattr(mrow, role[:3].lower() + "_turn", turn)
            elif role.endswith("_TURN"):
                setattr(mrow, role[:3].lower() + "_turn", turn_word(cell))
        mrow.qoq_change, mrow.qoq_pct = resolve_single(mrow.current, mrow.prior_period, mrow.qoq_change, mrow.qoq_pct)
        mrow.yoy_change, mrow.yoy_pct = resolve_single(mrow.current, mrow.prior_year, mrow.yoy_change, mrow.yoy_pct)
        out.metrics.append(mrow)

    if out.period.kind == "ANNUAL" and (moved := normalize_annual(out.metrics)):
        out.notes.append(f"연간 공시: 전기 = 전년으로 {moved}행 옮김")
    if not out.metrics and out.status == "OK":
        out.status = "NO_RESULTS_TABLE"
    return out
