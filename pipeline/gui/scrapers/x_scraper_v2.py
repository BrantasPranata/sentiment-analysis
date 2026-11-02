import csv
import html
import json
import re
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText
import urllib.parse
import feedparser
from googlenewsdecoder import new_decoderv1
import mysql.connector
from mysql.connector import Error
import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "Chrome/122.0.0.0 Safari/537.36"
    )
}

from pathlib import Path
import sys

# Menambahkan root project directory (2 tingkat di atas folder script saat ini) ke sys.path
sys.path.append(str(Path(__file__).resolve().parents[2]))

from config.config import get_db_connection


def ensure_platform_x_exists():
    """Memastikan ID platform X (id=4) siap di database."""
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
        print(f"[!] Gagal memastikan platform X di DB: {e}")


# --- DECODER & EXTRACTION HELPER FOR X (TWITTER) ---

def decode_google_news_url(rss_url: str) -> str:
    """Mendekode link RSS Google News menjadi URL asli x.com."""
    try:
        result = new_decoderv1(rss_url, interval=1)
        if result and result.get("status"):
            return result.get("decoded_url")
    except Exception as e:
        print(f"[!] Decoder error: {e}")

    try:
        response = requests.get(rss_url, headers=HEADERS, allow_redirects=True, timeout=6)
        return response.url
    except Exception as e:
        return rss_url


def parse_x_url(x_url: str):
    """
    Mengekstrak username dan tweet_id dari URL X.com / Twitter.com.
    Return: (username, tweet_id) atau (None, None) jika bukan link tweet spesifik.
    """
    match = re.search(r'(?:x|twitter)\.com/([^/]+)/status/(\d+)', x_url)
    if match:
        return match.group(1), match.group(2)
    return None, None


def scrape_x_media_urls(clean_url: str) -> list:
    """
    Mengambil URL Gambar dari Post X via OpenGraph Meta Tag (og:image).
    """
    media_urls = []
    try:
        # Panggil OEmbed / Meta Scrape dari link tweet
        res = requests.get(clean_url, headers=HEADERS, timeout=8)
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, "html.parser")
            
            # Cari meta tag og:image atau twitter:image
            og_image = (
                soup.find("meta", property="og:image") 
                or soup.find("meta", attrs={"name": "twitter:image"})
                or soup.find("meta", attrs={"name": "og:image"})
            )
            
            if og_image and og_image.get("content"):
                img_url = og_image["content"]
                # Filter agar bukan avatar profil bawaan twitter/x
                if "profile_images" not in img_url and "default_profile" not in img_url:
                    media_urls.append(img_url)
    except Exception as e:
        print(f"[!] Gagal scrape media gambar [{clean_url}]: {e}")

    return media_urls


def fetch_tweet_via_oembed(x_url: str) -> dict:
    """
    Mengambil data Teks & Author menggunakan API OEmbed Resmi dari X.
    """
    username_from_url, tweet_id = parse_x_url(x_url)

    if not tweet_id:
        return {
            "url": x_url,
            "username": username_from_url or "unknown",
            "author": f"@{username_from_url}" if username_from_url else "Unknown",
            "text": "",
            "is_valid": False,
            "error": "Bukan format URL Tweet spesifik"
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
            display_author = f"{author_name} ({default_username})" if author_name else default_username

            return {
                "url": x_url,
                "username": default_username,
                "author": display_author,
                "text": text if text else "Teks tweet tidak tersedia.",
                "is_valid": True,
                "original_id": tweet_id
            }
    except Exception as e:
        print(f"[!] OEmbed API error: {e}")

    return {
        "url": x_url,
        "username": default_username,
        "author": default_username,
        "text": "",
        "is_valid": True,
        "original_id": tweet_id
    }


def check_posts_exist_in_db(posts: list) -> list:
    """Mengecek apakah URL post X sudah tersimpan di database."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        cursor.execute("SELECT clean_url FROM contents")
        existing_urls = {row["clean_url"] for row in cursor.fetchall()}

        cursor.close()
        conn.close()

        for item in posts:
            clean_url = item.get("clean_url") or item.get("rss_url")
            if clean_url in existing_urls:
                item["db_status"] = "Ada di DB"
            else:
                item["db_status"] = "Belum Ada di DB"

    except Exception as e:
        print(f"[!] Gagal mengecek status DB: {e}")
        for item in posts:
            item["db_status"] = "Belum Cek"

    return posts


# --- CRAWLER RSS FEED X (DENGAN FILTER TWEET) ---

def fetch_x_rss_news(keyword: str, limit: int, log_callback=None) -> list:
    """
    Crawl RSS X dari Google News -> Decode -> Filter Hanya Tweet Spesifik.
    """
    query = f"site:x.com {keyword}"
    rss_url = f"https://news.google.com/rss/search?q={urllib.parse.quote(query)}&hl=id&gl=ID&ceid=ID:id"

    feed = feedparser.parse(rss_url)
    results = []

    for entry in feed.entries:
        if len(results) >= limit:
            break

        raw_rss_link = entry.link
        raw_title = entry.title or "Post X"

        # Decode URL Google News ke Clean URL asli
        clean_url = decode_google_news_url(raw_rss_link)

        # Extract & Filter: Cek Apakah URL Benar-benar Tweet
        username, tweet_id = parse_x_url(clean_url)

        if not tweet_id or not username:
            if log_callback:
                log_callback(f"   [DISCARDED NON-TWEET] Skipped: {clean_url[:60]}...")
            continue

        author = f"@{username}"

        clean_title = html.unescape(raw_title)
        clean_title = re.sub(r'\s*-\s*@?\w+\s*$', '', clean_title).strip()

        if log_callback:
            log_callback(f"   [VALID TWEET] Author: {author} | Tweet ID: {tweet_id}")

        results.append({
            "platform_code": "x",
            "author": author,
            "title": clean_title,
            "rss_url": raw_rss_link,
            "clean_url": clean_url,
            "db_status": "Mengecek...",
        })

    return results


# --- GUI APPLICATION ---

class XScraperApp:

    def __init__(self, root):
        self.root = root
        self.root.title("Scraper X (Twitter) - db_sentimenanalisis_v2")
        self.root.geometry("1020x750")

        self.results_data = []

        # Pastikan Platform X (id=4) terdaftar di DB
        ensure_platform_x_exists()

        # Style
        style = ttk.Style()
        style.theme_use("clam")

        # Input Frame
        input_frame = ttk.LabelFrame(root, text=" Pengaturan Pencarian X (Twitter) ", padding=10)
        input_frame.pack(fill="x", padx=10, pady=5)

        ttk.Label(input_frame, text="Kata Kunci:").grid(row=0, column=0, sticky="w", padx=5)
        self.keyword_entry = ttk.Entry(input_frame, width=35)
        self.keyword_entry.grid(row=0, column=1, padx=5, pady=5, sticky="w")

        ttk.Label(input_frame, text="Batas Maksimal Tweet:").grid(row=0, column=2, sticky="w", padx=5)
        self.limit_spinbox = ttk.Spinbox(input_frame, from_=1, to=100, width=5)
        self.limit_spinbox.set(10)
        self.limit_spinbox.grid(row=0, column=3, padx=5, pady=5, sticky="w")

        # Action Buttons
        btn_frame = ttk.Frame(root, padding=5)
        btn_frame.pack(fill="x", padx=10)

        self.search_btn = ttk.Button(btn_frame, text="1. Cari Post X (Filtered Valid Tweet)", command=self.start_search_thread)
        self.search_btn.pack(side="left", padx=5)

        self.db_btn = ttk.Button(
            btn_frame,
            text="2. Fetch Text, Gambar & Simpan ke DB",
            command=self.start_save_thread,
            state="disabled",
        )
        self.db_btn.pack(side="left", padx=5)

        self.export_btn = ttk.Button(
            btn_frame,
            text="Export ke CSV",
            command=self.export_to_csv,
            state="disabled",
        )
        self.export_btn.pack(side="left", padx=5)

        self.status_label = ttk.Label(btn_frame, text="Siap", font=("Arial", 9, "italic"))
        self.status_label.pack(side="right", padx=5)

        # Display Table (Treeview)
        table_frame = ttk.Frame(root, padding=10)
        table_frame.pack(fill="both", expand=True)

        columns = ("No", "Author", "Title", "DBStatus", "ProcessStatus")
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="browse")

        self.tree.heading("No", text="No")
        self.tree.heading("Author", text="Penulis / Author")
        self.tree.heading("Title", text="Judul / Snippet Tweet")
        self.tree.heading("DBStatus", text="Status di DB")
        self.tree.heading("ProcessStatus", text="Status Validasi / Proses")

        self.tree.column("No", width=40, anchor="center")
        self.tree.column("Author", width=180, anchor="w")
        self.tree.column("Title", width=420)
        self.tree.column("DBStatus", width=120, anchor="center")
        self.tree.column("ProcessStatus", width=180, anchor="center")

        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscroll=scrollbar.set)

        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        # Live Console Log Panel
        log_frame = ttk.LabelFrame(root, text=" Live Console / Log ", padding=5)
        log_frame.pack(fill="x", padx=10, pady=5)

        self.log_console = ScrolledText(log_frame, height=8, font=("Consolas", 8))
        self.log_console.pack(fill="both", expand=True)

    def append_log(self, text):
        """Menampilkan log ke console GUI secara real-time."""
        self.log_console.insert(tk.END, text + "\n")
        self.log_console.see(tk.END)

    def start_search_thread(self):
        keyword = self.keyword_entry.get().strip()
        if not keyword:
            messagebox.showwarning("Peringatan", "Silakan masukkan kata kunci pencarian.")
            return

        self.search_btn.config(state="disabled")
        self.db_btn.config(state="disabled")
        self.export_btn.config(state="disabled")
        self.status_label.config(text="Mencari & menyaring tweet valid...")

        self.log_console.delete("1.0", tk.END)
        self.append_log(f"[*] Memulai pencarian & filtering Tweet X untuk kata kunci: '{keyword}'...")

        for item in self.tree.get_children():
            self.tree.delete(item)

        threading.Thread(target=self.perform_search, args=(keyword,), daemon=True).start()

    def perform_search(self, keyword):
        try:
            limit = int(self.limit_spinbox.get())
        except ValueError:
            limit = 10

        self.results_data = []

        self.results_data.extend(
            fetch_x_rss_news(keyword, limit, log_callback=lambda txt: self.root.after(0, lambda: self.append_log(txt)))
        )

        self.append_log("[*] Memeriksa status ketersediaan tweet di MySQL...")
        self.results_data = check_posts_exist_in_db(self.results_data)

        self.root.after(0, self.update_table)

    def update_table(self):
        if self.results_data:
            exist_count = sum(1 for item in self.results_data if item["db_status"] == "Ada di DB")
            new_count = len(self.results_data) - exist_count

            for idx, item in enumerate(self.results_data, start=1):
                self.tree.insert(
                    "",
                    "end",
                    iid=str(idx - 1),
                    values=(
                        idx,
                        item["author"],
                        item["title"],
                        item["db_status"],
                        "Valid Tweet",
                    ),
                )

            self.status_label.config(
                text=f"Total Tweet Valid: {len(self.results_data)} | Baru: {new_count} | Ada di DB: {exist_count}"
            )
            self.append_log(
                f"[+] Selesai! Ditemukan {len(self.results_data)} tweet valid "
                f"({new_count} belum ada di DB, {exist_count} sudah ada di DB)."
            )
            self.db_btn.config(state="normal")
            self.export_btn.config(state="normal")
        else:
            self.status_label.config(text="Tweet valid tidak ditemukan.")
            self.append_log("[-] Tidak ada tweet valid ditemukan.")
            messagebox.showinfo("Informasi", "Tidak ada tweet valid ditemukan.")

        self.search_btn.config(state="normal")

    def start_save_thread(self):
        self.db_btn.config(state="disabled")
        self.search_btn.config(state="disabled")
        self.status_label.config(text="Memproses Teks, Gambar & Menyimpan...")

        threading.Thread(target=self.perform_save_to_db, daemon=True).start()

    def update_tree_row(self, row_idx, author=None, db_status=None, process_status=None):
        """Update kolom Author dan Status di tabel GUI secara live."""
        current_vals = list(self.tree.item(str(row_idx), "values"))
        if author:
            current_vals[1] = author
        if db_status:
            current_vals[3] = db_status
        if process_status:
            current_vals[4] = process_status
        self.tree.item(str(row_idx), values=current_vals)

    def perform_save_to_db(self):
        inserted_count = 0
        skipped_count = 0

        items_to_process = [
            (idx, item) for idx, item in enumerate(self.results_data)
            if item.get("db_status") == "Belum Ada di DB"
        ]

        total = len(items_to_process)

        if total == 0:
            self.root.after(0, lambda: self.append_log("\n[i] Semua tweet valid dalam list sudah ada di Database."))
            self.root.after(0, lambda: messagebox.showinfo("Informasi", "Semua tweet sudah ada di Database."))
            self.root.after(0, lambda: self.db_btn.config(state="normal"))
            self.root.after(0, lambda: self.search_btn.config(state="normal"))
            return

        self.root.after(0, lambda: self.append_log(f"\n[*] Memproses {total} tweet baru ke Database..."))

        try:
            conn = get_db_connection()
            cursor = conn.cursor(dictionary=True)

            p_id = 4  # Platform ID untuk X

            for step, (idx, item) in enumerate(items_to_process, start=1):
                clean_url = item["clean_url"]
                username_from_url, tweet_id = parse_x_url(clean_url)

                # 1. Fetch Teks via OEmbed
                self.root.after(0, lambda i=idx: self.update_tree_row(i, process_status="1/3 Fetching Text..."))
                post_data = fetch_tweet_via_oembed(clean_url)

                actual_text = post_data["text"]
                username = post_data.get("username", f"@{username_from_url}")
                author_full = post_data["author"]

                # 2. Scrape Gambar via OpenGraph
                self.root.after(0, lambda i=idx: self.update_tree_row(i, process_status="2/3 Scraping Gambar..."))
                media_urls = scrape_x_media_urls(clean_url)

                # FORMAT DB: @username - actual tweet
                if actual_text and actual_text != "Teks tweet tidak tersedia.":
                    formatted_raw_text = f"{username} - {actual_text}"
                else:
                    formatted_raw_text = f"{username} - {item['title']}"

                title = f"Tweet oleh {author_full}: {actual_text[:60] if actual_text else item['title'][:60]}..."
                original_id = post_data.get("original_id") or tweet_id

                item["author"] = author_full

                # 3. Simpan ke DB contents
                self.root.after(0, lambda i=idx, a=author_full: self.update_tree_row(i, author=a, process_status="3/3 Menyimpan ke DB..."))
                media_urls_json = json.dumps(media_urls) if media_urls else None

                query = """
                    INSERT IGNORE INTO contents 
                    (platform_id, clean_url, original_id, title, raw_text, media_urls)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """
                cursor.execute(
                    query,
                    (p_id, clean_url, original_id, title, formatted_raw_text, media_urls_json),
                )

                if cursor.rowcount > 0:
                    inserted_count += 1
                    content_id = cursor.lastrowid
                    item["db_status"] = "Ada di DB"

                    cursor.execute(
                        """
                        INSERT IGNORE INTO sentiment_results 
                        (target_type, target_id, execution_source, status)
                        VALUES ('content', %s, 'ml_pipeline', 'pending')
                    """,
                        (content_id,),
                    )
                    conn.commit()
                    self.root.after(0, lambda i=idx, a=author_full: self.update_tree_row(i, author=a, db_status="Ada di DB", process_status="SUKSES"))
                    self.root.after(0, lambda u=clean_url, a=author_full, m=len(media_urls): self.append_log(f"   [OK] [{a}] ({m} Gambar) Saved: {u}"))
                else:
                    skipped_count += 1
                    item["db_status"] = "Ada di DB"
                    self.root.after(0, lambda i=idx, a=author_full: self.update_tree_row(i, author=a, db_status="Ada di DB", process_status="SKIPPED"))
                    self.root.after(0, lambda: self.append_log("   [SKIP] Tweet ini sudah ada di database."))

            cursor.close()
            conn.close()

            self.root.after(0, lambda: self.on_save_finished(inserted_count, skipped_count))

        except Exception as e:
            self.root.after(0, lambda err=e: self.append_log(f"\n[ERROR FATAL] {err}"))
            self.root.after(0, lambda err=e: messagebox.showerror("Database Error", f"Gagal memproses:\n{err}"))
            self.root.after(0, lambda: self.db_btn.config(state="normal"))
            self.root.after(0, lambda: self.search_btn.config(state="normal"))

    def on_save_finished(self, inserted, skipped):
        msg = f"Selesai! {inserted} tweet berhasil disimpan, {skipped} di-skip/duplikat."
        self.status_label.config(text="Selesai memproses.")
        self.append_log("\n==========================================")
        self.append_log(f"[+] {msg}")
        messagebox.showinfo("Proses Selesai", msg)
        self.search_btn.config(state="normal")
        self.db_btn.config(state="normal")

    def export_to_csv(self):
        if not self.results_data:
            return

        keyword = re.sub(r"\W+", "_", self.keyword_entry.get().strip().lower())
        default_filename = f"x_tweets_{keyword}.csv"

        filepath = filedialog.asksaveasfilename(
            defaultextension=".csv",
            initialfile=default_filename,
            filetypes=[("CSV Files", "*.csv"), ("All Files", "*.*")],
        )

        if filepath:
            try:
                fieldnames = ["author", "title", "db_status", "rss_url", "clean_url"]
                with open(filepath, mode="w", newline="", encoding="utf-8") as file:
                    writer = csv.DictWriter(file, fieldnames=fieldnames, extrasaction='ignore')
                    writer.writeheader()
                    writer.writerows(self.results_data)

                messagebox.showinfo("Berhasil", f"Data berhasil disimpan di:\n{filepath}")
            except Exception as e:
                messagebox.showerror("Error", f"Gagal menyimpan file CSV: {e}")


if __name__ == "__main__":
    root = tk.Tk()
    app = XScraperApp(root)
    root.mainloop()