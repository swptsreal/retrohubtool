# -*- coding: utf-8 -*-
"""Playback backend abstraction for YouTube.

The UI only needs two things from a backend: what it can do (`capabilities`)
and the handoff script that starts playback. The blocking playback loop itself
runs inside `rh.yt_player` (a separate process, because the app hands control
over to the emulator/player and exits). Keeping the interface here means an
in-app player can replace RetroArch later without touching the UI or the
session logic.
"""

import os

from . import playback, state
from .paths import APP_DIR, SDCARD_PATH, YT_SESSION_FILE


class PlayerBackend:
    name = "base"
    capabilities = {
        "seek_absolute": False,   # can resume to an exact position
        "audio_only": False,      # can play audio with the screen off
        "quality_select": False,  # can pick a stream resolution
        "speed": False,           # can change playback speed
        "inline_ui": False,       # can draw UI while playing
    }

    def play(self, video, start=0.0, audio_only=False, duration=0.0, session=None):
        """Play one video and block until it ends or the user exits.

        Returns True when the player exited normally, False on failure.
        """
        raise NotImplementedError

    def stop(self):
        pass


class RetroArchBackend(PlayerBackend):
    """Playback via RetroArch + the libretro FFMPEG core (current engine)."""

    name = "retroarch"
    capabilities = {
        # The ffmpeg core has no absolute seek from the command line, so resume
        # is stored and shown but not applied yet.
        "seek_absolute": False,
        # Audio-only plays an audio stream with video_driver=null and turns the
        # backlight off (see rh.backlight); falls back gracefully if unsupported.
        "audio_only": True,
        # Only progressive 360p (format 18) plays reliably through the core.
        "quality_select": False,
        "speed": True,            # fast-forward toggle
        "inline_ui": False,
    }

    def play(self, video, start=0.0, audio_only=False, duration=0.0, session=None):
        from .yt_player import play_video
        return play_video(video.get("id"), audio_only=audio_only)


class InAppBackend(PlayerBackend):
    """Playback inside the app process (ffmpeg + SDL2, see rh.inapp_player).

    The blocking loop lives in the PlayerScreen, not here: this backend only
    advertises what it can do so the UI knows to open the in-app player.
    """

    name = "inapp"
    capabilities = {
        "seek_absolute": True,
        "audio_only": True,
        "quality_select": True,
        "speed": True,
        "inline_ui": True,
    }

    def play(self, video, start=0.0, audio_only=False, duration=0.0, session=None):
        raise NotImplementedError("in-app playback runs in rh.screens.player")


def inapp_available() -> bool:
    try:
        from . import inapp_player
        if not inapp_player.available():
            return False
        h264 = inapp_player.h264_status()
        http = inapp_player.http_protocol_status()
        if h264 is None:
            inapp_player.precompute_decoder_check()
        if http is None:
            inapp_player.precompute_protocol_check()
        if h264 is None or http is None:
            # Probe off the main thread; optimistically allow in-app for now.
            return True
        return bool(h264 and http)
    except Exception:
        return False


def get_backend(name=None) -> PlayerBackend:
    name = (name or getattr(state, "player_backend", "auto") or "auto").lower()
    if name == "retroarch":
        return RetroArchBackend()
    if name == "inapp":
        return InAppBackend()
    return InAppBackend() if inapp_available() else RetroArchBackend()


BACKEND = RetroArchBackend()


# ---------------------------------------------------------------------------
# Handoff script
# ---------------------------------------------------------------------------
def build_session_command(session_path=None) -> str:
    """Shell script that runs the yt_player session runner.

    Mirrors rh.yt.build_play_command: locates a Python 3 interpreter, cd's to
    the app dir and runs the module. Written to /tmp/launch_game.sh by the UI.
    """
    session_path = session_path or YT_SESSION_FILE
    # NB: use .replace(), not str.format(), because the shell uses ${VAR}.
    return """#!/bin/sh
SDCARD_PATH="${SDCARD_PATH:-/mnt/SDCARD}"
APP_DIR="$SDCARD_PATH/Apps/RetroHub"
LOG_FILE="$SDCARD_PATH/RetroHub-yt.log"

echo "=== YouTube Session ($(date 2>/dev/null)) ===" >> "$LOG_FILE"

# System/lib carries OpenSSL 1.1.1 and SDL2 for the player.
export LD_LIBRARY_PATH="/mnt/SDCARD/System/lib:/usr/trimui/lib:$LD_LIBRARY_PATH"

PY3="python3"
if [ -f "$APP_DIR/python/bin/python3" ]; then
    PY3="$APP_DIR/python/bin/python3"
elif [ -f "$SDCARD_PATH/System/bin/python3" ]; then
    PY3="$SDCARD_PATH/System/bin/python3"
elif [ -f "$SDCARD_PATH/.retrohub/python/bin/python3" ]; then
    PY3="$SDCARD_PATH/.retrohub/python/bin/python3"
elif which python3 >/dev/null 2>&1; then
    PY3="python3"
fi

cd "$APP_DIR"
"$PY3" -m rh.yt_player --session "__SESSION__" >> "$LOG_FILE" 2>&1
EXIT_CODE=$?

echo "Session Exit Code: $EXIT_CODE" >> "$LOG_FILE"
""".replace("__SESSION__", session_path)


def launch_session(engine, session) -> bool:
    """Persist a session and hand control to the yt_player runner.

    Writes the handoff script and tells the app to exit; launch.sh then runs the
    runner, which plays the whole queue before the app comes back.
    """
    try:
        playback.save_session(session)
        with open("/tmp/launch_game.sh", "w", encoding="utf-8") as f:
            f.write(build_session_command())
        os.chmod("/tmp/launch_game.sh", 0o755)
        with open("/tmp/rh_last_screen.txt", "w", encoding="utf-8") as f:
            f.write("youtube")
        engine.running = False
        return True
    except Exception as e:
        try:
            engine.toast(f"Lỗi mở video: {e}")
        except Exception:
            pass
        return False
