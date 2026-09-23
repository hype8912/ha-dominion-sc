# Home Assistant Integration for Dominion Energy SC

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![GitHub Release](https://img.shields.io/github/release/sctigercat1/ha-dominion-sc.svg)](https://github.com/sctigercat1/ha-dominion-sc/releases)
[![License](https://img.shields.io/github/license/sctigercat1/ha-dominion-sc.svg)](LICENSE)

A Home Assistant custom integration for Dominion Energy South Carolina customers to monitor their energy usage and billing information.

## Features

- **Hourly Interval Energy Usage Data**: Track your energy consumption with hourly granularity
- **Billing Information**: Monitor current billing cycle costs and forecasts
- **Energy Dashboard Compatibility**: Seamlessly integrate with Home Assistant's Energy Dashboard
- **Automatic Updates**: Data refreshes every 6 hours
- **Cost Estimation Options**:
  - South Carolina Rate Schedule 8 (Residential Service — tiered)
  - South Carolina Rate Schedule 6 (Energy Saver/Conservation — tiered)
  - South Carolina Rate Schedule 5 (Time of Use)
  - South Carolina Rate Schedule 7 (Time-of-Use Demand — energy portion only)
  - South Carolina Rate Schedule 2 (Low Use Residential Service — flat rate)
  - South Carolina Rate Schedule 1 (Good Cents Residential Service — tiered; closed to new customers)
  - South Carolina Rate Schedule 32S and 32V (Gas Standard / Gas Value)
  - Fixed rate pricing (custom $/kWh)
- **Multiple Energy Sources**: Support for both electric and gas services at the same service address

---

## Prerequisites

Before installing, verify the following:

### 1. Home Assistant version

This integration requires **Home Assistant 2025.6.3 or newer**. Check your version at **Settings** → **About**.

### 2. Two-factor authentication (TFA)

TFA is **supported but not required**. If your Dominion account has TFA enabled, the integration will prompt you to complete it during setup. If your account does not have TFA, setup proceeds directly after entering your credentials.

If you do have TFA enabled, **SMS (text message) verification is recommended** because it is the most reliable method with this integration.

### 3. Know your rate schedule

During setup you will be asked which rate schedule your account uses. To find yours:
- Check your most recent Dominion Energy bill — the rate schedule is printed in the rate/tariff section
- Log in to [dominionenergy.com](https://www.dominionenergy.com) → **My Account** → **My Bill** → look for "Rate Schedule"
- Call Dominion Energy SC customer service if you are unsure

If you don't know your rate, selecting **Rate 8** (Residential Service) is the most common choice for standard residential customers.

---

## Installation

### HACS (Recommended)

[HACS](https://hacs.xyz/) must be installed in your Home Assistant instance before proceeding.

1. Open HACS in your Home Assistant instance
2. Click the three dots (⋮) in the top-right corner
3. Select **Custom repositories**
4. In the **Repository** field enter: `https://github.com/sctigercat1/ha-dominion-sc`
5. Set **Category** to **Integration**
6. Click **Add**
7. Search for "Dominion Energy SC" in HACS and click **Download**
8. Restart Home Assistant

### Manual Installation

1. Download the latest release from the [releases page](https://github.com/sctigercat1/ha-dominion-sc/releases)
2. Extract the archive and copy the `custom_components/dominionsc` folder into your Home Assistant `config/custom_components/` directory:
   ```
   config/
   └── custom_components/
       └── dominionsc/       ← place folder here
           ├── __init__.py
           ├── manifest.json
           └── ...
   ```
3. Restart Home Assistant

---

## Configuration

### Initial Setup

1. Go to **Settings** → **Devices & Services**
2. Click **Add Integration**
3. Search for **Dominion Energy SC** and select it
4. Enter your Dominion Energy SC online account credentials:
   - **Username**: The email address or username you use to log in at dominionenergy.com
   - **Password**: Your dominionenergy.com account password
   - **Bidgely pilot ID (advanced)**: Leave at the default unless Dominion support tells you your account routes to a different Bidgely data pipeline
5. Complete two-factor authentication when prompted:
   - If your account has multiple TFA methods, you will be asked to choose one
   - Enter the code sent to you
6. Choose your **historical data backfill** preferences:
   - **Backfill extra consumption data** (default: off): Loads up to 365 days of historical electric and gas consumption. Off by default — only the current billing cycle is loaded.
   - **Backfill extra cost data** (default: off): Calculates estimated electric cost for the extended range. Requires consumption backfill to also be enabled. Cost accuracy decreases for older data because billing-cycle boundaries must be estimated.
   - > **Note**: You cannot enable extended backfill later without removing and re-adding the integration.
7. Choose your **electric cost tracking** preference:
   | Option | Description |
   |--------|-------------|
   | None | No cost calculation |
   | Rate 8 | Residential Service (tiered, most common) |
   | Rate 6 | Energy Saver/Conservation (tiered) |
   | Rate 5 | Time of Use (TOU) |
   | Rate 7 | Time-of-Use Demand (TOU energy portion only; demand charge not tracked) |
   | Rate 2 | Low Use Residential Service (flat rate; requires ≤ 400 kWh/month — verify eligibility with Dominion) |
   | Rate 1 | Good Cents Residential Service (tiered; closed to new customers — existing certified dwellings only) |
   | Fixed Rate | Enter a custom $/kWh rate |
8. If a **gas account** is detected on your service address, you will also be asked to choose a gas cost tracking preference:
   | Option | Description |
   |--------|-------------|
   | None | No gas cost calculation |
   | Rate 32S | Gas Standard Service |
   | Rate 32V | Gas Value Service |
9. Click **Submit**

### What to expect after setup

- **First data may take up to 24–48 hours to appear.** Dominion Energy SC reports interval data with a delay of 1–2 days. The integration will poll within minutes, but if Dominion has not yet published recent data, the Energy Dashboard will show no consumption until the next poll cycle (every 6 hours) when data becomes available.
- **The integration creates a device** named after your service address under **Settings** → **Devices & Services** → **Dominion Energy SC**.
- If you enabled extended backfill, the initial load of 365 days of data runs in the background and may take a few minutes to complete.

### Changing Cost Settings After Setup

Go to **Settings** → **Devices & Services** → **Dominion Energy SC** → **Configure**. When switching between rate schedules (electric) or enabling a gas rate plan, you will be offered the option to recalculate historical cost statistics over a date range you choose using the new rate — electric and gas are recalculated independently, so this works whether you're changing one, the other, or both at once.

The same screen also lets you change the **Bidgely pilot ID (advanced)**. Changing it reloads the integration.

The date-range picker defaults its start date to the earliest consumption data available for whatever you're recalculating, so accepting the default reprices your full history. If you only want to reprice part of your history, adjust the start date.

### Configuration Parameters Reference

| Parameter | Description | Required | Default |
|-----------|-------------|----------|---------|
| Username | dominionenergy.com login email/username | Yes | — |
| Password | dominionenergy.com account password | Yes | — |
| Bidgely Pilot ID | Advanced: Bidgely data pipeline identifier; change only if Dominion support tells you to | No | Library default |
| Extended Backfill | Load up to 365 days of historical consumption | No | Off |
| Extended Cost Backfill | Calculate cost for the extended range | No | Off |
| Cost Mode | Electric cost calculation rate schedule | No | Rate 8 |
| Fixed Rate | Custom rate in $/kWh (only when Cost Mode = Fixed) | No | 0.14164 |
| Gas Cost Mode | Gas cost calculation rate schedule | No | None |

---

## Sensors

All sensors are diagnostic entities and appear under the device created for your service address.

### Per-account sensors (one per energy source: Electric, Gas)

| Sensor | Description |
|--------|-------------|
| **{Account} Latest Data** | Timestamp of the most recent interval inserted into statistics (e.g. "Electric Latest Data"). Useful for checking data freshness. |

### Billing sensors (one set per service address)

These sensors are only created when Dominion's billing forecast is available for your account.

| Sensor | Description | Unit |
|--------|-------------|------|
| **Current bill cost to date** | Dominion's reported spend in the current billing cycle | USD |
| **Current bill forecasted cost** | Projected end-of-cycle spend | USD |
| **Typical monthly cost** | Historical average for comparison | USD |
| **Current bill start date** | Billing cycle start (hidden by default) | date |
| **Current bill end date** | Billing cycle end (hidden by default) | date |
| **Last Polling** | UTC timestamp of the most recent coordinator poll | timestamp |

### Gas cost sensor

| Sensor | Description | Unit |
|--------|-------------|------|
| **Gas accumulated cost** | Running total of gas cost from long-term statistics (only created when Rate 32S or 32V is selected) | USD |

---

## Energy Dashboard Setup

The integration writes long-term statistics directly to the HA recorder. These appear in the **Energy Dashboard** under **Settings** → **Dashboards** → **Energy**.

### Statistic IDs

Statistics are named using your service address. The format is:

```
dominionsc:<service_address>_electric_energy_consumption
dominionsc:<service_address>_electric_energy_cost
dominionsc:<service_address>_gas_energy_consumption
dominionsc:<service_address>_gas_energy_cost
```

where `<service_address>` is your service address/account number with spaces and special characters replaced by underscores and lowercased. For example, service address `"12345 Main St"` becomes `12345_main_st`.

To find the exact ID in use: go to **Developer Tools** → **Statistics** and search for `dominionsc`.

### Adding statistics to the Energy Dashboard

1. Go to **Settings** → **Dashboards** → **Energy**
2. Under **Electricity grid**, click **Add consumption**
3. Search for `dominionsc` or your service address
4. Select the `..._electric_energy_consumption` statistic
5. For cost tracking, select **Use an entity tracking the total costs** and choose `..._electric_energy_cost`
6. For gas (if applicable), scroll to **Gas consumption**, click **Add gas source**, and select `..._gas_energy_consumption`
7. For gas cost tracking, select **Use an entity tracking the total costs** and choose `..._gas_energy_cost`

| Statistic | Description | Unit |
|-----------|-------------|------|
| `dominionsc:<addr>_electric_energy_consumption` | Hourly electric consumption | Wh |
| `dominionsc:<addr>_electric_energy_cost` | Hourly electric cost (when a rate is selected) | USD |
| `dominionsc:<addr>_gas_energy_consumption` | Hourly gas consumption | ft³ |
| `dominionsc:<addr>_gas_energy_cost` | Hourly gas cost (when Rate 32S or 32V is selected) | USD |

---

## Example Automations

### Alert when forecasted bill exceeds a threshold

```yaml
automation:
  - alias: "High Energy Cost Alert"
    trigger:
      - platform: numeric_state
        entity_id: sensor.dominion_energy_sc_current_bill_forecasted_cost
        above: 150
    action:
      - service: notify.mobile_app
        data:
          message: "Your forecasted energy bill is ${{ states('sensor.dominion_energy_sc_current_bill_forecasted_cost') }}"
```

---

## Troubleshooting

### No data in the Energy Dashboard after setup

This is normal on first install. Dominion reports data with a 24–48 hour delay.

1. Wait at least 24 hours after setup, then manually trigger a refresh: **Settings** → **Devices & Services** → **Dominion Energy SC** → click the three dots → **Reload**
2. Check that the **{Account} Latest Data** sensor has a timestamp. If it reads "unavailable", Dominion has not yet published data for the current period.
3. Check Home Assistant logs (**Settings** → **System** → **Logs**) and search for `dominionsc` — the coordinator logs how many statistics rows it inserted on each poll.

### Authentication errors

1. Verify your credentials at [dominionenergy.com](https://www.dominionenergy.com)
2. Confirm TFA is enabled and working on your Dominion account (see [Prerequisites](#prerequisites))
3. If you recently changed your password, remove and re-add the integration
4. If you see a re-authentication prompt in HA, click it and re-enter your credentials — TFA session tokens expire periodically

### "Integration already configured" error

Each Dominion Energy SC account can only be added once. If you need to reconfigure, go to **Settings** → **Devices & Services** → **Dominion Energy SC** → click the three dots → **Delete**, then re-add.

### Cost statistics show $0 for historical data

Each rate schedule only has pricing from the date its rates became effective. Rate 6 and Rate 8 also include the prior tariff that was in effect from 2025-07-23 through 2026-06-30. All other schedules start on 2026-07-01. Cost shows $0.00 for any interval before the earliest known rates for your schedule (for example, before 2025-07-23 for Rate 8, or before 2026-07-01 for Rate 5). This is expected, and it only affects extended backfill or recalculated history.

### Energy Dashboard shows gaps or missing history

If you see a gap that starts on a specific date, your account's data was not available from the Dominion API for that period. The integration will automatically fill in late-arriving data on the next poll cycle (every 6 hours).

---

## Known Limitations

- **Data Delay**: Energy usage data is reported by Dominion Energy SC with a 24–48 hour delay. Real-time monitoring is not available.
- **Single Service Address**: Only one service address per Dominion Energy SC account is supported. Multiple service addresses may be added in a future release.
- **Solar / Net Metering**: If your electric account has two meter registers (grid delivery and solar export), each register is recorded as its own statistic with the last six digits of the meter ID in its name. Dominion does not reliably report which register is import and which is export, so you must identify them yourself when adding them to the Energy Dashboard. Registers are detected only when the account is first set up.
- **Cost Estimates**: Calculations are estimates based on the selected rate schedule. Actual bills may differ, particularly for historical data where billing-cycle boundaries must be estimated.
- **Fixed Charges Not Included**: Daily and monthly fixed charges (Basic Facilities Charge, DER Program charge) are not included in cost statistics. Only energy usage charges are calculated.
- **Rate 7 Demand Charge**: For Rate 7 (Time-of-Use Demand), only the time-of-use energy portion is tracked. The monthly on-peak billing demand charge cannot be determined from interval data.
- **TFA**: If your Dominion account has two-factor authentication enabled, you will be prompted to complete it during setup and periodically when the session token expires.

### Supported Rate Schedules

| Rate | Description | Type | Pricing Effective From |
|------|-------------|------|----------------|
| Rate 1 | Good Cents Residential Service | Tiered electric (closed to new customers) | 2026-07-01 |
| Rate 2 | Low Use Residential Service | Flat electric | 2026-07-01 |
| Rate 5 | Time of Use | TOU electric | 2026-07-01 |
| Rate 6 | Energy Saver/Conservation | Tiered electric | 2026-07-01 (prior rates from 2025-07-23) |
| Rate 7 | Time-of-Use Demand | TOU electric (energy only; demand charge not tracked) | 2026-07-01 |
| Rate 8 | Residential Service | Tiered electric | 2026-07-01 (prior rates from 2025-07-23) |
| Rate 32S | Gas Standard Service | Flat gas | 2026-07-01 |
| Rate 32V | Gas Value Service | Flat gas | 2026-07-01 |
| Fixed Rate | Custom $/kWh | Flat electric | n/a |

---

## Removal

Go to **Settings** → **Devices & Services** → **Dominion Energy SC** → click the three dots → **Delete**.

Removing the integration does not delete the long-term statistics already stored in the HA recorder. Your Energy Dashboard history is preserved. If you re-add the integration, it will resume writing to the same statistic IDs.

---

## Support

- [Open an issue](https://github.com/sctigercat1/ha-dominion-sc/issues)
- [dominion-sc-power library](https://github.com/sctigercat1/dominion-sc-power)

## Contributing

Contributions are welcome! Please submit a pull request with your proposed changes.

### Development Environment Setup

```bash
git clone https://github.com/sctigercat1/ha-dominion-sc.git
cd ha-dominion-sc

# Install uv if not already installed
curl -LsSf https://astral.sh/uv/install.sh | sh

uv sync
uv run pytest
uv run ruff check custom_components/ tests/
```

See [docs/DEVELOPER.md](docs/DEVELOPER.md) for full developer documentation.

## Credits

This project was inspired by [Opower](https://www.home-assistant.io/integrations/opower/), [ha-dominion-energy](https://github.com/YeomansIII/ha-dominion-energy), [ha-unraid](https://github.com/ruaan-deysel/ha-unraid), and [integration_blueprint](https://github.com/ludeeus/integration_blueprint). Much appreciated!

Special thanks to the Home Assistant and HACS communities.

## License

This project is licensed under the Apache License 2.0 — see the [LICENSE](LICENSE) file for details.

## Disclaimer

This is an unofficial integration and is not affiliated with, endorsed by, or connected to Dominion Energy SC. Use at your own risk. The authors are not responsible for any issues that may arise from using this integration.

## Privacy

This integration communicates directly with Dominion Energy SC's servers using your credentials. No data is sent to third parties. Your credentials are stored securely in Home Assistant's configuration and are only used to authenticate with Dominion Energy SC.
