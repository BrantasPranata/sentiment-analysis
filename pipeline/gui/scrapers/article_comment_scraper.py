import json
import logging
import re
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText
from urllib.parse import quote, urlparse

import requests
from bs4 import BeautifulSoup

# Fix Path import config/Config (sama seperti scraper lainnya)
sys.path.append(str(Path(__file__).resolve().parents[2]))
from config.config import get_db_connection

# Logging Setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "id-ID,id;q=0.9,en;q=0.8",
}

PORTAL_CODES = ("detik", "kompas", "antaranews")

# Batas atas komentar yang diambil per artikel dalam satu kali crawl.
MAX_FETCH_CAP = 500

# Jeda antar request artikel (detik) supaya tidak diblokir.
REQUEST_DELAY = 1.5

# Folder untuk menyimpan respons mentah saat tombol "Tes Fetch" dipakai.
DEBUG_DIR = Path(__file__).resolve().parent / "debug_comments"

# =====================================================================
# KONFIGURASI ENDPOINT KOMENTAR PER PORTAL
# ---------------------------------------------------------------------
# Placeholder yang tersedia:
#   {url}      -> URL artikel (clean_url)
#   {url_enc}  -> URL artikel ter-encode
#   {id}       -> ID artikel (detik: angka setelah "/d-")
#   {limit}    -> jumlah komentar maksimal
#
# CATATAN: endpoint di bawah BELUM TERVERIFIKASI (tidak bisa dites dari sini).
# Cara cek: buka artikel di browser -> F12 -> tab Network -> filter Fetch/XHR
# -> muat ulang / scroll ke kolom komentar -> cari request yang isinya
# komentar -> Copy URL -> tempel ke daftar di bawah, ganti bagian URL artikel/ID
# dengan placeholder.
# =====================================================================
COMMENT_ENDPOINTS = {
    "detik": [
        # Terverifikasi dari DevTools (sneak-peek = pratinjau komentar; bisa saja hanya
        # sebagian komentar. Jika ada endpoint daftar lengkap, tambahkan di bawahnya).
        "https://apicomment.detik.com/detail-article/sneak-peek/?article_id={id}&channel_id={channel_id}&site=dtk",
    ],
    "kompas": [
        # Terverifikasi dari DevTools. limit=100 dipakai supaya tidak ditolak server;
        # jika respons memuat "last": false berarti masih ada komentar lagi (paging belum diimplementasi).
        "https://apiscomment.kompas.com/list?urlpage={url_enc}&json&limit=100&type=article",
    ],
    "antaranews": [
        # contoh: "https://<host-komentar-antara>/...?url={url_enc}",
    ],
}

# Selector DOM cadangan (jika komentar ikut ter-render di HTML).
# Tiap entri: (selector item, selector penulis, selector isi)
DOM_SELECTORS = {
    "detik": [
        ("div.komentar-item, li.komentar-item, div.comment-item", ".komentar-nama, .name, .comment-name", ".komentar-isi, .text, .comment-text"),
    ],
    "kompas": [
        ("div.komentar__item, li.komentar__item, div.comment-item", ".komentar__name, .name", ".komentar__text, .text"),
    ],
    "antaranews": [
        ("div.comment-item, li.comment-item, div.media-comment", ".comment-author, .name, .media-heading", ".comment-text, .text, .media-body p"),
    ],
}

TEXT_KEYS = ("comment_text", "comment", "komentar", "text", "content", "message", "body", "isi")
AUTHOR_KEYS = ("author_name", "author", "user_name", "username", "nama", "name", "display_name", "fullname", "user")


# ---------------------------------------------------------------------
# Helper parsing
# ---------------------------------------------------------------------
def clean_text(value):
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
    """Telusuri JSON apa pun dan kumpulkan objek yang tampak seperti komentar
    (punya teks + penulis). Balasan (nested) ikut terambil."""
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
    soup = BeautifulSoup(html, "html.parser")
    found = []
    notes = []

    # 1. JSON di <script> (ld+json, __NEXT_DATA__, dll)
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
        # Abaikan objek artikel itu sendiri (biasanya author + articleBody)
        tmp = [c for c in tmp if len(c["text"]) < 2000]
        if tmp:
            found.extend(tmp)
    if found:
        notes.append(f"script-json: {len(found)}")

    # 2. DOM
    dom_found = []
    for item_sel, author_sel, text_sel in DOM_SELECTORS.get(portal, []):
        for el in soup.select(item_sel):
            a = el.select_one(author_sel)
            t = el.select_one(text_sel)
            text = clean_text(t.get_text(" ", strip=True)) if t else ""
            author = clean_text(a.get_text(" ", strip=True)) if a else "Unknown"
            if text:
                dom_found.append({"author": author or "Unknown", "text": text})
    if dom_found:
        notes.append(f"dom: {len(dom_found)}")
        found.extend(dom_found)

    return dedupe_comments(found), notes


def extract_detik_id(url):
    m = re.search(r"/d-(\d+)", url or "")
    return m.group(1) if m else ""


def extract_detik_channel_id(html):
    """channel_id diambil dari meta tag halaman artikel (dtk:kanalid)."""
    soup = BeautifulSoup(html or "", "html.parser")
    for name in ("dtk:kanalid", "kanalid"):
        tag = soup.find("meta", attrs={"name": name}) or soup.find("meta", attrs={"property": name})
        if tag and tag.get("content"):
            m = re.search(r"\d+", tag["content"].split("-")[-1])
            if m:
                return m.group(0)
    return ""


def parse_detik_comments(data):
    """Parser khusus respons apicomment.detik.com (kunci 'list_comment').
    Mengembalikan (list komentar, daftar kunci item pertama untuk debug)."""
    out = []
    first_keys = []

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
            if not first_keys:
                first_keys = list(item.keys())

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

            # Balasan (jika ada)
            for rk in ("reply", "replies", "list_reply", "child", "children"):
                rv = item.get(rk)
                if isinstance(rv, (dict, list)):
                    tmp = []
                    walk_json_for_comments(rv, tmp)
                    out.extend(tmp)

    return dedupe_comments(out), first_keys


def parse_comments_by_author_key(data, author_keys):
    """Cari semua objek yang punya salah satu kunci penulis (mis. 'user_fullname' di kompas),
    lalu ambil teks komentarnya. Mengembalikan (list komentar, kunci item pertama, nilai 'last')."""
    out = []
    first_keys = []
    skip_words = ("user", "avatar", "url", "image", "photo", "date", "time", "created", "login", "id")
    text_keys = ("comment", "comment_text", "comment_content", "content", "text", "message", "body", "isi")
    last_flag = None

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
        nonlocal first_keys
        if isinstance(node, dict):
            author = None
            for k in author_keys:
                v = node.get(k)
                if isinstance(v, str) and v.strip():
                    author = v.strip()
                    break
            if author:
                if not first_keys:
                    first_keys = list(node.keys())
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
    if isinstance(data, dict):
        last_flag = data.get("last")
        if last_flag is None and isinstance(data.get("data"), dict):
            last_flag = data["data"].get("last")
    return dedupe_comments(out), first_keys, last_flag


def detect_portal(code, url):
    if code in PORTAL_CODES:
        return code
    u = (url or "").lower()
    for p, dom in (("detik", "detik.com"), ("kompas", "kompas.com"), ("antaranews", "antaranews.com")):
        if dom in u:
            return p
    return None


# ---------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------
class NewsCommentsScraperGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("News Comments Scraper (Detik / Kompas / Antara) - db_sentimenanalisis_v2")
        self.root.geometry("900x700")

        self.session = requests.Session()
        self.session.headers.update(HEADERS)

        self.contents_map = {}  # display_str -> (content_id, clean_url, portal, title)

        self.create_widgets()
        self.load_news_contents_from_db()

    # ------------------------- UI -------------------------
    def create_widgets(self):
        frame_target = ttk.LabelFrame(self.root, text=" Target Artikel Berita dari Database ")
        frame_target.pack(fill="x", padx=10, pady=5)
        frame_target.columnconfigure(1, weight=1)

        ttk.Label(frame_target, text="Portal:").grid(row=0, column=0, padx=5, pady=5, sticky="w")
        self.cb_portal = ttk.Combobox(
            frame_target, state="readonly", width=20,
            values=["Semua", "detik", "kompas", "antaranews"],
        )
        self.cb_portal.current(0)
        self.cb_portal.grid(row=0, column=1, padx=5, pady=5, sticky="w")
        self.cb_portal.bind("<<ComboboxSelected>>", lambda e: self.load_news_contents_from_db())

        ttk.Label(frame_target, text="Pilih Artikel:").grid(row=1, column=0, padx=5, pady=5, sticky="w")
        self.cb_article = ttk.Combobox(frame_target, state="readonly", width=80)
        self.cb_article.grid(row=1, column=1, padx=5, pady=5, sticky="ew")

        btn_refresh = ttk.Button(frame_target, text="🔄 Refresh DB", command=self.load_news_contents_from_db)
        btn_refresh.grid(row=1, column=2, padx=5, pady=5)

        frame_settings = ttk.LabelFrame(self.root, text=" Pengaturan Crawling ")
        frame_settings.pack(fill="x", padx=10, pady=5)

        ttk.Label(frame_settings, text="Batas Komentar Baru per Artikel:").grid(row=0, column=0, padx=5, pady=5, sticky="w")
        self.ent_limit = ttk.Entry(frame_settings, width=15)
        self.ent_limit.insert(0, "50")
        self.ent_limit.grid(row=0, column=1, padx=5, pady=5, sticky="w")

        frame_buttons = ttk.Frame(self.root)
        frame_buttons.pack(fill="x", padx=10, pady=5)

        self.btn_start = ttk.Button(
            frame_buttons, text="▶️ Crawl Artikel Terpilih",
            command=lambda: self.start_crawling_thread(process_all=False),
        )
        self.btn_start.pack(side="left", padx=5)

        self.btn_start_all = ttk.Button(
            frame_buttons, text="⏩ Crawl SEMUA Artikel (sesuai filter portal)",
            command=lambda: self.start_crawling_thread(process_all=True),
        )
        self.btn_start_all.pack(side="left", padx=5)

        self.btn_test = ttk.Button(
            frame_buttons, text="🔍 Tes Fetch (tanpa simpan DB)",
            command=self.start_test_thread,
        )
        self.btn_test.pack(side="left", padx=5)

        frame_log = ttk.LabelFrame(self.root, text=" Log Aktivitas ")
        frame_log.pack(fill="both", expand=True, padx=10, pady=5)

        self.txt_log = ScrolledText(frame_log, state="disabled", height=20)
        self.txt_log.pack(fill="both", expand=True, padx=5, pady=5)

    def log(self, message):
        """Tulis log ke GUI (aman dipanggil dari thread lain)."""
        def _write():
            self.txt_log.config(state="normal")
            self.txt_log.insert(tk.END, message + "\n")
            self.txt_log.see(tk.END)
            self.txt_log.config(state="disabled")
        self.root.after(0, _write)

    def set_buttons(self, state):
        def _set():
            for b in (self.btn_start, self.btn_start_all, self.btn_test):
                b.config(state=state)
        self.root.after(0, _set)

    # ------------------------- DB -------------------------
    def load_news_contents_from_db(self):
        """Memuat daftar artikel (platform detik/kompas/antaranews) dari tabel contents."""
        self.cb_article["values"] = []
        self.cb_article.set("")
        self.contents_map.clear()

        portal_filter = self.cb_portal.get() or "Semua"
        codes = list(PORTAL_CODES) if portal_filter == "Semua" else [portal_filter]

        conn = None
        cursor = None
        try:
            conn = get_db_connection()
            cursor = conn.cursor(dictionary=True)

            placeholders = ", ".join(["%s"] * len(codes))
            cursor.execute(
                f"""
                SELECT c.id, c.clean_url, c.title, p.code
                FROM contents c
                JOIN platforms p ON p.id = c.platform_id
                WHERE p.code IN ({placeholders})
                ORDER BY c.id DESC
                """,
                tuple(codes),
            )
            rows = cursor.fetchall()

            if not rows:
                self.log(f"[!] Tidak ada artikel ({', '.join(codes)}) di tabel contents.")
                return

            combo_values = []
            for row in rows:
                portal = detect_portal(row["code"], row["clean_url"])
                title = row["title"][:60] if row["title"] else row["clean_url"]
                display_str = f"[ID: {row['id']}] [{row['code']}] {title}"
                combo_values.append(display_str)
                self.contents_map[display_str] = (row["id"], row["clean_url"], portal, row["title"])

            self.cb_article["values"] = combo_values
            self.cb_article.current(0)
            self.log(f"[+] Berhasil memuat {len(rows)} artikel dari database.")

        except Exception as e:
            self.log(f"[!] Error koneksi database: {e}")

        finally:
            if cursor:
                cursor.close()
            if conn:
                conn.close()

    def comment_exists(self, cursor, content_id, author, comment_text):
        """Cek duplikat: content_id -> author -> isi komentar (case-sensitive)."""
        cursor.execute(
            """
            SELECT 1 FROM comments
            WHERE content_id = %s
              AND author_name = %s
              AND BINARY comment_text = BINARY %s
            LIMIT 1
            """,
            (content_id, author, comment_text),
        )
        return cursor.fetchone() is not None

    # ------------------------- Fetch -------------------------
    def fetch_comments(self, url, portal, debug=False):
        """Ambil komentar artikel. Mengembalikan (list[{author,text}], list[str] catatan)."""
        notes = []
        results = []
        article_id = extract_detik_id(url) if portal == "detik" else ""

        def save_debug(name, content):
            if not debug:
                return
            DEBUG_DIR.mkdir(exist_ok=True)
            p = DEBUG_DIR / f"{portal}_{name}"
            p.write_text(content if isinstance(content, str) else str(content), encoding="utf-8", errors="ignore")
            notes.append(f"raw disimpan: {p}")

        # --- A. Endpoint komentar (jika dikonfigurasi) ---
        channel_id = ""
        for tpl in COMMENT_ENDPOINTS.get(portal, []):
            # channel_id (detik) diambil dari meta halaman artikel
            if "{channel_id}" in tpl and not channel_id:
                try:
                    ra = self.session.get(url, timeout=15)
                    channel_id = extract_detik_channel_id(ra.text) if ra.status_code == 200 else ""
                    notes.append(f"channel_id = {channel_id or '(tidak ketemu)'}")
                except Exception as e:
                    notes.append(f"gagal ambil channel_id: {e}")
                if not channel_id:
                    continue

            api_url = tpl.format(
                url=url, url_enc=quote(url, safe=""), id=article_id,
                channel_id=channel_id, limit=MAX_FETCH_CAP,
            )
            try:
                r = self.session.get(
                    api_url, timeout=15,
                    headers={
                        "Referer": url,
                        "Origin": f"{urlparse(url).scheme}://{urlparse(url).netloc}",
                        "Accept": "application/json, text/plain, */*",
                    },
                )
                notes.append(f"endpoint {r.status_code}: {api_url[:110]}")
                save_debug(f"endpoint_{abs(hash(tpl))}.txt", r.text)
                if r.status_code != 200:
                    continue
                try:
                    data = r.json()
                except ValueError:
                    data = None
                tmp = []
                if data is not None:
                    if portal == "detik":
                        tmp, keys = parse_detik_comments(data)
                        if keys:
                            notes.append(f"field item komentar: {keys}")
                    elif portal == "kompas":
                        tmp, keys, last_flag = parse_comments_by_author_key(data, ("user_fullname", "user_name"))
                        if keys:
                            notes.append(f"field item komentar: {keys}")
                        if last_flag is False:
                            notes.append("PERINGATAN: last=false, masih ada komentar lain yang belum terambil")
                    if not tmp:
                        walk_json_for_comments(data, tmp)
                else:
                    tmp, _ = extract_comments_from_html(r.text, portal)
                if tmp:
                    results.extend(tmp)
                    notes.append(f"  -> {len(tmp)} komentar")
            except Exception as e:
                notes.append(f"endpoint error: {e}")

        # --- B. HTML artikel (JSON tertanam / DOM) ---
        if not results:
            try:
                r = self.session.get(url, timeout=15)
                notes.append(f"halaman artikel {r.status_code}")
                save_debug("article.html", r.text)
                if r.status_code == 200:
                    tmp, n = extract_comments_from_html(r.text, portal)
                    notes.extend(n)
                    results.extend(tmp)
            except Exception as e:
                notes.append(f"halaman artikel error: {e}")

        return dedupe_comments(results)[:MAX_FETCH_CAP], notes

    # ------------------------- Threads -------------------------
    def start_crawling_thread(self, process_all=False):
        try:
            limit = int(self.ent_limit.get().strip())
        except ValueError:
            messagebox.showerror("Error", "Batas komentar harus berupa angka!")
            return
        if limit <= 0:
            messagebox.showerror("Error", "Batas komentar harus lebih besar dari 0!")
            return

        self.set_buttons("disabled")
        threading.Thread(
            target=self.run_comments_crawling, args=(limit, process_all), daemon=True
        ).start()

    def start_test_thread(self):
        selected = self.cb_article.get()
        if selected not in self.contents_map:
            messagebox.showerror("Error", "Pilih artikel dulu.")
            return
        self.set_buttons("disabled")
        threading.Thread(target=self.run_test_fetch, args=(selected,), daemon=True).start()

    def run_test_fetch(self, selected):
        try:
            c_id, url, portal, title = self.contents_map[selected]
            self.log(f"\n[TEST] [{portal}] {url}")
            if not portal:
                self.log("   [!] Portal tidak dikenali.")
                return
            comments, notes = self.fetch_comments(url, portal, debug=True)
            for n in notes:
                self.log(f"   [i] {n}")
            self.log(f"   [i] Total komentar terdeteksi: {len(comments)}")
            for c in comments[:5]:
                self.log(f"      - {c['author']}: {c['text'][:100]}")
            if not comments:
                self.log(
                    "   [!] 0 komentar. Cek file di folder debug_comments/ atau cari endpoint "
                    "komentar lewat F12 > Network, lalu isi COMMENT_ENDPOINTS."
                )
        except Exception as e:
            self.log(f"[!] Error test: {e}")
        finally:
            self.set_buttons("normal")

    # ------------------------- Crawl utama -------------------------
    def run_comments_crawling(self, limit, process_all):
        conn = None
        cursor = None

        try:
            conn = get_db_connection()
            cursor = conn.cursor(dictionary=True)

            targets = []
            if process_all:
                targets = list(self.contents_map.values())
            else:
                selected_str = self.cb_article.get()
                if selected_str in self.contents_map:
                    targets.append(self.contents_map[selected_str])

            if not targets:
                self.log("[!] Tidak ada target artikel terpilih.")
                return

            total_inserted = 0
            total_skipped = 0

            for c_id, url, portal, title in targets:
                self.log(f"\n[*] Crawling Komentar [Content ID: {c_id}] [{portal}] -> {url}")

                if not portal:
                    self.log("   [!] Portal tidak dikenali, dilewati.")
                    continue

                try:
                    comments, notes = self.fetch_comments(url, portal)
                    for n in notes:
                        self.log(f"   [i] {n}")

                    if not comments:
                        self.log("   [!] Tidak ada komentar yang ditemukan (kosong / dimatikan / endpoint belum diset).")
                        time.sleep(REQUEST_DELAY)
                        continue

                    self.log(f"   [i] {len(comments)} komentar diambil dari portal.")

                    count_for_article = 0
                    skipped_for_article = 0

                    for comment in comments:
                        if count_for_article >= limit:
                            break

                        comment_text = (comment.get("text") or "").strip()
                        author = (comment.get("author") or "Unknown")[:150]

                        if not comment_text:
                            continue

                        # Cek duplikat: kalau sudah ada -> skip
                        if self.comment_exists(cursor, c_id, author, comment_text):
                            skipped_for_article += 1
                            total_skipped += 1
                            continue

                        # Simpan ke tabel `comments`
                        cursor.execute(
                            """
                            INSERT INTO comments (content_id, comment_text, author_name)
                            VALUES (%s, %s, %s)
                            """,
                            (c_id, comment_text, author),
                        )
                        comment_id = cursor.lastrowid
                        count_for_article += 1
                        total_inserted += 1

                        # Antrikan ke sentiment_results (target_type = 'comment')
                        cursor.execute(
                            """
                            INSERT IGNORE INTO sentiment_results
                            (target_type, target_id, execution_source, status)
                            VALUES ('comment', %s, 'ml_pipeline', 'pending')
                            """,
                            (comment_id,),
                        )

                        conn.commit()

                    self.log(
                        f"   [SUCCESS] +{count_for_article} komentar baru, "
                        f"{skipped_for_article} duplikat dilewati."
                    )

                except Exception as c_err:
                    self.log(f"   [!] Error crawling komentar untuk ID {c_id}: {c_err}")

                time.sleep(REQUEST_DELAY)

            self.log("\n" + "=" * 50)
            self.log(
                f"[*] CRAWLING SELESAI! Komentar Baru: {total_inserted} | "
                f"Duplikat Dilewati: {total_skipped}"
            )
            self.log("=" * 50)

        except Exception as err:
            self.log(f"[!] Database Error: {err}")

        finally:
            if cursor:
                cursor.close()
            if conn:
                conn.close()
            self.set_buttons("normal")


if __name__ == "__main__":
    root = tk.Tk()
    app = NewsCommentsScraperGUI(root)
    root.mainloop()