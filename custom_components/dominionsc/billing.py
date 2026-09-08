"""
Pure billing-cycle estimation helpers.

Dominion does not report historical billing-cycle boundaries, so these
functions estimate them from a single known (current) cycle. They carry no
Home Assistant dependency.

Backward compatibility: coordinator.py re-exports these names, so existing
imports of ``from ...coordinator import _estimate_billing_cycles`` (etc.)
continue to resolve. See docs/REFACTOR_PLAN.md Phase 2.
"""

import calendar
import logging
from datetime import date, timedelta

_LOGGER = logging.getLogger(__name__)


def _billing_cycle_get_gap(d: date) -> int:
    """Return billing cycle gap for provided start date."""
    gaps = {
        1: 30,
        2: 31,
        3: 30,
        5: 30,
        7: 30,
        8: 31,
        9: 31,
        10: 30,
        11: 31,
        12: 30,
    }
    m = d.month
    if m in gaps:
        return gaps[m]
    near_leap = calendar.isleap(d.year) or calendar.isleap(d.year + 1)
    if m == 4:
        return 31 if near_leap else 30
    if m == 6:
        return 30 if near_leap else 31
    return 30


def _estimate_billing_cycles(
    anchor_start: date,
    anchor_end: date,
    earliest: date,
    latest: date | None = None,
) -> list[tuple[date, date]]:
    """
    Estimate billing cycle intervals given one known (current) billing cycle.

    NOTE: this is a temporary algorithm.

    Args:
        anchor_start: Start date of the known (current) billing cycle.
        anchor_end:   End date of the known (current) billing cycle.
        earliest:     Generate cycles back to (at least) this date.
        latest:       If provided, generate cycles forward to (at least)
                      this date.  This handles the case where the forecast
                      billing cycle hasn't been updated yet and today is
                      past the anchor end date.

    Returns:
        List of (start_date, end_date) tuples, ordered oldest-first.

    """
    # --- walk backward from the anchor to *earliest* ---
    starts: list[date] = [anchor_start]
    cur = anchor_start
    while cur > earliest:
        prev_month = cur.month - 1 or 12
        prev_year = cur.year - (1 if cur.month == 1 else 0)
        cur -= timedelta(days=_billing_cycle_get_gap(date(prev_year, prev_month, 1)))
        starts.append(cur)
    starts.reverse()

    # --- build (start, end) pairs; end = next_start - 1 ---
    cycles = [
        (starts[i], starts[i + 1] - timedelta(days=1)) for i in range(len(starts) - 1)
    ]
    cycles.append((anchor_start, anchor_end))

    # --- walk forward past the anchor if *latest* is beyond the anchor end ---
    if latest is not None and latest > anchor_end:
        _LOGGER.debug(
            "Forecast billing cycle (%s - %s) is stale; projecting forward to cover %s",
            anchor_start,
            anchor_end,
            latest,
        )
        cur = anchor_end + timedelta(days=1)  # next cycle starts day after anchor ends
        while cur <= latest:
            gap = _billing_cycle_get_gap(cur)
            cycle_end = cur + timedelta(days=gap - 1)
            cycles.append((cur, cycle_end))
            cur = cycle_end + timedelta(days=1)

    return cycles


def _find_billing_cycle_for_date(
    target: date,
    billing_cycles: list[tuple[date, date]],
) -> tuple[date, date] | None:
    """Return the billing cycle that contains *target*, or ``None``."""
    for start, end in billing_cycles:
        if start <= target <= end:
            return (start, end)
    return None
