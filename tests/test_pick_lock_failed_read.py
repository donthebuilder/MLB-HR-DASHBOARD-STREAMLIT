"""A failed ledger read is not an empty ledger (MLB pick_lock.fetch_lock; the NFL twin is tests/test_nfl_failed_reads.py).
Run: PYTHONPATH=. python3 tests/test_pick_lock_failed_read.py"""
import io
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bots"))
import pick_lock as pl  # noqa: E402

pl.CURRENT = Path(tempfile.mkdtemp())    # no local ledger
real_open = urllib.request.urlopen


class Resp(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): return False


def raising(exc):
    def _f(*a, **k):
        raise exc
    return _f


try:
    urllib.request.urlopen = raising(urllib.error.HTTPError("u", 404, "nf", {}, io.BytesIO(b"")))
    assert pl.fetch_lock("2026-10-16") == {}                      # 404 = no ledger yet -> fresh start
    for exc in (urllib.error.HTTPError("u", 503, "x", {}, io.BytesIO(b"")), TimeoutError("t"), urllib.error.URLError("dns")):
        urllib.request.urlopen = raising(exc)
        try:
            pl.fetch_lock("2026-10-16")
        except pl.LockLedgerUnreadable:
            pass
        else:
            raise AssertionError(f"{exc!r} was treated as an empty ledger")
    urllib.request.urlopen = lambda *a, **k: Resp(b"not json")
    try:
        pl.fetch_lock("2026-10-16")
    except pl.LockLedgerUnreadable:
        pass
    else:
        raise AssertionError("garbage ledger treated as empty")
    urllib.request.urlopen = lambda *a, **k: Resp(b'{"date": "2026-10-15"}')
    assert pl.fetch_lock("2026-10-16") == {}                      # another slate's ledger = fresh start
    urllib.request.urlopen = lambda *a, **k: Resp(b'{"date": "2026-10-16", "games": {}}')
    assert pl.fetch_lock("2026-10-16")["date"] == "2026-10-16"
finally:
    urllib.request.urlopen = real_open
print("ok   a failed ledger read raises; a 404 or another slate's ledger is a fresh start")
