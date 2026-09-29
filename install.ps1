<#
.SYNOPSIS
  jev-use — one command to a working install (Windows).

.DESCRIPTION
  Windows twin of install.sh:

    irm https://raw.githubusercontent.com/hamzajavaid2005/jev-use/main/install.ps1 | iex

  What it does, in order:

    1. checks node 18+ and npm
    2. collects the Jev/Typesafe API key (parameter, TYPESAFE_API_KEY, or a prompt)
    3. installs the package globally
    4. runs `jev-use install`, which provisions python, cua-driver, the MCP
       server and the /browser-use + /mobile-use skills
    5. runs `jev-use doctor` and prints the one manual step that is left

  Every step is idempotent — running it again repairs rather than reinstalls.

.PARAMETER Key
  Your Jev/Typesafe API key. Defaults to the TYPESAFE_API_KEY environment
  variable, and prompts if neither is set.

.PARAMETER Repo
  GitHub repo to install from. Default: hamzajavaid2005/jev-use

.PARAMETER Ref
  Branch, tag or commit to install. Default: main

.PARAMETER Npm
  Install a published npm package instead of the GitHub repo.

.PARAMETER NoDoctor
  Skip the final `jev-use doctor`.
#>
[CmdletBinding()]
param(
  [string]$Key = $env:TYPESAFE_API_KEY,
  [string]$Repo = 'hamzajavaid2005/jev-use',
  [string]$Ref = 'main',
  [string]$Npm = '',
  [switch]$NoDoctor
)

$ErrorActionPreference = 'Stop'

function Write-Say  { param([string]$Message) Write-Host $Message }
function Write-Step { param([string]$Message) Write-Host "> $Message" -ForegroundColor DarkGray }
function Write-Ok   { param([string]$Message) Write-Host "OK $Message" -ForegroundColor Green }
function Write-Warn { param([string]$Message) Write-Host "! $Message" -ForegroundColor Yellow }
function Fail       { param([string]$Message) Write-Host "x $Message" -ForegroundColor Red; exit 1 }

function Test-Node {
  # `where` rather than Get-Command: a missing command throws under
  # ErrorActionPreference = Stop, which would abort instead of explaining.
  if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
    Fail 'node not found — install Node 18+ from https://nodejs.org and re-run.'
  }
  if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
    Fail 'npm not found — it ships with Node; install Node 18+ and re-run.'
  }
  $major = 0
  try { $major = [int](node -p 'process.versions.node.split(".")[0]') } catch { $major = 0 }
  if ($major -lt 18) {
    Fail "node $(node -v) is too old — jev-use needs Node 18 or newer."
  }
}

function Get-Key {
  if ($Key) { return }

  Write-Host ''
  Write-Host '  Jev/Typesafe API key ' -NoNewline
  Write-Host '(https://console.typesafe.ai/settings/keys)' -ForegroundColor DarkGray
  Write-Host '  key: ' -NoNewline
  $secure = Read-Host -AsSecureString
  $plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
    [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
  )
  $script:Key = ($plain -replace '\s', '')

  if (-not $script:Key) {
    Write-Warn 'no API key yet — browser_use cannot decide anything without one'
    Write-Say  '    add it later:  jev-use install --key=<key>'
  }
}

function Get-PackageSpec {
  if ($Npm) { return $Npm }
  # The unqualified /archive/<ref>.tar.gz form accepts a branch, a tag or a SHA.
  return "https://github.com/$Repo/archive/$Ref.tar.gz"
}

# A private repo 404s the archive URL above — that fetch is unauthenticated even
# for someone who can see the repo — whereas git goes through the credential
# helper (the one `gh auth login` sets up).
function Get-GitSpec {
  return "git+https://github.com/$Repo.git#$Ref"
}

# Run one `npm install -g`, with the key exported for the postinstall that saves
# it. Returns whether npm succeeded.
function Invoke-NpmInstall {
  param([string]$Spec)

  $previous = $env:TYPESAFE_API_KEY
  if ($Key) { $env:TYPESAFE_API_KEY = $Key }

  & npm install -g --no-fund --no-audit $Spec
  $code = $LASTEXITCODE

  if ($null -eq $previous) { Remove-Item Env:TYPESAFE_API_KEY -ErrorAction SilentlyContinue }
  else { $env:TYPESAFE_API_KEY = $previous }

  return ($code -eq 0)
}

# Clone the repo into a tarball, and hand npm the tarball.
#
# Deliberately NOT `npm install -g git+https://...`: on npm 10 — what ships with
# Node 22 — that symlinks the package to a clone inside npm's own cache
# directory, so postinstall cannot find bin/jev-use.js and the install stops with
# MODULE_NOT_FOUND. Measured, npm 9 and npm 10 both install a packed tarball into
# a real directory instead, which is also what keeps the harness bound to a path
# npm will not later delete.
function New-GitTarball {
  param([string]$Dir)

  & npm pack --pack-destination $Dir (Get-GitSpec) | Out-Null
  if ($LASTEXITCODE -ne 0) { return '' }

  # Match the tarball rather than parsing npm's output: the directory was empty
  # before this and `npm pack` prints its notices on some versions.
  $tarball = Get-ChildItem -Path $Dir -Filter '*.tgz' | Select-Object -First 1
  if (-not $tarball) { return '' }
  return $tarball.FullName
}

function Remove-PackDir {
  param([string]$Dir)
  if ($Dir -and (Test-Path $Dir)) {
    Remove-Item -Recurse -Force $Dir -ErrorAction SilentlyContinue
  }
}

function Install-Package {
  # A published package is a single source, and the only one worth trying.
  if ($Npm) {
    Write-Step "installing $Npm"
    if (-not (Invoke-NpmInstall $Npm)) { Fail "could not install $Npm (see the npm output above)." }
    Write-Ok 'package installed'
    return
  }

  # The archive tarball is a plain remote fetch — no git, no credentials — and
  # npm copies it into place.
  Write-Step "installing $(Get-PackageSpec)"
  if (Invoke-NpmInstall (Get-PackageSpec)) {
    Write-Ok 'package installed'
    return
  }

  Write-Warn 'that failed — a private repo 404s the archive URL, so packing over git instead'

  # The temp directory is made here, not inside New-GitTarball, so that this
  # function owns it and can always clean it up.
  $dir = Join-Path ([System.IO.Path]::GetTempPath()) ('jev-use-pack-' + [System.Guid]::NewGuid().ToString('N'))
  New-Item -ItemType Directory -Force -Path $dir | Out-Null

  $tarball = New-GitTarball -Dir $dir
  if (-not $tarball) {
    Remove-PackDir -Dir $dir
    Fail "could not clone $(Get-GitSpec) — check that you can read the repo, then re-run."
  }

  Write-Step "installing $tarball"
  $installed = Invoke-NpmInstall $tarball
  Remove-PackDir -Dir $dir
  if (-not $installed) { Fail "could not install $tarball (see the npm output above)." }
  Write-Ok 'package installed'
}

# The CLI's location, without assuming the global bin directory is on PATH.
function Get-CliPath {
  $command = Get-Command jev-use -ErrorAction SilentlyContinue
  if ($command) { return $command.Source }

  $prefix = ''
  try { $prefix = (npm prefix -g 2>$null | Select-Object -First 1) } catch { $prefix = '' }
  if ($prefix) {
    foreach ($name in 'jev-use.cmd', 'jev-use.ps1', 'jev-use') {
      $candidate = Join-Path $prefix $name
      if (Test-Path $candidate) { return $candidate }
    }
  }
  return ''
}

function Complete-Install {
  $jev = Get-CliPath
  if (-not $jev) {
    Fail 'the package installed but the jev-use command was not found; check the npm output above.'
  }

  Write-Step 'provisioning the runtime and registering the MCP server'
  if ($Key) { & $jev install "--key=$Key" } else { & $jev install }

  if (-not $NoDoctor) {
    & $jev doctor
    if ($LASTEXITCODE -ne 0) {
      Write-Warn 'doctor reported problems — see the lines marked !!'
    }
  }
}

function Show-NextSteps {
  Write-Host ''
  Write-Host 'Ready. Two things left:' -ForegroundColor White
  Write-Host ''
  Write-Host '  1. Restart your harness so it picks up the new MCP server, then:'
  Write-Host '       /browser-use what you want it to do'
  Write-Host '       /mobile-use  what you want it to do'
  Write-Host ''
  Write-Host '  2. Chrome needs a CDP port before browser_use can drive it (once per machine):'
  Write-Host '       jev-use list'
  Write-Host '       jev-use open "<profile>"'
  Write-Host '     Android needs nothing beyond adb and a USB cable.'
  Write-Host ''

  if (-not (Get-Command jev-use -ErrorAction SilentlyContinue)) {
    Write-Warn 'jev-use is not on your PATH. The harness is unaffected (it stores an'
    Write-Say  '    absolute path), but to use the CLI yourself add the npm global'
    Write-Say  '    directory to PATH, or reopen your terminal.'
  }
}

Test-Node
Get-Key
Install-Package
Complete-Install
Show-NextSteps
