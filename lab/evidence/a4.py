# lab/evidence/a4.py
"""A-4 평가(A-4 spec 4·5·6절): 주 실험 정책 C(a4-subject-exp, ①c 코드 + ② JEV 주체 질문)를 a2-v1과 같은 JEV 확률 위에서
짝으로 비교한다. 보조 팔 A(①c만, X1·X2)·B(②만)·a3(A-3 규칙)는 같은 호출에서 추가 호출 없이 계산하는 기술 통계다.
관문은 C 하나에만 건다(결정 2-1). 감사 팔 a3x0(A-3 규칙 + X0)은 탐색 감사용이다.

- 판정·경로는 제품과 같은 함수(app subject_a4.decide·route)로 정한다. 정책 상수는 제품 runner의 것을 쓰고, 사전등록 파일의
  값과 같은지 a4-report가 대조한다.
- 탐색(확정 전): A-2 조정 세트 + A-3 확인 세트(저장된 주 판정 확률을 읽기만 하고 후속 호출만 새로). A-3 확인 세트는 교차
  언급 군집 단위로 설계용 반·점검용 반으로 나눈다. 신호·τ_d는 설계용에서만 고르고, 진행 기준은 점검용 반(교체)과 두 탐색
  세트 합산(재현율)으로 판정한다. 모든 조합을 공개한다.
- 확인: 새 무작위 45개사 전부(조정 세트 없음). 통제 주장 c2 하위 유형은 9칸 순환.
- 외부 호출 없음. 값은 lab/evidence/prereg_a4.json과 같아야 한다(tests/evidence/test_a4_cli.py가 대조한다).
"""
from __future__ import annotations

import math
import random

from app.services.evidence import judge, subject_a4 as sa
from app.services.evidence.runner import A2_V1, A3_SUBJECT, A4_SUBJECT, Policy
from lab.evidence import a2, a3

SEED = 20261305  # 미리 적은 임의 상수(A-1 20261002·A-2 20261103·A-3 20261204와 다름). 추첨·부트스트랩
HALF_SEED = 20261306  # A-3 확인 세트 설계용·점검용 반분(spec 4.2-0)
RANDOM_N = 45
HALF_N = 20  # 설계용 반: 섞은 군집을 앞에서부터 기업 수가 20개에 이를 때까지
PRIOR_STUDIES = ("a1", "a2", "a3")  # 추첨 제외(120개사)와 교차 언급 10회 이상 제외
BOOTSTRAP_N = 2000
KAPPA_MIN = a2.KAPPA_MIN
CLAIM_CAP = a2.CLAIM_CAP
TOKENS_PER_CALL = a2.TOKENS_PER_CALL  # 주 판정 호출 p95(5,070)
FOLLOWUP_HEAD = 100  # 후속 호출 머리·주장 토큰 추정
FOLLOWUP_PER_PASSAGE = 645  # 후보 문단 1개(문단 430 + 주체 질문 215) 토큰 추정(spec 2.3)
FOLLOWUP_MARGIN = 0.10  # 확인 미리 멈춤 식의 후속 비율 여유(spec 5.6)
K = 8  # 문단 8개 전부로 바꾼 판(결정 2-2)의 m
EXPLORE_CAP_MAX = 3_000_000
TOKEN_CAP_MAX = 9_600_000
DEADLINE_MS = 8_000  # 실행 마감(runner Policy.deadline_s)
BASE: Policy = A2_V1
EXP: Policy = A4_SUBJECT
A3: Policy = A3_SUBJECT
POLICY_FIELDS = ("version", "tau_s", "tau_c", "theta_low", "theta_high", "subject_check", "subject_question",
                 "tau_d", "subject_signal")
SIGNALS = sa.SIGNALS
TAU_D_GRID = (0.2, 0.3, 0.4, 0.5, 0.6, 0.7)
MAX_PROMPT_VERSIONS = 3  # 문구 수정 최대 2회(결정 2-2 전환 포함)
DESIGN_MAX_LOSS_RATE = 0.01  # 신호·τ_d 선택: 설계용 합산 C 손실률 상한(spec 6.2)
PROCEED_LOSS_RATE = 0.01  # 진행 기준 재현율: 두 탐색 세트 합산 손실 ≤ ⌊0.01 × 분모⌋건(spec 4.2-4)
PROCEED_SWAP_MIN = 0.92  # 진행 기준 교체: 점검용 반 관문 대상 정확도
FOLLOWUP_FAIL_MAX = 0.01  # 판정(주 판정·후속) 실패가 판정 대상의 1%를 넘으면 세 관문 기술 통계만(실패)
COST_MAX_INCREASE = 0.25  # 제품 반영 비용 조건: 자연 주장 주장당 입력 토큰 증가 ≤ +25%
IN_PASSAGE = a3.IN_PASSAGE
SWAP_SUBTYPES = a3.SWAP_SUBTYPES
GATED_SUBTYPES = SWAP_SUBTYPES[:4]
SWAP_CYCLE = GATED_SUBTYPES * 2 + (IN_PASSAGE,)  # 9칸 순환(spec 4.3)
NOTATION_TYPES = a3.NOTATION_TYPES
SWAP_TAGS = ("고유명", "일반명사")  # 교체어 태그(AI가 판정 전에 단다)
GATES = {
    "h_swap": {"accuracy_min": 0.90, "ci_lo_min": 0.80, "diff_lo_min_exclusive": 0.0, "min_n": 150},
    "h_recall": {"loss_hi_max": 0.05, "min_positive": 150},
    "h_prec": {"diff_lo_min": -0.02, "min_predicted": 150},
}
# 팔: (①c 코드 마스크 팔, ② 질문 사용)
ARMS = {"a2-v1": (None, False), "a3": ("a3", False), "A": ("a", False), "B": (None, True), "C": ("c", True),
        "a3x0": ("a3x0", False)}
FIVE = ("a2-v1", "a3", "A", "B", "C")  # 하위 유형 보고 다섯 정책
AUX = ("a3", "A", "B")  # 보조 팔(기술 통계, 권고에 쓰지 않는다)
# 탐색 감사 원인(spec 4.2). 가·나·다는 기계적으로 채우고, 라·마·바는 AI가 분류한다(AI 작성 표시)
CAUSES = {"가": "①c 코드", "나": "② '다름'", "다": "② 'unclear'(교체면 탈출구)",
          "라": "연결 종속회사 사실을 '당사'로 서술한 주장을 ②가 '다름'으로 봄",
          "마": "후보 문단만 보여 줘 약칭 정의를 못 봄", "바": "기타"}


def policy_fields(p: Policy) -> dict:
    return {k: getattr(p, k) for k in POLICY_FIELDS}


def assigned(qid: str, all_qids: list[str]) -> tuple[str, str]:
    """전체 질문을 qid 순으로 정렬한 순번 i로 (주체 교체 하위 유형 SWAP_CYCLE[i mod 9], 표기 변형 유형
    NOTATION_TYPES[(i div 9) mod 3]). 9가 3의 배수라 같은 i mod로 돌리면 두 순환이 묶여(회사 칸은 늘 법인 표기) 일부 칸이 영영
    나오지 않는다. 표기는 9칸 한 바퀴마다 넘겨 연속 27개에서 9×3 모든 칸이 한 번씩 나오게 한다(교체 하위 유형 비율은 그대로)."""
    i = sorted(all_qids).index(qid)
    return SWAP_CYCLE[i % len(SWAP_CYCLE)], NOTATION_TYPES[(i // len(SWAP_CYCLE)) % len(NOTATION_TYPES)]


def split_halves(clusters: list[list[str]], seed: int = HALF_SEED,
                 n: int = HALF_N) -> tuple[list[list[str]], list[list[str]]]:
    """A-3 확인 세트 반분(spec 4.2-0): 군집 id(목록 순번) 오름차순 목록을 시드로 섞어, 앞에서부터 기업 수가 n에 이를 때까지
    설계용, 나머지는 점검용. 결과를 보기 전에(호출 전에) 정해 원장에 남긴다."""
    ids = list(range(len(clusters)))
    random.Random(seed).shuffle(ids)
    design, check, total = [], [], 0
    for i in ids:
        if total < n:
            design.append(clusters[i])
            total += len(clusters[i])
        else:
            check.append(clusters[i])
    return design, check


# --- 판정 ---------------------------------------------------------------------------------------------------
def _q(r: dict, signal: str, tau_d: float) -> list[bool] | None:
    return None if r.get("q_failed") else sa.q_mask(r.get("q") or {}, len(r["s"]), signal, tau_d)


def decide_row(r: dict, arm: str, signal: str, tau_d: float) -> tuple[str, int | None, float, str]:
    """(판정, 출처 문단, 점수, 경로). 제품과 같은 subject_a4.route·decide. lex_low 점수 0, lex_high 1."""
    code_arm, question = ARMS[arm]
    code = r["code"][code_arm] if code_arm else None
    q = _q(r, signal, tau_d) if question else None
    route = sa.route(r["lex"], r["high_ok"], r["best"], code, q, BASE.theta_low, BASE.theta_high,
                     code=code_arm is not None, question=question)
    if route == "lex_low":
        return "no_evidence", None, 0.0, route
    if route == "lex_high":
        return "supported", r["best"], 1.0, route
    dec, idx, score = sa.decide(r["s"], r["c"], r["valid"], code, q, BASE.tau_s, BASE.tau_c,
                                code=code_arm is not None, question=question)
    return dec, idx, (0.0 if dec == "contradicted" else score), route


def annotate(rows: list[dict], signal: str, tau_d: float, arms=tuple(ARMS)) -> list[dict]:
    """행마다 팔별 판정(dec)·출처(idx)·점수(score)를 붙인다."""
    out = []
    for r in rows:
        d = {arm: decide_row(r, arm, signal, tau_d) for arm in arms}
        out.append({**r, "dec": {k: v[0] for k, v in d.items()}, "idx": {k: v[1] for k, v in d.items()},
                    "score": {k: v[2] for k, v in d.items()}})
    return out


def _high_first(r: dict, theta_high: float | None) -> int | None:
    """제품이 주 판정 전에 따로 묻는 상단 구간 최고 점수 문단(lex ≥ θ_high ∧ high_ok ∧ ①c 통과), 없으면 None."""
    high = r["high_ok"] and r["best"] is not None and theta_high is not None and r["lex"] >= theta_high
    return r["best"] if high and r["code"]["c"][r["best"]] else None


def followup_requests(r: dict, theta_high: float | None = BASE.theta_high) -> list[list[int]]:
    """평가의 후속 요청 묶음 — 제품 실행기와 같은 문맥(측정값 = 운영값): 상단 구간이면 최고 점수 문단 하나만 먼저 따로 묻고
    ([best]), a2-v1 ✅ 후보(숫자 확인 ∧ s ≥ τ_s, 반박·미판정 제외)는 이미 물은 문단을 뺀 두 번째 요청으로 묻는다.
    (제품은 첫 요청이 통과하면 두 번째를 보내지 않지만, 평가는 보조 팔 계산을 위해 보낸다.)"""
    first = _high_first(r, theta_high)
    rest = [i for i in sa.candidate_passages(r["s"], r["c"], r["valid"], BASE.tau_s, BASE.tau_c) if i != first]
    return [x for x in ([first] if first is not None else [], rest) if x]


def followup_candidates(r: dict, theta_high: float | None = BASE.theta_high) -> list[int]:
    """후속 요청에서 묻는 문단 전부(오름차순): a2-v1 ✅ 후보 ∪ 상단 구간 최고 점수 문단."""
    high = r["high_ok"] and r["best"] is not None and theta_high is not None and r["lex"] >= theta_high
    return sa.candidate_passages(r["s"], r["c"], r["valid"], BASE.tau_s, BASE.tau_c, best=r["best"] if high else None)


# --- 1a 실측·예산 ----------------------------------------------------------------------------------------------
def explore_estimate(n_followup: int, m_mean: float, versions: int = 1, all_passages: bool = False) -> int:
    """탐색 토큰 추정: 후속 수 × (100 + m 평균 × 645) × 문구 판 수. 문단 8개 전부로 바꾼 판은 m = 8(spec 5.6)."""
    m = K if all_passages else m_mean
    return round(n_followup * (FOLLOWUP_HEAD + m * FOLLOWUP_PER_PASSAGE) * versions)


def explore_cap(n_followup: int, m_mean: float, versions: int = 1, all_passages: bool = False) -> int:
    """탐색 상한(1a 실측 식). 3,000,000을 넘으면 그 판의 호출 전에 멈춘다(결정 6)."""
    est = explore_estimate(n_followup, m_mean, versions, all_passages)
    if est > EXPLORE_CAP_MAX:
        raise SystemExit(f"explore estimate {est} > max 3,000,000: stop before calls (decision 6)")
    return est


def confirm_estimate(uncached: int, n_judge: int, followup_ratio: float, followup_p95: float) -> int:
    """확인 미리 멈춤 식: 캐시에 없는 주장 × 5,070 + 판정 대상 × (실측 후속 비율 + 0.10) × 탐색 실측 후속 p95."""
    return uncached * TOKENS_PER_CALL + round(n_judge * (followup_ratio + FOLLOWUP_MARGIN) * followup_p95)


def check_confirm_cap(cap: int) -> int:
    """확정 상한이 9,600,000을 넘으면 확정하지 않고 멈춘다(결정 6)."""
    if cap > TOKEN_CAP_MAX:
        raise SystemExit(f"confirm token cap {cap} > max 9,600,000: do not register (decision 6)")
    return cap


def check_prompt_versions(pairs: list[tuple[str, str]]) -> None:
    """판은 (문구 판, 문맥) 쌍으로 센다. 최대 3개(판 고침 최대 2회 — 결정 2-2의 '문단 8개 전부' 전환도 1회로 센다)."""
    got = sorted({tuple(p) for p in pairs})
    if len(got) > MAX_PROMPT_VERSIONS:
        raise SystemExit(f"prompt versions (version, context) {got} exceed {MAX_PROMPT_VERSIONS}")


def measure(rows: list[dict]) -> dict:
    """1a 실측(호출 0): 저장된 주 판정 확률만으로 후속 비율과 후보 문단 수 m(평균·p95, 후속이 있는 주장만)을 센다."""
    from lab.evidence.metrics import percentile

    ms = [len(followup_candidates(r)) for r in rows]
    pos = [m for m in ms if m]
    m_mean = sum(pos) / len(pos) if pos else 0.0
    m_p95 = percentile(pos, 95) if pos else 0.0
    return {"claims": len(rows), "followup": len(pos), "followup_ratio": len(pos) / len(rows) if rows else None,
            "m_mean": m_mean, "m_p95": m_p95, "explore_estimate_per_version": explore_estimate(len(pos), m_mean),
            "followup_tokens_p95_estimate": round(FOLLOWUP_HEAD + m_p95 * FOLLOWUP_PER_PASSAGE)}


# --- 탐색 ---------------------------------------------------------------------------------------------------
def _is_swap(r: dict) -> bool:
    return (r.get("variant") or "").startswith("주체 교체")


def _subtype(r: dict) -> str:
    return (r.get("variant") or "").split(":", 1)[-1]


def _gated(swaps: list[dict]) -> list[dict]:
    return [r for r in swaps if _subtype(r) != IN_PASSAGE]


def _loss(nat: list[dict], arm: str) -> dict:
    kept = [r for r in nat if r["y"] and r["dec"]["a2-v1"] == "supported"]
    lost = [r for r in kept if r["dec"][arm] != "supported"]
    return {"denominator": len(kept), "loss": len(lost), "loss_rate": len(lost) / len(kept) if kept else None}


def _acc(rows: list[dict], arm: str) -> float | None:
    return sum(r["dec"][arm] != "supported" for r in rows) / len(rows) if rows else None


HALVES = {"design": ("a2-tune", "a3-design"), "check": ("a3-check",)}


def explore_grid(natural: list[dict], swaps: list[dict], signals=SIGNALS, grid=TAU_D_GRID,
                 halves=("design", "check")) -> list[dict]:
    """(신호 × τ_d)마다 C·A·B의 자연 주장 손실과 관문 대상 주체 교체 정확도(모든 조합 공개).
    설계용 = A-2 조정 세트 + A-3 설계용 반, 점검용 = A-3 점검용 반(행의 set: a2-tune / a3-design / a3-check).
    a4-explore는 halves=("design",)로 설계용만 계산한다. 점검용은 a4-proceed가 한 번만 본다.
    자연 주장 손실의 분모는 후속 실패 행을 뺀 것이다(호출 쪽에서 뺀다)."""
    out = []
    for sig in signals:
        for t in grid:
            nat, sw = annotate(natural, sig, t), annotate(_gated(swaps), sig, t)
            arms = {}
            for arm in ("C", "A", "B"):
                arms[arm] = {}
                for half in halves:
                    sets = HALVES[half]
                    n = [r for r in nat if r["set"] in sets]
                    s = [r for r in sw if r["set"] in sets]
                    arms[arm][half] = {**_loss(n, arm), "swap_n": len(s), "swap_accuracy": _acc(s, arm)}
            out.append({"signal": sig, "tau_d": t, "arms": arms})
    return out


def choose(grid: list[dict]) -> dict | None:
    """spec 6.2(설계용만): 설계용 C 손실률 ≤ 1.0%인 조합 중 설계용 교체 정확도가 가장 높은 것. 같으면 신호는 P(diff),
    τ_d는 큰 값. 해당 없으면 None(멈춤). 점검용 값은 읽지 않는다."""
    ok = [g for g in grid if (g["arms"]["C"]["design"]["loss_rate"] or 0.0) <= DESIGN_MAX_LOSS_RATE
          and g["arms"]["C"]["design"]["loss_rate"] is not None]
    if not ok:
        return None
    best = max(ok, key=lambda g: (g["arms"]["C"]["design"]["swap_accuracy"] or 0.0, g["signal"] == "p_diff",
                                  g["tau_d"]))
    return {"signal": best["signal"], "tau_d": best["tau_d"]}


def choose_all(grids: dict[tuple[str, str], list[dict]]) -> dict | None:
    """판·문맥도 설계용 격자 합본에서 6.2 규칙으로 고른다: 설계용 C 손실률 ≤ 1.0%인 (판, 문맥, 신호, τ_d) 중 설계용 교체
    정확도가 가장 높은 것. 같으면 P(diff), 큰 τ_d, 먼저 탐색한 판(grids 순서). 해당 없으면 None."""
    best, key = None, None
    for order, ((version, context), grid) in enumerate(grids.items()):
        for g in grid:
            d = g["arms"]["C"]["design"]
            if d["loss_rate"] is None or d["loss_rate"] > DESIGN_MAX_LOSS_RATE:
                continue
            k = (d["swap_accuracy"] or 0.0, g["signal"] == "p_diff", g["tau_d"], -order)
            if key is None or k > key:
                best, key = {"version": version, "context": context, "signal": g["signal"], "tau_d": g["tau_d"]}, k
    return best


def max_explore_loss(denominator: int) -> int:
    """진행 기준 재현율 손실 허용 건수: floor(0.01 × 분모 + 0.5)(리드 ruling, 분모 397이면 4건 — 결정 기록 4번과 작동 특성 표
    0.95/0.63/0.29/0.10/0.01이 재현되는 값)."""
    return math.floor(PROCEED_LOSS_RATE * denominator + 0.5)


def proceed(natural: list[dict], swaps: list[dict], signal: str, tau_d: float) -> dict:
    """진행 기준(spec 4.2-4, 모두 만족해야 확정): 재현율(두 탐색 세트 합산 C 손실 ≤ floor(0.01 × 분모 + 0.5)건), 교체(점검용 반 C 관문
    대상 정확도 ≥ 0.92), 방향(A-3 확인 세트 전체에서 C 부문·사업과 제품·브랜드 정확도가 각각 a3보다 높음)."""
    nat, sw = annotate(natural, signal, tau_d), annotate(_gated(swaps), signal, tau_d)
    loss = _loss(nat, "C")
    mx = max_explore_loss(loss["denominator"])
    recall = {"denominator": loss["denominator"], "lost": loss["loss"], "max_lost": mx, "pass": loss["loss"] <= mx}
    chk = [r for r in sw if r["set"] == "a3-check"]
    acc = _acc(chk, "C")
    swap = {"n": len(chk), "accuracy": acc, "min": PROCEED_SWAP_MIN, "pass": acc is not None and acc >= PROCEED_SWAP_MIN}
    a3rows = [r for r in sw if r["set"] in ("a3-design", "a3-check")]
    direction = {}
    for st in ("부문·사업", "제품·브랜드"):
        rs = [r for r in a3rows if _subtype(r) == st]
        direction[st] = {"n": len(rs), "C": _acc(rs, "C"), "a3": _acc(rs, "a3")}
    direction["pass"] = all(v["C"] is not None and v["a3"] is not None and v["C"] > v["a3"]
                            for v in direction.values())
    halves = {h: _loss([r for r in nat if r["set"] in sets], "C")
              for h, sets in (("design", ("a2-tune", "a3-design")), ("check", ("a3-check",)))}
    return {"signal": signal, "tau_d": tau_d, "recall": recall, "swap": swap, "direction": direction,
            "optimism": {"design": {**halves["design"], "swap_accuracy": _acc(
                [r for r in sw if r["set"] == "a3-design"], "C")},
                "check": {**halves["check"], "swap_accuracy": acc}},
            "proceed": recall["pass"] and swap["pass"] and direction["pass"]}


def audit(natural: list[dict], swaps: list[dict], signal: str, tau_d: float) -> dict:
    """탐색 감사 목록: 'a2-v1 ✅ → C ❔' 자연 주장과 C가 놓친 관문 대상 교체. 기계적 원인(가 ①c 코드, 나 ② '다름',
    다 ② 'unclear')만 채우고 ai_cause(라·마·바 포함)는 비워 둔다 — AI가 분류하고 AI 작성임을 밝힌다.
    판 고침·문맥 전환의 근거라 설계용 절반 행만 쓴다(점검용 행은 받아도 뺀다)."""
    design = HALVES["design"]
    natural = [r for r in natural if r["set"] in design]
    swaps = [r for r in swaps if r["set"] in design]
    nat = annotate(natural, signal, tau_d)
    out_n = []
    for r in nat:
        if not (r["dec"]["a2-v1"] == "supported" and r["dec"]["C"] != "supported"):
            continue
        i = r["idx"]["a2-v1"]
        code_fail = not r["code"]["c"][i]
        p = (r.get("q") or {}).get(i)
        q_fail = p is None or not sa.question_pass(p, signal, tau_d)
        cause = "가" if code_fail else (None if p is None else
                                        "나" if p["different_subject"] >= p["unclear"] else "다")
        out_n.append({"cid": r["cid"], "set": r["set"], "label": r.get("label"), "source": i, "code_fail": code_fail,
                      "q_fail": q_fail, "p_diff": None if p is None else p["different_subject"],
                      "p_unclear": None if p is None else p["unclear"], "cause": cause, "ai_cause": None})
    out_s = []
    for r in annotate(_gated(swaps), signal, tau_d):
        if r["dec"]["C"] != "supported":
            continue
        i = r["idx"]["C"]
        p = (r.get("q") or {}).get(i) or {}
        out_s.append({"cid": r["cid"], "set": r["set"], "subtype": _subtype(r), "source": i,
                      "p_diff": p.get("different_subject"), "p_unclear": p.get("unclear"),
                      "cause": "다" if p and p["unclear"] > p["different_subject"] else None, "ai_cause": None})
    return {"natural": out_n, "swaps": out_s, "causes": CAUSES}


def x0_audit(swaps: list[dict]) -> dict:
    """spec 2.1 감사 항목: A-3 규칙에 X0만 적용한 팔(a3x0)에서 a3 대비 ✅가 늘어난 관문 대상 교체(X0으로 코드가 더는 막지 못해 ②가
    막아야 하는 부문 교체 수). JEV 후속 확률을 읽지 않는다."""
    sw = annotate(_gated(swaps), "p_diff", 1.0, arms=("a3", "a3x0"))
    more = [r for r in sw if r["dec"]["a3x0"] == "supported" and r["dec"]["a3"] != "supported"]
    by: dict[str, int] = {}
    for r in more:
        by[_subtype(r)] = by.get(_subtype(r), 0) + 1
    return {"n": len(sw), "more_supported": len(more), "by_subtype": by, "cids": [r["cid"] for r in more]}


# --- 확인 관문 ------------------------------------------------------------------------------------------------
def _rate(rows, pred):
    return sum(1 for r in rows if pred(r)) / len(rows) if rows else None


def _precision(rows: list[dict], arm: str) -> float | None:
    sup = [r for r in rows if r["dec"][arm] == "supported"]
    return sum(r["y"] for r in sup) / len(sup) if sup else None


def _recall(rows: list[dict], arm: str) -> float | None:
    return _rate([r for r in rows if r["y"]], lambda r: r["dec"][arm] == "supported")


def _diff(f, g):
    def stat(rs):
        a, b = f(rs), g(rs)
        return None if a is None or b is None else a - b
    return stat


def _accf(arm: str):
    return lambda rs: _rate(rs, lambda r: r["dec"][arm] != "supported")


def _three(nat: list[dict], sw: list[dict], arm: str, n_boot: int, seed: int, forced_desc: bool) -> dict:
    """한 팔의 세 관문 값(같은 표본, a2-v1과 짝)."""
    from lab.evidence.metrics import cluster_bootstrap

    gs, gr, gp = GATES["h_swap"], GATES["h_recall"], GATES["h_prec"]
    p_acc, lo, hi = cluster_bootstrap(sw, _accf(arm), n=n_boot, seed=seed)
    d, dlo, dhi = cluster_bootstrap(sw, _diff(_accf(arm), _accf("a2-v1")), n=n_boot, seed=seed)
    small = forced_desc or len(sw) < gs["min_n"]
    h_swap = {"n": len(sw), "accuracy": p_acc, "ci": [lo, hi], "base_accuracy": _accf("a2-v1")(sw),
              "diff": {"point": d, "lo": dlo, "hi": dhi}, "descriptive_only": small,
              "pass": (not small and p_acc is not None and lo is not None and dlo is not None
                       and p_acc >= gs["accuracy_min"] and lo >= gs["ci_lo_min"] and dlo > gs["diff_lo_min_exclusive"])}
    pos = sum(r["y"] for r in nat)
    loss, llo, lhi = cluster_bootstrap(nat, _diff(lambda rs: _recall(rs, "a2-v1"), lambda rs: _recall(rs, arm)),
                                       n=n_boot, seed=seed)
    few = forced_desc or pos < gr["min_positive"]
    h_recall = {"positive": pos, "base_recall": _recall(nat, "a2-v1"), "recall": _recall(nat, arm), "loss": loss,
                "ci": [llo, lhi], "descriptive_only": few,
                "pass": not few and lhi is not None and lhi <= gr["loss_hi_max"]}
    pred = sum(r["dec"][arm] == "supported" for r in nat)
    prec, plo, phi = cluster_bootstrap(nat, lambda rs: _precision(rs, arm), n=n_boot, seed=seed)
    pd, pdlo, pdhi = cluster_bootstrap(nat, _diff(lambda rs: _precision(rs, arm), lambda rs: _precision(rs, "a2-v1")),
                                       n=n_boot, seed=seed)
    desc = forced_desc or pred < gp["min_predicted"]
    h_prec = {"predicted": pred, "precision": prec, "ci": [plo, phi], "base_precision": _precision(nat, "a2-v1"),
              "base_predicted": sum(r["dec"]["a2-v1"] == "supported" for r in nat),
              "diff": {"point": pd, "lo": pdlo, "hi": pdhi}, "descriptive_only": desc,
              "pass": not desc and pdlo is not None and pdlo >= gp["diff_lo_min"]}
    return {"h_swap": h_swap, "h_recall": h_recall, "h_prec": h_prec}


def check_gates(rows: list[dict], signal: str, tau_d: float, n_boot: int = BOOTSTRAP_N,
                seed: int = SEED, main_failed: int = 0) -> dict:
    """A-4 관문(spec 5.1): C(a4-subject-exp) 대 a2-v1. rows: 판정 대상 전부(이견 없는 판정 주장 — 자연·c1·c2·c3, 주 판정이
    성공한 행). 자연 주장(source natural, y 라벨)과 통제 주체 교체 주장(variant '주체 교체:*')을 안에서 나눈다.

    판정 실패 1% 규칙의 분자·분모는 같은 집합(이견 없는 판정 주장 전부)이다: 분모 = 행 수 + 주 판정 실패(main_failed, 이미 뺀
    행), 분자 = main_failed + 후속 호출이 실패한 행(q_failed). 실패 행은 모든 정책·팔에서 짝으로 빼고 수를 보고한다. 1%를
    넘으면 세 관문 모두 기술 통계만(실패). '문단 안 교체'는 관문 밖(다섯 정책 기술 통계). 보조 팔 a3·A·B의 세 관문 값은 기술 통계이고
    권고(recommendation)에 쓰지 않는다. H-swap 민감도(최종 라벨이 supported·disputed가 아닌 c2만)는 관문이 아니다."""
    from lab.evidence.metrics import cluster_bootstrap

    natural = [r for r in rows if r.get("source") == "natural"]
    swaps = [r for r in rows if _is_swap(r)]
    judged = len(rows) + main_failed
    failed = sum(1 for r in rows if r.get("q_failed")) + main_failed
    over = judged > 0 and failed / judged > FOLLOWUP_FAIL_MAX
    keep = lambda rs: [r for r in rs if not r.get("q_failed")]  # noqa: E731
    nat = annotate(keep(natural), signal, tau_d)
    all_sw = annotate(keep(swaps), signal, tau_d)
    sw = [r for r in all_sw if _subtype(r) != IN_PASSAGE]
    inside = [r for r in all_sw if _subtype(r) == IN_PASSAGE]
    out = _three(nat, sw, "C", n_boot, seed, over)
    by_subtype = {}
    for st in sorted({_subtype(r) for r in sw}):
        rs = [r for r in sw if _subtype(r) == st]
        by_subtype[st] = {"n": len(rs), "accuracy": {arm: _accf(arm)(rs) for arm in FIVE},
                          "discordant": {"c_only": sum(r["dec"]["C"] != "supported" and r["dec"]["a3"] == "supported"
                                                       for r in rs),
                                         "a3_only": sum(r["dec"]["a3"] != "supported" and r["dec"]["C"] == "supported"
                                                        for r in rs)},
                          "by_tag": {t: {"n": len(x), "accuracy": {arm: _accf(arm)(x) for arm in FIVE}}
                                     for t in SWAP_TAGS if (x := [r for r in rs if r.get("swap_tag") == t])}}
    out["h_swap"]["by_subtype"] = by_subtype
    out["h_swap"]["by_tag"] = {t: {"n": len(x), "accuracy": {arm: _accf(arm)(x) for arm in FIVE}}
                               for t in SWAP_TAGS if (x := [r for r in sw if r.get("swap_tag") == t])}
    sens = [r for r in sw if r.get("label") not in ("supported", "disputed")]
    s_acc, s_lo, s_hi = cluster_bootstrap(sens, _accf("C"), n=n_boot, seed=seed) if sens else (None, None, None)
    out["h_swap_sensitivity"] = {"n": len(sens), "accuracy": s_acc, "ci": [s_lo, s_hi],
                                 "base_accuracy": _accf("a2-v1")(sens)}
    out["in_passage_swap"] = {"n": len(inside), "accuracy": {arm: _accf(arm)(inside) for arm in FIVE}}
    out["arms"] = {arm: _three(nat, sw, arm, n_boot, seed, over) for arm in AUX}
    out["followup_failed_excluded"] = failed
    out["judged"] = judged
    out["judge_failed_over"] = over
    return out


def recommendation(gates: dict, cost: dict) -> dict:
    """사전등록 반영 규칙(spec 6.5 product_mapping). C의 세 관문과 운영 비용 조건만 본다(보조 팔은 보지 않는다, 6.4).
    전환은 별도 PR로 '제안'만 한다(이 함수는 제품을 바꾸지 않는다)."""
    sw, rc, pr = (gates[k]["pass"] for k in ("h_swap", "h_recall", "h_prec"))
    if gates.get("judge_failed_over"):
        case, note = "judge_failed", "판정 실패 1% 초과: 세 관문 기술 통계만(실패), a2-v1 유지. 다른 모델 버전으로 이어서 재지 않는다"
    elif sw and rc and pr and cost.get("pass"):
        case, note = "all_pass", "a4-subject-exp를 기본 정책으로 바꾸자고 별도 PR로 제안(머지는 노아에게 보고한 뒤)"
    elif sw and rc and pr:
        case, note = "all_pass_cost_over", "a2-v1 유지. 비용 조건(+25%) 초과를 노아에게 보고한다(관문 결과는 그대로 공개)"
    elif not sw:
        case, note = "h_swap_fail", ("a2-v1 유지. 하위 유형 감사로 다음 방향을 AI 리드가 정해 원장 ruling으로 남기고 "
                                     "노아에게 보고한다")
    elif not rc:
        case, note = "h_recall_fail", "a2-v1 유지. 감사 목록에서 손실 원인을 ①c·②로 나눠 다음 연구를 정한다"
    else:
        case, note = "h_prec_fail_only", ("a2-v1 유지. 다시 재려면 새 사전등록의 별도 연구로 하고 A-4 실패 결과를 함께 "
                                          "공개한다")
    return {"switch_default": case == "all_pass", "case": case, "note": note}


def removed_audit(natural: list[dict], signal: str, tau_d: float) -> list[dict]:
    """확인 세트 감사 목록: a2-v1 ✅ → C ❔ 자연 주장 전부와 원인(①c 코드·② 질문·둘 다), ②면 P(diff)."""
    out = []
    for r in annotate([r for r in natural if not r.get("q_failed")], signal, tau_d, arms=("a2-v1", "C")):
        if r["dec"]["a2-v1"] != "supported" or r["dec"]["C"] == "supported":
            continue
        i = r["idx"]["a2-v1"]
        code_fail = i is not None and not r["code"]["c"][i]
        p = (r.get("q") or {}).get(i) if i is not None else None
        q_fail = p is None or not sa.question_pass(p, signal, tau_d)
        out.append({"cid": r["cid"], "label": r.get("label"),
                    "cause": "both" if code_fail and q_fail else "code" if code_fail else "question",
                    "p_diff": None if p is None else p["different_subject"]})
    return out


def missed_swap_distribution(swaps: list[dict], signal: str, tau_d: float) -> list[dict]:
    """C가 놓친 관문 대상 교체의 출처 문단 P(diff)·P(unclear)(spec 5.3)."""
    out = []
    for r in annotate(_gated([r for r in swaps if not r.get("q_failed")]), signal, tau_d, arms=("a2-v1", "C")):
        if r["dec"]["C"] == "supported":
            p = (r.get("q") or {}).get(r["idx"]["C"]) or {}
            out.append({"cid": r["cid"], "subtype": _subtype(r), "p_diff": p.get("different_subject"),
                        "p_unclear": p.get("unclear")})
    return out


def cost_summary(records: list[dict]) -> dict:
    """운영 비용(spec 5.4, 관문 아님). records: 주장별 main_tokens·followup_tokens·m·latency_ms·followup_latency_ms·source.
    제품 반영 조건은 자연 주장 기준 주장당 입력 토큰 증가(후속 합 / 주 판정 합) ≤ +25%."""
    from lab.evidence.metrics import percentile

    nat = [r for r in records if r.get("source") == "natural"]
    fu = [r for r in records if r["m"]]
    main = sum(r["main_tokens"] for r in nat)
    inc = sum(r["followup_tokens"] for r in nat) / main if main else None
    ft = [r["followup_tokens"] for r in fu]
    fl = [r["followup_latency_ms"] for r in fu]
    over = [r for r in records if r["latency_ms"] + r["followup_latency_ms"] > DEADLINE_MS]
    return {"claims": len(records), "followup_ratio": len(fu) / len(records) if records else None,
            "m_mean": sum(r["m"] for r in fu) / len(fu) if fu else None,
            "m_p95": percentile([r["m"] for r in fu], 95) if fu else None,
            "followup_tokens_mean": sum(ft) / len(ft) if ft else None,
            "followup_tokens_p95": percentile(ft, 95) if ft else None,
            "followup_latency_p95_ms": percentile(fl, 95) if fl else None,
            "token_increase": inc, "over_deadline_share": len(over) / len(records) if records else None,
            "max_increase": COST_MAX_INCREASE, "pass": inc is not None and inc <= COST_MAX_INCREASE}


# --- R2 폴백 단조성(spec 6.3) ---------------------------------------------------------------------------------
def _supported(kind: str, value: float, s, c, valid, q) -> bool:
    if kind == "tau_s":
        return judge.sys_decision(judge.Judgement(s, c, True, 1), valid, value, BASE.tau_c)[0] == "supported"
    if kind == "tau_d":
        mask = [sa.question_pass(p, "p_diff", value) for p in q]
        return sa.decide(s, c, valid, None, mask, BASE.tau_s, BASE.tau_c, code=False)[0] == "supported"
    raise ValueError(f"unknown fallback kind {kind}")


def fallback_violations(kind: str, tuned: float, fallback: float, n: int = 2000, seed: int = SEED) -> int:
    """같은 무작위 주 판정 확률·후보 표본에서 ✅(폴백) ⊄ ✅(조정)인 표본 수. 0이어야 폴백을 둘 수 있다(R2)."""
    rng = random.Random(seed)
    bad = 0
    for _ in range(n):
        k = rng.randint(1, 8)
        s = [rng.random() for _ in range(k)]
        c = [rng.random() * 0.5 for _ in range(k)]
        valid = [rng.random() < 0.8 for _ in range(k)]
        q = []
        for _ in range(k):
            d, u = rng.random(), rng.random()
            d, u = d * (1 - u * 0.5), u * 0.5 * (1 - d)
            q.append({"same_subject": 1 - d - u, "different_subject": d, "unclear": u})
        if _supported(kind, fallback, s, c, valid, q) and not _supported(kind, tuned, s, c, valid, q):
            bad += 1
    return bad


def check_registrable(pre: dict, selection: dict | None = None, require_selection: bool = False) -> None:
    """사전등록을 registered로 바꾸기 전(그리고 split·a4-report에서 다시) 검사: τ_d·신호가 격자 안에서 정해졌고, a4-proceed
    결과의 선택(판·문맥·신호·τ_d)과 같고 진행 기준을 통과했으며, 폴백 운영점이 있으면 R2를 지킨다."""
    if pre.get("tau_d") not in TAU_D_GRID or pre.get("signal") not in SIGNALS:
        raise SystemExit("cannot register: tau_d·signal must be chosen from the grid in exploration")
    if selection is None and require_selection:
        raise SystemExit("cannot register: no a4-proceed selection in attempts.jsonl")
    if selection is not None:
        sq = pre.get("subject_question") or {}
        mine = (sq.get("version"), sq.get("context"), pre["signal"], pre["tau_d"])
        theirs = tuple(selection.get(k) for k in ("version", "context", "signal", "tau_d"))
        if mine != theirs:
            raise SystemExit(f"cannot register: prereg {mine} differs from the a4-proceed selection {theirs}")
        if not selection.get("proceed"):
            raise SystemExit("cannot register: a4-proceed criteria not met (proceed is false)")
    for f in pre.get("fallbacks") or []:
        v = fallback_violations(f["kind"], f["tuned"], f["fallback"])
        if v:
            raise SystemExit(f"cannot register: R2 fallback monotonicity violated ({f['kind']} {f}, {v} samples)")


# 주체 질문 해시·코드 상수 확인용(사전등록 대조)
SUBJECT_QUESTION_SHA = sa.SUBJECT_QUESTION_SHA
NUMERAL_DETERMINERS = sa.NUMERAL_DETERMINERS
ADVERBIAL_SUFFIXES = sa.ADVERBIAL_SUFFIXES
