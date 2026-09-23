"""
Pure hourly-aggregation logic.

:func:`aggregate_hourly_data` is the core of the statistics-insertion pipeline.
It takes raw usage-read intervals from the Dominion API and produces two
dictionaries keyed by hour:

- ``hourly_consumption`` — Wh consumed in each clock hour.
- ``hourly_cost``        — USD cost for each clock hour (empty for non-electric
  accounts or when cost mode is COST_MODE_NONE).

The function is a **pure function** — no Home Assistant dependency, no recorder
calls, no coordinator ``self``. Every input it needs is passed explicitly. This
makes it straightforward to unit-test with synthetic data.

Key behaviors
--------------
**Tiered-rate billing-cycle tracking**
    For Rate 8 and Rate 6 the tier resets at each billing-cycle boundary. The
    function estimates all cycles using :func:`~.billing._estimate_billing_cycles`
    and resets ``cumulative_wh`` to 0 when an interval crosses into a new cycle.
    This reset must happen even for hours that are already in the recorder
    (``existing_hours``) so the cumulative counter stays accurate for subsequent
    intervals. See the ordering note in the loop body.

**Skipping already-recorded hours**
    When called from the incremental-update path, ``existing_hours`` contains the
    set of hour timestamps already present in the HA recorder. Intervals whose
    hour bucket is in this set are skipped (not re-emitted) but their Wh still
    contribute to ``cumulative_wh`` for tier correctness.

**Cost start date gate**
    When the user enables extended consumption backfill but NOT extended cost
    backfill, ``cost_start_date`` is set to the start of the current billing
    cycle. Intervals before that date contribute to consumption statistics but
    produce no cost row.

The coordinator retains a thin ``_aggregate_hourly_data`` wrapper method that
resolves cost config from its options and delegates here, so existing tests that
call ``coordinator._aggregate_hourly_data(...)`` are unaffected.
See docs/REFACTOR_PLAN.md Phase 3.
"""

import logging
from datetime import date, datetime

from dominionsc import Forecast, RatePlan

from .billing import _estimate_billing_cycles, _find_billing_cycle_for_date
from .cost import _calculate_cost_for_wh
from .models import DominionSCStatisticMetadata

_LOGGER = logging.getLogger(__name__)


def aggregate_hourly_data(
    usage_reads: list,
    metadata: DominionSCStatisticMetadata,
    forecast: Forecast | None,
    start_date: date,
    is_electric: bool,
    cost_mode: str,
    fixed_rate: float,
    rate_plan: RatePlan | None,
    is_tiered_rate: bool,
    cost_start_date: date | None = None,
    existing_hours: set[datetime] | None = None,
) -> tuple[dict[datetime, float], dict[datetime, float]]:
    """
    Aggregate usage-read intervals into hourly consumption and cost buckets.

    Processes a flat list of ``UsageRead`` objects (typically ~15-minute
    intervals from the Dominion API), groups them into clock-hour buckets,
    calculates per-hour costs (for electric accounts), and returns both
    dictionaries. Hours already present in the recorder are skipped in the
    output but still contribute to the cumulative Wh counter so tier
    calculations remain correct.

    The cost configuration is passed in explicitly (rather than read from the
    coordinator's options) so this function has no Home Assistant dependency
    and can be unit-tested with synthetic inputs.

    Args:
        usage_reads:    Raw interval objects from the Dominion API. Each must
                        have ``.start_time`` (datetime) and ``.consumption``
                        (float, in Wh). The list may be unsorted; it is sorted
                        by start_time internally.
        metadata:       Statistic metadata for this account/register, used to
                        check whether a cost_id exists and which account type
                        this is.
        forecast:       Current billing-cycle forecast from the API. Required
                        when ``is_tiered_rate`` is ``True`` (provides the
                        anchor cycle dates for billing-cycle estimation).
                        May be ``None`` for non-tiered modes.
        start_date:     The earliest date of the fetch window. Used as the
                        ``earliest`` bound when estimating billing cycles.
        is_electric:    ``True`` if this account is ELECTRIC. Cost statistics
                        are only produced for electric accounts.
        cost_mode:      One of the ``COST_MODE_*`` constants. Passed through
                        to :func:`~.cost._calculate_cost_for_wh`.
        fixed_rate:     $/kWh rate for COST_MODE_FIXED. Ignored otherwise.
        rate_plan:      :class:`~dominionsc.RatePlan` for tiered modes, or
                        ``None``. Passed through to
                        :func:`~.cost._calculate_cost_for_wh`.
        is_tiered_rate: ``True`` when ``cost_mode`` maps to an entry in
                        :data:`~.rates.RATE_PLAN_REGISTRY`. Controls whether
                        billing-cycle boundary tracking is active.
        cost_start_date: When set, cost rows are only produced for intervals on
                        or after this date. Used when consumption backfill is
                        extended but cost backfill is not. ``None`` means no
                        additional restriction.
        existing_hours: Set of datetime values (hour starts) already present in
                        the HA recorder for this statistic. Hours in this set
                        are excluded from the output dictionaries to avoid
                        duplicate inserts, but their Wh still accumulate in
                        ``cumulative_wh``. ``None`` means no hours are known
                        to already exist (backfill path).

    Returns:
        A 2-tuple ``(hourly_consumption, hourly_cost)`` where both dicts are
        keyed by hour-start datetime and values are floats:
        - ``hourly_consumption``: Wh consumed in that hour.
        - ``hourly_cost``:        USD cost for that hour (empty dict when cost
          calculation is disabled or this is a gas account).

    """
    # Running total of Wh consumed in the current billing cycle (reset at each
    # cycle boundary). Used by tiered rates to track which tier applies.
    cumulative_wh = 0.0

    # Pre-compute billing cycles once (only for tiered rates).
    billing_cycles: list[tuple[date, date]] = []
    if is_tiered_rate:
        assert forecast is not None  # documented requirement for tiered rates
        today = date.today()
        billing_cycles = _estimate_billing_cycles(
            anchor_start=forecast.start_date,
            anchor_end=forecast.end_date,
            earliest=start_date,
            latest=today,
        )
        _LOGGER.debug(
            "Estimated %d billing cycles from %s to %s for tier tracking",
            len(billing_cycles),
            billing_cycles[0][0] if billing_cycles else "?",
            billing_cycles[-1][1] if billing_cycles else "?",
        )

    # Tracks which billing cycle the previous interval was in, so we can
    # detect when we cross into a new cycle and reset cumulative_wh.
    current_cycle: tuple[date, date] | None = None

    hourly_consumption: dict[datetime, float] = {}
    hourly_cost: dict[datetime, float] = {}

    # Process intervals in chronological order so cumulative_wh is correct.
    for usage_read in sorted(usage_reads, key=lambda i: i.start_time):
        interval_date = usage_read.start_time.date()
        # Truncate the sub-hour timestamp to the hour boundary for bucketing.
        hour_start = usage_read.start_time.replace(minute=0, second=0, microsecond=0)

        # --- Tiered-rate billing-cycle boundary tracking ---
        # This MUST happen before the existing_hours skip below so that
        # cumulative_wh stays correct even for already-recorded hours.
        # If we skipped this for already-recorded hours, cumulative_wh would
        # be wrong for subsequent intervals and tier splits would be incorrect.
        # Gas rates are flat (is_tiered_rate=False), so this block is a no-op
        # for gas accounts even though the outer condition now covers them.
        if metadata.cost_id:
            if is_tiered_rate and billing_cycles:
                row_cycle = _find_billing_cycle_for_date(interval_date, billing_cycles)
                if row_cycle != current_cycle:
                    # Crossed a billing-cycle boundary — reset the counter.
                    current_cycle = row_cycle
                    cumulative_wh = 0.0

        # --- Skip already-recorded hours ---
        # Hours already in the recorder should not be re-inserted, but we
        # still need their Wh contribution for tier-boundary accuracy.
        if existing_hours and hour_start in existing_hours:
            cumulative_wh += usage_read.consumption
            continue

        # --- Accumulate consumption ---
        if hour_start not in hourly_consumption:
            hourly_consumption[hour_start] = 0.0
        hourly_consumption[hour_start] += usage_read.consumption

        # --- Calculate cost (electric and gas accounts when a rate is configured) ---
        # For gas accounts, interval_usage is in ft³ and cost_mode is the gas
        # rate mode; _calculate_cost_for_wh handles the ft³ → therm conversion
        # inside _calculate_flat_cost based on rate_plan.commodity.
        if metadata.cost_id:
            # Honor the cost_start_date gate: when consumption is backfilled
            # further than cost (user only enabled one of the two extended
            # backfill options), skip cost rows for the early window.
            if cost_start_date is not None and interval_date < cost_start_date:
                cumulative_wh += usage_read.consumption
                continue

            if hour_start not in hourly_cost:
                hourly_cost[hour_start] = 0.0

            # cumulative_wh is passed for tiered-rate tier-boundary
            # calculations. TOU plans (Rate 5, Rate 7) ignore this argument —
            # TOU cost depends only on time-of-day and season, not cumulative
            # usage. The value is always forwarded so the function signature
            # stays uniform across all rate plan types.
            hourly_cost[hour_start] += _calculate_cost_for_wh(
                usage_read.consumption,
                usage_read.start_time,
                cumulative_wh,
                cost_mode,
                fixed_rate,
                rate_plan,
            )
            # Advance the cumulative counter AFTER pricing so the cost
            # function receives the Wh total *before* this interval.
            cumulative_wh += usage_read.consumption

    return hourly_consumption, hourly_cost
