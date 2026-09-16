"""In-memory stand-in for HA's recorder long-term statistics store.

The existing coordinator tests mock ``get_last_statistics`` /
``statistics_during_period`` with a hand-built ``side_effect`` list matching
one specific call sequence -- accurate, but tedious to extend to a scenario
spanning several poll cycles (a gap-fill catching up over multiple polls, a
billing-cycle rollover, a recalculation running against real accumulated
data).

:class:`FakeStatisticsStore` instead behaves like the real recorder: calling
the coordinator's ``_async_update_data()`` (or ``_process_account`` /
``async_recalculate_historic_costs``) repeatedly against the same store lets
a test simulate several real polls end-to-end and assert on the final
accumulated rows, exactly as they'd appear in Developer Tools -> Statistics.
"""

from collections.abc import Iterable
from contextlib import contextmanager
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch


class FakeStatisticsStore:
    """A minimal in-memory recorder: dedupes by hour, keeps rows sorted."""

    def __init__(self) -> None:
        self.rows: dict[str, list[dict]] = {}

    def add_external_statistics(self, hass: object, metadata: dict, statistics: Iterable[dict]) -> None:
        """Stand-in for ``async_add_external_statistics``."""
        stat_id = metadata["statistic_id"]
        rows = self.rows.setdefault(stat_id, [])
        existing_starts = {r["start"] for r in rows}
        for row in statistics:
            if row["start"] not in existing_starts:
                rows.append({"start": row["start"], "state": row["state"], "sum": row["sum"]})
                existing_starts.add(row["start"])
        rows.sort(key=lambda r: r["start"])

    def get_last_statistics(
        self,
        hass: object,
        number_of_stats: int,
        statistic_id: str,
        convert_units: bool,
        types: set[str],
    ) -> dict[str, list[dict]]:
        """Stand-in for ``homeassistant.components.recorder.statistics.get_last_statistics``.

        Returns ``start`` as a POSIX timestamp float, matching the real
        recorder's actual return shape (confirmed against production debug
        logs: ``start=1789354800.0 (type=float)``) rather than a datetime --
        callers that assume a float unconditionally (as
        ``_async_recalculate_historic_costs_locked`` does) need this to be
        faithful, not just the more defensive call sites that tolerate both.
        """
        rows = self.rows.get(statistic_id)
        if not rows:
            return {}
        last = rows[-1]
        return {
            statistic_id: [
                {"start": last["start"].timestamp(), **{t: last[t] for t in types if t in last}}
            ]
        }

    def statistics_during_period(
        self,
        hass: object,
        start_time: datetime,
        end_time: datetime,
        statistic_ids: set[str],
        period: str,
        units: object,
        types: set[str],
    ) -> dict[str, list[dict]]:
        """Stand-in for ``homeassistant.components.recorder.statistics.statistics_during_period``.

        Filtering happens in datetime space against the stored rows, but the
        returned ``start`` is a POSIX timestamp float -- see
        :meth:`get_last_statistics` for why that matters.
        """
        result: dict[str, list[dict]] = {}
        for stat_id in statistic_ids:
            rows = self.rows.get(stat_id, [])
            result[stat_id] = [
                {"start": r["start"].timestamp(), **{t: r[t] for t in types if t in r}}
                for r in rows
                if start_time <= r["start"] < end_time
            ]
        return result

    def row_count(self, statistic_id: str) -> int:
        """Convenience: number of hourly rows recorded so far for a statistic."""
        return len(self.rows.get(statistic_id, []))

    def last_sum(self, statistic_id: str) -> float | None:
        """Convenience: the running cumulative sum of the last recorded row."""
        rows = self.rows.get(statistic_id)
        return rows[-1]["sum"] if rows else None


@contextmanager
def patched_recorder(store: FakeStatisticsStore):
    """Patch the coordinator module's recorder calls to use ``store``.

    Routes ``get_instance(hass).async_add_executor_job(fn, *args)`` straight
    through to ``fn(*args)`` so the patched ``get_last_statistics`` /
    ``statistics_during_period`` names run synchronously against the fake
    store, exactly as the real executor-job call would run them against a
    real database.
    """
    executor = MagicMock()
    executor.async_add_executor_job = AsyncMock(side_effect=lambda fn, *args: fn(*args))
    with (
        patch("custom_components.dominionsc.coordinator.get_instance", return_value=executor),
        patch(
            "custom_components.dominionsc.coordinator.get_last_statistics",
            new=store.get_last_statistics,
        ),
        patch(
            "custom_components.dominionsc.coordinator.statistics_during_period",
            new=store.statistics_during_period,
        ),
        patch(
            "custom_components.dominionsc.coordinator.async_add_external_statistics",
            new=store.add_external_statistics,
        ),
    ):
        yield store
