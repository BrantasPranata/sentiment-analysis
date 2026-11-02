import csv
import json
import os
import re
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText
import urllib.parse

# 1. Matikan Warning Symlinks Hugging Face di Windows
os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
# HF_TOKEN (opsional) dibaca dari environment / file .env, tidak ditulis di kode.

# 2. Auto-inject FFmpeg & FFprobe path secara otomatis
try:
    import static_ffmpeg
    static_ffmpeg.add_paths()
except Exception as e:
    print(f"[!] Warning static-ffmpeg: {e}")

import mysql.connector
from mysql.connector import Error
import scrapetube
from youtube_transcript_api import YouTubeTranscriptApi
import yt_dlp
from pathlib import Path
import sys

# Menambahkan root project directory (2 tingkat di atas folder script saat ini) ke sys.path
sys.path.append(str(Path(__file__).resolve().parents[2]))

from config.config import get_db_connection

# Lazy loading untuk Transformers dan Faster-Whisper
whisper_model = None
summarizer_pipeline = None




def ensure_platform_youtube_exists():
    """Memastikan ID platform YouTube (id=5) siap di database tanpa menabrak constraint UNIQUE."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute("""
            INSERT IGNORE INTO platforms (id, code, name, rss_url)
            VALUES (5, 'youtube', 'YouTube', 'https://youtube.com')
        """)

        conn.commit()
        cursor.close()
        conn.close()
    except Error as e:
        print(f"[!] Gagal memastikan platform YouTube di DB: {e}")


# --- HELPER WHISPER & SUMMARIZER BAHASA INDONESIA ---

def get_whisper_model():
    global whisper_model
    if whisper_model is None:
        from faster_whisper import WhisperModel
        whisper_model = WhisperModel("small", device="cpu", compute_type="int8")
    return whisper_model


def get_summarizer():
    global summarizer_pipeline
    if summarizer_pipeline is None:
        from transformers import AutoTokenizer, AutoModelForSeq2SeqLM

        # Model T5 Khusus Peringkasan Teks Bahasa Indonesia
        model_name = "cahya/t5-base-indonesian-summarization"
        tokenizer = AutoTokenizer.from_pretrained(model_name, token=os.environ.get("HF_TOKEN"))
        model = AutoModelForSeq2SeqLM.from_pretrained(model_name, token=os.environ.get("HF_TOKEN"))

        class IndoSummarizer:
            def __init__(self, model, tokenizer):
                self.model = model
                self.tokenizer = tokenizer

            def summarize(self, text, max_length=150, min_length=40):
                # T5 memerlukan prefix prompt 'ringkas: ' untuk task summarization
                input_text = "ringkas: " + text
                inputs = self.tokenizer([input_text], max_length=1024, truncation=True, return_tensors="pt")
                
                summary_ids = self.model.generate(
                    inputs["input_ids"], 
                    num_beams=4, 
                    max_length=max_length, 
                    min_length=min_length, 
                    early_stopping=True
                )
                return self.tokenizer.decode(summary_ids[0], skip_special_tokens=True)

        summarizer_pipeline = IndoSummarizer(model, tokenizer)

    return summarizer_pipeline


def parse_duration_to_seconds(duration_str: str) -> int:
    """Mengubah format durasi "MM:SS" atau "HH:MM:SS" menjadi total detik."""
    if not duration_str or duration_str == "N/A":
        return 0
    parts = list(map(int, duration_str.split(":")))
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    elif len(parts) == 2:
        return parts[0] * 60 + parts[1]
    return 0


def download_youtube_audio(video_url: str, output_path: str = "temp_audio") -> str:
    """Mengunduh audio dari YouTube menggunakan yt-dlp."""
    os.makedirs(output_path, exist_ok=True)
    ydl_opts = {
        'format': 'm4a/bestaudio/best',
        'outtmpl': os.path.join(output_path, '%(id)s.%(ext)s'),
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'm4a',
        }],
        'quiet': True,
        'no_warnings': True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(video_url, download=True)
        filename = ydl.prepare_filename(info)
        base, _ = os.path.splitext(filename)
        audio_file = f"{base}.m4a"
        if os.path.exists(audio_file):
            return audio_file
        return filename


def transcribe_video(video_id: str, video_url: str, duration_sec: int, video_title: str, video_description: str, log_func=print) -> str:
    """
    ALUR SOLUSI 1 (Bahas Indonesia):
    1. Durasi > 10m (>600s):
       - SKIP WHISPER TOTAL.
       - Ambil Subtitle YouTube (jika ada).
       - Jika Subtitle TIDAK ADA -> Fallback langsung ke (Judul + Deskripsi Video).
       - Ringkas bahan teks menggunakan HuggingFace T5 Indo Summarizer.
    2. Durasi <= 10m (<=600s):
       - Cek Subtitle YouTube.
       - Jika tidak ada -> Unduh Audio & Jalankan Faster-Whisper.
    """

    # --- KONDISI 1: VIDEO PANJANG (> 10 MENIT) ---
    if duration_sec > 600:
        log_func("   [!] DURASI > 10 MENIT: Mengabaikan audio Whisper (Ringkasan Otomatis Bahasa Indonesia)...")

        source_text = ""
        try:
            log_func("   [*] Mengecek ketersediaan subtitle/caption resmi...")
            transcript_list = YouTubeTranscriptApi.get_transcript(video_id, languages=['id', 'en'])
            source_text = " ".join([item['text'] for item in transcript_list])
            log_func("   [*] Subtitle ditemukan! Menggunakan subtitle sebagai bahan ringkasan.")
        except Exception:
            log_func("   [*] Subtitle tidak ada. Menggunakan Judul & Deskripsi Video untuk bahan Summarizer...")
            source_text = f"Judul Video: {video_title}. Deskripsi: {video_description}"

        if not source_text or len(source_text.strip()) < 20:
            return f"[DESKRIPSI VIDEO]: {video_description}"

        log_func("   [*] Menjalankan Indo-T5 Summarizer (Bahasa Indonesia)...")
        try:
            summarizer = get_summarizer()
            truncated_text = source_text[:3000]
            summary_text = summarizer.summarize(truncated_text, max_length=150, min_length=30)
            return f"[RINGKASAN VIDEO > 10 MIN]: {summary_text}"
        except Exception as e:
            log_func(f"   [!] Gagal membuat ringkasan: {e}")
            return f"[DESKRIPSI VIDEO]: {video_description}"

    # --- KONDISI 2: VIDEO PENDEK (<= 10 MENIT) ---
    log_func("   [*] DURASI <= 10 MENIT: Memproses Transkripsi Audio Lengkap...")
    full_text = ""

    # Step A: Subtitle Resmi
    try:
        log_func("   [1/2] Mengecek subtitle bawaan YouTube...")
        transcript_list = YouTubeTranscriptApi.get_transcript(video_id, languages=['id', 'en'])
        full_text = " ".join([item['text'] for item in transcript_list])
        log_func("   [1/2] Subtitle bawaan ditemukan!")
    except Exception:
        log_func("   [1/2] Subtitle tidak tersedia. Menjalankan Faster-Whisper...")
        audio_file = None
        try:
            # Step B: Faster-Whisper
            audio_file = download_youtube_audio(video_url)
            model = get_whisper_model()
            segments, _ = model.transcribe(audio_file, beam_size=5, language="id")
            full_text = " ".join([segment.text for segment in segments])
        except Exception as e:
            log_func(f"   [!] Gagal transkripsi audio: {e}")
        finally:
            if audio_file and os.path.exists(audio_file):
                try:
                    os.remove(audio_file)
                except Exception:
                    pass

    if not full_text.strip():
        return f"[DESKRIPSI VIDEO]: {video_description}"

    return f"[TRANSKRIP LENGKAP]: {full_text}"


# --- GUI APPLICATION ---

class YouTubeTableApp:

    def __init__(self, root):
        self.root = root
        self.root.title("YouTube Scraper & Processor - db_sentimenanalisis_v2")
        self.root.geometry("1100x780")
        self.root.minsize(800, 500)

        self.results_data = []

        # Pastikan Platform YouTube (id=5) terdaftar di DB
        ensure_platform_youtube_exists()

        # Style
        style = ttk.Style()
        style.theme_use("clam")

        main_frame = ttk.Frame(root, padding="15")
        main_frame.pack(fill=tk.BOTH, expand=True)

        # --- Search Box Header ---
        search_frame = ttk.LabelFrame(main_frame, text=" Pengaturan Pencarian YouTube ", padding=10)
        search_frame.pack(fill=tk.X, pady=(0, 10))

        ttk.Label(search_frame, text="Kata Kunci:", font=("Segoe UI", 10, "bold")).grid(row=0, column=0, sticky="w", padx=5)

        self.keyword_entry = ttk.Entry(search_frame, font=("Segoe UI", 10), width=35)
        self.keyword_entry.grid(row=0, column=1, padx=5, pady=5, sticky="w")
        self.keyword_entry.focus()
        self.keyword_entry.bind("<Return>", lambda event: self.start_search_thread())

        ttk.Label(search_frame, text="Hasil Maksimal:").grid(row=0, column=2, sticky="w", padx=5)
        self.limit_spinbox = ttk.Spinbox(search_frame, from_=1, to=50, width=5)
        self.limit_spinbox.set(10)
        self.limit_spinbox.grid(row=0, column=3, padx=5, pady=5, sticky="w")

        # Action Buttons
        btn_frame = ttk.Frame(main_frame, padding=5)
        btn_frame.pack(fill=tk.X, pady=(0, 10))

        self.search_btn = ttk.Button(btn_frame, text="1. Cari Video", command=self.start_search_thread)
        self.search_btn.pack(side=tk.LEFT, padx=5)

        self.transcribe_btn = ttk.Button(
            btn_frame,
            text="2. Transkrip (<=10m) / Ringkas (>10m)",
            command=self.start_transcribe_thread,
            state="disabled",
        )
        self.transcribe_btn.pack(side=tk.LEFT, padx=5)

        self.db_btn = ttk.Button(
            btn_frame,
            text="3. Simpan ke Database (DB)",
            command=self.start_save_thread,
            state="disabled",
        )
        self.db_btn.pack(side=tk.LEFT, padx=5)

        self.export_btn = ttk.Button(
            btn_frame, text="Export CSV", command=self.export_to_csv, state="disabled"
        )
        self.export_btn.pack(side=tk.LEFT, padx=5)

        # --- Table View (Treeview) ---
        table_frame = ttk.Frame(main_frame)
        table_frame.pack(fill=tk.BOTH, expand=True)

        columns = ("no", "title", "url", "upload_date", "duration", "transcript_status", "db_status")
        self.tree = ttk.Treeview(
            table_frame, columns=columns, show="headings", selectmode="browse"
        )

        self.tree.heading("no", text="No")
        self.tree.heading("title", text="Judul Video")
        self.tree.heading("url", text="URL Video")
        self.tree.heading("upload_date", text="Diunggah")
        self.tree.heading("duration", text="Durasi")
        self.tree.heading("transcript_status", text="Status Proses")
        self.tree.heading("db_status", text="Status DB")

        self.tree.column("no", width=40, anchor=tk.CENTER)
        self.tree.column("title", width=280, anchor=tk.W)
        self.tree.column("url", width=200, anchor=tk.W)
        self.tree.column("upload_date", width=100, anchor=tk.CENTER)
        self.tree.column("duration", width=70, anchor=tk.CENTER)
        self.tree.column("transcript_status", width=160, anchor=tk.CENTER)
        self.tree.column("db_status", width=110, anchor=tk.CENTER)

        v_scrollbar = ttk.Scrollbar(table_frame, orient=tk.VERTICAL, command=self.tree.yview)
        h_scrollbar = ttk.Scrollbar(table_frame, orient=tk.HORIZONTAL, command=self.tree.xview)
        self.tree.configure(yscrollcommand=v_scrollbar.set, xscrollcommand=h_scrollbar.set)

        self.tree.grid(row=0, column=0, sticky="nsew")
        v_scrollbar.grid(row=0, column=1, sticky="ns")
        h_scrollbar.grid(row=1, column=0, sticky="ew")

        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)

        # Log Panel
        log_frame = ttk.LabelFrame(main_frame, text=" Live Console / Log ", padding=5)
        log_frame.pack(fill=tk.X, pady=(10, 0))

        self.log_console = ScrolledText(log_frame, height=6, font=("Consolas", 8))
        self.log_console.pack(fill=tk.BOTH, expand=True)

        # Status Bar
        self.status_var = tk.StringVar(value="Siap.")
        status_bar = ttk.Label(main_frame, textvariable=self.status_var, relief=tk.SUNKEN, anchor=tk.W, padding=(5, 3))
        status_bar.pack(fill=tk.X, pady=(5, 0))

    def append_log(self, text):
        self.log_console.insert(tk.END, text + "\n")
        self.log_console.see(tk.END)

    def start_search_thread(self):
        keyword = self.keyword_entry.get().strip()
        if not keyword:
            messagebox.showwarning("Peringatan", "Masukkan kata kunci pencarian.")
            return

        self.search_btn.config(state="disabled")
        self.transcribe_btn.config(state="disabled")
        self.db_btn.config(state="disabled")
        self.export_btn.config(state="disabled")
        self.status_var.set(f"Mencari video untuk '{keyword}'...")

        for item in self.tree.get_children():
            self.tree.delete(item)

        threading.Thread(target=self.perform_search, args=(keyword,), daemon=True).start()

    def perform_search(self, keyword):
        try:
            limit = int(self.limit_spinbox.get())
        except ValueError:
            limit = 10

        self.results_data = []
        self.root.after(0, lambda: self.append_log(f"[*] Memulai pencarian YouTube untuk: '{keyword}'..."))

        try:
            videos = list(scrapetube.get_search(query=keyword, limit=limit, results_type="video"))

            if not videos:
                self.root.after(0, lambda: self.status_var.set("Video tidak ditemukan."))
                self.root.after(0, lambda: self.search_btn.config(state="normal"))
                return

            conn = get_db_connection()
            cursor = conn.cursor(dictionary=True)
            cursor.execute("SELECT clean_url FROM contents WHERE platform_id = 5")
            existing_urls = {row["clean_url"] for row in cursor.fetchall()}
            cursor.close()
            conn.close()

            for video in videos:
                video_id = video.get("videoId", "")
                url = f"https://www.youtube.com/watch?v={video_id}"
                thumbnail_url = f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg"

                title = "N/A"
                if "title" in video and "runs" in video["title"]:
                    title = video["title"]["runs"][0].get("text", "N/A")

                duration = video.get("lengthText", {}).get("simpleText", "N/A")
                upload_date = video.get("publishedTimeText", {}).get("simpleText", "N/A")

                description = "Tidak ada deskripsi."
                if "detailedMetadataSnippets" in video and video["detailedMetadataSnippets"]:
                    snippets = video["detailedMetadataSnippets"][0].get("snippetText", {}).get("runs", [])
                    description = "".join([s.get("text", "") for s in snippets]).replace("\n", " ")

                db_status = "Ada di DB" if url in existing_urls else "Belum Ada di DB"

                self.results_data.append({
                    "video_id": video_id,
                    "title": title,
                    "url": url,
                    "thumbnail_url": thumbnail_url,
                    "upload_date": upload_date,
                    "duration": duration,
                    "description": description,
                    "transcript": "",
                    "transcribe_status": "Belum Diproses",
                    "db_status": db_status
                })

            self.root.after(0, self.update_table)

        except Exception as e:
            self.root.after(0, lambda: messagebox.showerror("Error", f"Gagal mencari: {e}"))
            self.root.after(0, lambda: self.search_btn.config(state="normal"))

    def update_table(self):
        for idx, item in enumerate(self.results_data, start=1):
            self.tree.insert(
                "",
                tk.END,
                iid=str(idx - 1),
                values=(
                    idx,
                    item["title"],
                    item["url"],
                    item["upload_date"],
                    item["duration"],
                    item["transcribe_status"],
                    item["db_status"]
                ),
            )

        self.status_var.set(f"Ditemukan {len(self.results_data)} video.")
        self.append_log(f"[+] Pencarian selesai. Ditemukan {len(self.results_data)} video.")
        self.search_btn.config(state="normal")
        self.transcribe_btn.config(state="normal")
        self.db_btn.config(state="normal")
        self.export_btn.config(state="normal")

    def start_transcribe_thread(self):
        self.transcribe_btn.config(state="disabled")
        self.search_btn.config(state="disabled")
        self.status_var.set("Memproses transkripsi / ringkasan video...")

        threading.Thread(target=self.perform_transcription, daemon=True).start()

    def update_tree_row(self, row_idx, transcribe_status=None, db_status=None):
        current_vals = list(self.tree.item(str(row_idx), "values"))
        if transcribe_status:
            current_vals[5] = transcribe_status
        if db_status:
            current_vals[6] = db_status
        self.tree.item(str(row_idx), values=current_vals)

    def perform_transcription(self):
        for idx, item in enumerate(self.results_data):
            self.root.after(0, lambda i=idx: self.update_tree_row(i, transcribe_status="Memproses..."))
            self.root.after(0, lambda t=item['title'], d=item['duration']: self.append_log(f"\n[*] Memproses Video [{d}]: {t}"))

            duration_sec = parse_duration_to_seconds(item["duration"])
            transcript_text = transcribe_video(
                item["video_id"],
                item["url"],
                duration_sec,
                item["title"],
                item["description"],
                log_func=lambda msg: self.root.after(0, lambda: self.append_log(msg))
            )

            item["transcript"] = transcript_text
            status_label = "Summarized (>10m)" if duration_sec > 600 else "Transcribed"
            item["transcribe_status"] = status_label
            self.root.after(0, lambda i=idx, s=status_label: self.update_tree_row(i, transcribe_status=s))

        self.root.after(0, lambda: self.status_var.set("Proses transkripsi / ringkasan selesai."))
        self.root.after(0, lambda: self.transcribe_btn.config(state="normal"))
        self.root.after(0, lambda: self.search_btn.config(state="normal"))

    def start_save_thread(self):
        self.db_btn.config(state="disabled")
        self.search_btn.config(state="disabled")
        self.status_var.set("Menyimpan video ke Database MySQL...")

        threading.Thread(target=self.perform_save_to_db, daemon=True).start()

    def perform_save_to_db(self):
        inserted_count = 0
        skipped_count = 0

        items_to_process = [
            (idx, item) for idx, item in enumerate(self.results_data)
            if item.get("db_status") == "Belum Ada di DB"
        ]

        if not items_to_process:
            self.root.after(0, lambda: messagebox.showinfo("Informasi", "Semua video sudah ada di Database."))
            self.root.after(0, lambda: self.db_btn.config(state="normal"))
            self.root.after(0, lambda: self.search_btn.config(state="normal"))
            return

        try:
            conn = get_db_connection()
            cursor = conn.cursor(dictionary=True)
            p_id = 5  # Platform ID untuk YouTube (5)

            for idx, item in items_to_process:
                url = item["url"]
                video_id = item["video_id"]
                title = item["title"]

                raw_text_content = f"Deskripsi: {item['description']}\n\nTranskrip/Ringkasan:\n{item['transcript']}"
                media_urls_json = json.dumps([item["thumbnail_url"]])

                query = """
                    INSERT IGNORE INTO contents 
                    (platform_id, clean_url, original_id, title, raw_text, media_urls)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """
                cursor.execute(query, (p_id, url, video_id, title, raw_text_content, media_urls_json))

                if cursor.rowcount > 0:
                    inserted_count += 1
                    content_id = cursor.lastrowid
                    item["db_status"] = "Ada di DB"

                    cursor.execute("""
                        INSERT IGNORE INTO sentiment_results 
                        (target_type, target_id, execution_source, status)
                        VALUES ('content', %s, 'ml_pipeline', 'pending')
                    """, (content_id,))
                    conn.commit()

                    self.root.after(0, lambda i=idx: self.update_tree_row(i, db_status="Ada di DB"))
                    self.root.after(0, lambda u=url: self.append_log(f"   [OK] Saved Video: {u}"))
                else:
                    skipped_count += 1
                    item["db_status"] = "Ada di DB"
                    self.root.after(0, lambda i=idx: self.update_tree_row(i, db_status="Ada di DB"))

            cursor.close()
            conn.close()

            msg = f"Selesai! {inserted_count} video berhasil disimpan, {skipped_count} di-skip."
            self.root.after(0, lambda: self.status_var.set(msg))
            self.root.after(0, lambda: messagebox.showinfo("Proses Selesai", msg))

        except Exception as e:
            self.root.after(0, lambda: messagebox.showerror("Database Error", f"Gagal menyimpan ke DB: {e}"))

        self.root.after(0, lambda: self.db_btn.config(state="normal"))
        self.root.after(0, lambda: self.search_btn.config(state="normal"))

    def export_to_csv(self):
        if not self.results_data:
            return

        file_path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV Files", "*.csv"), ("All Files", "*.*")],
            title="Simpan Hasil Pencarian YouTube As",
        )

        if not file_path:
            return

        try:
            fieldnames = ["video_id", "title", "url", "upload_date", "duration", "description", "transcript", "db_status"]
            with open(file_path, mode="w", newline="", encoding="utf-8") as file:
                writer = csv.DictWriter(file, fieldnames=fieldnames, extrasaction='ignore')
                writer.writeheader()
                writer.writerows(self.results_data)

            messagebox.showinfo("Berhasil", f"Data berhasil di-export ke:\n{file_path}")
        except Exception as e:
            messagebox.showerror("Error Export", f"Gagal menyimpan file: {e}")


if __name__ == "__main__":
    root = tk.Tk()
    app = YouTubeTableApp(root)
    root.mainloop()