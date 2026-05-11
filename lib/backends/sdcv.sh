#!/usr/bin/env bash
set -euo pipefail

query="$1"
out_dir="/tmp/dank-lookup"
out_file="${DANK_LOOKUP_OUTPUT:-$out_dir/backend_result.json}"
mkdir -p "$out_dir"

if ! command -v sdcv &>/dev/null; then
  python3 -c '
import json, sys
json.dump({"backend":"mock","title":"'"$query"'","subtitle":"mock result until sdcv is configured","body":"Install or configure a local dictionary backend for real definitions."}, sys.stdout, indent=2, ensure_ascii=False)
' > "$out_file"
  exit 0
fi

python3 -c '
import json, os, re, subprocess, sys, pathlib

QUERY = sys.argv[1]
CONFIG_DIR = os.path.expanduser("~/.config/dank-lookup")
CONFIG_PATH = os.environ.get("DANK_LOOKUP_CONFIG") or os.path.join(CONFIG_DIR, "config.json")

# === Language classification ===
def classify(text):
    has_cjk = any("\u4e00" <= c <= "\u9fff" or "\u3400" <= c <= "\u4dbf" for c in text)
    has_kana = any("\u3040" <= c <= "\u309f" or "\u30a0" <= c <= "\u30ff" for c in text)
    has_latin = text and text[0].isascii() and text[0].isalpha()
    if has_kana: return "kana"
    if has_cjk and has_latin: return "mixed"
    if has_cjk: return "cjk"
    if has_latin: return "latin"
    return "unknown"

# === Default config (matches current hardcoded behavior) ===
def default_config():
    return {
        "version": 1,
        "stardict_data_dir": [os.path.expanduser("~/.local/share/stardict/dic"), "/usr/share/stardict/dic"],
        "modes": {
            "han": {
                "label": "\u6f22\u8a9e", "enabled": True, "default_for": ["cjk"],
                "show_related": False,
                "sources": [{"name": "\u840c\u5178\u91cd\u7de8\u570b\u8a9e\u8fad\u5178", "data_dir": None, "when": "cjk", "exact_only": False, "allow_related": False}]
            },
            "han-en": {
                "label": "\u6f22\u82f1", "enabled": True, "default_for": ["latin", "mixed"],
                "show_related": False,
                "sources": [
                    {"name": "XDICT\u82f1\u6f22\u8fad\u5178", "data_dir": None, "when": "latin", "exact_only": False, "allow_related": False},
                    {"name": "XDICT\u6f22\u82f1\u8fad\u5178", "data_dir": None, "when": "cjk", "exact_only": False, "allow_related": False}
                ]
            },
            "en-en": {
                "label": "\u82f1\u82f1", "enabled": True, "default_for": [],
                "show_related": False,
                "sources": [
                    {"name": "dictd_www.dict.org_gcide", "data_dir": "/usr/share/stardict/dic", "when": "any", "exact_only": True, "allow_related": True},
                    {"name": "WordNet", "data_dir": "/usr/share/stardict/dic", "when": "any", "exact_only": True, "allow_related": True}
                ]
            }
        },
        "fallback": {"unfiltered": True, "mock": True}
    }

# === Config loading ===
config_warning = None
config_loaded = False
if os.path.isfile(CONFIG_PATH):
    try:
        cfg = json.loads(pathlib.Path(CONFIG_PATH).read_text(encoding="utf-8"))
        config_loaded = True
    except Exception as e:
        config_warning = str(e)
        cfg = default_config()
else:
    cfg = default_config()

# === sdcv runner ===
def run_sdcv(dict_name, data_dir=None):
    cmd = ["sdcv", "-n", "--json"]
    env = os.environ.copy()
    if data_dir:
        cmd += ["--data-dir", data_dir]
    else:
        env.setdefault("STARDICT_DATA_DIR", os.path.expanduser("~/.local/share/stardict/dic"))
    cmd += ["--use-dict", dict_name, QUERY]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=5, env=env)
        out = result.stdout.strip()
        if out and out != "[]":
            return json.loads(out)
    except:
        pass
    return []

def run_unfiltered():
    env = os.environ.copy()
    env.setdefault("STARDICT_DATA_DIR", os.path.expanduser("~/.local/share/stardict/dic"))
    try:
        result = subprocess.run(["sdcv", "-n", "--json", QUERY], capture_output=True, text=True, timeout=5, env=env)
        out = result.stdout.strip()
        if out and out != "[]":
            return json.loads(out)
    except:
        pass
    return []

# === Text cleanup ===
def strip_def(dict_name, text):
    text = re.sub(r"<[^>]+>", "", text)
    lines = [line.rstrip() for line in text.splitlines()]
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    dn = dict_name.lower()
    if "gcide" in dn or "webster" in dn:
        text = re.sub(r"\n\s*\[1913 Webster\]\s*(?=\n)", "\n", text)
        text = re.sub(r"\n(\d+\.)", r"\n\n\1", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()

def build_body(entries):
    seen = set()
    parts = []
    dicts = set()
    for entry in entries:
        dn = str(entry.get("dict", "")).strip()
        df = strip_def(dn, str(entry.get("definition", "")))
        if not dn or not df:
            continue
        key = (dn, str(entry.get("word", "")))
        if key in seen:
            continue
        seen.add(key)
        dicts.add(dn)
        parts.append("[" + dn + "]\n" + df)
    return "\n\n".join(parts), dicts

def is_exact(entry):
    return str(entry.get("word", "")).strip().casefold() == QUERY.strip().casefold()

def source_key(entry):
    dn = str(entry.get("dict", "")).lower()
    return 0 if "gcide" in dn or "webster" in dn else 1

# === Collect entries per mode ===
lang = classify(QUERY)
results_by_mode = {}
related_by_mode = {}
mode_preferences = {}
available_modes = []

for mode_key, mode_cfg in cfg.get("modes", {}).items():
    if not mode_cfg.get("enabled", True):
        continue

    # Skip if no sources match this query language
    sources_for_mode = [s for s in mode_cfg.get("sources", [])
                        if s.get("when", "any") == "any" or
                           (s.get("when") == "cjk" and lang in ("cjk", "mixed")) or
                           s.get("when") == lang]
    if not sources_for_mode:
        continue

    all_entries = []
    for source in sources_for_mode:
        dir_override = os.path.expanduser(source["data_dir"]) if source.get("data_dir") else None
        entries = run_sdcv(source["name"], dir_override)
        all_entries.extend(entries)

    # Unfiltered fallback only for han-en mode
    if not all_entries and mode_key == "han-en" and cfg.get("fallback", {}).get("unfiltered"):
        unfiltered = run_unfiltered()
        if unfiltered:
            all_entries.extend(unfiltered)

    if not all_entries:
        continue

    # Sort: exact first, then GCIDE priority within same exactness
    all_entries.sort(key=lambda e: (0 if is_exact(e) else 1, source_key(e)))

    exact = [e for e in all_entries if is_exact(e)]
    fuzzy = [e for e in all_entries if not is_exact(e)]

    body, dicts = build_body(exact)
    if body:
        results_by_mode[mode_key] = {
            "label": mode_cfg.get("label", mode_key),
            "subtitle": ", ".join(sorted(dicts)),
            "body": body
        }
        available_modes.append(mode_key)

    # Related: only if at least one source in this mode allows it
    allow_rel = any(s.get("allow_related", False) for s in mode_cfg.get("sources", []))
    if allow_rel and fuzzy:
        rel_body, rel_dicts = build_body(fuzzy)
        if rel_body:
            related_by_mode[mode_key] = {
                "label": "Related",
                "subtitle": ", ".join(sorted(rel_dicts)),
                "body": rel_body
            }

    mode_preferences[mode_key] = {"show_related": mode_cfg.get("show_related", False)}

# === Default mode selection ===
if not available_modes:
    if cfg.get("fallback", {}).get("mock"):
        json.dump({"backend":"mock","title":QUERY,"subtitle":"mock result until sdcv is configured","body":"Install or configure a local dictionary backend for real definitions."}, sys.stdout, indent=2, ensure_ascii=False)
    sys.exit(0)

candidates = [m for m in cfg.get("modes", {}) if lang in cfg["modes"][m].get("default_for", []) and m in available_modes]
default_mode = candidates[0] if candidates else available_modes[0]

dm = results_by_mode.get(default_mode, results_by_mode[available_modes[0]])

# === Output ===
output = {
    "backend": "sdcv",
    "title": QUERY,
    "default_mode": default_mode,
    "available_modes": available_modes,
    "subtitle": dm["subtitle"],
    "body": dm["body"],
    "results_by_mode": results_by_mode,
    "related_by_mode": related_by_mode,
    "mode_preferences": mode_preferences,
    "config": {"path": CONFIG_PATH, "loaded": config_loaded, "warning": config_warning}
}
json.dump(output, sys.stdout, indent=2, ensure_ascii=False)
' "$query" > "$out_file"
