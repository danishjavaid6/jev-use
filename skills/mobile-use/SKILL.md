---
name: mobile-use
description: Drive the user's own Android phone to do a task and report what the screens say — apps, settings, messages, anything reachable by tapping. Use when the user asks you to do something on their phone, or to check what it shows.
---

# mobile-use

Use the `jev-use` MCP server. It drives the user's **own** phone over adb, and Jev
makes every on-screen decision.

You bring the reasoning and the reporting. Jev brings the taps. You never pick a
coordinate; you describe a destination.

## Order of operations

**1. `android_devices` first.** It answers the two things that go wrong silently:

* **Nothing listed** → the phone is not connected. USB, or `adb connect <ip>:5555`
  for wireless debugging. On the phone: Settings → Developer options → USB debugging.
* **`[unauthorized]`** → the phone is connected but has not accepted the debugging
  prompt. Unlock it and accept the dialog. Nothing works until it is accepted.

If several devices are attached, pass `serial` explicitly. The server refuses to
guess, and it will not substitute a different phone.

**2. `android_use` to get there.**

```
android_use(goal="open Settings and turn on Airplane mode", act=true)
```

Pass `act=false` (the default) to have Jev decide and validate **without touching
the phone**. That is the right first move for anything destructive or for a screen
you are unsure about — it reports what it *would* do.

Describe the destination, not the taps. Leave `decompose` on unless the goal is
already one step: Jev decides one action at a time and cannot hold a plan, so a
compound goal is split into ordered subgoals first.

`go_back` and `go_home` are offered on every screen. **Backing out of a screen is
often the fastest route to the right one** — do not treat it as a failure.

**3. Get the answer.** `android_use` *changes* the screen; it does not report what
it says. Use **`android_read`** to answer a question about what the phone shows.
It reads the view hierarchy, so the text is exact rather than OCR — no vision model
involved.

**4. Report precisely.** Quote what you saw: names, numbers, times, toggle states.
If a screen needed a login and the session had expired, say so plainly rather than
guessing at the contents.

## Checking the account's Facebook location

`android_location` reports the location Facebook currently attributes to the
account signed in on the phone. It opens Facebook's own **primary location** page
*inside the app* and reads it — no browser, no screenshot, no vision model. Use it
when the user asks which country or city Facebook thinks they are in.

```
android_location()
  device=FFY5T18202024660
  app=com.facebook.katana
  state=location
  location=Lahore, Punjab 54
  seconds=9.42
  url=https://www.facebook.com/primary_location/info?ref=bookmarks&_jev=...
  note=this answers for whichever account is signed in right now; switch accounts
       in the app and check again for the other one
```

Read `state` before the value:

* **`state=location`** — the page named a location. Quote it as-is.
* **`state=login`** — the app is signed out, so there is no account to check. Say
  that; never report the phone's own location as if it were the account's.
* **`state=unknown`** — the page had not rendered. The page's raw text comes back
  under `shows:`; retry before concluding anything.

**For a second account, switch to it in the app first, then call again.** It
reports whichever session is signed in, and it cache-busts the URL on every call so
a re-run cannot be answered from the previous account's cached page.

**What it is not.** It reads Facebook's own inference — the profile's current city,
the connection's IP, check-ins and the device location. It cannot change any of it,
and it is **not** the payout country or a region setting. Do not present this number
as evidence of what an account can monetize.

## Shape of a good answer

Per section — what you opened, what it said, what you could not reach:

```
Phone — reached Settings > Battery
  Battery 78%, "About 1 day 4 hr left"
  Battery Saver: off

WhatsApp — could not reach the chat list: the app opened on a
  "verify your number" screen. Not guessing at the messages.
```

## What it can and cannot do

* **Tap, scroll, type, and navigate.** Tapping a real element the server
  enumerated; never a blind coordinate.
* **Typing is ASCII only.** `adb input text` cannot deliver non-ASCII, so a
  non-ASCII value is **refused rather than mangled**. Typing also needs a text
  model configured (`JEV_USE_TEXT_MODEL`); without one, `type_text` is not offered
  at all and tapping/scrolling/reading still work.
* **The screen must be on and unlocked.** There is no unlock support — a locked
  phone has no view hierarchy worth reading. Ask the user to unlock it.
* **One device, one screen at a time.** Sequential, not parallel.
* **scrcpy is not required.** It is a viewer; the automation channel is adb, the
  same channel scrcpy already uses. If the user has scrcpy open they can watch the
  work happen, which is the point of having it open.

## Do not

* Do not report a task as done because a tool call returned success — read the
  screen.
* Do not summarise a screen you never read.
* Do not call `android_use` repeatedly hoping a different result appears.
* Do not assume a tap worked. The step log says `acted:`; only `android_read`
  says whether the screen actually changed.
