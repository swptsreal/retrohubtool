# -*- coding: utf-8 -*-
"""In-app YouTube player engine (ffmpeg subprocess + SDL2).

Runs inside the app process so the UI can draw an overlay while the video
plays. Two ffmpeg processes decode the streams: one writes raw BGRA video
frames, the other writes signed 16-bit stereo audio that is queued straight
into the SDL audio device. Audio is the master clock; video frames are dropped
or held to stay in sync.

Everything is best-effort: if ffmpeg or the audio device is unavailable the
caller falls back to the RetroArch handoff.
"""

import ctypes
import os
import shutil
import subprocess
import threading
import time
from collections import deque

try:
    import sdl2
    import sdl2.audio as sdl_audio
    import sdl2.render as sdl_render
    import sdl2.pixels as sdl_pixels
except Exception:  # pragma: no cover - only on non-device hosts
    sdl2 = sdl_audio = sdl_render = sdl_pixels = None

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

AUDIO_RATE = 48000
AUDIO_CHANNELS = 2
AUDIO_SAMPLE_BYTES = 2
AUDIO_BYTES_PER_SEC = AUDIO_RATE * AUDIO_CHANNELS * AUDIO_SAMPLE_BYTES  # 192000
FPS = 30
AUDIO_HIGH = AUDIO_BYTES_PER_SEC // 2   # 0.5s queued -> backpressure
READ_CHUNK = 65536
FRAME_QUEUE_MAX = 8
SYNC_DROP = 0.12                        # drop frames later than this
SYNC_HOLD = 0.05                        # release a frame within this of the clock

# Quality -> decoded/render height. 720 is downscaled to 540 to keep the raw
# frame upload bounded on the A133; the source stream is still full quality.
RENDER_HEIGHTS = {"360": 360, "480": 480, "720": 540}


def _read_exact(f, n):
    """Read exactly *n* bytes from a raw pipe.

    A raw pipe read can return a short count even mid-stream, so a single
    read() is not enough to know EOF. Returns b"" only on a clean EOF at a
    frame boundary, or a shorter buffer if EOF hit mid-frame."""
    buf = bytearray()
    while len(buf) < n:
        chunk = f.read(n - len(buf))
        if not chunk:
            break
        buf.extend(chunk)
    return bytes(buf)


def _url_itag(url):
    """Extract the YouTube itag from a googlevideo URL (for diagnostics)."""
    try:
        import urllib.parse
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url or "").query)
        return q.get("itag", ["?"])[0]
    except Exception:
        return "?"


def _log(msg):
    """Write diagnostics to the same log the RetroArch path uses."""
    try:
        from .yt_player import log
        log("[inapp] %s" % msg)
    except Exception:
        pass


def _even(n):
    return n - (n % 2)


def target_size(quality):
    h = RENDER_HEIGHTS.get(str(quality), 360)
    return _even(int(h * 16 / 9)), h


def find_ffmpeg():
    for c in ("/usr/bin/ffmpeg", "/mnt/SDCARD/System/bin/ffmpeg", "/usr/local/bin/ffmpeg"):
        if os.path.exists(c):
            return c
    return shutil.which("ffmpeg")


_H264_CACHE = None


def has_h264_decoder():
    """True when the device ffmpeg can decode H.264.

    The bundled ffmpeg is built for the screen streamer (rawvideo -> mjpeg) and
    may not include an H.264 decoder; without it in-app playback cannot work and
    we must use the RetroArch path. Cached because `-decoders` is slow.
    """
    global _H264_CACHE
    if _H264_CACHE is not None:
        return _H264_CACHE
    ff = find_ffmpeg()
    if not ff:
        _H264_CACHE = False
        return False
    try:
        out = subprocess.run(
            [ff, "-hide_banner", "-decoders"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=6
        ).stdout.decode("utf-8", "ignore")
        _H264_CACHE = "h264" in out
    except Exception:
        _H264_CACHE = False
    return _H264_CACHE


def h264_status():
    """Cached result of has_h264_decoder(): True/False, or None if not probed."""
    return _H264_CACHE


def precompute_decoder_check():
    """Probe the H.264 decoder off the main thread (cached afterwards)."""
    threading.Thread(target=has_h264_decoder, daemon=True).start()


_HTTP_CACHE = None


def has_http_protocol():
    """True when the device ffmpeg can open http(s) URLs.

    The bundled ffmpeg is built for the screen streamer (rawvideo -> mjpeg) and
    often has no network protocols, so feeding it a googlevideo URL fails with
    "Protocol not found". Cached because `-protocols` is slow.
    """
    global _HTTP_CACHE
    if _HTTP_CACHE is not None:
        return _HTTP_CACHE
    ff = find_ffmpeg()
    if not ff:
        _HTTP_CACHE = False
        return False
    try:
        out = subprocess.run(
            [ff, "-hide_banner", "-protocols"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=6
        ).stdout.decode("utf-8", "ignore")
        _HTTP_CACHE = ("https" in out) or ("http" in out)
    except Exception:
        _HTTP_CACHE = False
    return _HTTP_CACHE


def http_protocol_status():
    """Cached result of has_http_protocol(): True/False, or None if not probed."""
    return _HTTP_CACHE


def precompute_protocol_check():
    threading.Thread(target=has_http_protocol, daemon=True).start()


def available():
    """Fast check (no subprocess): SDL audio + an ffmpeg binary are present."""
    return bool(sdl2 and sdl_audio and find_ffmpeg())


class InAppPlayer:
    def __init__(self, renderer):
        self.renderer = renderer
        self.audio_dev = 0
        self.texture = None
        self.tex_w = 0
        self.tex_h = 0
        self.video_proc = None
        self.audio_proc = None
        self._threads = []
        self._stop = threading.Event()
        self._frame_lock = threading.Lock()
        self._frames = deque()
        self._audio_fed = 0
        self._video_done = False
        self._audio_done = False
        self._got_audio = False
        self._got_video = False
        self._got_upload = False
        self._video_read = 0
        self._video_up = 0
        self._video_drop = 0
        self.audio_err = None
        self.video_err = None
        self.paused = False
        self.audio_only = False
        self.volume = 100
        self.speed = 1.0
        self.position = 0.0
        self.duration = 0.0
        self.error = ""
        self._w, self._h = target_size("360")
        self._start_pos = 0.0
        self._video_url = ""
        self._audio_url = ""

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self, video_url, audio_url, start_pos=0.0, audio_only=False,
              speed=1.0, quality="360", duration=0.0) -> bool:
        if not available():
            self.error = "ffmpeg or SDL audio unavailable"
            _log(self.error)
            return False
        if not has_h264_decoder():
            self.error = "ffmpeg has no H.264 decoder"
            _log(self.error)
            return False
        _log("start audio_only=%s quality=%s size=%dx%d start=%.1fs" %
             (self.audio_only, quality, self._w, self._h, self._start_pos))

        self._video_url = video_url or audio_url
        self._audio_url = audio_url or video_url
        self._start_pos = max(0.0, float(start_pos or 0.0))
        self.audio_only = bool(audio_only)
        self.speed = float(speed or 1.0)
        self.duration = float(duration or 0.0)
        self._w, self._h = target_size(quality)
        self.position = self._start_pos
        self.error = ""
        self.paused = False
        self._stop.clear()
        self._frames.clear()
        self._audio_fed = 0
        self._video_done = self.audio_only
        self._audio_done = False
        self._got_audio = False
        self._got_video = False
        self._got_upload = False
        self._video_read = self._video_up = self._video_drop = 0

        try:
            sdl2.SDL_InitSubSystem(sdl2.SDL_INIT_AUDIO)
            spec = sdl_audio.SDL_AudioSpec(AUDIO_RATE, sdl_audio.AUDIO_S16LSB,
                                           AUDIO_CHANNELS, 4096)
            self.audio_dev = sdl_audio.SDL_OpenAudioDevice(None, 0, spec, None, 0)
        except Exception as e:
            self.error = f"audio init: {e}"
            _log(self.error)
            return False
        if not self.audio_dev:
            self.error = "cannot open audio device"
            _log(self.error)
            return False
        sdl_audio.SDL_PauseAudioDevice(self.audio_dev, 0)

        if not self.audio_only:
            # ARGB8888 is the GLES2-friendly 32-bit format; on little-endian its
            # byte order is B,G,R,A, which matches ffmpeg's "bgra" output.
            self.texture = sdl2.SDL_CreateTexture(
                self.renderer,
                sdl_pixels.SDL_PIXELFORMAT_ARGB8888,
                sdl_render.SDL_TEXTUREACCESS_STREAMING,
                self._w, self._h)
            self.tex_w, self.tex_h = self._w, self._h
            _log("video: texture created=%s fmt=ARGB8888 %dx%d" %
                 (bool(self.texture), self._w, self._h))

        # Truncate the ffmpeg stderr logs once per session; spawns append so a
        # later seek cannot wipe the error that explains an earlier failure.
        for p in ("/tmp/rh_ffmpeg_a.log", "/tmp/rh_ffmpeg_v.log"):
            try:
                open(p, "wb").close()
            except Exception:
                pass

        ff = find_ffmpeg()
        self._spawn_audio(ff)
        if not self.audio_only:
            self._spawn_video(ff)
        return True

    def stop(self):
        self._stop.set()
        self._kill_procs()
        try:
            if self.audio_dev:
                sdl_audio.SDL_ClearQueuedAudio(self.audio_dev)
                sdl_audio.SDL_CloseAudioDevice(self.audio_dev)
        except Exception:
            pass
        self.audio_dev = 0
        if self.texture:
            try:
                sdl2.SDL_DestroyTexture(self.texture)
            except Exception:
                pass
            self.texture = None
        self._frames.clear()

    def seek(self, pos):
        """Pipe mode cannot seek; restart the stream from the beginning."""
        _log("seek %.1fs requested; pipe mode restarts from 0" % max(0.0, float(pos)))
        self._start_pos = 0.0
        self.position = 0.0
        self._kill_procs()
        self._stop.clear()
        self._frames.clear()
        self._audio_fed = 0
        self._video_done = self.audio_only
        self._audio_done = False
        self._got_audio = False
        self._got_video = False
        self._got_upload = False
        self._video_read = self._video_up = self._video_drop = 0
        try:
            sdl_audio.SDL_ClearQueuedAudio(self.audio_dev)
        except Exception:
            pass
        ff = find_ffmpeg()
        self._spawn_audio(ff)
        if not self.audio_only:
            self._spawn_video(ff)

    # ------------------------------------------------------------------
    # ffmpeg processes
    # ------------------------------------------------------------------
    def _spawn_feeder(self, url, proc, kind):
        t = threading.Thread(target=self._feed_stream, args=(url, proc, kind), daemon=True)
        t.start()
        self._threads.append(t)

    def _feed_stream(self, url, proc, kind):
        """Fetch the stream with Python and pipe it into ffmpeg's stdin.

        The device ffmpeg has no http(s) protocol (it is built for the screen
        streamer), so it cannot open a googlevideo URL directly. Feeding
        `-i pipe:0` avoids that entirely."""
        import ssl as _ssl
        import urllib.request as _urlreq
        total = 0
        try:
            req = _urlreq.Request(url, headers={"User-Agent": _UA})
            ctx = _ssl._create_unverified_context()
            with _urlreq.urlopen(req, timeout=20, context=ctx) as resp:
                _log("feed %s start" % kind)
                while not self._stop.is_set():
                    if self.paused:
                        time.sleep(0.02)
                        continue
                    chunk = resp.read(READ_CHUNK)
                    if not chunk:
                        break
                    try:
                        proc.stdin.write(chunk)
                        total += len(chunk)
                    except Exception:
                        break
        except Exception as e:
            _log("feed %s error: %s" % (kind, e))
        finally:
            _log("feed %s done bytes=%d" % (kind, total))
            try:
                if proc.stdin:
                    proc.stdin.close()
            except Exception:
                pass

    def _spawn_audio(self, ff):
        cmd = [ff, "-hide_banner", "-loglevel", "error", "-f", "mp4", "-i", "pipe:0", "-vn"]
        if abs(self.speed - 1.0) > 0.01:
            cmd += ["-af", "atempo=%.3f" % self.speed]
        cmd += ["-f", "s16le", "-ar", str(AUDIO_RATE), "-ac", str(AUDIO_CHANNELS), "pipe:1"]
        _log("audio: ffmpeg -f mp4 -i pipe:0 itag=%s" % _url_itag(self._audio_url))
        try:
            self.audio_err = open("/tmp/rh_ffmpeg_a.log", "ab")
        except Exception:
            self.audio_err = subprocess.DEVNULL
        self.audio_proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=self.audio_err, bufsize=0)
        self._spawn_feeder(self._audio_url, self.audio_proc, "a")
        t = threading.Thread(target=self._audio_loop, daemon=True)
        t.start()
        self._threads.append(t)

    def _spawn_video(self, ff):
        vf = "fps=%d,scale=%d:%d:flags=fast_bilinear" % (FPS, self._w, self._h)
        if abs(self.speed - 1.0) > 0.01:
            vf = "setpts=PTS/%.3f,%s" % (self.speed, vf)
        cmd = [ff, "-hide_banner", "-loglevel", "error", "-f", "mp4", "-i", "pipe:0",
               "-an", "-vf", vf, "-pix_fmt", "bgra", "-f", "rawvideo", "pipe:1"]
        _log("video: ffmpeg -f mp4 -i pipe:0 itag=%s size=%dx%d" %
             (_url_itag(self._video_url), self._w, self._h))
        try:
            self.video_err = open("/tmp/rh_ffmpeg_v.log", "ab")
        except Exception:
            self.video_err = subprocess.DEVNULL
        self.video_proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=self.video_err, bufsize=0)
        self._spawn_feeder(self._video_url, self.video_proc, "v")
        t = threading.Thread(target=self._video_loop, daemon=True)
        t.start()
        self._threads.append(t)

    def _kill_procs(self):
        for p in (self.video_proc, self.audio_proc):
            try:
                if p and p.poll() is None:
                    p.kill()
            except Exception:
                pass
        self.video_proc = None
        self.audio_proc = None
        for h in (self.audio_err, self.video_err):
            try:
                if hasattr(h, "close"):
                    h.close()
            except Exception:
                pass
        self.audio_err = None
        self.video_err = None
        for t in self._threads:
            try:
                t.join(timeout=0.5)
            except Exception:
                pass
        self._threads = []

    # ------------------------------------------------------------------
    # Reader threads
    # ------------------------------------------------------------------
    def _audio_loop(self):
        proc = self.audio_proc
        try:
            while not self._stop.is_set():
                if self.paused:
                    time.sleep(0.02)
                    continue
                try:
                    queued = sdl_audio.SDL_GetQueuedAudioSize(self.audio_dev)
                except Exception:
                    queued = 0
                if queued > AUDIO_HIGH:
                    time.sleep(0.01)
                    continue
                data = proc.stdout.read(READ_CHUNK)
                if not data:
                    break
                self._got_audio = True
                data = self._apply_volume(data)
                sdl_audio.SDL_QueueAudio(self.audio_dev, ctypes.c_char_p(data), len(data))
                self._audio_fed += len(data)
        except Exception as e:
            self.error = f"audio: {e}"
        finally:
            self._audio_done = True
            self._dump_err("a")

    def _video_loop(self):
        proc = self.video_proc
        frame_bytes = self._w * self._h * 4
        idx = 0
        try:
            while not self._stop.is_set():
                if self.paused:
                    time.sleep(0.02)
                    continue
                with self._frame_lock:
                    full = len(self._frames) >= FRAME_QUEUE_MAX
                if full:
                    time.sleep(0.01)
                    continue
                data = _read_exact(proc.stdout, frame_bytes)
                if not data:
                    _log("video: EOF after %d frames" % idx)
                    break
                if len(data) < frame_bytes:
                    _log("video: partial frame at EOF (%d of %d) after %d frames" %
                         (len(data), frame_bytes, idx))
                    break
                self._got_video = True
                if idx == 0:
                    _log("video: first frame read (%d bytes, expect %d)" % (len(data), frame_bytes))
                with self._frame_lock:
                    self._frames.append((idx / float(FPS), data))
                idx += 1
        except Exception as e:
            self.error = f"video: {e}"
        finally:
            self._video_done = True
            self._video_read = idx
            self._dump_err("v")
            _log("video: loop end read=%d up=%d drop=%d rc=%s" %
                 (idx, self._video_up, self._video_drop,
                  proc.poll() if proc else "?"))

    def _dump_err(self, kind):
        """Log the tail of ffmpeg's stderr so failures are diagnosable."""
        try:
            path = "/tmp/rh_ffmpeg_%s.log" % kind
            if os.path.exists(path):
                with open(path, "r", errors="ignore") as f:
                    txt = f.read().strip()
                if txt:
                    _log("ffmpeg %s stderr: %s" % (kind, txt[-400:]))
        except Exception:
            pass

    def _apply_volume(self, data):
        if self.volume >= 100 or not data:
            return data
        try:
            import audioop
            return audioop.mul(data, AUDIO_SAMPLE_BYTES, self.volume / 100.0)
        except Exception:
            return data

    # ------------------------------------------------------------------
    # Clock / sync
    # ------------------------------------------------------------------
    def _audio_pos(self):
        try:
            queued = sdl_audio.SDL_GetQueuedAudioSize(self.audio_dev)
        except Exception:
            queued = 0
        played = max(0, self._audio_fed - queued)
        return self._start_pos + played / float(AUDIO_BYTES_PER_SEC)

    def update(self):
        """Advance the clock and upload the next due video frame."""
        if self.paused:
            return
        self.position = self._audio_pos()
        if self.audio_only or not self.texture:
            return
        audio_pos = self.position
        due = None
        with self._frame_lock:
            while self._frames and self._frames[0][0] < audio_pos - SYNC_DROP:
                self._frames.popleft()
                self._video_drop += 1
            if self._frames and self._frames[0][0] <= audio_pos + SYNC_HOLD:
                due = self._frames.popleft()[1]
        if due is not None:
            ok = False
            try:
                px = ctypes.c_void_p()
                pitch = ctypes.c_int(0)
                rc = sdl2.SDL_LockTexture(self.texture, None,
                                          ctypes.byref(px), ctypes.byref(pitch))
                if rc == 0 and px.value:
                    ctypes.memmove(px, due, len(due))
                    sdl2.SDL_UnlockTexture(self.texture)
                    ok = True
                else:
                    rc2 = sdl2.SDL_UpdateTexture(self.texture, None,
                                                 ctypes.c_char_p(due), self._w * 4)
                    ok = (rc2 == 0)
                if not self._got_upload:
                    self._got_upload = True
                    _log("video: first upload ok=%s lock_rc=%s pitch=%s audio_pos=%.2f" %
                         (ok, rc, pitch.value, audio_pos))
            except Exception as e:
                _log("video: upload error: %s" % e)
            if ok:
                self._video_up += 1
                if self._video_up % 300 == 0:
                    _log("video: up=%d drop=%d pos=%.2f queued=%d" %
                         (self._video_up, self._video_drop, audio_pos, len(self._frames)))

    def produced_data(self) -> bool:
        """True once any audio or video bytes arrived (i.e. it actually played)."""
        return self._got_audio or self._got_video

    def is_finished(self) -> bool:
        if self.error:
            return True
        if self._stop.is_set():
            return True
        if not (self._audio_done and self._video_done):
            return False
        try:
            if sdl_audio.SDL_GetQueuedAudioSize(self.audio_dev) > 0:
                return False
        except Exception:
            pass
        return True

    # ------------------------------------------------------------------
    # Controls
    # ------------------------------------------------------------------
    def set_paused(self, paused):
        self.paused = bool(paused)
        try:
            sdl_audio.SDL_PauseAudioDevice(self.audio_dev, 1 if self.paused else 0)
        except Exception:
            pass

    def set_volume(self, volume):
        self.volume = max(0, min(100, int(volume)))

    def set_speed(self, speed):
        self.speed = max(0.5, min(2.0, float(speed)))
        self.seek(self.position)

    def get_position(self) -> float:
        return self.position

    def get_duration(self) -> float:
        return self.duration
