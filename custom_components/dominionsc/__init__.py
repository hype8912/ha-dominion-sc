"""
The dominionsc Home Assistant integration.

This module is the **entry point** called by Home Assistant's config-entry
machinery. It is responsible for three things:

1. **Setup** (``async_setup_entry``): creates the
   :class:`~.coordinator.DominionSCCoordinator`, triggers the first data
   fetch, stores the coordinator in ``hass.data`` for cross-module access, and
   registers the SENSOR platform so billing / account entities appear in the UI.

2. **Teardown** (``async_unload_entry``): removes all sensor entities cleanly
   when the user removes or reloads the integration.

3. **Options updates** (``update_listener``): called by Home Assistant whenever
   the user changes integration options (e.g. switching cost modes). It asks
   the coordinator to re-fetch so sensors and statistics reflect the new
   settings immediately.

Integration lifecycle (from Home Assistant's perspective)
---------------------------------------------------------
1. User completes the config flow → config entry is persisted to
   ``.storage/core.config_entries``.
2. HA calls ``async_setup_entry`` on every restart (and immediately after the
   flow completes).
3. The coordinator polls every 12 hours. On each poll it:
   a. Re-authenticates with the Dominion API.
   b. Fetches account list, forecast, and usage intervals.
   c. Inserts or updates hourly long-term statistics in the HA recorder.
4. When options change, ``update_listener`` forces an immediate refresh.
5. On removal / reload, ``async_unload_entry`` tears down sensor entities.

Related modules
---------------
- :mod:`.coordinator` — data fetching and statistics insertion logic.
- :mod:`.sensor`      — HA sensor entity definitions.
- :mod:`.config_flow` — guided setup wizard (credentials, TFA, cost mode).
- :mod:`.options_flow`— post-install settings (cost mode, recalculation).
"""

from __future__ import annotations

from homeassistant.components import persistent_notification
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import (
    CONF_LAST_RATE_SCHEMA_VERSION,
    CURRENT_RATE_SCHEMA_VERSION,
    DOMAIN,
)
from .coordinator import DominionSCConfigEntry, DominionSCCoordinator

# The only platform this integration exposes is SENSOR.
# Long-term statistics (energy consumption and cost) are inserted directly
# into the recorder and appear in the Energy Dashboard — they are NOT sensors.
PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: DominionSCConfigEntry) -> bool:
    """
    Set up DominionSC from a config entry.

    Called by Home Assistant after the config flow completes, and again on
    every restart while the entry exists. Raises
    :class:`~homeassistant.exceptions.ConfigEntryNotReady` (via
    ``async_config_entry_first_refresh``) if the first API call fails so that
    HA will retry setup later.

    Args:
        hass:  The Home Assistant instance.
        entry: The config entry created by the config flow. Its ``data`` dict
               holds credentials; its ``options`` dict holds cost-mode settings.

    Returns:
        ``True`` on success. HA will mark the entry as loaded.

    """
    coordinator = DominionSCCoordinator(hass, entry)

    # Performs the first data fetch. If it raises, HA surfaces an error to the
    # user and schedules a retry — the integration is NOT silently skipped.
    await coordinator.async_config_entry_first_refresh()

    # Attach the coordinator to the entry so sensor.py can reach it via
    # ``entry.runtime_data`` (the modern HA pattern since 2024.x).
    entry.runtime_data = coordinator

    # Also store it in hass.data so the options flow can look it up by
    # entry_id (the options flow has the entry but not runtime_data).
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    # Register the update_listener and ensure it is cleaned up when the entry
    # is unloaded (``async_on_unload`` schedules the deregistration call).
    # If the rate schema version stored in this entry is behind the current
    # version, the user's historical cost statistics were calculated with old
    # (now-superseded) tariff rates. Notify them to trigger a recalculation.
    last_schema_version = entry.data.get(CONF_LAST_RATE_SCHEMA_VERSION, 0)
    if last_schema_version < CURRENT_RATE_SCHEMA_VERSION:
        persistent_notification.async_create(
            hass,
            (
                "Dominion Energy SC rate values changed on 2026-07-01. "
                "Cost statistics from 2025-07-23 to 2026-06-30 were calculated "
                "at old tariff rates. "
                "Go to **Settings → Devices & Services → Dominion Energy SC → Configure** "
                "and select *Recalculate History* to update historical cost data."
            ),
            title="Dominion Energy SC: Rate Update",
            notification_id="dominionsc_rate_schema_update",
        )
        hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, CONF_LAST_RATE_SCHEMA_VERSION: CURRENT_RATE_SCHEMA_VERSION},
        )

    entry.async_on_unload(entry.add_update_listener(update_listener))

    # Register sensor entities. This calls sensor.async_setup_entry().
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: DominionSCConfigEntry) -> bool:
    """
    Unload a config entry.

    Called when the user removes the integration or when HA needs to reload it
    (e.g. after an options change). Removes all sensor entities belonging to
    this entry from the HA entity registry.

    Args:
        hass:  The Home Assistant instance.
        entry: The config entry being unloaded.

    Returns:
        ``True`` if all platforms unloaded successfully, ``False`` otherwise.

    """
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def update_listener(hass: HomeAssistant, entry: DominionSCConfigEntry) -> None:
    """
    Handle options update.

    Home Assistant calls this whenever the user saves changes in the options
    flow (e.g. switching from Rate 8 to a fixed rate). Triggering a coordinator
    refresh causes ``_async_update_data`` to run immediately, which picks up
    the new options (cost mode, rate schedule, etc.) and re-inserts any
    affected statistics.

    Args:
        hass:  The Home Assistant instance.
        entry: The config entry whose options were just updated.

    Note:
        ``async_request_refresh()`` is used instead of calling
        ``_async_update_data()`` directly because the former goes through the
        coordinator's debounce and listener-notification machinery, ensuring
        all registered entity listeners receive the updated data.

    """
    coordinator = hass.data[DOMAIN][entry.entry_id]
    await coordinator.async_request_refresh()
