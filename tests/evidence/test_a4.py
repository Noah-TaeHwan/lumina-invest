# tests/evidence/test_a4.py
"""A-4 평가(A-4 spec 4·5·6절): 9칸 순환 배정, A-3 확인 세트 군집 반분, 1a 실측, 탐색 격자·선택·진행 기준, 관문, 보조 팔,
하위 유형 다섯 정책, 민감도, 짝 불일치, 교체어 태그, 후속 실패 처리, R2 폴백 단조성, 운영 비용. 외부 호출 없음."""
from dataclasses import fields

import pytest

from app.services.evidence import runner as rn
from app.services.evidence import subject_a4 as sa
from lab.evidence import a3, a4

SAME = {"same_subject": 0.9, "different_subject": 0.05, "unclear": 0.05}
DIFF = {"same_subject": 0.1, "different_subject": 0.8, "unclear": 0.1}
UNCL = {"same_subject": 0.2, "different_subject": 0.3, "unclear": 0.5}


def row(cid, *, y=1, s=(0.9,), c=(0.0,), valid=None, code=None, q=0, variant=None, label=None, cluster=0, lex=0.1,
        high_ok=False, best=None, qid=None, tag=None, q_failed=False, set_="a3-design"):
    """평가 행. code: 팔별 ①c 마스크(기본 모두 통과), q: 문단별 후속 확률(0이면 첫 문단 SAME)."""
    n = len(s)
    code = code or {}
    masks = {arm: list(code.get(arm, [True] * n)) for arm in ("c", "a", "a3", "a3x0")}
    return {"cid": cid, "qid": qid or cid.rsplit("-", 1)[0], "cluster": cluster, "y": y, "s": list(s), "c": list(c),
            "valid": list(valid or [True] * n), "code": masks, "q": {0: SAME} if q == 0 else q, "q_failed": q_failed,
            "variant": variant, "expected": "not_supported" if variant else None,
            "label": label or ("supported" if y else "no_evidence"), "lex": lex, "high_ok": high_ok, "best": best,
            "swap_tag": tag, "set": set_}


def test_policies_are_the_product_constants():
    assert a4.BASE is rn.A2_V1 and a4.EXP is rn.A4_SUBJECT and a4.A3 is rn.A3_SUBJECT
    assert set(a4.POLICY_FIELDS) <= {f.name for f in fields(rn.Policy)}
    assert a4.policy_fields(a4.EXP)["subject_question"] is True
    assert a4.SEED == 20261305 and a4.HALF_SEED == 20261306 and a4.RANDOM_N == 45 and a4.HALF_N == 20
    assert a4.SEED not in (20261002, 20261103, 20261204) and a4.PRIOR_STUDIES == ("a1", "a2", "a3")
    assert a4.GATES == a3.GATES  # 관문 값은 A-3와 같다(spec 5.1)
    assert a4.TAU_D_GRID == (0.2, 0.3, 0.4, 0.5, 0.6, 0.7) and a4.SIGNALS == sa.SIGNALS
    assert (a4.EXPLORE_CAP_MAX, a4.TOKEN_CAP_MAX) == (3_000_000, 9_600_000)


# --- 배정·반분 ----------------------------------------------------------------------------------------------
def test_assignment_cycles_nine_slots():
    qids = [f"c-q{i}" for i in range(1, 12)]
    got = [a4.assigned(q, qids)[0] for q in sorted(qids)]
    assert a4.SWAP_CYCLE == ("회사", "부문·사업", "제품·브랜드", "거래상대·자회사") * 2 + (a4.IN_PASSAGE,)
    assert got == [a4.SWAP_CYCLE[i % 9] for i in range(11)] and got[8] == a4.IN_PASSAGE
    assert [a4.assigned(q, qids)[1] for q in sorted(qids)] == [a3.NOTATION_TYPES[i % 3] for i in range(11)]
    assert a4.SWAP_SUBTYPES == a3.SWAP_SUBTYPES  # 하위 유형 이름은 A-3와 같다(배정 비율만 바뀐다)


def test_half_split_is_cluster_level_seeded_and_deterministic():
    clusters = [["a", "b", "c"]] + [[f"x{i}"] for i in range(30)] + [["y1", "y2"], ["z1", "z2", "z3", "z4"]]
    d1, k1 = a4.split_halves(clusters)
    d2, k2 = a4.split_halves(clusters)
    assert (d1, k1) == (d2, k2)
    assert sum(len(c) for c in d1) >= a4.HALF_N and sum(len(c) for c in d1[:-1]) < a4.HALF_N  # 20개에 이를 때까지
    assert sorted(x for c in d1 + k1 for x in c) == sorted(x for c in clusters for x in c)
    assert all(c in clusters for c in d1 + k1)  # 군집을 쪼개지 않는다
    assert a4.split_halves(clusters, seed=1) != (d1, k1)


# --- 판정 팔 -----------------------------------------------------------------------------------------------
def test_arm_decisions_on_one_row():
    r = row("k-q1-n1", s=(0.9, 0.95), valid=[True, True], code={"c": [True, False], "a3": [False, False]},
            q={0: SAME, 1: DIFF})
    got = {arm: a4.decide_row(r, arm, "p_diff", 0.5)[0] for arm in a4.ARMS}
    assert got == {"a2-v1": "supported", "a3": "no_evidence", "A": "supported", "B": "supported", "C": "supported",
                   "a3x0": "supported"}
    assert a4.decide_row(r, "C", "p_diff", 0.5)[1] == 0 and a4.decide_row(r, "B", "p_diff", 0.5)[1] == 0
    r2 = dict(r, q={0: UNCL, 1: DIFF})
    assert a4.decide_row(r2, "C", "p_diff", 0.5)[0] == "supported"
    assert a4.decide_row(r2, "C", "p_diff_unclear", 0.5)[0] == "no_evidence"


def test_lex_high_row_needs_question_on_best():
    r = row("k-q1-n1", s=(0.1, 0.2), lex=0.99, high_ok=True, best=1, q={1: SAME})
    assert a4.decide_row(r, "a2-v1", "p_diff", 0.5)[3] == "lex_high"
    assert a4.decide_row(r, "C", "p_diff", 0.5)[:2] == ("supported", 1)
    r = dict(r, q={1: DIFF})
    assert a4.decide_row(r, "C", "p_diff", 0.5)[3] == "jev" and a4.decide_row(r, "C", "p_diff", 0.5)[0] == "no_evidence"


def test_followup_candidates_include_lex_high_best():
    r = row("k", s=(0.9, 0.2, 0.86), valid=[True, True, False], lex=0.99, high_ok=True, best=1)
    assert a4.followup_candidates(r) == [0, 1]
    assert a4.followup_candidates(dict(r, high_ok=False)) == [0]
    assert a4.followup_candidates(dict(r, lex=0.5)) == [0]


# --- 1a 실측(호출 0) -----------------------------------------------------------------------------------------
def test_measure_1a_counts_candidates_and_recomputes_caps():
    rows = [row("a", s=(0.9, 0.95, 0.1)), row("b", s=(0.9, 0.1, 0.1)), row("c", s=(0.1, 0.1, 0.1)),
            row("d", s=(0.9, 0.9, 0.9))]
    m = a4.measure(rows)
    assert (m["claims"], m["followup"], m["followup_ratio"]) == (4, 3, 0.75)
    assert m["m_mean"] == pytest.approx(2.0) and m["m_p95"] == pytest.approx(2.9)
    assert m["explore_estimate_per_version"] == round(3 * (100 + 2.0 * 645))
    assert m["followup_tokens_p95_estimate"] == round(100 + 2.9 * 645)
    assert a4.explore_estimate(940, 1.33, versions=3) == round(940 * (100 + 1.33 * 645) * 3)
    assert a4.explore_estimate(940, 1.33, versions=1, all_passages=True) == round(940 * (100 + 8 * 645))
    assert a4.confirm_estimate(1400, 1400, 0.65, 2000) == 1400 * 5070 + round(1400 * 0.75 * 2000)


def test_explore_cap_stops_above_max():
    assert a4.explore_cap(940, 1.33, versions=1) == round(940 * (100 + 1.33 * 645))
    with pytest.raises(SystemExit, match="3,000,000"):
        a4.explore_cap(3000, 2.0, versions=3)
    with pytest.raises(SystemExit, match="9,600,000"):
        a4.check_confirm_cap(9_600_001)
    assert a4.check_confirm_cap(9_000_000) == 9_000_000


def test_prompt_versions_capped_at_three():
    a4.check_prompt_versions(["v1", "v2", "v3"])
    with pytest.raises(SystemExit, match="3"):
        a4.check_prompt_versions(["v1", "v2", "v3", "v4"])


# --- 탐색 격자·선택·진행 기준 ---------------------------------------------------------------------------------
def _explore_rows():
    nat = [row(f"t{i}-q1-n1", cluster=i, set_="a2-tune") for i in range(10)]
    nat += [row(f"d{i}-q1-n1", cluster=100 + i) for i in range(10)]
    nat += [row(f"k{i}-q1-n1", cluster=200 + i, set_="a3-check") for i in range(10)]
    nat.append(row("t99-q1-n1", cluster=99, set_="a2-tune", q={0: UNCL}))  # P(diff)+P(unclear)이면 잃는 지지 주장
    sw = [row(f"d{i}-q1-c2", y=0, cluster=100 + i, variant="주체 교체:부문·사업", label="no_evidence", q={0: DIFF})
          for i in range(5)]
    sw += [row(f"d{i}-q2-c2", y=0, cluster=100 + i, variant="주체 교체:제품·브랜드", label="no_evidence",
               q={0: UNCL}) for i in range(5)]
    sw += [row(f"k{i}-q1-c2", y=0, cluster=200 + i, variant="주체 교체:부문·사업", label="no_evidence",
               set_="a3-check", q={0: DIFF}) for i in range(5)]
    return nat, sw


def test_grid_reports_every_combination_by_half():
    nat, sw = _explore_rows()
    grid = a4.explore_grid(nat, sw)
    assert len(grid) == len(a4.SIGNALS) * len(a4.TAU_D_GRID)
    g = next(x for x in grid if x["signal"] == "p_diff" and x["tau_d"] == 0.5)
    assert set(g["arms"]) == {"C", "A", "B"}
    assert g["arms"]["C"]["design"]["loss"] == 0 and g["arms"]["C"]["design"]["swap_accuracy"] == 0.5
    assert g["arms"]["C"]["check"]["swap_accuracy"] == 1.0
    u = next(x for x in grid if x["signal"] == "p_diff_unclear" and x["tau_d"] == 0.5)
    assert u["arms"]["C"]["design"]["loss"] == 1 and u["arms"]["C"]["design"]["swap_accuracy"] == 1.0


def test_choose_uses_design_only_and_breaks_ties():
    nat, sw = _explore_rows()
    grid = a4.explore_grid(nat, sw)
    # 설계용 손실률 1/21 > 1%라 P(diff)+P(unclear)는 탈락, P(diff) 중 정확도 동률 → 큰 τ_d
    assert a4.choose(grid) == {"signal": "p_diff", "tau_d": 0.7}
    tied = [{"signal": s, "tau_d": t, "arms": {"C": {"design": {"loss_rate": 0.0, "swap_accuracy": 0.9}}}}
            for s in a4.SIGNALS for t in (0.3, 0.5)]
    assert a4.choose(tied) == {"signal": "p_diff", "tau_d": 0.5}
    bad = [{"signal": "p_diff", "tau_d": 0.5, "arms": {"C": {"design": {"loss_rate": 0.02, "swap_accuracy": 1.0}}}}]
    assert a4.choose(bad) is None  # 해당 없음 → 멈춤
    # 점검용 값이 달라도 선택은 바뀌지 않는다
    flipped = [dict(x, arms={"C": dict(x["arms"]["C"], check={"swap_accuracy": 0.0})}) for x in tied]
    assert a4.choose(flipped) == a4.choose(tied)


def test_max_explore_loss_rule():
    assert a4.max_explore_loss(397) == 3 and a4.max_explore_loss(400) == 4 and a4.max_explore_loss(99) == 0


def test_proceed_criteria():
    nat, sw = _explore_rows()
    ok = a4.proceed(nat, sw, "p_diff", 0.5)
    assert ok["recall"]["denominator"] == 31 and ok["recall"]["lost"] == 0 and ok["recall"]["pass"]
    assert ok["swap"]["n"] == 5 and ok["swap"]["accuracy"] == 1.0 and ok["swap"]["pass"]
    assert ok["direction"]["부문·사업"]["C"] == 1.0 and ok["direction"]["pass"] is False  # 제품·브랜드는 a3와 같음(0)
    assert ok["proceed"] is False
    nat2 = nat + [row("k99-q1-n1", cluster=299, set_="a3-check", q={0: DIFF})]
    lost = a4.proceed(nat2, sw, "p_diff", 0.5)
    assert lost["recall"]["lost"] == 1 and lost["recall"]["max_lost"] == 0 and not lost["recall"]["pass"]


def test_audit_classifies_mechanical_cause_and_leaves_ai_cause_open():
    nat = [row("a-q1-n1", code={"c": [False]}), row("b-q1-n1", q={0: DIFF}), row("c-q1-n1", q={0: UNCL})]
    sw = [row("d-q1-c2", y=0, variant="주체 교체:회사", q={0: UNCL})]
    au = a4.audit(nat, sw, "p_diff_unclear", 0.5)
    assert [(x["cid"], x["cause"]) for x in au["natural"]] == [("a-q1-n1", "가"), ("b-q1-n1", "나"), ("c-q1-n1", "다")]
    assert all(x["ai_cause"] is None for x in au["natural"]) and set(a4.CAUSES) == {"가", "나", "다", "라", "마", "바"}
    au2 = a4.audit(nat, sw, "p_diff", 0.5)
    assert [(x["cid"], x["cause"], x["p_unclear"]) for x in au2["swaps"]] == [("d-q1-c2", "다", 0.5)]


# --- 확인 관문 ------------------------------------------------------------------------------------------------
def _gate_rows(n_nat=160, n_sw=160, lose=0, miss=0):
    nat = [row(f"n{i}-q1-n1", cluster=i % 40, qid=f"n{i}-q1") for i in range(n_nat)]
    for r in nat[:lose]:
        r["q"] = {0: DIFF}
    subs = ("회사", "부문·사업", "제품·브랜드", "거래상대·자회사")
    sw = [row(f"s{i}-q1-c2", y=0, cluster=i % 40, qid=f"s{i}-q1", variant=f"주체 교체:{subs[i % 4]}",
              q={0: DIFF}, tag="고유명" if i % 2 else "일반명사")
          for i in range(n_sw)]
    for r in sw[:miss]:
        r["q"] = {0: SAME}
        r["code"]["c"] = [True]
    return nat, sw


def test_gates_pass_and_report_five_policies():
    nat, sw = _gate_rows()
    inside = [row("p1-q1-c2", y=0, variant=f"주체 교체:{a4.IN_PASSAGE}", q={0: DIFF})]
    g = a4.check_gates(nat, sw + inside, "p_diff", 0.5, n_boot=50)
    assert g["h_swap"]["pass"] and g["h_recall"]["pass"] and g["h_prec"]["descriptive_only"] is False
    assert g["h_swap"]["n"] == 160 and g["in_passage_swap"]["n"] == 1
    assert set(g["h_swap"]["by_subtype"]["부문·사업"]["accuracy"]) == set(a4.FIVE)
    assert g["h_swap"]["by_subtype"]["부문·사업"]["accuracy"]["a3"] == 0.0
    d = g["h_swap"]["by_subtype"]["부문·사업"]["discordant"]
    assert d == {"c_only": 40, "a3_only": 0}
    assert set(g["h_swap"]["by_tag"]) == {"고유명", "일반명사"}
    assert set(g["arms"]) == {"a3", "A", "B"} and "pass" in g["arms"]["B"]["h_swap"]
    assert set(g["in_passage_swap"]["accuracy"]) == set(a4.FIVE)


def test_gate_fail_and_descriptive_cases():
    nat, sw = _gate_rows(miss=30)
    g = a4.check_gates(nat, sw, "p_diff", 0.5, n_boot=50)
    assert not g["h_swap"]["pass"]
    nat, sw = _gate_rows(n_sw=100)
    g = a4.check_gates(nat, sw, "p_diff", 0.5, n_boot=50)
    assert g["h_swap"]["descriptive_only"] and not g["h_swap"]["pass"]


def test_sensitivity_drops_supported_and_disputed_c2():
    nat, sw = _gate_rows()
    sw[0]["label"], sw[1]["label"] = "supported", "disputed"
    g = a4.check_gates(nat, sw, "p_diff", 0.5, n_boot=50)
    assert g["h_swap"]["n"] == 160 and g["h_swap_sensitivity"]["n"] == 158
    assert "pass" not in g["h_swap_sensitivity"]  # 관문 아님


def test_followup_failures_excluded_pairwise_and_over_one_percent_is_descriptive():
    nat, sw = _gate_rows()
    nat[0]["q_failed"] = True
    nat[0]["q"] = None
    g = a4.check_gates(nat, sw, "p_diff", 0.5, n_boot=50)
    assert g["followup_failed_excluded"] == 1 and g["h_recall"]["positive"] == 159
    assert g["h_swap"]["pass"]  # 1/320 ≤ 1%
    for r in nat[:5]:
        r["q_failed"], r["q"] = True, None
    g = a4.check_gates(nat, sw, "p_diff", 0.5, n_boot=50)
    assert g["followup_failed_excluded"] == 5 and g["judge_failed_over"]
    assert all(g[k]["descriptive_only"] and not g[k]["pass"] for k in ("h_swap", "h_recall", "h_prec"))


def test_recommendation_never_uses_arms_and_checks_cost():
    nat, sw = _gate_rows()
    g = a4.check_gates(nat, sw, "p_diff", 0.5, n_boot=50)
    g["h_prec"]["pass"] = True
    assert a4.recommendation(g, {"pass": True})["switch_default"] is True
    assert a4.recommendation(g, {"pass": False})["case"] == "all_pass_cost_over"
    bad = dict(g, h_swap=dict(g["h_swap"], pass_=False, **{"pass": False}))
    bad["arms"] = {"B": {"h_swap": {"pass": True}, "h_recall": {"pass": True}, "h_prec": {"pass": True}}}
    rec = a4.recommendation(bad, {"pass": True})
    assert rec["switch_default"] is False and rec["case"] == "h_swap_fail"
    assert a4.recommendation(dict(g, judge_failed_over=True), {"pass": True})["case"] == "judge_failed"


def test_cost_summary():
    recs = [{"source": "natural", "main_tokens": 4000, "followup_tokens": 1000, "m": 1, "latency_ms": 500,
             "followup_latency_ms": 300}, {"source": "natural", "main_tokens": 4000, "followup_tokens": 0, "m": 0,
                                           "latency_ms": 9000, "followup_latency_ms": 0}]
    c = a4.cost_summary(recs)
    assert c["followup_ratio"] == 0.5 and c["token_increase"] == pytest.approx(1000 / 8000)
    assert c["pass"] is True and c["over_deadline_share"] == 0.5
    recs[1]["followup_tokens"] = 2000
    assert a4.cost_summary(recs)["pass"] is False  # +37.5% > 25%


# --- R2 폴백 단조성 ------------------------------------------------------------------------------------------
def test_fallback_monotonicity_r2():
    assert a4.fallback_violations("tau_s", tuned=0.90, fallback=0.85) > 0  # A-2 역전 사례
    assert a4.fallback_violations("tau_s", tuned=0.85, fallback=0.90) == 0
    assert a4.fallback_violations("tau_d", tuned=0.5, fallback=0.6) > 0
    assert a4.fallback_violations("tau_d", tuned=0.5, fallback=0.4) == 0
    with pytest.raises(SystemExit, match="R2"):
        a4.check_registrable({"status": "registered", "tau_d": 0.5, "signal": "p_diff",
                              "fallbacks": [{"kind": "tau_d", "tuned": 0.5, "fallback": 0.6}]})
    a4.check_registrable({"status": "registered", "tau_d": 0.5, "signal": "p_diff",
                          "fallbacks": [{"kind": "tau_d", "tuned": 0.5, "fallback": 0.4}]})
    with pytest.raises(SystemExit, match="tau_d"):
        a4.check_registrable({"status": "registered", "tau_d": None, "signal": "p_diff", "fallbacks": []})
