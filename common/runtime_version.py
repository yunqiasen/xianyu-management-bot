"""One source identity for Web, scheduler and message worker."""
from functools import lru_cache
from pathlib import Path
import os
import subprocess

REPOSITORY = "https://github.com/yunqiasen/xianyu-management-bot"

@lru_cache(maxsize=1)
def build_identity() -> dict[str, str]:
    commit = os.environ.get("XYMB_BUILD_COMMIT", "").strip()
    dirty = False
    if not commit:
        root = Path(__file__).resolve().parents[1]
        try:
            commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, stderr=subprocess.DEVNULL, text=True, timeout=2).strip()
            dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=root, stderr=subprocess.DEVNULL, text=True, timeout=2).strip())
        except (OSError, subprocess.SubprocessError):
            commit = "unknown"
    suffix = "-dirty" if dirty else ""
    return {"version": f"xianyu-management-bot+{commit[:12]}{suffix}", "commit": commit, "repository": REPOSITORY}
