"""lab 시험 공통 설정."""
import pytest

from lab.jev_gate import gate


@pytest.fixture(autouse=True)
def dummy_input_price(monkeypatch):
    """실제 입력 토큰 단가는 비공개라 시험은 임의의 값으로 돈다. 환경에 단가가 없어도 같은 결과가 나온다."""
    monkeypatch.setattr(gate, "PRICE_PER_INPUT_TOKEN", 1e-6)
