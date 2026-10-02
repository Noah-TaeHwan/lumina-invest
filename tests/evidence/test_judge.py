# tests/evidence/test_judge.py
from app.lib.jev import JevResult
from app.services.evidence import judge


class FakeClient:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def ask(self, state, questions, *, use_cache=True, tag=""):
        self.calls.append((state, sorted(questions), use_cache, tag))
        if self.fail:
            return JevResult("k", False, None, 1.0, 0, 2, "x")
        ans = {q: {"supports": 0.1 * int(q[1:]), "contradicts": 0.05, "says_nothing": 1 - 0.1 * int(q[1:]) - 0.05}
               for q in questions}
        return JevResult("k", True, ans, 1.0, 10, 1, None)


def test_state_and_questions_shape():
    st = judge.build_state("삼성전자", "주장", ["A", "B"])
    assert st == "[Company] 삼성전자\n[Claim] 주장\n[Passage 1] A\n[Passage 2] B"
    qs = judge.build_questions(3, only=[2])
    assert list(qs) == ["p2"] and qs["p2"]["type"] == "choice" and "[Passage 2]" in qs["p2"]["instructions"]
    assert set(qs["p2"]["criteria"]) == {"supports", "contradicts", "says_nothing"}


def test_batched_judge_one_request_and_score():
    c = FakeClient()
    j = judge.judge_claim(c, "회사", "주장", ["a", "b", "c"], tag="t")
    assert j.ok and j.requests == 1 and len(c.calls) == 1
    assert [round(x, 9) for x in j.s] == [0.1, 0.2, 0.3]
    assert abs(judge.jev_score(j) - 0.3) < 1e-9
    assert judge.passage_labels(j)[0] == "says_nothing"


def test_single_mode_sends_one_question_per_request_with_same_state():
    c = FakeClient()
    j = judge.judge_claim(c, "회사", "주장", ["a", "b"], single=True)
    assert j.requests == 2 and [q for _, q, _, _ in c.calls] == [["p1"], ["p2"]]
    assert c.calls[0][0] == c.calls[1][0]


def test_failure_scores_zero():
    j = judge.judge_claim(FakeClient(fail=True), "회사", "주장", ["a", "b"])
    assert not j.ok and judge.jev_score(j) == 0.0 and j.s == [0.0, 0.0]
