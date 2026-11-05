#!/usr/bin/env bash
# Minimal public demo. No endpoint needed for the agent-mode steps.
set -euo pipefail
cd "$(dirname "$0")/.."
OUT=./out-demo

# 1. Agent mode: ingest a short public YouTube video, then translate the work
#    items yourself (your model) and assemble. No API key required.
python3 scripts/yt_translate.py prepare \
  "https://www.youtube.com/watch?v=9bZkp7q19f0" --langs en,uk --out "$OUT"
# -> edit each  $OUT/<video>/.work/<lang>/<n>.in.json  into  <n>.out.json  (same keys),
#    then:
python3 scripts/yt_translate.py status   --out "$OUT"
python3 scripts/yt_translate.py assemble --out "$OUT"

# 2. Endpoint mode (optional): translate directly if you have a server.
#    export YT_TX_BASE_URL=... YT_TX_API_KEY=... YT_TX_MODEL=...
#    python3 scripts/yt_translate.py run \
#      "https://www.youtube.com/watch?v=9bZkp7q19f0" --langs en,uk --out "$OUT" --concurrency 4

# 3. Structural QA (always works, no model):
python3 scripts/qa.py run "$OUT" --sample 0 --review en,uk
