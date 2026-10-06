"""Solcast utilities."""

import math
import os
from pathlib import Path
from typing import Any

from .log import get_logger

_LOGGER = get_logger(__name__)


def write_file_atomic(filename: str, payload: str) -> None:
    """Write a file through a temporary file and an atomic rename, so a crash never leaves a truncated file."""

    path = Path(filename)
    temporary = path.with_name(f"{path.name}.tmp")
    try:
        with open(temporary, "w", encoding="utf-8") as file:
            file.write(payload)
        os.replace(temporary, path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


def split_and_strip(value: str) -> list[str]:
    """Split a comma-separated string and strip whitespace, discarding empty items."""

    return [item.strip() for item in value.split(",") if item.strip()]


def azimuth_to_compass_degrees(azimuth: Any) -> float | None:
    """Convert Solcast azimuth to compass degrees in the range [0, 360).

    Solcast azimuth uses N=0, W=+90, E=-90, S=+/-180.
    Standard compass bearings use N=0, E=90, S=180, W=270.
    """
    try:
        return (-float(azimuth)) % 360.0
    except (TypeError, ValueError):
        return None


def azimuth_to_compass_direction(azimuth: Any) -> str | None:
    """Convert an azimuth value to a 16-point cardinal compass direction."""
    if (compass_degrees := azimuth_to_compass_degrees(azimuth)) is None:
        return None

    directions = (
        "N",
        "NNE",
        "NE",
        "ENE",
        "E",
        "ESE",
        "SE",
        "SSE",
        "S",
        "SSW",
        "SW",
        "WSW",
        "W",
        "WNW",
        "NW",
        "NNW",
    )
    return directions[int((compass_degrees + 11.25) // 22.5) % len(directions)]


def percentile(data: list[Any], _percentile: float) -> float | int:
    """Find the given percentile in a sorted list of values."""

    if not data:
        return 0.0
    k = (len(data) - 1) * (_percentile / 100)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return data[int(k)]
    d0 = data[int(f)] * (c - k)
    d1 = data[int(c)] * (k - f)
    return round(d0 + d1, 4)


def ease_insignificant(factor: float, threshold: float, start: float = 0.0) -> float:
    """Return a dampening factor eased towards 1.0 below the insignificant threshold, rounded to three places.

    A factor at or above the threshold is 1.0. Below it the factor rises in a straight line over a band as wide as the
    gap between the threshold and 1.0 (0.90 to 0.95 by default), so a factor just below the threshold no longer stays
    while one just above it jumps to 1.0. A threshold of 1.0 leaves every factor as it is.

    For a factor raised by delta adjustment, start is the factor before it: the band then begins there at the earliest,
    so a factor that was eased already, or that delta adjustment left alone, is not raised a second time.
    """
    return min(1.0, max(round(factor, 3), round(2 * factor - max(2 * threshold - 1, start), 3)))


def ordinal(value: int) -> str:
    """Return a number with an ordinal suffix."""

    abs_value = abs(value)
    return f"{value}{'th' if 11 <= abs_value % 100 <= 13 else {1: 'st', 2: 'nd', 3: 'rd'}.get(abs_value % 10, 'th')}"


def interquartile_bounds(sorted_data: list[Any], factor: float = 1.5) -> tuple[float | int, float | int]:
    """Return the lower and upper interquartile bounds of a sorted data set."""

    lower = 0.0
    upper = float("inf")
    iqr = 0.0
    if len(sorted_data) > 4:
        q1 = percentile(sorted_data, 25)
        q3 = percentile(sorted_data, 75)
        iqr = round(q3 - q1, 5)
        lower = round(q1 - factor * iqr, 4)
        upper = round(q3 + factor * iqr, 4)
    return (lower, upper)


def diff(lst: list[Any], non_negative: bool = True) -> list[Any]:
    """Build a numpy-like diff."""

    size = len(lst) - 1
    r: list[int | float] = [0] * size
    for i in range(size):
        r[i] = max(0, lst[i + 1] - lst[i]) if non_negative else lst[i + 1] - lst[i]
    return r
