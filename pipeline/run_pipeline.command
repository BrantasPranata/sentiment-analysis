#!/bin/bash
# ------------------------------------------------------------
# run_pipeline.command  (macOS: klik dua kali di Finder atau ./run_pipeline.command)
# Mengaktifkan .venv lalu menjalankan langkah-langkah di run_pipeline.sh
# ------------------------------------------------------------

# Pindah ke folder pipeline (folder tempat script ini berada)
cd "$(dirname "$0")" || exit 1

# Cegah Mac tidur selama pipeline berjalan (berhenti otomatis saat script selesai)
caffeinate -i -w $$ &

# Aktivasi Virtual Environment
if [ -f ".venv/bin/activate" ]; then
    source ".venv/bin/activate"
else
    echo "[ERROR] .venv tidak ditemukan di $(pwd)"
    exit 1
fi

bash ./run_pipeline.sh
