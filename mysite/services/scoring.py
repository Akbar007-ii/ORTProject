"""Pure scoring functions: no database, no clock. Easy to verify against reference tables."""

SCORING_VERSION = 1


def scale_lookup(points: list[tuple[int, float]], raw: int) -> float:
    """Converts a raw section score to exam points using a scale table.

    `points` are (raw, scaled) pairs. Between two points the value is interpolated linearly;
    outside the table it is clamped to the nearest end.
    """
    if not points:
        raise ValueError("Scale has no points")
    points = sorted(points)
    if raw <= points[0][0]:
        return float(points[0][1])
    if raw >= points[-1][0]:
        return float(points[-1][1])
    for (raw_lo, scaled_lo), (raw_hi, scaled_hi) in zip(points, points[1:], strict=False):
        if raw_lo <= raw <= raw_hi:
            share = (raw - raw_lo) / (raw_hi - raw_lo)
            return round(scaled_lo + share * (scaled_hi - scaled_lo), 1)
    raise AssertionError("unreachable")


def scale_problems(points: list[tuple[int, float]], max_raw: int) -> list[str]:
    """Checks one section's table. Empty list = valid."""
    problems = []
    points = sorted(points)
    raws = [raw for raw, _ in points]
    if not raws or raws[0] != 0:
        problems.append("the table must start at raw 0")
    if not raws or raws[-1] != max_raw:
        problems.append(f"the table must end at raw {max_raw} (number of questions)")
    if any(scaled < 0 for _, scaled in points):
        problems.append("scaled values cannot be negative")
    if any(hi[1] < lo[1] for lo, hi in zip(points, points[1:], strict=False)):
        problems.append("more correct answers cannot give fewer points")
    return problems
