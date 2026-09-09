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
│   ├── models.py                   # Pure dataclasses (no HA dependency)
│   ├── rates.py                    # Rate schedule definitions + calculations
│   ├── cost.py                     # Per-interval cost calculation
│   ├── billing.py                  # Billing-cycle boundary estimation
│   ├── aggregation.py              # Hourly interval aggregation
│   ├── statistics_ids.py           # Statistic ID construction
│   ├── strings.json                # UI translatable strings
│   └── translations/en.json        # English translations
├── tests/                          # Test suite (163 tests, 100% coverage)
│   ├── conftest.py                 # pytest fixtures
│   ├── test_config_flow.py
│   ├── test_coordinator_*.py       # Coordinator tests (multiple files)
│   ├── test_phase5_register_aware.py
│   ├── test_sensor.py
│   ├── test_rates.py
│   ├── test_const.py
│   └── test_init.py
├── docs/
│   ├── REFACTOR_PLAN.md            # History of the 5-phase refactor
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
Every 12 hours: _async_update_data()
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
| `models.py` | No | Data containers |
| `rates.py` | No | Rate schedule definitions and math |
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
| `_async_update_data()` | Every 12h by HA | Logs in, fetches data, inserts stats |
| `_insert_statistics()` | From `_async_update_data` | Routes accounts to backfill or update |
| `_discover_registers()` | Once per new account | Counts physical meter registers |
| `_process_account()` | Per account/register | Decides backfill vs incremental |
| `_backfill_statistics()` | First poll for an account | Loads historical data |
| `_update_statistics()` | Subsequent polls | Adds new + gap-fills |
| `_process_and_insert_statistics()` | Both paths above | Fetches, aggregates, writes |
| `async_recalculate_historic_costs()` | From options flow | Re-prices historical data |

### `rates.py` — Rate schedule definitions

Defines Dominion's tiered SC rate schedules. When Dominion updates their
rates, add a new `RateSchedule` constant here and register it in
`TIERED_RATE_REGISTRY`.

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

Dominion's Rate 8 and Rate 6 are two-tier rates:
- **First 800 kWh** per billing cycle: charged at `rate_under`.
- **Over 800 kWh**: charged at `rate_over`.

The integration tracks cumulative Wh consumed within each billing cycle.
When the cumulative counter crosses 800 kWh, the cost for that interval is
split: part at `rate_under`, part at `rate_over`. See `rates.calculate_tiered_cost()`.

The cumulative counter **resets to 0** at each billing-cycle boundary. The
boundaries are estimated by `billing._estimate_billing_cycles()`.

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

### 5.6 Zero-Consumption Filtering

The Dominion API sometimes returns all-zero intervals for a day when that
day's data hasn't been processed yet (e.g. very recent days or holidays).
Inserting these zeros would create false "no usage" records in the recorder.

The coordinator calculates a per-day total and filters out entire days where
the total is zero. This means a day that genuinely had zero usage (extremely
rare for a household) would also be filtered. This is an accepted trade-off
because it is far more common for zeros to indicate missing data.

---

## 6. How to Add a New Feature

### Add a new tiered rate schedule

1. In `rates.py`: add a new `RateSchedule` constant (copy `SC_RATE_8` as a
   template, update all values).
2. In `const.py`: add a new `COST_MODE_*` constant string.
3. In `rates.py` (`TIERED_RATE_REGISTRY`): add `COST_MODE_*: YOUR_RATE`.
4. Run the tests — `build_cost_mode_choices()` and `_resolve_cost_config()`
   will pick up the new entry automatically.

No other files need to change.

### Add a new sensor

1. In `sensor.py`: add a new `DominionSCEntityDescription` to either
   `ACCOUNT_SENSORS` (for per-account data) or `BILLING_SENSORS` (for
   forecast data).
2. Add the translation key to `strings.json` and `translations/en.json`.
3. Add a test in `test_sensor.py`.

### Add a new config/options field

1. Add a `CONF_*` constant in `const.py`.
2. Add the form field in the relevant `config_flow.py` or `options_flow.py`
   step.
3. Read the value in the coordinator (typically via `config_entry.options.get()`).
4. Update `tests/test_config_flow.py` / `test_coordinator_*.py` accordingly.

---

## 7. Testing

### Running the tests

```bash
# Install dev dependencies (uv required — https://docs.astral.sh/uv/)
uv sync

# Run all tests with coverage
uv run pytest --cov=custom_components/dominionsc --cov-report=term-missing

# Run a specific test file
uv run pytest tests/test_coordinator_complete.py -v

# Run a specific test
uv run pytest tests/test_rates.py::test_calculate_tiered_cost_straddles -v
```

### Test structure

Tests are in `tests/`. The main fixtures are in `conftest.py`:
- `hass` — a real (but in-memory) HA instance.
- Mocked `DominionSC` API client via `unittest.mock.AsyncMock`.
- Mocked recorder functions (`get_last_statistics`, `statistics_during_period`,
  `async_add_external_statistics`).

The pure modules (`billing.py`, `rates.py`, `cost.py`, `aggregation.py`,
`statistics_ids.py`) are tested with synthetic inputs — no HA mocking needed.

### Coverage target

100% line and branch coverage is enforced in CI. If you add new code, add
tests for every branch.

### Test files by area

| File | What it covers |
|---|---|
| `test_config_flow.py` | All config flow steps, TFA, reauth |
| `test_coordinator_complete.py` | Comprehensive coordinator scenarios |
| `test_phase5_register_aware.py` | Register discovery, multi-register routing |
| `test_sensor.py` | Sensor entity value extraction |
| `test_rates.py` | Tiered rate math, season detection |
| `test_const.py` | `clean_service_addr` output |
| `test_init.py` | `async_setup_entry`, `async_unload_entry` |

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

From the HA Developer Tools → Template tab:

```jinja
{% set stats = states | selectattr('entity_id', 'match', 'sensor.dominionsc.*') | list %}
{{ stats | map(attribute='entity_id') | list }}
```

Or query the SQLite database directly:
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
| Costs are zero for historical data | Rate schedule effective date is after the backfill start date |
| Duplicate statistics / incorrect sums | Backfill ran twice; check `_backfill_initiated` state |
