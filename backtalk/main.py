# backtalk: talk to your Claude Code agent out loud.
# Copyright (C) 2026 Jared Rhodenizer
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""backtalk — talk to your Claude Code agent out loud.

Flow: hold the key and speak -> local transcription -> your agent's warm
Claude session streams the reply -> sentences go to the mouth the moment
they complete (~1-2s to first audio on warm turns). The greeting plays
over a hidden warmup query so the first real turn is already hot.

Typing in this terminal is a first-class turn too: same conversation,
spoken reply, and typing while it talks interrupts it.

THE VOICE CONSOLE: exact phrases, spoken (or typed) alone, control the
session itself so you never go back to the keyboard: "clear the
session" / "compact the session" / "switch to the deep model" / "back
to the fast model" / "set effort to low" (or medium, high, max) /
"usage report" / "go hands free" and "push to talk mode" (the MIC) /
"stop asking for permission" and "start asking again" (permissions,
called auto-approve, a different axis than the microphone on purpose).
And with permission_mode "ask" (the default), gated tool calls ASK OUT
LOUD and your spoken yes or no decides them; any other answer is
passed back to the agent as the reason.

Flags:
  --open-mic   start in hands-free listening for this session (the
               config key mic_mode makes it the standing default, and
               the voice can switch live either way: "go hands free" /
               "push to talk mode"). Know the tradeoff: room audio (a
               video, music, another voice assistant) can trigger
               replies to speech never meant for the agent. The talk
               key keeps working: it interrupts, and holding it always
               gets you heard.
  --barge-in   with --open-mic: keep listening WHILE speaking.
               HEADPHONES REQUIRED — with open speakers the mic hears
               the reply and the agent interrupts itself.
  --model X    override the model for this session (full id).

Say "goodbye <name>" / "end voice mode" to hang up. Ctrl-C works.
"""
import asyncio
import json
import os
import queue
import re
import sys
import threading
import time

from backtalk import launch, screen, signals, wake
from backtalk.brain import WarmBrain
from backtalk.config import CFG
from backtalk.ears import Ears, record_held, warm as warm_ears
from backtalk.mouth import Mouth
from backtalk.ptt import PTTListener
from backtalk.vlog import log

NAME = CFG["name"]
QUIT_PHRASES = CFG["quit_phrases"]
SHUTDOWN_PHRASES = CFG["shutdown_phrases"]

# Written for the run's whole lifetime so an outside process (the
# cold-start listener in wake_listener.py) can tell "backtalk is
# already up" from "nothing is running" without guessing from
# .voice_state, which has no liveness signal of its own and would
# look permanently live after a crash. Removed on any clean exit;
# left behind after a crash/kill, so a PID check on read is what makes
# a stale file safe (see wake_listener.is_backtalk_running).
_LOCK_FILE = os.path.join(CFG["signals_dir"], ".backtalk_pid")

# ---- THE SPOKEN PERMISSION GATE (permission_mode "ask", the default).
# When the agent wants a gated tool, the SDK routes the decision here:
# the ask is spoken, the turn pauses (the SDK waits indefinitely; the
# timeout below is ours), and the NEXT utterance or typed line is the
# answer. "yes" approves; anything else denies, with the user's own
# words passed back as the reason. Silence means no.
PERM_TIMEOUT_S = 75
_PERM = {"fut": None, "asked_at": 0.0,   # pending ask + when it was posed
         "hinted": False}                # escape-hatch hint said yet?
_CONFIRM = {"verb": None, "at": 0.0}     # pending "say confirm" + when
_INTERRUPT_ANSWER = "\x00interrupt"      # sentinel: turn is being killed
# ---- AUTOMATIC MODEL TIERING (see 07 Problems/... no automatic model
# tiering note for the full design). Jarvis reads each request's shape
# and silently runs easy asks on Haiku and everything else on the
# normal default, except a genuinely hard request, which asks ONCE per
# session before it's allowed to spend a turn on Opus. "manual_until_
# fast" is set whenever Mike (or a face) explicitly picks a model, and
# only cleared by explicitly going back to the fast model — auto-
# tiering never fights a deliberate choice mid-session.
_AUTOTIER = {"manual_until_fast": False,   # a manual switch is in effect
            "opus_ok": False}             # one-time Opus confirm granted?
# The one-time "okay to use Opus for this?" ask. Same shape as _PERM
# (a future resolved by the next utterance, spoken, with a timeout) but
# its OWN slot: _PERM is specifically the SDK's per-tool-call gate,
# fired from inside can_use_tool, and folding this into it would make
# handle() misread the ask that follows a hard turn's auto-tier as if
# it were a tool-permission answer instead of a model-tier one.
TIER_CONFIRM_TIMEOUT_S = 30
_TIER_CONFIRM = {"fut": None, "asked_at": 0.0}
# Live AUTO-APPROVE is OUR flag, not an SDK mode flip: the CLI refuses
# a live switch INTO bypassPermissions unless it was launched with the
# danger flag, so instead the gate below auto-approves silently while
# this is on. Same behavior, no reconnect, conversation intact. A
# session that BOOTS in bypassPermissions never consults the gate at
# all; saying "start asking again" flips the SDK side live (that
# direction is allowed) and turns this off. ONLY the explicit
# bypassPermissions value arms this: any other mode (acceptEdits, plan)
# passes through to the SDK and keeps the spoken gate for whatever the
# SDK routes here. (Auto-approve is about PERMISSIONS; hands-free
# LISTENING is about the microphone: see _MIC below. Two different
# axes, deliberately never sharing a name.)
_AUTOAPPROVE = {"on": False}
# The microphone mode, switchable live by voice. "ptt" = mic closed
# except while the key is held. "open" = hands-free listening (VAD).
# The key keeps working in open mode: it interrupts, and holding it
# always gets you heard. gen bumps on every switch so an in-flight
# open-mic capture from before the switch gets discarded, never
# processed.
_MIC = {"mode": "ptt", "gen": 0, "btn": False}

# Approvals are EXACT matches after normalization, never prefixes:
# "yesterday", "yes or no", and "yes, but do not overwrite" must all
# fail. Anything that is not an exact yes DENIES, with the words passed
# back to the agent as the reason. Deny is always the default.
_YES = {"yes", "yeah", "yep", "yup", "sure", "approve", "approved",
        "go ahead", "do it", "yes please", "yes sir", "yes boss",
        "yes go ahead", "go for it", "green light", "okay", "ok", "y"}
_CHAIN_MARKS = ("&&", "||", ";", "|", "$(", "`", "\n")


def _norm_speech(text):
    """Lowercase, every non-letter to space, collapse. Whisper loves
    interior commas ("yes, confirm"); end-stripping alone misses them."""
    out = []
    for ch in text.lower():
        out.append(ch if "a" <= ch <= "z" else " ")
    return " ".join("".join(out).split())


def _deny_pending(reason=_INTERRUPT_ANSWER):
    """Resolve a pending spoken ask as a deny. Called whenever the turn
    that posed it is being interrupted, so the ask can never outlive its
    turn and hijack a later utterance (or stall the pipe drain)."""
    f = _PERM["fut"]
    if f is not None and not f.done():
        f.set_result(reason)


def _human_what(tool, tool_input, ctx):
    """The SHORT spoken form, built for a person who has never seen a
    terminal: plain words, no paths, no syntax. Built by code, never by
    the model, so it cannot understate; and every ask offers "details",
    which reads the full literal form below. (Field case: the gate read
    whole file paths and command syntax at a brand-new user.)"""
    d = tool_input or {}
    if tool in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        path = str(d.get("file_path") or d.get("notebook_path")
                   or "a file").replace("\\", "/")
        name = path.rsplit("/", 1)[-1]
        import os as _os
        homes = [CFG.get("agent_dir", "")] + list(CFG.get("extra_dirs")
                                                  or [])
        in_vault = any(h and path.startswith(str(h).rstrip("/") + "/")
                       for h in (CFG.get("extra_dirs") or []))
        verb = "edit" if "Edit" in tool else "create or change"
        if in_vault and name.endswith(".md"):
            return f"{verb} a note in your vault called {name[:-3]}"
        return f"{verb} a file called {name}"
    if tool == "Bash":
        cmd = " ".join(str(d.get("command", "")).split())
        first = (cmd.split() or ["a"])[0].rsplit("/", 1)[-1]
        chained = any(m in cmd for m in _CHAIN_MARKS)
        return (f"run a {first} command in the terminal"
                + (", with several chained parts" if chained else ""))
    if tool == "WebFetch":
        url = str(d.get("url", ""))
        host = url.split("//", 1)[-1].split("/", 1)[0] or "a site"
        return f"read a web page at {host}"
    name = getattr(ctx, "display_name", None) or tool
    return f"use the {name} tool"


_DETAILS = {"details", "the details", "give me details",
            "give me the details", "what command", "what is it",
            "say more", "more", "what exactly", "the exact command"}


def _full_detail(tool, tool_input, ctx):
    """The full literal form, spoken only when the person asks for
    "details". Never lets a long command hide its tail: truncation is
    DISCLOSED and shell chaining is called out (the agent composes
    tool_input itself, so this line must not be steerable into
    understatement)."""
    d = tool_input or {}
    if tool == "Bash":
        cmd = " ".join(str(d.get("command", "")).split())
        chained = any(m in cmd for m in _CHAIN_MARKS)
        line = ("a chained command: " if chained else
                "run a command: ") + cmd[:90]
        if len(cmd) > 90:
            line += (f", and {len(cmd) - 90} more characters. "
                     "Check the log before approving")
        return line
    if tool in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        path = str(d.get("file_path") or d.get("notebook_path")
                   or "a file").replace("\\", "/")
        bits = path.rsplit("/", 2)
        name = "/".join(bits[-2:]) if len(bits) >= 2 else path
        return f"{'edit' if 'Edit' in tool else 'write'} the file {name}"
    if tool == "WebFetch":
        return f"fetch a web page: {str(d.get('url', ''))[:70]}"
    desc = (getattr(ctx, "description", None) or "").strip()
    name = getattr(ctx, "display_name", None) or tool
    return f"use {name}" + (f", {desc[:70]}" if desc else "")


def make_permission_gate(mouth):
    from claude_agent_sdk import (PermissionResultAllow,
                                  PermissionResultDeny)

    async def gate(tool, tool_input, ctx):
        if _AUTOAPPROVE["on"]:
            return PermissionResultAllow(behavior="allow")
        what = _human_what(tool, tool_input, ctx)
        detail = _full_detail(tool, tool_input, ctx)
        loop = asyncio.get_running_loop()
        signals.static_stop()
        log(f"[perm]   asking: {what}")
        log(f"[perm]   detail: {detail}")
        if tool == "Bash":   # the FULL command always reaches the log
            log(f"[perm]   full command: {str((tool_input or {}).get('command', ''))[:2000]}")
        ask = f"Permission check. I want to {what}. Yes, no, or details?"
        if not _PERM["hinted"]:
            # the escape hatch announces itself exactly once, at the
            # moment it becomes relevant (a field case: a new user
            # couldn't find the phrase to turn the checks off)
            _PERM["hinted"] = True
            ask += (" And any time you're done with these checks, say "
                    "stop asking for permission.")
        mouth.say(ask)
        answer = None
        try:
            deadline = loop.time() + PERM_TIMEOUT_S
            while answer is None:
                fut = loop.create_future()
                _PERM["fut"] = fut
                _PERM["asked_at"] = time.monotonic()
                while True:
                    try:
                        got = await asyncio.wait_for(
                            asyncio.shield(fut), 1.0)
                        break
                    except asyncio.TimeoutError:
                        if loop.time() >= deadline:
                            fut.cancel()
                            mouth.say("No answer, so I didn't do it.")
                            log("[perm]   timed out, denied")
                            return PermissionResultDeny(
                                behavior="deny",
                                message="No spoken answer within the "
                                        "timeout; the action was not "
                                        "approved.",
                                interrupt=False)
                        # keep the ring honest while we wait
                        if not mouth.speaking:
                            signals.set_state("listening")
                if (got != _INTERRUPT_ANSWER
                        and _norm_speech(got) in _DETAILS):
                    # read the full literal form, then ask again with a
                    # fresh clock: asking for details is engagement,
                    # not silence
                    log("[perm]   details requested")
                    mouth.say(f"The details: I want to {detail}. "
                              "Yes or no?")
                    deadline = loop.time() + PERM_TIMEOUT_S
                    continue
                answer = got
        finally:
            _PERM["fut"] = None
        if answer == _INTERRUPT_ANSWER:
            log("[perm]   turn interrupted, denied silently")
            return PermissionResultDeny(
                behavior="deny",
                message="Interrupted by the user; the turn is being "
                        "cancelled.",
                interrupt=False)
        approved = _norm_speech(answer) in _YES
        # the model keeps working either way: restore the working state
        signals.set_state("thinking")
        signals.static_start()
        if approved:
            log("[perm]   approved by voice")
            return PermissionResultAllow(behavior="allow")
        log(f"[perm]   denied: {answer!r}")
        return PermissionResultDeny(
            behavior="deny",
            message=f'Denied by voice. The user said: "{answer[:500]}"',
            interrupt=False)
    return gate


# ---- THE VOICE CONSOLE: session verbs, spoken. Exact phrases only,
# spoken alone, so ordinary sentences can never trigger them. (Grown
# from a community member's own build shared in the Discord.)
CONSOLE_VERBS = {
    "clear":     ("clear the session", "clear the context",
                  "clear context", "fresh slate", "slash clear"),
    "compact":   ("compact the session", "compact the context",
                  "compact context", "slash compact"),
    "deep":      ("switch to the deep model", "use the deep model",
                  "slash model deep"),
    "fast":      ("switch to the fast model", "use the fast model",
                  "back to the fast model", "slash model fast"),
    "usage":     ("usage report", "slash usage"),
    "micopen":   ("go hands free", "hands free mode",
                  "hands free listening", "open mic", "open the mic"),
    "micptt":    ("push to talk", "push to talk mode",
                  "back to push to talk", "back to the button"),
    "micwake":   ("wake word mode", "listen for my wake word",
                  "listen for wake word", "hey jarvis mode",
                  "wait for hey jarvis"),
    "noask":     ("stop asking for permission",
                  "stop asking permission",
                  "stop asking me for permission",
                  "turn off the permission prompt",
                  "turn off the permission prompts",
                  "turn off the permissions prompt",
                  "turn off the permissions prompts",
                  "turn off permissions", "turn off permission checks",
                  "disable the permission checks",
                  "disable permission checks", "auto approve",
                  "auto approve mode"),
    "ask":       ("start asking again", "ask before acting",
                  "ask for permission again"),
    "launch:study_session": ("start my study session",
                             "start study session",
                             "begin my study session"),
}
_EFFORTS = ("low", "medium", "high", "xhigh", "max")

# ---- DIFFICULTY CLASSIFICATION for automatic model tiering. Jarvis
# picks the tier itself — Mike never says "easy" or "hard" out loud
# (see the "no automatic model tiering" problem note). This is a text
# heuristic on the utterance, not a model call: WarmBrain has exactly
# one shared message stream (see brain.py), so a real side-query to
# classify difficulty would either desync that stream or need a whole
# second SDK client just to answer "easy or hard" — too heavy to pay
# on every turn. A cheap heuristic costs nothing and is right often
# enough that being wrong just means a turn runs one tier off, not
# broken; MEDIUM is the fallback for anything ambiguous, matching
# today's fixed-model behavior when nothing else fires.
_HARD_CUES = (
    "build", "implement", "refactor", "design", "architect", "debug",
    "migrate", "rewrite", "write a whole", "write a full", "from scratch",
    "go through all", "go through every", "audit", "review the entire",
    "review the whole", "plan out", "figure out why", "fix the bug",
    "walk me through", "step by step", "multiple files", "the whole",
    "entire codebase", "deep dive",
)
_EASY_CUES = (
    "what time", "what's the time", "what day", "what's today",
    "hello", "hi jarvis", "hey there", "thanks", "thank you",
    "good morning", "good night", "what's up", "how are you",
    "define ", "what does", "what is a", "what is an",
    "spell ", "translate ", "convert ",
)
_EASY_MAX_WORDS = 8


def classify_difficulty(text: str) -> str:
    """"easy" / "medium" / "hard" — see the module-level note above for
    why this is a heuristic and not a model call. Hard cues win over
    easy ones (a long or clearly multi-step ask outranks an easy-cue
    word that happens to appear in it); short+cue-free is the only path
    to "easy", so a real question never gets starved onto Haiku just
    for being short-ish ("What's two plus two" is 5 words but has no
    easy cue — falls through to medium, which is fine: medium is the
    safe default, not a penalty)."""
    norm = " ".join(text.lower().split())
    if any(cue in norm for cue in _HARD_CUES):
        return "hard"
    word_count = len(norm.split())
    if word_count <= _EASY_MAX_WORDS and any(
            norm.startswith(cue) or cue in norm for cue in _EASY_CUES):
        return "easy"
    return "medium"


# Reuses config.py's available_models map rather than a new literal —
# same reasoning as deep_model: one place names model ids.
_TIER_MODEL = {
    "easy": CFG["available_models"].get("Haiku 4.5"),
    "medium": None,       # None = CFG["model"], the fast/default tier
    "hard": None,         # resolved from CFG["deep_model"] at use time
}

# Tag prepended to a model id pulled from .model_request so handle()
# can tell a face's click apart from a person's typed/spoken line —
# never logged as "[you]", never tested against quit phrases.
_GUI_MODEL_PREFIX = "\x00gui_model\x00"

# "Look at my screen": unlike CONSOLE_VERBS, this isn't a slash command
# on the running session — it's a REAL turn (screenshot attached, sent
# to the model), so it's matched as a leading phrase, not an exact
# whole-utterance match. "look at my screen, what's this error" still
# fires and passes the rest through as the question.
SCREEN_PHRASES = (
    "look at my screen", "look at the screen", "share my screen",
    "share the screen", "check my screen", "see my screen",
    "can you see my screen", "take a screenshot",
    "what am i looking at", "what's on my screen", "whats on my screen",
    "what is on my screen", "what am i on right now",
    "what's on the screen", "whats on the screen",
    "what is on the screen", "what do you see on my screen",
    "what can you see on my screen",
)


# Trailer words that carry no question content of their own ("what am I
# looking at RIGHT NOW" leaves "right now" behind, which read back as a
# follow-up question would just confuse the vision turn). Stripped from
# the leftover only when that's ALL that's left, never mid-sentence.
_SCREEN_LEFTOVER_FILLER = {"right now", "now", "currently", "at"}


def _strip_address(text):
    """Drops a leading 'hey <name>,' / '<name>,' address so it doesn't
    break a leading-phrase match. Address only — mid-sentence mentions
    of the agent's name are left alone."""
    norm = text.strip()
    lname = NAME.lower()
    for lead in (f"hey {lname}", lname):
        if norm.lower().startswith(lead):
            rest = norm[len(lead):].lstrip(" ,.!?")
            if rest:
                return rest
    return norm


def screen_share_match(text):
    """Returns the leftover question text if a screen-share phrase
    leads the utterance, else None. 'Look at my screen' alone -> ''
    (speak_reply gets a sensible default prompt instead)."""
    text = _strip_address(text)
    norm = " ".join(text.lower().split())
    for phrase in SCREEN_PHRASES:
        if norm.startswith(phrase):
            rest = text[len(phrase):].strip(" ,.!?")
            if rest.lower() in _SCREEN_LEFTOVER_FILLER:
                rest = ""
            return rest
    return None


def console_match(text):
    norm = " ".join(text.lower().replace("-", " ").split()).strip(" .,!?")
    for verb, phrases in CONSOLE_VERBS.items():
        if norm in phrases:
            return verb
    for lvl in _EFFORTS:
        if norm in (f"set effort to {lvl}", f"effort {lvl}",
                    f"slash effort {lvl}"):
            return f"effort:{lvl}"
    return None


def _write_config_key(key, value):
    """The agent rewrites the config; the person never hand-edits it.
    Returns True on a persisted write. A file that fails to PARSE is
    left untouched (rewriting from {} would wipe every other setting);
    the in-memory CFG updates either way so the session behaves."""
    from backtalk.config import CONFIG_PATH
    CFG[key] = value
    try:
        data = json.loads(CONFIG_PATH.read_text())
    except FileNotFoundError:
        data = {}
    except (OSError, ValueError) as e:
        log(f"[console] config not writable/parsable, session-only: {e}")
        return False
    data[key] = value
    try:
        CONFIG_PATH.write_text(json.dumps(data, indent=2) + "\n")
    except OSError as e:
        log(f"[console] config write failed, session-only: {e}")
        return False
    return True


def _fmt_tokens(n):
    if n >= 1_000_000:
        return f"about {round(n / 1_000_000, 1):g} million tokens"
    if n >= 1000:
        return f"about {round(n / 1000)} thousand tokens"
    return f"{n} tokens"


def _spoken_usage(sess, ctx_usage):
    """A short CFO brief of the session, written for the ear: plain
    numerals only (the TTS reads "40" fine; symbols come out garbled)."""
    turns = sess["turns"]
    parts = [f"{turns} turn{'s' if turns != 1 else ''} this session",
             _fmt_tokens(sess["out_tokens"]) + " spoken out"]
    cents = round(sess["cost"] * 100)
    if cents >= 1:
        parts.append(f"roughly {cents} cents" if cents < 100
                     else f"roughly {round(cents / 100)} dollars")
    try:
        cats = (getattr(ctx_usage, "categories", None)
                or (ctx_usage or {}).get("categories") or [])
        # the breakdown includes "Free space" and the autocompact
        # buffer; only OCCUPIED categories belong in the spoken number
        total = sum(int(c.get("tokens") or 0) for c in cats
                    if isinstance(c, dict)
                    and "free" not in str(c.get("name", "")).lower()
                    and "buffer" not in str(c.get("name", "")).lower())
        if total:
            parts.append(_fmt_tokens(total)
                         + " sitting in the context window")
    except Exception:
        pass
    return ". ".join(parts) + "."

_PASTE_ON = "\x1b[200~"    # bracketed-paste markers (we enable the mode below)
_PASTE_OFF = "\x1b[201~"


# <<anything>> is a stage direction: lifted out, never spoken, published on
# the bus when the audio carrying it starts. Bounded so a runaway model cannot
# swallow a paragraph into one "tag".
_DIRECTION_TAG = re.compile(r"<<([^<>]{1,80})>>")


def _clean_typed(line: str) -> str:
    """Scrub terminal-copy artifacts: blockquote gutter glyphs and stray
    whitespace (copying from a CLI chat render drags bars along)."""
    line = line.strip()
    while line[:1] in ("▎", "│", ">"):
        line = line[1:].lstrip()
    return line


def _join_paste(body: str) -> str:
    """Pasted blob -> one clean message (gutters scrubbed, lines joined)."""
    parts = [_clean_typed(l) for l in body.split("\n")]
    return " ".join(" ".join(p for p in parts if p).split())


def _typed_reader_pipe(q: "queue.Queue[str]", fd: int):
    """Non-tty stdin (pipes/tests): line assembly with paste markers."""
    import os
    pend = ""
    while True:
        try:
            b = os.read(fd, 65536)
        except OSError:
            return
        if not b:
            return
        pend += b.decode("utf-8", "replace")
        while True:
            if _PASTE_ON in pend:
                if _PASTE_OFF not in pend:
                    break
                head, rest = pend.split(_PASTE_ON, 1)
                body, pend = rest.split(_PASTE_OFF, 1)
                *hlines, hpart = head.split("\n")
                for l in hlines:
                    l = _clean_typed(l)
                    if l:
                        q.put(l)
                text = _join_paste(hpart + body)
                if text:
                    q.put(text)
                continue
            if "\n" in pend:
                line, pend = pend.split("\n", 1)
                line = _clean_typed(line)
                if line:
                    q.put(line)
                continue
            break


def _typed_reader_simple(q: "queue.Queue[str]"):
    """Windows (no termios): plain line input on a thread. Pastes work;
    they just echo normally instead of collapsing to a count."""
    while True:
        try:
            line = _clean_typed(input())
        except (EOFError, OSError):
            return
        if line:
            q.put(line)


def _typed_reader(q: "queue.Queue[str]"):
    """Terminal stdin -> typed messages (daemon thread). Typed lines are
    first-class turns: same pipeline as a spoken utterance, spoken reply.

    On a POSIX tty we OWN the input line (cbreak: no kernel echo, no
    canonical buffering — the little line editor below echoes keys,
    handles backspace, and assembles bracketed pastes invisibly). The
    kernel's canonical mode is unfixable for pastes: it echoes the
    markers as visible junk and holds unfinished marker lines hostage.
    Pastes show as `[pasted N chars]`; Enter sends everything as ONE
    message. Ctrl-C still works (ISIG stays on); termios restored at
    exit."""
    import atexit
    import os
    fd = sys.stdin.fileno()
    if not os.isatty(fd):
        _typed_reader_pipe(q, fd)
        return
    try:
        import termios
        import tty as _tty
    except ImportError:            # Windows: no termios — simple reader
        _typed_reader_simple(q)
        return
    old = termios.tcgetattr(fd)
    _tty.setcbreak(fd)                      # ECHO+ICANON off, ISIG kept
    sys.stdout.write("\x1b[?2004h")         # bracket pastes, please
    sys.stdout.flush()

    def _restore():
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        except Exception:
            pass
        sys.stdout.write("\x1b[?2004l")
        sys.stdout.flush()
    atexit.register(_restore)

    MARKS = (_PASTE_ON, _PASTE_OFF)

    def _partial_tail(s: str) -> int:
        """Length of a trailing partial paste-marker (hold it for the
        next read)."""
        for m in MARKS:
            for k in range(min(len(s), len(m) - 1), 0, -1):
                if m.startswith(s[-k:]):
                    return k
        return 0

    buf = ""          # the input line being composed
    paste = None      # accumulating paste body, or None
    pend = ""
    while True:
        try:
            b = os.read(fd, 4096)
        except OSError:
            _restore()
            return
        if not b:
            _restore()
            return
        pend += b.decode("utf-8", "replace")
        keep = _partial_tail(pend)
        proc = pend[:len(pend) - keep] if keep else pend
        pend = pend[len(pend) - keep:] if keep else ""
        i = 0
        while i < len(proc):
            if paste is not None:
                j = proc.find(_PASTE_OFF, i)
                if j < 0:
                    paste += proc[i:]
                    break
                paste += proc[i:j]
                i = j + len(_PASTE_OFF)
                text = _join_paste(paste)
                paste = None
                if text:
                    if buf and not buf.endswith(" "):
                        buf += " "
                    buf += text
                    sys.stdout.write(text if len(text) <= 60
                                     else f"[pasted {len(text)} chars]")
                    sys.stdout.flush()
                continue
            if proc.startswith(_PASTE_ON, i):
                paste = ""
                i += len(_PASTE_ON)
                continue
            ch = proc[i]
            i += 1
            if ch in ("\r", "\n"):
                sys.stdout.write("\n")
                sys.stdout.flush()
                line = buf.strip()
                buf = ""
                if line:
                    q.put(line)
            elif ch in ("\x7f", "\x08"):     # backspace
                if buf:
                    buf = buf[:-1]
                    sys.stdout.write("\b \b")
                    sys.stdout.flush()
            elif ch >= " " or ch == "\t":    # printable: echo + collect
                buf += ch
                sys.stdout.write(ch)
                sys.stdout.flush()


async def speak_reply(brain: WarmBrain, mouth: Mouth, text: str,
                       image_b64: str | None = None):
    """First sentence ships alone (fast start); the rest go in
    2-sentence breaths — fuller chunks get livelier prosody (single
    short sentences come out flat). image_b64, when given, is a
    screenshot attached to this one turn (see screen.py)."""
    t0 = time.time()
    first = True
    batch: list[str] = []
    pending: list[str] = []          # directions waiting for their chunk

    def emit(raw: str):
        nonlocal first, batch, pending
        # STAGE DIRECTIONS: your agent may write <<anything>> inline. It is
        # lifted out here, never spoken, and published on the signal bus when
        # this chunk's audio starts (signals.direction). backtalk has no
        # opinion on what a direction means; something watching the bus does.
        #
        # This used to strip only the ANGLE BRACKETS, which left the tag body
        # in the sentence and the TTS read it aloud.
        found = _DIRECTION_TAG.findall(raw)
        if found:
            pending += [d.strip() for d in found if d.strip()]
        raw = _DIRECTION_TAG.sub(" ", raw)
        # TTS hygiene: backticks and markdown fences are never speakable.
        s = " ".join(raw.replace("`", "").split()).strip()
        if not s:
            return
        if first:
            log(f"[{NAME}] ({time.time()-t0:.1f}s to first) {s}"
                + (f"  <directions: {pending}>" if pending else ""))
            mouth.say_chunk(s, pending)
            pending = []
            first = False
        else:
            log(f"[{NAME}] {s}" + (f"  <directions: {pending}>" if pending else ""))
            batch.append(s)
            if len(batch) >= 2:
                mouth.say_chunk(" ".join(batch), pending)
                pending = []
                batch = []

    try:
        async for sentence in brain.ask_stream(text, image_b64=image_b64):
            emit(sentence)
        if batch:
            mouth.say_chunk(" ".join(batch), pending)
            pending = []
        if first:
            # Zero sentences yielded (brain error / empty turn): nothing
            # will ever dequeue, so nothing resets the bus — park it here.
            signals.static_stop()
            signals.set_state("idle")
    except asyncio.CancelledError:
        try:
            await brain.interrupt()
        except Exception:
            pass
        raise


async def amain():
    open_mic = "--open-mic" in sys.argv
    barge_in = "--barge-in" in sys.argv
    model = None
    if "--model" in sys.argv:
        try:
            model = sys.argv[sys.argv.index("--model") + 1]
        except IndexError:
            pass

    CFG_BOOT_MODE = CFG["permission_mode"]
    _AUTOAPPROVE["on"] = CFG_BOOT_MODE == "bypassPermissions"
    if open_mic:
        _MIC["mode"] = "open"
    elif CFG.get("mic_mode") in ("open", "wake"):
        _MIC["mode"] = CFG["mic_mode"]
    else:
        _MIC["mode"] = "ptt"
    # resume_last_session: reattach to the saved conversation, if any
    resume_id = None
    if CFG.get("resume_last_session"):
        try:
            from backtalk.brain import SESSION_FILE
            with open(SESSION_FILE) as f:
                resume_id = f.read().strip() or None
        except OSError:
            resume_id = None

    mouth = Mouth()
    ears = Ears()
    brain = WarmBrain(model=model,
                      can_use_tool=make_permission_gate(mouth),
                      resume_id=resume_id)

    mode = ("hands-free listening (the talk key still works)"
            if _MIC["mode"] == "open"
            else f"push-to-talk ({CFG['ptt_key']})")
    log(f"[backtalk] up — agent={NAME} dir={CFG['agent_dir']} "
        f"model={brain.model} mic={mode} "
        f"(say 'goodbye {NAME.lower()}' to hang up)")
    mouth.say(CFG["greeting"])

    loop = asyncio.get_event_loop()
    # Warm the engines while the greeting plays: the STT model load and
    # the brain's prompt-cache toll both hide behind the spoken line.
    loop.run_in_executor(None, warm_ears)
    # THE BRAIN CONNECT, guarded. This is the one startup step that
    # needs a signed-in Claude Code, internet, and available usage.
    # When it fails or hangs, the mouth still works, so SAY SO instead
    # of dying silently with the face stuck on idle (a real field
    # case: the greeting played, then nothing, and on Windows the
    # window closed before anyone could read the error).
    log("[backtalk] connecting the brain...")
    try:
        await asyncio.wait_for(brain.start(), 120)

        async def _warmup():
            async for _ in brain.ask_stream(
                    "Warmup ping - reply with the single word: ready"):
                pass
        await asyncio.wait_for(_warmup(), 180)
    except (Exception, asyncio.TimeoutError) as e:
        kind = ("timed out" if isinstance(e, asyncio.TimeoutError)
                else f"failed: {e!r}"[:220])
        log(f"[backtalk] BRAIN CONNECT {kind}")
        mouth.say("Bad news. The voice and the face are fine, but I "
                  "couldn't reach my brain, the Claude Code session. "
                  "Check this window for the error. The usual causes: "
                  "Claude Code isn't signed in, the internet is down, "
                  "or the plan is out of usage.")
        mouth.wait_done(timeout=30)
        raise SystemExit(1)
    log("[backtalk] brain warm")
    signals.set_current_model(brain.model)
    # a stale request from before this launch must never silently
    # reapply — a face reads .model_current as truth and should
    # re-request if it still wants a switch
    signals.clear_model_request()
    # the hidden warmup ping is plumbing, not conversation
    brain.session.update(turns=0, out_tokens=0, in_tokens=0, cost=0.0)
    # a configured effort level applies at launch (saved by the spoken
    # "set effort to X", or written by the person's agent on request)
    boot_effort = str(CFG.get("effort") or "").strip().lower()
    if boot_effort in _EFFORTS:
        await brain.command(f"/effort {boot_effort}")
        log(f"[backtalk] effort set to {boot_effort} (from config)")
    elif boot_effort:
        log(f"[backtalk] ignoring unknown effort {boot_effort!r} in config")

    speak_task: asyncio.Task | None = None
    typed_q: "queue.Queue[str]" = queue.Queue()
    threading.Thread(target=_typed_reader, args=(typed_q,), daemon=True).start()
    threading.Thread(target=_model_request_watcher, args=(typed_q,), daemon=True).start()
    threading.Thread(target=_screenshot_key_watcher, args=(typed_q,), daemon=True).start()
    typed_fut: asyncio.Future | None = None

    async def run_console(verb):
        """One voice-console verb. The current reply was already
        cancelled and awaited by handle(); the pipe gets drained here
        before the command goes out. A verb that blows up must never
        take the whole voice session down with it."""
        try:
            await _run_console_inner(verb)
        except Exception as e:
            log(f"[console] {verb} failed: {e}")
            mouth.say("That command hit an error. Check the log.")
            signals.set_state("idle")

    async def _run_console_inner(verb):
        _deny_pending()
        await brain.reset_turn()
        say_after = None
        if verb == "clear":
            resp = await brain.command("/clear")
            say_after = "Cleared. Fresh slate."
        elif verb == "compact":
            mouth.say("Compacting. One moment.")
            resp = await brain.command("/compact")
            say_after = "Compacted. Same conversation, smaller footprint."
        elif verb == "deep":
            mouth.say("Switching to the deep model. Heads up, replies "
                      "get slower. Say back to the fast model when "
                      "you're done.")
            resp = await brain.command(f"/model {CFG['deep_model']}")
            brain.model = CFG["deep_model"]
            signals.set_current_model(brain.model)
            # A manual switch is a deliberate override — auto-tiering
            # must never fight it mid-session by dropping back down on
            # its own (see _AUTOTIER / _apply_auto_tier below).
            _AUTOTIER["manual_until_fast"] = True
            say_after = "Deep model online, for this session only."
        elif verb == "fast":
            resp = await brain.command(f"/model {CFG['model']}")
            brain.model = CFG["model"]
            signals.set_current_model(brain.model)
            _AUTOTIER["manual_until_fast"] = False
            say_after = "Back on the fast model."
        elif verb.startswith("gui_model:"):
            model_id = verb.split(":", 1)[1]
            resp = await brain.command(f"/model {model_id}")
            brain.model = model_id
            signals.set_current_model(model_id)
            signals.clear_model_request()
            # a face-driven switch is just as deliberate as a spoken
            # one — auto-tiering must not override it either
            _AUTOTIER["manual_until_fast"] = model_id != CFG["model"]
            say_after = f"Switched to {model_id}, from the face."
        elif verb.startswith("effort:"):
            lvl = verb.split(":", 1)[1]
            resp = await brain.command(f"/effort {lvl}")
            saved = _write_config_key("effort", lvl)
            say_after = (f"Effort set to {lvl}, and saved as your "
                         "default." if saved else
                         f"Effort set to {lvl} for this session. The "
                         "config file couldn't be written, so it won't "
                         "stick past a restart.")
        elif verb == "usage":
            resp = ""
            mouth.say(_spoken_usage(brain.session,
                                    await brain.context_usage()))
        elif verb == "micopen":
            resp = ""
            if _MIC["mode"] == "open":
                mouth.say("Already in hands-free listening.")
            else:
                _MIC["mode"] = "open"
                _MIC["gen"] += 1
                _write_config_key("mic_mode", "open")
                log("[console] mic_mode -> open (hands-free listening)")
                mouth.say("Hands-free listening on. I'm always "
                          "listening now, so anything said in the room "
                          "can reach me. The talk key still works, and "
                          "holding it always gets you heard. Say push "
                          "to talk mode to bring the button back.")
        elif verb == "micptt":
            resp = ""
            if _MIC["mode"] == "ptt":
                mouth.say("Already on push to talk.")
            else:
                _MIC["mode"] = "ptt"
                _MIC["gen"] += 1
                _write_config_key("mic_mode", "ptt")
                log("[console] mic_mode -> ptt")
                key = str(CFG.get("ptt_key", "home")).replace("_", " ")
                mouth.say(f"Push to talk. Hold the {key} key and "
                          "talk; the mic stays closed otherwise.")
        elif verb == "micwake":
            resp = ""
            if _MIC["mode"] == "wake":
                mouth.say("Already waiting for hey Jarvis.")
            else:
                _MIC["mode"] = "wake"
                _MIC["gen"] += 1
                _write_config_key("mic_mode", "wake")
                log("[console] mic_mode -> wake")
                mouth.say("Wake word mode. I'm listening, but I'll "
                          "only act after you say hey Jarvis. The "
                          "talk key still works too.")
        elif verb == "noask":
            resp = ""
            _CONFIRM["verb"] = "noask"
            _CONFIRM["at"] = time.monotonic()
            mouth.say("Auto-approve means I act without asking "
                      "permission, and it becomes your saved default. "
                      "Say confirm to switch.")
        elif verb == "noask:confirmed":
            resp = ""
            saved = _write_config_key("permission_mode",
                                      "bypassPermissions")
            _AUTOAPPROVE["on"] = True
            log("[console] permission_mode -> bypassPermissions"
                + (" (saved)" if saved else " (session only)"))
            mouth.say(("Auto-approve on, and saved as your default. "
                       if saved else
                       "Auto-approve on for this session. The config "
                       "file couldn't be written, so it won't stick "
                       "past a restart. ")
                      + "Say start asking again any time to flip it "
                        "back.")
        elif verb == "ask":
            resp = ""
            saved = _write_config_key("permission_mode", "ask")
            _AUTOAPPROVE["on"] = False
            flipped = True
            if CFG_BOOT_MODE == "bypassPermissions":
                # a bypass-booted session never consults the gate, so
                # the SDK itself must flip (the safe direction is
                # allowed live). If that fails, saying "done" would be
                # a lie: the agent would keep acting silently.
                try:
                    await brain.set_permission_mode("ask")
                except Exception as e:
                    flipped = False
                    log(f"[console] live flip to ask FAILED: {e}")
            log("[console] permission_mode -> ask"
                + (" (saved)" if saved else " (session only)"))
            if flipped:
                mouth.say("Done. I'll ask out loud before real "
                          "actions"
                          + (", and that's saved as your default."
                             if saved else
                             ". The config file couldn't be written, "
                             "so tell me again after a restart."))
            else:
                mouth.say("I saved asking as your default, but this "
                          "session couldn't switch over. Restart the "
                          "voice line to get asking back.")
        elif verb.startswith("launch:"):
            resp = ""
            macro_name = verb.split(":", 1)[1]
            mouth.say("Starting your study session.")
            ok = launch.run(macro_name)
            if not ok:
                mouth.say("That launch shortcut isn't set up.")
        else:
            resp = ""
        if say_after:
            # the CLI answers slash commands with its own text
            # (confirmations, API errors); an error outranks our line
            low = (resp or "").lower()
            if resp and ("error" in low or "invalid" in low):
                mouth.say(resp[:160])
                log(f"[console] {verb} answered: {resp[:120]}")
            else:
                mouth.say(say_after)
        signals.set_state("idle")

    async def _ask_opus_once() -> bool:
        """The one-time-per-session "okay to use Opus for hard tasks?"
        ask. Spoken exactly once per session: a "yes" sets opus_ok for
        the rest of the session (no more asking); anything else — a
        no, a timeout, an interruption — declines for THIS turn only
        and asks again next time a hard task comes up, since Mike might
        answer differently once he's actually paying attention."""
        loop = asyncio.get_running_loop()
        mouth.say("This looks like a hard one — okay if I use Opus? "
                  "Yes or no?")
        fut = loop.create_future()
        _TIER_CONFIRM["fut"] = fut
        _TIER_CONFIRM["asked_at"] = time.monotonic()
        try:
            answer = await asyncio.wait_for(fut, TIER_CONFIRM_TIMEOUT_S)
        except asyncio.TimeoutError:
            mouth.say("No answer, so I'll stay on the regular model "
                      "for this one.")
            return False
        finally:
            _TIER_CONFIRM["fut"] = None
        approved = _norm_speech(answer) in _YES
        if approved:
            _AUTOTIER["opus_ok"] = True
            log("[tier]   Opus approved for the rest of this session")
        else:
            log(f"[tier]   Opus declined: {answer!r}")
        return approved

    async def _tiered_reply(text: str, target: str, tier: str):
        """The actual tiered turn, run as ONE task (assigned to
        speak_task by the caller) so the existing interrupt/cancel
        machinery in handle() treats it exactly like a normal reply —
        including a mid-reply cancellation, which must still drop the
        model back down (the finally below) rather than strand Jarvis
        on Opus because the user talked over it."""
        log(f"[tier]   {tier} -> {target} (was {brain.model})")
        await brain.command(f"/model {target}")
        brain.model = target
        signals.set_current_model(target)
        try:
            await speak_reply(brain, mouth, text)
        finally:
            await brain.command(f"/model {CFG['model']}")
            brain.model = CFG["model"]
            signals.set_current_model(brain.model)
            log(f"[tier]   back to {CFG['model']} after the {tier} turn")

    async def _tier_then_reply(text: str, tier: str, target: str):
        """Runs entirely INSIDE the speak_task, never awaited by
        handle() itself. This matters: the main input loop calls
        `await handle(text)` once per utterance, sequentially (see the
        while-True loop below), so if the Opus ask were awaited before
        speak_task existed, the loop would be stuck on that one
        `await handle(...)` call and could never capture the "yes"/"no"
        that's supposed to answer it — a deadlock. Wrapping the ask
        itself inside the task (the same trick make_permission_gate
        already relies on, since IT runs inside speak_task's own
        brain.ask_stream() call) keeps the main loop free to keep
        capturing utterances and feed them to handle(), which is what
        resolves _TIER_CONFIRM["fut"]."""
        if tier == "hard" and not _AUTOTIER["opus_ok"]:
            if not await _ask_opus_once():
                await speak_reply(brain, mouth, text)
                return
        await _tiered_reply(text, target, tier)

    async def _apply_auto_tier(text: str):
        """Silently pick Haiku/Sonnet/Opus for this ONE turn based on
        a cheap read of the request (see classify_difficulty above),
        then run it. Sets speak_task itself, same contract as every
        other dispatch path in handle(), so an interrupt mid-reply
        still works normally. Never touches the model if Mike has
        manually switched it (manual_until_fast) — a deliberate choice
        always wins over the heuristic."""
        nonlocal speak_task
        if not _AUTOTIER["manual_until_fast"]:
            tier = classify_difficulty(text)
            target = _TIER_MODEL.get(tier) or (
                CFG["deep_model"] if tier == "hard" else CFG["model"])
            if target != brain.model:
                speak_task = asyncio.create_task(
                    _tier_then_reply(text, tier, target))
                return
        speak_task = asyncio.create_task(speak_reply(brain, mouth, text))

    async def handle_screen_share(question: str):
        """"Look at my screen[, <question>]": grab the whole virtual
        desktop and send it as this turn's image, same streaming/speak
        path as a normal question. The capture itself (~0.5-1s) blocks
        before any reply starts, so a short spoken heads-up covers that
        gap — otherwise it just looks like the mic didn't hear anything."""
        nonlocal speak_task
        _deny_pending()
        await brain.reset_turn()
        mouth.say("Let me take a look.")
        signals.set_state("thinking")
        signals.static_start()
        result = await asyncio.get_event_loop().run_in_executor(None, screen.capture)
        if result is None:
            mouth.say("Couldn't grab a screenshot just now — check the log.")
            signals.set_state("idle")
            return
        b64, path = result
        screen.prune_old_screenshots()
        log(f"[screen] captured {path}")
        prompt = question or ("Here's my screen right now. What am I looking at, "
                              "and is there anything I should know or fix?")
        speak_task = asyncio.create_task(speak_reply(brain, mouth, prompt, image_b64=b64))

    async def handle(text: str, spoke_from: float | None = None) -> bool:
        """Process one utterance; returns False on quit. spoke_from is
        when the utterance STARTED (the PTT press), so an answer can be
        told apart from speech that began before the ask even existed."""
        nonlocal speak_task
        # A face's model-switch request, injected via the same typed_q
        # a person's typing uses (see _model_request_watcher below) but
        # tagged so it never logs as spoken/typed input or gets tested
        # against quit phrases — it isn't something anyone said.
        if text.startswith(_GUI_MODEL_PREFIX):
            model_id = text[len(_GUI_MODEL_PREFIX):]
            log(f"[console] model switch requested from face: {model_id}")
            await run_console(f"gui_model:{model_id}")
            return True
        # The screenshot hotkey (screen.py's own thread, see
        # _screenshot_key_watcher) — a key press, not speech, so same
        # treatment as the model-switch sentinel above.
        if text == _SCREENSHOT_KEY_SENTINEL:
            log("[screen] hotkey pressed")
            if speak_task and not speak_task.done():
                speak_task.cancel()
                mouth.shut_up()
            await handle_screen_share("")
            return True
        log(f"[you]    {text}")
        # A pending spoken permission ask owns the next utterance IF
        # that utterance started after the ask was posed. Speech that
        # began earlier is the user interrupting the turn, not
        # answering a question they never heard: the ask resolves as a
        # silent deny and the utterance falls through as a normal
        # interrupt. Quit wins either way, but only as an EXACT phrase
        # here ("No! Don't hang up, skip it" must stay a deny reason,
        # not kill the session).
        if _PERM["fut"] is not None and not _PERM["fut"].done():
            started_after = (spoke_from is None
                             or spoke_from >= _PERM["asked_at"])
            if _norm_speech(text) in {_norm_speech(q)
                                      for q in QUIT_PHRASES + SHUTDOWN_PHRASES}:
                _PERM["fut"].set_result("no")
                # falls through to the quit body below
            elif started_after:
                _PERM["fut"].set_result(text)
                return True
            else:
                _deny_pending()
        # A pending "okay to use Opus?" ask owns the next utterance the
        # same way, minus the interrupt-vs-answer timing nuance above:
        # this ask only ever fires from the top of handle() itself
        # (never mid-turn from inside the SDK), so there's no earlier-
        # speech-vs-answer ambiguity to resolve. Quit/shutdown still
        # wins outright — "goodbye jarvis" must hang up even mid-ask,
        # not get swallowed as a declined answer.
        if _TIER_CONFIRM["fut"] is not None and not _TIER_CONFIRM["fut"].done():
            if _norm_speech(text) in {_norm_speech(q)
                                      for q in QUIT_PHRASES + SHUTDOWN_PHRASES}:
                _TIER_CONFIRM["fut"].set_result("no")
                # falls through to the quit body below
            else:
                _TIER_CONFIRM["fut"].set_result(text)
                return True
        # A pending auto-approve confirm owns it too, for two minutes;
        # after that it expires and speech flows normally again.
        verb = None
        if _CONFIRM["verb"]:
            pend, _CONFIRM["verb"] = _CONFIRM["verb"], None
            expired = time.monotonic() - _CONFIRM["at"] > 120
            if not expired and _norm_speech(text) in (
                    "confirm", "confirmed", "yes confirm",
                    "yes confirmed"):
                verb = pend + ":confirmed"
            elif not expired and not any(
                    q in text.lower() for q in QUIT_PHRASES + SHUTDOWN_PHRASES):
                mouth.say("Staying as we are.")
                return True
        shutting_down = any(q in text.lower() for q in SHUTDOWN_PHRASES)
        if shutting_down or any(q in text.lower() for q in QUIT_PHRASES):
            if speak_task and not speak_task.done():
                speak_task.cancel()
            mouth.shut_up()
            if shutting_down:
                mouth.say("Shutting everything down.")
                mouth.wait_done(timeout=10)
                launch.kill_face()
            else:
                mouth.say(CFG["signoff"])
            mouth.wait_done(timeout=15)
            return False
        if speak_task and not speak_task.done():
            log("[turn] interrupted mid-reply by new input")
            _deny_pending()          # an ask never outlives its turn
            speak_task.cancel()
            mouth.shut_up()
        if speak_task:
            # Let the cancellation fully land (its brain.interrupt()
            # included) BEFORE anything else touches the brain —
            # otherwise the dead turn's stop signal can race in after
            # the new query and kill the new answer (half of the
            # off-by-one bug; see brain.reset_turn for the other half).
            try:
                await speak_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
            speak_task = None
        verb = verb or console_match(text)
        if verb:
            await run_console(verb)
            return True
        screen_question = screen_share_match(text)
        if screen_question is not None:
            await handle_screen_share(screen_question)
            return True
        signals.set_state("thinking")
        signals.static_start()
        # Clean the pipe: drain the interrupted turn's leftovers so the
        # new question can't pair with a stale ResultMessage. A gate
        # that fired in the meantime resolves first, or the drain would
        # wait on a ResultMessage the CLI is withholding for an answer.
        _deny_pending()
        await brain.reset_turn()
        await _apply_auto_tier(text)
        return True

    try:
        # ONE loop, two mic modes, switchable live (_MIC). The talk key
        # is constructed and honored in BOTH modes: in hands-free
        # listening it is the interrupt and the guaranteed way to be
        # heard over room noise. The open mic joins the wait-set only
        # in "open" mode; a mode switch bumps _MIC["gen"], the abort
        # callable closes the in-flight open mic promptly, and any
        # capture born under an old gen is discarded unprocessed.
        ptt = PTTListener(CFG["ptt_key"])
        press_fut: asyncio.Future | None = None
        mic_fut: asyncio.Future | None = None
        mic_gen_seen = _MIC["gen"]
        # The open mic yields while the BUTTON records (or the double
        # capture would turn one held utterance into two turns), and,
        # without barge-in, while the mouth speaks.
        mic_gate = (lambda: _MIC["btn"]
                    or (not barge_in and mouth.speaking))

        def _wake_then_listen(g: int) -> str | None:
            """Blocks for the wake word, then does ONE normal capture
            (same as "open" mode) and returns its transcript. Runs on
            the executor thread, same as ears.listen_once — g is the
            _MIC generation this call was born under, checked at each
            stage so a live mode switch abandons it promptly instead of
            capturing a stray utterance under the old mode.

            require_full_phrase=False: every call here is, by
            definition, already a running voice session (cold start —
            launching from nothing running — is wake_listener.py's
            separate process, which always wants the full phrase and
            never touches this function). So the bare word "Jarvis"
            alone is accepted here as well as "hey Jarvis", per the
            wake-word problem note's decision."""
            heard = wake.wait_for_wake(
                gate=mic_gate, abort=lambda: _MIC["gen"] != g,
                require_full_phrase=False)
            if not heard or _MIC["gen"] != g:
                return None
            signals.set_state("listening")
            try:
                return ears.listen_once(
                    gate=mic_gate, abort=lambda: _MIC["gen"] != g)
            finally:
                if _MIC["gen"] == g:
                    signals.set_state("idle")

        mic_fails = 0
        while True:
            if _MIC["gen"] != mic_gen_seen:
                mic_gen_seen = _MIC["gen"]
                # consume futures that completed under the old mode so
                # a stale press or capture can't fire after a switch
                if press_fut is not None and press_fut.done():
                    press_fut.result(); press_fut = None
                if mic_fut is not None and mic_fut.done():
                    mic_fut.result(); mic_fut = None
            if typed_fut is None:
                typed_fut = loop.run_in_executor(None, typed_q.get)
            if press_fut is None:
                press_fut = loop.run_in_executor(None, ptt.wait_press)
            waiters = {press_fut, typed_fut}
            if _MIC["mode"] == "open":
                if mic_fut is None:
                    g = _MIC["gen"]
                    mic_fut = loop.run_in_executor(
                        None, lambda g=g: (g, ears.listen_once(
                            gate=mic_gate,
                            abort=lambda: _MIC["gen"] != g)))
                waiters.add(mic_fut)
            elif _MIC["mode"] == "wake":
                if mic_fut is None:
                    g = _MIC["gen"]
                    mic_fut = loop.run_in_executor(
                        None, lambda g=g: (g, _wake_then_listen(g)))
                waiters.add(mic_fut)
            done, _ = await asyncio.wait(
                waiters, return_when=asyncio.FIRST_COMPLETED)
            if typed_fut in done:
                text = typed_fut.result(); typed_fut = None
                if text and not await handle(text):
                    return
                continue
            if mic_fut is not None and mic_fut in done:
                try:
                    g, text = mic_fut.result()
                except Exception as e:
                    mic_fut = None
                    mic_fails += 1
                    log(f"[ears] open mic failed ({mic_fails}): {e!r}")
                    if mic_fails >= 3:
                        _MIC["mode"] = "ptt"
                        _MIC["gen"] += 1
                        mic_fails = 0
                        mouth.say("The open microphone keeps failing, "
                                  "so I'm switching to push to talk. "
                                  "Hold the key to reach me, and "
                                  "check this window for the error.")
                    continue
                mic_fut = None
                if g != _MIC["gen"]:
                    continue             # captured before a switch
                if text and not await handle(text):
                    return
                continue
            if press_fut in done:
                press_fut.result(); press_fut = None
                press_t = time.monotonic()
                perm_wait = (_PERM["fut"] is not None
                             and not _PERM["fut"].done())
                if speak_task and not speak_task.done() and not perm_wait:
                    log("[turn] interrupted mid-reply — key pressed")
                    speak_task.cancel()          # the button = interrupt
                # During a permission ask the TURN stays alive; the
                # press only silences playback and records the answer.
                mouth.shut_up()
                signals.static_stop()            # button kills the static too
                signals.set_state("listening")
                mouth.ducker.speech_start()      # duck NOW, while you talk
                print("[ptt] recording (release to send)...", flush=True)
                _MIC["btn"] = True               # open mic yields to the button
                try:
                    text = await loop.run_in_executor(
                        None, lambda: record_held(ptt.is_held))
                except Exception as e:
                    log(f"[ears] record/transcribe failed: {e!r}")
                    mouth.say("My ears hit an error. Check this "
                              "window for the details.")
                    text = None
                finally:
                    _MIC["btn"] = False
                mouth.ducker.speech_end(0.2)     # snap back fast on release
                if not text:
                    log("[ptt] (tap or empty — ignored)")
                    signals.set_state("idle")
                    continue
                if not await handle(text, spoke_from=press_t):
                    return
    except KeyboardInterrupt:
        pass
    finally:
        _MIC["gen"] += 1     # abort any live open-mic capture promptly
        if speak_task and not speak_task.done():
            speak_task.cancel()
        mouth.shutdown()  # restores the music on Ctrl-C / crash paths too
        signals.static_stop()
        signals.set_state("idle")
        await brain.stop()
        log("[backtalk] hung up")
        # THE REAL EXIT, not asyncio.run()'s normal return: press_fut
        # (ptt.py's PTTListener.wait_press, a bare threading.Event.wait()
        # with no timeout) is ALWAYS outstanding on the default executor
        # by this point, and it has no abort — it only unblocks on the
        # NEXT physical key press, which may never come. mic_fut (open
        # mic / wake mode) has the same shape whenever the abort hasn't
        # been noticed yet. asyncio.run() calls
        # loop.shutdown_default_executor(), which BLOCKS until every
        # submitted executor thread finishes — so with either of those
        # still alive, the interpreter never actually exits: this log
        # line prints, and the process just sits there (caught live —
        # "close jarvis" said goodbye and the process kept running
        # until something happened to unblock the key listener).
        # Every resource that matters is already closed above
        # (mouth/ducker, brain's SDK connection, the signal bus); a
        # blocked stdlib wait on a daemon-executor thread is not one of
        # them, so ending the process directly here is the correct fix,
        # not a workaround. The lock file is removed HERE, not in
        # main()'s own finally, because os._exit() below never lets
        # control return there.
        try:
            os.remove(_LOCK_FILE)
        except OSError:
            pass
        os._exit(0)


_MODEL_REQUEST_POLL_SECONDS = 1.0


# Sentinel a screenshot-hotkey press is tagged with, same idea as
# _GUI_MODEL_PREFIX: handle() must fire the real screen-share turn
# without logging a fake "[you] ..." line or matching it against
# console/quit phrases.
_SCREENSHOT_KEY_SENTINEL = "\x00screenshot_key\x00"


def _screenshot_key_watcher(q: "queue.Queue[str]"):
    """Daemon thread: a SECOND, independent PTTListener on
    screenshot_key (config.py). One tap (press+release, not held)
    pushes the sentinel into the same queue typed lines use. Disabled
    entirely when screenshot_key is empty in config, so nothing runs
    if the person never wants the hotkey."""
    key = str(CFG.get("screenshot_key") or "").strip()
    if not key:
        return
    try:
        listener = PTTListener(key)
    except Exception as e:
        log(f"[screen] could not start screenshot hotkey ({key!r}): {e}")
        return
    log(f"[screen] hotkey ready: {key}")
    while True:
        listener.wait_press()
        q.put(_SCREENSHOT_KEY_SENTINEL)


def _model_request_watcher(q: "queue.Queue[str]"):
    """Daemon thread: watches .model_request (see signals.py) and, the
    moment a face writes a model id there, pushes it into the SAME
    queue a person's typed line goes through — tagged with
    _GUI_MODEL_PREFIX so handle() routes it to the console instead of
    treating it as something someone said. Polling, not a filesystem
    watch: this file changes rarely (a person clicking a picker), so a
    once-a-second check costs nothing and needs no extra dependency."""
    seen = None
    while True:
        time.sleep(_MODEL_REQUEST_POLL_SECONDS)
        requested = signals.read_model_request()
        if requested and requested != seen:
            seen = requested
            q.put(_GUI_MODEL_PREFIX + requested)
        elif not requested:
            seen = None


def main():
    try:
        with open(_LOCK_FILE, "w") as f:
            f.write(str(os.getpid()))
    except OSError:
        pass
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        print("\n[backtalk] interrupted — hanging up", flush=True)
    # No finally/lock-cleanup here: amain()'s own finally calls
    # os._exit() before control ever returns to this frame — see its
    # comment for why a normal return can't be trusted to end the
    # process. The lock file is removed there, right before that exit.


if __name__ == "__main__":
    main()
