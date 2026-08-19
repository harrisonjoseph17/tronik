#!/usr/bin/env bash
# Runs one discovery scan followed by one analysis pass. Intended to be
# invoked by systemd (see deploy/polymarket-companion.service) on a
# recurring schedule so market_snapshots/features/analyses history
# actually accumulates over time without manual intervention.
#
# `set -e` means a failed scan aborts before analyze runs against stale or
# partial data - the next scheduled timer firing is the natural retry, so
# there's no retry logic here beyond what app/polymarket/client.py's own
# bounded backoff already provides.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

source .venv/bin/activate

echo "[run_scan_and_analyze] scan starting: $(date -u +%FT%TZ)"
python main.py scan

echo "[run_scan_and_analyze] analyze starting: $(date -u +%FT%TZ)"
python main.py analyze

echo "[run_scan_and_analyze] done: $(date -u +%FT%TZ)"
