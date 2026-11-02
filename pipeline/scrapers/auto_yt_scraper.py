import argparse
from datetime import datetime, timedelta
import json
import logging
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import time
import mysql.connector
from mysql.connector import Error, InterfaceError, OperationalError
import scrapetube
from youtube_transcript_api import YouTubeTranscriptApi
import yt_dlp

# Menambahkan root project directory ke sys.path
sys.path.append(str(Path(__file__).resolve().parent.parent))

from config.config import DATA_DIR, LOG_DIR, get_db_connection

# 1. Matikan Warning Symlinks Hugging Face di Windows
os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
# NOTE: HF_TOKEN JANGAN di-hardcode. Model Qwen bersifat publik sehingga token tidak wajib.
# Kalau perlu, set lewat environment variable di OS / file .env.

# 2. Auto-inject FFmpeg & FFprobe path
try:
    import static_ffmpeg
    static_ffmpeg.add_paths()
except Exception as e:
    print(f"[!] Warning static-ffmpeg: {e}")

# --- LOGGING SETUP ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_DIR / "auto_yt_scraper.log", encoding="utf-8"),
    ],
)

# --- KONFIGURASI ---
LONG_VIDEO_SEC = 300                      # > 5 menit => transkrip diringkas
SUMMARIZER_MODEL = os.environ.get("SUMMARIZER_MODEL", "Qwen/Qwen2.5-3B-Instruct")
CHUNK_CHARS = 6000                        # ukuran potongan transkrip untuk summarizer
MAX_SOURCE_CHARS = int(os.environ.get("MAX_SOURCE_CHARS", "40000"))
WHISPER_LANGUAGE = os.environ.get("WHISPER_LANGUAGE") or None   # None = auto-detect, "id" = paksa Indonesia
MAX_RAW_TEXT_BYTES = int(os.environ.get("MAX_RAW_TEXT_BYTES", "60000"))  # aman untuk kolom TEXT (64KB)
PENDING_DIR = DATA_DIR / "pending_contents"    # hasil olahan yang belum berhasil masuk DB
TEMP_AUDIO_DIR = DATA_DIR / "temp_audio"

whisper_model = None
summarizer_pipeline = None


# =====================================================================
#  DATABASE LAYER (anti-timeout)
#  Prinsip: koneksi TIDAK pernah dibiarkan menganggur saat proses berat
#  (download / Whisper / summarizer). Koneksi dibuka hanya sebentar
#  untuk menulis, dengan retry otomatis kalau putus.
# =====================================================================
RETRYABLE_ERRORS = (OperationalError, InterfaceError, OSError)


def open_connection():
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SET SESSION wait_timeout = 28800")
        cur.execute("SET SESSION net_read_timeout = 600")
        cur.execute("SET SESSION net_write_timeout = 600")
        cur.close()
    except Error:
        pass  # tidak fatal kalau user DB tidak boleh set session variable
    return conn


def run_db(fn, retries: int = 4):
    """Jalankan fn(conn) dengan koneksi baru. Retry + backoff kalau koneksi putus/timeout."""
    last_err = None
    for attempt in range(1, retries + 1):
        conn = None
        try:
            conn = open_connection()
            result = fn(conn)
            conn.commit()
            return result
        except RETRYABLE_ERRORS as e:
            last_err = e
            wait = min(2 ** attempt, 30)
            logging.warning(f"   [DB] Percobaan {attempt}/{retries} gagal: {e}. Retry dalam {wait}s...")
            if conn is not None:
                try:
                    conn.rollback()
                except Exception:
                    pass
            time.sleep(wait)
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
    raise last_err


def truncate_bytes(text: str, max_bytes: int) -> str:
    data = text.encode("utf-8")
    if len(data) <= max_bytes:
        return text
    return data[:max_bytes].decode("utf-8", errors="ignore")


def ensure_platform_youtube_exists():
    def _do(conn):
        cur = conn.cursor()
        cur.execute("SELECT id FROM platforms WHERE id = 5 OR code = 'youtube'")
        if not cur.fetchone():
            cur.execute("""
                INSERT INTO platforms (id, code, name, rss_url)
                VALUES (5, 'youtube', 'YouTube', 'https://youtube.com')
            """)
        cur.close()

    try:
        run_db(_do)
    except Exception as e:
        logging.error(f"Gagal memastikan platform YouTube di DB: {e}")


def load_keywords_and_urls(conn):
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT keyword FROM keywords WHERE platform_id = 5 AND is_active = 1")
    keywords = [row["keyword"] for row in cur.fetchall()]
    cur.execute("SELECT clean_url FROM contents WHERE platform_id = 5")
    urls = {row["clean_url"] for row in cur.fetchall()}
    cur.close()
    return keywords, urls


def insert_content(conn, rec: dict) -> bool:
    """Insert contents + sentiment_results dalam satu transaksi. Return True kalau baris baru."""
    cur = conn.cursor()
    cur.execute("""
        INSERT IGNORE INTO contents
        (platform_id, clean_url, original_id, title, raw_text, media_urls, published_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
    """, (rec["platform_id"], rec["clean_url"], rec["original_id"], rec["title"],
          rec["raw_text"], rec["media_urls"], rec["published_at"]))

    inserted = cur.rowcount > 0
    if inserted:
        content_id = cur.lastrowid
        cur.execute("""
            INSERT IGNORE INTO sentiment_results
            (target_type, target_id, execution_source, status)
            VALUES ('content', %s, 'ml_pipeline', 'pending')
        """, (content_id,))
    cur.close()
    return inserted


def flush_pending() -> int:
    """Kirim ulang hasil olahan yang tertunda (gagal masuk DB di run sebelumnya)."""
    if not PENDING_DIR.exists():
        return 0
    files = sorted(PENDING_DIR.glob("*.json"))
    if not files:
        return 0

    logging.info(f"=== Ada {len(files)} data tertunda di '{PENDING_DIR}', mencoba kirim ulang ke DB ===")
    done = 0
    for f in files:
        try:
            rec = json.loads(f.read_text(encoding="utf-8"))
            run_db(lambda conn: insert_content(conn, rec))
            f.unlink(missing_ok=True)
            done += 1
            logging.info(f"   [PENDING FLUSHED] {rec.get('clean_url')}")
        except Exception as e:
            logging.error(f"   [PENDING GAGAL] {f.name}: {e}")
    return done


# =====================================================================
#  MODEL LOADERS
# =====================================================================
def get_whisper_model():
    global whisper_model
    if whisper_model is None:
        from faster_whisper import WhisperModel
        logging.info("[*] Loading Model Faster-Whisper (small)...")
        whisper_model = WhisperModel("small", device="cpu", compute_type="int8")
    return whisper_model


def split_into_chunks(text: str, size: int = CHUNK_CHARS):
    chunks, cur, cur_len = [], [], 0
    for w in text.split():
        if cur and cur_len + len(w) + 1 > size:
            chunks.append(" ".join(cur))
            cur, cur_len = [], 0
        cur.append(w)
        cur_len += len(w) + 1
    if cur:
        chunks.append(" ".join(cur))
    return chunks


def get_summarizer():
    global summarizer_pipeline
    if summarizer_pipeline is None:
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        logging.info(f"[*] Loading Summarizer LLM: {SUMMARIZER_MODEL} ...")
        tokenizer = AutoTokenizer.from_pretrained(SUMMARIZER_MODEL)
        model = AutoModelForCausalLM.from_pretrained(SUMMARIZER_MODEL, torch_dtype="auto")
        model.eval()

        class LLMSummarizer:
            SYSTEM = (
                "You are an expert at summarizing YouTube video transcripts. "
                "The transcript may be in Indonesian or English. Always write the summary in English. "
                "Only use information that appears in the text. Never invent facts, names, or numbers."
            )

            def __init__(self, model, tokenizer):
                self.model = model
                self.tokenizer = tokenizer

            def _generate(self, user_prompt, max_new_tokens):
                messages = [
                    {"role": "system", "content": self.SYSTEM},
                    {"role": "user", "content": user_prompt},
                ]
                inputs = self.tokenizer.apply_chat_template(
                    messages, add_generation_prompt=True, return_tensors="pt", return_dict=True
                )
                with torch.no_grad():
                    out = self.model.generate(
                        **inputs,
                        max_new_tokens=max_new_tokens,
                        do_sample=False,
                        repetition_penalty=1.05,
                    )
                new_tokens = out[0][inputs["input_ids"].shape[1]:]
                return self.tokenizer.decode(new_tokens, skip_special_tokens=True).strip()

            def summarize(self, text):
                text = text[:MAX_SOURCE_CHARS]
                chunks = split_into_chunks(text)

                if len(chunks) == 1:
                    return self._generate(
                        "Write a comprehensive summary of this video transcript: first a short overview "
                        "paragraph, then the key points as bullet points covering the main topics, "
                        "arguments, and important details.\n\nTranscript:\n" + chunks[0],
                        max_new_tokens=500,
                    )

                partials = []
                for i, ch in enumerate(chunks, 1):
                    logging.info(f"   [*] Meringkas bagian {i}/{len(chunks)}...")
                    partials.append(self._generate(
                        "Summarize this part of a video transcript in detail, keeping all key points, "
                        "facts, and names.\n\nTranscript part:\n" + ch,
                        max_new_tokens=250,
                    ))

                logging.info("   [*] Menggabungkan ringkasan bagian...")
                return self._generate(
                    "Below are summaries of consecutive parts of one video. Merge them into one "
                    "coherent, comprehensive summary: a short overview paragraph, then key points "
                    "as bullet points in the order they appear.\n\n" + "\n\n".join(partials),
                    max_new_tokens=600,
                )

        summarizer_pipeline = LLMSummarizer(model, tokenizer)

    return summarizer_pipeline


# =====================================================================
#  PARSER
# =====================================================================
def parse_duration_to_seconds(duration_str: str) -> int:
    if not duration_str or duration_str == "N/A":
        return 0
    try:
        parts = list(map(int, duration_str.split(":")))
    except ValueError:
        return 0
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    elif len(parts) == 2:
        return parts[0] * 60 + parts[1]
    return 0


def parse_relative_time_to_datetime(published_text: str):
    """Mengonversi teks relatif YouTube (seperti '2 days ago', '1 bulan lalu') ke format DATETIME MySQL."""
    if not published_text or published_text == "N/A":
        return None

    now = datetime.now()
    text = published_text.lower().strip()

    match = re.search(r'(\d+)\s*(second|sec|minute|min|hour|hr|day|d|week|wk|month|mo|year|yr|detik|menit|jam|hari|minggu|bulan|tahun)', text)
    if not match:
        return None

    val = int(match.group(1))
    unit = match.group(2)

    if unit in ['second', 'seconds', 'sec', 'detik']:
        dt = now - timedelta(seconds=val)
    elif unit in ['minute', 'minutes', 'min', 'menit']:
        dt = now - timedelta(minutes=val)
    elif unit in ['hour', 'hours', 'hr', 'hrs', 'jam']:
        dt = now - timedelta(hours=val)
    elif unit in ['day', 'days', 'd', 'hari']:
        dt = now - timedelta(days=val)
    elif unit in ['week', 'weeks', 'wk', 'wks', 'minggu']:
        dt = now - timedelta(weeks=val)
    elif unit in ['month', 'months', 'mo', 'mos', 'bulan']:
        dt = now - timedelta(days=val * 30)
    elif unit in ['year', 'years', 'yr', 'yrs', 'tahun']:
        dt = now - timedelta(days=val * 365)
    else:
        dt = now

    return dt.strftime("%Y-%m-%d %H:%M:%S")


# =====================================================================
#  TRANSKRIP: subtitle resmi -> fallback Whisper (untuk SEMUA durasi)
# =====================================================================
def fetch_subtitle_text(video_id: str) -> str:
    """Ambil subtitle resmi. Kompatibel dengan youtube-transcript-api versi lama dan baru (>=1.0)."""
    languages = ['id', 'en']
    try:
        if hasattr(YouTubeTranscriptApi, "get_transcript"):
            items = YouTubeTranscriptApi.get_transcript(video_id, languages=languages)
            return " ".join(item["text"] for item in items)
        fetched = YouTubeTranscriptApi().fetch(video_id, languages=languages)
        return " ".join(snippet.text for snippet in fetched)
    except Exception:
        return ""


def download_youtube_audio(video_url: str, output_path: str) -> str:
    ydl_opts = {
        'format': 'm4a/bestaudio/best',
        'outtmpl': os.path.join(output_path, '%(id)s.%(ext)s'),
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'm4a',
        }],
        'quiet': True,
        'no_warnings': True,
        'socket_timeout': 30,
        'retries': 5,
        'fragment_retries': 5,
        'extractor_args': {
            'youtube': {
                'player_client': ['android', 'web'],
                'player_skip': ['js'],
            }
        },
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
            'Accept-Language': 'en-US,en;q=0.9,id;q=0.8',
        }
    }

    try:
        import static_ffmpeg
        ffmpeg_exe, _ = static_ffmpeg.run.get_or_fetch_platform_executables_or_raise()
        ydl_opts['ffmpeg_location'] = os.path.dirname(ffmpeg_exe)
    except Exception:
        pass

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(video_url, download=True)
        filename = ydl.prepare_filename(info)
        base, _ = os.path.splitext(filename)
        audio_file = f"{base}.m4a"
        if os.path.exists(audio_file):
            return audio_file
        return filename


def transcribe_with_whisper(video_url: str, video_id: str, duration_sec: int) -> str:
    TEMP_AUDIO_DIR.mkdir(exist_ok=True)
    work_dir = tempfile.mkdtemp(prefix=f"{video_id}_", dir=str(TEMP_AUDIO_DIR))
    try:
        logging.info("   [*] Download audio...")
        audio_file = download_youtube_audio(video_url, work_dir)

        model = get_whisper_model()
        # Video panjang: beam_size kecil supaya jauh lebih cepat di CPU
        beam = 5 if duration_sec <= LONG_VIDEO_SEC else 1
        logging.info(f"   [*] Whisper transcribing (beam_size={beam})...")
        segments, _ = model.transcribe(
            audio_file,
            beam_size=beam,
            language=WHISPER_LANGUAGE,
            vad_filter=True,                      # lewati bagian hening / musik
            condition_on_previous_text=False,     # cegah halusinasi berulang di video panjang
        )

        parts = []
        last_log = time.time()
        for seg in segments:
            parts.append(seg.text.strip())
            if time.time() - last_log > 60:
                logging.info(f"   [*] Whisper progress: {int(seg.end)}s / {duration_sec}s")
                last_log = time.time()
        return " ".join(parts).strip()
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def process_video_text(video_id: str, video_url: str, duration_sec: int,
                       video_title: str, video_description: str, max_audio_min: int = 0) -> str:
    is_long = duration_sec > LONG_VIDEO_SEC

    # 1) Subtitle resmi dulu (cepat)
    transcript = fetch_subtitle_text(video_id).strip()
    if transcript:
        logging.info("   [*] Subtitle resmi ditemukan.")
    else:
        # 2) Tidak ada subtitle -> Whisper, untuk SEMUA durasi (kecuali live/durasi tak diketahui)
        too_long = max_audio_min > 0 and duration_sec > max_audio_min * 60
        if duration_sec <= 0:
            logging.info("   [!] Durasi tidak diketahui (live/upcoming?), Whisper dilewati.")
        elif too_long:
            logging.info(f"   [!] Durasi > {max_audio_min} menit (--max-audio-min), Whisper dilewati.")
        else:
            try:
                transcript = transcribe_with_whisper(video_url, video_id, duration_sec).strip()
            except Exception as e:
                logging.warning(f"   [!] Gagal transkripsi audio Whisper: {e}")

    if len(transcript) < 20:
        return f"[DESKRIPSI VIDEO]: {video_description}"

    if not is_long:
        return f"[TRANSKRIP LENGKAP]: {transcript}"

    # 3) Video > 5 menit: ringkas transkrip LENGKAP (bukan judul/deskripsi)
    try:
        summary_text = get_summarizer().summarize(transcript)
        return f"[RINGKASAN VIDEO > 5 MIN]: {summary_text}"
    except Exception as e:
        logging.warning(f"   [!] Gagal membuat ringkasan: {e}")
        return f"[TRANSKRIP (TERPOTONG)]: {transcript[:3000]}"


# =====================================================================
#  PIPELINE
# =====================================================================
def search_videos(kw: str, limit: int, retries: int = 3):
    for attempt in range(1, retries + 1):
        try:
            return list(scrapetube.get_search(query=kw, limit=limit, results_type="video"))
        except Exception as e:
            logging.warning(f"Pencarian '{kw}' gagal (percobaan {attempt}/{retries}): {e}")
            time.sleep(2 ** attempt)
    logging.error(f"Gagal mencari video untuk keyword '{kw}' setelah {retries} percobaan.")
    return []


def run_youtube_crawling_pipeline(limit_per_keyword, max_audio_min: int = 0):
    ensure_platform_youtube_exists()
    flush_pending()

    total_inserted = 0
    total_skipped = 0
    total_failed = 0
    p_id = 5  # Platform ID untuk YouTube

    try:
        keywords, existing_urls = run_db(load_keywords_and_urls)
    except Exception as e:
        logging.error(f"Gagal memuat keyword/URL dari DB: {e}")
        return

    if not keywords:
        logging.info("Tidak ada keyword aktif di database untuk platform YouTube.")
        return

    logging.info(f"=== Menjalankan Crawling YouTube untuk Keywords DB: {keywords} ===")
    PENDING_DIR.mkdir(exist_ok=True)

    for kw in keywords:
        logging.info(f"=== Memulai Crawling YouTube untuk Kata Kunci: '{kw}' ===")

        videos = search_videos(kw, limit_per_keyword)
        logging.info(f"Ditemukan {len(videos)} video dari pencarian. Memeriksa keberadaan di DB...")

        for video in videos:
            video_id = video.get("videoId", "")
            if not video_id:
                continue
            url = f"https://www.youtube.com/watch?v={video_id}"

            if url in existing_urls:
                total_skipped += 1
                logging.info(f"   [SKIP FAST - DUPLIKAT DB] {url}")
                continue

            try:
                title = "N/A"
                if "title" in video and "runs" in video["title"]:
                    title = video["title"]["runs"][0].get("text", "N/A")

                duration_str = video.get("lengthText", {}).get("simpleText", "N/A")
                duration_sec = parse_duration_to_seconds(duration_str)

                # Ekstraksi Tanggal Upload (published_at) dengan Fallback Multi-Format
                published_text = "N/A"
                pub_obj = video.get("publishedTimeText", {})
                if "simpleText" in pub_obj:
                    published_text = pub_obj["simpleText"]
                elif "runs" in pub_obj and pub_obj["runs"]:
                    published_text = "".join([r.get("text", "") for r in pub_obj["runs"]]).strip()

                published_at = parse_relative_time_to_datetime(published_text)

                description = "Tidak ada deskripsi."
                if "detailedMetadataSnippets" in video and video["detailedMetadataSnippets"]:
                    snippets = video["detailedMetadataSnippets"][0].get("snippetText", {}).get("runs", [])
                    description = "".join([s.get("text", "") for s in snippets]).replace("\n", " ")

                thumbnail_url = f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg"
                media_urls_json = json.dumps([thumbnail_url])

                logging.info(f"[*] Processing ID {video_id} [{duration_str}] (Diunggah: {published_text} -> {published_at}): {title[:50]}...")

                # ---- Proses berat (TANPA koneksi DB terbuka) ----
                processed_text = process_video_text(
                    video_id, url, duration_sec, title, description, max_audio_min
                )
                formatted_raw_text = f"Deskripsi: {description}\n\nTranskrip/Ringkasan:\n{processed_text}"
                formatted_raw_text = truncate_bytes(formatted_raw_text, MAX_RAW_TEXT_BYTES)

                rec = {
                    "platform_id": p_id,
                    "clean_url": url,
                    "original_id": video_id,
                    "title": title,
                    "raw_text": formatted_raw_text,
                    "media_urls": media_urls_json,
                    "published_at": published_at,
                }

                # Simpan lokal dulu: kalau DB gagal, hasil olahan tidak hilang
                pending_file = PENDING_DIR / f"{video_id}.json"
                pending_file.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")

                # ---- Koneksi DB baru, hanya untuk menulis ----
                try:
                    inserted = run_db(lambda conn: insert_content(conn, rec))
                except Exception as e:
                    total_failed += 1
                    logging.error(f"   [DB GAGAL] {url}: {e} | Disimpan di {pending_file} untuk dicoba lagi di run berikutnya")
                    continue

                pending_file.unlink(missing_ok=True)

                if inserted:
                    total_inserted += 1
                    existing_urls.add(url)
                    logging.info(f"   [INSERTED SUCCESS] -> {url} | Published At: {published_at}")
                else:
                    total_skipped += 1
                    logging.info(f"   [SKIPPED IGNORE] -> {url}")

            except Exception as e:
                # Satu video bermasalah tidak boleh mematikan seluruh batch
                total_failed += 1
                logging.error(f"   [ERROR VIDEO] {url}: {e}")
                continue

    logging.info(
        f"=== BATCH CRAWLING YOUTUBE SELESAI | Inserted Baru: {total_inserted} | "
        f"Skipped/Duplikat: {total_skipped} | Gagal: {total_failed} ==="
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Headless YouTube Scraper & Processor untuk db_sentimenanalisis_v2"
    )
    parser.add_argument(
        "-l",
        "--limit",
        type=int,
        default=5,
        help="Batas jumlah video per kata kunci (default: 5)",
    )
    parser.add_argument(
        "--max-audio-min",
        type=int,
        default=0,
        help="Lewati Whisper untuk video lebih panjang dari N menit (default: 0 = tanpa batas)",
    )

    args = parser.parse_args()
    run_youtube_crawling_pipeline(args.limit, args.max_audio_min)