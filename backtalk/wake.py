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
"""The wake word — "Hey Jarvis" gates hands-free listening.

Local, free, no API key: openWakeWord's bundled "hey jarvis" ONNX
model, same ethos as ears.py's faster-whisper. Without this, hands-free
listening (mic_mode "open") sends every VAD-detected utterance straight
to the agent — a video, music, or someone else talking in the room can
trigger a real action. With mic_mode "wake" instead, the mic samples
continuously but stays inert until the wake word fires; only then does
it fall into a normal one-shot capture (ears.listen_once) for the
actual request, then goes back to waiting for the wake word.

wait_for_wake() owns ONLY the detection loop. The capture that follows
a detection is the existing Ears/listen_once path — this module adds
no new transcription or endpointing logic of its own.

BARE "JARVIS" AS A MID-SESSION RE-TRIGGER: openWakeWord ships no
pretrained model for the bare word alone (only fixed phrases like
"hey_jarvis" — training a custom one needs its own synthetic-data
pipeline, not a quick local add). So the bare word is caught a
different way: a short VAD-gated speech burst, transcribed by the
same faster-whisper model ears.py already uses, checked for a leading
"jarvis". This only runs from wait_for_wake(require_full_phrase=False)
— i.e. only for the mid-session re-trigger in main.py's "wake" mic
loop, never for cold start (wake_listener.py always wants the full
phrase and never passes this flag). Costs a transcription pass per
speech burst instead of per 80ms frame, which is why it stays off by
default rather than being the only path."""
import numpy as np
import sounddevice as sd
import webrtcvad

from backtalk.vlog import log

RATE = 16000
# openWakeWord's models expect 80ms frames (1280 samples @ 16kHz).
FRAME_LEN = 1280
THRESHOLD = 0.5

# The bare-word fallback's own VAD endpointing, independent of
# ears.py's (different frame size — this loop reads openWakeWord's
# native 80ms blocks; webrtcvad only accepts 10/20/30ms frames, so
# each 80ms block is split into four 20ms sub-frames for the VAD call,
# but speech/silence duration is still tracked in whole 80ms blocks —
# the same unit the outer loop already reads in).
_VAD_SUBFRAME = RATE * 20 // 1000        # 320 samples = 20ms
_OPEN_BLOCKS = 2                         # ~160ms speech opens an utterance
_CLOSE_BLOCKS = 6                        # ~480ms trailing quiet closes it
_MAX_UTTER_BLOCKS = 375                  # ~30s hard cap

_model = None


def _load():
    global _model
    if _model is None:
        import openwakeword
        from openwakeword.model import Model
        openwakeword.utils.download_models(["hey_jarvis"])
        log("[wake] loading hey_jarvis...")
        _model = Model(wakeword_models=["hey_jarvis"],
                       inference_framework="onnx")
        log("[wake] model ready")
    return _model


def _bare_name_heard(pcm: np.ndarray, name: str) -> bool:
    """True if a transcribed speech burst opens with the bare wake
    name (case-insensitive). Deliberately does NOT also match "hey
    <name>" — that phrase is openWakeWord's job in the same loop,
    and double-counting it here would just cost an extra
    transcription for no new coverage."""
    from backtalk.ears import transcribe
    text = transcribe(pcm).strip().lower()
    if not text:
        return False
    first_word = text.split(None, 1)[0].strip(" ,.!?")
    return first_word == name.lower()


def wait_for_wake(gate=None, abort=None, require_full_phrase=True,
                  bare_name: str = "jarvis") -> bool:
    """Blocks until the wake phrase is heard. Returns True on
    detection, False if `abort()` signaled a stop (a live mic-mode
    switch away from "wake"). `gate`, same contract as
    Ears.listen_once: while it returns True (the mouth is speaking and
    barge-in is off), frames are fed as silence instead of real audio,
    so the agent's own voice can never trigger itself.

    require_full_phrase=True (the default, and always true for cold
    start via wake_listener.py): only openWakeWord's "hey jarvis"
    fires. Set False to ALSO accept the bare name once a session is
    already awake (main.py's mid-session "wake" mic loop) — every
    VAD-detected speech burst is transcribed and checked for a leading
    bare_name, running alongside the openWakeWord model in the same
    stream so only one mic is ever open."""
    model = _load()
    model.reset()
    silence = np.zeros(FRAME_LEN, dtype=np.int16)
    vad = webrtcvad.Vad(2) if not require_full_phrase else None
    burst: list[np.ndarray] = []
    speech_run = 0
    silence_run = 0
    in_utterance = False
    with sd.InputStream(samplerate=RATE, channels=1, dtype="int16",
                        blocksize=FRAME_LEN) as stream:
        while True:
            if abort and abort():
                return False
            block, _ = stream.read(FRAME_LEN)
            muted = bool(gate and gate())
            frame = silence if muted else block[:, 0].copy()
            scores = model.predict(frame)
            if scores.get("hey_jarvis", 0.0) >= THRESHOLD:
                log("[wake] \"hey jarvis\" detected")
                model.reset()
                return True
            if not require_full_phrase and not muted:
                # openWakeWord already consumed `frame` above; VAD gets
                # the same audio, sliced into the sub-frame size it
                # expects, purely to decide whether to bother
                # transcribing at all — the actual wake check is the
                # transcript, not the VAD result.
                is_speech = any(
                    vad.is_speech(
                        frame[i:i + _VAD_SUBFRAME].tobytes(), RATE)
                    for i in range(0, FRAME_LEN, _VAD_SUBFRAME))
                if not in_utterance:
                    if is_speech:
                        speech_run += 1
                        burst.append(frame)
                    else:
                        speech_run = 0
                        burst = []
                    if speech_run >= _OPEN_BLOCKS:
                        in_utterance = True
                        silence_run = 0
                else:
                    burst.append(frame)
                    silence_run = 0 if is_speech else silence_run + 1
                    if silence_run >= _CLOSE_BLOCKS \
                            or len(burst) >= _MAX_UTTER_BLOCKS:
                        pcm = np.concatenate(burst)
                        burst, in_utterance = [], False
                        speech_run = silence_run = 0
                        if _bare_name_heard(pcm, bare_name):
                            log(f"[wake] bare \"{bare_name}\" detected")
                            model.reset()
                            return True
            elif muted:
                # speakers are talking and this frame was silenced for
                # openWakeWord above — an in-progress bare-word burst
                # must not straddle that gap into nonsense audio.
                burst, in_utterance, speech_run, silence_run = \
                    [], False, 0, 0
