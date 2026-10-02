# tests/evidence/test_retrieve_generate.py
import asyncio

from app.services.evidence import generate, retrieve


def test_top_k_orders_by_cosine_and_breaks_ties_by_index():
    vecs = [[1, 0], [0, 1], [1, 0], [0.9, 0.1]]
    assert retrieve.top_k([1, 0], vecs, k=3) == [0, 2, 3]


def test_build_messages_numbers_passages():
    m = generate.build_messages("삼성전자", "주요 제품은?", ["DRAM", "TV"])
    assert m[0]["role"] == "system" and "[문단 1] DRAM\n[문단 2] TV" in m[1]["content"]
    assert m[1]["content"].endswith("[질문] 주요 제품은?")


def test_generate_answer_uses_fixed_options():
    seen = {}

    class LLM:
        async def chat(self, model, messages, options=None):
            seen.update(model=model, options=options)
            return "  답변입니다.  "

    out = asyncio.run(generate.generate_answer(LLM(), "llama3.2:1b", "삼성전자", "Q", ["P"]))
    assert out == "답변입니다." and seen["options"] == {"temperature": 0, "seed": 20261002, "num_ctx": 8192, "num_predict": 300}
    assert len(generate.PROMPT_SHA) == 64
