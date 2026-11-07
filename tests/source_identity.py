"""Identify the source an owner-run measurement exercised.

Every manual evidence file carries ``tested_source``: the Git commit of the
code that was measured and whether it had uncommitted changes. The release
pipeline accepts the file only when that commit is the release commit or
differs from it in no path the evidence depends on.
"""

from __future__ import annotations

import json
import pathlib
import subprocess

INSTALLED_SOURCE = pathlib.Path.home() / ".local/lib/dank-lookup/installed-source.json"


def checkout_source(root: pathlib.Path) -> dict[str, object] | None:
    """The commit of the checkout a script runs from, or None outside Git."""

    def git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args], cwd=root, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, check=False,
        )

    head = git("rev-parse", "HEAD")
    status = git("status", "--porcelain")
    if head.returncode != 0 or status.returncode != 0:
        return None
    return {
        "origin": "checkout",
        "commit": head.stdout.strip(),
        "uncommitted_changes": any(
            line and not line.startswith("??") for line in status.stdout.splitlines()
        ),
    }


def installed_source(path: pathlib.Path = INSTALLED_SOURCE) -> dict[str, object] | None:
    """The commit install-user.sh recorded for the installed copy, or None."""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or not isinstance(value.get("commit"), str):
        return None
    return {
        "origin": "installed",
        "commit": value["commit"],
        "uncommitted_changes": value.get("uncommitted_changes") is not False,
    }
