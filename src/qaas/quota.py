"""The provider declining to serve: recognising it, and reading when it ends.

Its own module because three readers need it and one of them must stay light:
the router decides whether to wait or stop, `cli._quota_preflight` probes before
spending, and the dashboard's read model (`ui/state.py`) has to tell a paused
agent from a broken one without importing the router and the SDK behind it.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

#: What a provider's refusal-to-serve looks like in the error text that reaches
#: us. Matched case-blind against whatever the SDK surfaced -- `ResultError:
#: ... You've hit your session limit · resets 4:20pm` is the shape that cost the
#: run this exists for. Deliberately a small list of phrases rather than a regex
#: over status codes: the text is what every layer (SDK exception, result
#: subtype, CLI stderr) has in common, and a marker that is merely absent costs
#: one agent, while a marker that is too eager would stop a healthy run.
#:
#: A bare "429" was tried here and removed: `_dispatch`'s own timeout error
#: reads `exceeded the run's remaining wall clock (429s)`, so a run could stop
#: itself on quota because of how many seconds were left on its clock.
QUOTA_MARKERS = (
    "session limit",
    "rate limit",
    "usage limit",
    "quota",
    "too many requests",
)


def is_quota_error(text: str | None) -> bool:
    """Is this error the provider declining to serve, rather than a defect?"""
    if not text:
        return False
    lowered = str(text).lower()
    return any(marker in lowered for marker in QUOTA_MARKERS)


# `resets 3:50pm (America/New_York)`, `resets 4pm`, `resets Sep 25, 3pm (UTC)`.
_RESET_AT = re.compile(
    r"resets?\s+(?:at\s+)?"
    r"(?:(?P<month>[A-Za-z]{3,9})\s+(?P<day>\d{1,2}),?\s+(?:at\s+)?)?"
    r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<ampm>[ap]\.?m\.?)?"
    r"(?:\s*\((?P<tz>[^)]+)\))?",
    re.IGNORECASE,
)
# `retry after 30 seconds`, `try again in 5 minutes`.
_RETRY_IN = re.compile(
    r"(?:retry|try again)\s+(?:after|in)\s+(?P<n>\d+)\s*(?P<unit>s|sec|secs|seconds?|m|min|mins|minutes?)\b",
    re.IGNORECASE,
)
#: A reset time this far in the past is "just now", not "the same time
#: tomorrow" -- the message was written a moment before it was read.
_JUST_PASSED = timedelta(minutes=10)


def reset_at(text: str | None, now: datetime | None = None) -> datetime | None:
    """When the limit named in `text` lifts, in UTC, or None if it does not say.

    None is the honest answer to anything unrecognised, and the caller stops
    the run rather than guessing: sleeping until a misread time is worse than
    stopping with a resume command.
    """
    if not text:
        return None
    now = now or datetime.now(timezone.utc)

    retry = _RETRY_IN.search(text)
    if retry:
        n = int(retry["n"])
        seconds = n if retry["unit"].lower().startswith("s") else n * 60
        return now + timedelta(seconds=seconds)

    match = _RESET_AT.search(text)
    if not match or not (match["minute"] or match["ampm"]):
        return None  # "resets 4" is not a time
    hour, minute = int(match["hour"]), int(match["minute"] or 0)
    ampm = (match["ampm"] or "").lower().replace(".", "")
    if ampm == "pm" and hour != 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None

    zone = None
    if match["tz"]:
        try:
            zone = ZoneInfo(match["tz"].strip())
        except (ZoneInfoNotFoundError, ValueError):
            zone = None  # an unknown zone name falls back to this machine's
    local_now = now.astimezone(zone) if zone else now.astimezone()
    try:
        when = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if match["month"]:
            month = datetime.strptime(match["month"][:3].title(), "%b").month
            when = when.replace(month=month, day=int(match["day"]))
            if when < local_now - _JUST_PASSED:
                when = when.replace(year=when.year + 1)
        elif when < local_now - _JUST_PASSED:
            when += timedelta(days=1)
    except ValueError:  # Feb 30, a month name that is not one
        return None
    return when.astimezone(timezone.utc)
