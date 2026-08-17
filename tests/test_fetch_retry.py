#!/usr/bin/env python3
"""Does a 429 get retried instead of killing the job?

Written off GitHub Actions run 86867016481, which failed with

    No published slate at .../today_slim.json (HTTP 429).
    ##[error]Process completed with exit code 1.

on a file that was published and fine. Two faults: 429 was treated as absence,
and the message said so out loud. This locks down both.

Runs against a real local HTTP server rather than a mocked requests.get, because
the thing being tested is behaviour over a socket — statuses, headers and
sleeps — and a mock would let a wrong Retry-After parse pass.

No pytest needed: python tests/test_fetch_retry.py
"""
from __future__ import annotations

import http.server
import json
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bots.fetch_picks_for_grading import (  # noqa: E402
    get_with_retry, describe_status, TRANSIENT_STATUS, PERMANENT_ABSENT,
)

FAILS = 0
CHECKS = 0


def check(label, got, want):
    global FAILS, CHECKS
    CHECKS += 1
    ok = got == want
    if not ok:
        FAILS += 1
    print(f"  {'ok  ' if ok else 'FAIL'} {label}"
          + ('' if ok else f'   got={got!r} want={want!r}'))


class Handler(http.server.BaseHTTPRequestHandler):
    """Fails a configured number of times, then serves the payload."""

    plan = []          # list of statuses to return before succeeding
    hits = 0
    retry_after = None

    def do_GET(self):                      # noqa: N802
        cls = type(self)
        cls.hits += 1
        idx = cls.hits - 1
        if idx < len(cls.plan):
            code = cls.plan[idx]
            self.send_response(code)
            if cls.retry_after is not None:
                self.send_header('Retry-After', str(cls.retry_after))
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        body = json.dumps([{'name': 'Alec Burleson', 'hr_score': 66.5}]).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):             # silence the server
        pass


def serve():
    srv = http.server.HTTPServer(('127.0.0.1', 0), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, f'http://127.0.0.1:{srv.server_port}/slate.json'


def reset(plan, retry_after=None):
    Handler.plan = list(plan)
    Handler.hits = 0
    Handler.retry_after = retry_after


print('── describe_status: a status must not be called absence unless it is ──')
check('404 reads as not published', 'not published yet', describe_status(404))
check('410 reads as not published', 'not published yet', describe_status(410))
check('429 names the rate limit', True, 'rate limited' in describe_status(429))
check('429 says the file is fine', True, 'the file is fine' in describe_status(429))
check('429 does NOT claim absence', False, 'not published' in describe_status(429))
check('503 blames the server', True, 'erroring server-side' in describe_status(503))
check('403 does NOT claim absence', False, 'not published' in describe_status(403))
check('403 names refusal', True, 'refused' in describe_status(403))
check('429 is transient', True, 429 in TRANSIENT_STATUS)
check('404 is NOT transient', False, 404 in TRANSIENT_STATUS)
check('404 is permanent-absent', True, 404 in PERMANENT_ABSENT)
check('5xx are transient', True, {500, 502, 503, 504} <= TRANSIENT_STATUS)

srv, url = serve()
try:
    print()
    print('── the exact CI failure: 429 then 200 ──')
    reset([429])
    t0 = time.time()
    r = get_with_retry(url, tries=4, base=1.05, label='slate')
    check('recovers to 200', 200, r.status_code)
    check('server saw 2 requests', 2, Handler.hits)
    check('payload survived the retry', 'Alec Burleson', r.json()[0]['name'])
    check('it actually waited', True, (time.time() - t0) >= 1.0)

    print()
    print('── three 429s then 200 (a rate limit that lasts) ──')
    reset([429, 429, 429])
    r = get_with_retry(url, tries=5, base=1.05, label='slate')
    check('recovers to 200', 200, r.status_code)
    check('server saw 4 requests', 4, Handler.hits)

    print()
    print('── 429 forever: returns the response, does not raise ──')
    reset([429] * 20)
    r = get_with_retry(url, tries=3, base=1.02, label='slate')
    check('final status is 429', 429, r.status_code)
    check('gave up after exactly `tries`', 3, Handler.hits)

    print()
    print('── 404 must NOT be retried: absence is not transient ──')
    reset([404] * 20)
    r = get_with_retry(url, tries=5, base=1.02, label='slate')
    check('returns 404', 404, r.status_code)
    check('asked exactly once', 1, Handler.hits)

    print()
    print('── Retry-After is honoured, and capped ──')
    reset([429], retry_after=1)
    t0 = time.time()
    r = get_with_retry(url, tries=3, base=99.0, label='slate')
    waited = time.time() - t0
    check('recovers', 200, r.status_code)
    # base=99 would sleep ~60s (the cap) if Retry-After were ignored.
    check('used the 1s header, not the backoff', True, waited < 8)

    print()
    print('── a garbage Retry-After falls back to backoff, never crashes ──')
    reset([429], retry_after='next tuesday')
    r = get_with_retry(url, tries=3, base=1.05, label='slate')
    check('still recovers', 200, r.status_code)

    print()
    print('── 500 then 200 ──')
    reset([500])
    r = get_with_retry(url, tries=3, base=1.05, label='slate')
    check('recovers to 200', 200, r.status_code)
    check('server saw 2 requests', 2, Handler.hits)

    print()
    print('── unreachable host returns None rather than raising ──')
    r = get_with_retry('http://127.0.0.1:1/nope.json', tries=2, base=1.02, label='dead')
    check('returns None', None, r)
finally:
    srv.shutdown()

print()
print(f'{CHECKS - FAILS}/{CHECKS} checks passed')
sys.exit(1 if FAILS else 0)
