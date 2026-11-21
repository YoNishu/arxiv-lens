"""The day a result is built for, with its ISO week and its month.

A build reads the clock once and works from these three values only: the day goes into
stats.json and the header of a running week's page, the week decides which weeks have ended,
and the month decides which papers the vocabulary is fitted on. Weeks are ISO weeks, Monday to
Sunday in UTC, and are labelled by the ISO year of their Monday so that labels sort like weeks.
"""
import re
from dataclasses import dataclass
from datetime import date, timedelta

_WEEK_LABEL = re.compile(r"([0-9]{4})-W([0-9]{2})")


@dataclass(frozen=True)
class AsOf:
    day: date
    week_start: date
    month_start: date

    @property
    def week(self) -> str:
        return week_label(self.week_start)

    @property
    def month(self) -> str:
        return month_label(self.day)


def of(day: date) -> AsOf:
    return AsOf(day=day, week_start=monday_of(day), month_start=day.replace(day=1))


def monday_of(day: date) -> date:
    return day - timedelta(days=day.weekday())


def week_label(monday: date) -> str:
    """2026-W36 for the week that starts on Monday 2026-08-31."""
    iso = monday.isocalendar()
    return "%04d-W%02d" % (iso[0], iso[1])


def week_monday(label: str) -> date:
    """The Monday of a week label."""
    match = _WEEK_LABEL.fullmatch(label)
    if match is None:
        raise ValueError("not a week label: %s" % label)
    return date.fromisocalendar(int(match.group(1)), int(match.group(2)), 1)


def month_label(day: date) -> str:
    return "%04d-%02d" % (day.year, day.month)


def day_of(text: str) -> date:
    """The date of a day (2026-09-03) or an instant (2026-09-03T08:00:00Z)."""
    return date(int(text[0:4]), int(text[5:7]), int(text[8:10]))
