"""staleness_watch: it must stay quiet when nothing is scheduled to refresh
(2026-10-24: 13 false alarms in 3 days), and still check when a slot is due."""
import datetime as dt
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bots"))
import staleness_watch as sw  # noqa: E402

U = dt.timezone.utc
at = lambda *a: dt.datetime(*a, tzinfo=U)


def test_friday_afternoon_is_not_checked():          # the 10-23 17:09 / 20:27 / 23:32 alerts
    for h in (17, 20, 23):
        assert not sw.in_week_windows(at(2026, 10, 23, h), sw.NFL_BOARD_WINDOWS)
        assert not sw.in_week_windows(at(2026, 10, 23, h), sw.NFL_FANTASY_WINDOWS)


def test_thursday_night_after_the_last_pregame_build_is_not_checked():   # 10-23 04:43Z
    assert not sw.in_week_windows(at(2026, 10, 23, 4, 43), sw.NFL_BOARD_WINDOWS)


def test_due_windows_are_checked():
    assert sw.in_week_windows(at(2026, 10, 22, 18), sw.NFL_BOARD_WINDOWS)       # Thu run-up
    assert sw.in_week_windows(at(2026, 10, 25, 17), sw.NFL_BOARD_WINDOWS)       # Sun
    assert sw.in_week_windows(at(2026, 10, 26, 0, 30), sw.NFL_BOARD_WINDOWS)    # SNF kickoff
    assert sw.in_week_windows(at(2026, 10, 25, 20), sw.NFL_FANTASY_WINDOWS)     # Sun live stats
    assert not sw.in_week_windows(at(2026, 10, 24, 20), sw.NFL_FANTASY_WINDOWS)  # Sat: no games


def _sched(total):
    r = mock.MagicMock()
    r.__enter__.return_value = r
    r.read.return_value = ('{"totalGames": %d}' % total).encode()
    return r


def test_mlb_no_games_skips():
    with mock.patch("urllib.request.urlopen", return_value=_sched(0)):
        assert sw.mlb_has_games(at(2026, 10, 21, 17)) is False


def test_mlb_games_checks():
    with mock.patch("urllib.request.urlopen", return_value=_sched(1)):
        assert sw.mlb_has_games(at(2026, 10, 23, 17)) is True


def test_mlb_schedule_unreadable_fails_open():
    with mock.patch("urllib.request.urlopen", side_effect=OSError("down")):
        assert sw.mlb_has_games(at(2026, 10, 21, 17)) is True
