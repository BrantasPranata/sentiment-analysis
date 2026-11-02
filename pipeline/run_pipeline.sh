#!/usr/bin/env bash
# ------------------------------------------------------------
# run_pipeline.sh - urutan langkah pipeline (scraping -> sentimen).
# Dipakai oleh Docker (docker/scheduler.sh), Linux, dan run_pipeline.command (macOS).
# Jalankan dari mana saja:  ./run_pipeline.sh
# ------------------------------------------------------------

# Pindah ke folder pipeline (folder tempat script ini berada)
cd "$(dirname "$0")" || exit 1

export PYTHONUNBUFFERED=1
export PYTORCH_ENABLE_MPS_FALLBACK=1

PYTHON="${PYTHON:-python}"

# Jalankan satu langkah; kalau gagal tetap lanjut ke langkah berikutnya
run_step() {
    local label="$1"
    shift
    echo ""
    echo "=== $(date '+%Y-%m-%d %H:%M:%S') | $label ==="
    "$@"
    local code=$?
    if [ $code -ne 0 ]; then
        echo "[WARNING] '$label' selesai dengan kode $code, lanjut ke langkah berikutnya..."
    fi
}

# 1. Auto Scraper Artikel
run_step "1. Auto Scraper Artikel" "$PYTHON" scrapers/auto_article_scraper.py -l 10

# 2. Auto Scraper X / Twitter
run_step "2. Auto Scraper X / Twitter" "$PYTHON" scrapers/auto_x_scraper.py -l 50

# 3. Auto Scraper Instagram
run_step "3. Auto Scraper Instagram" "$PYTHON" scrapers/auto_ig_scraper.py -l 15

# 4. Auto Scraper YouTube
run_step "4. Auto Scraper YouTube" "$PYTHON" scrapers/auto_yt_scraper.py -l 3

# 5a. Translate YT Desc
run_step "5a. Translate YT Desc" "$PYTHON" helpers/translate_yt_desc.py

# 5b. Get Lost YT Published Date
run_step "5b. Get Lost YT Published Date" "$PYTHON" helpers/get_yt_published.py

# 6. Multimodal Sentiment Processor
run_step "6. Multimodal Sentiment Processor" "$PYTHON" sentiment/process_sentiment_db.py

# 7. Auto Scraping Comments for X, IG, YT, Detik, Kompas 200 komen, last 30 contents
run_step "7. Auto Scraping Comments" "$PYTHON" scrapers/comments_scraper.py -l 200 -cl 30

# 8. Sentiment Processor for Comments
run_step "8. Sentiment Processor for Comments" "$PYTHON" sentiment/process_comment_sentiment.py

echo ""
echo "=== $(date '+%Y-%m-%d %H:%M:%S') | PIPELINE SELESAI ==="
exit 0
