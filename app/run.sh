#!/usr/bin/env bash
# CAISO Market Surveillance dashboard launcher.
# Usage: ./run.sh            (builds derived data if missing, then launches)
#        ./run.sh --rebuild  (force re-run the preprocessing pipeline)
set -euo pipefail
cd "$(dirname "$0")"

PY=.venv/bin/python
if [ ! -x "$PY" ]; then
  echo "Creating virtualenv + installing deps..."
  python3 -m venv .venv
  .venv/bin/python -m pip install --quiet --upgrade pip
  .venv/bin/python -m pip install --quiet duckdb pandas pyarrow streamlit plotly
fi

if [ "${1:-}" == "--rebuild" ] || [ ! -f data/derived/meta.json ]; then
  echo "Running preprocessing pipeline (reads ../caiso-data/*.parquet)..."
  $PY pipeline.py
fi

echo "Launching dashboard at http://localhost:8531 ..."
exec .venv/bin/streamlit run dashboard.py --server.port 8531
