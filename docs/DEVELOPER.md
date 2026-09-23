# Developer Guide — ha-dominion-sc

This document is a reference for developers maintaining or extending the
Dominion Energy SC Home Assistant integration. It covers architecture,
key concepts, common tasks, and things that are easy to get wrong.

---

## Table of Contents

1. [Project Overview](#1-project-overview)
2. [Repository Layout](#2-repository-layout)
3. [Architecture & Data Flow](#3-architecture--data-flow)
4. [Module Reference](#4-module-reference)
5. [Key Concepts](#5-key-concepts)
6. [How to Add a New Feature](#6-how-to-add-a-new-feature)
7. [Testing](#7-testing)
8. [Running Locally](#8-running-locally)
9. [Things That Must Not Change](#9-things-that-must-not-change)
10. [Debugging Tips](#10-debugging-tips)

---

## 1. Project Overview

This is a [Home Assistant](https://www.home-assistant.io/) custom integration
that connects to the **Dominion Energy South Carolina** customer portal and
pulls hourly energy usage data into HA's long-term statistics database. That
data appears in the **Energy Dashboard** and can be used to track consumption
trends and estimated costs.

The integration also exposes a handful of **sensor entities** for billing
metadata (cost to date, forecasted cost, billing cycle dates).

The Dominion API is accessed via the external
[dominion-sc-power](https://github.com/sctigercat1/dominion-sc-power) library
(`dominionsc` package). This integration does not implement any HTTP calls
directly.

---

## 2. Repository Layout

```
ha-dominion-sc/
├── custom_components/dominionsc/   # Integration source code
│   ├── __init__.py                 # HA entry point (setup/unload)
│   ├── manifest.json               # HA integration manifest
│   ├── coordinator.py              # Data coordinator (API + statistics)
│   ├── config_flow.py              # Initial setup wizard
│   ├── options_flow.py             # Post-install settings
│   ├── sensor.py                   # HA sensor entities
│   ├── const.py                    # Constants and helpers
│   ├── models/                     # Pure dataclasses (no HA dependency), one per module
│   │   ├── statistic_metadata.py   # DominionSCStatisticMetadata
│   │   ├── account_data.py         # DominionSCAccountData
│   │   └── coordinator_data.py     # DominionSCData
│   ├── rates.py                    # Rate plan adapter (COST_MODE_* → library RatePlan)
│   ├── cost.py                     # Per-interval cost calculation
│   ├── billing.py                  # Billing-cycle boundary estimation
│   ├── aggregation.py              # Hourly interval aggregation
│   ├── statistics_ids.py           # Statistic ID construction
│   ├── strings.json                # UI translatable strings
│   └── translations/en.json        # English translations
├── tests/                          # Test suite
│   ├── conftest.py                 # pytest fixtures
│   ├── _fake_recorder.py           # In-memory recorder stand-in (see Section 7)
│   ├── test_config_flow.py
│   ├── test_coordinator.py
│   ├── test_coordinator_scenarios.py  # Multi-poll scenarios via _fake_recorder
│   ├── test_phase5_register_aware.py
│   ├── test_sensor.py
│   ├── test_rates.py
│   ├── test_const.py
│   └── test_init.py
├── docs/
│   ├── REFACTOR_PLAN.md            # History of the 5-phase refactor
│   ├── LIBRARY_MIGRATION_PLAN.md   # History of moving rate definitions to the library
│   └── windows-testing-setup.md
├── pyproject.toml                  # Build config, ruff, pytest settings
├── uv.lock                         # Locked dependencies
└── hacs.json                       # HACS metadata
```

---

## 3. Architecture & Data Flow

### 3.1 Integration Lifecycle

```
User installs integration
        |
config_flow.py  ──  Collects credentials, TFA, backfill prefs, cost mode
        |
__init__.async_setup_entry()
        |
coordinator.py  ──  Created, first refresh triggered
        |
Every 6 hours: _async_update_data()
        |
        ├── api.async_login()          Re-authenticate
        ├── api.async_get_accounts()   Get account types (ELECTRIC, GAS)
        ├── api.async_get_forecast()   Get billing forecast
        └── _insert_statistics()       Insert hourly stats into HA recorder
                |
                └── coordinator.data   Returned to sensor entities
```

### 3.2 Statistics Insertion Pipeline

```
_insert_statistics(accounts, service_addr, forecast)
    │
    for each account (ELECTRIC, GAS):
    │
    ├── get_last_statistics(legacy_id)  ──  Check if this is an established install
    │
    ├── [Established install / backfill in flight]
    │       └── _process_account(usage_point_id=None, legacy IDs)
    │
    └── [New install — never backfilled]
            │
            └── _discover_registers(account)  ──  One-time network call
                    │
                    ├── [0-1 registers]  →  _process_account(usage_point_id=None, legacy IDs)
                    └── [2+ registers]   →  for each register:
                                                _process_account(usage_point_id=<id>, per-register IDs)

_process_account()
    │
    ├── [No existing stats + backfill not started]  →  _backfill_statistics()
    ├── [No existing stats + backfill started]      →  (skip — wait for recorder)
    └── [Stats exist]                               →  _update_statistics()

Both paths converge at:
_process_and_insert_statistics()
    │
    ├── API fetch (usage reads or register reads)
    ├── Filter zero-consumption days
    ├── _aggregate_hourly_data()  →  aggregation.aggregate_hourly_data()
    └── async_add_external_statistics()  →  HA recorder
```

### 3.3 Pure vs HA-Dependent Modules

| Module | Has HA dependency? | Purpose |
|---|---|---|
| `models/` | No | Data containers (one dataclass per module, re-exported from the package) |
| `rates.py` | No | Rate plan adapter (COST_MODE_* → library RatePlan) |
| `cost.py` | No | Per-interval cost calculation |
| `billing.py` | No | Billing-cycle boundary estimation |
| `aggregation.py` | No | Hourly interval bucketing |
| `statistics_ids.py` | No | Statistic ID string construction |
| `coordinator.py` | Yes | Orchestration, API calls, recorder writes |
| `sensor.py` | Yes | HA sensor entities |
| `config_flow.py` | Yes | Setup wizard |
| `options_flow.py` | Yes | Post-install settings |
| `__init__.py` | Yes | HA integration entry point |

The pure modules are intentionally isolated so they can be unit-tested
without mocking HA internals.

---

## 4. Module Reference

### `coordinator.py` — DominionSCCoordinator

The central orchestrator. Key methods:

| Method | When called | What it does |
|---|---|---|
| `_async_update_data()` | Every 6h by HA | Logs in, fetches data, inserts stats |
| `_insert_statistics()` | From `_async_update_data` | Routes accounts to backfill or update |
| `_discover_registers()` | Once per new account | Counts physical meter registers |
| `_process_account()` | Per account/register | Decides backfill vs incremental |
| `_backfill_statistics()` | First poll for an account | Loads historical data |
| `_update_statistics()` | Subsequent polls | Adds new + gap-fills |
| `_process_and_insert_statistics()` | Both paths above | Fetches, aggregates, writes |
| `async_recalculate_historic_costs()` | From options flow | Re-prices historical data for ELECTRIC and/or GAS (whichever has a non-`COST_MODE_NONE` mode in the new options), each via `_recalculate_one_account()` |

### `rates.py` — Rate schedule adapter

Thin adapter between the integration's `COST_MODE_*` string keys and the
`dominion-sc-power` library's `RatePlan` objects. Rate definitions live in
the library; this module provides `RATE_PLAN_REGISTRY` (electric) and
`GAS_RATE_PLAN_REGISTRY` (gas), which mirror the library's
`RESIDENTIAL_ELECTRIC_RATE_PLANS` / `RESIDENTIAL_GAS_RATE_PLANS` catalogs, and
the UI choice builders `build_cost_mode_choices()` /
`build_gas_cost_mode_choices()`, which label each plan with its library
`RatePlan.name` (plus short HA notes for Rate 1 and Rate 7).

### `billing.py` — Billing cycle estimation

Estimates historical billing-cycle boundaries from the single known current
cycle provided by the API. Required for tiered-rate calculations (the tier
resets at each cycle boundary).

### `statistics_ids.py` — ID construction

Builds the string IDs used as primary keys in the HA recorder. **Do not
change existing ID-generation logic** — see Section 9.

---

## 5. Key Concepts

### 5.1 Long-term Statistics vs Sensors

HA has two ways to store numeric time-series data:

- **State history**: stored when a sensor's state changes. Short retention,
  not suitable for high-resolution energy data.
- **Long-term statistics**: stored explicitly via `async_add_external_statistics`.
  Retained indefinitely, used by the Energy Dashboard.

This integration uses **long-term statistics** for energy and cost data.
The sensor entities exposed by `sensor.py` only show billing metadata
(cost-to-date, forecast, etc.) — they do not produce the energy statistics.

### 5.2 Cumulative Sum Statistics

HA long-term statistics have two value fields per row:
- `state`: the value *for that hour* (e.g. 1500 Wh consumed this hour).
- `sum`: the *running total* since the beginning of the series (e.g. 450,000 Wh total).

Both must be accurate. The coordinator reads the last `sum` from the recorder
and continues from that value when appending new rows.

### 5.3 Tiered Rate Calculations

Dominion's Rate 8, Rate 6, and Rate 1 are two-tier rates:
- **First 800 kWh** per billing cycle: charged at `rate_under`.
- **Over 800 kWh**: charged at `rate_over`.

The integration tracks cumulative Wh consumed within each billing cycle.
When the cumulative counter crosses 800 kWh, the cost for that interval is
split: part at `rate_under`, part at `rate_over`. See `cost._calculate_tiered_cost()`.

The cumulative counter **resets to 0** at each billing-cycle boundary. The
boundaries are estimated by `billing._estimate_billing_cycles()`.

### 5.3.1 Rate History

Rates change over time, so the price for an interval depends on its date, not
only on the selected plan. The `dominion-sc-power` library keeps every known
tariff period for a plan (for example `RATE_8_2025` for 2025-07-23 through
2026-06-30, then the current `RATE_8`).
`cost._calculate_cost_for_wh()` calls `dominionsc.get_rate_plan_for_date()` with
the plan's code and the interval's date, and prices the interval with whichever
period is returned. Intervals before the earliest known period cost $0.
Superseded periods carry only the usage charge, which is all this integration
prices. To add a future tariff change, add the new plan and archive the old one
in the library. This integration needs no code change beyond bumping the
library version (and `CURRENT_RATE_SCHEMA_VERSION`, see Section 9).

### 5.4 Register Discovery (Phase 5)

Most accounts have a single physical meter. Net-metered solar accounts have
two registers on the same meter: one for grid delivery (import) and one for
solar export.

On the very first backfill for a new account, the coordinator calls
`_discover_registers()` to count registers. Based on the result:
- 1 register → use the legacy, merged-register statistic IDs.
- 2+ registers → use per-register IDs that embed the `usage_point_id`.

Established installs (those with existing statistics in the recorder) **never**
reach the discovery code path. This is the fundamental guarantee that
existing Energy Dashboard history is never broken.

### 5.5 Backfill vs Incremental Update

**Backfill** (first setup):
- Starts from 0 for `consumption_sum` and `cost_sum`.
- Fetches from either the billing-cycle start or 365 days ago.

**Incremental update** (subsequent polls):
- Continues from the last recorded `sum` values.
- Re-examines the last `LOOKBACK_DAYS` days to catch late-arriving data.
- Already-recorded hours are skipped in output but still contribute to the
  cumulative Wh counter for tier accuracy.

### 5.6 `entry.data` vs `entry.options`, and why some changes need a reload

`entry.options` (changed via the options flow) only triggers
`update_listener` → `coordinator.async_request_refresh()` — the existing
`DominionSCCoordinator` instance (and its `self.api` client) keeps running.
That's fine for preferences the coordinator reads fresh on every poll (cost
mode, gas cost mode).

`entry.data` is different: it's read once, at construction time, in
`DominionSCCoordinator.__init__()` (credentials, `CONF_LOGIN_DATA`,
`CONF_PILOT_ID`). Changing an `entry.data` value without recreating the
coordinator leaves it running against stale data until the next full HA
restart.

`CONF_PILOT_ID` is the example: it's a connection parameter for the
`DominionSC` API client (like the credentials), not a display preference, so
it belongs in `entry.data`. But the options flow is still the natural place
for the user to *change* it later without a full reconfigure. The fix in
`options_flow.py`'s `async_step_init` is to detect the change, write it with
`hass.config_entries.async_update_entry(..., data={...})`, and explicitly
call `hass.config_entries.async_schedule_reload(entry.entry_id)` — which tears
down and rebuilds the coordinator via `async_setup_entry`, exactly like a
restart would. If you add another `entry.data` field that's editable via the
options flow, follow this same pattern.

### 5.7 Fire-and-forget background tasks must handle their own errors

`async_recalculate_historic_costs()` is launched via
`hass.async_create_task(...)` from the options flow and never awaited by its
caller — the options flow saves and returns immediately (see its module
docstring). That means **nothing else will ever observe an exception raised
inside it.** An uncaught error (a network timeout, the connection being torn
down by an HA restart mid-request, etc.) doesn't fail loudly in a place the
user would see — it becomes an "unhandled task exception" traceback dumped
into the log by asyncio itself, with zero indication to the user that their
recalculation silently failed.

Any coroutine handed to `hass.async_create_task` (or otherwise fired without
being awaited) must catch its own expected exceptions at the top level and
surface them some other way — here, via `persistent_notification`. Don't
rely on a future caller to add error handling; there isn't one.

### 5.8 Recalculation is the *only* thing that re-prices already-recorded consumption

Regular polling (`_process_and_insert_statistics` → `aggregate_hourly_data`)
only ever prices **new** hours — any hour already in `existing_hours` is
skipped before the cost-calculation block even runs (see
`aggregation.py`'s `existing_hours` check, which happens *before* pricing).
This means once an hour of consumption is written to the recorder, its cost
is frozen at whatever it was priced at (or never priced at all, if no cost
mode was active yet) — no future regular poll will ever revisit it, no
matter what the cost mode later changes to.

`async_recalculate_historic_costs()` is the only code path that re-prices
existing rows, which is why it has to handle **every** account that might
need it, not just the one the user most recently touched. It didn't
originally — it only recalculated ELECTRIC on the (once-true) assumption
that gas had no cost statistic. Once gas cost modes were added, a user
enabling a gas rate plan had no way to price consumption recorded before
(or without) a priced cost — it silently stayed at $0.00 forever, since
nothing else was ever going to reprice it. The fix, `_recalculate_one_account`
(Section 4), made the pricing logic account-agnostic and
`_async_recalculate_historic_costs_locked` calls it once per account that
has a non-`COST_MODE_NONE` mode in the new options — independently, so
changing only gas (with electric untouched) still triggers a real
recalculation instead of silently no-op'ing.

The options flow's `async_step_init` had a matching bug: it only offered the
recalculate-history prompt based on the *electric* mode selection. A user
with electric cost mode `None` who only turned on a gas rate plan would
never even see the recalculation option. Fixed by also checking whether the
new gas mode is non-`COST_MODE_NONE` (see `options_flow.py`'s
`async_step_init`).

Relatedly, `async_step_recalculate_date_range`'s default start date used to
be the 1st of the current month — an easy trap, since it looks like a
sensible default but silently limits recalculation to the current billing
cycle. `_async_earliest_consumption_date()` now defaults it to the earliest
recorded consumption for whichever account(s) are being recalculated
(querying with `period="month"` so it's cheap even across a year-plus of
history), so accepting the default reprices full history rather than a
sliver of it. If you add a case where recalculation should be scoped
differently, don't reintroduce a hardcoded start date without a strong
reason — it's exactly the kind of thing that looks fine until someone's
old data quietly stays unpriced.

### 5.9 Zero-Consumption Filtering

The Dominion API sometimes returns all-zero intervals for a day when that
day's data hasn't been processed yet (e.g. very recent days or holidays).
Inserting these zeros would create false "no usage" records in the recorder.

The coordinator calculates a per-day total and filters out entire days where
the total is zero. This means a day that genuinely had zero usage (extremely
rare for a household) would also be filtered. This is an accepted trade-off
because it is far more common for zeros to indicate missing data.

---

## 6. How to Add a New Feature

### Add a new rate schedule

Rate definitions live in the `dominion-sc-power` library, not in the
integration. Both registries and the UI selectors are built from the library's
residential catalogs, so a plan added there (with a `RatePlan.code` such as
`rate_9`) appears in the selector automatically, labelled with its
`RatePlan.name`. Optionally:

- In `const.py`: add a `COST_MODE_*` constant (value equal to the library
  code) only if the integration refers to the plan by name in code.
- In `rates.py`: add the code to `_ELECTRIC_DISPLAY_ORDER` to place it among
  the common plans (otherwise it is listed after them), or to
  `_LABEL_SUFFIXES` to append an integration-specific note to its label.

`_resolve_cost_config()` and `_resolve_gas_cost_config()` pick up the new
entry automatically.

### Add a new sensor

1. In `sensor.py`: add a new `DominionSCAccountSensorDescription` to
   `ACCOUNT_SENSORS` (for per-account data) or a
   `DominionSCBillingSensorDescription` to `BILLING_SENSORS` (for forecast
   data).
2. Add the translation key to `strings.json` and `translations/en.json`.
3. Add a test in `test_sensor.py`.

Description fields:
- `value_fn`: required. Callable that receives `DominionSCAccountData` (account
  descriptions) or `DominionSCData` (billing descriptions) and returns the
  sensor's state value. Return `None` to mark the sensor unavailable.
- `last_reset_fn`: optional, billing descriptions only. Only set this for `SensorStateClass.TOTAL` sensors
  whose value resets mid-stream (e.g. a billing-cycle running total). Callable
  that receives the full `DominionSCData` and returns a UTC-aware `datetime`
  of the last reset. HA uses this to avoid misclassifying the drop as a negative
  delta. Leave `None` (the default) for all other sensors.

**State class guidance:**
- Use `TOTAL` for running sums that reset on a known event (e.g. `cost_to_date`
  resets at billing cycle start). Always pair with `last_reset_fn`.
- Use no `state_class` at all (leave it unset) for point-in-time values such
  as forecasts or averages (e.g. `forecasted_cost`, `typical_cost`).
  **`device_class=MONETARY` only accepts `state_class` of `None` or `TOTAL`
  — `MEASUREMENT` is invalid for monetary sensors and HA will log a startup
  warning ("state class 'measurement' which is impossible considering device
  class") and refuse to record it as a statistic.** This bit us once already
  — see `test_forecasted_cost_state_class_is_none` / `test_typical_cost_state_class_is_none`
  in `test_sensor.py`.

### Add a new config/options field

1. Add a `CONF_*` constant in `const.py`.
2. Decide whether it belongs in `entry.data` or `entry.options` — see
   Section 5.6. A user preference the coordinator re-reads every poll goes
   in `entry.options`; a connection parameter the coordinator only reads at
   construction time goes in `entry.data` and needs an explicit
   `async_schedule_reload()` when changed post-install.
3. Add the form field in the relevant `config_flow.py` or `options_flow.py`
   step.
4. Read the value in the coordinator (via `config_entry.options.get()` or
   `config_entry.data.get()`, per step 2).
5. Update `tests/test_config_flow.py` / `test_coordinator_*.py` accordingly.

---

## 7. Testing

### Running the tests

```bash
# Install dev dependencies (uv required — https://docs.astral.sh/uv/)
uv sync

# Run all tests with coverage
uv run pytest --cov=custom_components/dominionsc --cov-report=term-missing

# Run a specific test file
uv run pytest tests/test_coordinator.py -v

# Run a specific test
uv run pytest tests/test_coordinator.py::test_cost_tiered_straddles_boundary -v
```

### Test structure

Tests are in `tests/`. The main fixtures are in `conftest.py`:
- `hass` — a real (but in-memory) HA instance.
- Mocked `DominionSC` API client via `unittest.mock.AsyncMock`.
- Mocked recorder functions (`get_last_statistics`, `statistics_during_period`,
  `async_add_external_statistics`).

The pure modules (`billing.py`, `rates.py`, `cost.py`, `aggregation.py`,
`statistics_ids.py`) are tested with synthetic inputs — no HA mocking needed.

Most coordinator tests mock each recorder call individually with a
hand-built `side_effect` for one specific call sequence — precise, but
tedious to extend across several poll cycles. `tests/_fake_recorder.py`
provides `FakeStatisticsStore`, a lightweight in-memory stand-in for the
real recorder (dedupes by hour, returns POSIX-timestamp floats exactly like
the real recorder does) plus a `patched_recorder()` context manager that
wires it in. Use it when a test needs to drive the coordinator through
several real `_async_update_data()` polls and assert on the final
accumulated statistics — see `tests/test_coordinator_scenarios.py` for
worked examples (a multi-poll gap-fill, and a recalculation run against real
backfilled data).

### Coverage target

The goal is 100% line and branch coverage. CI reports coverage but does not
fail the build below a threshold, so check the `term-missing` output yourself
and add tests for every new branch.

### Test files by area

| File | What it covers |
|---|---|
| `test_config_flow.py` | All config flow steps, TFA, reauth, gas cost mode, pilot ID |
| `test_coordinator.py` | Comprehensive coordinator scenarios, cost calculation, statistics pipeline |
| `test_coordinator_scenarios.py` | Multi-poll scenarios (gap-fill, recalculation) via `_fake_recorder.py` |
| `test_phase5_register_aware.py` | Register discovery, multi-register routing |
| `test_sensor.py` | Sensor entity value extraction, gas cost sensor |
| `test_rates.py` | Rate plan registry, cost mode choices, prior-period pricing via library history |
| `test_const.py` | `clean_service_addr` output |
| `test_init.py` | `async_setup_entry`, `async_unload_entry`, schema version notification |

---

## 8. Running Locally

### Development setup (macOS/Linux)

```bash
git clone https://github.com/sctigercat1/ha-dominion-sc
cd ha-dominion-sc

# Install uv if not already installed
curl -LsSf https://astral.sh/uv/install.sh | sh

# Install all dependencies
uv sync

# Run tests
uv run pytest

# Lint and format
uv run ruff check custom_components/ tests/
uv run ruff format custom_components/ tests/
```

See `docs/windows-testing-setup.md` for Windows-specific instructions.

### Installing into a local HA instance

1. Copy `custom_components/dominionsc/` into your HA config directory under
   `custom_components/dominionsc/`.
2. Restart HA.
3. Go to **Settings → Integrations → Add Integration** and search for
   "Dominion Energy SC".

---

## 9. Things That Must Not Change

These items are **release-critical**. Changing them breaks existing installs.

### Statistic IDs

Every statistic ID written to the HA recorder becomes a permanent key in the
user's database. Changing the ID formula orphans all existing data — the
Energy Dashboard shows a gap and the old data is no longer associated with the
new ID.

**Do not modify:**
- `statistics_ids._build_statistic_ids()` — any change to the formula or
  output format breaks every existing single-register install.
- `const.clean_service_addr()` — the ID uses the cleaned address as a prefix.
- `const.DOMAIN` — the ID uses the domain as a prefix.

If you must change ID construction (e.g. to fix a collision), implement a
migration step that reads old rows from the recorder and rewrites them under
the new ID.

### `_backfill_initiated` guard

The `_backfill_initiated` dict prevents duplicate backfills when the recorder
hasn't committed the first batch yet. Do not remove this guard or change its
key format without understanding the race condition it prevents.

### `CONF_LOGIN_DATA` key

Stored in `entry.data`. Changing this key (or removing it) logs out all
existing users on the next HA restart.

### `CONF_LAST_RATE_SCHEMA_VERSION` key and `CURRENT_RATE_SCHEMA_VERSION`

`CONF_LAST_RATE_SCHEMA_VERSION` is stored in `entry.data` and tracks which
tariff values the user's statistics were last computed against. Increment
`CURRENT_RATE_SCHEMA_VERSION` only when tariff rates change (not when new
rates are added). Do not rename the key — renaming it would suppress the
migration notification for existing users who need to recalculate.

### `COST_MODE_*` rate values (`"rate_8"`, `"rate_6"`, `"rate_1"`, etc.)

These string values are stored in `entry.options` for every existing user.
Changing them would silently clear all users' cost mode selection on the
next HA restart.

---

## 10. Debugging Tips

### Enabling debug logs

In `configuration.yaml`:

```yaml
logger:
  default: warning
  logs:
    custom_components.dominionsc: debug
```

This enables `_LOGGER.debug(...)` calls throughout the coordinator, which show
API call timing, statistics insertion counts, and gap-fill decisions.

### Checking what statistics are in the recorder

Use **Developer Tools** → **Statistics** and search for `dominionsc`. The
statistics are external statistics, not entities, so they don't appear in the
States or Template tools.

To query the SQLite database directly:
```sql
SELECT statistic_id, COUNT(*) as rows, MAX(start) as latest
FROM statistics
WHERE statistic_id LIKE 'dominionsc:%'
GROUP BY statistic_id;
```

### Forcing a coordinator refresh

From HA Developer Tools → Services:

```yaml
service: homeassistant.reload_config_entry
target:
  entity_id: sensor.dominion_energy_sc_<your_address>_last_updated
```

### Common errors

| Error | Likely cause |
|---|---|
| `ConfigEntryAuthFailed` | TFA session token expired; HA will prompt for re-auth |
| `UpdateFailed` | Network error; HA retries automatically |
| Statistics not appearing in Energy Dashboard | Statistic ID mismatch; check logs for the actual ID being used |
| Costs are zero for historical data | Interval predates the earliest tariff period the library knows for that plan (see 5.3.1) |
| Duplicate statistics / incorrect sums | Backfill ran twice; check `_backfill_initiated` state |
