#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Headless YouTube decode check for the in-app player (runs ON the device).

The in-app player draws into an SDL renderer, so its video path can only be
tested with a display. This script exercises everything except the GL upload:
it resolves the streams with the app's own rh.yt, drives the real ffmpeg
commands from rh.inapp_player and reports whether frames actually come out.
That separates "ffmpeg/pipe produces nothing" (decode side) from "the renderer
does not draw the texture" (probe/render side).

Usage on the device:
    python3 /tmp/yt_decode_check.py <video_id> [quality] [pix_fmt] [ss_seconds]
Examples:
    python3 /tmp/yt_decode_check.py dQw4w9WgXcQ 360 rgba
    python3 /tmp/yt_decode_check.py dQw4w9WgXcQ 360 rgba 60
"""

import os
import subprocess
import sys
import threading
import time
import urllib.request

APP_DIR = os.environ.get("APP_DIR", "/mnt/SDCARD/Apps/RetroHub")
sys.path.insert(0, APP_DIR)

video_id = sys.argv[1] if len(sys.argv) > 1 else "dQw4w9WgXcQ"
quality = sys.argv[2] if len(sys.argv) > 2 else "360"
pix_fmt = sys.argv[3] if len(sys.argv) > 3 else "rgba"
start_pos = float(sys.argv[4]) if len(sys.argv) > 4 else 0.0

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def section(title):
    print("\n=== %s ===" % title)


from rh import yt  # noqa: E402  (APP_DIR must be on sys.path first)
from rh import inapp_player as ip  # noqa: E402
import ssl as _ssl  # noqa: E402

section("1. ffmpeg")
ff = ip.find_ffmpeg()
print("ffmpeg:", ff)
if not ff:
    sys.exit("no ffmpeg found")
try:
    pix = subprocess.run([ff, "-hide_banner", "-pix_fmts"], stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, timeout=15).stdout.decode("utf-8", "ignore")
    for want in ("rgba", "bgra", "argb", "abgr", "yuv420p"):
        print("  pix_fmt %-8s %s" % (want, "yes" if want in pix else "NO"))
except Exception as e:
    print("  pix_fmts failed:", e)
print("  h264 decoder:", ip.has_h264_decoder())
print("  http protocol:", ip.has_http_protocol())

section("2. resolve_streams (rh.yt + yt-dlp bootstrap + cache)")
t0 = time.time()
res = yt.resolve_streams(video_id, quality)
print("  took %.1fs" % (time.time() - t0))
if not res:
    sys.exit("resolve_streams returned None")
for k in ("video_url", "audio_url", "title", "height", "progressive"):
    v = res.get(k)
    if isinstance(v, str) and len(v) > 70:
        v = v[:70] + "..."
    print("  %-12s %s" % (k, v))
t1 = time.time()
again = yt.resolve_streams(video_id, quality)
print("  second call (cache): %s in %.2fs" % (bool(again), time.time() - t1))


def feed(url, proc):
    total = 0
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        ctx = _ssl._create_unverified_context()
        with urllib.request.urlopen(req, timeout=20, context=ctx) as resp:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                try:
                    proc.stdin.write(chunk)
                    total += len(chunk)
                except Exception:
                    break
    except Exception as e:
        print("  feed error:", e)
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
    print("  fed %d bytes" % total)


section("3. video pipeline (same command as _spawn_video, pix_fmt=%s, ss=%.1f)" %
        (pix_fmt, start_pos))
p = ip.InAppPlayer(None)          # no renderer: only the ffmpeg/pipe half runs
p._w, p._h = ip.target_size(quality)
p._pix_fmt = pix_fmt
p._video_url = res.get("video_url")
p._start_pos = start_pos
print("  size: %dx%d" % (p._w, p._h))
p._spawn_video(ff)
deadline = time.time() + 20
while time.time() < deadline and not p._got_video and p._video_read < 45:
    time.sleep(0.5)
p._stop.set()
p._kill_procs()
print("  frames read: %d | got_video=%s | video_done=%s" %
      (p._video_read, p._got_video, p._video_done))
frame = None
try:
    with p._frame_lock:
        if p._frames:
            frame = p._frames[0][1]
except Exception:
    pass
if frame:
    nz = sum(1 for i in range(0, min(60000, len(frame)), 4)
             if frame[i] or frame[i + 1] or frame[i + 2])
    print("  first frame: %d bytes, non-zero pixels in first 15000 px: %d" %
          (len(frame), nz))
    print("  first pixel bytes:", frame[:4].hex())
for path in ("/tmp/rh_ffmpeg_v.log",):
    if os.path.exists(path):
        with open(path, "r", errors="ignore") as f:
            tail = f.read()[-600:]
        print("  %s tail:\n%s" % (path, tail))

section("4. audio pipeline (same command as _spawn_audio)")
cmd = ["-hide_banner", "-loglevel", "error"]
if start_pos > 0.05:
    cmd += ["-ss", "%.3f" % start_pos]
cmd += ["-f", "mp4", "-i", "pipe:0", "-vn", "-f", "s16le",
        "-ar", str(ip.AUDIO_RATE), "-ac", str(ip.AUDIO_CHANNELS), "pipe:1"]
proc = subprocess.Popen([ff] + cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, bufsize=0)
t = threading.Thread(target=feed, args=(res.get("audio_url") or res.get("video_url"), proc))
t.daemon = True
t.start()
got = 0
chunks = 0
while chunks < 5:
    data = proc.stdout.read(65536)
    if not data:
        break
    got += len(data)
    chunks += 1
proc.kill()
t.join(timeout=2)
print("  audio bytes: %d (%.2f s of audio)" % (got, got / float(ip.AUDIO_BYTES_PER_SEC)))
try:
    err = proc.stderr.read().decode("utf-8", "ignore")[-400:]
    if err.strip():
        print("  ffmpeg stderr:", err)
except Exception:
    pass

print("\n[+] done")
