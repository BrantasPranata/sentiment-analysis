import argparse
from datetime import datetime
import html
import json
import logging
from pathlib import Path
import re
import sys
import urllib.parse
from bs4 import BeautifulSoup
from email.utils import parsedate_to_datetime
import feedparser
from googlenewsdecoder import gnewsdecoder
import mysql.connector
from mysql.connector import Error
import requests

# Menambahkan root project directory (1 tingkat di atas folder script saat ini) ke sys.path
sys.path.append(str(Path(__file__).resolve().parent.parent))

from config.config import LOG_DIR, get_db_connection


# --- LOGGING SETUP ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_DIR / "auto_x_scraper.log", encoding="utf-8"),
    ],
)

# Sembunyikan log internal HTTP Request dari httpx & urllib3 agar terminal bersih
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "Chrome/122.0.0.0 Safari/537.36"
    )
}


def ensure_platform_x_exists():
    """Memastikan ID platform X (id=4) terdaftar di database."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute("""
            INSERT INTO platforms (id, code, name, rss_url)
            VALUES (4, 'x', 'X / Twitter', 'https://x.com')
            ON DUPLICATE KEY UPDATE code=VALUES(code), name=VALUES(name)
        """)

        conn.commit()
        cursor.close()
        conn.close()
    except Error as e:
        logging.error(f"Gagal memastikan platform X di DB: {e}")


def decode_google_news_url(rss_url: str) -> str:
    """Mendekode link RSS Google News menjadi URL asli terurai."""
    try:
        result = gnewsdecoder(rss_url)

        decoded_url = None
        if isinstance(result, str):
            decoded_url = result
        elif isinstance(result, dict):
            decoded_url = result.get("decoded_url") or result.get("url")
        elif hasattr(result, "decoded_url"):
            decoded_url = getattr(result, "decoded_url")

        if (
            decoded_url
            and isinstance(decoded_url, str)
            and decoded_url.startswith(("http://", "https://"))
        ):
            return decoded_url

    except Exception as e:
        logging.warning(f"Decoder error [{rss_url}]: {e}")

    try:
        response = requests.get(
            rss_url, headers=HEADERS, allow_redirects=True, timeout=6
        )
        if response.url and response.url.startswith(("http://", "https://")):
            return response.url
    except Exception:
        pass

    return rss_url


def parse_x_url(x_url: str):
    """Mengekstrak username dan tweet_id dari URL X.com / Twitter.com."""
    match = re.search(r'(?:x|twitter)\.com/([^/]+)/status/(\d+)', x_url)
    if match:
        return match.group(1), match.group(2)
    return None, None


def parse_pub_date(pub_date_str):
    """Mengubah string/tuple tanggal dari RSS Feed menjadi format DATETIME MySQL (YYYY-MM-DD HH:MM:SS)."""
    if not pub_date_str:
        return None
    try:
        dt = parsedate_to_datetime(pub_date_str)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def scrape_x_media_urls(clean_url: str) -> list:
    """Mengambil URL Gambar dari Post X via OpenGraph Meta Tag (og:image)."""
    media_urls = []
    try:
        res = requests.get(clean_url, headers=HEADERS, timeout=8)
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, "html.parser")

            og_image = (
                soup.find("meta", property="og:image")
                or soup.find("meta", attrs={"name": "twitter:image"})
                or soup.find("meta", attrs={"name": "og:image"})
            )

            if og_image and og_image.get("content"):
                img_url = og_image["content"]
                if (
                    "profile_images" not in img_url
                    and "default_profile" not in img_url
                ):
                    media_urls.append(img_url)
    except Exception as e:
        logging.warning(f"Gagal scrape media gambar [{clean_url}]: {e}")

    return media_urls


def fetch_tweet_via_oembed(x_url: str) -> dict:
    """Mengambil data Teks & Author menggunakan API OEmbed Resmi dari X."""
    username_from_url, tweet_id = parse_x_url(x_url)

    if not tweet_id:
        return {
            "url": x_url,
            "username": username_from_url or "unknown",
            "author": f"@{username_from_url}" if username_from_url else "Unknown",
            "text": "",
            "is_valid": False,
            "error": "Bukan format URL Tweet spesifik",
        }

    default_username = f"@{username_from_url}"
    oembed_url = f"https://publish.twitter.com/oembed?url={requests.utils.quote(x_url)}"

    try:
        res = requests.get(oembed_url, headers=HEADERS, timeout=8)
        if res.status_code == 200:
            data = res.json()
            author_name = data.get("author_name")

            html_content = data.get("html", "")
            soup = BeautifulSoup(html_content, "html.parser")

            for a in soup.find_all("a"):
                a.decompose()

            text = soup.get_text(strip=True)
            display_author = (
                f"{author_name} ({default_username})"
                if author_name
                else default_username
            )

            return {
                "url": x_url,
                "username": default_username,
                "author": display_author,
                "text": text if text else "Teks tweet tidak tersedia.",
                "is_valid": True,
                "original_id": tweet_id,
            }
    except Exception as e:
        logging.warning(f"OEmbed API error [{x_url}]: {e}")

    return {
        "url": x_url,
        "username": default_username,
        "author": default_username,
        "text": "",
        "is_valid": True,
        "original_id": tweet_id,
    }


def fetch_x_rss_news(keyword: str, limit: int) -> list:
    """Crawl RSS X dari Google News -> Decode -> Ekstraksi Tanggal -> Filter Hanya Tweet Spesifik."""
    query = f"site:x.com {keyword}"
    rss_url = f"https://news.google.com/rss/search?q={urllib.parse.quote(query)}&hl=id&gl=ID&ceid=ID:id"

    # Ambil RSS lewat requests (pakai certifi), bukan urllib bawaan feedparser
    try:
        resp = requests.get(rss_url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        logging.error(f"Gagal fetch RSS Google News [{keyword}]: {e}")
        return []

    feed = feedparser.parse(resp.content)
    if feed.bozo and not feed.entries:
        logging.warning(f"Feed bermasalah [{keyword}]: {feed.get('bozo_exception')}")

    logging.info(f"    RSS [{keyword}] status={resp.status_code}, entries={len(feed.entries)}")

    results = []

    for entry in feed.entries:
        if len(results) >= limit:
            break

        raw_rss_link = entry.link
        raw_title = entry.title or "Post X"
        raw_pub_date = getattr(entry, "published", None)
        published_at = parse_pub_date(raw_pub_date)

        # Decode URL Google News ke Clean URL
        clean_url = decode_google_news_url(raw_rss_link)

        # Extract & Filter: Cek Apakah URL Benar-benar Tweet Spesifik
        username, tweet_id = parse_x_url(clean_url)

        if not tweet_id or not username:
            logging.info(f"    [DISCARDED NON-TWEET] Skipped: {clean_url}")
            continue

        author = f"@{username}"
        clean_title = html.unescape(raw_title)
        clean_title = re.sub(r"\s*-\s*@?\w+\s*$", "", clean_title).strip()

        results.append(
            {
                "platform_code": "x",
                "author": author,
                "title": clean_title,
                "rss_url": raw_rss_link,
                "clean_url": clean_url,
                "original_id": tweet_id,
                "published_at": published_at,
            }
        )

    return results


def get_active_keywords_for_x(cursor):
    """Mengambil daftar keyword aktif dari database khusus untuk platform X (platform_id = 4)."""
    query = """
        SELECT keyword 
        FROM keywords 
        WHERE platform_id = 4 AND is_active = 1
    """
    cursor.execute(query)
    rows = cursor.fetchall()
    return [row["keyword"] for row in rows]


def run_x_crawling_pipeline(limit_per_keyword):
    """Fungsi utama eksekusi batch crawler X secara otomatis."""
    ensure_platform_x_exists()

    total_inserted = 0
    total_skipped = 0

    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        p_id = 4  # Platform ID untuk X (Twitter)

        # Tarik keyword dari database khusus untuk Platform X
        keywords = get_active_keywords_for_x(cursor)
        if not keywords:
            logging.info("Tidak ada keyword aktif di database untuk platform X (Twitter).")
            cursor.close()
            conn.close()
            return

        logging.info(f"=== Menjalankan Crawling X untuk Keywords DB: {keywords} ===")

        # Load cache URL yang sudah tersimpan di database untuk menghindari duplikasi
        cursor.execute("SELECT clean_url FROM contents WHERE platform_id = 4")
        existing_urls = {row["clean_url"] for row in cursor.fetchall()}

        for kw in keywords:
            logging.info(f"=== Memulai Crawling X untuk Kata Kunci: '{kw}' ===")

            tweets = fetch_x_rss_news(kw, limit_per_keyword)
            logging.info(
                f"Ditemukan {len(tweets)} tweet valid dari RSS. Memproses detail..."
            )

            for item in tweets:
                clean_url = item["clean_url"]

                # Cek Duplikasi di Database
                if clean_url in existing_urls:
                    total_skipped += 1
                    logging.info(f"    [SKIP - DUPLIKAT DB] {clean_url}")
                    continue

                # 1. Fetch Detail & Author via OEmbed API
                post_data = fetch_tweet_via_oembed(clean_url)

                actual_text = post_data["text"]
                username = post_data.get("username", item["author"])
                author_full = post_data["author"]

                # 2. Scrape Gambar via OpenGraph
                media_urls = scrape_x_media_urls(clean_url)
                media_urls_json = (
                    json.dumps(media_urls) if media_urls else None
                )

                # 3. Format Simpan DB: @username - actual tweet
                if actual_text and actual_text != "Teks tweet tidak tersedia.":
                    formatted_raw_text = f"{username} - {actual_text}"
                else:
                    formatted_raw_text = f"{username} - {item['title']}"

                title = f"Tweet oleh {author_full}: {actual_text[:60] if actual_text else item['title'][:60]}..."
                original_id = item["original_id"]
                published_at = item["published_at"]

                # 4. Insert ke Tabel contents (termasuk kolom published_at)
                query = """
                    INSERT IGNORE INTO contents 
                    (platform_id, clean_url, original_id, title, raw_text, media_urls, published_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                """
                cursor.execute(
                    query,
                    (
                        p_id,
                        clean_url,
                        original_id,
                        title,
                        formatted_raw_text,
                        media_urls_json,
                        published_at,
                    ),
                )

                if cursor.rowcount > 0:
                    inserted_id = cursor.lastrowid
                    total_inserted += 1
                    existing_urls.add(clean_url)

                    # Antrikan ke sentiment_results (status: pending)
                    cursor.execute(
                        """
                        INSERT IGNORE INTO sentiment_results 
                        (target_type, target_id, execution_source, status)
                        VALUES ('content', %s, 'ml_pipeline', 'pending')
                    """,
                        (inserted_id,),
                    )
                    conn.commit()
                    logging.info(
                        f"    [INSERTED SUCCESS] [{author_full}] ({len(media_urls)} Gambar) -> {clean_url} | Published At: {published_at}"
                    )
                else:
                    total_skipped += 1
                    logging.info(f"    [SKIPPED IGNORE] {clean_url}")

        cursor.close()
        conn.close()

    except Error as e:
        logging.error(f"Error Database MySQL: {e}")

    logging.info(
        f"=== BATCH CRAWLING X SELESAI | Inserted Baru: {total_inserted} | Skipped/Duplikat: {total_skipped} ==="
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Headless X (Twitter) Scraper untuk db_sentimenanalisis_v2"
    )
    parser.add_argument(
        "-l",
        "--limit",
        type=int,
        default=10,
        help="Batas tweet per kata kunci (default: 10)",
    )

    args = parser.parse_args()
    run_x_crawling_pipeline(args.limit)