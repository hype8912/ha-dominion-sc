"""
Pure billing-cycle estimation helpers.

Dominion Energy SC does not expose historical billing-cycle boundaries via the
API. The current billing cycle (start and end date) is available through the
forecast endpoint, but all prior cycles must be *estimated* from that single
known anchor point.

The estimation algorithm walks backward month-by-month from the anchor using
empirically observed billing-cycle lengths (see :func:`_billing_cycle_get_gap`)
until it has covered the requested ``earliest`` date, then optionally walks
forward if ``latest`` extends beyond the anchor end.

Why this matters
----------------
Dominion's tiered rates reset at each billing cycle boundary: the first 800 kWh
of each cycle is billed at the lower rate and subsequent usage at the higher
rate. To calculate costs correctly for historical data, the aggregation layer
must know which billing cycle each interval belongs to so it can maintain an
accurate cumulative Wh counter. Without accurate cycle boundaries the tier
split would be wrong (either always in the first tier or always in the second).

Accuracy
--------
The gap table is based on observed cycle lengths and approximates the actual
meter-read schedule. It is accurate enough for cost estimation purposes but
should not be treated as ground truth — actual billing may vary by a day or two.

Backward compatibility: coordinator.py re-exports these names, so existing
imports of ``from ...coordinator import _estimate_billing_cycles`` (etc.)
continue to resolve. See docs/REFACTOR_PLAN.md Phase 2.
"""

import calendar
import logging
from datetime import date, timedelta

_LOGGER = logging.getLogger(__name__)


def _billing_cycle_get_gap(d: date) -> int:
    """
    Return the number of days in the billing cycle that *starts* on date ``d``.

    Dominion's meter-read schedule produces billing cycles of varying length
    depending on the month. The table below encodes the observed pattern.
    Months 4 (April) and 6 (June) have leap-year-sensitive lengths because
    the prior month (March / May) may have an extra day in a leap year, which
    shifts the read date.

    Gap table (month of cycle start -> cycle length in days):

        Jan=30, Feb=31, Mar=30, Apr=31*, May=30, Jun=30†, Jul=30,
        Aug=31, Sep=31, Oct=30, Nov=31, Dec=30

        * April: 31 if current year OR next year is a leap year, else 30.
        † June:  30 if current year OR next year is a leap year, else 31.

    Args:
        d: The first day of the billing cycle whose length is needed.

    Returns:
        The estimated number of days in that billing cycle.

    """
    # Observed billing-cycle lengths per starting month.
    # Months 4 and 6 are absent because they require leap-year logic below.
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

    # April and June: length depends on whether the current or immediately
    # following year is a leap year (affects February days and thus the
    # overall meter-read schedule shift).
    near_leap = calendar.isleap(d.year) or calendar.isleap(d.year + 1)
    if m == 4:
        return 31 if near_leap else 30
    if m == 6:
        return 30 if near_leap else 31

    # Fallback (should never be reached given the gap table covers all months).
    return 30


def _estimate_billing_cycles(
    anchor_start: date,
    anchor_end: date,
    earliest: date,
    latest: date | None = None,
) -> list[tuple[date, date]]:
    """
    Estimate all billing cycles between ``earliest`` and ``latest``.

    Given one known (current) billing cycle as an anchor, walks backward and
    optionally forward to produce a complete list of estimated cycles.

    NOTE: This is an approximation algorithm. Actual billing-cycle boundaries
    are determined by the utility's meter-read schedule, which is not exposed
    by the API. Cost estimates near cycle boundaries may be off by a day.

    Algorithm:
        1. Start from ``anchor_start`` and step backward one cycle at a time
           using :func:`_billing_cycle_get_gap` until the walk reaches or
           passes ``earliest``.
        2. Reverse the accumulated start dates and build ``(start, end)`` pairs
           where ``end = next_start - 1 day``.
        3. Append the anchor cycle itself.
        4. If ``latest > anchor_end``, walk forward from the day after the
           anchor ends to cover ``latest`` (handles a stale forecast where the
           current billing cycle hasn't been refreshed yet).

    Args:
        anchor_start: Start date of the **known** current billing cycle
                      (from the API forecast).
        anchor_end:   End date of the known current billing cycle.
        earliest:     Generate cycles back to (at least) this date. Typically
                      the backfill start date or ``date.today() - 365 days``.
        latest:       If provided, generate cycles forward to (at least) this
                      date. Pass ``date.today()`` to handle a stale forecast.
                      Pass ``None`` if forward projection is not needed.

    Returns:
        List of ``(start_date, end_date)`` tuples ordered oldest-first.
        The list always includes the anchor cycle and covers at least
        ``earliest`` through ``anchor_end`` (or ``latest`` if provided).

    """
    # --- Walk backward from the anchor to *earliest* ---
    starts: list[date] = [anchor_start]
    cur = anchor_start
    while cur > earliest:
        # Determine the month of the *previous* cycle's start date.
        prev_month = cur.month - 1 or 12  # Wrap January -> December
        prev_year = cur.year - (1 if cur.month == 1 else 0)
        # Step back by the gap of the previous month's cycle.
        cur -= timedelta(days=_billing_cycle_get_gap(date(prev_year, prev_month, 1)))
        starts.append(cur)
    # Reverse so list is oldest-first.
    starts.reverse()

    # --- Build (start, end) pairs; each cycle ends the day before the next starts ---
    cycles = [(starts[i], starts[i + 1] - timedelta(days=1)) for i in range(len(starts) - 1)]
    # Append the anchor itself (its end date comes from the API, not estimation).
    cycles.append((anchor_start, anchor_end))

    # --- Walk forward if *latest* extends beyond the anchor end ---
    # This happens when the Dominion API hasn't updated the forecast yet (e.g.
    # the billing cycle rolled over but the forecast still shows the previous
    # cycle's end date).
    if latest is not None and latest > anchor_end:
        _LOGGER.debug(
            "Forecast billing cycle (%s - %s) is stale; projecting forward to cover %s",
            anchor_start,
            anchor_end,
            latest,
        )
        cur = anchor_end + timedelta(days=1)  # Next cycle starts day after anchor ends
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
    """
    Return the billing cycle tuple that contains ``target``, or ``None``.

    Linear scan over the ``billing_cycles`` list. The list is typically
    short (< 15 entries even for a 365-day backfill) so performance is fine.

    Args:
        target:         The date to look up.
        billing_cycles: List of ``(start, end)`` tuples as produced by
                        :func:`_estimate_billing_cycles`.

    Returns:
        The ``(start_date, end_date)`` tuple whose range includes ``target``,
        or ``None`` if no cycle contains ``target`` (e.g. a gap between cycles
        due to estimation error, or a date outside the estimated range).

    """
    for start, end in billing_cycles:
        if start <= target <= end:
            return (start, end)
    return None
