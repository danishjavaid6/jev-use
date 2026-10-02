---
name: mobile-use
description: Read and operate the user's Android phone through adb.
---

Use jev-use tools. Keep goals, calls and output short. Run device listing,
reading, location inspection, and actions sequentially; the MCP server runs one
tool at a time. Await each result before starting the next call.

1. `android_devices()` once. No device: ask to connect it. Unauthorized: ask to
   unlock and accept USB debugging. Several devices: pass `serial` explicitly.
2. Read the current screen with `android_read(serial="...")`.
3. Perform the requested action with
   `android_use(serial="...", goal="...", act=true)`.
   Use `decompose=false` for a single step. Then `android_read` to verify.
4. For Facebook's account location use `android_location`. Report `location`
   only when `state=location`; `state=login` means sign-in is required.

If MCP tools are missing, search once, then use the built-in shell fallback:

```text
jev-use call android_devices
jev-use call android_read
jev-use call android_use --goal "open Settings" --act
```

Use `--serial <id>` when needed and `--json-file <file>` for advanced arguments.
Do not write helper clients, install packages, or loop over failed calls.
A "Request not started" busy response means this call was rejected, not that
its requested action is running. Wait for the named original call to finish.
Do not poll by starting another action/read or assume an unchanged screen proves
a hang. If the original call times out, report its error and inspect once after
the server is available; never repeat an uncertain state-changing action.
Retry a transient failure once; otherwise report it and stop.
The phone must be unlocked. Typing requires a configured text model and supports
ASCII only. scrcpy is optional. Quote what the screen shows; do not invent values.
