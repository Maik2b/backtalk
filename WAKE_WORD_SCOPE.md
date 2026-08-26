# (C) Wake-word activation — scope

Not built. Written up for review before implementation. Two genuinely
separate problems hide behind "say Hey Jarvis" — this doc splits them
so they can be scoped, approved, and built independently.

## What's true today (checked against the code, 2026-08-26)

- **Hands-free mode has no wake word.** `ears.py`'s `Ears.listen_once()`
  uses VAD (voice activity detection) only: ~120ms of sustained speech
  opens an utterance, ~480ms of trailing silence closes it, and
  whatever gets transcribed is sent to Claude as a real turn — no
  gating on content. Anything said in the room while hands-free is on
  can trigger a real action. This matches the README's own framing
  ("room audio and videos CAN trigger it").
- **Nothing runs until backtalk is started.** There is no background
  process today. `start.bat`/`start.sh` launch the face, the voice
  line, and (for hands mode) barehands as foreground processes tied to
  an open window; closing the window stops everything. There's no
  listener alive before that.

## Problem 1: "Hey Jarvis" as a gate on hands-free listening

**Goal:** in hands-free mode, ignore all speech until "Hey Jarvis" (or
similar) is heard, then open the mic for the actual request — so room
noise/conversations/TV can't trigger real actions, without going back
to push-to-talk.

**What it needs:**
- A wake-word detector running continuously while hands-free is
  active, cheap enough to run alongside the VAD loop without competing
  for the mic or the CPU budget faster-whisper needs.
- Options, roughly in order of fit for a Windows/local/free setup:
  - **openWakeWord** (Apache 2.0, ONNX models, CPU-light, trainable
    custom wake words) — closest fit to backtalk's "local, free, no
    API keys" ethos, but has no stock "Jarvis" model; a custom model
    would need training or borrowing a community one.
  - **Porcupine** (Picovoice) — very low CPU, ships a "Jarvis" model
    out of the box, but requires a free API key and has usage limits
    on the free tier — breaks backtalk's "no accounts, no per-word
    costs" promise unless that tradeoff is accepted explicitly.
  - A cheap DIY approach: keep VAD as the "something was said" trigger,
    transcribe short snippets with the existing faster-whisper model,
    and gate on whether the transcript starts with the wake phrase.
    Simplest to build (no new dependency), but burns a full STT pass
    on every stray sound in the room, which is the opposite of
    lightweight.
- A new `mic_mode` or a modifier on `"open"` (e.g. `"wake"`) in
  `backtalk.json`, plus the matching voice-console verbs ("wake word
  mode" / back to plain hands-free) alongside the existing `micopen`/
  `micptt` verbs in `main.py`.
- Decide the UX after the wake word fires: does it then behave like
  one push-to-talk turn (listen once, act, go back to waiting for the
  wake word), or does it open a normal hands-free window for some
  seconds? The former is safer and matches "Hey Siri"-style assistants;
  the latter is more natural for multi-sentence requests.

**Open questions for Mike:**
- Porcupine's free-tier API key vs. openWakeWord's "no ready-made
  Jarvis model" — which tradeoff is acceptable?
- After the wake word, one-shot listen or a short open window?

## Problem 2: auto-launching the whole stack from a cold start

**Goal:** say "Hey Jarvis" when nothing is running yet (just booted the
PC, no backtalk process alive) and have the full voice+face stack come
up on its own.

**Why this is a different problem, not an extension of Problem 1:**
Problem 1's detector only exists *inside* an already-running backtalk
process. For a cold start, *something* has to be listening before
backtalk itself is running — that means a second, separate,
always-on process.

**What it needs:**
- A standalone lightweight listener (same wake-word engine as Problem
  1, ideally shared code) that runs in the background at all times —
  either started at Windows login (Task Scheduler / Startup folder) or
  manually toggled on.
- On detecting the wake word with nothing running, it shells out to
  `fullstack-agent\start.bat voice` (the same command the desktop
  shortcut runs) and then hands off — once backtalk is up, the
  standalone listener should stand down so it isn't competing with
  backtalk's own mic access for the rest of the session.
- Needs a "is backtalk already running?" check (e.g. a lock file or
  port check) so the cold-start listener doesn't try to launch a
  second instance if one's already up.
- The "pop up in Chrome" ask from the original request maps to the
  existing face — `ai-visualizer` already opens a browser tab as part
  of `start.bat`; no separate mechanism needed there, just confirming
  the face's window comes forward instead of opening behind other
  windows.

**Open questions for Mike:**
- Should this cold-start listener run at every Windows login
  automatically, or only when you manually turn it on?
- Acceptable latency from saying "Hey Jarvis" cold to the face actually
  appearing — backtalk's own startup (model loads, browser launch)
  takes several seconds per the logs (`ears model ready`, `mouth voice
  ready` etc. each land a few seconds apart), so cold-start won't be
  instant the way triggering mid-session is.

## Suggested build order, if greenlit

1. Problem 1 first (gate hands-free on a wake word) — smaller, self-
   contained inside backtalk, immediately useful even without
   Problem 2.
2. Problem 2 second, reusing whatever wake-word engine Problem 1
   settles on, as a thin separate always-on script.

## Not in scope here

- Anything about push-to-talk mode — it already requires a deliberate
  key press, so it has no wake-word gap to close.
- Changing what happens *after* the wake word/PTT triggers a real
  question — that's the existing `handle()` path, untouched by this.
