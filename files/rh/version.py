# -*- coding: utf-8 -*-
"""Single source of truth for the build number.

The build script reads APP_VERSION from here to name the release, and the
updater compares it against the version in the published manifest, so the
constant must stay in sync with the git tag."""

import re

APP_VERSION = "3.14"

# Pre-release tags sort before the final release of the same number.
_TAG_ORDER = {"alpha": 0, "a": 0, "beta": 1, "b": 1, "rc": 2}


def version_tuple(v=None):
    """Split a version string into a sortable tuple.

    Handles plain numeric versions and pre-release suffixes, so "3.2-rc1"
    sorts after "3.1" but before "3.2". Anything unparsable sorts lowest, which
    makes a malformed manifest look older than the running build instead of
    triggering a bogus update."""
    s = str(v or APP_VERSION).strip().lstrip("vV")
    m = re.match(r"(\d+(?:\.\d+)*)(.*)$", s)
    if not m:
        return (0,)
    nums = [int(p) for p in m.group(1).split(".")]
    while len(nums) > 1 and nums[-1] == 0:
        nums.pop()
    suffix = m.group(2).strip(" -_.")
    if not suffix:
        return tuple(nums) + (1,)  # final release of this number
    sm = re.match(r"([a-zA-Z]+)(\d*)", suffix)
    tag = sm.group(1).lower() if sm else suffix.lower()
    num = int(sm.group(2)) if (sm and sm.group(2)) else 0
    return tuple(nums) + (-1, _TAG_ORDER.get(tag, 3), num)


def is_newer(remote, local=None):
    """True when *remote* is a strictly later version than *local*."""
    return version_tuple(remote) > version_tuple(local)
