# app/services/evidence/generate.py
"""질문과 근거 문단을 명시적으로 받아 한국어 답변을 한 번 생성한다(평가·채팅 연결 공용)."""
from __future__ import annotations

import hashlib
import json

SYSTEM = ("당신은 상장사 사업보고서를 읽고 질문에 답하는 리서치 보조입니다. "
          "아래 [문단]에 적힌 내용만 근거로 한국어 3~5문장으로 답하세요. 문단에 없는 내용은 추측하지 마세요.")
OPTIONS = {"temperature": 0, "seed": 20261002}
PROMPT_SHA = hashlib.sha256((SYSTEM + json.dumps(OPTIONS, sort_keys=True)).encode()).hexdigest()


def build_messages(company: str, question: str, passages: list[str]) -> list[dict]:
    """시스템 지시와 [회사]·[문단 n]·[질문]으로 된 사용자 메시지를 만든다."""
    ctx = "\n".join(f"[문단 {i}] {t}" for i, t in enumerate(passages, 1))
    return [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": f"[회사] {company}\n{ctx}\n\n[질문] {question}"}]


async def generate_answer(llm, model: str, company: str, question: str, passages: list[str]) -> str:
    """고정 옵션(temperature 0, seed)으로 한 번 생성한다."""
    return (await llm.chat(model, build_messages(company, question, passages), OPTIONS)).strip()
