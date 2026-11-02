import argparse
from datetime import datetime
import html
import json
import logging
from pathlib import Path
import re
import sys
import xml.etree.ElementTree as ET
from bs4 import BeautifulSoup
from email.utils import parsedate_to_datetime
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
        logging.FileHandler(LOG_DIR / "auto_scraper.log", encoding="utf-8"),
    ],
)

# Sembunyikan log internal HTTP Request dari httpx, httpcore, & urllib3 agar terminal bersih
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "Chrome/122.0.0.0 Safari/537.36"
    )
}

# --- KONFIGURASI VALIDASI ARTIKEL ---
# URL yang bukan halaman artikel (tag, foto, video, indeks, pencarian, dll.)
BAD_URL_PATTERNS = re.compile(
    r"/(tag|tags|topik|topic|foto|photo|galeri|gallery|video|videos|indeks|index|search|cari)(/|$|\?)",
    re.IGNORECASE,
)
# Nilai og:type yang dianggap artikel
VALID_OG_TYPES = ("article", "news", "newsarticle")
# Batas minimal isi artikel (sesuaikan dengan data nyata)
MIN_TEXT_LEN = 300  # karakter
MIN_PARAGRAPHS = 3


def is_bad_url(clean_url):
    """Cek cepat (tanpa request) apakah URL jelas bukan halaman artikel."""
    if "news.google.com" in clean_url:
        return True, "decode URL gagal (masih link Google News)"
    if BAD_URL_PATTERNS.search(clean_url):
        return True, "URL bukan halaman artikel (tag/foto/video/indeks)"
    return False, "ok"


def is_valid_article(clean_url, raw_text, og_type=None):
    """Validasi akhir setelah scraping. Return (bool, alasan)."""
    bad, reason = is_bad_url(clean_url)
    if bad:
        return False, reason

    if og_type and og_type.strip().lower() not in VALID_OG_TYPES:
        return False, f"og:type bukan artikel ({og_type})"

    if not raw_text or len(raw_text) < MIN_TEXT_LEN:
        return False, "teks terlalu pendek / kosong"

    if raw_text.count("\n\n") + 1 < MIN_PARAGRAPHS:
        return False, "paragraf terlalu sedikit"

    return True, "ok"


def decode_google_news_url(rss_url):
    """Mendekode URL terenkripsi Google News menjadi Actual Clean URL."""
    try:
        # Panggil gnewsdecoder() LANGSUNG sebagai fungsi dengan memasukkan rss_url
        result = gnewsdecoder(rss_url)

        decoded_url = None
        # Handle 1: Jika kembalian berupa String langsung
        if isinstance(result, str):
            decoded_url = result
        # Handle 2: Jika kembalian berupa Dictionary
        elif isinstance(result, dict):
            decoded_url = result.get("decoded_url") or result.get("url")
        # Handle 3: Jika kembalian berupa Object yang memiliki atribut decoded_url
        elif hasattr(result, "decoded_url"):
            decoded_url = getattr(result, "decoded_url")

        # Validasi Kunci: Pastikan decoded_url adalah URL valid (diawali http:// atau https://)
        if (
            decoded_url
            and isinstance(decoded_url, str)
            and decoded_url.startswith(("http://", "https://"))
        ):
            return decoded_url

    except Exception as e:
        logging.warning(f"Gagal decode Google URL [{rss_url}]: {e}")

    # Fallback manual ke requests.get jika library dekoder gagal
    try:
        response = requests.get(
            rss_url, headers=HEADERS, allow_redirects=True, timeout=6
        )
        if response.url and response.url.startswith(("http://", "https://")):
            return response.url
    except Exception:
        pass

    return rss_url


def _extract_paragraphs(container):
    """Ambil teks paragraf (tidak kosong) dari sebuah elemen."""
    if not container:
        return []
    return [
        p.get_text(strip=True)
        for p in container.find_all("p")
        if p.get_text(strip=True)
    ]


def scrape_article_detail(clean_url, platform_code):
    """Mengambil teks isi artikel, URL gambar utama, dan og:type dari Clean URL.

    Return: (raw_text, media_urls, og_type)
    """
    raw_text = ""
    media_urls = []
    og_type = None

    try:
        res = requests.get(clean_url, headers=HEADERS, timeout=12)
        if res.status_code != 200:
            logging.warning(f"HTTP {res.status_code} untuk {clean_url}")
            return raw_text, media_urls, og_type

        soup = BeautifulSoup(res.text, "html.parser")

        # 0. Ambil og:type untuk membedakan artikel vs halaman lain
        og_type_tag = soup.find("meta", property="og:type") or soup.find(
            "meta", attrs={"name": "og:type"}
        )
        if og_type_tag and og_type_tag.get("content"):
            og_type = og_type_tag["content"].strip()

        # 1. Ekstraksi Gambar Utama (OpenGraph Meta Tag)
        og_image = soup.find("meta", property="og:image") or soup.find(
            "meta", attrs={"name": "og:image"}
        )
        if og_image and og_image.get("content"):
            media_urls.append(og_image["content"])

        # 2. Ekstraksi Paragraf Isi Berita berdasarkan Platform
        paragraphs = []
        if platform_code == "detik":
            detail_body = soup.find(
                "div", class_="detail__body-text"
            ) or soup.find("div", id="detikdetailtext")
            paragraphs = _extract_paragraphs(detail_body)

        elif platform_code == "kompas":
            detail_body = soup.find(
                "div", class_="read__content"
            ) or soup.find("div", class_="kcm-read-text")
            paragraphs = _extract_paragraphs(detail_body)

        elif platform_code == "antaranews":
            detail_body = soup.find(
                "div", class_="post-content"
            ) or soup.find("div", class_="post-wrapper")
            paragraphs = _extract_paragraphs(detail_body)

        # Fallback umum HANYA jika halaman ini memang bertipe artikel.
        # Untuk halaman tag/foto/dll, fallback ini hanya menangkap menu/footer/teaser.
        if not paragraphs and og_type and og_type.lower() in VALID_OG_TYPES:
            all_p = soup.find_all("p")
            paragraphs = [
                p.get_text(strip=True)
                for p in all_p
                if len(p.get_text(strip=True)) > 30
            ]

        raw_text = "\n\n".join(paragraphs)

    except Exception as e:
        logging.error(f"Gagal scrape detail artikel [{clean_url}]: {e}")

    return raw_text, media_urls, og_type


def parse_pub_date(pub_date_str):
    """Mengubah string pubDate dari RSS Feed menjadi format DATETIME MySQL (YYYY-MM-DD HH:MM:SS)."""
    if not pub_date_str:
        return None
    try:
        dt = parsedate_to_datetime(pub_date_str)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def fetch_rss_news(platform_code, source_name, domain_filter, keyword, limit):
    """Mengambil RSS Feed dari Google News beserta tanggal terbitnya."""
    articles = []
    query = f"{keyword} site:{domain_filter}"
    rss_url = f"https://news.google.com/rss/search?q={requests.utils.quote(query)}&hl=id&gl=ID&ceid=ID:id"

    try:
        response = requests.get(rss_url, headers=HEADERS, timeout=10)
        if response.status_code == 200:
            root = ET.fromstring(response.content)
            for item in root.findall(".//item"):
                if len(articles) >= limit:
                    break

                title_elem = item.find("title")
                link_elem = item.find("link")
                pub_date_elem = item.find("pubDate")

                if title_elem is not None and link_elem is not None:
                    raw_title = title_elem.text or ""
                    link = link_elem.text or ""
                    raw_pub_date = (
                        pub_date_elem.text if pub_date_elem is not None else None
                    )
                    published_at = parse_pub_date(raw_pub_date)

                    clean_title = html.unescape(raw_title)
                    clean_title = re.sub(
                        r"\s*-\s*detik[A-Za-z]+$",
                        "",
                        clean_title,
                        flags=re.IGNORECASE,
                    )
                    clean_title = re.sub(
                        r"\s*-\s*[^-]+$", "", clean_title
                    ).strip()

                    if clean_title and link:
                        articles.append(
                            {
                                "platform_code": platform_code,
                                "source": source_name,
                                "title": clean_title,
                                "rss_url": link,
                                "published_at": published_at,
                            }
                        )
        else:
            logging.warning(
                f"RSS {source_name} mengembalikan HTTP {response.status_code}"
            )
    except Exception as e:
        logging.error(f"Error fetching RSS {source_name}: {e}")

    return articles


def get_active_keywords_by_platform(cursor, platform_id):
    """Mengambil daftar keyword aktif dari database berdasarkan platform_id."""
    query = """
        SELECT keyword 
        FROM keywords 
        WHERE platform_id = %s AND is_active = 1
    """
    cursor.execute(query, (platform_id,))
    rows = cursor.fetchall()
    return [row["keyword"] for row in rows]


def run_crawling_pipeline(limit_per_source):
    """Fungsi utama eksekusi pipeline crawling otomatis."""
    sources = [
        ("detik", "Detikcom", "detik.com"),
        ("antaranews", "Antara News", "antaranews.com"),
        ("kompas", "Kompas", "kompas.com"),
    ]

    total_inserted = 0
    total_skipped = 0
    total_invalid = 0

    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # 1. Ambil platform ID & daftar judul/URL yang sudah ada di database
        cursor.execute("SELECT id, code FROM platforms")
        platform_map = {row["code"]: row["id"] for row in cursor.fetchall()}

        cursor.execute("SELECT clean_url, title FROM contents")
        existing_rows = cursor.fetchall()
        existing_titles = {
            row["title"].lower().strip()
            for row in existing_rows
            if row["title"]
        }
        existing_urls = {row["clean_url"] for row in existing_rows}

        for p_code, p_name, domain in sources:
            p_id = platform_map.get(p_code)
            if not p_id:
                logging.warning(
                    f"Platform code '{p_code}' tidak ditemukan di tabel platforms!"
                )
                continue

            # Tarik keyword dari database untuk platform ini
            keywords = get_active_keywords_by_platform(cursor, p_id)
            if not keywords:
                logging.info(
                    f"Tidak ada keyword aktif di database untuk platform [{p_name}]."
                )
                continue

            logging.info(
                f"=== Processing Platform: {p_name} | Keywords: {keywords} ==="
            )

            for kw in keywords:
                articles = fetch_rss_news(
                    p_code, p_name, domain, kw, limit_per_source
                )
                logging.info(
                    f"[{p_name}] Keyword: '{kw}' | Ditemukan {len(articles)} RSS link."
                )

                for item in articles:
                    title_clean = item["title"].lower().strip()

                    # PENCEGAHAN 1: Cek apakah judul sudah ada di database
                    if title_clean in existing_titles:
                        total_skipped += 1
                        logging.info(
                            f"   [SKIP - ADA DI DB] Judul: '{item['title'][:50]}...'"
                        )
                        continue

                    # Step 1: Decode ke Clean URL
                    clean_url = decode_google_news_url(item["rss_url"])

                    # PENCEGAHAN 2: Cek apakah clean URL hasil decode sudah ada di DB
                    if clean_url in existing_urls:
                        total_skipped += 1
                        logging.info(
                            f"   [SKIP - URL DUPLIKAT] Clean URL: {clean_url}"
                        )
                        continue

                    # VALIDASI 1 (sebelum request): decode gagal / URL tag-foto-video-indeks
                    bad, reason = is_bad_url(clean_url)
                    if bad:
                        total_invalid += 1
                        total_skipped += 1
                        existing_urls.add(clean_url)  # cegah proses ulang dalam batch ini
                        logging.info(
                            f"   [SKIP - TIDAK VALID] {reason} | {clean_url}"
                        )
                        continue

                    # Step 2: Scrape teks, gambar, & og:type
                    logging.info(
                        f"   [PROCESSING] Scraping detail: {item['title'][:50]}..."
                    )
                    raw_text, media_urls, og_type = scrape_article_detail(
                        clean_url, p_code
                    )

                    # VALIDASI 2 (setelah scraping): og:type, panjang teks, jumlah paragraf
                    valid, reason = is_valid_article(clean_url, raw_text, og_type)
                    if not valid:
                        total_invalid += 1
                        total_skipped += 1
                        existing_urls.add(clean_url)  # cegah scrape ulang dalam batch ini
                        logging.info(
                            f"   [SKIP - TIDAK VALID] {reason} | {clean_url}"
                        )
                        continue

                    media_urls_json = (
                        json.dumps(media_urls) if media_urls else None
                    )

                    # Step 3: Simpan ke DB (termasuk kolom published_at)
                    query = """
                        INSERT IGNORE INTO contents 
                        (platform_id, clean_url, title, raw_text, media_urls, published_at)
                        VALUES (%s, %s, %s, %s, %s, %s)
                    """
                    cursor.execute(
                        query,
                        (
                            p_id,
                            clean_url,
                            item["title"],
                            raw_text,
                            media_urls_json,
                            item["published_at"],
                        ),
                    )

                    if cursor.rowcount > 0:
                        total_inserted += 1
                        content_id = cursor.lastrowid

                        # Tambahkan ke cache lokal agar tidak diproses ulang dalam batch yang sama
                        existing_titles.add(title_clean)
                        existing_urls.add(clean_url)

                        # Antrikan ke sentiment_results (status: pending)
                        cursor.execute(
                            """
                            INSERT IGNORE INTO sentiment_results 
                            (target_type, target_id, execution_source, status)
                            VALUES ('content', %s, 'ml_pipeline', 'pending')
                        """,
                            (content_id,),
                        )
                        conn.commit()
                        logging.info(
                            f"   [SUCCESS INSERTED] {clean_url} | Published At: {item['published_at']}"
                        )
                    else:
                        total_skipped += 1
                        logging.info(f"   [SKIP - DUPLICATE] {clean_url}")

        cursor.close()
        conn.close()

    except Error as e:
        logging.error(f"Error Database MySQL: {e}")

    logging.info(
        f"=== BATCH CRAWLING SELESAI | Inserted Baru: {total_inserted} "
        f"| Skipped: {total_skipped} (Tidak Valid: {total_invalid}) ==="
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Headless Article Scraper untuk db_sentimenanalisis_v2"
    )
    parser.add_argument(
        "-l",
        "--limit",
        type=int,
        default=5,
        help="Batas artikel per sumber berita (default: 5)",
    )

    args = parser.parse_args()
    run_crawling_pipeline(args.limit)