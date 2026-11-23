"""Broker-independent rounding and lot sizing helpers."""
import math


def floor_volume(raw: float, min_volume: float, max_volume: float, step: float) -> float:
    if step <= 0 or min_volume <= 0 or max_volume < min_volume or raw < min_volume:
        return 0.0
    n = math.floor((min(raw, max_volume) - min_volume) / step + 1e-8)
    return round(min_volume + n * step, 8)


def price_to_tick(price: float, tick_size: float, direction: str, digits: int) -> float:
    if price <= 0 or tick_size <= 0:
        raise ValueError("Invalid price or broker tick size")
    ratio = price / tick_size
    value = (math.floor(ratio + 1e-9) if direction == "down" else math.ceil(ratio - 1e-9)) * tick_size
    return round(value, digits)

