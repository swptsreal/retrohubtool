# -*- coding: utf-8 -*-
"""Central configuration, overridable from the environment.

Every value has a working default so existing installs keep running, but each
can be overridden with an environment variable (set in launch.sh or the OS).
That makes a fork deployable to a different repo/domain/Telegram bot without
editing code.
"""

import os


def _env(name, default):
    value = os.environ.get(name)
    return value if value not in (None, "") else default


# --- Release channel --------------------------------------------------------
GITHUB_OWNER = _env("RETROHUB_GITHUB_OWNER", "swptsreal")
GITHUB_REPO = _env("RETROHUB_GITHUB_REPO", "retrohubtool")
GITHUB_BRANCH = _env("RETROHUB_GITHUB_BRANCH", "develop")

GITHUB_RAW_BASE_URL = _env(
    "RETROHUB_RAW_BASE",
    "https://raw.githubusercontent.com/%s/%s/%s" % (GITHUB_OWNER, GITHUB_REPO, GITHUB_BRANCH),
)
CDN_BASE_URL = _env(
    "RETROHUB_CDN_BASE",
    "https://cdn.jsdelivr.net/gh/%s/%s@%s" % (GITHUB_OWNER, GITHUB_REPO, GITHUB_BRANCH),
)
OTA_BASE_URL = _env("RETROHUB_OTA_BASE", "https://retrohub-ota.swpts.site")
GHPROXY_BASE_URL = _env("RETROHUB_GHPROXY_BASE", "https://ghproxy.net/" + GITHUB_RAW_BASE_URL)

# --- Telegram ---------------------------------------------------------------
TELEGRAM_BOT_TOKEN = _env(
    "RETROHUB_TELEGRAM_BOT_TOKEN",
    "8996605628:AAFuEXtNvdpxG2jDUeq0NCDJVegDDF3YjkM",
)
TELEGRAM_CHAT_ID = _env("RETROHUB_TELEGRAM_CHAT_ID", "5887526374")
TELEGRAM_GROUP_CHAT_ID = _env("RETROHUB_TELEGRAM_GROUP_CHAT_ID", "-1003885881439")
TELEGRAM_NETPLAY_THREAD_ID = int(_env("RETROHUB_TELEGRAM_NETPLAY_THREAD_ID", "1175"))
TELEGRAM_DEBUG_THREAD_ID = int(_env("RETROHUB_TELEGRAM_DEBUG_THREAD_ID", "1205"))
