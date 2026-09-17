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
import sys
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

# Packed-32 layouts to try, each paired with the ffmpeg pixel format whose byte
# order matches it. SDL only samples one of these natively; for the others its
# GLES2 renderer swaps channels in the shader and the Mali driver on these
# handhelds draws those frames black - i.e. video with sound but a black screen.
# On little-endian `rgba` bytes are R,G,B,A, which is SDL's ABGR8888 (SDL's own
# SDL_PIXELFORMAT_RGBA32 alias is ABGR8888 on LE), so that pair goes first.
PIXEL_CANDIDATES = (
    ("ABGR8888", "rgba"),   # 0xAABBGGRR -> memory R,G,B,A
    ("RGBA8888", "abgr"),   # 0xRRGGBBAA -> memory A,B,G,R
    ("ARGB8888", "bgra"),   # 0xAARRGGBB -> memory B,G,R,A
    ("BGRA8888", "argb"),   # 0xBBGGRRAA -> memory A,R,G,B
)
VIDEO_STALL_WARN = 2.5      # react quickly when a decoder stops
HOLD_MAX = 3.0              # release the seek audio hold even without a frame
_PIXEL_FMT_CACHE = {}


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


def mem_available_mb():
    """Free RAM in MB, or -1 when /proc/meminfo is unreadable."""
    try:
        with open("/proc/meminfo", "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except Exception:
        pass
    return -1


def choose_pixel_format(renderer):
    """Pick the packed layout this renderer can draw, plus the matching pix_fmt.

    SDL lists its native texture format first in SDL_RendererInfo, and only that
    one is sampled without a channel swizzle; the Mali GLES2 driver on these
    handhelds draws the swizzled variants black (video with sound, black
    screen). So take the first advertised format we know how to feed and ask
    ffmpeg for exactly that byte order. Nothing is drawn and no pixel is read
    back: reading the window after a buffer swap is undefined and has upset this
    driver, and a texture probe is not worth that risk.

    On the TrimUI Brick the list starts with ABGR8888 - bytes R,G,B,A, the same
    order ffmpeg's `rgba` writes - which is also the fallback for a renderer
    reporting nothing useful.

    Must be called from the thread that owns the renderer.
    """
    known = {}
    for label, pix_fmt in PIXEL_CANDIDATES:
        fmt = getattr(sdl_pixels, "SDL_PIXELFORMAT_" + label, None)
        if fmt is not None:
            known[fmt] = (label, pix_fmt)

    name, formats = "?", []
    try:
        info = sdl2.SDL_RendererInfo()
        if sdl2.SDL_GetRendererInfo(renderer, ctypes.byref(info)) == 0:
            if info.name:
                name = info.name.decode()
            formats = [info.texture_formats[i] for i in range(info.num_texture_formats)]
    except Exception as e:
        _log("pixel format: renderer info failed (%s)" % e)

    cached = _PIXEL_FMT_CACHE.get(name)
    if cached:
        return cached

    listed = ",".join("0x%x" % f for f in formats) or "none"
    for fmt in formats:
        if fmt in known:
            label, pix_fmt = known[fmt]
            _log("pixel format: renderer=%s native=0x%x layout=%s pix_fmt=%s "
                 "advertised=[%s]" % (name, fmt, label, pix_fmt, listed))
            chosen = (label, fmt, pix_fmt)
            _PIXEL_FMT_CACHE[name] = chosen
            return chosen

    label, pix_fmt = PIXEL_CANDIDATES[0]
    fmt = getattr(sdl_pixels, "SDL_PIXELFORMAT_" + label, None)
    _log("pixel format: no known layout in [%s] (renderer=%s), defaulting to "
         "layout=%s pix_fmt=%s" % (listed, name, label, pix_fmt))
    chosen = (label, fmt, pix_fmt)
    _PIXEL_FMT_CACHE[name] = chosen
    return chosen
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
        self._tex_label = PIXEL_CANDIDATES[0][0]
        self._tex_fmt = None
        self._pix_fmt = PIXEL_CANDIDATES[0][1]
        self._spawn_time = 0.0
        self._stall_logged = False
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
        self._upload_err_logged = False
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
        self.seek_note = ""
        self.decode_pos = 0.0
        self._hold_audio = False
        self.error = ""
        self._w, self._h = target_size("360")
        self._start_pos = 0.0
        self._local_pos = 0.0
        self._video_url = ""
        self._audio_url = ""
        self._video_ss = 0.0
        self._audio_ss = 0.0
        self._respawns = 0
        self._hold_since = 0.0
        self.video_id = ""
        self.quality = "360"
        self._info_json = ""
        self._dl_procs = {}
        self._url_src = {}
        self._pending_spawn = False
        self._info_json_failed = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self, video_url, audio_url, start_pos=0.0, audio_only=False,
              speed=1.0, quality="360", duration=0.0, video_id=None) -> bool:
        if not available():
            self.error = "ffmpeg or SDL audio unavailable"
            _log(self.error)
            return False
        if not has_h264_decoder():
            self.error = "ffmpeg has no H.264 decoder"
            _log(self.error)
            return False
        try:
            info = sdl2.SDL_RendererInfo()
            if sdl2.SDL_GetRendererInfo(self.renderer, ctypes.byref(info)) == 0:
                fmts = ",".join(hex(info.texture_formats[i])
                                for i in range(info.num_texture_formats))
                _log("renderer: %s flags=0x%x max=%dx%d formats=[%s]" %
                     (info.name.decode() if info.name else "?",
                      info.flags, info.max_texture_width, info.max_texture_height, fmts))
        except Exception as e:
            _log("renderer info error: %s" % e)

        self.video_id = video_id or ""
        self._video_url = video_url or audio_url
        self._audio_url = audio_url or video_url
        self._start_pos = max(0.0, float(start_pos or 0.0))
        self.audio_only = bool(audio_only)
        self.speed = float(speed or 1.0)
        self.duration = float(duration or 0.0)
        self.quality = str(quality or "360")
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
        self._upload_err_logged = False
        self._video_read = self._video_up = self._video_drop = 0
        self._local_pos = 0.0
        self.decode_pos = self._start_pos
        self.seek_note = ""
        self._video_ss = self._start_pos
        self._audio_ss = self._start_pos
        self._respawns = 0
        self._pending_spawn = False
        self._info_json_failed = False
        _log("player build: section-v3 (pipe + yt-dlp sections for seek/resume)")
        _log("start audio_only=%s quality=%s size=%dx%d start=%.1fs speed=%.2f "
             "mem_avail=%dMB" %
             (self.audio_only, quality, self._w, self._h, self._start_pos, self.speed,
              mem_available_mb()))

        # yt-dlp's metadata for this video is needed to download a section on the
        # next seek. Building it takes a few seconds, so do it in the background
        # while the video plays instead of in the seek itself.
        if self.video_id:
            t = threading.Thread(target=self._build_info_json, daemon=True)
            t.start()
            self._threads.append(t)

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
        if self._start_pos > 0.5:
            # Picking up mid-stream: hold the sound until the picture is there,
            # so the progress bar does not run ahead of the screen.
            self._hold_audio = True
            self._hold_since = time.time()
            sdl_audio.SDL_PauseAudioDevice(self.audio_dev, 1)
            _log("audio: held until the video catches up (resume at %.1fs)" %
                 self._start_pos)

        if not self.audio_only:
            self._tex_label, self._tex_fmt, self._pix_fmt = choose_pixel_format(self.renderer)
            self.texture = sdl2.SDL_CreateTexture(
                self.renderer, self._tex_fmt,
                sdl_render.SDL_TEXTUREACCESS_STREAMING, self._w, self._h)
            self.tex_w, self.tex_h = self._w, self._h
            try:
                sdl2.SDL_SetTextureBlendMode(self.texture, sdl2.SDL_BLENDMODE_NONE)
                sdl2.SDL_SetTextureAlphaMod(self.texture, 255)
                fmt = ctypes.c_uint(0)
                acc = ctypes.c_int(0)
                qw = ctypes.c_int(0)
                qh = ctypes.c_int(0)
                qrc = sdl2.SDL_QueryTexture(self.texture, ctypes.byref(fmt),
                                            ctypes.byref(acc), ctypes.byref(qw),
                                            ctypes.byref(qh))
                _log("video: texture created=%s layout=%s pix_fmt=%s query rc=%s "
                     "fmt=0x%x %dx%d" %
                     (bool(self.texture), self._tex_label, self._pix_fmt, qrc,
                      fmt.value, qw.value, qh.value))
            except Exception as e:
                _log("video: texture setup error: %s" % e)

        for p in ("/tmp/rh_ffmpeg_a.log", "/tmp/rh_ffmpeg_v.log"):
            try:
                open(p, "wb").close()
            except Exception:
                pass

        ff = find_ffmpeg()
        self._spawn_time = time.time()
        self._stall_logged = False
        if self._start_pos > 0.5 and not self._info_json:
            # Resuming: the source has to be a section from that point (reading
            # from the start would take minutes on a long video). yt-dlp's
            # metadata is still being built, so start the decoders as soon as it
            # is ready - update() does that on the next frames.
            self._pending_spawn = True
            _log("resume %.1fs: waiting for yt-dlp metadata before starting" %
                 self._start_pos)
        else:
            self._spawn_decoders(ff)
        return True

    def _spawn_decoders(self, ff=None):
        self._pending_spawn = False
        ff = ff or find_ffmpeg()
        self._spawn_time = time.time()
        self._stall_logged = False
        self._spawn_audio(ff)
        if not self.audio_only:
            self._spawn_video(ff)

    def _build_info_json(self):
        try:
            from .yt import resolve_info_json
            path = resolve_info_json(self.video_id, self.quality)
            if path:
                self._info_json = path
                _log("info json ready: %s" % path)
            else:
                self._info_json_failed = True
        except Exception as e:
            self._info_json_failed = True
            _log("info json failed: %s" % e)

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

    def seek(self, pos, note="restart"):
        """Restart both decoders at *pos*.

        Nothing is re-read from the beginning: for a seek the source is a
        yt-dlp *section* download (it knows the fragment byte ranges), so the
        picture starts within a second or two at any position, forwards or
        backwards, on any network.
        """
        target = max(0.0, float(pos or 0.0))
        if self.duration > 0:
            target = min(target, max(0.0, self.duration - 1.0))
        self._start_pos = target
        self._local_pos = 0.0
        self.position = target
        self.seek_note = note
        self._respawns += 1
        _log("seek %.1fs -> section restart (respawn #%d)" % (target, self._respawns))
        self._kill_procs()
        self._stop.clear()
        self._frames.clear()
        self._audio_fed = 0
        self._video_done = self.audio_only
        self._audio_done = False
        self._got_audio = False
        self._got_video = False
        self._got_upload = False
        self._upload_err_logged = False
        self._video_read = self._video_up = self._video_drop = 0
        self.decode_pos = target
        try:
            sdl_audio.SDL_ClearQueuedAudio(self.audio_dev)
        except Exception:
            pass
        if self.audio_dev:
            try:
                self._hold_audio = True
                self._hold_since = time.time()
                sdl_audio.SDL_PauseAudioDevice(self.audio_dev, 1)
            except Exception:
                self._hold_audio = False
        ff = find_ffmpeg()
        self._spawn_time = time.time()
        self._stall_logged = False
        self._spawn_audio(ff)
        if not self.audio_only:
            self._spawn_video(ff)

    # ------------------------------------------------------------------
    # Sources: the plain URL, or a yt-dlp section download when seeking
    # ------------------------------------------------------------------
    def _ytdlp_boot(self):
        """Prefix that runs the bundled yt-dlp package as a CLI."""
        from .yt import ytdlp_zip_path
        zip_path = ytdlp_zip_path()
        py = sys.executable or "python3"
        boot = ("import sys; sys.path.insert(0, %r); import yt_dlp; "
                "sys.exit(yt_dlp.main())" % zip_path)
        return [py, "-c", boot]

    def _section_cmd(self, kind, ss):
        """yt-dlp command that writes the stream from *ss* onwards to stdout."""
        if not self._info_json or not os.path.exists(self._info_json):
            return None
        itag = _url_itag(self._audio_url if kind == "a" else self._video_url)
        return self._ytdlp_boot() + [
            "--no-warnings", "--quiet", "--no-playlist",
            "--load-info-json", self._info_json,
            "--download-sections", "*%.1f-" % ss,
            "-f", str(itag), "--output", "-"]

    def _section_err(self, kind):
        try:
            return open("/tmp/rh_section_%s.log" % kind, "ab")
        except Exception:
            return subprocess.DEVNULL

    def _section_err_tail(self, kind):
        try:
            with open("/tmp/rh_section_%s.log" % kind, "r", errors="ignore") as f:
                txt = f.read().strip()
            if txt:
                _log("yt-dlp %s stderr: %s" % (kind, txt[-400:]))
        except Exception:
            pass

    def _spawn_feeder(self, url, proc, kind, ss):
        t = threading.Thread(target=self._feed_stream, args=(url, proc, kind, ss),
                             daemon=True)
        t.start()
        self._threads.append(t)

    def _feed_stream(self, url, proc, kind, ss):
        """Copy one stream into ffmpeg's stdin.

        ss <= 0: fetch the resolved URL. That is the whole stream, and it is paced
        by playback because the readers only pull what they can use.
        ss  > 0: let yt-dlp download *that section*. It knows the fragment byte
        ranges, so this is a real seek: nothing is re-read, and ffmpeg never gets
        `-ss` on a pipe (which feeds a misaligned bitstream and decodes nothing).
        """
        total = 0
        src = None
        section = None
        try:
            cmd = self._section_cmd(kind, ss) if ss > 0.05 else None
            if cmd:
                _log("feed %s: section from %.1fs (itag %s)" %
                     (kind, ss, _url_itag(url)))
                section = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                           stderr=self._section_err(kind), bufsize=0)
                self._dl_procs[kind] = section
                reader = section.stdout
            else:
                if ss > 0.05:
                    _log("feed %s: yt-dlp info not ready, falling back to the full "
                         "URL from the start" % kind)
                import ssl as _ssl
                import urllib.request as _urlreq
                ctx = _ssl._create_unverified_context()
                req = _urlreq.Request(url, headers={"User-Agent": _UA})
                src = _urlreq.urlopen(req, timeout=20, context=ctx)
                self._url_src[kind] = src
                reader = src
            _log("feed %s start" % kind)
            while not self._stop.is_set():
                if self.paused:
                    time.sleep(0.02)
                    continue
                chunk = reader.read(READ_CHUNK)
                if not chunk:
                    break
                try:
                    proc.stdin.write(chunk)
                    total += len(chunk)
                except Exception:
                    break
        except Exception as e:
            _log("feed %s error: %s" % (kind, e))
            if section:
                self._section_err_tail(kind)
        finally:
            try:
                if proc.stdin:
                    proc.stdin.close()
            except Exception:
                pass
            try:
                if section and section.poll() is None:
                    section.kill()
            except Exception:
                pass
            self._dl_procs.pop(kind, None)
            _log("feed %s done bytes=%d" % (kind, total))

    # ------------------------------------------------------------------
    # ffmpeg processes
    # ------------------------------------------------------------------
    def _spawn_audio(self, ff, ss=None):
        ss = self._start_pos if ss is None else ss
        self._audio_ss = ss
        cmd = [ff, "-hide_banner", "-loglevel", "error", "-f", "mp4", "-i", "pipe:0", "-vn"]
        if abs(self.speed - 1.0) > 0.01:
            cmd += ["-af", "atempo=%.3f" % self.speed]
        cmd += ["-f", "s16le", "-ar", str(AUDIO_RATE), "-ac", str(AUDIO_CHANNELS), "pipe:1"]
        _log("audio: ffmpeg -f mp4 -i pipe:0 itag=%s from=%.1fs" %
             (_url_itag(self._audio_url), ss))
        try:
            self.audio_err = open("/tmp/rh_ffmpeg_a.log", "ab")
        except Exception:
            self.audio_err = subprocess.DEVNULL
        self.audio_proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=self.audio_err, bufsize=0)
        self._spawn_feeder(self._audio_url, self.audio_proc, "a", ss)
        t = threading.Thread(target=self._audio_loop, daemon=True)
        t.start()
        self._threads.append(t)

    def _spawn_video(self, ff, ss=None):
        ss = self._start_pos if ss is None else ss
        self._video_ss = ss
        vf = "fps=%d,scale=%d:%d:flags=fast_bilinear" % (FPS, self._w, self._h)
        if abs(self.speed - 1.0) > 0.01:
            vf = "setpts=PTS/%.3f,%s" % (self.speed, vf)
        cmd = [ff, "-hide_banner", "-loglevel", "error", "-f", "mp4", "-i", "pipe:0",
               "-an", "-vf", vf, "-pix_fmt", self._pix_fmt,
               "-f", "rawvideo", "pipe:1"]
        _log("video: ffmpeg -f mp4 -i pipe:0 itag=%s size=%dx%d pix_fmt=%s from=%.1fs" %
             (_url_itag(self._video_url), self._w, self._h, self._pix_fmt, ss))
        try:
            self.video_err = open("/tmp/rh_ffmpeg_v.log", "ab")
        except Exception:
            self.video_err = subprocess.DEVNULL
        self.video_proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=self.video_err, bufsize=0)
        self._spawn_feeder(self._video_url, self.video_proc, "v", ss)
        t = threading.Thread(target=self._video_loop, daemon=True)
        t.start()
        self._threads.append(t)

    def _kill_procs(self):
        for p in (self.video_proc, self.audio_proc):
            try:
                if p and p.stdin:
                    p.stdin.close()
            except Exception:
                pass
        for p in (self.video_proc, self.audio_proc):
            try:
                if p and p.poll() is None:
                    p.kill()
            except Exception:
                pass
        self.video_proc = None
        self.audio_proc = None
        for kind in ("a", "v"):
            p = self._dl_procs.pop(kind, None)
            try:
                if p and p.poll() is None:
                    p.kill()
            except Exception:
                pass
            s = self._url_src.pop(kind, None)
            try:
                if s:
                    s.close()
            except Exception:
                pass
        for h in (self.audio_err, self.video_err):
            try:
                if hasattr(h, "close"):
                    h.close()
            except Exception:
                pass
        self.audio_err = None
        self.video_err = None
        alive = []
        for t in self._threads:
            try:
                t.join(timeout=1.0)
            except Exception:
                pass
            if t.is_alive():
                alive.append(t)
        self._threads = alive

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
        frame_bytes = self._w * self._h * 4   # rgba
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
                pts = self._video_ss + idx / float(FPS)
                with self._frame_lock:
                    self._frames.append((pts, data))
                self.decode_pos = pts * self.speed
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
    def _local_clock(self):
        """Seconds of decoded output since the last spawn (0-based)."""
        try:
            queued = sdl_audio.SDL_GetQueuedAudioSize(self.audio_dev)
        except Exception:
            queued = 0
        played = max(0, self._audio_fed - queued)
        return played / float(AUDIO_BYTES_PER_SEC)

    def update(self):
        """Advance the clock and upload the next due video frame."""
        if self._pending_spawn:
            # Waiting for the metadata a section-based resume needs. Give it a
            # deadline so a failed extraction cannot hold playback forever.
            if (self._info_json or self._info_json_failed
                    or time.time() - self._spawn_time > 15.0):
                _log("resume: metadata %s, starting decoders" %
                     ("ready" if self._info_json else "unavailable"))
                self._spawn_decoders()
            return
        if self.paused:
            return
        # Audio is the master clock: `_local_clock` counts the audio queued since
        # the decoders were (re)started, i.e. content seconds / speed (atempo
        # compresses the output). The position adds the seek offset, and video
        # frames - whose timestamps are stream content seconds scaled by setpts
        # when speed != 1 - are compared against it.
        self._local_pos = self._local_clock()
        self.position = self._start_pos + self._local_pos * self.speed
        # Nothing on screen yet: log the decoder stderr once so a bad source is
        # distinguishable from a slow one.
        now = time.time()
        if self._hold_audio and self._hold_since and now - self._hold_since > HOLD_MAX:
            # No picture yet (slow download / position not downloaded): let the
            # sound go rather than hanging silently on the held device.
            self._hold_audio = False
            try:
                sdl_audio.SDL_PauseAudioDevice(self.audio_dev, 0)
            except Exception:
                pass
            _log("audio: hold released after %.0fs without a picture" % HOLD_MAX)
        if self.audio_only or not self.texture:
            return
        clock_pos = self.position
        due = None
        with self._frame_lock:
            while self._frames and (self._frames[0][0] * self.speed) < clock_pos - SYNC_DROP:
                self._frames.popleft()
                self._video_drop += 1
            if self._frames and (self._frames[0][0] * self.speed) <= clock_pos + SYNC_HOLD:
                due = self._frames.popleft()[1]
        if due is not None:
            ok = False
            try:
                rc = sdl2.SDL_UpdateTexture(self.texture, None,
                                            ctypes.c_char_p(due), self._w * 4)
                ok = (rc == 0)
                if not self._got_upload:
                    self._got_upload = True
                    nz = 0
                    for i in range(0, min(16000, len(due)), 4):
                        if due[i] or due[i + 1] or due[i + 2]:
                            nz += 1
                    _log("video: first %s upload rc=%s ok=%s clock=%.2f "
                         "px0=%s mid=%s rgb_nz=%d err=%r" %
                         (self._pix_fmt, rc, ok, clock_pos, due[:4].hex(),
                          due[len(due) // 2:len(due) // 2 + 4].hex(), nz,
                          sdl2.SDL_GetError().decode()))
            except Exception as e:
                if not self._upload_err_logged:
                    self._upload_err_logged = True
                    _log("video: RGBA upload error: %s" % e)
            if ok:
                self._video_up += 1
                if self.seek_note:
                    # The first frame at/after the seek target is on screen: the
                    # seek is done, stop telling the user it is still working.
                    self.seek_note = ""
                if self._hold_audio:
                    # Picture has caught up: let the sound start here too.
                    self._hold_audio = False
                    try:
                        sdl_audio.SDL_PauseAudioDevice(self.audio_dev, 0)
                    except Exception:
                        pass
                    _log("audio: released after catch-up")
                if self._video_up % 300 == 0:
                    _log("video: up=%d drop=%d pos=%.2f queued=%d" %
                         (self._video_up, self._video_drop, clock_pos, len(self._frames)))

    def produced_data(self) -> bool:
        """True once any audio or video bytes arrived (i.e. it actually played)."""
        return self._got_audio or self._got_video

    def has_uploaded(self) -> bool:
        """True once at least one video frame reached the texture."""
        return self._video_up > 0

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
            if not self.paused and self._hold_audio:
                # Still catching up after a seek: the release path owns the device.
                return
            sdl_audio.SDL_PauseAudioDevice(self.audio_dev, 1 if self.paused else 0)
        except Exception:
            pass

    def set_volume(self, volume):
        self.volume = max(0, min(100, int(volume)))

    def set_speed(self, speed):
        new = max(0.5, min(2.0, float(speed)))
        if abs(new - self.speed) < 0.01:
            return
        pos = self.get_position()
        self.speed = new
        _log("speed %.2fx -> restart at %.1fs" % (self.speed, pos))
        self.seek(pos)

    def get_position(self) -> float:
        return self.position

    def get_duration(self) -> float:
        return self.duration
