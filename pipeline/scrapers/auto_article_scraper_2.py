import argparse
import html
import json
import logging
import re
import sys
import time
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from googlenewsdecoder import gnewsdecoder
from mysql.connector import Error

# Menambahkan root project directory (1 tingkat di atas folder script saat ini) ke sys.path
sys.path.append(str(Path(__file__).resolve().parent.parent))

from config.config import LOG_DIR, get_db_connection


# --- LOGGING SETUP ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_DIR / "auto_scraper_extra.log", encoding="utf-8"),
    ],
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "Chrome/122.0.0.0 Safari/537.36"
    )
}

# --- KONFIGURASI SUMBER ---
# code      : harus sama persis dengan kolom platforms.code di database
# domain    : dipakai untuk query "site:" Google News & validasi host hasil decode
# selectors : CSS selector kontainer isi artikel (dicoba berurutan, diambil yang teksnya terpanjang)
SOURCES = [
    {
        "code": "cnn",
        "name": "CNN Indonesia",
        "domain": "cnnindonesia.com",
        "selectors": ["div.detail-text", "div.detail_text"],
    },
    {
        "code": "tribun",
        "name": "Tribunnews",
        "domain": "tribunnews.com",  # termasuk subdomain *.tribunnews.com
        "selectors": ["div.side-article.txt-article", "div.txt-article"],
    },
    {
        "code": "metrotv",
        "name": "Metro TV News",
        "domain": "metrotvnews.com",
        "selectors": ["div.news-text", "div.news-content"],
    },
    {
        "code": "beritasatu",
        "name": "Berita Satu",
        "domain": "beritasatu.com",
        "selectors": ["div.body-content", "div.article-content", "div.content-detail"],
    },
    {
        "code": "kabarika",
        "name": "Kabarika ID",
        "domain": "kabarika.id",
        "selectors": ["div.entry-content", "div.td-post-content", "div.tdb-block-inner"],
    },
    {
        "code": "republika",
        "name": "Republika",
        "domain": "republika.co.id",
        "selectors": ["div.article-content", "div.artikel"],
    },
    {
        "code": "idntimes",
        "name": "IDN Times",
        "domain": "idntimes.com",
        "selectors": ["div#article-content", "div.content-article", "article"],
    },
    {
        "code": "rri",
        "name": "RRI",
        "domain": "rri.co.id",
        "selectors": ["div.content-detail", "div.detail-content", "div.post-content"],
    },
    {
        "code": "pertanian",
        "name": "Kementerian Pertanian",
        "domain": "pertanian.go.id",
        "selectors": ["div.entry-content", "div.post-content", "div.content-detail"],
        # Situs Kementan punya banyak subdomain dengan struktur URL berbeda.
        # URL harus cocok SALAH SATU pola allow, dan TIDAK cocok pola deny.
        "article_url_patterns": [
            r"/news/view/\d+",                                  # pertanian.go.id/index.php/news/view/8226
            r"/(?:index\.php/)?(?:en/)?(?:berita|artikel)/[^/?#]+",  # brmp, ditjenpkh, bpmsph, tanamanpangan
            r"/post/berita/[^/?#]+",                            # bppsdmp
            r"^https?://[^/]+/[a-z0-9]+(?:-[a-z0-9]+){4,}/?$",  # slug panjang di root (ditjenbun)
        ],
        "deny_url_patterns": [
            r"/(?:download|regulasi|peraturan|informasi-publik)(?:/|$)",
            r"/news/all",
            r"[?&]page=",
        ],
    },
    {
        "code": "viva",
        "name": "VIVA",
        "domain": "viva.co.id",
        "selectors": ["div.main-content-detail", "div.content-detail", "div.article-content"],
    },
]

# Selector umum bila selector khusus situs tidak menemukan apa pun
GENERIC_SELECTORS = [
    "[itemprop='articleBody']",
    "div.entry-content",
    "div.post-content",
    "div.article-content",
    "div.detail-text",
    "div.content-detail",
    "article",
]

# --- KONFIGURASI VALIDASI ARTIKEL ---
BAD_URL_PATTERNS = re.compile(
    r"/(tag|tags|topik|topic|foto|photo|galeri|gallery|video|videos|indeks|index|search|cari)(/|$|\?)",
    re.IGNORECASE,
)
VALID_OG_TYPES = ("article", "news", "newsarticle")
MIN_TEXT_LEN = 300
MIN_PARAGRAPHS = 3
# Batas ukuran raw_text (byte UTF-8). 60000 aman untuk kolom TEXT (maks 65535 byte).
# Jika kolom sudah diubah ke MEDIUMTEXT/LONGTEXT, nilai ini boleh dinaikkan.
MAX_RAW_TEXT_BYTES = 60000
REQUEST_DELAY = 0.5  # detik jeda antar scraping agar tidak membebani server

# Paragraf sisipan yang bukan isi berita
NOISE_PARAGRAPH = re.compile(
    r"^(baca juga|baca selengkapnya|simak juga|lihat juga|advertisement|scroll to continue|iklan)\b",
    re.IGNORECASE,
)


def check_source_url(source, url):
    """Cek URL terhadap aturan khusus sumber. Return (ok, alasan)."""
    if not source:
        return True, "ok"
    for pat in source.get("deny_url_patterns", []):
        if re.search(pat, url, re.IGNORECASE):
            return False, "URL termasuk halaman non-artikel (deny)"
    allow = source.get("article_url_patterns", [])
    if allow and not any(re.search(pat, url, re.IGNORECASE) for pat in allow):
        return False, "URL tidak cocok pola artikel"
    return True, "ok"


def truncate_text(text, max_bytes=MAX_RAW_TEXT_BYTES):
    """Potong teks agar tidak melebihi max_bytes (UTF-8) tanpa merusak karakter."""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    logging.warning(f"raw_text dipotong dari {len(encoded)} ke {max_bytes} byte")
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def is_bad_url(clean_url, domain=None, source=None):
    """Cek cepat (tanpa request) apakah URL jelas bukan halaman artikel."""
    if "news.google.com" in clean_url:
        return True, "decode URL gagal (masih link Google News)"
    if domain:
        host = (urlparse(clean_url).hostname or "").lower()
        if not (host == domain or host.endswith("." + domain)):
            return True, f"domain bukan {domain} ({host})"
    ok, why = check_source_url(source, clean_url)
    if not ok:
        return True, why
    if BAD_URL_PATTERNS.search(clean_url):
        return True, "URL bukan halaman artikel (tag/foto/video/indeks)"
    return False, "ok"


def is_valid_article(clean_url, raw_text, og_type=None, domain=None, source=None):
    """Validasi akhir setelah scraping. Return (bool, alasan)."""
    bad, reason = is_bad_url(clean_url, domain, source)
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
        logging.warning(f"Gagal decode Google URL [{rss_url}]: {e}")

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
    """Ambil teks paragraf (tidak kosong, bukan sisipan 'Baca juga' dll.) dari sebuah elemen."""
    if not container:
        return []
    result = []
    for p in container.find_all("p"):
        text = p.get_text(" ", strip=True)
        if text and not NOISE_PARAGRAPH.match(text):
            result.append(text)
    return result


def _jsonld_paragraphs(soup):
    """Fallback terakhir: ambil articleBody dari JSON-LD bila ada."""
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except Exception:
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if isinstance(item, dict) and item.get("articleBody"):
                body = html.unescape(str(item["articleBody"]))
                return [line.strip() for line in body.splitlines() if line.strip()]
    return []


def _best_paragraphs(soup, selectors):
    """Coba semua selector, kembalikan paragraf dengan total teks terpanjang."""
    best = []
    best_len = 0
    for sel in selectors:
        try:
            container = soup.select_one(sel)
        except Exception:
            continue
        paragraphs = _extract_paragraphs(container)
        total = sum(len(p) for p in paragraphs)
        if total > best_len:
            best, best_len = paragraphs, total
    return best


def scrape_article_detail(clean_url, source):
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

        # 0. og:type
        og_type_tag = soup.find("meta", property="og:type") or soup.find(
            "meta", attrs={"name": "og:type"}
        )
        if og_type_tag and og_type_tag.get("content"):
            og_type = og_type_tag["content"].strip()

        # 1. Gambar utama
        og_image = soup.find("meta", property="og:image") or soup.find(
            "meta", attrs={"name": "og:image"}
        )
        if og_image and og_image.get("content"):
            media_urls.append(og_image["content"])

        # JSON-LD diambil sebelum <script> dibuang
        jsonld_paragraphs = _jsonld_paragraphs(soup)

        # Buang elemen yang bukan isi berita
        for tag in soup(["script", "style", "noscript", "aside", "figure", "iframe", "form"]):
            tag.decompose()

        # 2. Selector khusus situs -> selector umum -> JSON-LD
        paragraphs = _best_paragraphs(soup, source["selectors"])
        if len(paragraphs) < MIN_PARAGRAPHS:
            generic = _best_paragraphs(soup, GENERIC_SELECTORS)
            if sum(len(p) for p in generic) > sum(len(p) for p in paragraphs):
                paragraphs = generic
        if len(paragraphs) < MIN_PARAGRAPHS and jsonld_paragraphs:
            paragraphs = jsonld_paragraphs

        # Fallback semua <p> HANYA jika halaman bertipe artikel
        if not paragraphs and og_type and og_type.lower() in VALID_OG_TYPES:
            paragraphs = [
                p.get_text(" ", strip=True)
                for p in soup.find_all("p")
                if len(p.get_text(strip=True)) > 30
            ]

        raw_text = truncate_text("\n\n".join(paragraphs))

    except Exception as e:
        logging.error(f"Gagal scrape detail artikel [{clean_url}]: {e}")

    return raw_text, media_urls, og_type


def parse_pub_date(pub_date_str):
    """Ubah pubDate RSS menjadi DATETIME MySQL (YYYY-MM-DD HH:MM:SS)."""
    if not pub_date_str:
        return None
    try:
        dt = parsedate_to_datetime(pub_date_str)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def clean_rss_title(raw_title, rss_source_name):
    """Bersihkan suffix ' - Nama Media' dari judul Google News."""
    title = html.unescape(raw_title or "").strip()
    if rss_source_name and title.endswith(f" - {rss_source_name}"):
        title = title[: -len(f" - {rss_source_name}")]
    else:
        title = re.sub(r"\s+-\s+[^-]+$", "", title)
    return title.strip()


def fetch_rss_news(source, keyword, limit):
    """Ambil RSS Google News (filter site:) beserta tanggal terbitnya."""
    articles = []
    query = f"{keyword} site:{source['domain']}"
    rss_url = (
        f"https://news.google.com/rss/search?q={requests.utils.quote(query)}"
        f"&hl=id&gl=ID&ceid=ID:id"
    )

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
                source_elem = item.find("source")

                if title_elem is not None and link_elem is not None:
                    link = link_elem.text or ""
                    rss_source_name = (
                        source_elem.text if source_elem is not None else None
                    )
                    title = clean_rss_title(title_elem.text, rss_source_name)
                    published_at = parse_pub_date(
                        pub_date_elem.text if pub_date_elem is not None else None
                    )

                    if title and link:
                        articles.append(
                            {
                                "platform_code": source["code"],
                                "source": source["name"],
                                "title": title,
                                "rss_url": link,
                                "published_at": published_at,
                            }
                        )
        else:
            logging.warning(
                f"RSS {source['name']} mengembalikan HTTP {response.status_code}"
            )
    except Exception as e:
        logging.error(f"Error fetching RSS {source['name']}: {e}")

    return articles


def get_active_keywords_by_platform(cursor, platform_id):
    """Ambil daftar keyword aktif dari database berdasarkan platform_id."""
    cursor.execute(
        "SELECT keyword FROM keywords WHERE platform_id = %s AND is_active = 1",
        (platform_id,),
    )
    return [row["keyword"] for row in cursor.fetchall()]


def run_crawling_pipeline(limit_per_source, only_codes=None):
    """Eksekusi pipeline crawling untuk sumber berita tambahan."""
    sources = [s for s in SOURCES if not only_codes or s["code"] in only_codes]

    total_inserted = 0
    total_skipped = 0
    total_invalid = 0

    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        cursor.execute("SELECT id, code FROM platforms")
        platform_map = {row["code"]: row["id"] for row in cursor.fetchall()}

        cursor.execute("SELECT clean_url, title FROM contents")
        existing_rows = cursor.fetchall()
        existing_titles = {
            row["title"].lower().strip() for row in existing_rows if row["title"]
        }
        existing_urls = {row["clean_url"] for row in existing_rows}

        for source in sources:
            p_code, p_name = source["code"], source["name"]
            p_id = platform_map.get(p_code)
            if not p_id:
                logging.warning(
                    f"Platform code '{p_code}' tidak ditemukan di tabel platforms! "
                    f"(jalankan DML insert platforms dulu)"
                )
                continue

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
                articles = fetch_rss_news(source, kw, limit_per_source)
                logging.info(
                    f"[{p_name}] Keyword: '{kw}' | Ditemukan {len(articles)} RSS link."
                )

                for item in articles:
                    title_clean = item["title"].lower().strip()

                    # PENCEGAHAN 1: judul sudah ada
                    if title_clean in existing_titles:
                        total_skipped += 1
                        logging.info(
                            f"   [SKIP - ADA DI DB] Judul: '{item['title'][:50]}...'"
                        )
                        continue

                    # Step 1: Decode ke Clean URL
                    clean_url = decode_google_news_url(item["rss_url"])

                    # PENCEGAHAN 2: URL sudah ada
                    if clean_url in existing_urls:
                        total_skipped += 1
                        logging.info(f"   [SKIP - URL DUPLIKAT] Clean URL: {clean_url}")
                        continue

                    # VALIDASI 1 (sebelum request)
                    bad, reason = is_bad_url(clean_url, source["domain"], source)
                    if bad:
                        total_invalid += 1
                        total_skipped += 1
                        existing_urls.add(clean_url)
                        logging.info(f"   [SKIP - TIDAK VALID] {reason} | {clean_url}")
                        continue

                    # Step 2: Scrape
                    logging.info(
                        f"   [PROCESSING] Scraping detail: {item['title'][:50]}..."
                    )
                    raw_text, media_urls, og_type = scrape_article_detail(
                        clean_url, source
                    )
                    time.sleep(REQUEST_DELAY)

                    # VALIDASI 2 (setelah scraping)
                    valid, reason = is_valid_article(
                        clean_url,
                        raw_text,
                        og_type,
                        source["domain"],
                        source,
                    )
                    if not valid:
                        total_invalid += 1
                        total_skipped += 1
                        existing_urls.add(clean_url)
                        logging.info(f"   [SKIP - TIDAK VALID] {reason} | {clean_url}")
                        continue

                    media_urls_json = json.dumps(media_urls) if media_urls else None

                    # Step 3: Simpan ke DB (error satu artikel tidak menghentikan batch)
                    try:
                        cursor.execute(
                            """
                            INSERT IGNORE INTO contents
                            (platform_id, clean_url, title, raw_text, media_urls, published_at)
                            VALUES (%s, %s, %s, %s, %s, %s)
                            """,
                            (
                                p_id,
                                clean_url,
                                item["title"],
                                raw_text,
                                media_urls_json,
                                item["published_at"],
                            ),
                        )
                    except Error as e:
                        conn.rollback()
                        total_skipped += 1
                        existing_urls.add(clean_url)
                        logging.error(f"   [SKIP - DB ERROR] {e} | {clean_url}")
                        continue

                    if cursor.rowcount > 0:
                        total_inserted += 1
                        content_id = cursor.lastrowid

                        existing_titles.add(title_clean)
                        existing_urls.add(clean_url)

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
    all_codes = [s["code"] for s in SOURCES]

    parser = argparse.ArgumentParser(
        description="Headless Article Scraper (sumber tambahan) untuk db_sentimenanalisis_v2"
    )
    parser.add_argument(
        "-l",
        "--limit",
        type=int,
        default=5,
        help="Batas artikel per keyword per sumber berita (default: 5)",
    )
    parser.add_argument(
        "-o",
        "--only",
        type=str,
        default=None,
        help=f"Jalankan hanya sumber tertentu, pisahkan koma. Pilihan: {', '.join(all_codes)}",
    )

    args = parser.parse_args()
    only = None
    if args.only:
        only = {c.strip().lower() for c in args.only.split(",") if c.strip()}
        unknown = only - set(all_codes)
        if unknown:
            parser.error(f"Kode sumber tidak dikenal: {', '.join(sorted(unknown))}")

    run_crawling_pipeline(args.limit, only)