import pytest
from app.agents.tools import calculator


def test_calculator_basic():
    assert calculator.invoke({"expression":"20/100*500"}) == "100.0"


def test_calculator_rejects_code():
    with pytest.raises(ValueError):
        calculator.invoke({"expression":"__import__('os').system('whoami')"})
