# Library Migration Plan — `ha-dominion-sc`

**Status:** Implemented (Phases 1-8). This document is kept as a historical
record of the migration; see [DEVELOPER.md](DEVELOPER.md) for current behavior.
Since it was written, Rate 1 (Good Cents) has been added and the superseded
2025 tariff periods described in Phase 2 and Appendix B moved into the library.
**Written against:** `dominion-sc-power` new public API (effective 2026-07-01 rate plans)
**Current HA version:** 0.0.7
**Target HA version:** 0.1.0
**Test baseline entering this work:** 163 tests, 100% line and branch coverage

---

## 0. Executive Summary

The `dominion-sc-power` library has been significantly revised. The HA
integration must be updated to consume the new API. This plan covers **eight
phases** addressing everything from rate-value corrections and TOU support to
gas cost calculation and config-flow expansion.

### What Changed in the Library

| Area | Before | After |
|------|--------|-------|
| Rate definitions | Custom `RateSchedule` / `TieredRate` dataclasses in HA `rates.py` | Library's `RatePlan` with typed `Charge` objects |
| Rate 8 summer lower tier | $0.14599/kWh | $0.15878/kWh |
| Rate 8 summer upper tier | $0.15983/kWh | $0.17442/kWh |
| Rate 8 winter lower tier | $0.14599/kWh | $0.15878/kWh |
| Rate 8 winter upper tier | $0.14045/kWh | $0.15253/kWh |
| Rate 6 summer lower tier | $0.14164/kWh | $0.15333/kWh |
| Rate 6 summer upper tier | $0.15505/kWh | $0.16842/kWh |
| Rate 6 winter lower tier | $0.14164/kWh | $0.15333/kWh |
| Rate 6 winter upper tier | $0.13628/kWh | $0.14729/kWh |
| Rate 8 effective date | 2025-07-23 | 2026-07-01 |
| Basic Facilities Charge | `$9.00/month` (informational only) | `DailyCharge($0.36164/day)` + `MonthlyCharge("DER", $1.00)` |
| Available rate plans | Rate 6, Rate 8 only | Rate 2, Rate 5, Rate 6, Rate 7, Rate 8, Rate 32S, Rate 32V |
| TOU support | None | Rate 5 (TOU), Rate 7 (TOU + demand) |
| Gas rate plans | None | Rate 32S, Rate 32V |
| Rate lookup | `TIERED_RATE_REGISTRY` dict in HA | `get_rate_plan(code)`, `get_available_rate_plans()` in library |

### Impact Summary

- **Breaking cost inaccuracy**: Users on Rate 8 or Rate 6 since July 2025 have
  been accumulating cost statistics calculated at the old tariff rates. After
  this migration, existing stats must be recalculated with the correct 2026-07-01
  rates, and the transition must be handled gracefully.
- **New rate options**: Rate 2 (flat), Rate 5 (TOU), Rate 7 (TOU+demand) need
  to be selectable in the config flow.
- **Gas costs**: Rate 32S and Rate 32V unlock gas cost calculation for the
  first time.
- **Dependency update**: `manifest.json` requirement pin must advance.

### Files Touched (summary)

| File | Change Type |
|------|-------------|
| `custom_components/dominionsc/rates.py` | Complete rewrite |
| `custom_components/dominionsc/cost.py` | Significant rewrite |
| `custom_components/dominionsc/const.py` | Add/rename COST_MODE_* constants |
| `custom_components/dominionsc/aggregation.py` | Update call sites for TOU |
| `custom_components/dominionsc/coordinator.py` | Gas cost threading, version bump logic |
| `custom_components/dominionsc/config_flow.py` | Expanded rate plan selection |
| `custom_components/dominionsc/options_flow.py` | Expanded rate plan selection |
| `custom_components/dominionsc/strings.json` | New rate plan labels |
| `custom_components/dominionsc/translations/en.json` | New rate plan labels |
| `custom_components/dominionsc/manifest.json` | Dependency version bump |
| `tests/test_rates.py` | New tests for all charge types |
| `tests/test_coordinator.py` | Update mocks, new gas cost tests |
| `tests/test_config_flow.py` | New rate plan flow tests |

---

## Phase 1: Drop HA's Custom Rate Definitions — Adopt Library `RatePlan`

**Goal:** Remove the HA integration's bespoke `RateSchedule`/`TieredRate`/
`SeasonalTieredRates` dataclasses and the hardcoded SC_RATE_8/SC_RATE_6
constants. Replace them everywhere with the library's `RatePlan` model,
accessed via `get_rate_plan(code)`.

**Why first:** Every subsequent phase builds on cost.py. The cost engine must
speak `RatePlan` before Rate 2, Rate 5, Rate 7, and gas rates can be added.
Doing this in isolation also keeps the diff reviewable.

### 1.1 — Rewrite `rates.py`

The current `rates.py` (~330 lines) mixes custom type definitions, rate
constants, and helper functions. After this phase it becomes a thin adapter.

**Delete entirely:**
- `Season` enum (use `dominionsc.Season` from library instead)
- `TieredRate` dataclass
- `SeasonalTieredRates` dataclass
- `RateSchedule` dataclass
- `SC_RATE_8` constant
- `SC_RATE_6` constant
- `TIERED_RATE_REGISTRY` dict
- `get_season()` function
- `calculate_tiered_cost()` function
- `calculate_sc_rate_interval_cost()` function

**Keep and rewrite:**
- `build_cost_mode_choices()` — rebuild using library rate plans (see Phase 5
  for the expanded set; for now keep Rate 6 and Rate 8 only so the diff is
  minimal and the tests stay green)

**New content — `rates.py` after Phase 1:**

```python
"""
Adapter between HA cost-mode keys and the library's RatePlan objects.

The actual rate definitions now live in the `dominion-sc-power` library
(dominionsc.rates). This module is responsible only for:
  - mapping COST_MODE_* string keys → RatePlan objects
  - building the UI selector dict used by ConfigFlow / OptionsFlow
"""

from dominionsc import get_rate_plan

from .const import (
    COST_MODE_FIXED,
    COST_MODE_NONE,
    COST_MODE_RATE_6,
    COST_MODE_RATE_8,
)

# Maps every cost-mode key that resolves to a library RatePlan.
# Keys must match the `code` field of the corresponding RatePlan
# (e.g. "rate_8" maps to RATE_8 whose code == "rate_8").
RATE_PLAN_REGISTRY: dict[str, object] = {
    mode: get_rate_plan(mode) for mode in (COST_MODE_RATE_6, COST_MODE_RATE_8) if get_rate_plan(mode) is not None
}


def build_cost_mode_choices() -> dict[str, str]:
    """Build ordered cost-mode selector for ConfigFlow / OptionsFlow."""
    tiered_choices = {mode: plan.name for mode, plan in RATE_PLAN_REGISTRY.items()}
    return {
        COST_MODE_NONE: "None (no cost calculation)",
        **tiered_choices,
        COST_MODE_FIXED: "Fixed Rate (custom)",
    }
```

### 1.2 — Rewrite `cost.py`

The current `cost.py` calls `calculate_sc_rate_interval_cost()` which is
being deleted. Replace with a new `_calculate_tiered_cost_from_rate_plan()`
that walks the `RatePlan.charges` list and extracts the `TieredUsageCharge`.

**Key logic change in `_calculate_cost_for_wh`:**

```
Old path for tiered mode:
  rate_schedule: RateSchedule → calculate_sc_rate_interval_cost(...)

New path for tiered mode:
  rate_plan: RatePlan → find TieredUsageCharge in rate_plan.charges
                      → determine season from interval_dt.month
                      → find seasonal tiers
                      → call _split_tiered_cost(interval_wh, cumulative_before, tiers)
```

**New `_resolve_cost_config` signature** — return type changes from
`tuple[str, float, RateSchedule | None]` to `tuple[str, float, RatePlan | None]`.
Update the type annotation and the registry lookup:

```python
from dominionsc import RatePlan
from .rates import RATE_PLAN_REGISTRY


def _resolve_cost_config(options):
    cost_mode = options.get(CONF_COST_MODE, COST_MODE_RATE_8)
    fixed_rate = options.get(CONF_FIXED_RATE, DEFAULT_FIXED_RATE)
    rate_plan = RATE_PLAN_REGISTRY.get(cost_mode)  # returns RatePlan or None
    return cost_mode, fixed_rate, rate_plan
```

**New `_calculate_cost_for_wh`** — effective date check updates:

The old code gated on `rate_schedule.effective_date`. The library `RatePlan`
uses `effective_from: date`. Update the check:

```python
# OLD:
if interval_dt.date() < rate_schedule.effective_date:
    return 0.0

# NEW:
if interval_dt.date() < rate_plan.effective_from:
    return 0.0
```

**New helper — `_calculate_tiered_cost`:**

```python
def _calculate_tiered_cost(
    interval_wh: float,
    interval_dt: datetime,
    cumulative_wh_before: float,
    rate_plan: RatePlan,
) -> float:
    """Calculate cost for one Wh interval under a TieredUsageCharge."""
    from dominionsc import TieredUsageCharge, Season

    for charge in rate_plan.charges:
        if not isinstance(charge, TieredUsageCharge):
            continue
        season = Season.SUMMER if 5 <= interval_dt.month <= 9 else Season.WINTER
        tiers = charge.tiers_by_season[season]
        # boundary is in kWh (Decimal); convert to Wh for comparison
        boundary_wh = float(tiers[0].upper_bound or 0) * 1000
        cumulative_after = cumulative_wh_before + interval_wh

        if cumulative_after <= boundary_wh:
            return interval_wh * float(tiers[0].price_per_unit) / 1000
        if cumulative_wh_before >= boundary_wh:
            return interval_wh * float(tiers[1].price_per_unit) / 1000
        wh_under = boundary_wh - cumulative_wh_before
        wh_over = interval_wh - wh_under
        return wh_under * float(tiers[0].price_per_unit) / 1000 + wh_over * float(tiers[1].price_per_unit) / 1000
    return 0.0
```

Note: `price_per_unit` in the library is in `$/kWh` (Decimal). Dividing by
1000 converts to `$/Wh` so it can be multiplied against `interval_wh` in Wh.

### 1.3 — Update `aggregation.py`

`aggregation.py` currently imports `RateSchedule` and passes it as the
`rate_schedule` parameter type. Update the type annotation to `RatePlan | None`.
No logic changes yet (TOU support is Phase 3).

### 1.4 — Update `coordinator.py`

`coordinator.py` re-exports `_calculate_cost_for_wh`, `_resolve_cost_config`,
and `RateSchedule` (via the old `rates.py`). After Phase 1:
- Remove `RateSchedule` from the `__all__` re-exports (it no longer exists)
- The function re-exports remain unchanged since the function names stay the same

### 1.5 — Update `const.py`

Verify that the existing `COST_MODE_RATE_8 = "rate_8"` and
`COST_MODE_RATE_6 = "rate_6"` match the library's `RatePlan.code` fields.
They already match — no change needed.

### 1.6 — Update Tests

**`tests/test_rates.py`** — Delete tests for:
- `calculate_tiered_cost()`
- `calculate_sc_rate_interval_cost()`
- `get_season()`

Replace with tests for:
- `RATE_PLAN_REGISTRY` contains Rate 6 and Rate 8 with correct `RatePlan` objects
- `build_cost_mode_choices()` returns correct keys and ordering

**`tests/test_coordinator.py`** — Update all fixtures and mocks that currently
reference `RateSchedule`, `TieredRate`, `SeasonalTieredRates`, or `SC_RATE_8`:
- Replace with `RATE_8` from the library (or a test fixture `RatePlan`)
- Update `_resolve_cost_config` return type assertions
- Update `_calculate_cost_for_wh` call sites to pass `RatePlan` instead of
  `RateSchedule`

**New tests for cost calculation with `RatePlan`:**
- Tiered boundary straddle (same scenario as before, now using library's
  `TieredUsageCharge`)
- Effective date gating: interval before `2026-07-01` → $0.0
- Interval on or after `2026-07-01` → non-zero cost
- `COST_MODE_NONE` still returns $0.0
- `COST_MODE_FIXED` uses flat rate, ignoring `RatePlan`

### Success Criteria for Phase 1
- All 163 existing tests pass (no regressions)
- `custom_components/dominionsc/rates.py` has no custom dataclass definitions
- `custom_components/dominionsc/cost.py` imports from `dominionsc` (the library),
  not from `rates.py` custom types
- `RateSchedule` does not appear anywhere in `custom_components/`
- `_calculate_cost_for_wh` returns numerically identical results for intervals
  after `2026-07-01` using Rate 8 or Rate 6 (regression test with known values)

---

## Phase 2: Rate Value Update and Versioned Rate History

**Goal:** The library's Rate 8 and Rate 6 carry new tariff values effective
`2026-07-01`. Users who installed the integration before this date have cost
statistics computed at the old (now-superseded) rates. This phase:
1. Adds the old rate values as an archived `RatePlan` for historical use.
2. Wires in the new rate values for all intervals on or after `2026-07-01`.
3. Adds a migration prompt to the options flow to recalculate existing history.

### 2.1 — Add Historical Rate Constants to `rates.py`

> **Superseded:** the internal `_HistoricalTieredRate` / `HISTORICAL_RATE_REGISTRY`
> design described below was later replaced. The archived 2025 tariff periods now
> live in the library as `RATE_6_2025` / `RATE_8_2025` (`RatePlan` objects with
> `effective_to` set), and `cost.py` picks the plan for an interval's date with
> `dominionsc.get_rate_plan_for_date`. The text below is kept as history.

The library does not carry the old (pre-2026-07-01) rate values. The HA
integration must preserve them internally so that existing historical cost
statistics can be re-priced correctly when requested.

Add two frozen `RatePlan`-shaped objects (or simple `dataclass` instances that
the cost engine can consume) representing the July 2025 tariff:

**Decision: Use a simple internal dataclass rather than a fake `RatePlan`.**

The `RatePlan` from the library is a frozen dataclass tied to the library's
model. Constructing fake instances for historical rates would create a fragile
coupling. Instead, add a small `_HistoricalTieredRate` dataclass to `rates.py`
that mirrors only the fields needed by cost calculation:

```python
@dataclass(frozen=True)
class _HistoricalTieredRate:
    """Internal type for superseded tariff rates not in the library."""

    effective_from: date
    effective_to: date
    summer_boundary_wh: float
    summer_under_per_wh: float
    summer_over_per_wh: float
    winter_boundary_wh: float
    winter_under_per_wh: float
    winter_over_per_wh: float


_RATE_8_2025 = _HistoricalTieredRate(
    effective_from=date(2025, 7, 23),
    effective_to=date(2026, 6, 30),
    summer_boundary_wh=800_000,
    summer_under_per_wh=0.14599 / 1000,
    summer_over_per_wh=0.15983 / 1000,
    winter_boundary_wh=800_000,
    winter_under_per_wh=0.14599 / 1000,
    winter_over_per_wh=0.14045 / 1000,
)

_RATE_6_2025 = _HistoricalTieredRate(
    effective_from=date(2025, 7, 23),
    effective_to=date(2026, 6, 30),
    summer_boundary_wh=800_000,
    summer_under_per_wh=0.14164 / 1000,
    summer_over_per_wh=0.15505 / 1000,
    winter_boundary_wh=800_000,
    winter_under_per_wh=0.14164 / 1000,
    winter_over_per_wh=0.13628 / 1000,
)

# Historical rate registry — keyed same as RATE_PLAN_REGISTRY
HISTORICAL_RATE_REGISTRY: dict[str, list[_HistoricalTieredRate]] = {
    COST_MODE_RATE_8: [_RATE_8_2025],
    COST_MODE_RATE_6: [_RATE_6_2025],
}
```

### 2.2 — Update `_calculate_cost_for_wh` for Versioned Rates

The cost engine must pick the right rate version for a given `interval_dt`:

```
If interval_dt.date() < 2025-07-23 (start of oldest known rate):
    return 0.0  # no tariff in effect

For each historical rate in HISTORICAL_RATE_REGISTRY[cost_mode] (sorted asc):
    if historical_rate.effective_from <= interval_dt.date() <= historical_rate.effective_to:
        return _calculate_historical_tiered_cost(interval_wh, interval_dt,
                                                 cumulative_wh_before, hist)

If interval_dt.date() >= rate_plan.effective_from:
    return _calculate_tiered_cost(interval_wh, interval_dt,
                                  cumulative_wh_before, rate_plan)

return 0.0  # gap between historical and current (should not occur)
```

**New function `_calculate_historical_tiered_cost`** in `cost.py`:

```python
def _calculate_historical_tiered_cost(
    interval_wh: float,
    interval_dt: datetime,
    cumulative_wh_before: float,
    hist: _HistoricalTieredRate,
) -> float:
    is_summer = 5 <= interval_dt.month <= 9
    if is_summer:
        boundary = hist.summer_boundary_wh
        rate_under = hist.summer_under_per_wh
        rate_over = hist.summer_over_per_wh
    else:
        boundary = hist.winter_boundary_wh
        rate_under = hist.winter_under_per_wh
        rate_over = hist.winter_over_per_wh
    cumulative_after = cumulative_wh_before + interval_wh
    if cumulative_after <= boundary:
        return interval_wh * rate_under
    if cumulative_wh_before >= boundary:
        return interval_wh * rate_over
    wh_under = boundary - cumulative_wh_before
    wh_over = interval_wh - wh_under
    return wh_under * rate_under + wh_over * rate_over
```

### 2.3 — Add Version-Bump Migration Prompt to Options Flow

On first load after the integration is updated, users should be offered an
automatic recalculation of costs for the `2025-07-23` to `2026-06-30` period
(the period where rates were applied at old values).

**Mechanism:**
- Add a new `const.py` key: `CONF_LAST_RATE_SCHEMA_VERSION = "last_rate_schema_version"`
- On `async_setup_entry()`, compare `entry.data.get(CONF_LAST_RATE_SCHEMA_VERSION, 0)`
  to `CURRENT_RATE_SCHEMA_VERSION = 2`.
- If schema version is behind, fire a persistent notification via
  `hass.components.persistent_notification.create()` telling the user that
  rate values have changed and they should go to Options → Recalculate History
  to update their historical cost data.
- Update `CONF_LAST_RATE_SCHEMA_VERSION` in `entry.data` to `CURRENT_RATE_SCHEMA_VERSION`.

**Do NOT auto-trigger recalculation** — this is a long-running async task and
the user may have intentionally chosen not to backfill costs. Let them decide.

### 2.4 — Tests

**`tests/test_rates.py`:**
- `_RATE_8_2025` and `_RATE_6_2025` have correct values (regression against
  known hand-calculated costs)
- `HISTORICAL_RATE_REGISTRY` keys match `COST_MODE_RATE_8` and
  `COST_MODE_RATE_6`

**`tests/test_coordinator.py`:**
- Interval on 2025-10-01 with Rate 8 → uses `_RATE_8_2025` values
- Interval on 2026-07-15 with Rate 8 → uses library's `RATE_8` values
- Interval on 2025-07-01 (before either rate) → $0.0
- Version-bump notification fires when `CONF_LAST_RATE_SCHEMA_VERSION` is 0 or absent
- Notification does not fire when version matches `CURRENT_RATE_SCHEMA_VERSION`

### Success Criteria for Phase 2
- Cost calculations for 2025-07-23 to 2026-06-30 use historical rate values
- Cost calculations for 2026-07-01 onward use new library values
- Version bump notification test passes
- 100% coverage maintained

---

## Phase 3: TOU Support (Rate 5 and Rate 7)

**Goal:** Rate 5 (pure TOU) and Rate 7 (TOU + demand) are now available in the
library. Add TOU cost calculation so users can select these rates.

### 3.1 — New `_calculate_tou_cost` in `cost.py`

TOU pricing does not use cumulative counters — each interval's cost depends
only on its time and season:

```python
from dominionsc import TimeOfUseCharge, Season
from datetime import timezone


def _calculate_tou_cost(
    interval_wh: float,
    interval_dt: datetime,
    rate_plan: RatePlan,
) -> float:
    """Calculate cost for one Wh interval under a TimeOfUseCharge."""
    for charge in rate_plan.charges:
        if not isinstance(charge, TimeOfUseCharge):
            continue
        season = Season.SUMMER if 5 <= interval_dt.month <= 9 else Season.WINTER
        periods = charge.periods_by_season[season]

        # interval_dt must be in America/New_York (utility timezone) for
        # window comparison. The coordinator stores intervals in the utility's
        # local timezone already, but confirm the tzinfo is correct here.
        local_dt = interval_dt.astimezone(ZoneInfo("America/New_York"))
        local_time = local_dt.time().replace(second=0, microsecond=0)

        fallback_price = None
        for period in periods:
            if period.fallback:
                fallback_price = period.price_per_unit
                continue
            for window in period.windows:
                # TimeWindow is a half-open interval [start, end)
                if window.start <= local_time < window.end:
                    return interval_wh * float(period.price_per_unit) / 1000

        # No window matched — use fallback period
        if fallback_price is not None:
            return interval_wh * float(fallback_price) / 1000
    return 0.0
```

**Edge cases to handle:**
- Window crossing midnight: `TimeWindow(time(22), time(6))` — the library
  definition appears to use non-crossing windows for Dominion SC but the
  engine should handle crossing windows defensively.
- DST transitions: Use `ZoneInfo("America/New_York")` for local time.
- Intervals that span multiple TOU periods (15-minute intervals in practice
  will not span 4-hour windows, but code should not assume this).

### 3.2 — Update `_calculate_cost_for_wh` for TOU Dispatch

Add TOU dispatch to the decision tree after tiered-rate dispatch:

```python
# In _calculate_cost_for_wh:
if rate_plan is not None:
    if interval_dt.date() < rate_plan.effective_from:
        return 0.0
    if _rate_plan_is_tiered(rate_plan):
        return _calculate_tiered_cost(interval_wh, interval_dt, cumulative_wh_before, rate_plan)
    if _rate_plan_is_tou(rate_plan):
        return _calculate_tou_cost(interval_wh, interval_dt, rate_plan)
```

Helper predicates:

```python
def _rate_plan_is_tiered(rate_plan: RatePlan) -> bool:
    from dominionsc import TieredUsageCharge

    return any(isinstance(c, TieredUsageCharge) for c in rate_plan.charges)


def _rate_plan_is_tou(rate_plan: RatePlan) -> bool:
    from dominionsc import TimeOfUseCharge

    return any(isinstance(c, TimeOfUseCharge) for c in rate_plan.charges)
```

### 3.3 — Demand Charges (Rate 7)

Rate 7 includes an `On-Peak Billing Demand` charge ($10.90/kW). Demand charges
are **not calculable** from 15-minute interval data stored in long-term
statistics because:
- The charge requires knowing the **maximum** 15-minute demand across the
  billing period, not the sum
- Billing statistics in HA are additive (sum), not max

**Decision: Skip demand charges in per-interval cost calculation.** Document
explicitly that Rate 7 cost statistics will show energy cost only (TOU portion)
and that the demand charge cannot be tracked per-interval. Add a UI description
string that communicates this to users.

Add `_rate_plan_has_demand(rate_plan)` predicate and log a debug message when
a demand charge is skipped.

### 3.4 — Update `aggregation.py`

The current `aggregate_hourly_data()` signature passes `cumulative_wh` to
`_calculate_cost_for_wh`. For TOU rates, `cumulative_wh` is passed but unused.
This is correct behavior — the function already exists; TOU just ignores the
`cumulative_wh_before` argument.

No signature change needed. Add a comment clarifying the behavior.

### 3.5 — Add Rate 5 and Rate 7 to `RATE_PLAN_REGISTRY`

```python
# In rates.py
from .const import COST_MODE_RATE_2, COST_MODE_RATE_5, COST_MODE_RATE_7

RATE_PLAN_REGISTRY = {
    mode: get_rate_plan(mode)
    for mode in (
        COST_MODE_RATE_2,
        COST_MODE_RATE_5,
        COST_MODE_RATE_6,
        COST_MODE_RATE_7,
        COST_MODE_RATE_8,
    )
    if get_rate_plan(mode) is not None
}
```

Add `COST_MODE_RATE_2 = "rate_2"`, `COST_MODE_RATE_5 = "rate_5"`,
`COST_MODE_RATE_7 = "rate_7"` to `const.py`.

### 3.6 — Tests

**New tests in `tests/test_rates.py` (or new `tests/test_cost_tou.py`):**

```
TestTouCostCalculation:
- on_peak interval (summer, 5 PM America/New_York) → $0.29907/kWh rate
- super_off_peak interval (summer, 2 AM America/New_York) → $0.09623/kWh rate
- off_peak interval (summer, noon America/New_York) → fallback $0.15074/kWh rate
- winter on_peak (7 AM America/New_York) → $0.29907/kWh rate
- winter super_off_peak (12:30 PM America/New_York) → $0.09623/kWh rate
- interval in UTC before local noon but local time is on-peak → correct dispatch
- DST transition interval (spring forward gap) → no crash

TestRatePlanDispatch:
- Rate 5 RatePlan → _rate_plan_is_tou() returns True
- Rate 8 RatePlan → _rate_plan_is_tiered() returns True
- Rate 7 RatePlan → _rate_plan_is_tou() is True AND _rate_plan_has_demand() is True

TestDemandChargeSkipped:
- Rate 7 cost calculation returns TOU-portion cost only (no demand component)
- Debug log message emitted when demand charge is present and skipped
```

### Success Criteria for Phase 3
- `_calculate_tou_cost()` correctly dispatches on- vs off-peak for both seasons
- Rate 5 and Rate 7 appear in `RATE_PLAN_REGISTRY`
- Rate 7 demand charge is silently skipped with a debug log
- 100% coverage maintained

---

## Phase 4: Gas Cost Calculation (Rate 32S and Rate 32V)

**Goal:** Enable cost calculation for the GAS account using Rate 32S (Standard)
and Rate 32V (Value).

### 4.1 — Understand the Unit Conversion

- Library API returns gas usage in **cubic feet (ft³)**
- Rate 32S and Rate 32V charge in **$/therm**
- 1 therm = 100 ft³
- `FlatUsageCharge.usage_unit == UsageUnit.THERM`
- Conversion: `cost = (interval_ft3 / 100.0) x price_per_therm`

### 4.2 — Update `_calculate_cost_for_wh` to Handle Gas

The function name `_calculate_cost_for_wh` is misleading for gas (usage is in
ft³, not Wh). Two options:

**Option A (preferred):** Rename to `_calculate_cost_for_interval(interval_usage, ...)` and
add an `account_type: str` parameter (`"ELECTRIC"` vs `"GAS"`). The caller in
`aggregation.py` already knows the account type.

**Option B:** Keep the function name, document that `interval_wh` is used as a
generic "interval usage" value (Wh for electric, ft³ for gas), and handle the
unit conversion inside based on `rate_plan.commodity`.

**Choose Option B** — it minimizes changes to call sites and keeps the
`coordinator.py` re-export clean. Add a comment documenting the dual usage.

**Gas dispatch in `_calculate_cost_for_wh`:**

```python
from dominionsc import FlatUsageCharge, Commodity

if _rate_plan_is_flat(rate_plan):  # Rate 32S, Rate 32V, Rate 2
    for charge in rate_plan.charges:
        if not isinstance(charge, FlatUsageCharge):
            continue
        if rate_plan.commodity == Commodity.GAS:
            # interval_usage is in ft³; convert to therms
            therms = interval_wh / 100.0
            return therms * float(charge.price_per_unit)
        else:
            # Electric flat rate (Rate 2); interval_usage is in Wh
            kwh = interval_wh / 1000.0
            return kwh * float(charge.price_per_unit)
```

### 4.3 — Add Gas Cost Statistics Series

Currently the GAS account writes only a consumption statistic (ft³). Add a
cost statistic (USD) for GAS when a gas rate plan is selected.

**In `models.py`:** Update `DominionSCStatisticMetadata` — the `cost_id` field
is currently `str | None` with a comment "only for ELECTRIC". Remove that
restriction; gas will now also have a `cost_id` when a gas rate is configured.

**In `statistics_ids.py`:** Update `_build_statistic_ids()` and
`_build_register_statistic_ids()` to generate gas cost IDs. The format:

```
dominionsc:{clean_addr}_gas_cost
```

This is a **new statistic series** for gas, not a change to existing IDs —
no migration risk.

### 4.4 — Add Gas Rate Mode to Config/Options Flow

Gas rate selection is separate from electric rate selection because a user may
have both ELECTRIC and GAS accounts on different rate plans.

**New `const.py` keys:**
```python
CONF_GAS_COST_MODE = "gas_cost_mode"
COST_MODE_RATE_32S = "rate_32s"
COST_MODE_RATE_32V = "rate_32v"
```

**New `RATE_PLAN_REGISTRY` entries:**
```python
GAS_RATE_PLAN_REGISTRY: dict[str, RatePlan] = {
    mode: get_rate_plan(mode) for mode in (COST_MODE_RATE_32S, COST_MODE_RATE_32V) if get_rate_plan(mode) is not None
}
```

**Config flow step change — `async_step_cost_mode`:** Add a second selector or
a separate step `async_step_gas_cost_mode` that only appears when the account
has GAS. Gate on presence of "GAS" in `measurement_types`.

**Options flow `async_step_init`:** Add `CONF_GAS_COST_MODE` field to the
existing options schema, with "None" as default (no gas cost = backward-
compatible).

### 4.5 — Coordinator `_push_cost_statistics` for Gas

Currently `_push_cost_statistics` skips gas accounts. Update to emit cost
statistics for gas when `CONF_GAS_COST_MODE != COST_MODE_NONE`.

The coordinator's `_process_and_insert_statistics` already passes
`is_electric` to `aggregate_hourly_data`. The aggregation function already
accepts gas data. Ensure it also calls `_calculate_cost_for_wh` for gas
intervals when a gas rate plan is configured.

### 4.6 — Tests

**`tests/test_coordinator.py`:**
```
TestGasCostStatistics:
- Rate 32S: 100 ft³ interval → cost = $2.04149 (1 therm x $2.04149/therm)
- Rate 32V: 100 ft³ interval → cost = $1.91847
- COST_MODE_NONE for gas → $0.0, no cost statistic emitted
- Gas cost statistic ID is generated and distinct from electric cost ID
- GAS account with Rate 32S selected → push_statistics called with gas cost rows
```

### Success Criteria for Phase 4
- Gas cost statistics are written to HA recorder when a gas rate is configured
- Unit conversion (ft³ → therms) is correct
- Gas cost statistic IDs don't collide with electric IDs
- Config flow presents gas rate selection only when GAS account exists
- 100% coverage maintained

---

## Phase 5: Rate 2 Support (Low-Use Flat Electric Rate)

**Goal:** Rate 2 is a flat-rate electric plan for low-use customers. It uses
`FlatUsageCharge` with no tiers.

### 5.1 — Update Cost Dispatch

Rate 2 uses a `FlatUsageCharge` (same charge type as gas rates). The dispatch
added in Phase 4 for `_rate_plan_is_flat()` already handles Rate 2 electric:

```
Electric flat rate (Rate 2):
  cost = interval_wh / 1000 x $0.13111
```

No new code needed if Phase 4 is implemented correctly. Verify that the
`Commodity.ELECTRICITY` branch handles Wh → kWh conversion.

### 5.2 — Add Eligibility Rule Display (Informational)

Rate 2 has an eligibility rule: "Usage must not exceed 400 kWh in each of
the preceding twelve billing periods." The integration cannot enforce this
(it's the utility's responsibility), but it can display it.

Add a brief warning in the config flow description for Rate 2 selection:
```json
"rate_2_eligibility_warning": "Rate 2 is available only to customers who
have not exceeded 400 kWh in each of the prior 12 billing periods.
Verify eligibility with Dominion before selecting this rate."
```

### 5.3 — Tests

```
TestRate2FlatCost:
- 500 Wh interval → $0.500 x 0.13111 = $0.065555
- Effective date gate: interval before 2026-07-01 → $0.0
- Rate 2 appears in RATE_PLAN_REGISTRY
- build_cost_mode_choices() includes Rate 2 label
```

---

## Phase 6: Config Flow and Options Flow Expansion

**Goal:** The config and options flow UI must expose all new rate options and
handle gas rate selection.

### 6.1 — `const.py` Changes

Add all new constants:

```python
# New cost mode constants
COST_MODE_RATE_2 = "rate_2"
COST_MODE_RATE_5 = "rate_5"
COST_MODE_RATE_7 = "rate_7"
COST_MODE_RATE_32S = "rate_32s"
COST_MODE_RATE_32V = "rate_32v"

# Gas cost config key
CONF_GAS_COST_MODE = "gas_cost_mode"

# Schema version tracking (for migration notifications)
CONF_LAST_RATE_SCHEMA_VERSION = "last_rate_schema_version"
CURRENT_RATE_SCHEMA_VERSION = 2
```

### 6.2 — `rates.py` `build_cost_mode_choices()` Update

Expand to include all electric rate plans in a logical order:

```python
def build_cost_mode_choices() -> dict[str, str]:
    """Electric cost mode choices for config/options flow selectors."""
    return {
        COST_MODE_NONE: "None (no cost calculation)",
        COST_MODE_RATE_8: "Rate 8 - Residential Service",
        COST_MODE_RATE_6: "Rate 6 - Energy Saver / Conservation Rate",
        COST_MODE_RATE_5: "Rate 5 - Time of Use",
        COST_MODE_RATE_7: "Rate 7 - Time-of-Use Demand (energy cost only)",
        COST_MODE_RATE_2: "Rate 2 - Low Use Residential Service",
        COST_MODE_FIXED: "Fixed Rate (custom $/kWh)",
    }


def build_gas_cost_mode_choices() -> dict[str, str]:
    """Gas cost mode choices for config/options flow selectors."""
    return {
        COST_MODE_NONE: "None (no cost calculation)",
        COST_MODE_RATE_32S: "Rate 32S - Gas Standard Service",
        COST_MODE_RATE_32V: "Rate 32V - Gas Value Service",
    }
```

### 6.3 — `config_flow.py` Changes

**`async_step_cost_mode`:**
- Use `build_cost_mode_choices()` which now includes Rate 2, 5, 7
- Add Rate 7 demand-charge disclaimer to the step description:
  `"Rate 7 calculates only the energy (TOU) portion. The monthly demand charge cannot be calculated from interval data."`

**New step `async_step_gas_cost_mode`:**
- Gate: only show if `"GAS" in measurement_types`
- Schema: `vol.Schema({vol.Required(CONF_GAS_COST_MODE, default=COST_MODE_NONE): selector.SelectSelector(...)}`
- Flow: `async_step_cost_mode → async_step_gas_cost_mode → async_step_backfill_options`
  (or skip gas step if no GAS account)

**`async_step_cost_mode_fixed_rate`:**
- Only reachable from the electric cost mode step (not gas)
- No changes needed

### 6.4 — `options_flow.py` Changes

**`async_step_init`:**
- Add `CONF_GAS_COST_MODE` field to the schema
- Only include gas rate selector when GAS account is present
- Gate similarly to config flow: check `coordinator.data.accounts`

### 6.5 — `strings.json` and `translations/en.json`

Add descriptions for new config flow steps:

```json
"config": {
  "step": {
    "gas_cost_mode": {
      "title": "Gas Cost Calculation",
      "description": "Select the rate schedule for your gas account cost calculation.",
      "data": {
        "gas_cost_mode": "Gas Rate Plan"
      }
    },
    "cost_mode": {
      "description": "Select the rate schedule for your electric account...\n\n**Rate 7 note:** Only the time-of-use energy portion is calculated. The monthly demand charge cannot be determined from interval data."
    }
  }
}
```

Also add:
- `entity.sensor` descriptions for any new gas cost sensor (if added)
- Error strings for invalid gas rate selection

### 6.6 — Tests

**`tests/test_config_flow.py`:**
```
TestGasCostModeStep:
- Gas account present → gas cost mode step shown after electric cost mode step
- No gas account → gas cost mode step skipped
- Rate 32S selection → CONF_GAS_COST_MODE = "rate_32s" saved in entry.options
- Default (None) for gas cost → CONF_GAS_COST_MODE = "none"

TestExpandedElectricRateModes:
- Rate 5 selectable in cost_mode step
- Rate 7 selectable; disclaimer present in step description
- Rate 2 selectable
- build_cost_mode_choices() returns 7 options in correct order
```

**`tests/test_rates.py`:**
```
- build_gas_cost_mode_choices() returns Rate 32S, Rate 32V, None
- build_cost_mode_choices() returns all 7 options in correct order
```

### Success Criteria for Phase 6
- All 7 electric cost modes appear in the config flow
- Gas rate selector appears for accounts with GAS
- `strings.json` and `translations/en.json` are complete
- Rate 7 demand-charge disclaimer is shown in UI
- 100% coverage maintained

---

## Phase 7: Sensor Updates for Gas Cost and Daily Charges

**Goal:** Expose gas cost-to-date and daily charge information via diagnostic
sensors.

### 7.1 — Gas Cost Sensor (Optional)

If gas cost statistics are being written (Phase 4), add a diagnostic sensor
`gas_cost_to_date` that reads from the gas cost statistic. This mirrors the
existing `cost_to_date` sensor for electric.

**Implementation:** Add to `sensor.py` an additional `DominionSCEntityDescription`
entry that queries the gas cost statistic via `statistics.get_last_statistics`.

This is a **low priority** addition — the billing forecast already shows total
bill including gas. Only add if the gas-only cost figure provides clear value.

### 7.2 — Daily Charge Exposure (Optional)

The library's Rate 8, Rate 6, and Rate 2 now use `DailyCharge($0.36164/day)`
instead of `MonthlyCharge($9.00/month)`. The DER monthly charge ($1.00/month)
is separate.

These fixed charges are NOT currently tracked in HA statistics (by design —
the integration tracks usage-based costs only). However, users may want to know
their fixed charges to understand the full bill.

**Decision: Keep fixed charges out of energy statistics for now.** Add
information to the options flow description (e.g., "Rate 8 includes a
$0.36164/day Basic Facilities Charge and $1.00/month DER charge, which are
not included in the cost statistics.") and to `README.md`.

Revisit in a future phase if there is user demand for a fixed-charge sensor.

---

## Phase 8: Dependency Update and Version Bump

**Goal:** Update the `dominion-sc-power` version pin and bump the HA integration
version.

### 8.1 — `manifest.json`

```json
"requirements": ["dominion-sc-power==0.1.0"],
"version": "0.1.0"
```

The version number is a policy decision — given the breaking change in rate
values and the new features (gas costs, TOU support), a minor-version bump
from `0.0.7` to `0.1.0` is appropriate.

### 8.2 — `pyproject.toml`

Update the `[project.optional-dependencies]` dev section if it pins the library:

```toml
[project.optional-dependencies]
dev = [
    "dominion-sc-power>=0.1.0",
    ...
]
```

Also confirm that the `[tool.pytest.ini_options]` `pythonpath` includes the
library's new source layout if it changed.

### 8.3 — `uv.lock`

Run `uv lock` to regenerate `uv.lock` after the requirement change. Do not
manually edit the lockfile.

### 8.4 — `README.md`

Update:
- Known limitations: remove "solar support" from the limitation section (Phase
  5 of the previous refactor already removed it; confirm it's been removed)
- Add note about TOU support (Rate 5, Rate 7)
- Add note about gas cost calculation (Rate 32S, Rate 32V)
- Add note about demand charges being excluded from Rate 7 statistics
- Update rate values table if one exists

### 8.5 — `DEVELOPER.md`

Update:
- "Things That Must Not Change" section: add `CONF_LAST_RATE_SCHEMA_VERSION` key
- "Adding a new rate schedule" recipe: replace the old 3-step process with
  the new process (rate plans come from the library; adding one requires only
  updating `RATE_PLAN_REGISTRY` and the config flow choices)
- Module Reference: update `rates.py` description

### Success Criteria for Phase 8
- `manifest.json` pins the correct library version
- `uv lock` completes without conflict
- All 163+ tests pass against the new library version
- README accurately reflects all supported rate plans

---

## Implementation Order and Dependencies

```
Phase 1 (rate model migration)
  └── Phase 2 (versioned rate history + migration prompt)
        └── Phase 3 (TOU support: Rate 5, Rate 7)
              └── Phase 4 (gas cost: Rate 32S, Rate 32V)
                    └── Phase 5 (Rate 2 flat electric)
                          └── Phase 6 (config/options flow expansion)
                                └── Phase 7 (sensor additions — optional)
                                      └── Phase 8 (dependency bump, version)
```

Phases 1–3 are the critical path and must be done sequentially (each builds
on the previous cost engine change). Phases 4–6 are largely independent once
Phase 3 is complete and can be developed in parallel if needed.

Phase 8 should be the last commit because updating the version pin before all
code is ready risks breaking CI on intermediate commits.

---

## Release-Critical Constraints (Must Not Change)

These are inherited from the previous refactor and remain in force:

| Item | Why |
|------|-----|
| `_build_statistic_ids()` output format | Changing orphans all existing Energy Dashboard history |
| `clean_service_addr()` normalization | Same as above |
| `DOMAIN = "dominionsc"` | Part of statistic ID prefix |
| `CONF_LOGIN_DATA` key | Changing logs out all existing users |
| `_backfill_initiated` guard | Prevents race-condition duplicate backfill |
| Phase 5 register-ID stability design | Established installs must never reach `_discover_registers()` |

**New constraint from this migration:**
- `COST_MODE_RATE_8 = "rate_8"` and `COST_MODE_RATE_6 = "rate_6"` must
  remain unchanged — they are stored in `entry.options` for all existing users.
  Changing them would lose all users' cost mode selection on next HA restart.

---

## Appendix A: Rate Value Quick Reference

### Rate 8 (Residential Service) — effective 2026-07-01

| Period | Tier | $/kWh |
|--------|------|-------|
| Summer (May–Sep) | First 800 kWh | $0.15878 |
| Summer (May–Sep) | Over 800 kWh | $0.17442 |
| Winter (Oct–Apr) | First 800 kWh | $0.15878 |
| Winter (Oct–Apr) | Over 800 kWh | $0.15253 |
| All | Basic Facilities | $0.36164/day |
| All | DER Program | $1.00/month |

### Rate 6 (Energy Saver) — effective 2026-07-01

| Period | Tier | $/kWh |
|--------|------|-------|
| Summer | First 800 kWh | $0.15333 |
| Summer | Over 800 kWh | $0.16842 |
| Winter | First 800 kWh | $0.15333 |
| Winter | Over 800 kWh | $0.14729 |
| All | Basic Facilities | $0.36164/day |
| All | DER Program | $1.00/month |

### Rate 5 (Time of Use) — effective 2026-07-01

| Season | Period | Hours (ET) | $/kWh |
|--------|--------|-----------|-------|
| Summer | On-Peak | 4:00 PM – 8:00 PM | $0.29907 |
| Summer | Super Off-Peak | 1:00 AM – 5:00 AM | $0.09623 |
| Summer | Off-Peak | All other hours | $0.15074 |
| Winter | On-Peak | 6:00 AM – 9:00 AM | $0.29907 |
| Winter | Super Off-Peak | 1:00 AM – 5:00 AM, 12:00 PM – 3:00 PM | $0.09623 |
| Winter | Off-Peak | All other hours | $0.15074 |
| All | Basic Facilities | — | $0.42740/day |
| All | DER Program | — | $1.00/month |

### Rate 7 (TOU Demand) — effective 2026-07-01

| Season | Period | Hours (ET) | $/kWh |
|--------|--------|-----------|-------|
| Summer | On-Peak | 4:00 PM – 8:00 PM | $0.17441 |
| Summer | Super Off-Peak | 1:00 AM – 5:00 AM | $0.08841 |
| Summer | Off-Peak | All other hours | $0.10124 |
| Winter | On-Peak | 6:00 AM – 9:00 AM | $0.17441 |
| Winter | Super Off-Peak | 1:00 AM – 5:00 AM, 12:00 PM – 3:00 PM | $0.08841 |
| Winter | Off-Peak | All other hours | $0.10124 |
| All | On-Peak Billing Demand | — | $10.90/kW (NOT tracked) |
| All | Basic Facilities | — | $0.42740/day |
| All | DER Program | — | $1.00/month |

### Rate 2 (Low Use) — effective 2026-07-01

| Period | $/kWh |
|--------|-------|
| All | $0.13111 (flat) |
| All | $0.36164/day (Basic Facilities) |
| All | $1.00/month (DER) |

### Rate 32S (Gas Standard) — effective 2026-07-01

| Period | $/therm |
|--------|---------|
| All | $2.04149 (flat) |
| All | $10.90/month (Basic Facilities) |

### Rate 32V (Gas Value) — effective 2026-07-01

| Period | $/therm |
|--------|---------|
| All | $1.91847 (flat) |
| All | $10.90/month (Basic Facilities) |

---

## Appendix B: Historical Rate Values (2025-07-23 to 2026-06-30)

These are the superseded tariff values that must be preserved internally for
historical cost recalculation (Phase 2). They are NOT available in the library.

### Rate 8 — historical (2025-07-23 to 2026-06-30)

| Period | Tier | $/kWh |
|--------|------|-------|
| Summer | First 800 kWh | $0.14599 |
| Summer | Over 800 kWh | $0.15983 |
| Winter | First 800 kWh | $0.14599 |
| Winter | Over 800 kWh | $0.14045 |
| All | Basic Facilities | $9.00/month |

### Rate 6 — historical (2025-07-23 to 2026-06-30)

| Period | Tier | $/kWh |
|--------|------|-------|
| Summer | First 800 kWh | $0.14164 |
| Summer | Over 800 kWh | $0.15505 |
| Winter | First 800 kWh | $0.14164 |
| Winter | Over 800 kWh | $0.13628 |
| All | Basic Facilities | $9.00/month |
