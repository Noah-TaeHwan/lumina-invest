# tests/evidence/test_cli_data.py
import pytest

from lab.evidence import __main__ as cli


def _q(cat, text="질문?"):
    return {"category": cat, "question": text}


def test_merge_questions_orders_by_category_and_validates():
    rows = [_q(c, f"{c} 질문?") for c in reversed(cli.CATEGORIES)]
    out = cli.merge_questions(rows, "000001", "tune")
    assert [r["qid"] for r in out] == [f"000001-q{i}" for i in range(1, 7)]
    assert out[0]["category"] == "사업 개요" and out[0]["split"] == "tune"
    with pytest.raises(ValueError):
        cli.merge_questions(rows[:5], "000001", "tune")
    with pytest.raises(ValueError):
        cli.merge_questions(rows[:5] + [_q("엉뚱한 범주")], "000001", "tune")


def test_natural_claims_take_first_five_sentences():
    ans = [{"qid": "x-q1", "answer": "하나입니다. 둘입니다. 셋입니다. 넷입니다. 다섯입니다. 여섯입니다."}]
    out = cli.natural_claims(ans)
    assert [c["cid"] for c in out] == [f"x-q1-n{i}" for i in range(1, 6)]
    assert out[0] == {"cid": "x-q1-n1", "qid": "x-q1", "source": "natural", "text": "하나입니다.",
                      "variant": None, "expected": None}


def test_variant_rotation_and_controlled_check():
    assert [cli.variant_for(i) for i in range(6)] == cli.VARIANTS + [cli.VARIANTS[0]]
    row = {"qid": "x-q1", "true_text": "DX 매출은 100억원이다.", "variant_text": "DX 매출은 900억원이다.",
           "variant_type": "숫자 변경"}
    assert cli.check_controlled(row, ["DX 매출액: 100"], ()) == (True, "ok")
    bad = dict(row, variant_text="DX 매출은 100억원이고 2025년이다.")
    assert cli.check_controlled(bad, ["2025년 DX 매출액: 100"], ())[0] is False
    same = dict(row, variant_text=row["true_text"])
    assert cli.check_controlled(same, ["x"], ())[1] == "variant equals true text"
