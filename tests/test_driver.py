"""Tests for the driver client's failure behaviour.

The transport must fail fast and legibly: a wedged or dead `cua-driver` used to
hang the whole MCP tool call until the *harness* timed out, which is what made a
real run burn steps restarting Chrome.
"""

from __future__ import annotations

import os
import stat

import pytest

from jev_use import driver as driver_mod
from jev_use.driver import Driver, DriverError


def _script(tmp_path, body: str) -> str:
    path = tmp_path / "driver.sh"
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


@pytest.mark.skipif(os.name == "nt", reason="needs a POSIX shell script")
def test_a_call_that_never_answers_times_out(tmp_path, monkeypatch) -> None:
    """A blocked CSS/JS dialog wedges `Runtime.evaluate`; the call must give up on a
    deadline instead of hanging until the harness kills it."""
    monkeypatch.setattr(driver_mod, "CALL_TIMEOUT_SECONDS", 0.3)
    d = Driver(command=_script(tmp_path, "exec sleep 60"))
    try:
        with pytest.raises(DriverError, match="did not answer"):
            d.start()
        assert d.alive is False, "the wedged driver is killed so the session is dropped"
    finally:
        d.close()


@pytest.mark.skipif(os.name == "nt", reason="needs a POSIX shell script")
def test_a_driver_that_dies_is_reported_not_hidden(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(driver_mod, "CALL_TIMEOUT_SECONDS", 2.0)
    d = Driver(command=_script(tmp_path, "exit 0"))
    try:
        with pytest.raises(DriverError, match="closed stdout|not accepting input"):
            d.start()
    finally:
        d.close()
