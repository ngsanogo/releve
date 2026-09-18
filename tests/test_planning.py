"""Planning invariants, checked over many generated inputs."""

from __future__ import annotations

from datetime import date, timedelta
from itertools import pairwise

from hypothesis import given
from hypothesis import strategies as st

from releve.domain import Dataset
from releve.planning import (
    SETTLE_DAYS,
    history_start,
    missing_days,
    plan_windows,
    settled_gaps,
)

TODAY = date(2026, 9, 12)

day_offsets = st.sets(st.integers(min_value=1, max_value=800), max_size=120)


def _newest_first(offsets: set[int]) -> list[date]:
    return sorted((TODAY - timedelta(days=n) for n in offsets), reverse=True)


@given(offsets=day_offsets, window=st.integers(min_value=1, max_value=365))
def test_windows_cover_every_missing_day_exactly_once(offsets: set[int], window: int) -> None:
    missing = _newest_first(offsets)
    windows = plan_windows(missing, window)

    covered = [day for day in missing for start, end in windows if start <= day < end]
    assert covered == missing
    for start, end in windows:
        assert 1 <= (end - start).days <= window
        assert start in missing
        assert end - timedelta(days=1) in missing


@given(offsets=day_offsets, window=st.integers(min_value=1, max_value=365))
def test_windows_are_newest_first_and_as_few_as_possible(offsets: set[int], window: int) -> None:
    windows = plan_windows(_newest_first(offsets), window)
    for (newer_start, newer_end), (_, older_end) in pairwise(windows):
        assert older_end <= newer_start
        # the older window's newest day could not have fitted in the newer window
        assert older_end - timedelta(days=1) < newer_end - timedelta(days=window)


@given(
    offsets=day_offsets,
    refresh_offsets=st.sets(st.integers(min_value=1, max_value=800), max_size=12),
    window=st.integers(min_value=1, max_value=365),
)
def test_refresh_days_never_add_a_window_nor_move_a_missing_day(
    offsets: set[int], refresh_offsets: set[int], window: int
) -> None:
    missing = _newest_first(offsets)
    refresh = {TODAY - timedelta(days=n) for n in refresh_offsets}

    plain = plan_windows(missing, window)
    stretched = plan_windows(missing, window, refresh=refresh)

    assert len(stretched) == len(plain)
    for (start, end), (plain_start, plain_end) in zip(stretched, plain, strict=True):
        assert start <= plain_start < plain_end <= end
        assert (end - start).days <= window
        assert [d for d in missing if start <= d < end] == [
            d for d in missing if plain_start <= d < plain_end
        ]
    for (newer_start, _), (_, older_end) in pairwise(stretched):
        assert older_end <= newer_start
    for day in refresh:
        if not any(start <= day < end for start, end in stretched):
            # left out only when no window could take it in
            assert all(
                (max(end, day + timedelta(days=1)) - min(start, day)).days > window
                for start, end in stretched
            )


def test_refresh_days_ride_along_with_yesterday_and_never_alone() -> None:
    yesterday = TODAY - timedelta(days=1)
    partial = {TODAY - timedelta(days=3), TODAY - timedelta(days=6)}
    assert plan_windows([yesterday], 7, refresh=partial) == [(TODAY - timedelta(days=6), TODAY)]
    assert plan_windows([yesterday], 5, refresh=partial) == [(TODAY - timedelta(days=3), TODAY)]
    assert plan_windows([], 7, refresh=partial) == []


@given(
    span=st.integers(min_value=0, max_value=400),
    known=st.sets(st.integers(min_value=0, max_value=400), max_size=50),
)
def test_missing_days_are_the_unknown_days_newest_first(span: int, known: set[int]) -> None:
    start = TODAY - timedelta(days=span)
    known_days = {start + timedelta(days=n) for n in known}

    missing = missing_days(start, TODAY, known_days)

    expected = {start + timedelta(days=n) for n in range(span)} - known_days
    assert set(missing) == expected
    assert missing == sorted(missing, reverse=True)


def test_history_is_capped_by_what_enedis_keeps() -> None:
    assert history_start(TODAY, Dataset.DAILY_CONSUMPTION, 365) == TODAY - timedelta(days=365)
    assert history_start(TODAY, Dataset.CURVE_CONSUMPTION, 1094) == TODAY - timedelta(days=729)


def test_only_old_empty_answers_are_final() -> None:
    old = TODAY - timedelta(days=SETTLE_DAYS)
    recent = TODAY - timedelta(days=SETTLE_DAYS - 1)
    delivered = TODAY - timedelta(days=SETTLE_DAYS + 1)
    assert settled_gaps([old, recent, delivered], {delivered}, TODAY) == {old}
