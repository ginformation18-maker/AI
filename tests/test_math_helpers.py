import pytest
from mt5_agents.math_helpers import floor_volume, price_to_tick


def test_volume_round_down_never_round_up():
    assert floor_volume(0.076, 0.01, 20.0, 0.01) == pytest.approx(0.07)
    assert floor_volume(0.009, 0.01, 20, 0.01) == 0
    assert floor_volume(0.1, 0.1, 1.0, 0.1) == pytest.approx(0.1)
    assert floor_volume(2, 0.01, 0.5, 0.01) == pytest.approx(0.5)


def test_tick_rounding():
    assert price_to_tick(2340.031, 0.01, "down", 2) == pytest.approx(2340.03)
    assert price_to_tick(2340.031, 0.01, "up", 2) == pytest.approx(2340.04)
