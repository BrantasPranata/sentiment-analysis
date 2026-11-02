#!/usr/bin/env python3
"""
crawl_comments.py - Crawler komentar semua platform (non-GUI) untuk db_sentimenanalisis_v2.

Platform yang didukung : youtube, x, instagram, detik, kompas
(antaranews sengaja dilewati karena endpoint komentarnya tidak berfungsi)

Cara kerja:
  1. Untuk tiap platform, ambil N konten TERBARU dari tabel `contents`
     (urut published_at DESC)                                  -> -cl / --contentlimit
  2. Untuk tiap konten, ambil maksimal M komentar BARU dan
     simpan ke `comments` + antrikan ke `sentiment_results`    -> -l / --limit
     (duplikat: content_id + author + isi komentar -> dilewati, tidak dihitung)

Contoh:
  python crawl_comments.py                          # semua platform, -l 50 -cl 100
  python crawl_comments.py -l 30 -cl 50             # 30 komentar baru x 50 konten terbaru per platform
  python crawl_comments.py -p youtube,detik         # hanya platform tertentu
  python crawl_comments.py -p x,instagram --headed  # browser ditampilkan (debug)
  python crawl_comments.py --login x                # login manual X, simpan sesi x_session.json
  python crawl_comments.py --login instagram        # login manual IG, simpan sesi ig_session.json
"""

import argparse
import json
import logging
import random
import re
import sys
import time
from pathlib import Path
from urllib.parse import quote, urlparse

# Fix Path import config/Config (sama seperti scraper lainnya)
sys.path.append(str(Path(__file__).resolve().parent.parent))
from config.config import SESSION_DIR, get_db_connection

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("crawl_comments")

# =====================================================================
# KONFIGURASI
# =====================================================================
DEFAULT_LIMIT = 50           # -l  : maks komentar baru per konten
DEFAULT_CONTENT_LIMIT = 100  # -cl : jumlah konten terbaru per platform

ALL_PLATFORMS = ("youtube", "x", "instagram", "detik", "kompas")

# Konfigurasi platform.
#   fallback_id : platform_id yang dipakai scraper lama (dipakai lebih dulu kalau ada)
#   codes       : kode di tabel `platforms` (dipakai kalau fallback_id kosong)
#   url_like    : filter clean_url supaya hanya konten yang valid untuk di-crawl
PLATFORM_SPECS = {
    "youtube":   {"fallback_id": 5, "codes": ("youtube", "yt"), "url_like": None},
    "x":         {"fallback_id": 4, "codes": ("x", "twitter"), "url_like": ["%/status/%"]},
    "instagram": {"fallback_id": None, "codes": ("instagram",),
                  "url_like": ["%instagram.com/p/%", "%instagram.com/reel/%"]},
    "detik":     {"fallback_id": None, "codes": ("detik",), "url_like": None},
    "kompas":    {"fallback_id": None, "codes": ("kompas",), "url_like": None},
}

BASE_DIR = Path(__file__).resolve().parent
X_SESSION_FILE = SESSION_DIR / "x_session.json"
IG_SESSION_FILE = SESSION_DIR / "ig_session.json"
LOGIN_TIMEOUT = 300  # detik, menunggu login manual

# --- YouTube ---
INCLUDE_REPLIES = False
YT_MAX_FETCH_CAP = 2000

# --- X ---
X_MAX_SCROLLS = 80
X_MAX_IDLE_SCROLLS = 6

# --- Instagram ---
IG_MAX_SCROLLS = 40
IG_MAX_IDLE_SCROLLS = 4
COMMENT_MARKERS = ("comment_like_count", "child_comment_count", "parent_comment_id", "has_liked_comment_like")

# --- Berita ---
NEWS_MAX_FETCH_CAP = 500
NEWS_REQUEST_DELAY = 1.5
NEWS_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "id-ID,id;q=0.9,en;q=0.8",
}
COMMENT_ENDPOINTS = {
    "detik": [
        "https://apicomment.detik.com/detail-article/sneak-peek/?article_id={id}&channel_id={channel_id}&site=dtk",
    ],
    "kompas": [
        "https://apiscomment.kompas.com/list?urlpage={url_enc}&json&limit=100&type=article",
    ],
}
DOM_SELECTORS = {
    "detik": [
        ("div.komentar-item, li.komentar-item, div.comment-item",
         ".komentar-nama, .name, .comment-name", ".komentar-isi, .text, .comment-text"),
    ],
    "kompas": [
        ("div.komentar__item, li.komentar__item, div.comment-item",
         ".komentar__name, .name", ".komentar__text, .text"),
    ],
}
TEXT_KEYS = ("comment_text", "comment", "komentar", "text", "content", "message", "body", "isi")
AUTHOR_KEYS = ("author_name", "author", "user_name", "username", "nama", "name", "display_name", "fullname", "user")

# JS: ambil id, username, teks dari semua <article> tweet yang sedang ter-render
JS_EXTRACT = r"""
els => els.map(a => {
    const t = a.querySelector('time');
    const link = t ? t.closest('a') : null;
    const href = link ? (link.getAttribute('href') || '') : '';
    const m = href.match(/^\/([^\/]+)\/status\/(\d+)/);
    const textEl = a.querySelector('[data-testid="tweetText"]');
    return {
        handle: m ? m[1] : '',
        id: m ? m[2] : '',
        text: textEl ? textEl.innerText : ''
    };
})
"""

# JS: scroll panel komentar IG (fallback scroll halaman)
JS_SCROLL_COMMENTS = """
() => {
    let moved = false;
    document.querySelectorAll('div, ul').forEach(el => {
        const st = getComputedStyle(el);
        if ((st.overflowY === 'auto' || st.overflowY === 'scroll')
            && el.scrollHeight > el.clientHeight + 20
            && el.querySelector('time')) {
            el.scrollTop = el.scrollHeight;
            moved = true;
        }
    });
    if (!moved) window.scrollTo(0, document.body.scrollHeight);
    return moved;
}
"""


class SessionError(Exception):
    """Sesi login X/Instagram tidak valid / kedaluwarsa."""


class RateLimitError(Exception):
    """Platform membatasi request (HTTP 429)."""


# =====================================================================
# DATABASE
# =====================================================================
class DB:
    def __init__(self):
        self.conn = get_db_connection()
        self.cursor = self.conn.cursor(dictionary=True)

    def ensure(self):
        """Pastikan koneksi MySQL hidup; sambung ulang otomatis kalau putus."""
        try:
            if self.cursor:
                self.cursor.close()
        except Exception:
            pass
        try:
            self.conn.ping(reconnect=True, attempts=3, delay=2)
        except Exception:
            try:
                self.conn.close()
            except Exception:
                pass
            log.info("   Koneksi DB putus, menyambung ulang...")
            self.conn = get_db_connection()
        self.cursor = self.conn.cursor(dictionary=True)

    def rollback(self):
        try:
            self.conn.rollback()
        except Exception:
            pass

    def close(self):
        for obj in (self.cursor, self.conn):
            try:
                if obj:
                    obj.close()
            except Exception:
                pass

    def resolve_platform_ids(self, wanted):
        """Return {nama_platform: platform_id}. Platform yang tidak ketemu diberi peringatan."""
        self.ensure()
        self.cursor.execute("SELECT id, code FROM platforms")
        by_code = {str(r["code"]).lower(): r["id"] for r in self.cursor.fetchall()}

        result = {}
        for name in wanted:
            spec = PLATFORM_SPECS[name]
            pid = spec["fallback_id"]
            if pid is None:
                for code in spec["codes"]:
                    if code in by_code:
                        pid = by_code[code]
                        break
            if pid is None:
                log.warning("[%s] platform tidak ditemukan di tabel platforms, dilewati.", name)
                continue
            result[name] = pid
        return result

    def latest_contents(self, platform_id, content_limit, url_like=None):
        """N konten terbaru (published_at DESC) untuk satu platform."""
        self.ensure()
        sql = "SELECT id, original_id, clean_url, title FROM contents WHERE platform_id = %s"
        params = [platform_id]
        if url_like:
            sql += " AND (" + " OR ".join(["clean_url LIKE %s"] * len(url_like)) + ")"
            params += url_like
        sql += " ORDER BY published_at DESC, id DESC LIMIT %s"
        params.append(content_limit)
        self.cursor.execute(sql, tuple(params))
        return self.cursor.fetchall()

    def count_existing(self, content_id):
        self.ensure()
        self.cursor.execute("SELECT COUNT(*) AS n FROM comments WHERE content_id = %s", (content_id,))
        row = self.cursor.fetchone()
        return int(row["n"]) if row else 0

    def comment_exists(self, content_id, author, text):
        """Cek duplikat: content_id -> author -> isi komentar (case-sensitive)."""
        self.cursor.execute(
            """
            SELECT 1 FROM comments
            WHERE content_id = %s
              AND author_name = %s
              AND BINARY comment_text = BINARY %s
            LIMIT 1
            """,
            (content_id, author, text),
        )
        return self.cursor.fetchone() is not None

    def insert_comment(self, content_id, author, text):
        self.cursor.execute(
            "INSERT INTO comments (content_id, comment_text, author_name) VALUES (%s, %s, %s)",
            (content_id, text, author),
        )
        comment_id = self.cursor.lastrowid
        self.cursor.execute(
            """
            INSERT IGNORE INTO sentiment_results
            (target_type, target_id, execution_source, status)
            VALUES ('comment', %s, 'ml_pipeline', 'pending')
            """,
            (comment_id,),
        )
        self.conn.commit()


def store_comments(db, content_id, comments, limit):
    """
    Simpan komentar dari iterable `comments` (yield (author, text)) sampai `limit` komentar
    BARU tersimpan. Duplikat dilewati dan tidak dihitung. Return (inserted, skipped).
    """
    inserted = skipped = 0
    try:
        for author, text in comments:
            if inserted >= limit:
                break
            text = (text or "").strip()
            author = (author or "Unknown")[:150]
            if not text:
                continue

            db.ensure()  # fetch bisa lama (scroll browser), jadi cek koneksi dulu
            if db.comment_exists(content_id, author, text):
                skipped += 1
                continue
            db.insert_comment(content_id, author, text)
            inserted += 1
    finally:
        close = getattr(comments, "close", None)
        if close:
            try:
                close()  # lepaskan listener Playwright dsb.
            except Exception:
                pass
    return inserted, skipped


def new_stats():
    return {"contents": 0, "new": 0, "dup": 0, "errors": 0, "note": ""}


# =====================================================================
# YOUTUBE
# =====================================================================
def fetch_youtube_comments(yt_url, max_fetch):
    from yt_dlp import YoutubeDL

    if INCLUDE_REPLIES:
        max_comments = [str(max_fetch)]
    else:
        # max-comments, max-parents, max-replies, max-replies-per-thread
        max_comments = [str(max_fetch), str(max_fetch), "0", "0"]

    opts = {
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "getcomments": True,
        "extractor_args": {"youtube": {"max_comments": max_comments, "comment_sort": ["new"]}},
    }
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(yt_url, download=False)
    return (info or {}).get("comments") or []


def crawl_youtube(db, contents, limit, **_):
    stats = new_stats()
    total = len(contents)
    for idx, row in enumerate(contents, 1):
        c_id, orig_id, clean_url = row["id"], row["original_id"], row["clean_url"]
        yt_url = f"https://www.youtube.com/watch?v={orig_id}" if orig_id and len(orig_id) == 11 else clean_url
        log.info("[youtube] (%d/%d) Content ID %s -> %s", idx, total, c_id, yt_url)
        stats["contents"] += 1
        try:
            # Ambil sebanyak (yang sudah ada + batas baru) supaya setelah duplikat dilewati
            # masih tersisa cukup komentar baru.
            existing = db.count_existing(c_id)
            max_fetch = min(existing + limit, YT_MAX_FETCH_CAP)
            comments = fetch_youtube_comments(yt_url, max_fetch)
            log.info("   %d komentar diambil dari YouTube (target maks %d).", len(comments), max_fetch)

            if not comments:
                log.warning("   Tidak ada komentar (dimatikan / privat / diblokir).")
                continue

            pairs = ((c.get("author"), c.get("text")) for c in comments)
            ins, skp = store_comments(db, c_id, pairs, limit)
            stats["new"] += ins
            stats["dup"] += skp
            log.info("   [OK] +%d komentar baru, %d duplikat dilewati.", ins, skp)
        except Exception as e:
            db.rollback()
            stats["errors"] += 1
            log.error("   Error content ID %s: %s", c_id, e)
    return stats


# =====================================================================
# X (TWITTER) - replies
# =====================================================================
def parse_x_url(x_url):
    m = re.search(r"(?:x|twitter)\.com/([^/]+)/status/(\d+)", x_url or "")
    return (m.group(1), m.group(2)) if m else (None, None)


def collect_x_replies(page, tweet_id):
    """Scroll halaman tweet & yield (author, text) tiap reply. Dedupe lewat tweet id."""
    seen_ids = set()
    idle = 0
    main_id = int(tweet_id)

    for _ in range(X_MAX_SCROLLS):
        for label in ("Show additional replies", "Tampilkan balasan tambahan",
                      "Show more replies", "Tampilkan balasan lainnya"):
            try:
                btn = page.get_by_role("button", name=label)
                if btn.count() > 0:
                    btn.first.click(timeout=1500)
                    time.sleep(1)
            except Exception:
                pass

        articles = page.eval_on_selector_all('article[data-testid="tweet"]', JS_EXTRACT)

        new_in_step = 0
        for art in articles:
            a_id = art.get("id")
            if not a_id or a_id in seen_ids:
                continue
            seen_ids.add(a_id)
            # Reply selalu punya snowflake id > tweet aslinya (membuang tweet utama / parent)
            if int(a_id) <= main_id:
                continue
            text = (art.get("text") or "").strip()
            if not text:
                continue
            new_in_step += 1
            yield f"@{art.get('handle') or 'unknown'}", text

        if new_in_step == 0:
            idle += 1
            if idle >= X_MAX_IDLE_SCROLLS:
                log.info("   Tidak ada reply baru lagi, berhenti scroll.")
                return
        else:
            idle = 0

        page.mouse.wheel(0, random.randint(1800, 2600))
        time.sleep(random.uniform(1.5, 2.8))


def crawl_x(db, contents, limit, browser=None, **_):
    stats = new_stats()
    if not X_SESSION_FILE.exists():
        stats["note"] = "tidak ada sesi (jalankan: --login x)"
        log.warning("[x] Sesi %s tidak ada. Jalankan: python crawl_comments.py --login x", X_SESSION_FILE.name)
        return stats

    context = browser.new_context(
        storage_state=str(X_SESSION_FILE), viewport={"width": 1100, "height": 900}, locale="id-ID"
    )
    page = context.new_page()
    total = len(contents)
    try:
        for idx, row in enumerate(contents, 1):
            c_id, clean_url = row["id"], row["clean_url"]
            _, tweet_id = parse_x_url(clean_url)
            tweet_id = tweet_id or row["original_id"]
            if not tweet_id:
                continue

            log.info("[x] (%d/%d) Content ID %s -> %s", idx, total, c_id, clean_url)
            stats["contents"] += 1
            try:
                page.goto(clean_url, wait_until="domcontentloaded", timeout=45000)
                time.sleep(2)
                if "login" in page.url or "/i/flow" in page.url:
                    stats["note"] = "sesi tidak valid (login ulang: --login x)"
                    log.error("   Sesi X tidak valid / kedaluwarsa. Platform X dihentikan.")
                    break
                try:
                    page.wait_for_selector('article[data-testid="tweet"]', timeout=20000)
                except Exception:
                    log.warning("   Tweet tidak termuat (dihapus / dibatasi / rate limit?). Dilewati.")
                    continue

                ins, skp = store_comments(db, c_id, collect_x_replies(page, tweet_id), limit)
                stats["new"] += ins
                stats["dup"] += skp
                log.info("   [OK] +%d reply baru, %d duplikat dilewati.", ins, skp)
            except Exception as e:
                db.rollback()
                stats["errors"] += 1
                log.error("   Error content ID %s: %s", c_id, e)

            if idx < total:
                time.sleep(random.uniform(3, 6))
    finally:
        context.close()
    return stats


# =====================================================================
# INSTAGRAM
# =====================================================================
def parse_json_text(text):
    """Parse JSON respons Instagram (kadang berawalan 'for (;;);' atau beberapa JSON per baris)."""
    t = (text or "").strip()
    if t.startswith("for (;;);"):
        t = t[len("for (;;);"):]
    try:
        return json.loads(t)
    except Exception:
        pass
    out = []
    for line in t.splitlines():
        line = line.strip()
        if line.startswith("{") or line.startswith("["):
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out or None


def is_comment_obj(node):
    user = node.get("user")
    return (
        isinstance(node.get("text"), str)
        and isinstance(user, dict)
        and bool(user.get("username"))
        and any(k in node for k in COMMENT_MARKERS)
    )


def iter_comment_objects(node, depth=0):
    """Telusuri JSON rekursif & yield komentar top-level (balasan dilewati)."""
    if depth > 40:
        return
    if isinstance(node, dict):
        if is_comment_obj(node) and not node.get("parent_comment_id"):
            yield node
        for v in node.values():
            if isinstance(v, (dict, list)):
                yield from iter_comment_objects(v, depth + 1)
    elif isinstance(node, list):
        for v in node:
            if isinstance(v, (dict, list)):
                yield from iter_comment_objects(v, depth + 1)


def ig_load_more(page):
    """Klik tombol 'muat komentar lainnya' kalau ada; kalau tidak, scroll panel komentar."""
    try:
        btn = page.locator(
            'svg[aria-label="Load more comments"], '
            'svg[aria-label="Muat komentar lainnya"], '
            'svg[aria-label="Muat lebih banyak komentar"]'
        )
        if btn.count() > 0:
            btn.first.click(timeout=1500)
            return
    except Exception:
        pass
    try:
        page.evaluate(JS_SCROLL_COMMENTS)
    except Exception:
        pass


def collect_ig_comments(page, clean_url):
    """
    Buka post & tangkap komentar dari respons jaringan IG + JSON tertanam. Yield (author, text).
    Catatan: event respons Playwright (sync API) hanya diproses saat memanggil method
    Playwright, jadi jeda memakai page.wait_for_timeout (bukan time.sleep).
    """
    state = {"found": {}, "order": [], "rate_limited": False, "endpoints": []}

    def add_from(data):
        for c in iter_comment_objects(data):
            text = (c.get("text") or "").strip()
            handle = (c.get("user") or {}).get("username") or "unknown"
            if not text:
                continue
            key = str(c.get("pk") or c.get("id") or f"{handle}|{text}")
            if key in state["found"]:
                continue
            state["found"][key] = (f"@{handle}", text)
            state["order"].append(key)

    def on_response(resp):
        try:
            if resp.status == 429:
                state["rate_limited"] = True
                return
            url = resp.url
            if "instagram.com" not in url or not any(k in url for k in ("/graphql", "/api/v1/", "/api/graphql")):
                return
            body = resp.text()
        except Exception:
            return
        data = parse_json_text(body)
        if data is None:
            return
        try:
            name = resp.request.headers.get("x-fb-friendly-name") or url.split("instagram.com")[-1].split("?")[0]
        except Exception:
            name = "?"
        if name[:80] not in state["endpoints"] and len(state["endpoints"]) < 12:
            state["endpoints"].append(name[:80])
        add_from(data)

    page.on("response", on_response)
    try:
        page.goto(clean_url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(3000)

        if "accounts/login" in page.url or "/challenge" in page.url:
            raise SessionError("diarahkan ke halaman login")

        # Komentar awal sering sudah tertanam di <script type=application/json>
        try:
            texts = page.eval_on_selector_all('script[type="application/json"]', "els => els.map(e => e.textContent)")
        except Exception:
            texts = []
        for t in texts:
            if t and any(m in t for m in COMMENT_MARKERS):
                data = parse_json_text(t)
                if data is not None:
                    add_from(data)

        yielded = 0
        idle = 0
        for _ in range(IG_MAX_SCROLLS):
            while yielded < len(state["order"]):
                yield state["found"][state["order"][yielded]]
                yielded += 1

            if state["rate_limited"]:
                raise RateLimitError("HTTP 429")

            before = len(state["order"])
            ig_load_more(page)
            page.wait_for_timeout(random.randint(1800, 3200))

            if len(state["order"]) == before:
                idle += 1
                if idle >= IG_MAX_IDLE_SCROLLS:
                    break
            else:
                idle = 0

        while yielded < len(state["order"]):
            yield state["found"][state["order"][yielded]]
            yielded += 1

        if yielded == 0:
            log.info("   Tidak ada komentar tertangkap (tanpa komentar / dinonaktifkan / format respons berubah).")
            if state["endpoints"]:
                log.info("   Endpoint JSON yang terlihat: %s", " | ".join(state["endpoints"]))
    finally:
        try:
            page.remove_listener("response", on_response)
        except Exception:
            pass


def crawl_instagram(db, contents, limit, browser=None, **_):
    stats = new_stats()
    if not IG_SESSION_FILE.exists():
        stats["note"] = "tidak ada sesi (jalankan: --login instagram)"
        log.warning("[instagram] Sesi %s tidak ada. Jalankan: python crawl_comments.py --login instagram",
                    IG_SESSION_FILE.name)
        return stats

    context = browser.new_context(
        storage_state=str(IG_SESSION_FILE), viewport={"width": 1280, "height": 900}, locale="id-ID"
    )
    page = context.new_page()
    total = len(contents)
    try:
        for idx, row in enumerate(contents, 1):
            c_id, clean_url = row["id"], row["clean_url"]
            log.info("[instagram] (%d/%d) Content ID %s -> %s", idx, total, c_id, clean_url)
            stats["contents"] += 1
            try:
                ins, skp = store_comments(db, c_id, collect_ig_comments(page, clean_url), limit)
                stats["new"] += ins
                stats["dup"] += skp
                log.info("   [OK] +%d komentar baru, %d duplikat dilewati.", ins, skp)
            except SessionError as e:
                db.rollback()
                stats["note"] = "sesi ditolak (login ulang: --login instagram)"
                log.error("   Sesi Instagram ditolak (%s). Platform Instagram dihentikan.", e)
                break
            except RateLimitError:
                db.rollback()
                stats["note"] = f"rate limit (429) di konten ke-{idx}"
                log.error("   Kena rate limit Instagram (429). Platform Instagram dihentikan; "
                          "coba lagi beberapa jam kemudian.")
                break
            except Exception as e:
                db.rollback()
                stats["errors"] += 1
                log.error("   Error content ID %s: %s", c_id, e)

            if idx < total:
                time.sleep(random.uniform(5, 10))
    finally:
        context.close()
    return stats


# =====================================================================
# BERITA (detik, kompas)
# =====================================================================
def clean_text(value):
    from bs4 import BeautifulSoup

    if not isinstance(value, str):
        return ""
    if "<" in value and ">" in value:
        value = BeautifulSoup(value, "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", value).strip()


def _author_of(d):
    for k in AUTHOR_KEYS:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, dict):
            for kk in ("name", "username", "display_name", "nama", "fullname"):
                vv = v.get(kk)
                if isinstance(vv, str) and vv.strip():
                    return vv.strip()
    return None


def _text_of(d):
    for k in TEXT_KEYS:
        v = d.get(k)
        if isinstance(v, str):
            t = clean_text(v)
            if 2 <= len(t) <= 5000:
                return t
    return None


def walk_json_for_comments(node, out):
    """Telusuri JSON apa pun & kumpulkan objek yang tampak seperti komentar (teks + penulis)."""
    if isinstance(node, dict):
        author = _author_of(node)
        text = _text_of(node)
        if author and text:
            out.append({"author": author, "text": text})
        for v in node.values():
            if isinstance(v, (dict, list)):
                walk_json_for_comments(v, out)
    elif isinstance(node, list):
        for item in node:
            walk_json_for_comments(item, out)


def dedupe_comments(items):
    seen = set()
    result = []
    for it in items:
        key = (it["author"], it["text"])
        if key in seen:
            continue
        seen.add(key)
        result.append(it)
    return result


def extract_comments_from_html(html, portal):
    """Strategi 1: JSON tertanam di <script>. Strategi 2: selector DOM."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    found = []

    for sc in soup.find_all("script"):
        raw = (sc.string or sc.get_text() or "").strip()
        if not raw or raw[0] not in "{[":
            continue
        if "comment" not in raw.lower() and "komentar" not in raw.lower():
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        tmp = []
        walk_json_for_comments(data, tmp)
        found.extend(c for c in tmp if len(c["text"]) < 2000)  # abaikan objek artikel itu sendiri

    for item_sel, author_sel, text_sel in DOM_SELECTORS.get(portal, []):
        for el in soup.select(item_sel):
            a = el.select_one(author_sel)
            t = el.select_one(text_sel)
            text = clean_text(t.get_text(" ", strip=True)) if t else ""
            author = clean_text(a.get_text(" ", strip=True)) if a else "Unknown"
            if text:
                found.append({"author": author or "Unknown", "text": text})

    return dedupe_comments(found)


def extract_detik_id(url):
    m = re.search(r"/d-(\d+)", url or "")
    return m.group(1) if m else ""


def extract_detik_channel_id(html):
    """channel_id diambil dari meta tag halaman artikel (dtk:kanalid)."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html or "", "html.parser")
    for name in ("dtk:kanalid", "kanalid"):
        tag = soup.find("meta", attrs={"name": name}) or soup.find("meta", attrs={"property": name})
        if tag and tag.get("content"):
            m = re.search(r"\d+", tag["content"].split("-")[-1])
            if m:
                return m.group(0)
    return ""


def parse_detik_comments(data):
    """Parser khusus respons apicomment.detik.com (kunci 'list_comment')."""
    out = []

    def find_lists(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if k in ("list_comment", "list_comments", "comments") and isinstance(v, list):
                    yield v
                elif isinstance(v, (dict, list)):
                    yield from find_lists(v)
        elif isinstance(node, list):
            for i in node:
                yield from find_lists(i)

    for lst in find_lists(data):
        for item in lst:
            if not isinstance(item, dict):
                continue
            name = item.get("name") or _author_of(item)

            text = None
            for k in ("comment", "comment_text", "text", "content", "message", "body", "isi"):
                v = item.get(k)
                if isinstance(v, str) and v.strip():
                    text = clean_text(v)
                    break
            if not text:
                # Cadangan: string terpanjang yang bukan nama/URL/tanggal
                cands = []
                for k, v in item.items():
                    if k in ("name", "avatar") or not isinstance(v, str):
                        continue
                    if v.startswith("http") or re.match(r"^[\d\-: /T.+Z]+$", v.strip()):
                        continue
                    cands.append(clean_text(v))
                text = max(cands, key=len) if cands else None

            if name and text:
                out.append({"author": str(name).strip(), "text": text})

            for rk in ("reply", "replies", "list_reply", "child", "children"):
                rv = item.get(rk)
                if isinstance(rv, (dict, list)):
                    tmp = []
                    walk_json_for_comments(rv, tmp)
                    out.extend(tmp)

    return dedupe_comments(out)


def parse_comments_by_author_key(data, author_keys):
    """Cari objek yang punya salah satu kunci penulis (mis. 'user_fullname' di kompas).
    Return (list komentar, nilai flag 'last')."""
    out = []
    skip_words = ("user", "avatar", "url", "image", "photo", "date", "time", "created", "login", "id")
    text_keys = ("comment", "comment_text", "comment_content", "content", "text", "message", "body", "isi")

    def pick_text(node):
        for k in text_keys:
            v = node.get(k)
            if isinstance(v, str) and v.strip():
                return clean_text(v)
        cands = []
        for k, v in node.items():
            if not isinstance(v, str):
                continue
            if any(w in k.lower() for w in skip_words):
                continue
            if v.startswith("http") or re.match(r"^[\d\-: /T.+Z]+$", v.strip()):
                continue
            cands.append(clean_text(v))
        return max(cands, key=len) if cands else None

    def visit(node):
        if isinstance(node, dict):
            author = None
            for k in author_keys:
                v = node.get(k)
                if isinstance(v, str) and v.strip():
                    author = v.strip()
                    break
            if author:
                text = pick_text(node)
                if text:
                    out.append({"author": author, "text": text})
            for v in node.values():
                if isinstance(v, (dict, list)):
                    visit(v)
        elif isinstance(node, list):
            for i in node:
                visit(i)

    visit(data)
    last_flag = None
    if isinstance(data, dict):
        last_flag = data.get("last")
        if last_flag is None and isinstance(data.get("data"), dict):
            last_flag = data["data"].get("last")
    return dedupe_comments(out), last_flag


def fetch_news_comments(session, url, portal):
    """Ambil komentar artikel detik/kompas. Return list[{author, text}]."""
    results = []
    article_id = extract_detik_id(url) if portal == "detik" else ""
    channel_id = ""

    # A. Endpoint komentar
    for tpl in COMMENT_ENDPOINTS.get(portal, []):
        if "{channel_id}" in tpl and not channel_id:
            try:
                ra = session.get(url, timeout=15)
                channel_id = extract_detik_channel_id(ra.text) if ra.status_code == 200 else ""
            except Exception as e:
                log.info("   gagal ambil channel_id: %s", e)
            if not channel_id:
                log.info("   channel_id tidak ketemu, endpoint dilewati.")
                continue

        api_url = tpl.format(
            url=url, url_enc=quote(url, safe=""), id=article_id,
            channel_id=channel_id, limit=NEWS_MAX_FETCH_CAP,
        )
        try:
            r = session.get(
                api_url, timeout=15,
                headers={
                    "Referer": url,
                    "Origin": f"{urlparse(url).scheme}://{urlparse(url).netloc}",
                    "Accept": "application/json, text/plain, */*",
                },
            )
            log.info("   endpoint %s: %s", r.status_code, api_url[:100])
            if r.status_code != 200:
                continue
            try:
                data = r.json()
            except ValueError:
                data = None

            tmp = []
            if data is not None:
                if portal == "detik":
                    tmp = parse_detik_comments(data)
                elif portal == "kompas":
                    tmp, last_flag = parse_comments_by_author_key(data, ("user_fullname", "user_name"))
                    if last_flag is False:
                        log.info("   PERINGATAN: last=false, masih ada komentar lain yang belum terambil.")
                if not tmp:
                    walk_json_for_comments(data, tmp)
            else:
                tmp = extract_comments_from_html(r.text, portal)
            results.extend(tmp)
        except Exception as e:
            log.info("   endpoint error: %s", e)

    # B. Fallback: HTML artikel
    if not results:
        try:
            r = session.get(url, timeout=15)
            if r.status_code == 200:
                results.extend(extract_comments_from_html(r.text, portal))
            else:
                log.info("   halaman artikel %s", r.status_code)
        except Exception as e:
            log.info("   halaman artikel error: %s", e)

    return dedupe_comments(results)[:NEWS_MAX_FETCH_CAP]


def make_news_crawler(portal):
    def crawl(db, contents, limit, **_):
        import requests

        stats = new_stats()
        session = requests.Session()
        session.headers.update(NEWS_HEADERS)
        total = len(contents)
        for idx, row in enumerate(contents, 1):
            c_id, url = row["id"], row["clean_url"]
            log.info("[%s] (%d/%d) Content ID %s -> %s", portal, idx, total, c_id, url)
            stats["contents"] += 1
            try:
                comments = fetch_news_comments(session, url, portal)
                if not comments:
                    log.info("   Tidak ada komentar (kosong / dimatikan / endpoint berubah).")
                else:
                    log.info("   %d komentar diambil dari portal.", len(comments))
                    pairs = ((c["author"], c["text"]) for c in comments)
                    ins, skp = store_comments(db, c_id, pairs, limit)
                    stats["new"] += ins
                    stats["dup"] += skp
                    log.info("   [OK] +%d komentar baru, %d duplikat dilewati.", ins, skp)
            except Exception as e:
                db.rollback()
                stats["errors"] += 1
                log.error("   Error content ID %s: %s", c_id, e)
            time.sleep(NEWS_REQUEST_DELAY)
        return stats

    return crawl


CRAWLERS = {
    "youtube": crawl_youtube,
    "x": crawl_x,
    "instagram": crawl_instagram,
    "detik": make_news_crawler("detik"),
    "kompas": make_news_crawler("kompas"),
}
BROWSER_PLATFORMS = {"x", "instagram"}


# =====================================================================
# LOGIN MANUAL (membuat file sesi untuk X / Instagram)
# =====================================================================
def manual_login(platform):
    from playwright.sync_api import sync_playwright

    cfg = {
        "x": ("https://x.com/login", "https://x.com", "auth_token", X_SESSION_FILE),
        "instagram": ("https://www.instagram.com/accounts/login/", "https://www.instagram.com",
                      "sessionid", IG_SESSION_FILE),
    }[platform]
    login_url, cookie_url, cookie_name, session_file = cfg

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(viewport={"width": 1100, "height": 800}, locale="id-ID")
        page = context.new_page()
        page.goto(login_url)
        log.info("Silakan login di jendela browser (maks %d menit)...", LOGIN_TIMEOUT // 60)

        deadline = time.time() + LOGIN_TIMEOUT
        logged_in = False
        while time.time() < deadline:
            try:
                cookies = context.cookies(cookie_url)
            except Exception:
                break  # browser ditutup user
            if any(c["name"] == cookie_name and c["value"] for c in cookies):
                logged_in = True
                break
            time.sleep(2)

        if logged_in:
            time.sleep(3)
            context.storage_state(path=str(session_file))
            log.info("[OK] Sesi login disimpan ke %s", session_file.name)
        else:
            log.error("Login tidak terdeteksi / waktu habis. Sesi tidak disimpan.")
        browser.close()
    return logged_in


# =====================================================================
# MAIN
# =====================================================================
def parse_args():
    ap = argparse.ArgumentParser(
        description="Crawl komentar dari konten terbaru tiap platform di db_sentimenanalisis_v2.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Platform: " + ", ".join(ALL_PLATFORMS) + "  (antaranews dilewati)",
    )
    ap.add_argument("-l", "--limit", type=int, default=DEFAULT_LIMIT,
                    help=f"maks komentar BARU yang disimpan per konten (default {DEFAULT_LIMIT})")
    ap.add_argument("-cl", "--contentlimit", type=int, default=DEFAULT_CONTENT_LIMIT,
                    help=f"jumlah konten terbaru (published_at) per platform (default {DEFAULT_CONTENT_LIMIT})")
    ap.add_argument("-p", "--platforms", default="all",
                    help="daftar platform dipisah koma, mis. youtube,x (default: all)")
    ap.add_argument("--headed", action="store_true",
                    help="tampilkan browser untuk X/Instagram (default headless)")
    ap.add_argument("--login", choices=("x", "instagram"),
                    help="buka browser untuk login manual & simpan sesi, lalu keluar")
    args = ap.parse_args()

    if args.limit <= 0 or args.contentlimit <= 0:
        ap.error("-l dan -cl harus lebih besar dari 0")

    if args.platforms.strip().lower() == "all":
        args.platform_list = list(ALL_PLATFORMS)
    else:
        args.platform_list = [p.strip().lower() for p in args.platforms.split(",") if p.strip()]
        bad = [p for p in args.platform_list if p not in ALL_PLATFORMS]
        if bad:
            ap.error(f"platform tidak dikenal: {', '.join(bad)} (pilihan: {', '.join(ALL_PLATFORMS)})")
        # urutan tetap & tanpa duplikat
        args.platform_list = [p for p in ALL_PLATFORMS if p in args.platform_list]
    return args


def main():
    args = parse_args()

    if args.login:
        sys.exit(0 if manual_login(args.login) else 1)

    log.info("Mulai crawling | platform: %s | -l %d komentar baru/konten | -cl %d konten terbaru/platform",
             ",".join(args.platform_list), args.limit, args.contentlimit)

    db = DB()
    all_stats = {}
    pw = browser = None
    try:
        platform_ids = db.resolve_platform_ids(args.platform_list)

        if BROWSER_PLATFORMS & set(platform_ids):
            from playwright.sync_api import sync_playwright

            pw = sync_playwright().start()
            browser = pw.chromium.launch(headless=not args.headed)

        for name in args.platform_list:
            if name not in platform_ids:
                continue
            log.info("=" * 60)
            contents = db.latest_contents(platform_ids[name], args.contentlimit, PLATFORM_SPECS[name]["url_like"])
            log.info("[%s] %d konten terbaru dimuat (platform_id=%s).", name, len(contents), platform_ids[name])
            if not contents:
                all_stats[name] = new_stats()
                continue
            try:
                all_stats[name] = CRAWLERS[name](db, contents, args.limit, browser=browser)
            except Exception as e:  # satu platform gagal tidak menghentikan platform lain
                db.rollback()
                log.error("[%s] Platform gagal total: %s", name, e)
                st = new_stats()
                st["errors"] += 1
                st["note"] = f"gagal: {e}"
                all_stats[name] = st
    except KeyboardInterrupt:
        log.warning("Dihentikan oleh user (Ctrl+C).")
    finally:
        if browser:
            try:
                browser.close()
            except Exception:
                pass
        if pw:
            try:
                pw.stop()
            except Exception:
                pass
        db.close()

    # Ringkasan
    print("\n" + "=" * 68)
    print(f"{'PLATFORM':<12}{'KONTEN':>8}{'BARU':>8}{'DUPLIKAT':>10}{'ERROR':>8}  CATATAN")
    print("-" * 68)
    tot = new_stats()
    for name, st in all_stats.items():
        print(f"{name:<12}{st['contents']:>8}{st['new']:>8}{st['dup']:>10}{st['errors']:>8}  {st['note']}")
        for k in ("contents", "new", "dup", "errors"):
            tot[k] += st[k]
    print("-" * 68)
    print(f"{'TOTAL':<12}{tot['contents']:>8}{tot['new']:>8}{tot['dup']:>10}{tot['errors']:>8}")
    print("=" * 68)


if __name__ == "__main__":
    main()