#!/usr/bin/env python3
"""Render the lookup card offscreen to PNG files for visual review.

Each scenario is a lookup result JSON file (for example real backend output);
it is injected through the controller's mock IPC into the real
LookupController/LookupCard/LookupTheme, inside a temporary Quickshell config
with an isolated HOME. Nothing reads a clipboard or selection, and the
generated config never leaves the temporary directory.

    python3 tests/render_card_screenshots.py --output DIR \
        --scenario spring=/path/spring.json --variant dark --variant light
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time

from quickshell_offscreen import launch_arguments, offscreen_environment


ROOT = pathlib.Path(__file__).resolve().parent.parent
TARGET = "dank-lookup-render"

# The backdrop only makes the translucent card and its shadow visible in a PNG.
RENDER_SHELL = """
import QtQuick
import Quickshell
import Quickshell.Io
import "qml" as DankLookup

ShellRoot {
    id: shell

    DankLookup.LookupTheme {
        id: theme
        requestedWidth: Number(Quickshell.env("DANK_LOOKUP_CARD_WIDTH") || 0)
        requestedHeight: Number(Quickshell.env("DANK_LOOKUP_CARD_HEIGHT") || 0)
        dark: (Quickshell.env("DANK_LOOKUP_THEME_VARIANT") || "dark") !== "light"
    }

    DankLookup.MatugenThemeAdapter {
        theme: theme
        path: Quickshell.env("DANK_LOOKUP_THEME_FILE") || ""
    }

    DankLookup.LookupController {
        id: controller
        ipcTarget: "%(target)s"
        windowVisible: true
    }

    FloatingWindow {
        visible: true
        implicitWidth: theme.windowWidth + 2 * theme.shadowPadding
        implicitHeight: theme.windowHeight + 2 * theme.shadowPadding
        color: "transparent"

        Rectangle {
            id: frame
            anchors.fill: parent
            color: theme.dark ? "%(dark_backdrop)s" : "%(light_backdrop)s"

            DankLookup.LookupCard {
                anchors.fill: parent
                controller: controller
                theme: theme
            }
        }

        IpcHandler {
            target: "%(target)s-grab"

            function grab(path: string): string {
                frame.grabToImage(function(result) {
                    result.saveToFile(path)
                })
                return "started"
            }
        }
    }
}
"""


def slim(result):
    """Drop fields the card never draws; large mock IPC payloads stall.

    Entries stay (the card draws them as sections), trimmed to the fields the
    card reads; their bodies are already bounded previews.
    """

    def entry(value):
        return {key: value.get(key) for key in ("word", "source", "body", "runs") if key in value}

    def mode_body(body):
        kept = {key: item for key, item in body.items() if key not in {"entries", "full_body"}}
        if body.get("entries"):
            kept["entries"] = [entry(item) for item in body["entries"]]
        return kept

    def mode_map(value):
        return {mode: mode_body(body) for mode, body in (value or {}).items()}

    result = dict(result)
    for key in ("results_by_mode", "related_by_mode"):
        result[key] = mode_map(result.get(key))
    result["results_by_token"] = {
        token: {**value, "results_by_mode": mode_map(value.get("results_by_mode")),
                "related_by_mode": mode_map(value.get("related_by_mode"))}
        for token, value in (result.get("results_by_token") or {}).items()
    }
    return result


def ipc(quickshell, pid, env, *arguments):
    return subprocess.run(
        [quickshell, "ipc", "--pid", str(pid), "call", *arguments],
        capture_output=True,
        text=True,
        env=env,
        timeout=20,
        check=False,
    )


def wait_for(predicate, timeout, description):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise RuntimeError(f"timed out waiting for {description}")


def render(quickshell, scenarios, variant, output, timeout, theme_file=None, label=None):
    with tempfile.TemporaryDirectory(prefix="dank-lookup-render-") as temporary:
        temp = pathlib.Path(temporary)
        app = temp / "app"
        app.mkdir(mode=0o700)
        shutil.copytree(ROOT / "qml", app / "qml")
        (app / "shell.qml").write_text(
            RENDER_SHELL % {"target": TARGET, "dark_backdrop": "#3a3a3a",
                            "light_backdrop": "#d8d8d8"},
            encoding="utf-8",
        )
        home = temp / "home"
        runtime = temp / "runtime"
        home.mkdir(mode=0o700)
        runtime.mkdir(mode=0o700)
        env = offscreen_environment(
            os.environ,
            HOME=str(home),
            XDG_CACHE_HOME=str(home / ".cache"),
            XDG_STATE_HOME=str(home / ".local/state"),
            XDG_RUNTIME_DIR=str(runtime),
            DANK_LOOKUP_ENABLE_MOCK_IPC="1",
            DANK_LOOKUP_THEME_VARIANT=variant,
            NO_COLOR="1",
        )
        env.pop("DANK_LOOKUP_THEME_FILE", None)
        if theme_file is not None:
            env["DANK_LOOKUP_THEME_FILE"] = str(theme_file)
        label = label or variant
        log_path = output / f"quickshell-{label}.log"
        with log_path.open("wb") as log:
            process = subprocess.Popen(
                launch_arguments(quickshell, "-p", str(app)),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                env=env,
            )
            try:
                wait_for(
                    lambda: process.poll() is None
                    and ipc(quickshell, process.pid, env, TARGET, "status").returncode == 0,
                    timeout,
                    "render shell IPC",
                )
                for name, path in scenarios:
                    payload = json.dumps(slim(json.loads(path.read_text(encoding="utf-8"))),
                                         ensure_ascii=False, separators=(",", ":"))
                    reply = ipc(quickshell, process.pid, env, TARGET, "mockResult", payload)
                    if reply.stdout.strip().removeprefix("Ret:").strip().strip('"') != "ok":
                        raise RuntimeError(f"{name}: mockResult failed: {reply.stdout}{reply.stderr}")
                    time.sleep(0.5)
                    target = output / f"{name}-{label}.png"
                    target.unlink(missing_ok=True)
                    ipc(quickshell, process.pid, env, f"{TARGET}-grab", "grab", str(target))
                    wait_for(lambda: target.exists() and target.stat().st_size > 0,
                             timeout, f"{target.name}")
                    print(target)
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    parser.add_argument("--scenario", action="append", required=True, metavar="NAME=JSON")
    parser.add_argument("--variant", action="append", choices=["dark", "light"])
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--theme-file", type=pathlib.Path,
                        help="generated matugen JSON to load through MatugenThemeAdapter")
    parser.add_argument("--label", help="file-name label instead of the variant")
    args = parser.parse_args(argv)
    quickshell = shutil.which("quickshell")
    if quickshell is None:
        parser.error("quickshell is not installed")
    scenarios = []
    for item in args.scenario:
        name, separator, path = item.partition("=")
        if not separator or not name or not pathlib.Path(path).is_file():
            parser.error(f"invalid scenario: {item}")
        scenarios.append((name, pathlib.Path(path)))
    args.output.mkdir(parents=True, exist_ok=True)
    for variant in args.variant or ["dark", "light"]:
        render(quickshell, scenarios, variant, args.output.resolve(), args.timeout,
               args.theme_file, args.label)
    return 0


if __name__ == "__main__":
    sys.exit(main())
