"""Pure learning maths: mastery level and the review schedule. No database, no clock.

Mastery is a transparent heuristic, not a probability: the share of correct answers among the
student's last WINDOW answers on a skill, pulled towards a prior while there is little data.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta

WINDOW = 10
PRIOR_WEIGHT = 2  # the prior counts as this many answers
DEFAULT_PRIOR = 0.5
TARGET_MASTERY = 0.85
MASTERED_MIN_ANSWERS = 6

# Review policy v1: a starting hypothesis to be checked on the pilot, not a scientific optimum.
REVIEW_POLICY_VERSION = 1
REVIEW_INTERVALS_DAYS = (1, 3, 7, 14, 30)
SUCCESSES_TO_MASTER = 3


def mastery(recent_correct: int, recent_total: int, prior: float = DEFAULT_PRIOR) -> float:
    return round((recent_correct + prior * PRIOR_WEIGHT) / (recent_total + PRIOR_WEIGHT), 3)


def is_mastered(recent_correct: int, recent_total: int) -> bool:
    return recent_total >= MASTERED_MIN_ANSWERS and recent_correct / recent_total >= TARGET_MASTERY


@dataclass(frozen=True)
class CardState:
    interval_index: int
    consecutive_successes: int
    last_success_on: date | None
    mastered: bool
    due_at: datetime | None
    interval_days: int  # 0 when the card left the schedule


def after_success(
    interval_index: int, consecutive: int, last_success_on: date | None, now: datetime, today: date
) -> CardState:
    """A correct review. Successes count towards mastery only on different calendar days."""
    if last_success_on != today:
        consecutive += 1
    if consecutive >= SUCCESSES_TO_MASTER:
        return CardState(interval_index, consecutive, today, True, None, 0)
    index = min(interval_index + 1, len(REVIEW_INTERVALS_DAYS) - 1)
    days = REVIEW_INTERVALS_DAYS[index]
    return CardState(index, consecutive, today, False, now + timedelta(days=days), days)


def after_failure(last_success_on: date | None, now: datetime) -> CardState:
    """A mistake sends the card back to the first interval."""
    days = REVIEW_INTERVALS_DAYS[0]
    return CardState(0, 0, last_success_on, False, now + timedelta(days=days), days)


def current_streak(active_days: set[date], today: date) -> int:
    """Consecutive study days ending today, or yesterday if today has no activity yet."""
    day = today if today in active_days else today - timedelta(days=1)
    streak = 0
    while day in active_days:
        streak += 1
        day -= timedelta(days=1)
    return streak


def longest_streak(active_days: set[date]) -> int:
    best = 0
    for day in active_days:
        if day - timedelta(days=1) not in active_days:
            length = 1
            while day + timedelta(days=length) in active_days:
                length += 1
            best = max(best, length)
    return best
