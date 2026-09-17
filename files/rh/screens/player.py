# -*- coding: utf-8 -*-
"""In-app YouTube player screen.

Owns the in-app playback engine (rh.inapp_player) and draws a YouTube-like
overlay (title, progress bar, controls) while the video plays. Drives the
PlaybackSession in-process, so auto-next, resume and progress saving all happen
without leaving the app. Falls back to the RetroArch handoff if the engine
cannot start.
"""

import ctypes
import threading
import time

try:
    import sdl2
except Exception:  # pragma: no cover - only on non-device hosts
    sdl2 = None

from .. import playback, state, yt
from ..i18n import tr
from .base import BaseScreen

QUALITY_CYCLE = ("360", "480", "720")
WATCHED_STEP = 30.0   # seconds of playback between watch-history disk writes
SEEK_SETTLE = 0.45    # wait this long after the last seek key before restarting


class PlayerScreen(BaseScreen):
    def __init__(self, engine=None):
        super().__init__(engine)
        self.session = None
        self.player = None
        self.quality = "360"
        self.current = None
        self.duration = 0.0
        self.title = ""
        self.channel = ""
        self.status = ""
        self.resolving = False
        self._overlay_until = 0.0
        self._last_progress = 0.0
        self._pending_start = None
        self._leaving = False
        self._fallback_done = False
        self._rc_logged = False
        self._seek_target = None
        self._seek_deadline = 0.0
        self._last_watched_pos = -1e9
        self._cached_url_used = False
        self._pending_res = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def on_enter(self, params=None):
        params = params or {}
        self.session = params.get("session") or playback.load_session()
        self.quality = str(params.get("quality") or getattr(state, "video_quality", "360"))
        self._leaving = False
        self._fallback_done = False
        self._pending_start = None
        self._overlay_until = time.time() + 4
        if not self.session or not self.session.queue:
            self.engine.pop_screen()
            return
        self._play_current()

    def on_exit(self):
        self._save_progress(force=True)
        self._seek_target = None
        self._pending_res = None
        self._stop_player()

    def get_header_title(self):
        return "YouTube"

    def get_footer_actions(self):
        return [
            ("A", tr("yt_pl_pause"), (0, 230, 150), (220, 225, 235), True),
            ("Y", tr("yt_pl_next"), (0, 210, 255), (220, 225, 235), True),
            ("B", tr("yt_pl_exit"), (255, 75, 75), (220, 225, 235), False),
        ]

    # ------------------------------------------------------------------
    # Playback
    # ------------------------------------------------------------------
    def _play_current(self):
        v = self.session.current()
        if not v:
            self._finish_session()
            return
        self.current = v
        self.title = v.get("disp_title") or v.get("title", "")
        self.channel = v.get("channel", "")
        self.duration = float(playback.duration_seconds(v.get("duration", "")))
        if self._pending_start is not None:
            start = self._pending_start
            self._pending_start = None
        else:
            start = playback.resume_position(v.get("id"), self.duration)
        self._seek_target = None
        self._last_watched_pos = -1e9
        self._cached_url_used = False
        self._pending_res = None
        if start > 1.0:
            # Resuming: a pipe cannot be seeked, so the decoders re-read the
            # stream from the start and drop everything before the target.
            self.status = "%s %s" % (tr("yt_pl_resume"),
                                     playback.format_seconds(start))
        else:
            self.status = tr("yt_pl_buffering")
        self.resolving = True
        self._stop_player()
        threading.Thread(target=self._bg_resolve, args=(v, start), daemon=True).start()

    def _bg_resolve(self, v, start):
        res = None
        # A cached URL is not re-validated by yt-dlp, so remember whether the
        # stream about to play came from the cache (it may have expired).
        cached = bool(yt.get_cached_streams(v.get("id"), self.quality))
        try:
            res = yt.resolve_streams(v.get("id"), self.quality)
        except Exception as e:
            print(f"[player] resolve error: {e}")
        # This runs on a worker thread. SDL textures, the audio device and the
        # renderer may only be touched from the thread that owns the renderer:
        # a texture created off that thread is never filled by GL and the video
        # shows up as a black screen (with audio still playing). So only hand
        # the result over - update() starts the player on the main thread.
        self._pending_res = (res, start, cached)

    def _start_player(self, res, start):
        from ..inapp_player import InAppPlayer
        player = InAppPlayer(self.engine.renderer)
        try:
            ok = player.start(res.get("video_url"), res.get("audio_url"), start_pos=start,
                              audio_only=self.session.audio_only, quality=self.quality,
                              duration=self.duration, video_id=(self.current or {}).get("id"))
        except Exception as e:
            # Never let a media-path exception escape into the engine loop: it
            # would kill the whole app instead of just falling back.
            try:
                from ..yt_player import log as _ytlog
                _ytlog("in-app start failed: %r" % e)
            except Exception:
                pass
            ok = False
        if not ok:
            self._fallback_retroarch()
            return
        self.player = player
        self.resolving = False
        self.status = ""
        self._last_progress = time.time()

    def _stop_player(self):
        if self.player:
            try:
                self.player.stop()
            except Exception:
                pass
            self.player = None

    def _player_error(self, exc):
        """Log an in-app player failure and hand the session to RetroArch."""
        try:
            from ..yt_player import log as _ytlog
            _ytlog("in-app player error: %r" % exc)
        except Exception:
            pass
        self._stop_player()
        if not self._fallback_done:
            self._fallback_retroarch(str(exc))
        else:
            self._finish_session()

    def _fallback_retroarch(self, reason=""):
        """Hand the session to the RetroArch runner (the proven path)."""
        if not reason and self.player:
            reason = getattr(self.player, "error", "") or ""
        if reason:
            try:
                from ..yt_player import log as _ytlog
                _ytlog("In-app playback failed, falling back to RetroArch: %s" % reason)
            except Exception:
                pass
        if self._fallback_done:
            if reason:
                self.engine.toast(reason)
            self._finish_session()
            return
        self._fallback_done = True
        from ..player import launch_session
        self._stop_player()
        self._leaving = True
        if not launch_session(self.engine, self.session):
            self._finish_session()

    def _advance_or_finish(self):
        self._save_progress(force=True)
        self._stop_player()
        if self.session and self.session.advance():
            playback.save_session(self.session)
            self._play_current()
        else:
            self._finish_session()

    def _finish_session(self):
        self._save_progress(force=True)
        if self.session:
            self.session.state = "idle"
            playback.save_session(self.session)
        self._stop_player()
        self._leaving = True
        if self.engine.current_screen_name == "player":
            self.engine.pop_screen()

    def _save_progress(self, force=False):
        if not self.player or not self.current:
            return
        pos = self.player.get_position()
        dur = self.duration or self.player.get_duration()
        vid = self.current.get("id")
        if dur and (dur - pos) < playback.RESUME_TAIL:
            playback.clear_progress(vid)
        elif pos >= playback.RESUME_MIN_POS:
            playback.set_progress(vid, pos, dur, self.current.get("title", ""), self.channel)
        # Watch history is a disk write; refresh it every WATCHED_STEP seconds
        # (or when leaving the video) instead of on every progress tick.
        if force or abs(pos - self._last_watched_pos) >= WATCHED_STEP:
            self._last_watched_pos = pos
            playback.add_watched(self.current, pos)

    def _queue_seek(self, delta):
        """Accumulate a seek and apply it once the D-pad stops repeating.

        Seeking re-spawns both ffmpeg decoders, so applying every autorepeat
        event (~13/s while held) would spawn a decoder storm.
        """
        if not self.player:
            return
        base = self._seek_target
        if base is None:
            base = self.player.get_position()
        target = max(0.0, base + delta)
        if self.duration > 0:
            target = min(target, max(0.0, self.duration - 1.0))
        self._seek_target = target
        self._seek_deadline = time.time() + SEEK_SETTLE
        self.status = "%s %s" % (tr("yt_pl_seek"), playback.format_seconds(target))
        self._overlay_until = time.time() + 4

    # ------------------------------------------------------------------
    # Input
    # ------------------------------------------------------------------
    def handle_input(self, inputs):
        if self._leaving:
            return True
        if inputs.get("btn_b"):
            self._save_progress(force=True)
            if self.session:
                self.session.state = "idle"
                playback.save_session(self.session)
            self._leaving = True
            self._stop_player()
            self.engine.pop_screen()
            return True
        if not self.player:
            return True

        if inputs.get("btn_a"):
            self.player.set_paused(not self.player.paused)
            self.status = tr("yt_pl_paused") if self.player.paused else ""
        elif inputs.get("btn_y"):
            self._save_progress(force=True)
            self._advance_or_finish()
            return True
        elif inputs.get("btn_left"):
            self._queue_seek(-10)
        elif inputs.get("btn_right"):
            self._queue_seek(10)
        elif inputs.get("btn_l1"):
            self._queue_seek(-60)
        elif inputs.get("btn_r1"):
            self._queue_seek(60)
        elif inputs.get("btn_up"):
            self.player.set_volume(self.player.volume + 5)
        elif inputs.get("btn_down"):
            self.player.set_volume(self.player.volume - 5)
        elif inputs.get("btn_x"):
            self._cycle_quality()
            return True

        self._overlay_until = time.time() + 4
        return True

    def _cycle_quality(self):
        try:
            i = QUALITY_CYCLE.index(self.quality)
        except ValueError:
            i = 0
        self.quality = QUALITY_CYCLE[(i + 1) % len(QUALITY_CYCLE)]
        state.video_quality = self.quality
        try:
            state.save_settings()
        except Exception:
            pass
        self.engine.toast("%s: %sp" % (tr("yt_pl_quality"), self.quality))
        if self.player:
            self._pending_start = self.player.get_position()
        self._play_current()

    # ------------------------------------------------------------------
    # Update
    # ------------------------------------------------------------------
    def update(self, dt):
        # Start playback on the main thread once resolving finished (see
        # _bg_resolve for why it cannot happen on the worker thread).
        if self._pending_res is not None:
            res, start, cached = self._pending_res
            self._pending_res = None
            if self._leaving:
                return
            if not res:
                # In-app cannot resolve this video: fall back to RetroArch
                # rather than skipping through the whole queue.
                self._fallback_retroarch(tr("yt_err_play"))
                return
            self._cached_url_used = cached
            self._start_player(res, start)
            return
        if not self.player:
            return
        # Apply a pending seek once the user stops pressing left/right.
        if self._seek_target is not None and time.time() >= self._seek_deadline:
            target = self._seek_target
            self._seek_target = None
            try:
                self.player.seek(target)
            except Exception as e:
                self._player_error(e)
                return
            if getattr(self.player, "seek_note", "") == "restart":
                # Backward seek: the stream is read again from the beginning.
                self.status = tr("yt_pl_seeking_back")
            else:
                self.status = ""
            self._overlay_until = time.time() + 2
        try:
            self.player.update()
        except Exception as e:
            self._player_error(e)
            return
        if self.player.error:
            self.status = self.player.error
        # Waiting for the first frame of a seek/resume: show how far the
        # decoders have got, so a pipe re-read does not look like a freeze.
        if not self.player.has_uploaded() and not self.resolving:
            decoded = float(getattr(self.player, "decode_pos", 0.0) or 0.0)
            if decoded > 0.5:
                label = (tr("yt_pl_seeking_back")
                         if getattr(self.player, "seek_note", "") == "restart"
                         else tr("yt_pl_buffering"))
                self.status = "%s %s" % (label, playback.format_seconds(decoded))
                self._overlay_until = time.time() + 1.0
        now = time.time()
        if now - self._last_progress >= 5:
            self._save_progress()
            self._last_progress = now
        if self.player.is_finished():
            played = self.player.produced_data()
            self._save_progress(force=True)
            if not played:
                cur = self.current or {}
                if self._cached_url_used:
                    # A cached googlevideo URL can expire (403): drop it and
                    # re-resolve once before giving up on the in-app player.
                    self._cached_url_used = False
                    yt.clear_cached_streams(cur.get("id"), self.quality)
                    self._pending_start = self.player.get_position()
                    self._play_current()
                else:
                    # ffmpeg produced nothing (bad URL/codec): use RetroArch instead.
                    self._fallback_retroarch()
            else:
                self._advance_or_finish()

    # ------------------------------------------------------------------
    # Render
    # ------------------------------------------------------------------
    def render(self, engine):
        area_y = 64
        area_h = state.SCREEN_H - 64 - 56
        engine.fill_rect(0, area_y, state.SCREEN_W, area_h, 0, 0, 0, 255)

        has_video = bool(self.player and self.player.texture and not self.player.audio_only)
        if has_video:
            self._draw_video(engine, area_y, area_h)

        if self.resolving or (self.player and not self.player.audio_only
                              and self.player.get_position() <= 0):
            engine.draw_text(self.status or tr("yt_pl_buffering"), engine.font_sub,
                             state.SCREEN_W // 2, area_y + area_h // 2,
                             0, 220, 245, center_x=True, center_y=True)

        paused = bool(self.player and self.player.paused)
        if paused or time.time() < self._overlay_until:
            self._draw_overlay(engine, area_y)

    def _draw_video(self, engine, area_y, area_h):
        tw, th = self.player.tex_w, self.player.tex_h
        if tw <= 0 or th <= 0 or sdl2 is None:
            return
        scale = min(state.SCREEN_W / float(tw), area_h / float(th))
        dw, dh = int(tw * scale), int(th * scale)
        dx = (state.SCREEN_W - dw) // 2
        dy = area_y + (area_h - dh) // 2
        rect = sdl2.SDL_Rect(dx, dy, dw, dh)
        rc = sdl2.SDL_RenderCopy(engine.renderer, self.player.texture, None, rect)
        # Sample the drawn pixel only once a real frame has been uploaded.
        if not self._rc_logged and self.player.has_uploaded():
            self._rc_logged = True
            try:
                from ..yt_player import log as _ytlog
                _ytlog("[inapp] video: RenderCopy rc=%s rect=(%d,%d,%d,%d)" %
                       (rc, dx, dy, dw, dh))
                buf = (ctypes.c_ubyte * 4)()
                pr = sdl2.SDL_Rect(dx + dw // 2, dy + dh // 2, 1, 1)
                rrc = sdl2.SDL_RenderReadPixels(engine.renderer, ctypes.byref(pr),
                                                sdl2.SDL_PIXELFORMAT_RGBA8888, buf, 4)
                _ytlog("[inapp] video: ReadPixels rc=%s center=%s" % (rrc, bytes(buf).hex()))
            except Exception as e:
                _ytlog("[inapp] video: RenderCopy/ReadPixels error: %s" % e)

    def _draw_overlay(self, engine, area_y):
        pad = 24
        engine.fill_rect(0, area_y, state.SCREEN_W, 54, 0, 0, 0, 190)
        engine.draw_text(self.title, engine.font_badge, pad, area_y + 27,
                         255, 255, 255, center_y=True)
        if self.channel:
            engine.draw_text(self.channel, engine.font_footer, state.SCREEN_W - pad,
                             area_y + 27, 180, 200, 225, center_y=True, right_align=True)

        by = state.SCREEN_H - 56 - 84
        engine.fill_rect(0, by, state.SCREEN_W, 84, 0, 0, 0, 200)
        pos = self.player.get_position() if self.player else 0.0
        dur = self.duration or (self.player.get_duration() if self.player else 0.0)
        bar_x = pad
        bar_w = state.SCREEN_W - pad * 2
        engine.fill_rect(bar_x, by + 16, bar_w, 8, 60, 70, 95, 255)
        frac = 0.0
        if dur > 0:
            frac = max(0.0, min(1.0, pos / dur))
        engine.fill_rect(bar_x, by + 16, int(bar_w * frac), 8, 230, 33, 23, 255)
        engine.draw_text(playback.format_seconds(pos), engine.font_footer, bar_x, by + 30,
                         220, 230, 245)
        engine.draw_text(playback.format_seconds(dur), engine.font_footer, bar_x + bar_w,
                         by + 30, 220, 230, 245, right_align=True)

        state_lbl = self.status or (tr("yt_pl_paused") if (self.player and self.player.paused) else "")
        if state_lbl:
            engine.draw_text(state_lbl, engine.font_footer, state.SCREEN_W // 2, by + 34,
                             0, 220, 245, center_x=True)

        vol = self.player.volume if self.player else 100
        hints = "A:%s  Y:%s  X:%sp  L/R:%s  +/-:%d%%  B:%s" % (
            tr("yt_pl_pause"), tr("yt_pl_next"), self.quality,
            tr("yt_pl_seek"), vol, tr("yt_pl_exit"))
        engine.draw_text(hints, engine.font_footer, pad, by + 60, 150, 170, 200)
