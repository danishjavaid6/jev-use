#!/usr/bin/env bash
#
# jev-use — one command to a working install.
#
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/hamzajavaid2005/jev-use/main/install.sh)"
#
# `bash -c "$(curl ...)"` rather than `curl ... | bash` on purpose: piping into
# bash makes stdin the script itself, so the API-key prompt below could not read
# the terminal. This form keeps stdin on the tty.
#
# Linux and macOS. Windows has its own twin, install.ps1.
#
# What it does, in order:
#
#   1. checks node 18+ and npm
#   2. collects the Jev/Typesafe API key (flag, TYPESAFE_API_KEY, or a prompt)
#   3. installs the package globally
#   4. runs `jev-use install`, which provisions python, cua-driver, the MCP
#      server and the /browser-use + /mobile-use skills
#   5. runs `jev-use doctor` and prints the one manual step that is left
#
# Every step is idempotent — running it again repairs rather than reinstalls.
set -euo pipefail

REPO="hamzajavaid2005/jev-use"
REF="main"
NPM_NAME=""
KEY="${TYPESAFE_API_KEY:-}"
RUN_DOCTOR=1

#: Set when npm's global directory is not writable and we fall back to a
#: user-owned prefix; the CLI is then looked up here first.
USED_PREFIX=""

#: Holds the tarball the git fallback builds, and is the only thing here that
#: needs cleaning up.
TMP_DIR=""
cleanup() {
  if [ -n "$TMP_DIR" ] && [ -d "$TMP_DIR" ]; then
    rm -rf "$TMP_DIR"
  fi
}
trap cleanup EXIT

IS_TTY=1
[ -t 2 ] || IS_TTY=0
if [ "$IS_TTY" -eq 0 ] || [ -n "${NO_COLOR:-}" ]; then
  C_DIM=""; C_BOLD=""; C_GREEN=""; C_YELLOW=""; C_RED=""; C_OFF=""
else
  C_DIM=$'\033[2m'; C_BOLD=$'\033[1m'; C_GREEN=$'\033[32m'
  C_YELLOW=$'\033[33m'; C_RED=$'\033[31m'; C_OFF=$'\033[0m'
fi

say()  { printf '%s\n' "$*" >&2; }
step() { printf '%s %s\n' "${C_DIM}>${C_OFF}" "$*" >&2; }
ok()   { printf '%s %s\n' "${C_GREEN}OK${C_OFF}" "$*" >&2; }
warn() { printf '%s %s\n' "${C_YELLOW}!${C_OFF}" "$*" >&2; }
die()  { printf '%s %s\n' "${C_RED}x${C_OFF}" "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
jev-use install — one command to a working browser/Android decision loop.

Usage
  install.sh [options]

Options
  --key=<key>          Your Jev/Typesafe API key. Also read from the
                       TYPESAFE_API_KEY environment variable. If neither is
                       set you are prompted.
  --repo=<owner/name>  GitHub repo to install from.
                       Default: hamzajavaid2005/jev-use
  --ref=<ref>          Branch, tag or commit to install. Default: main
  --npm=<name>         Install a published npm package instead of the repo.
  --no-doctor          Skip the final `jev-use doctor`.
  -h, --help           This text.
EOF
}

parse_args() {
  for arg in "$@"; do
    case "$arg" in
      --key=*)   KEY="${arg#--key=}" ;;
      --repo=*)  REPO="${arg#--repo=}" ;;
      --ref=*)   REF="${arg#--ref=}" ;;
      --npm=*)   NPM_NAME="${arg#--npm=}" ;;
      --no-doctor) RUN_DOCTOR=0 ;;
      -h|--help) usage; exit 0 ;;
      '') ;;
      *) die "unknown option: $arg (try --help)" ;;
    esac
  done
}

need_node() {
  command -v node >/dev/null 2>&1 || die "node not found — install Node 18+ from https://nodejs.org and re-run."
  command -v npm  >/dev/null 2>&1 || die "npm not found — it ships with Node; install Node 18+ and re-run."
  local major
  major="$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0)"
  if [ "$major" -lt 18 ] 2>/dev/null; then
    die "node $(node -v) is too old — jev-use needs Node 18 or newer."
  fi
}

# This script is the POSIX bootstrap; Windows has its own twin. Under Git Bash,
# MSYS or Cygwin the POSIX tools run against the *Windows* node/npm, so the venv
# layout, the driver installer and the profile paths all differ. Handing off here
# beats building a half-working install whose breakage only shows up later.
guard_windows() {
  case "$(uname -s 2>/dev/null || echo)" in
    MINGW*|MSYS*|CYGWIN*)
      die "this is a Windows shell — use install.ps1 instead:
    powershell -ExecutionPolicy Bypass -c \"irm https://raw.githubusercontent.com/${REPO}/${REF}/install.ps1 | iex\""
      ;;
  esac
}

# The key goes in the state directory, never the package directory, so it is
# only ever entered once per machine and never logged.
collect_key() {
  [ -n "$KEY" ] && return 0

  # `curl | bash` consumes stdin with the script itself, so the prompt has to
  # come from the terminal; `bash -c "$(curl ...)"` leaves stdin on the tty.
  #
  # The subshell is the only reliable test that /dev/tty is *openable*: `[ -r ]`
  # passes on a box with no controlling terminal, and the prompt would then
  # appear to a user whose answer has nowhere to go.
  local source=""
  if [ -t 0 ]; then
    source="/dev/stdin"
  elif (exec </dev/tty) 2>/dev/null; then
    source="/dev/tty"
  fi

  if [ -n "$source" ]; then
    say ""
    say "  ${C_BOLD}Jev/Typesafe API key${C_OFF} ${C_DIM}(https://console.typesafe.ai/settings/keys)${C_OFF}"
    printf '  key: ' >&2
    local entered=""
    if read -r -s entered <"$source" 2>/dev/null; then
      printf '\n' >&2
      KEY="$(printf '%s' "$entered" | tr -d '[:space:]')"
    fi
  fi

  if [ -z "$KEY" ]; then
    warn "no API key yet — browser_use cannot decide anything without one"
    say "    add it later:  jev-use install --key=<key>"
  fi
  return 0
}

package_spec() {
  if [ -n "$NPM_NAME" ]; then printf '%s' "$NPM_NAME"; return; fi
  # The unqualified /archive/<ref>.tar.gz form accepts a branch, a tag or a SHA.
  printf 'https://github.com/%s/archive/%s.tar.gz' "$REPO" "$REF"
}

# The same repo, over git.
#
# A private repo 404s the archive URL above — that fetch is unauthenticated even
# for someone who can see the repo — whereas git goes through the credential
# helper (the one `gh auth login` sets up). That is the whole reason collaborator
# access works at all.
git_spec() {
  printf 'git+https://github.com/%s.git#%s' "$REPO" "$REF"
}

# Clone the repo into a tarball, and hand npm the tarball.
#
# Deliberately NOT `npm install -g git+https://...`: on npm 10 — the version
# shipped with Node 22 — that symlinks the package to a clone inside npm's own
# cache directory, so `postinstall` cannot find `bin/jev-use.js` and the install
# dies with MODULE_NOT_FOUND. Measured, both npm 9 and npm 10 install a packed
# tarball into a real directory instead, which is also what keeps the harness
# bound to a path npm will not later delete.
pack_git_source() {
  npm pack --pack-destination "$TMP_DIR" "$(git_spec)" >/dev/null 2>&1 || return 1

  # Match the tarball rather than parsing npm's output: the directory was empty
  # before this and `npm pack` prints its notices on stdout on some versions.
  local tarball="" candidate
  for candidate in "$TMP_DIR"/*.tgz; do
    if [ -f "$candidate" ]; then
      tarball="$candidate"
      break
    fi
  done
  [ -n "$tarball" ] || return 1
  printf '%s' "$tarball"
}

#: Where npm may install: its global directory when that is writable, otherwise a
#: user-owned prefix. A system node often leaves the global one needing root, and
#: probing for that here keeps the install to one command instead of sending the
#: user off to fix npm first. Empty output means "the global directory".
install_target() {
  local prefix
  prefix="$(npm prefix -g 2>/dev/null || true)"
  if [ -n "$prefix" ]; then
    if mkdir -p "$prefix/lib/node_modules" 2>/dev/null && [ -w "$prefix/lib/node_modules" ]; then
      return 0
    fi
  fi
  printf '%s' "$HOME/.local/share/jev-use/npm"
}

# The key is exported rather than passed as a flag because the lifecycle script
# that saves it (`postinstall`) runs inside npm, not here.
npm_install() {
  local spec="$1" prefix="$2"
  local args=(npm install -g --no-fund --no-audit)
  [ -n "$prefix" ] && args+=(--prefix "$prefix")
  args+=("$spec")
  if [ -n "$KEY" ]; then
    TYPESAFE_API_KEY="$KEY" "${args[@]}"
  else
    "${args[@]}"
  fi
}

install_package() {
  local target
  target="$(install_target)"
  if [ -n "$target" ]; then
    warn "npm's global directory is not writable; installing into $target instead"
    mkdir -p "$target"
    USED_PREFIX="$target"
  fi

  # A published package is a single source, and the only one worth trying.
  if [ -n "$NPM_NAME" ]; then
    step "installing $NPM_NAME"
    npm_install "$NPM_NAME" "$target" || die "could not install $NPM_NAME (see the npm output above)."
    ok "package installed"
    return 0
  fi

  # The archive tarball is a plain remote fetch — no git, no credentials — and
  # npm copies it into place.
  step "installing $(package_spec)"
  if npm_install "$(package_spec)" "$target"; then
    ok "package installed"
    return 0
  fi

  warn "that failed — a private repo 404s the archive URL, so packing over git instead"

  # Created here rather than inside pack_git_source: that runs in a command
  # substitution, so a TMP_DIR it set would die with the subshell and the cleanup
  # trap above would have nothing to remove.
  [ -n "$TMP_DIR" ] || TMP_DIR="$(mktemp -d)"

  local tarball
  tarball="$(pack_git_source)" || die "could not clone $(git_spec) — check that you can read the repo, then re-run."

  step "installing $tarball"
  npm_install "$tarball" "$target" || die "could not install $tarball (see the npm output above)."
  ok "package installed"
}

# The CLI's location, without assuming the global bin directory is on PATH —
# and without assuming it is the *default* global directory, since the fallback
# above may have put it somewhere else.
cli_path() {
  local prefix
  for prefix in "$USED_PREFIX" "$(npm prefix -g 2>/dev/null || true)"; do
    [ -n "$prefix" ] || continue
    if [ -x "$prefix/bin/jev-use" ]; then printf '%s' "$prefix/bin/jev-use"; return 0; fi
    if [ -x "$prefix/jev-use.cmd" ]; then printf '%s' "$prefix/jev-use.cmd"; return 0; fi
    if [ -x "$prefix/jev-use" ]; then printf '%s' "$prefix/jev-use"; return 0; fi
  done
  command -v jev-use 2>/dev/null || true
}

finish() {
  local jev
  jev="$(cli_path)"
  [ -n "$jev" ] || die "the package installed but the jev-use command was not found; re-run with --npm=<name> or check the npm output above."

  step "provisioning the runtime and registering the MCP server"
  if [ -n "$KEY" ]; then
    "$jev" install --key="$KEY"
  else
    "$jev" install
  fi

  if [ "$RUN_DOCTOR" -eq 1 ]; then
    "$jev" doctor || warn "doctor reported problems — see the lines marked !!"
  fi
}

next_steps() {
  local bin_dir=""
  bin_dir="$(npm prefix -g 2>/dev/null || true)"
  bin_dir="${bin_dir:-$HOME/.local}/bin"

  say ""
  say "${C_BOLD}Ready.${C_OFF} Two things left:"
  say ""
  say "  1. ${C_BOLD}Restart your harness${C_OFF} so it picks up the new MCP server, then:"
  say "       /browser-use what you want it to do"
  say "       /mobile-use  what you want it to do"
  say ""
  say "  2. ${C_BOLD}Chrome needs a CDP port${C_OFF} before browser_use can drive it (once per machine):"
  say "       jev-use list"
  say "       jev-use open \"<profile>\""
  say "     Android needs nothing beyond adb and a USB cable."

  if ! command -v jev-use >/dev/null 2>&1; then
    say ""
    say "  ${C_YELLOW}!${C_OFF} jev-use is not on your PATH. The harness is unaffected (it stores"
    say "    an absolute path), but to use the CLI yourself add:"
    if [ -n "$USED_PREFIX" ]; then
      say "       export PATH=\"$USED_PREFIX/bin:\$PATH\""
    else
      say "       export PATH=\"$bin_dir:\$PATH\""
    fi
  fi
}

main() {
  parse_args "$@"
  guard_windows
  need_node
  collect_key
  install_package
  finish
  next_steps
}

main "$@"
