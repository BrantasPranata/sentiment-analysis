import json
import logging
import random
import re
import sys
import threading
import time
import tkinter as tk
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

# File sesi login Instagram (dibuat lewat tombol Login, dipakai bersama ig_scraper.py)
SESSION_FILE = SESSION_DIR / "gui" / "ig_session.json"

# Batas waktu menunggu login manual (detik)
LOGIN_TIMEOUT = 300
# Batas pengaman jumlah langkah scroll / klik "muat komentar lainnya" per post
MAX_SCROLLS = 40
# Berhenti kalau N langkah berturut-turut tidak ada komentar baru
MAX_IDLE_SCROLLS = 4

# Penanda objek komentar (bukan caption) di JSON Instagram, baik REST maupun GraphQL
COMMENT_MARKERS = ("comment_like_count", "child_comment_count", "parent_comment_id", "has_liked_comment_like")

# JS: scroll panel komentar (elemen yang bisa di-scroll & berisi <time>), fallback scroll halaman
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

POST_RE = re.compile(r"instagram\.com/(?:[^/]+/)?(?:p|reel)/([A-Za-z0-9_-]+)")


class SessionError(Exception):
    """Sesi Instagram tidak valid / kedaluwarsa / kena checkpoint."""


class RateLimitError(Exception):
    """Instagram membatasi request (HTTP 429)."""


def parse_ig_shortcode(url: str):
    m = POST_RE.search(url or "")
    return m.group(1) if m else None


def parse_json_text(text: str):
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


def is_comment_obj(node: dict) -> bool:
    user = node.get("user")
    return (
        isinstance(node.get("text"), str)
        and isinstance(user, dict)
        and bool(user.get("username"))
        and any(k in node for k in COMMENT_MARKERS)
    )


def iter_comment_objects(node, depth=0):
    """Telusuri JSON secara rekursif & yield objek komentar top-level (balasan dilewati)."""
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


class IGCommentScraperGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Instagram Comments Scraper - db_sentimenanalisis_v2")
        self.root.geometry("850x700")

        self.contents_map = {}  # display string -> (content_id, shortcode, clean_url)
        self.platform_id = None

        self.create_widgets()

        try:
            self.platform_id = get_or_create_instagram_platform_id()
        except Exception as e:
            self.log(f"[!] Gagal menyiapkan platform Instagram: {e}")

        self.load_ig_contents_from_db()
        self.update_session_label()

    # ------------------------------------------------------------------ GUI
    def create_widgets(self):
        frame_target = ttk.LabelFrame(self.root, text=" Target Post Instagram dari Database ")
        frame_target.pack(fill="x", padx=10, pady=5)
        frame_target.columnconfigure(1, weight=1)

        ttk.Label(frame_target, text="Pilih Post:").grid(row=0, column=0, padx=5, pady=5, sticky="w")

        self.cb_post = ttk.Combobox(frame_target, state="readonly", width=80)
        self.cb_post.grid(row=0, column=1, padx=5, pady=5, sticky="ew")

        btn_refresh = ttk.Button(frame_target, text="🔄 Refresh DB", command=self.load_ig_contents_from_db)
        btn_refresh.grid(row=0, column=2, padx=5, pady=5)

        # Sesi login
        frame_session = ttk.LabelFrame(self.root, text=" Sesi Login Instagram ")
        frame_session.pack(fill="x", padx=10, pady=5)

        self.lbl_session = ttk.Label(frame_session, text="")
        self.lbl_session.pack(side="left", padx=5, pady=5)

        self.btn_login = ttk.Button(
            frame_session, text="🔑 Login Instagram (simpan sesi)", command=self.start_login_thread
        )
        self.btn_login.pack(side="right", padx=5, pady=5)

        # Settings
        frame_settings = ttk.LabelFrame(self.root, text=" Pengaturan Crawling ")
        frame_settings.pack(fill="x", padx=10, pady=5)

        ttk.Label(frame_settings, text="Batas Komentar Baru per Post:").grid(
            row=0, column=0, padx=5, pady=5, sticky="w"
        )
        self.ent_limit = ttk.Entry(frame_settings, width=15)
        self.ent_limit.insert(0, "50")
        self.ent_limit.grid(row=0, column=1, padx=5, pady=5, sticky="w")

        self.var_headless = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            frame_settings,
            text="Headless (browser tidak ditampilkan)",
            variable=self.var_headless,
        ).grid(row=0, column=2, padx=15, pady=5, sticky="w")

        ttk.Label(frame_settings, text="Mulai dari Post ke-N (mode SEMUA):").grid(
            row=1, column=0, padx=5, pady=5, sticky="w"
        )
        self.ent_start = ttk.Entry(frame_settings, width=15)
        self.ent_start.insert(0, "1")
        self.ent_start.grid(row=1, column=1, padx=5, pady=5, sticky="w")

        # Buttons
        frame_buttons = ttk.Frame(self.root)
        frame_buttons.pack(fill="x", padx=10, pady=5)

        self.btn_start = ttk.Button(
            frame_buttons,
            text="▶️ Crawl Post Terpilih",
            command=lambda: self.start_crawling_thread(process_all=False),
        )
        self.btn_start.pack(side="left", padx=5)

        self.btn_start_all = ttk.Button(
            frame_buttons,
            text="⏩ Crawl SEMUA Post Instagram di DB",
            command=lambda: self.start_crawling_thread(process_all=True),
        )
        self.btn_start_all.pack(side="left", padx=5)

        # Log
        frame_log = ttk.LabelFrame(self.root, text=" Log Aktivitas ")
        frame_log.pack(fill="both", expand=True, padx=10, pady=5)

        self.txt_log = ScrolledText(frame_log, state="disabled", height=20)
        self.txt_log.pack(fill="both", expand=True, padx=5, pady=5)

    def _append_log(self, message):
        self.txt_log.config(state="normal")
        self.txt_log.insert(tk.END, message + "\n")
        self.txt_log.see(tk.END)
        self.txt_log.config(state="disabled")

    def log(self, message):
        """Thread-safe: aman dipanggil dari thread worker."""
        self.root.after(0, lambda m=message: self._append_log(m))

    def set_buttons_state(self, state):
        for btn in (self.btn_start, self.btn_start_all, self.btn_login):
            btn.config(state=state)

    def update_session_label(self):
        if SESSION_FILE.exists():
            self.lbl_session.config(text=f"✅ Sesi tersimpan: {SESSION_FILE.name}")
        else:
            self.lbl_session.config(text="⚠️ Belum ada sesi. Klik Login Instagram dulu (wajib).")

    # ------------------------------------------------------------------ DB
    def load_ig_contents_from_db(self):
        """Memuat daftar post Instagram dari tabel contents."""
        self.cb_post["values"] = []
        self.contents_map.clear()

        if not self.platform_id:
            self.log("[!] Platform Instagram belum siap di database.")
            return

        conn = None
        cursor = None
        try:
            conn = get_db_connection()
            cursor = conn.cursor(dictionary=True)

            cursor.execute(
                """
                SELECT id, original_id, clean_url, title, raw_text
                FROM contents
                WHERE platform_id = %s
                  AND (clean_url LIKE %s OR clean_url LIKE %s)
                ORDER BY id DESC
                """,
                (self.platform_id, "%instagram.com/p/%", "%instagram.com/reel/%"),
            )
            rows = cursor.fetchall()

            combo_values = []
            for row in rows:
                shortcode = parse_ig_shortcode(row["clean_url"]) or row["original_id"]
                if not shortcode:
                    continue

                c_id = row["id"]
                title = re.sub(r"\s+", " ", row["title"] or row["raw_text"] or row["clean_url"])[:60]
                display_str = f"[ID: {c_id}] {title}"

                combo_values.append(display_str)
                self.contents_map[display_str] = (c_id, str(shortcode), row["clean_url"])

            if not combo_values:
                self.log(f"[!] Tidak ada post Instagram (platform_id = {self.platform_id}) di tabel contents.")
                return

            self.cb_post["values"] = combo_values
            self.cb_post.current(0)
            self.log(f"[+] Berhasil memuat {len(combo_values)} post Instagram dari database.")

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

    def safe_rollback(self, conn):
        try:
            conn.rollback()
        except Exception:
            pass

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

    # ------------------------------------------------------------- Crawling
    def start_crawling_thread(self, process_all=False):
        try:
            limit = int(self.ent_limit.get().strip())
        except ValueError:
            messagebox.showerror("Error", "Batas komentar harus berupa angka!")
            return

        try:
            start_from = int(self.ent_start.get().strip() or "1")
            if start_from < 1:
                raise ValueError
        except ValueError:
            messagebox.showerror("Error", "'Mulai dari Post ke-N' harus angka >= 1!")
            return

        if not SESSION_FILE.exists():
            messagebox.showwarning("Belum Login", "Login Instagram dulu lewat tombol 'Login Instagram'.")
            return

        self.set_buttons_state("disabled")

        threading.Thread(
            target=self.run_comments_crawling,
            args=(limit, process_all, self.var_headless.get(), start_from),
            daemon=True,
        ).start()

    def parse_embedded_json(self, page, add_from):
        """Komentar awal sering sudah tertanam di <script type=application/json> pada halaman."""
        try:
            texts = page.eval_on_selector_all(
                'script[type="application/json"]', "els => els.map(e => e.textContent)"
            )
        except Exception:
            return
        for t in texts:
            if not t or not any(m in t for m in COMMENT_MARKERS):
                continue
            data = parse_json_text(t)
            if data is not None:
                add_from(data)

    def load_more_comments(self, page):
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

    def collect_comments(self, page, clean_url, log):
        """
        Buka post lalu tangkap komentar dari respons jaringan (GraphQL/REST) yang dipanggil
        Instagram sendiri + JSON yang tertanam di halaman. Yield dict {id, handle, text}.

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
                state["found"][key] = {"id": key, "handle": handle, "text": text}
                state["order"].append(key)

        def on_response(resp):
            try:
                if resp.status == 429:
                    state["rate_limited"] = True
                    return
                url = resp.url
                if "instagram.com" not in url or not any(
                    k in url for k in ("/graphql", "/api/v1/", "/api/graphql")
                ):
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

            if is_login_page(page.url):
                raise SessionError("diarahkan ke halaman login")

            self.parse_embedded_json(page, add_from)

            yielded = 0
            idle = 0
            for _ in range(MAX_SCROLLS):
                while yielded < len(state["order"]):
                    yield state["found"][state["order"][yielded]]
                    yielded += 1

                if state["rate_limited"]:
                    raise RateLimitError("HTTP 429")

                before = len(state["order"])
                self.load_more_comments(page)
                page.wait_for_timeout(random.randint(1800, 3200))

                if len(state["order"]) == before:
                    idle += 1
                    if idle >= MAX_IDLE_SCROLLS:
                        break
                else:
                    idle = 0

            while yielded < len(state["order"]):
                yield state["found"][state["order"][yielded]]
                yielded += 1

            if yielded == 0:
                log(
                    "   [i] Tidak ada komentar tertangkap (post mungkin tanpa komentar / komentar dinonaktifkan / "
                    "format respons Instagram berubah)."
                )
                if state["endpoints"]:
                    log("   [i] Endpoint JSON yang terlihat: " + " | ".join(state["endpoints"]))
        finally:
            try:
                page.remove_listener("response", on_response)
            except Exception:
                pass

    def run_comments_crawling(self, limit, process_all, headless, start_from=1):
        conn = None
        cursor = None

        try:
            conn = get_db_connection()
            cursor = conn.cursor(dictionary=True)

            targets = []
            offset = 0
            if process_all:
                all_targets = list(self.contents_map.values())
                offset = max(0, start_from - 1)
                targets = all_targets[offset:]
                if offset:
                    self.log(f"[i] Melompati {min(offset, len(all_targets))} post pertama, mulai dari post ke-{start_from}.")
            else:
                selected_str = self.cb_post.get()
                if selected_str in self.contents_map:
                    targets.append(self.contents_map[selected_str])

            if not targets:
                self.log("[!] Tidak ada target post terpilih.")
                return

            total_inserted = 0
            total_skipped = 0

            with sync_playwright() as p:
                browser = p.chromium.launch(headless=headless)
                context = browser.new_context(
                    storage_state=str(SESSION_FILE),
                    viewport={"width": 1280, "height": 900},
                    locale="id-ID",
                )
                page = context.new_page()

                total_all = offset + len(targets)
                for idx, (c_id, shortcode, clean_url) in enumerate(targets, start=offset + 1):
                    self.log(f"\n[*] ({idx}/{total_all}) Crawling Komentar [Content ID: {c_id}] -> {clean_url}")

                    gen = None
                    try:
                        conn, cursor = self.refresh_db(conn, cursor)

                        count_for_post = 0
                        skipped_for_post = 0

                        gen = self.collect_comments(page, clean_url, self.log)
                        for cm in gen:
                            if count_for_post >= limit:
                                break

                            comment_text = cm["text"]
                            author = f"@{cm['handle']}"[:150]

                            conn, cursor = self.refresh_db(conn, cursor)

                            if self.comment_exists(cursor, c_id, author, comment_text):
                                skipped_for_post += 1
                                total_skipped += 1
                                continue

                            cursor.execute(
                                """
                                INSERT INTO comments (content_id, comment_text, author_name)
                                VALUES (%s, %s, %s)
                                """,
                                (c_id, comment_text, author),
                            )
                            comment_id = cursor.lastrowid
                            count_for_post += 1
                            total_inserted += 1

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
                            f"   [SUCCESS] +{count_for_post} komentar baru, "
                            f"{skipped_for_post} duplikat dilewati."
                        )

                    except SessionError as s_err:
                        self.safe_rollback(conn)
                        self.log(f"   [!] Sesi ditolak Instagram ({s_err}). Klik Login Instagram lagi, lalu ulangi.")
                        break

                    except RateLimitError:
                        self.safe_rollback(conn)
                        self.log("   [!] Kena rate limit Instagram (429). Berhenti — tunggu beberapa jam / besok, lalu lanjut "
                                 f"dari post ke-{idx} lewat 'Mulai dari Post ke-N'.")
                        break

                    except Exception as c_err:
                        self.safe_rollback(conn)
                        self.log(f"   [!] Error crawling komentar untuk ID {c_id}: {c_err}")

                    finally:
                        if gen is not None:
                            try:
                                gen.close()  # pastikan listener respons dilepas
                            except Exception:
                                pass

                    # Jeda antar post supaya tidak terlalu agresif
                    if idx < total_all:
                        time.sleep(random.uniform(5, 10))

                browser.close()

            self.log("\n" + "=" * 50)
            self.log(
                f"[*] CRAWLING SELESAI! Komentar Baru: {total_inserted} | "
                f"Duplikat Dilewati: {total_skipped}"
            )
            self.log("=" * 50)

        except Exception as err:
            self.log(f"[!] Error: {err}")

        finally:
            try:
                if cursor:
                    cursor.close()
                if conn:
                    conn.close()
            except Exception:
                pass
            self.root.after(0, lambda: self.set_buttons_state("normal"))


if __name__ == "__main__":
    root = tk.Tk()
    app = IGCommentScraperGUI(root)
    root.mainloop()