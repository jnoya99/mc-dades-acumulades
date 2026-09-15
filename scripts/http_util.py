#!/usr/bin/env python3
"""Shared HTTP GET with exponential backoff + jitter.

Retries on: timeouts, URLError, HTTP 429 and 5xx.
Does not retry other 4xx. Stdlib only.
"""
from __future__ import annotations

import random
import time
import urllib.error
import urllib.request
from typing import Mapping

DEFAULT_UA = "mc-dades-acumulades/1.0 (+https://github.com/jnoya99/mc-dades-acumulades)"
DEFAULT_ATTEMPTS = 5
DEFAULT_TIMEOUT = 90


def http_get(
    url: str,
    *,
    timeout: int = DEFAULT_TIMEOUT,
    headers: Mapping[str, str] | None = None,
    max_attempts: int = DEFAULT_ATTEMPTS,
    user_agent: str | None = None,
) -> bytes:
    """GET url; retry transient failures with exponential backoff + jitter."""
    hdrs = {
        "User-Agent": user_agent or DEFAULT_UA,
        "Accept": "*/*",
    }
    if headers:
        hdrs.update(headers)

    last_exc: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        req = urllib.request.Request(url, headers=hdrs, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            last_exc = e
            code = int(getattr(e, "code", 0) or 0)
            # Always drain/close the body when present
            try:
                if e.fp is not None:
                    e.fp.read()
            except Exception:
                pass
            if code == 429 or 500 <= code <= 599:
                if attempt >= max_attempts:
                    raise
                _sleep_backoff(attempt)
                continue
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_exc = e
            if attempt >= max_attempts:
                raise
            _sleep_backoff(attempt)
            continue

    assert last_exc is not None
    raise last_exc


def _sleep_backoff(attempt: int) -> None:
    """attempt is 1-based; sleep ~ 2^(attempt-1) + jitter seconds, capped."""
    base = min(30.0, float(2 ** (attempt - 1)))
    jitter = random.uniform(0.0, 0.75)
    time.sleep(base + jitter)


# Alias used by capture scripts
_http_get = http_get
