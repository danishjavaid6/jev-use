"""Tests for the platform layer.

These run on Linux and assert Windows behaviour, which is the whole point: the
Windows paths are the ones that cannot be exercised by hand here, so they are the
ones that most need pinning down. Everything below drives `host` by flipping
`IS_WINDOWS`/`IS_MACOS`, so the branches are covered on any development machine.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jev_use import host


# -- parsing an arbitrary command line --------------------------------------


@pytest.mark.parametrize(
    "args, expected",
    [
        ("/opt/google/chrome/chrome --remote-debugging-port=9222", "/opt/google/chrome/chrome"),
        ('"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" --foo', "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"),
        (r"C:\Chrome\chrome.exe --foo", r"C:\Chrome\chrome.exe"),
        ("   /usr/bin/chromium   ", "/usr/bin/chromium"),
        ("", ""),
    ],
)
def test_argv0_unwraps_the_executable(args: str, expected: str) -> None:
    assert host.argv0(args) == expected


@pytest.mark.parametrize(
    "path, expected",
    [
        ("/opt/google/chrome/chrome", "chrome"),
        (r"C:\Program Files\Google\Chrome\Application\chrome.exe", "chrome"),
        (r"C:\Chrome\CHROME.EXE", "chrome"),
        ("/usr/bin/google-chrome-stable", "google-chrome-stable"),
        ("/snap/bin/brave-browser", "brave-browser"),
        ("", ""),
    ],
)
def test_executable_stem_is_separator_agnostic(path: str, expected: str) -> None:
    """`Path.stem` is wrong here: on POSIX a backslash is not a separator, so a
    Windows path would never reduce to `chrome` and discovery would silently fail."""
    assert host.executable_stem(path) == expected


# -- the Windows process table ----------------------------------------------


def test_parses_powershell_cim_json_array() -> None:
    payload = (
        '[{"ProcessId":4242,"CommandLine":"\\"C:\\\\Chrome\\\\chrome.exe\\" '
        '--remote-debugging-port=9222"},'
        '{"ProcessId":1,"CommandLine":null}]'
    )
    assert host._parse_windows_table("powershell", payload) == [
        (4242, '"C:\\Chrome\\chrome.exe" --remote-debugging-port=9222')
    ]


def test_parses_powershell_cim_json_single_object() -> None:
    """ConvertTo-Json emits a bare object, not a one-element array, for one match."""
    payload = '{"ProcessId":7,"CommandLine":"chrome.exe"}'
    assert host._parse_windows_table("pwsh", payload) == [(7, "chrome.exe")]


def test_parses_wmic_csv_whose_command_line_contains_commas() -> None:
    """wmic quotes fields containing commas, so a bare split() would shear a path
    in half and the profile directory would be garbage."""
    payload = (
        "Node,CommandLine,ProcessId\n"
        'MYPC,"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" '
        '--remote-debugging-port=9222 --user-data-dir="C:\\a,b",4242\n'
    )
    rows = host._parse_windows_table("wmic", payload)
    assert rows == [
        (4242, '"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" --remote-debugging-port=9222 --user-data-dir="C:\\a,b"')
    ]


@pytest.mark.parametrize("payload", ["", "   ", "not json", "{}", "[]"])
def test_unparseable_tables_yield_nothing(payload: str) -> None:
    assert host._parse_windows_table("powershell", payload) == []


def test_process_table_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Discovery is best-effort: a broken `ps` means "nothing found", not a crash."""

    def boom(*_a, **_k):
        raise OSError("no ps here")

    monkeypatch.setattr(host, "IS_WINDOWS", False)
    monkeypatch.setattr(host.subprocess, "run", boom)
    assert host.process_table() == []


# -- per-platform paths -----------------------------------------------------


def test_chrome_root_on_windows_ignores_home(monkeypatch: pytest.MonkeyPatch) -> None:
    """%LOCALAPPDATA% wins, because a redirected profile directory is common and
    Path.home() does not follow it."""
    monkeypatch.setattr(host, "IS_WINDOWS", True)
    monkeypatch.setattr(host, "IS_MACOS", False)
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\me\AppData\Local")
    assert host.chrome_user_data_root() == (
        Path(r"C:\Users\me\AppData\Local") / "Google" / "Chrome" / "User Data"
    )


def test_chrome_root_on_macos() -> None:
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(host, "IS_WINDOWS", False)
    monkeypatch.setattr(host, "IS_MACOS", True)
    try:
        assert host.chrome_user_data_root() == Path.home() / (
            "Library/Application Support/Google/Chrome"
        )
        assert host.work_root() == Path.home() / "Library/Application Support/jev-use"
    finally:
        monkeypatch.undo()


def test_chrome_root_on_linux() -> None:
    assert host.chrome_user_data_root() == Path.home() / ".config" / "google-chrome"


def test_work_root_is_never_the_profile_root(monkeypatch: pytest.MonkeyPatch) -> None:
    """The copy must live somewhere other than the default data directory, or
    Chrome refuses the debug port and the whole design collapses."""
    monkeypatch.setattr(host, "IS_WINDOWS", True)
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\me\AppData\Local")
    assert host.work_root() != host.chrome_user_data_root()
    assert "jev-use" in str(host.work_root())


# -- the driver binary ------------------------------------------------------


def test_driver_override_beats_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CUA_DRIVER_COMMAND", "/opt/cua/cua-driver")
    monkeypatch.setattr(host.shutil, "which", lambda *a, **k: "/usr/bin/cua-driver")
    assert host.find_cua_driver() == "/opt/cua/cua-driver"


def test_driver_found_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CUA_DRIVER_COMMAND", raising=False)
    monkeypatch.setattr(host.shutil, "which", lambda *a, **k: "/usr/bin/cua-driver")
    assert host.find_cua_driver() == "/usr/bin/cua-driver"


def test_driver_found_in_the_windows_install_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """install.ps1 appends to the *User* PATH, a change a running process cannot
    see — so the known install directory must be checked, not just PATH."""
    monkeypatch.setattr(host, "IS_WINDOWS", True)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.delenv("CUA_DRIVER_COMMAND", raising=False)
    monkeypatch.setattr(host.shutil, "which", lambda *a, **k: None)

    binary = tmp_path / "Programs" / "Cua" / "cua-driver" / "bin" / "cua-driver.exe"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"")

    assert host.find_cua_driver() == str(binary)


def test_driver_not_found_returns_none(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(host, "IS_WINDOWS", True)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.delenv("CUA_DRIVER_COMMAND", raising=False)
    monkeypatch.setattr(host.shutil, "which", lambda *a, **k: None)
    assert host.find_cua_driver() is None


def test_windows_install_hint_is_powershell_not_bash(monkeypatch: pytest.MonkeyPatch) -> None:
    """install.sh contains no Windows handling at all — it has a separate
    install.ps1 — so a Windows user handed the curl|bash command is stuck."""
    monkeypatch.setattr(host, "IS_WINDOWS", True)
    assert "install.ps1" in host.driver_install_hint()
    assert "powershell" in host.driver_install_hint()

    monkeypatch.setattr(host, "IS_WINDOWS", False)
    assert "install.sh" in host.driver_install_hint()


def test_detach_kwargs_are_posix_on_posix() -> None:
    assert host.detach_kwargs() == {"start_new_session": True}


# -- launcher naming --------------------------------------------------------


def test_launcher_hint_is_per_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    assert host.launcher_hint() == "scripts/enable-cdp.sh"

    monkeypatch.setattr(host, "IS_WINDOWS", True)
    assert host.launcher_hint() == "scripts/enable-cdp.ps1"
