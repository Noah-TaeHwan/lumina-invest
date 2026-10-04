# lab/evidence/a3.py
"""A-3 평가(A-3 spec 5절): 주체 확인 실험 정책(a3-subject-exp)과 a2-v1을 같은 JEV 확률 위에서 짝으로 비교한다.

- 바뀌는 것은 주체 확인 하나뿐이다(τ_s 0.85·τ_c 0.35·θ_high 0.95는 a2-v1과 같고, 이번 연구에서 고르는 값이 없다).
  그래서 조정 세트가 없고 새 무작위 40개사 전부가 확인 세트다.
- 판정·경로는 제품과 같은 함수(app subject.route_claim·decide_claim)로 정한다. 정책 상수는 제품 runner의 것을 쓰고,
  사전등록 파일의 값과 같은지 a3-report가 대조한다.
- 통제 주장: 질문마다 c1 의역 참, c2 주체 교체(하위 유형 순환), c3 표기 변형 참(유형 순환). 외부 호출 없음.
- 값은 lab/evidence/prereg_a3.json과 같아야 한다(tests/evidence/test_a3_cli.py가 대조한다).
"""
from __future__ import annotations

from app.services.evidence import lexical, subject
from app.services.evidence.claims import new_values_absent, numeric_tokens
from app.services.evidence.runner import A2_V1, A3_SUBJECT, Policy
from lab.evidence import a2

SEED = 20261204  # 미리 적은 임의 상수(A-2 20261103과 다름)
RANDOM_N = 40
TOKEN_CAP = 7_000_000
TOKENS_PER_CALL = a2.TOKENS_PER_CALL
CLAIM_CAP = a2.CLAIM_CAP
BOOTSTRAP_N = 2000
KAPPA_MIN = a2.KAPPA_MIN
BASE: Policy = A2_V1
EXP: Policy = A3_SUBJECT
POLICY_FIELDS = ("version", "tau_s", "tau_c", "theta_low", "theta_high", "subject_check")
SWAP_SUBTYPES = ("회사", "부문·사업", "제품·브랜드", "거래상대·자회사")
NOTATION_TYPES = ("법인 표기", "띄어쓰기", "영문·약칭")
GATES = {
    "h_swap": {"accuracy_min": 0.90, "ci_lo_min": 0.80, "diff_lo_min_exclusive": 0.0, "min_n": 150},
    "h_recall": {"loss_hi_max": 0.05, "min_positive": 150},
    "h_prec": {"precision_min": 0.90, "ci_lo_min": 0.80, "min_predicted": 150},
}


def policy_fields(p: Policy) -> dict:
    return {k: getattr(p, k) for k in POLICY_FIELDS}


def assigned(qid: str, all_qids: list[str]) -> tuple[str, str]:
    """전체 질문을 qid 순으로 정렬한 순번으로 (주체 교체 하위 유형, 표기 변형 유형)을 순환 배정한다."""
    i = sorted(all_qids).index(qid)
    return SWAP_SUBTYPES[i % len(SWAP_SUBTYPES)], NOTATION_TYPES[i % len(NOTATION_TYPES)]


def _core(tok: str) -> str:
    """토큰에서 앞뒤 문장부호·법인 표지·뒤 조사를 뗀 핵심(비교용 정규화 전)."""
    t = lexical._EDGE.sub("", subject._strip_legal(tok))
    return lexical._stem(t) or t


def _new_cores(text: str, base: str) -> list[str]:
    """text에만 있는 토큰의 핵심(base에 없는 토큰)."""
    old = set(base.split())
    return [c for c in (_core(t) for t in text.split() if t not in old) if c]


def check_swap(true: str, variant: str, passages: list[str], names: tuple[str, ...]) -> tuple[bool, str]:
    """주체 교체 검사: 참 문장과 다르고, 숫자는 그대로이며, 새로 넣은 이름이 문단 8개 어디에도 없다.

    후보 추출(subject_groups)과 독립인 토큰 차이로 본다. 후보 추출로 거르면 추출이 놓친 교체가 빠져 정확도가 부풀려진다.
    """
    if variant.strip() == true.strip():
        return False, "variant equals true text"
    if numeric_tokens(variant) != numeric_tokens(true):
        return False, "numbers changed"
    blob = subject.normalize(" ".join(passages))
    new = _new_cores(variant, true)
    if not new:
        return False, "no new name"
    if any(subject.normalize(c) in blob for c in new) or not new_values_absent(variant, true, passages, names):
        return False, "new name present in passages"
    return True, "ok"


def check_notation(true: str, text: str, ntype: str, passages: list[str]) -> tuple[bool, str]:
    """표기 변형 검사: 참 문장과 다르고 숫자는 그대로. 법인 표기·띄어쓰기는 법인 표지·공백·대소문자를 지우면 참 문장과 같다.
    영문·약칭은 새로 넣은 표기가 문단 8개에 그대로 있다(문단이 그 약칭을 쓰거나 정의한다)."""
    if text.strip() == true.strip():
        return False, "notation equals true text"
    if numeric_tokens(text) != numeric_tokens(true):
        return False, "numbers changed"
    if ntype in ("법인 표기", "띄어쓰기"):
        return (True, "ok") if subject.normalize(text) == subject.normalize(true) else (False, "not a notation change")
    joined = subject._strip_legal(" ".join(passages))
    new = _new_cores(text, true)
    if not new or not all(subject._found(c, joined) for c in new):
        return False, "abbreviation not in passages"
    return True, "ok"


def decide(row: dict, policy: Policy) -> tuple[str, int | None, float, str]:
    """(판정, 출처 문단, 점수, 경로). 제품과 같은 route_claim·decide_claim. lex_low 점수 0, lex_high 1."""
    subj = row["subj"] if policy.subject_check else None
    route = subject.route_claim(policy, row["lex"], row["high_ok"], row["best"], subj)
    if route == "lex_low":
        return "no_evidence", None, 0.0, route
    if route == "lex_high":
        return "supported", row["best"], 1.0, route
    dec, idx, score = subject.decide_claim(policy, row["s"], row["c"], row["valid"], subj)
    return dec, idx, (0.0 if dec == "contradicted" else score), route


def annotate(rows: list[dict]) -> list[dict]:
    """행마다 두 정책의 판정·출처·점수를 붙인다(base: a2-v1, exp: a3-subject-exp)."""
    out = []
    for r in rows:
        b, e = decide(r, BASE), decide(r, EXP)
        out.append({**r, "base": b[0], "base_idx": b[1], "base_score": b[2], "exp": e[0], "exp_score": e[2]})
    return out


def _rate(rows: list[dict], pred) -> float | None:
    return sum(1 for r in rows if pred(r)) / len(rows) if rows else None


def _precision(rows: list[dict], key: str) -> float | None:
    sup = [r for r in rows if r[key] == "supported"]
    return sum(r["y"] for r in sup) / len(sup) if sup else None


def _recall(rows: list[dict], key: str) -> float | None:
    return _rate([r for r in rows if r["y"]], lambda r: r[key] == "supported")


def _diff(f, g):
    def stat(rs):
        a, b = f(rs), g(rs)
        return None if a is None or b is None else a - b
    return stat


def check_gates(natural: list[dict], swaps: list[dict], n_boot: int = BOOTSTRAP_N, seed: int = SEED) -> dict:
    """A-3 관문(spec 5.3). natural: 판정 가능 자연 주장(y 라벨), swaps: 통제 주체 교체 주장. 둘 다 같은 JEV 확률."""
    from lab.evidence.metrics import cluster_bootstrap

    G = GATES
    nat, sw = annotate(natural), annotate(swaps)

    acc = lambda key: (lambda rs: _rate(rs, lambda r: r[key] != "supported"))  # noqa: E731
    p_acc, lo, hi = cluster_bootstrap(sw, acc("exp"), n=n_boot, seed=seed)
    d, dlo, dhi = cluster_bootstrap(sw, _diff(acc("exp"), acc("base")), n=n_boot, seed=seed)
    small = len(sw) < G["h_swap"]["min_n"]
    gs = G["h_swap"]
    h_swap = {"n": len(sw), "exp_accuracy": p_acc, "ci": [lo, hi], "base_accuracy": acc("base")(sw),
              "diff": {"point": d, "lo": dlo, "hi": dhi}, "descriptive_only": small,
              "pass": (not small and p_acc is not None and lo is not None and dlo is not None
                       and p_acc >= gs["accuracy_min"] and lo >= gs["ci_lo_min"]
                       and dlo > gs["diff_lo_min_exclusive"])}

    pos = sum(r["y"] for r in nat)
    loss, llo, lhi = cluster_bootstrap(nat, _diff(lambda rs: _recall(rs, "base"), lambda rs: _recall(rs, "exp")),
                                       n=n_boot, seed=seed)
    few = pos < G["h_recall"]["min_positive"]
    h_recall = {"positive": pos, "base_recall": _recall(nat, "base"), "exp_recall": _recall(nat, "exp"),
                "loss": loss, "ci": [llo, lhi], "descriptive_only": few,
                "pass": not few and lhi is not None and lhi <= G["h_recall"]["loss_hi_max"]}

    pred = sum(r["exp"] == "supported" for r in nat)
    prec, plo, phi = cluster_bootstrap(nat, lambda rs: _precision(rs, "exp"), n=n_boot, seed=seed)
    desc = pred < G["h_prec"]["min_predicted"]
    gp = G["h_prec"]
    h_prec = {"predicted": pred, "precision": prec, "ci": [plo, phi], "base_precision": _precision(nat, "base"),
              "base_predicted": sum(r["base"] == "supported" for r in nat), "descriptive_only": desc,
              "pass": (not desc and prec is not None and plo is not None
                       and prec >= gp["precision_min"] and plo >= gp["ci_lo_min"])}
    return {"h_swap": h_swap, "h_recall": h_recall, "h_prec": h_prec}


def recommendation(gates: dict) -> dict:
    """세 관문을 모두 통과해야 기본값 전환을 '제안'한다. 전환 자체는 노아가 별도 PR로 결정한다(이 함수는 제품을 바꾸지 않는다)."""
    ok = all(gates[k]["pass"] for k in ("h_swap", "h_recall", "h_prec"))
    return {"switch_default": ok,
            "note": "a3-subject-exp를 기본 정책으로 바꾸자고 제안(별도 PR, 결정권자 노아)" if ok else
                    "a2-v1 유지. 실패한 관문과 감사 목록으로 다음 방법(spec 2절 ②·③)을 정한다"}


def controlled_summary(rows: list[dict]) -> dict:
    """통제 주장 변형별 정확도(두 정책)와 표기 변형 오탐: a2-v1이 ✅로 둔 c3 중 a3에서 ✅가 아닌 비율.

    c3는 AI 라벨이 supported인 것만 센다(표기를 바꾼 문장이 여전히 참인지 라벨러가 확인한 것).
    """
    d = annotate(rows)
    by: dict[str, dict[str, list[bool]]] = {"base": {}, "exp": {}}
    for r in d:
        for key in ("base", "exp"):
            by[key].setdefault(r["variant"], []).append((r[key] == "supported") == (r["expected"] == "supported"))
    c3 = [r for r in d if r["variant"].startswith("표기 변형") and r.get("label") == "supported"]
    kept = [r for r in c3 if r["base"] == "supported"]
    lost = [r for r in kept if r["exp"] != "supported"]
    types: dict[str, dict[str, int]] = {}
    for r in kept:
        t = types.setdefault(r["variant"].split(":", 1)[1], {"n": 0, "lost": 0})
        t["n"] += 1
        t["lost"] += r["exp"] != "supported"
    return {"accuracy": {k: {v: sum(x) / len(x) for v, x in sorted(m.items())} for k, m in by.items()},
            "notation_false_negative": {"n_labeled_supported": len(c3), "n_base_supported": len(kept),
                                        "lost": len(lost), "rate": len(lost) / len(kept) if kept else None,
                                        "by_type": types}}


def removed_audit(natural: list[dict]) -> list[dict]:
    """a2-v1 ✅인데 a3에서 ✅가 아닌 자연 주장과 a2-v1 출처 문단에서 못 찾은 이름(재현율 손실 원인 감사용)."""
    out = []
    for r in annotate(natural):
        if r["base"] == "supported" and r["exp"] != "supported":
            idx = r["base_idx"]
            out.append({"cid": r["cid"], "label": r.get("label"), "text": r.get("text"),
                        "missing": (r.get("missing") or [[]] * (idx + 1))[idx] if idx is not None else []})
    return out
