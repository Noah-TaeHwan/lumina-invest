# tests/evidence/test_a3.py
"""A-3 평가(A-3 spec 5절): 통제 주장 배정·검사, 두 정책(a2-v1 대 a3-subject-exp) 짝 비교 관문. 외부 호출 없음."""
from dataclasses import fields

import pytest

from app.services.evidence import runner as rn
from lab.evidence import a3


def test_policies_are_the_product_constants():
    assert a3.BASE is rn.A2_V1 and a3.EXP is rn.A3_SUBJECT
    assert a3.policy_fields(a3.EXP) == {"version": "a3-subject-exp", "tau_s": 0.85, "tau_c": 0.35, "theta_low": None,
                                        "theta_high": 0.95, "subject_check": True}
    assert set(a3.POLICY_FIELDS) <= {f.name for f in fields(rn.Policy)}


def test_assignment_cycles_subtypes_by_sorted_qid():
    qids = [f"c-q{i}" for i in range(1, 7)] + ["a-q1"]
    got = [a3.assigned(q, qids) for q in sorted(qids)]
    assert len(a3.SWAP_SUBTYPES) == 5 and a3.SWAP_SUBTYPES[-1] == a3.IN_PASSAGE
    assert [g[0] for g in got] == [a3.SWAP_SUBTYPES[i % 5] for i in range(7)]
    assert [g[1] for g in got] == [a3.NOTATION_TYPES[i % 3] for i in range(7)]


PS = ["당사는 2006년 품질경영시스템 인증을 획득하였습니다.", "커피 부문은 커피와 커피머신을 판매합니다.",
      "에프엔씨엔터테인먼트(이하 'FNC')는 2006년 설립되었습니다."]


@pytest.mark.parametrize("true,variant,ok", [
    ("커피 부문은 커피머신을 판매한다.", "건설 부문은 커피머신을 판매한다.", True),
    ("대원산업은 2006년 인증을 획득했다.", "고려제강은 2006년 인증을 획득했다.", True),
    ("커피 부문은 커피머신을 판매한다.", "커피 부문은 커피머신을 판매한다.", False),        # 같음
    ("대원산업은 2006년 인증을 획득했다.", "고려제강은 2007년 인증을 획득했다.", False),     # 숫자도 바뀜
    ("대원산업은 2006년 인증을 획득했다.", "커피는 2006년 인증을 획득했다.", False),         # 새 이름이 문단에 있음
])
def test_check_swap(true, variant, ok):
    assert a3.check_swap(true, variant, PS, ("고려제강",))[0] is ok


@pytest.mark.parametrize("true,variant,ok", [
    ("커피 부문은 커피머신을 판매한다.", "커피 부문은 커피머신을 판매한다.", False),
    ("대원산업은 2006년 인증을 획득했다.", "에프엔씨엔터테인먼트는 2006년 인증을 획득했다.", True),   # 문단에 있는 다른 이름
    ("대원산업은 2006년 인증을 획득했다.", "고려제강은 2006년 인증을 획득했다.", False),            # 문단에 없는 이름
])
def test_check_swap_in_passage(true, variant, ok):
    """'문단 안 교체': 같은 문단 묶음에 있는 다른 이름으로 바꾼다. 주체 확인을 통과하므로 JEV만 막을 수 있다(약점 측정)."""
    assert a3.check_swap(true, variant, PS, ("고려제강",), subtype=a3.IN_PASSAGE)[0] is ok


def test_check_swap_does_not_use_subject_candidates():
    """교체 검사는 후보 추출과 독립(후보 추출이 놓친 교체를 버리면 정확도가 부풀려진다)."""
    import inspect
    assert "subject_groups(" not in inspect.getsource(a3.check_swap)


@pytest.mark.parametrize("true,text,ntype,ok", [
    ("한빛소재와 계약했다.", "㈜한빛소재와 계약했다.", "법인 표기", True),
    ("한빛소재와 계약했다.", "한빛소재 주식회사와 계약했다.", "법인 표기", True),
    ("고려제강과 거래한다.", "고려 제강과 거래한다.", "띄어쓰기", True),
    ("고려제강과 거래한다.", "고려제강과 거래했다.", "띄어쓰기", False),                  # 이름 말고 다른 곳이 바뀜
    ("에프엔씨엔터테인먼트는 2006년 설립됐다.", "FNC는 2006년 설립됐다.", "영문·약칭", True),  # 문단 안 정의
    ("에프엔씨엔터테인먼트는 2006년 설립됐다.", "FNCE는 2006년 설립됐다.", "영문·약칭", False),  # 문단에 없는 약칭
    ("한빛소재와 계약했다.", "한빛소재와 계약했다.", "법인 표기", False),
])
def test_check_notation(true, text, ntype, ok):
    assert a3.check_notation(true, text, ntype, PS)[0] is ok


def _row(cid, *, y=None, s=(0.9,), c=(0.0,), valid=(True,), subj=(True,), cluster=None, variant=None,
         expected=None, lex=0.3, high_ok=False, best=0):
    qid = cid.rsplit("-", 1)[0]
    return {"cid": cid, "qid": qid, "cluster": cluster if cluster is not None else qid, "y": y, "s": list(s),
            "c": list(c), "valid": list(valid), "subj": list(subj), "lex": lex, "high_ok": high_ok, "best": best,
            "variant": variant, "expected": expected}


def test_decide_uses_shared_product_functions():
    r = _row("k-q1-n1", y=1, subj=(False,))
    assert a3.decide(r, a3.BASE)[0] == "supported" and a3.decide(r, a3.EXP)[0] == "no_evidence"
    hi = _row("k-q1-n2", y=1, lex=0.99, high_ok=True, subj=(False,))
    assert a3.decide(hi, a3.BASE)[3] == "lex_high" and a3.decide(hi, a3.EXP)[3] == "jev"


def _swaps(n, caught):
    return [_row(f"k{i}-q1-c2", cluster=i, variant="주체 교체:회사", expected="not_supported", subj=(i >= caught,))
            for i in range(n)]


def _natural(n_pos, lost, n_neg=0, neg_supported=0):
    pos = [_row(f"p{i}-q1-n1", y=1, cluster=i, subj=(i >= lost,)) for i in range(n_pos)]
    neg = [_row(f"n{i}-q1-n1", y=0, cluster=1000 + i, s=(0.9 if i < neg_supported else 0.1,)) for i in range(n_neg)]
    return pos + neg


def test_gates_pass_when_swaps_caught_and_recall_kept():
    # 교체 160건 중 a2-v1은 전부 ✅(오답), a3는 155건 ❔. 지지 주장 200건 중 2건만 잃는다
    g = a3.check_gates(_natural(200, 2, n_neg=20, neg_supported=5), _swaps(160, 155), n_boot=200)
    assert g["h_swap"]["exp_accuracy"] == pytest.approx(155 / 160) and g["h_swap"]["base_accuracy"] == 0
    assert g["h_swap"]["pass"] and g["h_recall"]["pass"] and g["h_prec"]["pass"]
    assert g["h_recall"]["loss"] == pytest.approx(2 / 200)
    assert g["h_prec"]["predicted"] == 198 + 5 and g["h_prec"]["diff"]["point"] < 0  # 정답 ✅ 2건이 빠져 조금 내려간다


def test_precision_gate_is_non_inferiority_against_a2_v1():
    """H-prec는 절대 0.90이 아니라 a2-v1 대비 비열등(차이 95% 하한 ≥ −0.02). 빠지는 ✅가 정답이면 정밀도가 내려간다."""
    nat = _natural(200, 0, n_neg=40, neg_supported=40)  # a2-v1 정밀도 200/240
    lost = [dict(r, subj=[False]) for r in nat[:60]]   # a3가 정답 ✅ 60건을 뺀다 → 140/180
    g = a3.check_gates(lost + nat[60:], _swaps(160, 160), n_boot=200)
    assert g["h_prec"]["precision"] == pytest.approx(140 / 180) and g["h_prec"]["base_precision"] == pytest.approx(200 / 240)
    assert g["h_prec"]["diff"]["point"] < 0 and not g["h_prec"]["pass"]
    assert a3.GATES["h_prec"] == {"diff_lo_min": -0.02, "min_predicted": 150}


def test_in_passage_swaps_are_reported_but_not_gated():
    sw = _swaps(160, 155) + [_row(f"z{i}-q1-c2", cluster=500 + i, variant=f"주체 교체:{a3.IN_PASSAGE}",
                                  expected="not_supported") for i in range(30)]
    g = a3.check_gates(_natural(200, 2), sw, n_boot=100)
    assert g["h_swap"]["n"] == 160 and g["in_passage_swap"]["n"] == 30
    assert g["in_passage_swap"]["exp_accuracy"] == 0.0 == g["in_passage_swap"]["base_accuracy"]


def test_explore_loss_rate_and_criterion():
    ok = a3.explore_summary(_natural(200, 2))
    assert ok["loss_rate"] == pytest.approx(2 / 200) and ok["proceed"] is True
    bad = a3.explore_summary(_natural(200, 4))
    assert bad["loss_rate"] == pytest.approx(0.02) and bad["proceed"] is False
    assert a3.EXPLORE_MAX_LOSS == 0.015


def test_gates_fail_on_recall_loss_and_small_swap_sample():
    g = a3.check_gates(_natural(200, 30), _swaps(40, 40), n_boot=200)
    assert not g["h_recall"]["pass"] and g["h_recall"]["loss"] == pytest.approx(0.15)
    assert g["h_swap"]["descriptive_only"] and not g["h_swap"]["pass"]


def test_precision_gate_is_descriptive_below_min_predicted():
    g = a3.check_gates(_natural(100, 0), _swaps(160, 160), n_boot=100)
    assert g["h_prec"]["descriptive_only"] and not g["h_prec"]["pass"]


def test_recommendation_needs_all_three():
    ok = {"h_swap": {"pass": True}, "h_recall": {"pass": True}, "h_prec": {"pass": True}}
    assert a3.recommendation(ok)["switch_default"] is True
    for k in ok:
        bad = {**ok, k: {"pass": False}}
        assert a3.recommendation(bad)["switch_default"] is False


def test_controlled_summary_and_notation_false_negatives():
    rows = [_row("a-q1-c1", variant="의역", expected="supported"),
            _row("a-q1-c3", variant="표기 변형:법인 표기", expected="supported", subj=(False,)),
            _row("b-q1-c3", variant="표기 변형:띄어쓰기", expected="supported"),
            _row("b-q1-c2", variant="주체 교체:회사", expected="not_supported", subj=(False,))]
    rows[1]["label"] = rows[2]["label"] = "supported"
    out = a3.controlled_summary(rows)
    assert out["accuracy"]["exp"]["주체 교체:회사"] == 1.0 and out["accuracy"]["base"]["주체 교체:회사"] == 0.0
    nf = out["notation_false_negative"]
    assert nf["n_base_supported"] == 2 and nf["lost"] == 1 and nf["rate"] == 0.5
    assert nf["by_type"]["법인 표기"] == {"n": 1, "lost": 1}


def test_removed_audit_lists_missing_subjects():
    r = _row("k-q1-n1", y=1, subj=(False,))
    r.update(text="고려제강은 인증을 획득했다.", label="supported", missing=[["고려제강"]])
    out = a3.removed_audit([r, _row("k-q1-n2", y=1)])
    assert out == [{"cid": "k-q1-n1", "label": "supported", "text": "고려제강은 인증을 획득했다.",
                    "missing": ["고려제강"]}]


def test_h_swap_reports_accuracy_by_subtype():
    """허용 목록 밖 교체(일반명사 제품·부문)를 놓치는 대가를 드러내도록 하위 유형별 정확도를 따로 낸다."""
    sw = [_row(f"a{i}-q1-c2", cluster=i, variant="주체 교체:회사", expected="not_supported", subj=(False,))
          for i in range(10)]
    sw += [_row(f"b{i}-q1-c2", cluster=100 + i, variant="주체 교체:제품·브랜드", expected="not_supported",
                subj=(i < 2,)) for i in range(10)]
    by = a3.check_gates(_natural(200, 0), sw, n_boot=50)["h_swap"]["by_subtype"]
    assert by["회사"] == {"n": 10, "exp_accuracy": 1.0, "base_accuracy": 0.0}
    assert by["제품·브랜드"] == {"n": 10, "exp_accuracy": 0.8, "base_accuracy": 0.0}
