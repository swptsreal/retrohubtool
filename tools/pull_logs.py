#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Pull RetroHub logs from the device over SSH - no SD-card removal.

Usage:
    python tools/pull_logs.py [user@]host[:port]      # default root@192.168.100.115

It tars the known log files on the device, streams them here and extracts into
logs/device_<timestamp>/. Requires:
  - the device SSH server to be ON (RetroHub -> Network -> SSH Server), and
  - an OpenSSH client (ssh) on this machine (you will be asked for the password,
    default root/root).
"""

import datetime
import os
import shutil
import subprocess
import sys
import tarfile

# Log files worth collecting (missing ones are ignored by the device tar).
DEVICE_LOGS = [
    "/mnt/SDCARD/RetroHub-yt.log",
    "/mnt/SDCARD/RetroHub-java.log",
    "/mnt/SDCARD/RetroHub-loi.txt",
    "/mnt/SDCARD/RetroHub_Debug_Report.txt",
    "/mnt/SDCARD/RetroHub/logs",
    "/tmp/rh_ffmpeg_a.log",
    "/tmp/rh_ffmpeg_v.log",
    "/tmp/retrohub_yt.log",
    "/tmp/yt_last_error.txt",
]


def main():
    raw = sys.argv[1] if len(sys.argv) > 1 else "root@192.168.100.115"
    user = "root"
    if "@" in raw:
        user, raw = raw.split("@", 1)
    port = "22"
    if ":" in raw:
        raw, port = raw.split(":", 1)
    host = raw
    target = "%s@%s" % (user, host)

    if not shutil.which("ssh"):
        print("[-] OpenSSH client 'ssh' not found on this machine.")
        return 1

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    outdir = os.path.join(root, "logs")
    os.makedirs(outdir, exist_ok=True)

    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    tgz = os.path.join(outdir, "retrohub_logs_%s_%s.tar.gz" % (host, ts))
    extract_dir = os.path.join(outdir, "device_%s" % ts)

    remote_cmd = "tar czf - " + " ".join(DEVICE_LOGS) + " 2>/dev/null"
    ssh = [
        "ssh",
        "-p", port,
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "ConnectTimeout=8",
        target,
        remote_cmd,
    ]

    print("[*] ssh -p %s %s  ->  %s" % (port, target, tgz))
    with open(tgz, "wb") as fh:
        proc = subprocess.run(ssh, stdout=fh)
    if proc.returncode != 0 or os.path.getsize(tgz) == 0:
        print("[-] Failed. Check: SSH server is ON, host/IP is right, password root.")
        try:
            os.remove(tgz)
        except OSError:
            pass
        return 1

    print("[+] Downloaded %d bytes" % os.path.getsize(tgz))
    os.makedirs(extract_dir, exist_ok=True)
    try:
        with tarfile.open(tgz, "r:gz") as tar:
            try:
                tar.extractall(extract_dir, filter="data")
            except TypeError:
                tar.extractall(extract_dir)
    except Exception as e:
        print("[!] Saved tarball but could not extract: %s" % e)
        print("    Open it manually: %s" % tgz)
        return 0

    print("[+] Extracted to %s" % extract_dir)
    for base, _dirs, files in os.walk(extract_dir):
        for fn in files:
            p = os.path.join(base, fn)
            print("    %8d  %s" % (os.path.getsize(p), os.path.relpath(p, extract_dir)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
