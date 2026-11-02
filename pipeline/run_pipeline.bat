@echo off
:: Pindah ke folder pipeline (folder tempat script ini berada)
cd /d "%~dp0"

:: Aktivasi Virtual Environment
call ".venv\Scripts\activate.bat"

:: 1. Auto Scraper Artikel
python "scrapers\auto_article_scraper.py" -l 10

:: 2. Auto Scraper X / Twitter
python "scrapers\auto_x_scraper.py" -l 50

:: 3. Auto Scraper Instagram
python "scrapers\auto_ig_scraper.py" -l 15

:: 4. Auto Scraper YouTube
python "scrapers\auto_yt_scraper.py" -l 3

:: 5a. Translate YT Desc
python "helpers\translate_yt_desc.py"

:: 5b. Get Lost YT Published Date
python "helpers\get_yt_published.py"

:: 6. Multimodal Sentiment Processor
python "sentiment\process_sentiment_db.py"

:: 7. Auto Scraping Comments for X, IG, YT, Detik, Kompas 200 komen, last 30 contents
python "scrapers\comments_scraper.py" -l 200 -cl 30

:: 8. Multimodal Sentiment Processor for Comments
python "sentiment\process_comment_sentiment.py"

:: Keluar secara otomatis (JANGAN pakai pause)
exit
