# lab/evidence/baselines.py
"""spec 5절 기준선. 어느 것도 JEV 출력으로 학습하지 않는다."""
from __future__ import annotations

import re

import numpy as np

from app.services.evidence.numbers import number_check

NLI_MODEL = "MoritzLaurer/mDeBERTa-v3-base-xnli-multilingual-nli-2mil7"
LLM_SYSTEM = ("당신은 근거 판정자입니다. [문단]들 중 하나가 [주장]의 모든 사실을 그대로 뒷받침하는 정도를 "
              "0~100 사이 정수 하나로만 답하세요. 뒷받침하지 않거나 어긋나면 0에 가깝게 답하세요.")
LLM_OPTIONS = {"temperature": 0, "seed": 20261002, "num_ctx": 8192, "num_predict": 8}
_WS = re.compile(r"\s+")


def char_bigrams(text: str) -> set[str]:
    """공백을 지운 글자 바이그램(한국어 조사 변화에 덜 민감한 어휘 단위)."""
    t = _WS.sub("", text)
    return {t[i:i + 2] for i in range(len(t) - 1)}


def lex_score(claim: str, passages: list[str]) -> float:
    """B-lex: 숫자 확인을 통과한 문단에서 주장 바이그램 재현율의 최댓값."""
    cb = char_bigrams(claim)
    if not cb:
        return 0.0
    return max((len(cb & char_bigrams(p)) / len(cb) if number_check(claim, p) else 0.0) for p in passages)


def emb_score(claim_vec, passage_vecs) -> float:
    """B-emb: 코사인 유사도 최댓값."""
    q = np.asarray(claim_vec, dtype=float)
    m = np.asarray(passage_vecs, dtype=float)
    return float(np.max((m @ q) / (np.linalg.norm(m, axis=1) * np.linalg.norm(q) + 1e-12)))


def llm_messages(claim: str, passages: list[str]) -> list[dict]:
    """B-llm 프롬프트."""
    ctx = "\n".join(f"[문단 {i}] {t}" for i, t in enumerate(passages, 1))
    return [{"role": "system", "content": LLM_SYSTEM}, {"role": "user", "content": f"{ctx}\n\n[주장] {claim}\n점수:"}]


def parse_llm_score(text: str) -> float | None:
    """첫 정수(0~100)를 0~1로. 없거나 범위 밖이면 None."""
    m = re.search(r"\d+", text)
    if not m or int(m.group()) > 100:
        return None
    return int(m.group()) / 100


def nli_entailment(pairs: list[tuple[str, str]], revision: str) -> list[float]:
    """B-nli: (문단, 주장) 쌍의 entailment 확률. torch·transformers는 평가 환경에서만 import한다."""
    import torch
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    tok = AutoTokenizer.from_pretrained(NLI_MODEL, revision=revision)
    model = AutoModelForSequenceClassification.from_pretrained(NLI_MODEL, revision=revision).eval()
    dev = "mps" if torch.backends.mps.is_available() else "cpu"
    model.to(dev)
    ent = [i for i, l in model.config.id2label.items() if l.lower().startswith("entail")][0]
    out: list[float] = []
    with torch.no_grad():
        for i in range(0, len(pairs), 16):
            batch = pairs[i:i + 16]
            enc = tok([p for p, _ in batch], [c for _, c in batch], truncation="only_first", max_length=512,
                      padding=True, return_tensors="pt").to(dev)
            out += torch.softmax(model(**enc).logits, dim=-1)[:, ent].tolist()
    return out
