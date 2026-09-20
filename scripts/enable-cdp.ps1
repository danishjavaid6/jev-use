<#
.SYNOPSIS
    Open a Chrome profile with a CDP endpoint so browser_use can drive it.

.DESCRIPTION
    Windows twin of scripts/enable-cdp.sh. It delegates to
    `python -m jev_use.profiles` so the copy-and-launch logic has exactly one
    implementation, shared with the browser_open MCP tool.

    Why this COPIES instead of using your profile in place
    ------------------------------------------------------
    Chrome refuses to open a remote-debugging port on the default data
    directory:

        > chrome.exe --remote-debugging-port=9222 `
              --user-data-dir="$env:LOCALAPPDATA\Google\Chrome\User Data"
        DevTools remote debugging requires a non-default data directory.

    So the obvious approach is impossible -- Chrome exits immediately. The
    supported route is a copy in a non-default location. Your cookies travel
    with the copy, so the logins are there; but the copy is a SNAPSHOT, so a
    sign-in made to the original afterwards will not reach it. Use -Refresh to
    re-copy.

    One profile is typically 0.5-1.5 GB, copied once and then reused.

.EXAMPLE
    .\enable-cdp.ps1 --list
    .\enable-cdp.ps1 --open "Work"
    .\enable-cdp.ps1 --open "Outlook 3" --refresh
#>

$ErrorActionPreference = "Stop"

$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $Here

# Prefer the project venv, then the Windows Python launcher, then PATH.
$Py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Py)) {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $Py = "py"
    } elseif (Get-Command python -ErrorAction SilentlyContinue) {
        $Py = (Get-Command python).Source
    } else {
        Write-Error ("no python found (looked for $Root\.venv\Scripts\python.exe, " +
            "the 'py' launcher, and python on PATH)")
        exit 1
    }
}

Push-Location $Root
try {
    # $args forwards --list / --open NAME / --refresh / --port unchanged.
    & $Py -m jev_use.profiles @args
} finally {
    Pop-Location
}

exit $LASTEXITCODE
