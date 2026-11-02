#!/usr/bin/env bash
# ------------------------------------------------------------
# Entrypoint container pipeline: jalankan run_pipeline.sh berulang.
#   PIPELINE_INTERVAL_MINUTES  jeda setelah satu putaran selesai (default 60)
#   PIPELINE_RUN_ONCE=true     jalankan satu kali lalu keluar
# Perintah lain (mis. "python scrapers/auto_x_scraper.py -l 5") dijalankan apa adanya.
# ------------------------------------------------------------
set -u

if [ "$#" -gt 0 ]; then
    exec "$@"
fi

INTERVAL_MINUTES="${PIPELINE_INTERVAL_MINUTES:-60}"

while true; do
    bash /app/run_pipeline.sh 2>&1 | tee -a "${LOG_DIR:-/app/logs}/pipeline.log"

    if [ "${PIPELINE_RUN_ONCE:-false}" = "true" ]; then
        exit 0
    fi

    echo "=== $(date '+%Y-%m-%d %H:%M:%S') | Putaran berikutnya dalam ${INTERVAL_MINUTES} menit ==="
    sleep "$((INTERVAL_MINUTES * 60))"
done
