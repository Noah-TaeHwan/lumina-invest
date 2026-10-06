"""숫자 주장 ↔ XBRL 재무 수치 대조(설계 D4·D5, Codex #1·#2·#7). 외부 호출 없음.

- 입력 행은 T1 적재기의 데이터 계약(xbrl_facts.json) 그대로의 dict다: {corp_code, period, fs_div∈{CFS,OFS}, account_id,
  account_nm, amount(원), rcept_no, period_start, period_end, value_kind∈{instant,duration}, cumulative, currency, unit,
  rcept_dt, is_correction, column, report_type}. report_type이 'preliminary'면 잠정, 그 밖은 확정(정기)으로 본다.
- 계정 사전: 매출액·영업이익(손실)·당기순이익(손실)·자산총계·부채총계·자본총계. 순이익은 연결 당기순이익과 지배기업
  소유주지분 순이익을 둘 다 후보로 보고, 하나라도 맞으면 일치, 둘 다 있는데 어느 것도 안 맞으면 불일치, 한쪽만 있고 안
  맞으면 판단 불가(unknown)다. 매출원가·매출총이익·유형자산·자본금, 제품·부문 매출('HBM 매출')은 대조하지 않는다.
- 금액과 기간 짝: 계정 뒤 첫 금액. 'X에서 Y로'면 뒤 금액(Y)이 주장 기간 값이고, '각각'이면 앞의 기간들과 순서대로 짝짓는다.
  금액 없이 이어진 계정 묶음은 계정 수와 금액 수가 같을 때만 순서대로 짝짓는다. 기간은 금액 앞 가장 가까운 표현.
- 기간은 scope.extract_periods로 푼 주장 기간(as_of 기준)을 시작·끝 날짜로 맞춘다: 'n분기'는 분기 단독, '상반기'·'누적'은
  1월 1일부터 누적, 재무상태표 계정은 기간 끝 시점 값. XBRL에 없는 기간(4분기 단독 등)은 unknown.
- 연결/별도는 그 계정 절(앞 금액 또는 절 경계 뒤 ~ 이 금액)에 적힌 것만 본다(다음 절로 잇지 않는다). 표시가 없으면 연결 우선, 연결이
  어긋나고 별도가 맞으면 맞음 + 'separate_only'.
- 같은 기간·계정 행이 여럿이면: 대체된 행(superseded) 제외, 확정이 있으면 확정만. 잠정은 순위 1위 하나만(정정·나중 공시가
  앞선다). 확정 행들(원 보고값·뒤 보고서의 재작성 비교값)은 모두 후보로, 하나라도 맞으면 일치 + 'restated'(값이 서로
  다를 때) 표시, 하나도 안 맞으면 대표 행(당기 칸 > 늦은 접수일 > 정정)으로 불일치를 보인다.
- 금액 비교(단위 환산·반올림·버림 일치)는 evidence.numbers.number_check를 그대로 쓴다. 손실·적자 표현은 부호를 따로 본다.
- 영업이익률은 같은 기간·같은 연결/별도의 영업이익 ÷ 매출액(부호 포함)으로 계산해 같은 규칙으로 비교한다.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from app.services.evidence.numbers import number_check, parse
from app.services.factcheck import scope
from app.services.factcheck.scope import CompanyIndex, Period

REVENUE, OPERATING, NET, OWNERS, ASSETS, LIABILITIES, EQUITY = (
    "ifrs-full_Revenue", "dart_OperatingIncomeLoss", "ifrs-full_ProfitLoss",
    "ifrs-full_ProfitLossAttributableToOwnersOfParent", "ifrs-full_Assets", "ifrs-full_Liabilities", "ifrs-full_Equity")
INSTANT_ACCOUNTS = frozenset({ASSETS, LIABILITIES, EQUITY})
DISPLAY = {REVENUE: "매출액", OPERATING: "영업이익", NET: "당기순이익", OWNERS: "지배기업 소유주지분 순이익",
           ASSETS: "자산총계", LIABILITIES: "부채총계", EQUITY: "자본총계", "margin": "영업이익률"}
# 주장 계정 → 대조 후보 계정. '순이익'은 연결 당기순이익과 지배주주 순이익 둘 다
CANDIDATES = {NET: (NET, OWNERS), OWNERS: (OWNERS,)}

_B = r"(?<![가-힣A-Za-z])"  # 토큰 첫머리('DS부문매출'·'유형자산'은 계정이 아니다)
_ACCOUNT = re.compile(
    rf"(?P<rev>{_B}(?:매출액|영업수익|매출(?!\s*(?:원가|총이익|총손실|채권|채무|비중|처|구성|이익))))"
    rf"|(?P<op>{_B}영업\s*(?:이익|손실)(?!\s*률))"
    rf"|(?P<own>{_B}지배(?:기업)?\s*(?:소유주\s*)?(?:지분\s*|주주\s*)?(?:당기)?\s*순(?:이익|손실)(?!\s*률))"
    rf"|(?P<net>{_B}(?:당기|반기|분기)?\s*순(?:이익|손실)(?!\s*률))"
    rf"|(?P<ast>{_B}(?:자산\s*총계|총\s*자산|자산(?!\s*(?:가치|운용|매각|재평가|유동화|건전성|총계))))"
    rf"|(?P<lia>{_B}(?:부채\s*총계|총\s*부채|부채(?!\s*(?:비율|총계))))"
    rf"|(?P<eq>{_B}(?:자본\s*총계|총\s*자본|자본(?!\s*(?:금|잉여금|변동|조정|적정|비율|총계|시장|지출))))"
    rf"|(?P<margin>{_B}영업\s*이익률)")
_GROUP_ACCOUNT = {"rev": REVENUE, "op": OPERATING, "own": OWNERS, "net": NET, "ast": ASSETS, "lia": LIABILITIES,
                  "eq": EQUITY, "margin": "margin"}
_MONEY = re.compile(r"-?\d[\d,]*(?:\.\d+)?(?:\s*(?:조|십억|억|천만|백만|만|천)(?:\s*\d[\d,]*(?:\.\d+)?)?)+\s*원?"
                    r"|-?\d[\d,]*(?:\.\d+)?\s*원")
_PERCENT = re.compile(r"[-−]?\d[\d,]*(?:\.\d+)?\s*%(?!p)")
# 절 경계(연결/별도 표시가 미치는 범위를 끊는다): 쉼표·세미콜론·연결 어미
_CLAUSE_BREAK = re.compile(r"[,;]|지만|는데|으며|이며|며\s|고\s")
_JOIN = re.compile(r"\s*(?:과|와|및|,|·|그리고)?\s*")  # 계정 묶음의 이음말('매출과 영업이익', '매출, 영업이익')
_FROM = re.compile(r"\s*에서")  # 'X에서 Y로'의 '에서'
_EACH = re.compile(r"각각")
# 계정 바로 앞 토큰으로 허용하는 수식어(회사 전체 값). 그 밖의 명사가 앞에 붙으면 제품·부문 값으로 보고 대조하지 않는다
_QUALIFIERS = frozenset({"연결", "별도", "개별", "총", "전체", "전사", "회사", "당사", "동사", "연간", "분기", "반기",
                         "누적", "누계", "합산", "말", "기준", "기말", "당기", "확정", "잠정", "실제", "실적", "합계",
                         "동기", "전년", "전기", "작년", "올해", "금년", "지난해", "재작년"})
_PERIOD_TOKEN = re.compile(r"\d|년|분기|반기|월|FY")
# 앞말이 이 끝으로 끝나면 계정을 꾸미는 말이 아니다(조사·연결 어미·쉼표). '이'·'가'는 명사 끝('디스플레이')과 겹쳐 뺀다
_NOT_MODIFIER_END = ("은", "는", "을", "를", "도", "에", "와", "과", "로", "고", "며", ",")
_LOSS = re.compile(r"손실|적자|마이너스")


@dataclass
class AmountClaim:
    """문장 속 계정 금액(또는 영업이익률) 주장 하나."""

    account_id: str  # 영업이익률이면 'margin'
    term: str
    value_text: str
    pos: int
    negative: bool
    value_start: int = 0  # 문장 안 금액 위치(연결/별도 절 계산용)
    value_end: int = 0
    period_idx: int | None = None  # '각각' 짝: 계정 앞 기간 표현의 순번


@dataclass
class XbrlItem:
    """주장 하나의 대조 결과. status: match / mismatch / unknown. amount는 공시 값(원, 이익률이면 %)."""

    status: str
    account_id: str
    account_nm: str
    period: str | None
    fs_div: str | None
    amount: int | float | None
    claimed: str
    rcept_no: str | None = None
    note: str | None = None  # separate_only / restated / no_period / no_fact / candidate_missing (여럿이면 쉼표)
    unit: str = "원"  # 영업이익률이면 '%'
    cumulative: bool | None = None
    is_correction: bool | None = None
    column: str | None = None
    report_type: str | None = None  # 고른 행의 보고서 종류(periodic / preliminary)
    report_nm: str | None = None  # 고른 행의 원 보고서명(가능할 때: '반기보고서 (2025.06)'·'영업(잠정)실적(공정공시)')


@dataclass
class XbrlResult:
    """문장 전체 대조 결과. status: none(계정 주장 없음) / match(전부 일치) / mismatch(하나라도 불일치) /
    partial(일부 일치, 나머지 XBRL 없음) / unknown(대조할 XBRL 없음)."""

    status: str
    items: list[XbrlItem]

    def primary(self) -> dict | None:
        """SentenceResult.xbrl에 넣을 값: 불일치가 있으면 그 공시 값, 아니면 첫 일치 값."""
        for want in ("mismatch", "match"):
            for it in self.items:
                if it.status == want:
                    return {"account_nm": it.account_nm, "period": it.period, "fs_div": it.fs_div,
                            "amount": it.amount, "unit": it.unit, "rcept_no": it.rcept_no,
                            "cumulative": it.cumulative, "is_correction": it.is_correction, "column": it.column,
                            "note": it.note}
        return None


def _company_wide(text: str, pos: int, names: CompanyIndex | None) -> bool:
    """계정 앞말이 회사 전체 값을 가리키는가: 앞말 없음, 조사·쉼표로 끝난 앞말, 기간·허용 수식어, 상장사 이름('삼성전자의').
    그 밖의 명사('HBM', 'DS부문', '메모리의')가 앞에 붙으면 제품·부문 값이다."""
    words = text[:pos].split()
    if not words:
        return True
    prev = words[-1]
    if prev.endswith(_NOT_MODIFIER_END):
        return True
    stem = prev[:-1] if prev.endswith("의") and len(prev) > 1 else prev
    if stem in _QUALIFIERS or _PERIOD_TOKEN.search(stem):
        return True
    return names is not None and names.lookup(stem) is not None


def amount_claims(text: str, names: CompanyIndex | None = None) -> list[AmountClaim]:
    """계정 언급마다 그 뒤(다음 계정 언급 전까지)의 금액(이익률이면 퍼센트)을 짝짓는다.

    - 금액 없이 이어진 계정 묶음('매출과 영업이익은 333.6조원, 43.6조원')은 묶음 계정 수와 금액 수가 같을 때만 순서대로
      짝짓고, 다르면 짝을 모르므로 대조하지 않는다.
    - 계정 하나에 '각각 A, B'면 금액마다 하나씩(period_idx = 계정 앞 기간 표현 순번).
    - 'X에서 Y로'면 Y. 그 밖은 첫 금액.
    """
    hits = list(_ACCOUNT.finditer(text))
    out: list[AmountClaim] = []
    pending: list[re.Match] = []  # 금액 없이 앞에 늘어선 계정(같은 종류끼리만 묶는다)
    for i, m in enumerate(hits):
        kind = m.lastgroup
        if kind != "margin" and not _company_wide(text, m.start(), names):
            pending = []
            continue
        end = hits[i + 1].start() if i + 1 < len(hits) else len(text)
        pattern = _PERCENT if kind == "margin" else _MONEY
        values = list(pattern.finditer(text, m.end(), end))
        if pending and not _JOIN.fullmatch(text, pending[-1].end(), m.start()):
            pending = []  # 바로 이어진 계정끼리만 묶는다
        if not values:
            pending.append(m)
            continue
        group, pending = pending + [m], []
        if any((g.lastgroup == "margin") != (kind == "margin") for g in group):
            group = [m]
        if len(group) > 1:
            if len(values) != len(group):
                continue  # 계정과 금액 수가 다르면 짝을 모른다
            picked = [(g, v, None) for g, v in zip(group, values, strict=True)]
        elif len(values) >= 2 and _EACH.search(text, m.end(), values[0].start()):
            picked = [(m, v, j) for j, v in enumerate(values)]
        elif len(values) >= 2 and _FROM.match(text, values[0].end()):
            picked = [(m, values[1], None)]
        else:
            picked = [(m, values[0], None)]
        for g, v, j in picked:
            raw = v.group(0)
            negative = bool(_LOSS.search(g.group(0))) or raw[0] in "-−" or \
                bool(_LOSS.search(text, v.end(), min(end, v.end() + 6))) or bool(_LOSS.search(text, m.end(), v.start()))
            out.append(AmountClaim(_GROUP_ACCOUNT[g.lastgroup], g.group(0), raw.lstrip("-−"), g.start(), negative,
                                   v.start(), v.end(), j))
    return out


def fs_div_hint(text: str) -> str | None:
    """글에 적힌 연결/별도. 둘 다 있거나 없으면 None. check는 문장 전체가 아니라 계정 절마다 이 함수를 쓴다."""
    cfs, ofs = "연결" in text, bool(re.search(r"별도|개별", text))
    return "CFS" if cfs and not ofs else "OFS" if ofs and not cfs else None


def _date(v) -> date | None:
    if not v:
        return None
    s = str(v).replace("-", "")
    return date(int(s[:4]), int(s[4:6]), int(s[6:8]))


def _period_matches(row: Mapping, period: Period, instant: bool) -> bool:
    end = _date(row.get("period_end"))
    if end is not None:
        if instant:
            return row.get("value_kind", "instant") == "instant" and end == period.end
        return row.get("value_kind", "duration") == "duration" and end == period.end and \
            _date(row.get("period_start")) == period.start
    if instant:  # 날짜가 없는 행: 기간 표기로만 맞춘다
        return row.get("period") == period.label
    return row.get("period") == period.label and bool(row.get("cumulative")) == period.cumulative


def _thstrm(row: Mapping) -> bool:
    return str(row.get("column") or "thstrm").startswith("thstrm")


def _rank(row: Mapping) -> tuple:
    """대표 행 순위: 확정 > 잠정, 당기 칸(thstrm) > 비교 칸, 늦은 접수일, (같은 날이면) 정정."""
    return (row.get("report_type") != "preliminary", _thstrm(row), str(row.get("rcept_dt") or ""),
            bool(row.get("is_correction")), str(row.get("rcept_no") or ""))


def candidate_rows(facts: Iterable[Mapping], corp_code: str, account_id: str, period: Period,
                   fs_div: str) -> list[dict]:
    """대조 후보 행(대표 행이 앞). 대체된 행 제외, 확정이 있으면 확정 전부(재작성 비교값 포함), 없으면 잠정 1위 하나."""
    instant = account_id in INSTANT_ACCOUNTS
    rows = [dict(r) for r in facts if r.get("corp_code") == corp_code and r.get("account_id") == account_id
            and r.get("fs_div") == fs_div and _period_matches(r, period, instant)]
    live = [r for r in rows if not r.get("superseded")]  # 대체된 행만 있으면 후보 없음(unknown)
    final = [r for r in live if r.get("report_type") != "preliminary"]
    ranked = sorted(final or live, key=_rank, reverse=True)
    return ranked if final else ranked[:1]


def select_fact(facts: Iterable[Mapping], corp_code: str, account_id: str, period: Period, fs_div: str) -> dict | None:
    """같은 회사·계정·기간·연결/별도의 대표 행 하나(candidate_rows의 첫 행)."""
    rows = candidate_rows(facts, corp_code, account_id, period, fs_div)
    return rows[0] if rows else None


def _same_amount(claim: AmountClaim, amount: int, rounding_unit: int = 1) -> bool:
    """부호가 같고 number_check로 같은 값. 공시 값이 rounding_unit(잠정실적 10^10원 등)으로 반올림돼 있으면, 그보다
    자세한 주장은 그 단위로 반올림해 맞춘다('171.499조' ↔ 171.50조)."""
    if amount and claim.negative != (amount < 0):
        return False
    if number_check(claim.value_text, str(abs(amount))):
        return True
    if rounding_unit > 1:
        nums = parse(claim.value_text)
        if len(nums) == 1 and nums[0].kind == "abs" and nums[0].step < rounding_unit:
            unit = Decimal(rounding_unit)
            return (abs(nums[0].value) / unit).to_integral_value(ROUND_HALF_UP) == Decimal(abs(amount)) / unit
    return False


_REPORT_NM = {"11011": ("사업보고서", "12"), "11012": ("반기보고서", "06"), "11013": ("분기보고서", "03"),
              "11014": ("분기보고서", "09")}


def report_name(row: Mapping) -> str | None:
    """계약 행의 원 보고서명: 정기는 reprt_code·bsns_year로 'OO보고서 (YYYY.MM)', 잠정실적은 공시 이름. 모르면 None."""
    if row.get("report_type") == "preliminary":
        return "영업(잠정)실적(공정공시)"
    got = _REPORT_NM.get(str(row.get("reprt_code") or ""))
    return f"{got[0]} ({row.get('bsns_year')}.{got[1]})" if got and row.get("bsns_year") else None


def _item(status: str, claim: AmountClaim, account_id: str, row: Mapping, period: Period, fs_div: str,
          amount, unit: str = "원", note: str | None = None) -> XbrlItem:
    nm = DISPLAY[account_id] if account_id in (OWNERS, "margin") else row.get("account_nm") or DISPLAY[account_id]
    return XbrlItem(status, account_id, nm, row.get("period") or period.label, fs_div, amount, claim.value_text,
                    row.get("rcept_no"), note, unit, row.get("cumulative"), row.get("is_correction"), row.get("column"),
                    row.get("report_type"), report_name(row))


def _compare_account(claim: AmountClaim, facts: Sequence[Mapping], corp_code: str, account_id: str, period: Period,
                     fs_div: str) -> XbrlItem | None:
    """한 계정·한 연결/별도 기준 비교. 행이 없으면 None. 후보 중 하나라도 맞으면 match."""
    rows = candidate_rows(facts, corp_code, account_id, period, fs_div)
    if not rows:
        return None
    note = "restated" if len({int(r["amount"]) for r in rows}) > 1 else None
    for r in rows:
        if _same_amount(claim, int(r["amount"]), int(r.get("rounding_unit") or 1)):
            return _item("match", claim, account_id, r, period, fs_div, int(r["amount"]), note=note)
    return _item("mismatch", claim, account_id, rows[0], period, fs_div, int(rows[0]["amount"]), note=note)


def _compare_margin(claim: AmountClaim, facts: Sequence[Mapping], corp_code: str, period: Period,
                    fs_div: str) -> XbrlItem | None:
    op = select_fact(facts, corp_code, OPERATING, period, fs_div)
    rev = select_fact(facts, corp_code, REVENUE, period, fs_div)
    if not op or not rev or not int(rev["amount"]):
        return None
    value = Decimal(int(op["amount"])) * 100 / Decimal(int(rev["amount"]))
    ok = claim.negative == (value < 0) and number_check(claim.value_text, f"{abs(value):.6f}%")
    return _item("match" if ok else "mismatch", claim, "margin", op, period, fs_div, float(round(value, 2)), "%")


def _evaluate(claim: AmountClaim, facts: Sequence[Mapping], corp_code: str, period: Period,
              fs_div: str) -> XbrlItem | None:
    """한 연결/별도 기준 결과. 순이익은 후보 계정 중 하나라도 맞으면 match, 후보가 다 있고 다 어긋나면 mismatch,
    후보가 모자라 판단할 수 없으면 unknown(candidate_missing). 행이 하나도 없으면 None."""
    if claim.account_id == "margin":
        return _compare_margin(claim, facts, corp_code, period, fs_div)
    accounts = CANDIDATES.get(claim.account_id, (claim.account_id,))
    got = [_compare_account(claim, facts, corp_code, a, period, fs_div) for a in accounts]
    present = [g for g in got if g is not None]
    if not present:
        return None
    hit = next((g for g in present if g.status == "match"), None)
    if hit:
        return hit
    if len(present) == len(accounts):
        return present[0]
    miss = present[0]
    return XbrlItem("unknown", claim.account_id, DISPLAY[claim.account_id], miss.period, fs_div, None,
                    claim.value_text, note="candidate_missing")


def _merge_note(*notes: str | None) -> str | None:
    return ",".join(n for n in notes if n) or None


def _claim_period(claim: AmountClaim, mentions: list[scope.PeriodMention]) -> Period | None:
    """'각각'이면 계정 앞 기간 표현의 같은 순번. 그 밖은 금액 앞의 가장 가까운 기간 표현('매출은 2025년 333.6조원'도
    2025), 앞에 없고 문장에 기간이 하나뿐이면 그것."""
    if claim.period_idx is not None:
        listed = [m for m in mentions if m.start < claim.pos]
        return listed[claim.period_idx].period if claim.period_idx < len(listed) else None
    before = [m for m in mentions if m.start < claim.value_start]
    if before:
        return before[-1].period
    return mentions[0].period if len(mentions) == 1 else None


def check(text: str, facts: Sequence[Mapping], *, corp_code: str, as_of: Period,
          names: CompanyIndex | None = None) -> XbrlResult:
    """문장 속 계정 금액·영업이익률 주장을 XBRL 행과 대조한다. names는 '삼성전자의 매출'처럼 회사 이름이 앞말일 때 쓴다."""
    claims = amount_claims(text, names)
    if not claims:
        return XbrlResult("none", [])
    mentions = scope.extract_periods(text, as_of)
    items: list[XbrlItem] = []
    prev_end = 0
    for c in claims:
        breaks = [b.end() for b in _CLAUSE_BREAK.finditer(text, prev_end, c.pos)]
        start = max([prev_end] + breaks)
        hint = fs_div_hint(text[start:c.value_end])  # 이 계정 절에 적힌 표시만(다음 절로 번지지 않는다)
        prev_end = c.value_end
        period = _claim_period(c, mentions)
        if period is None:
            items.append(XbrlItem("unknown", c.account_id, DISPLAY[c.account_id], None, None, None, c.value_text,
                                  note="no_period", unit="%" if c.account_id == "margin" else "원"))
            continue
        if hint:
            got = _evaluate(c, facts, corp_code, period, hint)
        else:
            cfs = _evaluate(c, facts, corp_code, period, "CFS")
            ofs = None if cfs and cfs.status == "match" else _evaluate(c, facts, corp_code, period, "OFS")
            if ofs and ofs.status == "match":
                ofs.note = _merge_note("separate_only", ofs.note)
                got = ofs
            else:
                got = next((g for g in (cfs, ofs) if g and g.status == "mismatch"), None) or cfs or ofs
        items.append(got or XbrlItem("unknown", c.account_id, DISPLAY[c.account_id], period.label, hint, None,
                                     c.value_text, note="no_fact", unit="%" if c.account_id == "margin" else "원"))
    st = [it.status for it in items]
    if "mismatch" in st:
        status = "mismatch"
    elif all(s == "match" for s in st):
        status = "match"
    elif "match" in st:
        status = "partial"
    else:
        status = "unknown"
    return XbrlResult(status, items)
