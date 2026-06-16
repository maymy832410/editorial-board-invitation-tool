#!/bin/sh
set -eu

(
  while true; do
    python collector_worker.py
    echo "collector_worker.py exited; restarting in 5 seconds" >&2
    sleep 5
  done
) &

exec streamlit run app.py --server.port "${PORT:-8501}" --server.address 0.0.0.0 --server.headless true
