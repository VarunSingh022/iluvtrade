"""The declared bar interval against observed bar times: per instrument, calendar-aware, exact.

These pin :func:`iluvtrade.data.temporal.check_spacing` directly, case by case:
which spacings refuse a declaration, which are accepted, and on what evidence.
The rule they encode is narrow on purpose. Timestamps alone never show how long
a bar is -- the declaration says that. The spacing can only *refuse* a
declaration it contradicts (bars closer than one interval, gaps that are not
whole intervals), and each instrument must answer for itself: one instrument's
bars never vouch for another's. An instrument whose own bars are never one
interval apart is ambiguous and is accepted only on the user's explicit
confirmation that its gaps are missing observations.

Expectations are built here from the calendar and the zone, never copied from
a previous run.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from iluvtrade.data.schema import TimestampFormat
from iluvtrade.data.temporal import (
    TemporalDeclaration,
    TemporalError,
    TimestampConvention,
    TimestampKind,
    check_spacing,
    label_instant,
)

NEW_YORK = ZoneInfo("America/New_York")
IST = ZoneInfo("Asia/Kolkata")
UTC = dt.UTC

START = TimestampConvention.INTERVAL_START
END = TimestampConvention.INTERVAL_END
SESSION = TimestampConvention.SESSION_DATE


def _declare(
    interval: str,
    *,
    convention: TimestampConvention = END,
    zone: str | None = None,
    confirmed: tuple[str, ...] = (),
) -> TemporalDeclaration:
    return TemporalDeclaration(convention, zone, interval, frozenset(confirmed))


def _every(start: dt.datetime, step: dt.timedelta, count: int) -> list[float]:
    return [(start + step * i).timestamp() for i in range(count)]


def _walls(zone: ZoneInfo, *texts: str) -> list[float]:
    """Wall-clock times in ``zone``, as the instants they name."""

    return [dt.datetime.fromisoformat(t).replace(tzinfo=zone).timestamp() for t in texts]


def _end_of_days(zone: ZoneInfo, *days: str) -> list[float]:
    """What a bare date is read as: the last instant of that civil date in ``zone``."""

    return [
        (
            dt.datetime.fromisoformat(d).replace(tzinfo=zone)
            + dt.timedelta(days=1)
            - dt.timedelta(microseconds=1)
        ).timestamp()
        for d in days
    ]


FIFTEEN = dt.timedelta(minutes=15)
OPEN = dt.datetime(2024, 1, 2, 9, 15, tzinfo=IST)


# --- one instrument's evidence never validates another's ----------------------


def test_one_instruments_exact_pairs_do_not_validate_another() -> None:
    """The reviewed blocker: AAA's 15-minute bars vouched for BBB's hourly ones.

    Every BBB gap is a whole number of 15-minute intervals, so a file-wide count
    of one-interval pairs (all of them AAA's) accepted BBB as 15-minute bars --
    and moved each start-stamped BBB bar 15 minutes forward when its hour had 45
    minutes left to run: a close stamped before it existed.
    """

    stamps = {
        "AAA": _every(OPEN, FIFTEEN, 12),
        "BBB": _every(OPEN, dt.timedelta(hours=1), 4),
    }
    with pytest.raises(TemporalError) as refused:
        check_spacing(stamps, _declare("15m", convention=START), TimestampKind.AWARE)
    message = str(refused.value)
    assert message.startswith("BBB:"), message
    assert "AAA" not in message
    assert "confirm missing observations for exactly these instruments" in message
    assert "15m is your declaration for them, not something their bars show" in message


def test_each_instrument_is_reported_separately() -> None:
    stamps = {"AAA": _every(OPEN, FIFTEEN, 12), "BBB": _every(OPEN, FIFTEEN, 5)}
    report = check_spacing(stamps, _declare("15m"), TimestampKind.AWARE).to_dict()
    assert report["instruments"] == {
        "AAA": {
            "bars": 12,
            "one_interval_pairs": 11,
            "longer_gaps": 0,
            "accepted_on_confirmation": False,
        },
        "BBB": {
            "bars": 5,
            "one_interval_pairs": 4,
            "longer_gaps": 0,
            "accepted_on_confirmation": False,
        },
    }
    assert report["accepted_on_confirmation"] == []
    assert report["arithmetic"] == "absolute"


def test_a_sparse_instrument_is_accepted_only_on_the_missing_observation_confirmation() -> None:
    sparse = [OPEN + FIFTEEN * i for i in (0, 4, 8, 9 * 4)]  # gaps of 4, 4 and 28 intervals
    stamps = {"AAA": _every(OPEN, FIFTEEN, 12), "BBB": [m.timestamp() for m in sparse]}
    with pytest.raises(TemporalError, match=r"^BBB:"):
        check_spacing(stamps, _declare("15m"), TimestampKind.AWARE)

    report = check_spacing(stamps, _declare("15m", confirmed=("BBB",)), TimestampKind.AWARE)
    assert report.instruments["BBB"].accepted_on_confirmation
    assert not report.instruments["AAA"].accepted_on_confirmation
    assert report.to_dict()["accepted_on_confirmation"] == ["BBB"]
    assert report.to_dict()["missing_observations_confirmed_for"] == ["BBB"]
    assert report.to_dict()["ambiguous"] == ["BBB"]


def test_a_single_bar_instrument_is_ambiguous() -> None:
    stamps = {"AAA": _every(OPEN, FIFTEEN, 12), "ONE": [OPEN.timestamp()]}
    with pytest.raises(TemporalError, match=r"^ONE:"):
        check_spacing(stamps, _declare("15m"), TimestampKind.AWARE)
    report = check_spacing(stamps, _declare("15m", confirmed=("ONE",)), TimestampKind.AWARE)
    assert report.instruments["ONE"].accepted_on_confirmation


def test_bars_closer_than_the_interval_are_refused_naming_the_instrument() -> None:
    stamps = {
        "AAA": _every(OPEN, FIFTEEN, 12),
        "BBB": _every(OPEN, dt.timedelta(minutes=5), 6),
    }
    for confirmed in ((), ("BBB",)):  # a confirmation never excuses overlapping bars
        with pytest.raises(TemporalError, match="Two BBB bars are 5m apart, less than"):
            check_spacing(stamps, _declare("15m", confirmed=confirmed), TimestampKind.AWARE)


def test_a_coarser_instrument_cannot_be_told_from_a_sparse_one() -> None:
    """30-minute bars every 30 minutes look exactly like 15-minute bars with every other missing.

    So the check does not claim to tell them apart: without the confirmation it
    refuses; with it, the user has said which they are, and is recorded saying so.
    """

    stamps = {
        "AAA": _every(OPEN, FIFTEEN, 12),
        "HALF": _every(OPEN, dt.timedelta(minutes=30), 6),
    }
    with pytest.raises(TemporalError, match=r"^HALF:"):
        check_spacing(stamps, _declare("15m"), TimestampKind.AWARE)
    report = check_spacing(stamps, _declare("15m", confirmed=("HALF",)), TimestampKind.AWARE)
    assert report.to_dict()["accepted_on_confirmation"] == ["HALF"]


def test_the_confirmation_names_exactly_the_ambiguous_instruments() -> None:
    """Never a blanket waiver: each name must be in the file, and must need it."""

    stamps = {
        "AAA": _every(OPEN, FIFTEEN, 12),
        "BBB": _every(OPEN, dt.timedelta(hours=1), 4),
        "CCC": _every(OPEN, dt.timedelta(minutes=30), 4),
    }
    # Confirming one ambiguous instrument does not cover another.
    with pytest.raises(TemporalError, match=r"^CCC: no two consecutive bars"):
        check_spacing(stamps, _declare("15m", confirmed=("BBB",)), TimestampKind.AWARE)
    # An instrument whose own bars show the interval needs no confirmation.
    with pytest.raises(TemporalError, match=r"confirmed for AAA, whose own bars already sit"):
        check_spacing(stamps, _declare("15m", confirmed=("AAA", "BBB", "CCC")), TimestampKind.AWARE)
    # A name with no bars in the file.
    with pytest.raises(TemporalError, match=r"confirmed for ZZZ, which this file has no bars"):
        check_spacing(stamps, _declare("15m", confirmed=("BBB", "CCC", "ZZZ")), TimestampKind.AWARE)
    report = check_spacing(stamps, _declare("15m", confirmed=("BBB", "CCC")), TimestampKind.AWARE)
    assert report.to_dict()["accepted_on_confirmation"] == ["BBB", "CCC"]


def test_the_preview_reports_ambiguity_instead_of_refusing_it() -> None:
    stamps = {"AAA": _every(OPEN, FIFTEEN, 12), "BBB": _every(OPEN, dt.timedelta(hours=1), 4)}
    report = check_spacing(stamps, _declare("15m"), TimestampKind.AWARE, enforce_confirmation=False)
    assert report.ambiguous == ("BBB",)
    assert not report.instruments["BBB"].accepted_on_confirmation
    # Every other refusal still applies in the preview.
    stamps["CCC"] = _every(OPEN, dt.timedelta(minutes=5), 4)
    with pytest.raises(TemporalError, match="Two CCC bars are 5m apart"):
        check_spacing(stamps, _declare("15m"), TimestampKind.AWARE, enforce_confirmation=False)


# --- a single instrument -----------------------------------------------------


def test_a_single_instrument_whose_gaps_are_all_multiples_is_ambiguous() -> None:
    stamps = {"TCS": _every(OPEN, FIFTEEN * 2, 8)}
    with pytest.raises(TemporalError, match=r"^TCS: no two consecutive bars are one 15m"):
        check_spacing(stamps, _declare("15m"), TimestampKind.AWARE)


def test_a_single_irregular_instrument_is_refused_even_when_confirmed() -> None:
    stamps = {"TCS": _every(OPEN, dt.timedelta(minutes=25), 8)}
    for confirmed in ((), ("TCS",)):
        with pytest.raises(TemporalError, match="not a whole number of 15m intervals"):
            check_spacing(stamps, _declare("15m", confirmed=confirmed), TimestampKind.AWARE)


def test_one_exact_pair_is_the_instruments_own_evidence() -> None:
    """One pair one interval apart, every other gap a whole number of intervals.

    Two of its bars one interval apart rule out bars longer than the interval for
    this instrument without the confirmation; the rest of its gaps are counted as
    longer gaps. (They do not prove the bars are not shorter -- nothing in
    timestamps does; that is what the declaration states.)
    """

    moments = [OPEN, OPEN + FIFTEEN] + [OPEN + FIFTEEN + dt.timedelta(hours=h) for h in range(1, 6)]
    report = check_spacing(
        {"TCS": [m.timestamp() for m in moments]}, _declare("15m"), TimestampKind.AWARE
    )
    assert report.instruments["TCS"].one_interval_pairs == 1
    assert report.instruments["TCS"].longer_gaps == 5
    assert not report.instruments["TCS"].accepted_on_confirmation


# --- no tolerance on absolute instants ----------------------------------------


def test_millisecond_jitter_is_refused_not_rounded() -> None:
    start = dt.datetime(2024, 1, 2, 3, 45, tzinfo=UTC)
    stamps = _every(start, FIFTEEN, 6)
    stamps[3] += 0.003
    with pytest.raises(TemporalError, match="not a whole number of 15m intervals"):
        check_spacing({"BTC": stamps}, _declare("15m"), TimestampKind.EPOCH_MILLIS)


def test_daily_utc_instants_get_no_daylight_saving_hour() -> None:
    """A 23- or 25-hour gap between UTC instants is not one day.

    A blanket hour of slack for daily bars used to accept both; there is no
    daylight saving in UTC to excuse them.
    """

    midnight = dt.datetime(2024, 3, 8, tzinfo=UTC)
    exact = _every(midnight, dt.timedelta(days=1), 6)
    assert check_spacing({"BTC": exact}, _declare("1d"), TimestampKind.EPOCH_SECONDS)
    for hours, reason in (
        (23, "1380m apart, less than the declared 1d"),
        (25, "not a whole number of 1d intervals"),
    ):
        shifted = list(exact)
        shifted[3:] = [t + (hours - 24) * 3600 for t in shifted[3:]]
        with pytest.raises(TemporalError, match=reason):
            check_spacing({"BTC": shifted}, _declare("1d"), TimestampKind.EPOCH_SECONDS)


def test_offset_aware_daily_bars_across_a_dst_change_are_refused_with_the_remedy() -> None:
    """16:00 New York written with its offset: -05:00 on Friday, -04:00 on Monday.

    As absolute instants those are 71 hours apart, not three days; the offset
    alone does not say a zone's daylight-saving rule moved it. The refusal says
    how to upload such a series so it is measured on its own wall clock.
    """

    stamps = [
        dt.datetime.fromisoformat(t).timestamp()
        for t in (
            "2024-03-07T16:00:00-05:00",
            "2024-03-08T16:00:00-05:00",
            "2024-03-11T16:00:00-04:00",
            "2024-03-12T16:00:00-04:00",
        )
    ]
    with pytest.raises(
        TemporalError, match="upload them as wall-clock times and declare their zone"
    ):
        check_spacing({"SPY": stamps}, _declare("1d"), TimestampKind.AWARE)


# --- calendar days on the declared zone's wall clock -------------------------


def test_session_dates_across_a_dst_change_are_whole_days() -> None:
    days = ("2024-03-07", "2024-03-08", "2024-03-11", "2024-03-12", "2024-11-01", "2024-11-04")
    report = check_spacing(
        {"SPY": _end_of_days(NEW_YORK, *days)},
        _declare("1d", convention=SESSION, zone="America/New_York"),
        TimestampKind.DATE,
    )
    assert report.arithmetic == "wall_clock"
    # 7->8 March and 11->12 March (across the change) are one day each; the
    # weekend, March to November, and the weekend after the autumn change are longer.
    assert report.instruments["SPY"].one_interval_pairs == 2
    assert report.instruments["SPY"].longer_gaps == 3


def test_wall_clock_daily_closes_across_a_dst_change_are_whole_days() -> None:
    stamps = _walls(NEW_YORK, "2024-03-08 16:00", "2024-03-11 16:00", "2024-03-12 16:00")
    report = check_spacing(
        {"SPY": stamps}, _declare("1d", zone="America/New_York"), TimestampKind.NAIVE
    )
    assert report.instruments["SPY"].one_interval_pairs == 1


def test_wall_clock_daily_bars_an_hour_off_are_refused() -> None:
    """On the wall clock there is no daylight-saving excuse for 15:00 one day and 16:00 the next."""

    stamps = _walls(NEW_YORK, "2024-03-07 16:00", "2024-03-08 16:00", "2024-03-11 15:00")
    with pytest.raises(TemporalError, match="not a whole number of 1d intervals"):
        check_spacing({"SPY": stamps}, _declare("1d", zone="America/New_York"), TimestampKind.NAIVE)


def test_weekly_bars_across_a_dst_change() -> None:
    fridays = ("2024-03-01", "2024-03-08", "2024-03-15", "2024-03-29")
    report = check_spacing(
        {"SPY": _end_of_days(NEW_YORK, *fridays)},
        _declare("1w", zone="America/New_York"),
        TimestampKind.DATE,
    )
    assert (
        report.instruments["SPY"].one_interval_pairs,
        report.instruments["SPY"].longer_gaps,
    ) == (2, 1)


def test_start_stamped_four_hour_bars_spanning_a_dst_change_are_refused() -> None:
    """A 00:00 New York bar on 10 March 2024 ends at 04:00 wall clock, three hours later.

    Adding the interval's fixed four hours would stamp its close at 05:00, an
    hour after it happened -- or, in autumn, an hour before. Neither is guessed.
    """

    stamps = _walls(
        NEW_YORK, "2024-03-09 16:00", "2024-03-09 20:00", "2024-03-10 00:00", "2024-03-10 04:00"
    )
    declaration = _declare("4h", convention=START, zone="America/New_York")
    with pytest.raises(TemporalError, match="spans a daylight-saving change"):
        check_spacing({"SPY": stamps}, declaration, TimestampKind.NAIVE)
    # The same bars stamped at their ends are placed where they say.
    ends = _walls(NEW_YORK, "2024-03-09 20:00", "2024-03-10 00:00", "2024-03-10 04:00")
    check_spacing({"SPY": ends}, _declare("4h", zone="America/New_York"), TimestampKind.NAIVE)


def test_unusual_whole_day_intervals_are_fixed_lengths_not_months() -> None:
    """27 and 45 days are fixed intervals; a month-length range used to swallow 27-day gaps."""

    start = dt.datetime(2024, 1, 3, tzinfo=UTC)
    for days in (27, 45):
        stamps = _every(start, dt.timedelta(days=days), 5)
        report = check_spacing({"X": stamps}, _declare(f"{days}d"), TimestampKind.EPOCH_SECONDS)
        assert report.instruments["X"].one_interval_pairs == 4


# --- calendar months ----------------------------------------------------------


def test_month_end_bars_are_counted_in_calendar_months() -> None:
    month_ends = ("2024-01-31", "2024-02-29", "2024-03-31", "2024-04-30", "2024-06-30")
    report = check_spacing(
        {"IDX": _end_of_days(IST, *month_ends)},
        _declare("1M", zone="Asia/Kolkata"),
        TimestampKind.DATE,
    )
    assert report.arithmetic == "calendar_months"
    assert (
        report.instruments["IDX"].one_interval_pairs,
        report.instruments["IDX"].longer_gaps,
    ) == (3, 1)

    firsts = ("2024-01-01", "2024-04-01", "2024-07-01", "2024-10-01")
    quarterly = check_spacing(
        {"IDX": _end_of_days(IST, *firsts)}, _declare("3M", zone="Asia/Kolkata"), TimestampKind.DATE
    )
    assert quarterly.instruments["IDX"].one_interval_pairs == 3


@pytest.mark.parametrize(
    ("days", "reason"),
    [
        # 27 days after a month end is not a month end: not a month.
        (("2024-01-31", "2024-02-27", "2024-03-31"), "not labelled on a consistent day"),
        # 45 days: neither one month nor two.
        (("2024-01-15", "2024-02-29", "2024-03-15"), "not labelled on a consistent day"),
        # The same day each time, but two in one month.
        (("2024-01-15", "2024-01-15", "2024-02-15"), r"0 month\(s\) apart, less than"),
    ],
)
def test_a_monthly_declaration_the_dates_contradict_is_refused(
    days: tuple[str, ...], reason: str
) -> None:
    with pytest.raises(TemporalError, match=reason):
        check_spacing(
            {"IDX": _end_of_days(IST, *days)},
            _declare("1M", zone="Asia/Kolkata"),
            TimestampKind.DATE,
        )


def test_quarterly_bars_a_month_apart_are_refused() -> None:
    with pytest.raises(TemporalError, match="less than the declared 3M"):
        check_spacing(
            {"IDX": _end_of_days(IST, "2024-01-31", "2024-02-29", "2024-05-31")},
            _declare("3M", zone="Asia/Kolkata"),
            TimestampKind.DATE,
        )


# --- wall-clock times a zone shows twice, or never ----------------------------


@pytest.mark.parametrize(
    ("text", "problem"),
    [("2024-11-03 01:30:00", "happens twice"), ("2024-03-10 02:30:00", "does not exist")],
)
def test_ambiguous_and_nonexistent_wall_clock_times_are_refused(text: str, problem: str) -> None:
    declaration = _declare("15m", zone="America/New_York")
    with pytest.raises(TemporalError, match=problem):
        label_instant(text, TimestampFormat.DATETIME_SPACE, declaration)
    # The same wall-clock time a day later names one instant, and is read.
    later = text.replace("-03 ", "-04 ").replace("-10 ", "-11 ")
    label_instant(later, TimestampFormat.DATETIME_SPACE, declaration)
