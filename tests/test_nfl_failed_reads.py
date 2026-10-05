"""A failed read is not an empty read: nfl_pick_lock.fetch_lock and nfl_injuries.fetch.
Run: PYTHONPATH=. python3 tests/test_nfl_failed_reads.py"""
import io
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bots" / "nfl"))
import nfl_pick_lock as pl  # noqa: E402
import nfl_injuries as inj  # noqa: E402

cur = Path(tempfile.mkdtemp())          # no local ledger
real_open = urllib.request.urlopen


def raising(exc):
    def _f(*a, **k):
        raise exc
    return _f


try:
    # 404 = genuinely no ledger yet -> fresh start
    urllib.request.urlopen = raising(urllib.error.HTTPError("u", 404, "nf", {}, io.BytesIO(b"")))
    assert pl.fetch_lock("nfl_", 2026, 5, cur) == {}
    # 5xx / timeout / bad JSON = a failed read -> raises, never {}
    for exc in (urllib.error.HTTPError("u", 503, "x", {}, io.BytesIO(b"")), TimeoutError("t"), urllib.error.URLError("dns")):
        urllib.request.urlopen = raising(exc)
        try:
            pl.fetch_lock("nfl_", 2026, 5, cur)
        except pl.LockLedgerUnreadable:
            pass
        else:
            raise AssertionError(f"{exc!r} was treated as an empty ledger")

    class Resp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False
    urllib.request.urlopen = lambda *a, **k: Resp(b"not json")
    try:
        pl.fetch_lock("nfl_", 2026, 5, cur)
    except pl.LockLedgerUnreadable:
        pass
    else:
        raise AssertionError("garbage ledger treated as empty")
    # a ledger for another week is a legitimate fresh start
    urllib.request.urlopen = lambda *a, **k: Resp(b'{"season": 2026, "week": 4}')
    assert pl.fetch_lock("nfl_", 2026, 5, cur) == {}
finally:
    urllib.request.urlopen = real_open


class BadSession:
    def get(self, *a, **k):
        raise ConnectionError("down")


class EmptyOk:
    def get(self, *a, **k):
        class R:
            def raise_for_status(self): pass
            def json(self): return {"injuries": []}
        return R()


try:
    inj.fetch(BadSession())
except inj.InjuriesUnavailable:
    pass
else:
    raise AssertionError("a dead injury feed returned an empty report")

class Malformed:
    def get(self, *a, **k):
        class R:
            def raise_for_status(self): pass
            def json(self): return {"oops": 1}
        return R()
try:
    inj.fetch(Malformed())
except inj.InjuriesUnavailable:
    pass
else:
    raise AssertionError("malformed report accepted")

inj._crosswalk = lambda: {}
assert inj.fetch(EmptyOk()) == {}       # a readable report with nobody on it is real
print("test_nfl_failed_reads: ok")
