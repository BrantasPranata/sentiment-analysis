import json
import logging
import random
import re
import sys
import threading
import time
import tkinter as tk
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from playwright.sync_api import sync_playwright

# Fix Path import config/Config
sys.path.append(str(Path(__file__).resolve().parents[2]))
from config.config import SESSION_DIR, get_db_connection

# Logging Setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

# File sesi login Instagram (dipakai bersama oleh ig_scraper.py & ig_comment_scraper.py)
SESSION_FILE = SESSION_DIR / "gui" / "ig_session.json"

# Berhenti scroll kalau N kali scroll berturut-turut tidak ada post baru
MAX_IDLE_SCROLLS = 5
# Batas pengaman jumlah scroll per pencarian
MAX_SCROLLS = 60
# Batas waktu menunggu login manual (detik)
LOGIN_TIMEOUT = 300

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


def is_login_page(url: str) -> bool:
    return "accounts/login" in url or "/challenge" in url


# -------------------------------------------------------------------- GUI
class IGScraperApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Scraper Instagram - db_sentimenanalisis_v2")
        self.root.geometry("1180x780")

        self.results_data = []
        self.platform_id = None

        try:
            self.platform_id = get_or_create_instagram_platform_id()
        except Exception as e:
            print(f"[!] Gagal menyiapkan platform Instagram di DB: {e}")

        style = ttk.Style()
        style.theme_use("clam")

        self.create_widgets()
        self.update_session_label()
        if self.platform_id:
            self.log(f"[i] Platform Instagram di DB: platform_id = {self.platform_id}")
        else:
            self.log("[!] Platform Instagram belum siap di DB (cek tabel platforms / koneksi).")

    # ------------------------------------------------------------------ GUI
    def create_widgets(self):
        input_frame = ttk.LabelFrame(self.root, text=" Pengaturan Pencarian Instagram ", padding=10)
        input_frame.pack(fill="x", padx=10, pady=5)

        ttk.Label(input_frame, text="Mode:").grid(row=0, column=0, sticky="w", padx=5)
        self.cb_mode = ttk.Combobox(
            input_frame, state="readonly", width=18, values=["Hashtag", "Akun (username)"]
        )
        self.cb_mode.current(0)
        self.cb_mode.grid(row=0, column=1, padx=5, pady=5, sticky="w")

        ttk.Label(input_frame, text="Kata Kunci / Username:").grid(row=0, column=2, sticky="w", padx=5)
        self.ent_query = ttk.Entry(input_frame, width=30)
        self.ent_query.grid(row=0, column=3, padx=5, pady=5, sticky="w")

        ttk.Label(input_frame, text="Batas Maksimal Post:").grid(row=1, column=0, sticky="w", padx=5)
        self.limit_spinbox = ttk.Spinbox(input_frame, from_=1, to=200, width=6)
        self.limit_spinbox.set(10)
        self.limit_spinbox.grid(row=1, column=1, padx=5, pady=5, sticky="w")

        self.var_headless = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            input_frame,
            text="Headless (browser tidak ditampilkan)",
            variable=self.var_headless,
        ).grid(row=1, column=2, columnspan=2, padx=5, pady=5, sticky="w")

        # Sesi login
        frame_session = ttk.LabelFrame(self.root, text=" Sesi Login Instagram ", padding=5)
        frame_session.pack(fill="x", padx=10, pady=5)

        self.lbl_session = ttk.Label(frame_session, text="")
        self.lbl_session.pack(side="left", padx=5)

        self.btn_login = ttk.Button(
            frame_session, text="🔑 Login Instagram (simpan sesi)", command=self.start_login_thread
        )
        self.btn_login.pack(side="right", padx=5)

        # Buttons
        btn_frame = ttk.Frame(self.root, padding=5)
        btn_frame.pack(fill="x", padx=10)

        self.search_btn = ttk.Button(btn_frame, text="1. Cari Post Instagram", command=self.start_search_thread)
        self.search_btn.pack(side="left", padx=5)

        self.db_btn = ttk.Button(
            btn_frame,
            text="2. Fetch Caption, Gambar & Simpan ke DB",
            command=self.start_save_thread,
            state="disabled",
        )
        self.db_btn.pack(side="left", padx=5)

        self.status_label = ttk.Label(btn_frame, text="Siap", font=("Arial", 9, "italic"))
        self.status_label.pack(side="right", padx=5)

        # Tabel
        table_frame = ttk.Frame(self.root, padding=10)
        table_frame.pack(fill="both", expand=True)

        columns = ("No", "Author", "DatePublished", "Caption", "URL", "DBStatus", "ProcessStatus")
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="browse")

        self.tree.heading("No", text="No")
        self.tree.heading("Author", text="Username Author")
        self.tree.heading("DatePublished", text="Date Published")
        self.tree.heading("Caption", text="Caption")
        self.tree.heading("URL", text="URL Post")
        self.tree.heading("DBStatus", text="Status di DB")
        self.tree.heading("ProcessStatus", text="Status Proses")

        self.tree.column("No", width=35, anchor="center")
        self.tree.column("Author", width=120, anchor="w")
        self.tree.column("DatePublished", width=130, anchor="center")
        self.tree.column("Caption", width=380, anchor="w")
        self.tree.column("URL", width=190, anchor="w")
        self.tree.column("DBStatus", width=95, anchor="center")
        self.tree.column("ProcessStatus", width=110, anchor="center")

        self.tree.bind("<Double-1>", self.on_row_double_click)

        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscroll=scrollbar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        # Log
        log_frame = ttk.LabelFrame(self.root, text=" Live Console / Log ", padding=5)
        log_frame.pack(fill="x", padx=10, pady=5)

        self.log_console = ScrolledText(log_frame, height=8, font=("Consolas", 8), state="disabled")
        self.log_console.pack(fill="both", expand=True)

    def _append_log(self, message):
        self.log_console.config(state="normal")
        self.log_console.insert(tk.END, message + "\n")
        self.log_console.see(tk.END)
        self.log_console.config(state="disabled")

    def log(self, message):
        """Thread-safe: aman dipanggil dari thread worker."""
        self.root.after(0, lambda m=message: self._append_log(m))

    def set_buttons_state(self, state):
        for btn in (self.search_btn, self.btn_login):
            btn.config(state=state)

    def update_session_label(self):
        if SESSION_FILE.exists():
            self.lbl_session.config(text=f"✅ Sesi tersimpan: {SESSION_FILE.name}")
        else:
            self.lbl_session.config(text="⚠️ Belum ada sesi. Klik Login Instagram dulu (wajib).")

    # ------------------------------------------------------------------ DB
    def refresh_db(self, conn, cursor):
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
            self.log("   [i] Koneksi DB putus, menyambung ulang...")
            conn = get_db_connection()

        return conn, conn.cursor(dictionary=True)

    def check_posts_exist_in_db(self, posts):
        try:
            conn = get_db_connection()
            cursor = conn.cursor(dictionary=True)
            cursor.execute("SELECT clean_url FROM contents WHERE platform_id = %s", (self.platform_id,))
            existing = {row["clean_url"] for row in cursor.fetchall()}
            cursor.close()
            conn.close()

            for item in posts:
                item["db_status"] = "Ada di DB" if item["clean_url"] in existing else "Belum Ada di DB"
        except Exception as e:
            self.log(f"[!] Gagal mengecek status DB: {e}")
            for item in posts:
                item["db_status"] = "Belum Cek"
        return posts

    # ---------------------------------------------------------------- Login
    def start_login_thread(self):
        self.set_buttons_state("disabled")
        threading.Thread(target=self.run_login, daemon=True).start()

    def run_login(self):
        """Buka browser, tunggu user login manual, lalu simpan sesi (cookies)."""
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=False)
                context = browser.new_context(viewport={"width": 1100, "height": 800}, locale="id-ID")
                page = context.new_page()
                page.goto("https://www.instagram.com/accounts/login/")

                self.log(f"[*] Silakan login di jendela browser (maks {LOGIN_TIMEOUT // 60} menit)...")

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
                    time.sleep(3)  # beri waktu cookie lain selesai di-set
                    context.storage_state(path=str(SESSION_FILE))
                    self.log(f"[SUCCESS] Sesi login disimpan ke {SESSION_FILE.name}")
                else:
                    self.log("[!] Login tidak terdeteksi / waktu habis. Sesi tidak disimpan.")

                browser.close()

        except Exception as e:
            self.log(f"[!] Error saat login: {e}")

        finally:
            self.root.after(0, self.update_session_label)
            self.root.after(0, lambda: self.set_buttons_state("normal"))

    # --------------------------------------------------------------- Search
    def start_search_thread(self):
        query = self.ent_query.get().strip()
        if not query:
            messagebox.showwarning("Peringatan", "Masukkan kata kunci / username dulu.")
            return
        if not SESSION_FILE.exists():
            messagebox.showwarning("Belum Login", "Login Instagram dulu lewat tombol 'Login Instagram'.")
            return
        if not self.platform_id:
            messagebox.showerror("Error", "Platform Instagram belum siap di database.")
            return

        try:
            limit = int(self.limit_spinbox.get())
        except ValueError:
            limit = 10

        mode = self.cb_mode.get()
        self.set_buttons_state("disabled")
        self.db_btn.config(state="disabled")
        self.status_label.config(text="Mencari post...")

        for item in self.tree.get_children():
            self.tree.delete(item)

        threading.Thread(
            target=self.perform_search,
            args=(mode, query, limit, self.var_headless.get()),
            daemon=True,
        ).start()

    def build_search_url(self, mode, query):
        if mode.startswith("Hashtag"):
            tag = re.sub(r"\s+", "", query.lstrip("#")).lower()
            return f"https://www.instagram.com/explore/tags/{urllib.parse.quote(tag)}/", None
        username = query.lstrip("@").strip()
        return f"https://www.instagram.com/{urllib.parse.quote(username)}/", f"@{username}"

    def perform_search(self, mode, query, limit, headless):
        self.results_data = []
        try:
            url, known_author = self.build_search_url(mode, query)
            self.log(f"\n[*] Membuka {url}")

            found = {}  # shortcode -> item

            with sync_playwright() as p:
                browser = p.chromium.launch(headless=headless)
                context = browser.new_context(
                    storage_state=str(SESSION_FILE),
                    viewport={"width": 1100, "height": 900},
                    locale="id-ID",
                )
                page = context.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
                time.sleep(3)

                if is_login_page(page.url):
                    self.log("[!] Sesi Instagram tidak valid / kedaluwarsa. Klik Login Instagram lagi.")
                    browser.close()
                    return

                try:
                    page.wait_for_selector('a[href*="/p/"], a[href*="/reel/"]', timeout=20000)
                except Exception:
                    self.log("[-] Tidak ada post terdeteksi (halaman kosong / dibatasi / selector berubah).")
                    browser.close()
                    return

                # ---- Tahap 1: kumpulkan link post
                idle = 0
                for _ in range(MAX_SCROLLS):
                    hrefs = page.eval_on_selector_all('a[href*="/p/"], a[href*="/reel/"]', JS_HREFS)
                    before = len(found)

                    for h in hrefs:
                        kind, code, clean_url = normalize_post_url(h)
                        if not code or code in found:
                            continue
                        found[code] = {
                            "shortcode": code,
                            "kind": kind,
                            "clean_url": clean_url,
                            "author": known_author or "-",
                            "published_at": None,
                            "caption": "",
                            "db_status": "Mengecek...",
                            "detail_ok": False,
                        }
                        if len(found) >= limit:
                            break

                    if len(found) >= limit:
                        break

                    idle = idle + 1 if len(found) == before else 0
                    if idle >= MAX_IDLE_SCROLLS:
                        self.log("   [i] Tidak ada post baru lagi, berhenti scroll.")
                        break

                    page.mouse.wheel(0, random.randint(1500, 2400))
                    time.sleep(random.uniform(1.5, 3.0))

                self.results_data = list(found.values())[:limit]
                self.log(f"[*] Ditemukan {len(self.results_data)} post. Mengecek status di MySQL...")
                self.results_data = self.check_posts_exist_in_db(self.results_data)
                self.root.after(0, self.populate_table)

                # ---- Tahap 2: ambil username, tanggal terbit & caption untuk post yang belum ada di DB
                to_fetch = [
                    (idx, item) for idx, item in enumerate(self.results_data)
                    if item["db_status"] != "Ada di DB"
                ]
                self.log(f"[*] Mengambil username, tanggal terbit & caption untuk {len(to_fetch)} post baru...")

                for n, (idx, item) in enumerate(to_fetch, start=1):
                    self.ui_row(idx, process_status="Fetch detail...")
                    try:
                        detail, err = extract_post_detail(page, item["clean_url"], item["author"])
                    except Exception as e:
                        detail, err = None, f"error: {e}"

                    if err == "login":
                        self.log("   [!] Sesi Instagram tidak valid / kedaluwarsa. Klik Login Instagram lagi.")
                        break

                    if detail is None:
                        self.ui_row(idx, process_status="GAGAL")
                        self.log(f"   [!] ({n}/{len(to_fetch)}) Gagal ambil detail ({err}): {item['clean_url']}")
                        continue

                    item.update(detail)
                    item["detail_ok"] = True
                    self.ui_row(
                        idx,
                        author=detail["username"],
                        published_at=detail["published_at"],
                        caption=detail["caption"],
                        process_status="Siap",
                    )
                    self.log(
                        f"   [OK] ({n}/{len(to_fetch)}) {detail['username']} [{detail['handle_source']}] | "
                        f"{detail['published_at'] or '-'} | {one_line(detail['caption'], 60)}"
                    )

                    if n < len(to_fetch):
                        time.sleep(random.uniform(2, 4))

                browser.close()

        except Exception as e:
            self.log(f"[!] Error saat mencari: {e}")

        finally:
            self.root.after(0, self.finish_search)

    def populate_table(self):
        """Isi tabel dengan hasil pencarian (detail menyusul lewat ui_row)."""
        for idx, item in enumerate(self.results_data, start=1):
            self.tree.insert(
                "",
                "end",
                iid=str(idx - 1),
                values=(
                    idx,
                    item["author"],
                    item.get("published_at") or "-",
                    one_line(item["caption"]) or "-",
                    item["clean_url"],
                    item["db_status"],
                    "Menunggu detail..." if item["db_status"] != "Ada di DB" else "Sudah di DB",
                ),
            )

    def finish_search(self):
        if self.results_data:
            exist_count = sum(1 for i in self.results_data if i["db_status"] == "Ada di DB")
            new_count = len(self.results_data) - exist_count
            got_detail = sum(1 for i in self.results_data if i.get("detail_ok"))

            self.status_label.config(
                text=f"Total Post: {len(self.results_data)} | Baru: {new_count} | Ada di DB: {exist_count}"
            )
            self.log(
                f"[+] Selesai! {len(self.results_data)} post ({new_count} baru, {exist_count} sudah ada di DB), "
                f"detail berhasil diambil: {got_detail}."
            )
            self.db_btn.config(state="normal")
        else:
            self.status_label.config(text="Post tidak ditemukan.")

        self.set_buttons_state("normal")

    def on_row_double_click(self, _event):
        sel = self.tree.focus()
        if not sel:
            return
        try:
            item = self.results_data[int(sel)]
        except (ValueError, IndexError):
            return
        if not item.get("detail_ok"):
            return
        messagebox.showinfo(
            "Detail Post",
            f"Author : {item['author']}\n"
            f"Terbit : {item.get('published_at') or '-'} (UTC)\n\n"
            f"Caption:\n{item['caption'] or '(tanpa caption)'}\n\n"
            f"URL: {item['clean_url']}",
        )

    # ----------------------------------------------------------------- Save
    def start_save_thread(self):
        self.db_btn.config(state="disabled")
        self.set_buttons_state("disabled")
        self.status_label.config(text="Menyimpan ke database...")
        threading.Thread(
            target=self.perform_save_to_db, args=(self.var_headless.get(),), daemon=True
        ).start()

    def update_tree_row(self, row_idx, author=None, published_at=None, caption=None, db_status=None, process_status=None):
        try:
            vals = list(self.tree.item(str(row_idx), "values"))
            if author:
                vals[1] = author
            if published_at:
                vals[2] = published_at
            if caption is not None and (caption or vals[3] in ("", "-")):
                vals[3] = one_line(caption) or "(tanpa caption)"
            if db_status:
                vals[5] = db_status
            if process_status:
                vals[6] = process_status
            self.tree.item(str(row_idx), values=vals)
        except Exception:
            pass

    def ui_row(self, idx, **kw):
        self.root.after(0, lambda: self.update_tree_row(idx, **kw))

    def perform_save_to_db(self, headless):
        inserted = 0
        skipped = 0
        conn = None
        cursor = None

        items = [
            (idx, item) for idx, item in enumerate(self.results_data)
            if item.get("db_status") == "Belum Ada di DB"
        ]

        if not items:
            self.log("\n[i] Semua post dalam list sudah ada di database.")
            self.root.after(0, lambda: self.set_buttons_state("normal"))
            self.root.after(0, lambda: self.db_btn.config(state="normal"))
            return

        self.log(f"\n[*] Memproses {len(items)} post baru ke database...")

        try:
            conn = get_db_connection()
            cursor = conn.cursor(dictionary=True)

            with sync_playwright() as p:
                browser = None
                page = None

                for step, (idx, item) in enumerate(items, start=1):
                    clean_url = item["clean_url"]
                    code = item["shortcode"]
                    fetched_now = False

                    try:
                        # Detail biasanya sudah diambil di langkah 1; fetch ulang hanya kalau belum ada
                        if not item.get("detail_ok"):
                            if page is None:
                                browser = p.chromium.launch(headless=headless)
                                context = browser.new_context(
                                    storage_state=str(SESSION_FILE),
                                    viewport={"width": 1100, "height": 900},
                                    locale="id-ID",
                                )
                                page = context.new_page()

                            self.ui_row(idx, process_status="1/2 Fetch detail...")
                            detail, err = extract_post_detail(page, clean_url, item.get("author"))
                            fetched_now = True

                            if err == "login":
                                self.log("   [!] Sesi Instagram tidak valid / kedaluwarsa. Klik Login Instagram lagi.")
                                break
                            if detail is None:
                                self.log(f"   [!] Post tidak termuat (dihapus / privat / rate limit?): {clean_url}")
                                self.ui_row(idx, process_status="GAGAL")
                                continue

                            item.update(detail)
                            item["detail_ok"] = True

                        username = item["username"]
                        caption = item["caption"]
                        title = item["title"]
                        media_urls = item.get("media_urls") or []
                        media_urls_json = json.dumps(media_urls) if media_urls else None
                        raw_text = f"{username} - {caption}" if caption else username

                        self.ui_row(
                            idx, author=username, published_at=item.get("published_at"), caption=caption,
                            process_status="2/2 Menyimpan ke DB...",
                        )

                        conn, cursor = self.refresh_db(conn, cursor)
                        cursor.execute(
                            """
                            INSERT IGNORE INTO contents
                            (platform_id, clean_url, original_id, title, raw_text, media_urls, published_at)
                            VALUES (%s, %s, %s, %s, %s, %s, %s)
                            """,
                            (
                                self.platform_id, clean_url, code, title, raw_text,
                                media_urls_json, item.get("published_at"),
                            ),
                        )

                        if cursor.rowcount > 0:
                            inserted += 1
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
                            item["db_status"] = "Ada di DB"
                            self.ui_row(idx, db_status="Ada di DB", process_status="SUKSES")
                            self.log(f"   [OK] [{username}] ({len(media_urls)} gambar) Saved: {clean_url}")
                        else:
                            skipped += 1
                            item["db_status"] = "Ada di DB"
                            self.ui_row(idx, db_status="Ada di DB", process_status="SKIPPED")
                            self.log("   [SKIP] Post ini sudah ada di database.")

                    except Exception as e:
                        try:
                            conn.rollback()
                        except Exception:
                            pass
                        self.ui_row(idx, process_status="ERROR")
                        self.log(f"   [!] Error memproses {clean_url}: {e}")

                    if fetched_now and step < len(items):
                        time.sleep(random.uniform(3, 6))

                if browser:
                    browser.close()

            msg = f"Selesai! {inserted} post berhasil disimpan, {skipped} di-skip/duplikat."
            self.log("\n" + "=" * 50)
            self.log(f"[+] {msg}")
            self.root.after(0, lambda: messagebox.showinfo("Proses Selesai", msg))

        except Exception as e:
            self.log(f"\n[ERROR FATAL] {e}")

        finally:
            try:
                if cursor:
                    cursor.close()
                if conn:
                    conn.close()
            except Exception:
                pass
            self.root.after(0, lambda: self.status_label.config(text="Selesai memproses."))
            self.root.after(0, lambda: self.set_buttons_state("normal"))
            self.root.after(0, lambda: self.db_btn.config(state="normal"))


if __name__ == "__main__":
    root = tk.Tk()
    app = IGScraperApp(root)
    root.mainloop()