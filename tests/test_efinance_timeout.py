# -*- coding: utf-8 -*-
from __future__ import annotations

import threading
import time

import pytest

from data_provider.efinance_fetcher import FuturesTimeoutError, _ef_call_with_timeout


def test_ef_call_with_timeout_uses_daemon_worker_for_hung_calls() -> None:
    started = threading.Event()
    release = threading.Event()

    def hang_until_released() -> None:
        started.set()
        release.wait(timeout=5)

    start = time.monotonic()
    try:
        with pytest.raises(FuturesTimeoutError):
            _ef_call_with_timeout(hang_until_released, timeout=0.01)

        elapsed = time.monotonic() - start
        assert elapsed < 0.5
        assert started.wait(timeout=1)

        workers = [
            thread
            for thread in threading.enumerate()
            if thread.name.startswith("efinance-call-hang_until_released")
        ]
        assert any(worker.daemon for worker in workers)
    finally:
        release.set()


def test_ef_call_with_timeout_propagates_provider_exception() -> None:
    def fail_fast() -> None:
        raise ValueError("provider failed")

    with pytest.raises(ValueError, match="provider failed"):
        _ef_call_with_timeout(fail_fast, timeout=1)
