"""
Pure hourly-aggregation logic.

``aggregate_hourly_data`` buckets usage-read intervals into hourly consumption
and cost dictionaries, handling tiered billing-cycle boundary resets and
already-recorded-hour skipping. It is a pure function -- no Home Assistant, no
recorder, no coordinator ``self`` -- so it can be tested with synthetic inputs.

The coordinator retains a thin ``_aggregate_hourly_data`` method that resolves
cost config from its options and delegates here, so existing tests that call
``coordinator._aggregate_hourly_data(...)`` (or patch it) are unaffected.
See docs/REFACTOR_PLAN.md Phase 3.
"""

import logging
from datetime import date, datetime

from dominionsc import Forecast

from .billing import _estimate_billing_cycles, _find_billing_cycle_for_date
from .cost import _calculate_cost_for_wh
from .models import DominionSCStatisticMetadata
from .rates import RateSchedule

_LOGGER = logging.getLogger(__name__)


def aggregate_hourly_data(
    usage_reads: list,
    metadata: DominionSCStatisticMetadata,
    forecast: Forecast | None,
    start_date: date,
    is_electric: bool,
    cost_mode: str,
    fixed_rate: float,
    rate_schedule: RateSchedule | None,
    is_tiered_rate: bool,
    cost_start_date: date | None = None,
    existing_hours: set[datetime] | None = None,
) -> tuple[dict[datetime, float], dict[datetime, float]]:
    """
    Aggregate usage-read intervals into hourly consumption and cost buckets.

    Handles billing-cycle boundary resets for tiered rates, skips hours
    already present in ``existing_hours``, and respects ``cost_start_date``
    for extended-backfill scenarios.

    The resolved cost configuration (``cost_mode``, ``fixed_rate``,
    ``rate_schedule``, ``is_tiered_rate``) is passed in explicitly so this
    function has no dependency on the coordinator or its options.

    Returns:
        (hourly_consumption, hourly_cost) dictionaries keyed by hour start.

    """
    cumulative_wh = 0.0

    billing_cycles: list[tuple[date, date]] = []
    if is_tiered_rate:
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

    current_cycle: tuple[date, date] | None = None
    hourly_consumption: dict[datetime, float] = {}
    hourly_cost: dict[datetime, float] = {}

    for usage_read in sorted(usage_reads, key=lambda i: i.start_time):
        interval_date = usage_read.start_time.date()
        hour_start = usage_read.start_time.replace(minute=0, second=0, microsecond=0)

        # For tiered rates, always track billing cycle boundaries
        # even for already-recorded hours so cumulative_wh is correct.
        if is_electric and metadata.cost_id:
            if is_tiered_rate and billing_cycles:
                row_cycle = _find_billing_cycle_for_date(interval_date, billing_cycles)
                if row_cycle != current_cycle:
                    current_cycle = row_cycle
                    cumulative_wh = 0.0

        # Skip hours that already have recorded statistics — we still
        # need to accumulate cumulative_wh above so tier boundaries
        # stay correct, but we don't re-emit these hours.
        if existing_hours and hour_start in existing_hours:
            cumulative_wh += usage_read.consumption
            continue

        if hour_start not in hourly_consumption:
            hourly_consumption[hour_start] = 0.0
        hourly_consumption[hour_start] += usage_read.consumption

        # Calculate cost for electric accounts with cost tracking
        if is_electric and metadata.cost_id:
            # Skip cost for intervals before cost_start_date when set
            # (e.g. extended consumption backfill without extended cost)
            if cost_start_date is not None and interval_date < cost_start_date:
                cumulative_wh += usage_read.consumption
                continue

            if hour_start not in hourly_cost:
                hourly_cost[hour_start] = 0.0

            hourly_cost[hour_start] += _calculate_cost_for_wh(
                usage_read.consumption,
                usage_read.start_time,
                cumulative_wh,
                cost_mode,
                fixed_rate,
                rate_schedule,
            )
            cumulative_wh += usage_read.consumption

    return hourly_consumption, hourly_cost
