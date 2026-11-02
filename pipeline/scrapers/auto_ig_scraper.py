"""
Auto Instagram Scraper (tanpa GUI) untuk db_sentimenanalisis_v2.
Dirancang untuk dijalankan lewat Windows Task Scheduler.

Contoh:
  # sekali saja (interaktif): login & simpan sesi ke ig_session.json
  python auto_ig_scraper.py --login

  # scraping terjadwal: keyword aktif Instagram diambil dari tabel `keywords` di database
  # (keyword "@kementan.ri" = akun, "#pertanian" = hashtag/pencarian keyword,
  #  "andi amran" = mode default --mode)
  python auto_ig_scraper.py -l 10

  # opsional: override keyword database (untuk tes manual)
  python auto_ig_scraper.py -m hashtag -k pertanian politik "andi amran" -l 10
  python auto_ig_scraper.py -m akun -k kementan.ri -l 15

  # lihat browsernya (debug)
  python auto_ig_scraper.py -m hashtag -k pertanian -l 5 --no-headless

Catatan mode "hashtag":
  Instagram mengalihkan /explore/tags/<tag>/ ke /explore/search/keyword/?q=%23<tag>
  dan halaman itu sering error ("Ada masalah dan halaman tidak bisa dimuat") kalau q
  diawali '#'. Karena itu mode hashtag sekarang langsung membuka
  /explore/search/keyword/?q=<kata> (tanpa '#').

Exit code: 0 = sukses, 1 = error, 2 = sesi login tidak valid, 3 = terlalu banyak gagal
           berturut-turut (kemungkinan diblokir), 4 = proses sebelumnya masih berjalan.
"""
import argparse
import json
import logging
import os
import random
import re
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

# Menambahkan root project directory (1 tingkat di atas folder script saat ini) ke sys.path
sys.path.append(str(Path(__file__).resolve().parent.parent))

from config.config import LOG_DIR, SESSION_DIR, get_db_connection

# Semua path memakai folder script (Task Scheduler biasanya start di C:\Windows\System32)
BASE_DIR = Path(__file__).resolve().parent
LOG_FILE = LOG_DIR / "auto_ig_scraper.log"
LOCK_FILE = BASE_DIR / "auto_ig_scraper.lock"

# Lock dianggap basi (proses mati mendadak) setelah N detik
LOCK_MAX_AGE = 6 * 3600
# Hentikan run kalau N post berturut-turut gagal diambil (kemungkinan kena blokir / rate limit)
MAX_CONSECUTIVE_FAIL = 5

# --- LOGGING SETUP ---
if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_handlers = [logging.FileHandler(LOG_FILE, encoding="utf-8")]
if sys.stderr is not None:
    _handlers.insert(0, logging.StreamHandler())

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=_handlers,
)

# File sesi login Instagram (dipakai bersama oleh ig_scraper.py & ig_comment_scraper.py)
SESSION_FILE = SESSION_DIR / "ig_session.json"

# Berhenti scroll kalau N kali scroll berturut-turut tidak ada post baru
MAX_IDLE_SCROLLS = 5
# Batas pengaman jumlah scroll per pencarian
MAX_SCROLLS = 60
# Batas waktu menunggu login manual (detik)
LOGIN_TIMEOUT = 300
# Berapa kali halaman pencarian/profil dicoba dimuat ulang kalau Instagram menampilkan halaman error
MAX_PAGE_RETRIES = 3

# Teks halaman error Instagram (ID & EN)
ERROR_TEXTS = (
    "Ada masalah dan halaman tidak bisa dimuat",
    "Halaman tidak bisa dimuat",
    "Something went wrong",
    "Sorry, something went wrong",
    "Page isn't available",
    "Halaman ini tidak tersedia",
)

POST_RE = re.compile(r"/(p|reel)/([A-Za-z0-9_-]+)")

JS_HREFS = "els => els.map(a => a.getAttribute('href') || '')"

# JS: ambil detail dari halaman post (username, caption, tanggal, gambar)
JS_DETAIL = r"""
() => {
    const meta = (n) => {
        const e = document.querySelector(`meta[property="${n}"]`);
        return e ? (e.getAttribute('content') || '') : '';
    };
    const RESERVED = new Set(['explore', 'reels', 'reel', 'accounts', 'direct', 'stories',
                              'p', 'tv', 'about', 'legal', 'privacy', 'web', 'api']);
    const NAV_SEL = 'nav, [role="navigation"]';
    const handleOf = (a) => {
        const m = (a.getAttribute('href') || '').match(/^\/([A-Za-z0-9._]+)\/?$/);
        return (m && !RESERVED.has(m[1].toLowerCase())) ? m[1] : '';
    };

    // Link profil di sidebar/nav = akun yang sedang login -> dicatat untuk DIKECUALIKAN
    const navHandles = [];
    document.querySelectorAll('nav a[href^="/"], [role="navigation"] a[href^="/"]').forEach(a => {
        const h = handleOf(a);
        if (h && !navHandles.includes(h)) navHandles.push(h);
    });

    // 1) Username author: hanya dari dalam <article> post, di luar nav, bukan akun login
    const article = document.querySelector('main article') || document.querySelector('article');
    const root = article || document;
    let handle = '';
    if (article) {
        const cands = [
            ...Array.from(article.querySelectorAll('header a[href^="/"]')),
            ...Array.from(article.querySelectorAll('a[href^="/"]')),
        ];
        for (const a of cands) {
            if (a.closest(NAV_SEL)) continue;
            const h = handleOf(a);
            if (h && !navHandles.includes(h)) { handle = h; break; }
        }
    }

    // 2) Username & tanggal dari JSON-LD (kalau ada)
    let ldHandle = '', ldDate = '';
    document.querySelectorAll('script[type="application/ld+json"]').forEach(s => {
        try {
            let j = JSON.parse(s.textContent);
            if (Array.isArray(j)) j = j[0];
            const au = j && j.author;
            const a0 = Array.isArray(au) ? au[0] : au;
            if (a0 && !ldHandle) {
                const cand = String(a0.alternateName || a0.identifier || '');
                const mm = cand.match(/@?([A-Za-z0-9._]+)/);
                if (mm) ldHandle = mm[1];
            }
            if (j && !ldDate) ldDate = j.uploadDate || j.datePublished || '';
        } catch (e) {}
    });

    const h1 = root.querySelector('h1') || document.querySelector('h1');
    const t = root.querySelector('time[datetime]') || document.querySelector('time[datetime]');
    const imgs = Array.from(root.querySelectorAll('img'))
        .filter(i => (i.naturalWidth || 0) >= 300)
        .map(i => i.currentSrc || i.src);
    return {
        handle: handle,
        navHandles: navHandles,
        ldHandle: ldHandle,
        ldDate: ldDate,
        h1: h1 ? h1.innerText : '',
        ogTitle: meta('og:title'),
        ogDesc: meta('og:description'),
        ogImage: meta('og:image'),
        datetime: t ? t.getAttribute('datetime') : '',
        imgs: imgs
    };
}
"""


# ---------------------------------------------------------------- Helpers
def normalize_post_url(href: str):
    """Return (kind, shortcode, clean_url) atau (None, None, None)."""
    m = POST_RE.search(href or "")
    if not m:
        return None, None, None
    kind, code = m.group(1), m.group(2)
    return kind, code, f"https://www.instagram.com/{kind}/{code}/"


def parse_caption(d: dict) -> str:
    cap = (d.get("h1") or "").strip()
    if cap:
        return cap

    m = re.search(r'on Instagram:\s*[“"](.*)[”"]\s*$', d.get("ogTitle") or "", re.S)
    if m:
        return m.group(1).strip()

    m = re.search(r':\s*[“"](.*)[”"]\.?\s*$', d.get("ogDesc") or "", re.S)
    if m:
        return m.group(1).strip()
    return ""


def resolve_handle(d: dict):
    """
    Return (username_tanpa_@, sumber).
    Prioritas: JSON-LD -> og:title '(@user)' -> og:description -> link header di <article>
    -> og:title polos. Link sidebar (akun yang login) tidak pernah dipakai.
    """
    if d.get("ldHandle"):
        return d["ldHandle"], "json-ld"

    m = re.search(r"\(@([\w.]+)\)", d.get("ogTitle") or "")
    if m:
        return m.group(1), "og:title"

    # "1,234 likes, 56 comments - username on October 1, 2026: ..." (bagian statistik tanpa '-', ':' atau tanda kutip)
    m = re.match(r'^[^\-:"“]{0,80}\s-\s*([\w.]+)\s+(?:on|pada)\s', d.get("ogDesc") or "")
    if m:
        return m.group(1), "og:description"

    if d.get("handle"):
        return d["handle"], "header-article"

    # og:title "username on Instagram: ..." (kurang pasti: bisa nama tampilan)
    m = re.match(r"^([\w.]+)\s+on Instagram", d.get("ogTitle") or "")
    if m:
        return m.group(1), "og:title-polos"

    return "unknown", "-"


def parse_handle(d: dict) -> str:
    return resolve_handle(d)[0]


def parse_display_name(d: dict) -> str:
    m = re.match(r"^(.*?)\s+on Instagram", d.get("ogTitle") or "")
    return m.group(1).strip() if m else ""


def one_line(text: str, max_len: int = 150) -> str:
    return re.sub(r"\s+", " ", text or "").strip()[:max_len]


def extract_post_detail(page, clean_url: str, fallback_author: str = None):
    """
    Buka halaman post & ambil username, caption, tanggal terbit, gambar.
    Return (detail_dict, None) kalau sukses, atau (None, 'login' | 'notloaded').
    """
    page.goto(clean_url, wait_until="domcontentloaded", timeout=45000)
    time.sleep(2)

    if is_login_page(page.url):
        return None, "login"

    try:
        page.wait_for_selector("meta[property='og:title']", state="attached", timeout=15000)
    except Exception:
        return None, "notloaded"

    d = page.evaluate(JS_DETAIL)

    caption = parse_caption(d)
    handle, handle_source = resolve_handle(d)
    if handle == "unknown" and fallback_author and fallback_author.startswith("@"):
        handle = fallback_author.lstrip("@")

    username = f"@{handle}"
    display = parse_display_name(d)
    author_full = f"{display} ({username})" if display and display.lower() != handle.lower() else username

    media_urls = []
    for u in [d.get("ogImage")] + list(d.get("imgs") or []):
        if u and u not in media_urls and "profile" not in u.lower():
            media_urls.append(u)

    # Judul untuk kolom contents.title (tidak ditampilkan di GUI)
    if caption:
        title = f"Post Instagram oleh {author_full}: {caption[:60]}..."
    else:
        title = f"Post Instagram oleh {author_full}"

    return {
        "author": username,
        "username": username,
        "handle_source": handle_source,
        "title": title,
        "caption": caption,
        "published_at": parse_published(d),
        "media_urls": media_urls,
    }, None


def parse_iso_datetime(value: str):
    """ISO 8601 -> 'YYYY-MM-DD HH:MM:SS' (UTC). None kalau gagal."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def parse_published(d: dict):
    """Tanggal terbit post: <time datetime> -> JSON-LD -> teks og:description."""
    for key in ("datetime", "ldDate"):
        v = parse_iso_datetime(d.get(key))
        if v:
            return v

    m = re.search(r"\bon ([A-Z][a-z]+ \d{1,2}, \d{4})", d.get("ogDesc") or "")
    if m:
        try:
            return datetime.strptime(m.group(1), "%B %d, %Y").strftime("%Y-%m-%d 00:00:00")
        except ValueError:
            pass
    return None


def get_or_create_instagram_platform_id() -> int:
    """Cari platform Instagram di tabel platforms (code='instagram'); buat kalau belum ada."""
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT id FROM platforms WHERE code = %s LIMIT 1", ("instagram",))
        row = cursor.fetchone()
        if row:
            return row["id"]

        cursor.execute(
            "INSERT INTO platforms (code, name, rss_url) VALUES (%s, %s, %s)",
            ("instagram", "Instagram", "https://www.instagram.com"),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        cursor.close()
        conn.close()


def get_active_keywords_for_instagram(cursor, platform_id: int) -> list:
    """Mengambil daftar keyword aktif dari database khusus untuk platform Instagram."""
    cursor.execute(
        """
        SELECT keyword
        FROM keywords
        WHERE platform_id = %s AND is_active = 1
        """,
        (platform_id,),
    )
    return [row["keyword"] for row in cursor.fetchall()]


def build_jobs(raw_keywords: list, default_mode: str) -> list:
    """
    Ubah daftar keyword mentah menjadi daftar (mode, query) unik.
    Aturan penentuan mode per keyword:
      "@kementan.ri" -> akun
      "#pertanian"   -> hashtag (pencarian keyword)
      "andi amran"   -> mode default (--mode, default: hashtag)
    """
    jobs, seen = [], set()
    for raw in raw_keywords:
        kw = (raw or "").strip()
        if not kw:
            continue

        if kw.startswith("@"):
            mode, query = "akun", kw.lstrip("@").strip()
        elif kw.startswith("#"):
            mode, query = "hashtag", kw.lstrip("#").strip()
        else:
            mode, query = default_mode, kw

        if not query:
            continue

        # Kunci dedup: spasi dirapikan & huruf kecil (spasi di keyword dipertahankan)
        norm = re.sub(r"\s+", " ", query).strip().lower()
        if (mode, norm) in seen:
            continue
        seen.add((mode, norm))
        jobs.append((mode, query))
    return jobs


def is_login_page(url: str) -> bool:
    return "accounts/login" in url or "/challenge" in url


def page_has_error(page) -> bool:
    """True kalau Instagram menampilkan halaman error ('Ada masalah dan halaman tidak bisa dimuat', dll)."""
    for txt in ERROR_TEXTS:
        try:
            if page.get_by_text(txt).count() > 0:
                return True
        except Exception:
            pass
    return False


def has_login_form(page) -> bool:
    """True kalau halaman menampilkan form login (login wall tanpa redirect URL)."""
    try:
        return page.locator('input[name="username"], input[name="password"]').count() > 0
    except Exception:
        return False


def dump_debug(page, label: str):
    """Simpan screenshot + cuplikan teks halaman untuk diagnosis kalau tidak ada post terdeteksi."""
    try:
        shot = BASE_DIR / f"debug_ig_{int(time.time())}.png"
        page.screenshot(path=str(shot))
        try:
            body = one_line(page.inner_text("body"), 200)
        except Exception:
            body = "-"
        logging.warning(f"   [DEBUG] {label} | URL={page.url} | screenshot={shot.name} | teks='{body}'")
    except Exception as e:
        logging.warning(f"   [DEBUG] gagal simpan screenshot: {e}")


# ------------------------------------------------------------------ Pipeline
class SessionExpired(Exception):
    """Sesi Instagram tidak valid / kedaluwarsa."""


class TooManyFailures(Exception):
    """Terlalu banyak post gagal berturut-turut."""


def build_search_url(mode: str, query: str):
    """
    hashtag -> halaman pencarian keyword TANPA '#' (q=%23... membuat Instagram menampilkan error)
    akun    -> halaman profil
    """
    if mode == "hashtag":
        term = re.sub(r"\s+", " ", query.lstrip("#")).strip()
        return f"https://www.instagram.com/explore/search/keyword/?q={urllib.parse.quote(term)}", None
    username = query.lstrip("@").strip()
    return f"https://www.instagram.com/{urllib.parse.quote(username)}/", f"@{username}"


def run_login():
    """Mode interaktif: buka browser, login manual, simpan sesi."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(viewport={"width": 1100, "height": 800}, locale="id-ID")
        page = context.new_page()
        page.goto("https://www.instagram.com/accounts/login/")

        logging.info(f"Silakan login di jendela browser (maks {LOGIN_TIMEOUT // 60} menit)...")

        deadline = time.time() + LOGIN_TIMEOUT
        logged_in = False
        while time.time() < deadline:
            try:
                cookies = context.cookies("https://www.instagram.com")
            except Exception:
                break  # browser ditutup user
            if any(c["name"] == "sessionid" and c["value"] for c in cookies):
                logged_in = True
                break
            time.sleep(2)

        if logged_in:
            time.sleep(3)
            context.storage_state(path=str(SESSION_FILE))
            logging.info(f"Sesi login disimpan ke {SESSION_FILE}")
        else:
            logging.error("Login tidak terdeteksi / waktu habis. Sesi tidak disimpan.")

        browser.close()
    return 0 if logged_in else 1


def acquire_lock() -> bool:
    """Cegah dua run tumpang tindih (Task Scheduler bisa memicu run baru saat run lama belum selesai)."""
    for _ in range(2):
        try:
            fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return True
        except FileExistsError:
            try:
                age = time.time() - LOCK_FILE.stat().st_mtime
            except FileNotFoundError:
                continue
            if age < LOCK_MAX_AGE:
                return False
            logging.warning(f"Lock lama ({int(age)} detik) dianggap basi, dihapus.")
            try:
                LOCK_FILE.unlink()
            except FileNotFoundError:
                pass
    return False


def release_lock():
    try:
        LOCK_FILE.unlink()
    except FileNotFoundError:
        pass


def refresh_db(conn, cursor):
    """Pastikan koneksi MySQL masih hidup; sambung ulang otomatis kalau putus."""
    try:
        if cursor:
            cursor.close()
    except Exception:
        pass

    try:
        conn.ping(reconnect=True, attempts=3, delay=2)
    except Exception:
        try:
            conn.close()
        except Exception:
            pass
        logging.warning("Koneksi DB putus, menyambung ulang...")
        conn = get_db_connection()

    return conn, conn.cursor(dictionary=True)


def open_listing_page(page, url: str) -> bool:
    """
    Buka halaman pencarian/profil dan tunggu link post muncul.
    - Redirect ke login / form login terlihat -> SessionExpired
    - Halaman error Instagram -> reload sampai MAX_PAGE_RETRIES kali
    Return True kalau link post ditemukan, False kalau tidak.
    """
    post_sel = 'a[href*="/p/"], a[href*="/reel/"]'

    for attempt in range(1, MAX_PAGE_RETRIES + 1):
        if attempt == 1:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
        else:
            page.reload(wait_until="domcontentloaded", timeout=45000)
        time.sleep(3)

        if is_login_page(page.url):
            raise SessionExpired()

        try:
            page.wait_for_selector(post_sel, timeout=15000)
            return True
        except Exception:
            pass

        if has_login_form(page):
            raise SessionExpired()

        if page_has_error(page):
            logging.warning(f"   Halaman error dari Instagram (percobaan {attempt}/{MAX_PAGE_RETRIES}): {page.url}")
            time.sleep(random.uniform(8, 15))
            continue

        # Bukan error & bukan login wall, tapi memang tidak ada post
        break

    dump_debug(page, "Tidak ada post terdeteksi (halaman kosong / dibatasi / selector berubah)")
    return False


def collect_post_links(page, url: str, limit: int, known_author):
    """Buka halaman hashtag/akun, scroll, dan kumpulkan link post. Return list dict."""
    if not open_listing_page(page, url):
        return []

    found = {}
    idle = 0
    for _ in range(MAX_SCROLLS):
        hrefs = page.eval_on_selector_all('a[href*="/p/"], a[href*="/reel/"]', JS_HREFS)
        before = len(found)

        for h in hrefs:
            kind, code, clean_url = normalize_post_url(h)
            if not code or code in found:
                continue
            found[code] = {"shortcode": code, "kind": kind, "clean_url": clean_url}
            if len(found) >= limit:
                break

        if len(found) >= limit:
            break

        idle = idle + 1 if len(found) == before else 0
        if idle >= MAX_IDLE_SCROLLS:
            logging.info("   Tidak ada post baru lagi, berhenti scroll.")
            break

        page.mouse.wheel(0, random.randint(1500, 2400))
        time.sleep(random.uniform(1.5, 3.0))

    return list(found.values())[:limit]


def run_pipeline(mode, queries, limit, headless, delay_min, delay_max):
    """
    Fungsi utama eksekusi batch crawler Instagram secara otomatis.
    Keyword diambil dari tabel `keywords` (platform Instagram, is_active = 1).
    Kalau `queries` diisi (lewat -k), daftar itu dipakai sebagai pengganti keyword database.
    """
    platform_id = get_or_create_instagram_platform_id()
    logging.info(f"Platform Instagram: platform_id = {platform_id}")

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    stats = {"inserted": 0, "skipped": 0, "failed": 0}

    try:
        # Tarik keyword dari database khusus untuk Platform Instagram
        if queries:
            raw_keywords = queries
            logging.info(f"Memakai keyword dari argumen -k (override database): {raw_keywords}")
        else:
            raw_keywords = get_active_keywords_for_instagram(cursor, platform_id)
            if not raw_keywords:
                logging.info("Tidak ada keyword aktif di database untuk platform Instagram.")
                return stats
            logging.info(f"=== Menjalankan Crawling Instagram untuk Keywords DB: {raw_keywords} ===")

        jobs = build_jobs(raw_keywords, mode)
        if not jobs:
            logging.info("Tidak ada keyword valid untuk diproses.")
            return stats

        # Cache URL yang sudah tersimpan di database untuk menghindari duplikasi
        cursor.execute("SELECT clean_url FROM contents WHERE platform_id = %s", (platform_id,))
        existing_urls = {row["clean_url"] for row in cursor.fetchall()}

        consecutive_fail = 0

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=headless)
            try:
                context = browser.new_context(
                    storage_state=str(SESSION_FILE),
                    viewport={"width": 1100, "height": 900},
                    locale="id-ID",
                )
                page = context.new_page()

                for qi, (q_mode, query) in enumerate(jobs):
                    url, known_author = build_search_url(q_mode, query)
                    logging.info(f"=== Memulai Crawling Instagram ({q_mode}): '{query}' -> {url} ===")

                    posts = collect_post_links(page, url, limit, known_author)
                    new_posts = [x for x in posts if x["clean_url"] not in existing_urls]
                    dup = len(posts) - len(new_posts)
                    stats["skipped"] += dup
                    logging.info(f"Ditemukan {len(posts)} post ({len(new_posts)} baru, {dup} duplikat DB).")

                    for n, post in enumerate(new_posts, start=1):
                        clean_url = post["clean_url"]

                        try:
                            detail, err = extract_post_detail(page, clean_url, known_author)
                        except Exception as e:
                            detail, err = None, f"error: {e}"

                        if err == "login":
                            raise SessionExpired()

                        if detail is None:
                            stats["failed"] += 1
                            consecutive_fail += 1
                            logging.warning(f"   [GAGAL] ({n}/{len(new_posts)}) {err}: {clean_url}")
                            if consecutive_fail >= MAX_CONSECUTIVE_FAIL:
                                raise TooManyFailures(f"{consecutive_fail} post gagal berturut-turut")
                            time.sleep(random.uniform(delay_min, delay_max))
                            continue

                        consecutive_fail = 0

                        username = detail["username"]
                        caption = detail["caption"]
                        media_urls = detail["media_urls"]
                        media_urls_json = json.dumps(media_urls) if media_urls else None
                        raw_text = f"{username} - {caption}" if caption else username

                        try:
                            conn, cursor = refresh_db(conn, cursor)
                            cursor.execute(
                                """
                                INSERT IGNORE INTO contents
                                (platform_id, clean_url, original_id, title, raw_text, media_urls, published_at)
                                VALUES (%s, %s, %s, %s, %s, %s, %s)
                                """,
                                (
                                    platform_id, clean_url, post["shortcode"], detail["title"],
                                    raw_text, media_urls_json, detail["published_at"],
                                ),
                            )

                            if cursor.rowcount > 0:
                                content_id = cursor.lastrowid
                                cursor.execute(
                                    """
                                    INSERT IGNORE INTO sentiment_results
                                    (target_type, target_id, execution_source, status)
                                    VALUES ('content', %s, 'ml_pipeline', 'pending')
                                    """,
                                    (content_id,),
                                )
                                conn.commit()
                                existing_urls.add(clean_url)
                                stats["inserted"] += 1
                                logging.info(
                                    f"   [INSERTED] ({n}/{len(new_posts)}) {username} "
                                    f"[{detail['handle_source']}] | {detail['published_at'] or '-'} | "
                                    f"{len(media_urls)} gambar -> {clean_url}"
                                )
                            else:
                                existing_urls.add(clean_url)
                                stats["skipped"] += 1
                                logging.info(f"   [SKIPPED IGNORE] {clean_url}")

                        except Exception as db_err:
                            try:
                                conn.rollback()
                            except Exception:
                                pass
                            stats["failed"] += 1
                            logging.error(f"   [DB ERROR] {clean_url}: {db_err}")

                        time.sleep(random.uniform(delay_min, delay_max))

                    if qi < len(jobs) - 1:
                        time.sleep(random.uniform(10, 20))  # jeda antar kata kunci

            finally:
                browser.close()

    finally:
        try:
            cursor.close()
            conn.close()
        except Exception:
            pass
        logging.info(
            f"=== BATCH CRAWLING INSTAGRAM SELESAI | Inserted Baru: {stats['inserted']} | "
            f"Skipped/Duplikat: {stats['skipped']} | Gagal: {stats['failed']} ==="
        )

    return stats


def main():
    parser = argparse.ArgumentParser(
        description="Headless Instagram Scraper untuk db_sentimenanalisis_v2 (Windows Task Scheduler)"
    )
    parser.add_argument("--login", action="store_true",
                        help="Login manual lewat browser & simpan sesi (jalankan sekali, bukan lewat Task Scheduler)")
    parser.add_argument("-m", "--mode", choices=["hashtag", "akun"], default="hashtag",
                        help="Mode default untuk keyword tanpa awalan (default: hashtag). "
                             "Keyword berawalan @ selalu akun, berawalan # selalu hashtag")
    parser.add_argument("-k", "--keywords", nargs="+",
                        help="OPSIONAL: override keyword database (untuk tes manual). Jika tidak diisi, "
                             "keyword aktif Instagram diambil dari tabel keywords")
    parser.add_argument("-l", "--limit", type=int, default=10,
                        help="Batas post per kata kunci / akun (default: 10)")
    parser.add_argument("--headless", action=argparse.BooleanOptionalAction, default=True,
                        help="Jalankan browser tanpa tampilan (default: ya; matikan dengan --no-headless)")
    parser.add_argument("--delay-min", type=float, default=3.0, help="Jeda minimum antar post, detik (default: 3)")
    parser.add_argument("--delay-max", type=float, default=6.0, help="Jeda maksimum antar post, detik (default: 6)")

    args = parser.parse_args()

    if args.login:
        sys.exit(run_login())

    if args.delay_max < args.delay_min:
        parser.error("--delay-max harus >= --delay-min")

    if not SESSION_FILE.exists():
        logging.error(f"Sesi {SESSION_FILE} belum ada. Jalankan dulu: python {Path(__file__).name} --login")
        sys.exit(2)

    if not acquire_lock():
        logging.warning("Proses sebelumnya masih berjalan (lock aktif). Run ini dibatalkan.")
        sys.exit(4)

    code = 0
    try:
        run_pipeline(args.mode, args.keywords, args.limit, args.headless, args.delay_min, args.delay_max)
    except SessionExpired:
        logging.error(
            "Sesi Instagram tidak valid / kedaluwarsa. "
            f"Jalankan ulang: python {Path(__file__).name} --login"
        )
        code = 2
    except TooManyFailures as e:
        logging.error(f"Dihentikan: {e}. Kemungkinan akun kena rate limit / blokir; coba lagi nanti.")
        code = 3
    except Exception:
        logging.exception("Error tak terduga")
        code = 1
    finally:
        release_lock()

    sys.exit(code)


if __name__ == "__main__":
    main()