"""FOMC decision-day schedule + high-frequency posting window.

The Fed publishes FOMC meeting dates a year ahead. The rate decision is
announced on the 2nd (statement) day at 2:00 PM ET, followed by the Powell
press conference at 2:30 PM ET. Crypto reacts hard from just before the
announcement through the press conference, so the bot densifies posting
around that window (X only — see the workflow).

Announcement *instants* are stored in UTC with the correct DST offset baked
in: 2:00 PM ET = 18:00 UTC during EDT (Mar–early Nov), 19:00 UTC during EST
(Jan & Dec). Keeping them as absolute UTC instants makes the window check
DST-correct without any timezone math at call time, and makes the date-
targeted cron in the workflow safe — if the cron ever fires on the same
calendar date in a *later* year, the year in these datetimes won't match and
``active_announcement`` returns None, so nothing is posted.

UPDATE THIS LIST EACH YEAR when the Fed publishes the next calendar.
Source: https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

# 2026 FOMC rate-decision announcements (2:00 PM ET), expressed in UTC.
#   EDT (UTC-4), Mar–Oct meetings → 18:00 UTC
#   EST (UTC-5), Jan & Dec meetings → 19:00 UTC
# Statement day = 2nd day of each two-day meeting. ✓ = Summary of Economic
# Projections / dot plot meeting.
FOMC_ANNOUNCEMENTS_UTC: tuple[datetime, ...] = (
    datetime(2026, 1, 28, 19, 0, tzinfo=timezone.utc),   # Jan 27-28 (EST)
    datetime(2026, 3, 18, 18, 0, tzinfo=timezone.utc),   # Mar 17-18 (EDT) ✓
    datetime(2026, 4, 29, 18, 0, tzinfo=timezone.utc),   # Apr 28-29 (EDT)
    datetime(2026, 6, 17, 18, 0, tzinfo=timezone.utc),   # Jun 16-17 (EDT) ✓
    datetime(2026, 7, 29, 18, 0, tzinfo=timezone.utc),   # Jul 28-29 (EDT)
    datetime(2026, 9, 16, 18, 0, tzinfo=timezone.utc),   # Sep 15-16 (EDT) ✓
    datetime(2026, 10, 28, 18, 0, tzinfo=timezone.utc),  # Oct 27-28 (EDT)
    datetime(2026, 12, 9, 19, 0, tzinfo=timezone.utc),   # Dec 8-9  (EST) ✓
)

# Dense-posting window relative to the announcement instant.
# 30 min before → 120 min after, sampled every 30 min by the cron, yields
# 6 posts: T-30, T+0, T+30, T+60, T+90, T+120. Covers the announcement and
# the bulk of the 2:30 PM ET press conference.
WINDOW_BEFORE = timedelta(minutes=30)
WINDOW_AFTER = timedelta(minutes=120)


def active_announcement(now: datetime | None = None) -> datetime | None:
    """Return the FOMC announcement whose dense window contains ``now``.

    Returns None when ``now`` is outside every window (the common case).
    A naive ``now`` is assumed to be UTC.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    for ann in FOMC_ANNOUNCEMENTS_UTC:
        if ann - WINDOW_BEFORE <= now <= ann + WINDOW_AFTER:
            return ann
    return None


def fomc_window(now: datetime | None = None) -> bool:
    """True if ``now`` falls inside a FOMC high-frequency posting window."""
    return active_announcement(now) is not None
