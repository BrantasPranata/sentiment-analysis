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

# Platform ID untuk X di tabel platforms / contents
X_PLATFORM_ID = 4

# File sesi login X (disimpan setelah login manual sekali lewat tombol Login)
SESSION_FILE = SESSION_DIR / "gui" / "x_session.json"

# Berhenti scroll kalau N kali scroll berturut-turut tidak ada reply baru
MAX_IDLE_SCROLLS = 6
# Batas pengaman jumlah scroll per tweet
MAX_SCROLLS = 80
# Batas waktu menunggu login manual (detik)
LOGIN_TIMEOUT = 300

# JS: ambil id, username, dan teks dari semua <article> tweet yang sedang ter-render
JS_EXTRACT = """
els => els.map(a => {
    const t = a.querySelector('time');
    const link = t ? t.closest('a') : null;
    const href = link ? (link.getAttribute('href') || '') : '';
    const m = href.match(/^\\/([^\\/]+)\\/status\\/(\\d+)/);
    const textEl = a.querySelector('[data-testid="tweetText"]');
    return {
        handle: m ? m[1] : '',
        id: m ? m[2] : '',
        text: textEl ? textEl.innerText : ''
    };
})
"""


def parse_x_url(x_url: str):
    """Ekstrak (username, tweet_id) dari URL x.com / twitter.com."""
    match = re.search(r"(?:x|twitter)\.com/([^/]+)/status/(\d+)", x_url or "")
    if match:
        return match.group(1), match.group(2)
    return None, None


class XRepliesScraperGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("X Replies Scraper - db_sentimenanalisis_v2")
        self.root.geometry("850x700")

        self.contents_map = {}  # display string -> (content_id, tweet_id, clean_url)

        self.create_widgets()
        self.load_x_contents_from_db()
        self.update_session_label()

    # ------------------------------------------------------------------ GUI
    def create_widgets(self):
        # Frame Target
        frame_target = ttk.LabelFrame(self.root, text=" Target Tweet X dari Database ")
        frame_target.pack(fill="x", padx=10, pady=5)
        frame_target.columnconfigure(1, weight=1)

        ttk.Label(frame_target, text="Pilih Tweet:").grid(row=0, column=0, padx=5, pady=5, sticky="w")

        self.cb_tweet = ttk.Combobox(frame_target, state="readonly", width=80)
        self.cb_tweet.grid(row=0, column=1, padx=5, pady=5, sticky="ew")

        btn_refresh = ttk.Button(frame_target, text="🔄 Refresh DB", command=self.load_x_contents_from_db)
        btn_refresh.grid(row=0, column=2, padx=5, pady=5)

        # Frame Sesi Login
        frame_session = ttk.LabelFrame(self.root, text=" Sesi Login X ")
        frame_session.pack(fill="x", padx=10, pady=5)

        self.lbl_session = ttk.Label(frame_session, text="")
        self.lbl_session.pack(side="left", padx=5, pady=5)

        self.btn_login = ttk.Button(frame_session, text="🔑 Login X (simpan sesi)", command=self.start_login_thread)
        self.btn_login.pack(side="right", padx=5, pady=5)

        # Frame Settings
        frame_settings = ttk.LabelFrame(self.root, text=" Pengaturan Crawling ")
        frame_settings.pack(fill="x", padx=10, pady=5)

        ttk.Label(frame_settings, text="Batas Reply Baru per Tweet:").grid(row=0, column=0, padx=5, pady=5, sticky="w")
        self.ent_limit = ttk.Entry(frame_settings, width=15)
        self.ent_limit.insert(0, "50")
        self.ent_limit.grid(row=0, column=1, padx=5, pady=5, sticky="w")

        self.var_headless = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            frame_settings,
            text="Headless (browser tidak ditampilkan)",
            variable=self.var_headless,
        ).grid(row=0, column=2, padx=15, pady=5, sticky="w")

        ttk.Label(frame_settings, text="Mulai dari Tweet ke-N (mode SEMUA):").grid(
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
            text="▶️ Crawl Tweet Terpilih",
            command=lambda: self.start_crawling_thread(process_all=False),
        )
        self.btn_start.pack(side="left", padx=5)

        self.btn_start_all = ttk.Button(
            frame_buttons,
            text="⏩ Crawl SEMUA Tweet X di DB",
            command=lambda: self.start_crawling_thread(process_all=True),
        )
        self.btn_start_all.pack(side="left", padx=5)

        # Log Area
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
            self.lbl_session.config(text="⚠️ Belum ada sesi. Klik Login X dulu (wajib untuk melihat replies).")

    # ------------------------------------------------------------------ DB
    def load_x_contents_from_db(self):
        """Memuat daftar tweet X (platform_id = 4) dari tabel contents."""
        self.cb_tweet["values"] = []
        self.contents_map.clear()

        conn = None
        cursor = None
        try:
            conn = get_db_connection()
            cursor = conn.cursor(dictionary=True)

            cursor.execute(
                """
                SELECT id, original_id, clean_url, title
                FROM contents
                WHERE platform_id = %s
                  AND clean_url LIKE %s
                ORDER BY id DESC
                """,
                (X_PLATFORM_ID, "%/status/%"),
            )
            rows = cursor.fetchall()

            if not rows:
                self.log(f"[!] Tidak ada tweet X (platform_id = {X_PLATFORM_ID}) di tabel contents.")
                return

            combo_values = []
            for row in rows:
                c_id = row["id"]
                _, tweet_id = parse_x_url(row["clean_url"])
                tweet_id = tweet_id or row["original_id"]
                if not tweet_id:
                    continue

                title = row["title"][:60] if row["title"] else row["clean_url"]
                display_str = f"[ID: {c_id}] {title}"

                combo_values.append(display_str)
                self.contents_map[display_str] = (c_id, str(tweet_id), row["clean_url"])

            self.cb_tweet["values"] = combo_values
            if combo_values:
                self.cb_tweet.current(0)

            self.log(f"[+] Berhasil memuat {len(combo_values)} tweet X dari database.")

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
        """
        Pastikan koneksi MySQL masih hidup; kalau putus (idle lama / jaringan / VPN),
        sambung ulang otomatis. Mengembalikan (conn, cursor) yang valid.
        """
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
        """Rollback yang tidak melempar error kalau koneksi sudah mati."""
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
                page.goto("https://x.com/login")

                self.log(f"[*] Silakan login di jendela browser (maks {LOGIN_TIMEOUT // 60} menit)...")

                deadline = time.time() + LOGIN_TIMEOUT
                logged_in = False
                while time.time() < deadline:
                    try:
                        cookies = context.cookies("https://x.com")
                    except Exception:
                        break  # browser ditutup user
                    if any(c["name"] == "auth_token" for c in cookies):
                        logged_in = True
                        break
                    time.sleep(2)

                if logged_in:
                    time.sleep(2)  # beri waktu cookie lain selesai di-set
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
        """Menjalankan crawling di thread terpisah agar GUI lancar."""
        try:
            limit = int(self.ent_limit.get().strip())
        except ValueError:
            messagebox.showerror("Error", "Batas reply harus berupa angka!")
            return

        try:
            start_from = int(self.ent_start.get().strip() or "1")
            if start_from < 1:
                raise ValueError
        except ValueError:
            messagebox.showerror("Error", "'Mulai dari Tweet ke-N' harus angka >= 1!")
            return

        if not SESSION_FILE.exists():
            messagebox.showwarning("Belum Login", "Login X dulu lewat tombol 'Login X (simpan sesi)'.")
            return

        self.set_buttons_state("disabled")

        threading.Thread(
            target=self.run_replies_crawling,
            args=(limit, process_all, self.var_headless.get(), start_from),
            daemon=True,
        ).start()

    def collect_replies(self, page, tweet_id, limit, existing_checker, log):
        """
        Scroll halaman tweet dan kumpulkan reply baru (yang belum ada di DB).
        Yield dict {id, handle, text} satu per satu.

        X memakai virtualized list (tweet di luar layar dihapus dari DOM), jadi
        kita harus ekstrak di setiap langkah scroll dan dedupe lewat tweet id.
        """
        seen_ids = set()
        idle_scrolls = 0
        main_id = int(tweet_id)

        for scroll_no in range(MAX_SCROLLS):
            # Klik tombol "tampilkan balasan tambahan" kalau ada
            for label in ("Show additional replies", "Tampilkan balasan tambahan", "Show more replies", "Tampilkan balasan lainnya"):
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

                # Reply selalu punya snowflake id > tweet aslinya.
                # Ini otomatis membuang tweet utama dan tweet parent (kalau URL-nya sendiri reply).
                if int(a_id) <= main_id:
                    continue

                text = (art.get("text") or "").strip()
                handle = art.get("handle") or "unknown"
                if not text:
                    continue

                new_in_step += 1
                yield {"id": a_id, "handle": handle, "text": text}

            if new_in_step == 0:
                idle_scrolls += 1
                if idle_scrolls >= MAX_IDLE_SCROLLS:
                    log("   [i] Tidak ada reply baru lagi, berhenti scroll.")
                    return
            else:
                idle_scrolls = 0

            page.mouse.wheel(0, random.randint(1800, 2600))
            time.sleep(random.uniform(1.5, 2.8))

    def run_replies_crawling(self, limit, process_all, headless, start_from=1):
        """Eksekutor crawling replies X."""
        conn = None
        cursor = None

        try:
            conn = get_db_connection()
            cursor = conn.cursor(dictionary=True)

            # Menentukan daftar tweet yang akan di-crawl
            targets = []
            offset = 0  # jumlah tweet yang dilewati di awal (hanya mode SEMUA)
            if process_all:
                all_targets = list(self.contents_map.values())
                offset = max(0, start_from - 1)
                targets = all_targets[offset:]
                if offset:
                    self.log(f"[i] Melompati {min(offset, len(all_targets))} tweet pertama, mulai dari tweet ke-{start_from}.")
            else:
                selected_str = self.cb_tweet.get()
                if selected_str in self.contents_map:
                    targets.append(self.contents_map[selected_str])

            if not targets:
                self.log("[!] Tidak ada target tweet terpilih.")
                return

            total_inserted = 0
            total_skipped = 0

            with sync_playwright() as p:
                browser = p.chromium.launch(headless=headless)
                context = browser.new_context(
                    storage_state=str(SESSION_FILE),
                    viewport={"width": 1100, "height": 900},
                    locale="id-ID",
                )
                page = context.new_page()

                total_all = offset + len(targets)
                for idx, (c_id, tweet_id, clean_url) in enumerate(targets, start=offset + 1):
                    self.log(f"\n[*] ({idx}/{total_all}) Crawling Replies [Content ID: {c_id}] -> {clean_url}")

                    try:
                        conn, cursor = self.refresh_db(conn, cursor)

                        page.goto(clean_url, wait_until="domcontentloaded", timeout=45000)

                        # Sesi kedaluwarsa / dipaksa login
                        time.sleep(2)
                        if "login" in page.url or "/i/flow" in page.url:
                            self.log("   [!] Sesi X tidak valid / kedaluwarsa. Klik Login X lagi.")
                            break

                        try:
                            page.wait_for_selector('article[data-testid="tweet"]', timeout=20000)
                        except Exception:
                            self.log("   [!] Tweet tidak termuat (dihapus / dibatasi / rate limit?). Dilewati.")
                            continue

                        count_for_tweet = 0
                        skipped_for_tweet = 0

                        for reply in self.collect_replies(page, tweet_id, limit, None, self.log):
                            if count_for_tweet >= limit:
                                break

                            comment_text = reply["text"]
                            author = f"@{reply['handle']}"[:150]

                            # Scroll bisa lama antar reply, jadi koneksi DB dicek dulu
                            conn, cursor = self.refresh_db(conn, cursor)

                            # Lewati kalau content_id + author + isi komentar sudah ada
                            if self.comment_exists(cursor, c_id, author, comment_text):
                                skipped_for_tweet += 1
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
                            count_for_tweet += 1
                            total_inserted += 1

                            # Antrikan komentar ke sentiment_results (target_type = 'comment')
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
                            f"   [SUCCESS] +{count_for_tweet} reply baru, "
                            f"{skipped_for_tweet} duplikat dilewati."
                        )

                    except Exception as c_err:
                        self.safe_rollback(conn)
                        self.log(f"   [!] Error crawling replies untuk ID {c_id}: {c_err}")

                    # Jeda antar tweet supaya tidak terlalu agresif
                    if idx < total_all:
                        time.sleep(random.uniform(3, 6))

                browser.close()

            self.log("\n" + "=" * 50)
            self.log(
                f"[*] CRAWLING SELESAI! Reply Baru: {total_inserted} | "
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
    app = XRepliesScraperGUI(root)
    root.mainloop()