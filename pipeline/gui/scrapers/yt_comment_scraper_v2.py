import logging
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from yt_dlp import YoutubeDL

# Fix Path import config/Config
sys.path.append(str(Path(__file__).resolve().parents[2]))
from config.config import get_db_connection

# Logging Setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

# True  -> ikut mengambil balasan (reply) komentar
# False -> hanya komentar tingkat atas (lebih cepat)
INCLUDE_REPLIES = False

# Batas atas jumlah komentar yang diambil dari YouTube per video dalam satu kali crawl.
# Jumlah yang diambil = (komentar yang sudah ada di DB untuk video itu) + (batas komentar baru),
# dibatasi oleh angka ini supaya video dengan ribuan komentar tidak membuat crawl terlalu lama.
MAX_FETCH_CAP = 2000


class YoutubeCommentsScraperGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("YouTube Comments Scraper - db_sentimenanalisis_v2")
        self.root.geometry("850x650")

        self.contents_map = {}  # Map Kombinasi String -> (content_id, original_id, clean_url)

        self.create_widgets()
        self.load_youtube_contents_from_db()

    def create_widgets(self):
        # Frame Target Selection
        frame_target = ttk.LabelFrame(self.root, text=" Target Video YouTube dari Database ")
        frame_target.pack(fill="x", padx=10, pady=5)

        ttk.Label(frame_target, text="Pilih Video:").grid(row=0, column=0, padx=5, pady=5, sticky="w")

        self.cb_video = ttk.Combobox(frame_target, state="readonly", width=80)
        self.cb_video.grid(row=0, column=1, padx=5, pady=5, sticky="ew")

        btn_refresh = ttk.Button(frame_target, text="🔄 Refresh DB", command=self.load_youtube_contents_from_db)
        btn_refresh.grid(row=0, column=2, padx=5, pady=5)

        # Frame Settings
        frame_settings = ttk.LabelFrame(self.root, text=" Pengaturan Crawling ")
        frame_settings.pack(fill="x", padx=10, pady=5)

        ttk.Label(frame_settings, text="Batas Komentar Baru per Video:").grid(row=0, column=0, padx=5, pady=5, sticky="w")
        self.ent_limit = ttk.Entry(frame_settings, width=15)
        self.ent_limit.insert(0, "50")
        self.ent_limit.grid(row=0, column=1, padx=5, pady=5, sticky="w")

        # Buttons
        frame_buttons = ttk.Frame(self.root)
        frame_buttons.pack(fill="x", padx=10, pady=5)

        self.btn_start = ttk.Button(
            frame_buttons,
            text="▶️ Crawl Video Terpilih",
            command=lambda: self.start_crawling_thread(process_all=False),
        )
        self.btn_start.pack(side="left", padx=5)

        self.btn_start_all = ttk.Button(
            frame_buttons,
            text="⏩ Crawl SEMUA Video YouTube di DB",
            command=lambda: self.start_crawling_thread(process_all=True),
        )
        self.btn_start_all.pack(side="left", padx=5)

        # Log Area
        frame_log = ttk.LabelFrame(self.root, text=" Log Aktivitas ")
        frame_log.pack(fill="both", expand=True, padx=10, pady=5)

        self.txt_log = ScrolledText(frame_log, state="disabled", height=20)
        self.txt_log.pack(fill="both", expand=True, padx=5, pady=5)

    def log(self, message):
        """Menampilkan teks log di ScrolledText GUI."""
        self.txt_log.config(state="normal")
        self.txt_log.insert(tk.END, message + "\n")
        self.txt_log.see(tk.END)
        self.txt_log.config(state="disabled")

    def load_youtube_contents_from_db(self):
        """Memuat daftar konten YouTube (platform_id = 5) dari tabel contents."""
        self.cb_video["values"] = []
        self.contents_map.clear()

        conn = None
        cursor = None
        try:
            conn = get_db_connection()
            cursor = conn.cursor(dictionary=True)

            query = """
                SELECT id, original_id, clean_url, title
                FROM contents
                WHERE platform_id = 5
                ORDER BY id DESC
            """
            cursor.execute(query)
            rows = cursor.fetchall()

            if not rows:
                self.log("[!] Tidak ada video YouTube (platform_id = 5) di tabel contents.")
                return

            combo_values = []
            for row in rows:
                c_id = row["id"]
                title = row["title"][:60] if row["title"] else row["clean_url"]
                display_str = f"[ID: {c_id}] {title}"

                combo_values.append(display_str)
                self.contents_map[display_str] = (c_id, row["original_id"], row["clean_url"])

            self.cb_video["values"] = combo_values
            if combo_values:
                self.cb_video.current(0)

            self.log(f"[+] Berhasil memuat {len(rows)} video YouTube dari database.")

        except Exception as e:
            self.log(f"[!] Error koneksi database: {e}")

        finally:
            if cursor:
                cursor.close()
            if conn:
                conn.close()

    def extract_youtube_url_or_id(self, original_id, clean_url):
        """Mendapatkan Video URL atau Video ID yang valid."""
        if original_id and len(original_id) == 11:
            return f"https://www.youtube.com/watch?v={original_id}"
        return clean_url

    def start_crawling_thread(self, process_all=False):
        """Menjalankan proses crawling di thread terpisah agar GUI lancar."""
        try:
            limit = int(self.ent_limit.get().strip())
        except ValueError:
            messagebox.showerror("Error", "Batas komentar harus berupa angka!")
            return

        if limit <= 0:
            messagebox.showerror("Error", "Batas komentar harus lebih besar dari 0!")
            return

        self.btn_start.config(state="disabled")
        self.btn_start_all.config(state="disabled")

        t = threading.Thread(
            target=self.run_comments_crawling,
            args=(limit, process_all),
            daemon=True,
        )
        t.start()

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

    def count_existing_comments(self, cursor, content_id):
        """Jumlah komentar video ini yang sudah tersimpan di DB."""
        cursor.execute(
            "SELECT COUNT(*) AS n FROM comments WHERE content_id = %s",
            (content_id,),
        )
        row = cursor.fetchone()
        return int(row["n"]) if row else 0

    def fetch_comments(self, yt_url, max_fetch):
        """Ambil komentar (terbaru dulu) dari YouTube memakai yt-dlp. Mengembalikan list dict."""
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
            "extractor_args": {
                "youtube": {
                    "max_comments": max_comments,
                    "comment_sort": ["new"],
                }
            },
        }
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(yt_url, download=False)

        return (info or {}).get("comments") or []

    def run_comments_crawling(self, limit, process_all):
        """Fungsi eksekutor crawling komentar YouTube."""
        conn = None
        cursor = None

        try:
            conn = get_db_connection()
            cursor = conn.cursor(dictionary=True)

            # Menentukan daftar video yang akan di-crawl
            targets = []
            if process_all:
                for display_str, info in self.contents_map.items():
                    targets.append(info)
            else:
                selected_str = self.cb_video.get()
                if selected_str in self.contents_map:
                    targets.append(self.contents_map[selected_str])

            if not targets:
                self.log("[!] Tidak ada target video terpilih.")
                return

            total_inserted_comments = 0
            total_skipped_comments = 0

            for c_id, orig_id, clean_url in targets:
                yt_url = self.extract_youtube_url_or_id(orig_id, clean_url)
                self.log(f"\n[*] Crawling Komentar [Content ID: {c_id}] -> {yt_url}")

                try:
                    # Ambil sebanyak (yang sudah ada + batas baru) supaya setelah
                    # duplikat dilewati masih tersisa cukup komentar baru.
                    existing = self.count_existing_comments(cursor, c_id)
                    max_fetch = min(existing + limit, MAX_FETCH_CAP)

                    comments = self.fetch_comments(yt_url, max_fetch)
                    self.log(f"   [i] {len(comments)} komentar diambil dari YouTube (target maks {max_fetch}).")

                    if not comments:
                        self.log("   [!] Tidak ada komentar dari YouTube (komentar dimatikan, video privat, atau diblokir).")
                        continue

                    count_for_video = 0
                    skipped_for_video = 0

                    for comment in comments:
                        if count_for_video >= limit:
                            break

                        comment_text = (comment.get("text") or "").strip()
                        author = (comment.get("author") or "Unknown")[:150]

                        if not comment_text:
                            continue

                        # Lewati kalau content_id + author + isi komentar sudah ada
                        if self.comment_exists(cursor, c_id, author, comment_text):
                            skipped_for_video += 1
                            total_skipped_comments += 1
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
                        count_for_video += 1
                        total_inserted_comments += 1

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
                        f"   [SUCCESS] +{count_for_video} komentar baru, "
                        f"{skipped_for_video} duplikat dilewati."
                    )

                except Exception as c_err:
                    self.log(f"   [!] Error crawling komentar untuk ID {c_id}: {c_err}")

            self.log("\n" + "=" * 50)
            self.log(
                f"[*] CRAWLING SELESAI! Komentar Baru: {total_inserted_comments} | "
                f"Duplikat Dilewati: {total_skipped_comments}"
            )
            self.log("=" * 50)

        except Exception as err:
            self.log(f"[!] Database Error: {err}")

        finally:
            if cursor:
                cursor.close()
            if conn:
                conn.close()
            self.btn_start.config(state="normal")
            self.btn_start_all.config(state="normal")


if __name__ == "__main__":
    root = tk.Tk()
    app = YoutubeCommentsScraperGUI(root)
    root.mainloop()