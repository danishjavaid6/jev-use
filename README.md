# jev-use

Global browser and Android tools for agent harnesses. Chrome runs in the
background; Jev chooses controls for ambiguous tasks. Exact actions and page
reads run directly without a decision-model request.

## Install once per device

Requires Node 18+. The installer provisions Python, the CDP client, optional
Jev SDK, and the driver fallback. It registers detected harnesses in their
user configuration and installs `/browser-use` and `/mobile-use` globally.
No repository folder needs to be open when using the tools.

Linux/macOS:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/hamzajavaid2005/jev-use/main/install.sh)"
```

Windows PowerShell:

```powershell
irm https://raw.githubusercontent.com/hamzajavaid2005/jev-use/main/install.ps1 | iex
```

Repair/register Command Code:

```text
jev-use install --harness=commandcode
jev-use doctor
```

Set keys once:

```text
jev-use install --key=<typesafe-key>
jev-use install --gologin-token=<gologin-token>
```

Restart the harness after installation. Supported registration adapters:
Command Code, Claude Code, Codex, Cursor, OpenCode, Windsurf, Gemini CLI, VS Code.
Slash-command discovery depends on the harness's skill support. Shared skills
are installed under `~/.agents/skills` (Windows: `%USERPROFILE%\.agents\skills`).
Harnesses installed later need another `jev-use install` to register MCP.

```text
jev-use harnesses
jev-use harnesses --print
```

The second command prints configuration for other MCP-capable harnesses.
For a private repository, installers need GitHub access. To install a specific
revision, download `install.ps1` and run it with `-Ref <commit>`; on POSIX use
the bootstrap's `--ref=<commit>` option. Do not install the unrelated public
registry package named `jev-use`. Install this repository's archive or a packed
tarball. Avoid direct npm git installs that can register temporary cache paths.

## Choose the shortest tool path

Call `browser_profiles` once. Reuse the matching port, or call `browser_open`
with the requested profile. Use its returned port in later calls.

| Need | Tool | Uses Jev? |
| --- | --- | --- |
| Read a known URL | `browser_read(port=N, url="https://...")` | No |
| Read several URLs in parallel | `browser_read_many(port=N, urls=[...])` | No |
| Click an exact label | `browser_action(port=N, action="click", label="Save")` | No |
| Fill an exact field | `browser_action(port=N, action="fill", label="Email", text="...")` | No |
| Scroll/back | `browser_action(port=N, action="scroll_down")` or `action="back"` | No |
| Ambiguous or multi-step goal | `browser_use(port=N, goal="...", act=true)` | Yes, when needed |
| Extract typed answers | `browser_extract(port=N, questions={...})` | Yes |

`browser_action` acts once and returns page text. Click/fill require exactly one
matching visible control; ambiguous labels are refused. `browser_use` validates
Jev's choice against real controls; exact deterministic decisions and cached
plans bypass Jev. Use `decompose=false` for one-step goals. Compound planning
and generated text require a configured `JEV_USE_TEXT_MODEL`; an unavailable
planner is skipped. Literal text supplied to `browser_action` needs no writer.

Reads default to 6,000 characters per page; `max_chars` can raise this to 20,000.
The harness receives 12 tool schemas, loaded skill instructions, and tool results;
it does not receive the project source. Actual context usage depends on the
harness's tool discovery and tokenizer.

## Background work

`browser_open` launches Chrome headless by default. Set `background=false` to
show it. Known URL tasks use a dedicated background tab; parallel reads pin
each tab by ID. CDP actions do not move the system mouse or send OS keystrokes,
so the user can work in other windows. Do not automate the same page the user
is editing. Sites that open native dialogs may still need user interaction.

Chrome profiles are copies in a private data directory because Chrome blocks
CDP on its default directory. Copies are snapshots; `refresh=true` updates them.
Copying a large profile is a one-time cost, not a model delay. On Windows,
installed Chrome takes precedence over PATH/registry entries that may point to
Orbita. `JEV_USE_BROWSER_BINARY` selects an explicit browser executable.

GoLogin uses its own SDK and Orbita with the profile's fingerprint, proxy and
cookies. Use `browser_open(profile="name", vendor="gologin")`, then
`browser_close()` to stop and save the profile. Its native launch is preserved;
`headless=true` requests no visible window. Cloud debugger URLs are unsupported;
this transport expects a local CDP port. Get a token from
<https://app.gologin.com/#/personalArea/TokenApi>.

## Android

Install Android platform-tools (`adb`) and accept USB debugging on an unlocked
phone. `scrcpy` is optional. Start with `android_devices`, then `android_read` or
`android_use(goal="...", act=true)`. Read again to verify actions.

Pass `serial` when multiple devices are attached. `android_location` reads
Facebook's account location; quote it only when `state=location`. Typing supports
ASCII and generated text requires a text model. Phone automation can run while
the user works on the PC, but shares the phone's visible screen with manual use.

## Missing MCP tools

Use the built-in fallback instead of asking the model to write clients:

```text
jev-use call browser_profiles
jev-use call browser_open --profile Default
jev-use call browser_read --port 9222 --url https://example.com
jev-use call browser_action --port 9222 --action click --label Save
jev-use call android_devices
jev-use call android_use --goal "open Settings" --act
```

Use the actual returned port. `--json-file <path>` supplies advanced arguments
without shell quoting problems. The fallback starts a process per command;
native MCP reuses a session and is faster for repeated calls. Search for missing
tools once, use the fallback, and report persistent failures instead of repair
loops. `doctor` checks configuration and installed paths; it does not prove the
app connected its MCP session.

## Development

```text
pip install -e '.[dev,harness]'
python -m pytest -q
npm test
```

Source and historical design notes: [docs/architecture.md](docs/architecture.md).
The npm install excludes retired desktop modules and development tests.
