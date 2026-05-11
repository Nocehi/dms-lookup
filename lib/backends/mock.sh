#!/usr/bin/env bash
set -euo pipefail

query="$1"
out_dir="/tmp/dank-lookup"
mkdir -p "$out_dir"

export TITLE="$query"
python3 -c '
import json, os, sys
result = {
  "backend": "mock",
  "title": os.environ.get("TITLE", ""),
  "subtitle": "mock result until sdcv is configured",
  "body": "Install or configure a local dictionary backend for real definitions."
}
json.dump(result, sys.stdout, indent=2, ensure_ascii=False)
' > "$out_dir/backend_result.json"
