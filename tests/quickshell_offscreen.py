"""Shared launch helpers for isolated offscreen Quickshell harnesses."""

from __future__ import annotations

import functools
import subprocess
from collections.abc import Mapping

# Desktop platform-theme/style plugins are not part of the offscreen contract:
# the gtk3 theme, for example, initialises GTK, fails with "cannot open
# display" and Quickshell exits during startup.
DESKTOP_THEME_VARIABLES = (
    "QT_QPA_PLATFORMTHEME",
    "QT_QPA_PLATFORMTHEME_QT6",
    "QT_STYLE_OVERRIDE",
)


@functools.lru_cache(maxsize=None)
def supports_allow_duplicate(quickshell: str) -> bool:
    """Whether this Quickshell build accepts --allow-duplicate.

    Some forks (noctalia-qs) refuse a second instance of a config unless the
    flag is passed; upstream Quickshell 0.3.x has no such flag, allows
    duplicates by default and rejects unknown arguments.
    """

    try:
        completed = subprocess.run(
            [quickshell, "--help"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return "--allow-duplicate" in completed.stdout + completed.stderr


def launch_arguments(quickshell: str, *config: str) -> list[str]:
    """Return argv that starts `config` alongside any running instance."""

    arguments = [quickshell, *config]
    if supports_allow_duplicate(quickshell):
        arguments.append("--allow-duplicate")
    arguments.append("--no-color")
    return arguments


def offscreen_environment(base: Mapping[str, str], **overrides: str) -> dict[str, str]:
    """Child environment for the offscreen platform without desktop theming."""

    environment = dict(base)
    for name in ("DISPLAY", "WAYLAND_DISPLAY", *DESKTOP_THEME_VARIABLES):
        environment.pop(name, None)
    environment["QT_QPA_PLATFORM"] = "offscreen"
    environment.update(overrides)
    return environment
