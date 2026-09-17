#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""On-device diagnostic for the in-app player's seek strategy.

Runs from the Apps menu (see launch.sh next to this file) because the handheld
has no console and SFTP cannot execute anything. Everything is written to
/mnt/SDCARD/rh_test_report.txt so it can be read back over SFTP.

What it answers: can the device's ffmpeg seek inside a *downloaded* file with
`-ss`? The pipe-based path cannot seek at all (it produced "Invalid NAL unit
size" and zero frames), so a file-backed strategy is only worth building if a
plain `-ss 30` on a local file decodes frames.
"""

import os
import subprocess
import sys
import time
import urllib.request

SDCARD = os.environ.get("SDCARD_PATH", "/mnt/SDCARD")
APP = os.path.join(SDCARD, "Apps", "RetroHub")
CACHE = os.path.join(SDCARD, ".retrohub", "seektest")
VIDEO_ID = os.environ.get("RH_TEST_VIDEO", "dQw4w9WgXcQ")
QUALITY = "360"
MAX_BYTES = 3 * 1024 * 1024       # enough for the seeks below, quick to wait for
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

sys.path.insert(0, APP)
from rh import yt            # noqa: E402
from rh import inapp_player as ip  # noqa: E402
import ssl as _ssl           # noqa: E402


def line(msg):
    print(msg, flush=True)


def download(url, path, limit=MAX_BYTES):
    got = 0
    ctx = _ssl._create_unverified_context()
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with open(path, "wb") as f, urllib.request.urlopen(req, timeout=20, context=ctx) as resp:
        while got < limit:
            chunk = resp.read(262144)
            if not chunk:
                break
            f.write(chunk)
            got += len(chunk)
    return got


def run_ffmpeg(args, timeout=45):
    t0 = time.time()
    try:
        p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        rc = p.returncode
        err = p.stderr.decode("utf-8", "ignore")
    except subprocess.TimeoutExpired:
        rc, err = "timeout", ""
    took = time.time() - t0
    frames = ""
    for l in err.splitlines():
        if "frame=" in l:
            frames = l.strip()[:120]
    head = " | ".join(err.strip().splitlines()[:3])[:300]
    return rc, took, frames, head


def main():
    ff = ip.find_ffmpeg()
    line("ffmpeg: %s" % ff)
    line("h264 decoder: %s | http protocol: %s" % (ip.has_h264_decoder(), ip.has_http_protocol()))
    if not ff:
        return
    rc, took, frames, head = run_ffmpeg([ff, "-hide_banner", "-version"], timeout=20)
    line("version: %s" % (head or "?"))

    line("")
    line("resolve_streams(%s, %s)" % (VIDEO_ID, QUALITY))
    t0 = time.time()
    res = yt.resolve_streams(VIDEO_ID, QUALITY)
    line("  took %.1fs -> %s" % (time.time() - t0, bool(res)))
    if not res:
        return
    line("  title=%r height=%s progressive=%s" %
         (res.get("title"), res.get("height"), res.get("progressive")))
    for k in ("video_url", "audio_url"):
        v = res.get(k) or ""
        line("  %s: %s..." % (k, v[:110]))

    os.makedirs(CACHE, exist_ok=True)
    vpath = os.path.join(CACHE, "video.mp4")
    apath = os.path.join(CACHE, "audio.mp4")
    if not (os.path.exists(vpath) and os.path.getsize(vpath) > 1024 * 1024):
        line("")
        line("downloading video (max %dMB)..." % (MAX_BYTES // (1024 * 1024)))
        n = download(res.get("video_url"), vpath)
        line("  got %d bytes" % n)
    else:
        line("reusing %s (%d bytes)" % (vpath, os.path.getsize(vpath)))
    vbytes = os.path.getsize(vpath)
    line("file size: %d bytes" % vbytes)

    line("")
    line("A) file + -ss 30 (this is what a file-backed seek would use)")
    rc, took, frames, head = run_ffmpeg(
        [ff, "-hide_banner", "-ss", "30", "-i", vpath, "-an", "-f", "null", "-"])
    line("   rc=%s took=%.1fs %s" % (rc, took, frames))
    line("   stderr: %s" % head)

    line("B) file without -ss (baseline)")
    rc, took, frames, head = run_ffmpeg(
        [ff, "-hide_banner", "-i", vpath, "-an", "-f", "null", "-"])
    line("   rc=%s took=%.1fs %s" % (rc, took, frames))
    line("   stderr: %s" % head)

    line("C) file + -ss 60")
    rc, took, frames, head = run_ffmpeg(
        [ff, "-hide_banner", "-ss", "60", "-i", vpath, "-an", "-f", "null", "-"])
    line("   rc=%s took=%.1fs %s" % (rc, took, frames))
    line("   stderr: %s" % head)

    line("")
    line("done")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        line("FAILED: %r" % e)
        traceback.print_exc(file=sys.stdout)
