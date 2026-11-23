"""ATR, spread ratio, UTC-session and adverse-fill all affect research penalty."""
from datetime import datetime, timezone
import pytest
from mt5_agents.slippage import SlippagePenalty, adverse_fill, session_factor

ASIA = datetime(2026,11,20,2,0,tzinfo=timezone.utc)
NY_OPEN_SUMMER = datetime(2026,7,20,13,0,tzinfo=timezone.utc)
NY_OPEN_WINTER = datetime(2026,11,20,14,0,tzinfo=timezone.utc)


def price(model, *, spread_points=10, at_utc=ASIA, atr=10, point=.01):
    return model.price_penalty(atr=atr,point=point,spread_points=spread_points,at_utc=at_utc)


def test_adverse_penalty_both_sides_and_both_fills():
    m = SlippagePenalty(reference_spread_points=10)
    p = price(m)
    assert p == pytest.approx(.5)
    assert adverse_fill(3000,side="BUY",is_entry=True,penalty=p) == pytest.approx(3000.5)
    assert adverse_fill(3000,side="BUY",is_entry=False,penalty=p) == pytest.approx(2999.5)
    assert adverse_fill(3000,side="SELL",is_entry=True,penalty=p) == pytest.approx(2999.5)
    assert adverse_fill(3000,side="SELL",is_entry=False,penalty=p) == pytest.approx(3000.5)


def test_session_summer_winter_dst_and_wider_spread():
    m = SlippagePenalty(reference_spread_points=10)
    assert session_factor(NY_OPEN_SUMMER)[0] == "NEW_YORK_OPEN_ASSUMED"
    assert session_factor(NY_OPEN_WINTER)[0] == "NEW_YORK_OPEN_ASSUMED"
    assert price(m,at_utc=NY_OPEN_WINTER) == pytest.approx(.9)
    assert price(m,spread_points=30) == pytest.approx(1.5)
    assert price(m,spread_points=30,at_utc=NY_OPEN_WINTER) == pytest.approx(2.7)


def test_min_points_and_stress():
    m = SlippagePenalty(atr_fraction=.001,min_points=8,stress_multiplier=3,
                        reference_spread_points=10)
    assert price(m, atr=1, point=.1) == pytest.approx(2.4)


@pytest.mark.parametrize("atr,point,spread",[(-1,.01,10),(1,0,10),
                                             (float("nan"),.01,10),(1,.01,-3)])
def test_invalid_market_inputs_rejected(atr,point,spread):
    with pytest.raises(ValueError):
        price(SlippagePenalty(reference_spread_points=10),atr=atr,point=point,spread_points=spread)


def test_missing_measured_reference_is_fail_closed():
    with pytest.raises(ValueError,match="measured"):
        price(SlippagePenalty())


def test_invalid_multiplier_rejected():
    with pytest.raises(ValueError):
        price(SlippagePenalty(stress_multiplier=.5,reference_spread_points=10),atr=4)
