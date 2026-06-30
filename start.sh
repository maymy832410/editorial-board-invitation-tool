#!/bin/sh
set -eu

# Collection is owned exclusively by Railway's collector-worker service.
exec streamlit run app.py --server.port "${PORT:-8501}" --server.address 0.0.0.0 --server.headless true
