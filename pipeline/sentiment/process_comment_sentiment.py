"""
Pemrosesan sentimen KOMENTAR - dua tahap.

Tahap 1 (cepat)  : IndoBERT (batch inference) -> hasil langsung 'completed', model_name='indobert'
Tahap 2 (verifikasi): Qwen3.8-9B Distill MLX 5-bit (logit scoring, tanpa generate teks) HANYA untuk
                      baris sentiment='negative' AND model_name='indobert'. Konteks judul konten induk
                      ikut dikirim. Hasil akhir model_name='indobert+qwen3.8-9b-5bit'.

Pemakaian:
    python process_comment_sentiment_db.py              # tahap 1 lalu tahap 2
    python process_comment_sentiment_db.py --stage 1    # hanya IndoBERT
    python process_comment_sentiment_db.py --stage 2    # hanya verifikasi Qwen (bisa dijalankan terpisah)

Tahap 2 butuh: pip install -U mlx-lm   (unduhan pertama ± 6 GB)
"""
import argparse
import logging
import os
import re
import sys
from pathlib import Path

# Menambahkan root project directory (1 tingkat di atas folder script saat ini) ke sys.path
sys.path.append(str(Path(__file__).resolve().parent.parent))

from config.config import LOG_DIR, get_db_connection

# ---------------------------------------------------------
# 1. SETUP ENVIRONMENT & LOGGING
# ---------------------------------------------------------
os.environ["PURE_PYTHON_REGEX"] = "1"
os.environ["TRANSFORMERS_NO_ADVISORY_WARNINGS"] = "1"
os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
# HF_TOKEN (opsional) dibaca dari environment / file .env, tidak ditulis di kode.
# Izinkan fallback ke CPU untuk operasi yang belum didukung MPS
os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")

import mysql.connector
from mysql.connector import Error
import torch
from transformers import pipeline

# MLX untuk verifikator Qwen (format model MLX tidak bisa dimuat lewat transformers/PyTorch)
try:
    import mlx.core as mx
    from mlx_lm import load as mlx_load, stream_generate as mlx_stream_generate
    MLX_IMPORT_OK = True
    _MLX_IMPORT_ERR = None
except Exception as _mlx_err:
    mx = None
    MLX_IMPORT_OK = False
    _MLX_IMPORT_ERR = _mlx_err

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_DIR / "comment_sentiment_processor.log", encoding="utf-8"),
    ],
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)

# Device: CUDA -> Apple MPS -> CPU (untuk IndoBERT; Qwen berjalan via MLX)
if torch.cuda.is_available():
    device = "cuda"
elif torch.backends.mps.is_available():
    device = "mps"
else:
    device = "cpu"
logging.info(f"🚀 Running Comment ML Engine on Device: {device.upper()}")


def free_memory():
    try:
        if device == "mps":
            torch.mps.empty_cache()
        elif device == "cuda":
            torch.cuda.empty_cache()
    except Exception:
        pass
    # Cache MLX (verifikator Qwen)
    try:
        if mx is not None:
            if hasattr(mx, "clear_cache"):
                mx.clear_cache()
            else:
                mx.metal.clear_cache()
    except Exception:
        pass


# ---------------------------------------------------------
# 2. KONFIGURASI
# ---------------------------------------------------------
INDOBERT_MODEL_ID = "agufsamudra/indo-sentiment-analysis"
INDOBERT_TAG = "indobert"
STAGE1_BATCH_LIMIT = 50          # jumlah komentar pending per putaran tahap 1
INDOBERT_BATCH_SIZE = 16         # batch inference IndoBERT
LONG_TEXT_WORDS = 300            # > ini dipecah per chunk

# Model komunitas (distilasi empero-ai di atas arsitektur Qwen3.5-9B), BUKAN rilis resmi Qwen.
QWEN_MODEL_ID = "RolanDorisTech/Qwen3.8-9B-Distill-MLX-5bit"
QWEN_MODEL_TAG = "qwen3.8-9b-5bit"
VERIFIED_MODEL_NAME = f"{INDOBERT_TAG}+{QWEN_MODEL_TAG}"
STAGE2_BATCH_LIMIT = 50          # jumlah komentar negatif per putaran tahap 2
QWEN_MAX_COMMENT_CHARS = 2000    # batas karakter komentar yang dikirim ke Qwen
QWEN_MAX_TITLE_CHARS = 200
QWEN_OVERRIDE_MIN_PROB = 0.50    # Qwen harus >= ini untuk mengubah negative -> label lain

# Kolom di tabel `comments` yang mungkin menunjuk ke contents.id (dideteksi otomatis)
PARENT_FK_CANDIDATES = ["content_id", "post_id", "article_id", "video_id", "parent_content_id"]

SENTIMENT_LABELS = ["positive", "negative", "neutral"]


# ---------------------------------------------------------
# 3. LAZY MODEL LOADERS
# ---------------------------------------------------------
_text_classifier = None
_qwen = {"model": None, "tokenizer": None, "label_ids": None}


def get_text_classifier():
    global _text_classifier
    if _text_classifier is None:
        logging.info("⏳ Memuat Model IndoBERT Sentiment Analysis...")
        _text_classifier = pipeline(
            "sentiment-analysis",
            model=INDOBERT_MODEL_ID,
            device=device,
        )
        logging.info("✅ Model IndoBERT Siap Digunakan!\n")
    return _text_classifier


def get_qwen():
    """Muat Qwen MLX sekali. Melempar exception jika mlx-lm tidak ada / model gagal dimuat."""
    if _qwen["model"] is None:
        if not MLX_IMPORT_OK:
            raise RuntimeError(
                f"mlx / mlx-lm tidak bisa di-import ({_MLX_IMPORT_ERR}). Jalankan: pip install -U mlx-lm"
            )
        logging.info(f"⏳ Memuat Model {QWEN_MODEL_ID} (verifikator komentar negatif)...")
        model, tokenizer = mlx_load(QWEN_MODEL_ID)

        label_ids = [
            tokenizer.encode(lbl, add_special_tokens=False)[0] for lbl in SENTIMENT_LABELS
        ]
        if len(set(label_ids)) != len(label_ids):
            raise RuntimeError(
                f"Token pertama label sentimen bertabrakan: {dict(zip(SENTIMENT_LABELS, label_ids))}"
            )
        _qwen.update({"model": model, "tokenizer": tokenizer, "label_ids": label_ids})
        logging.info("✅ Qwen MLX Berhasil Dimuat!\n")
    return _qwen["model"], _qwen["tokenizer"], _qwen["label_ids"]


# ---------------------------------------------------------
# 4. TAHAP 1: INDOBERT
# ---------------------------------------------------------
def _polarity_from_result(res: dict) -> float:
    label_raw = str(res["label"]).upper()
    if label_raw in ("LABEL_1", "POSITIVE"):
        return res["score"]
    if label_raw in ("LABEL_0", "NEGATIVE"):
        return -res["score"]
    return 0.0


def _polarity_to_label(polarity: float) -> str:
    if polarity > 0.05:
        return "positive"
    if polarity < -0.05:
        return "negative"
    return "neutral"


def is_trivial_text(text: str) -> bool:
    """Komentar tanpa kata bermakna (hanya tag @user, URL, angka, simbol/emoji) -> neutral."""
    cleaned = re.sub(r"https?://\S+|@\w+", " ", text)
    return len(re.findall(r"[A-Za-z]{2,}", cleaned)) == 0


def analyze_long_text(text: str) -> tuple:
    """Komentar panjang: pecah per 250 kata, rata-ratakan polaritas."""
    classifier = get_text_classifier()
    words = text.split()
    chunks = [" ".join(words[i : i + 250]) for i in range(0, len(words), 250)]
    scores = []
    for c in chunks:
        res = classifier(c, truncation=True, max_length=512)[0]
        scores.append(_polarity_from_result(res))
    avg = sum(scores) / len(scores) if scores else 0.0
    return _polarity_to_label(avg), round(abs(avg), 4), INDOBERT_TAG


def analyze_comments_batch(texts: list) -> list:
    """
    Analisis sekumpulan komentar. Mengembalikan list sejajar dengan `texts`;
    tiap elemen berupa tuple (label, confidence, model_name) atau Exception.
    """
    results = [None] * len(texts)
    normal_idx = []

    for i, text in enumerate(texts):
        if not text or not text.strip() or is_trivial_text(text):
            results[i] = ("neutral", 0.0, "rule")
        elif len(text.split()) > LONG_TEXT_WORDS:
            try:
                results[i] = analyze_long_text(text)
            except Exception as e:
                results[i] = e
        else:
            normal_idx.append(i)

    if normal_idx:
        classifier = get_text_classifier()
        batch_texts = [texts[i] for i in normal_idx]
        try:
            outputs = classifier(
                batch_texts, batch_size=INDOBERT_BATCH_SIZE, truncation=True, max_length=512
            )
            for i, res in zip(normal_idx, outputs):
                pol = _polarity_from_result(res)
                results[i] = (_polarity_to_label(pol), round(abs(pol), 4), INDOBERT_TAG)
        except Exception as batch_err:
            logging.warning(f"Batch IndoBERT gagal ({batch_err}); fallback per item...")
            for i in normal_idx:
                try:
                    res = classifier(texts[i], truncation=True, max_length=512)[0]
                    pol = _polarity_from_result(res)
                    results[i] = (_polarity_to_label(pol), round(abs(pol), 4), INDOBERT_TAG)
                except Exception as e:
                    results[i] = e
    return results


def ensure_comment_queue_exists():
    """Memastikan komentar baru di tabel `comments` terdaftar di `sentiment_results` sebagai 'pending'."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT IGNORE INTO sentiment_results (target_type, target_id, execution_source, status)
            SELECT 'comment', cm.id, 'ml_pipeline', 'pending'
            FROM comments cm
            LEFT JOIN sentiment_results sr
                ON sr.target_id = cm.id AND sr.target_type = 'comment'
            WHERE sr.id IS NULL
            """
        )
        conn.commit()
        if cursor.rowcount > 0:
            logging.info(f"📌 Menambahkan {cursor.rowcount} komentar baru ke antrean pending...")
        cursor.close()
        conn.close()
    except Error as e:
        logging.error(f"[!] Gagal menyinkronkan antrean komentar: {e}")


def process_pending_comment_queue() -> int:
    """Tahap 1: proses komentar 'pending' dengan IndoBERT (batch)."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            """
            SELECT sr.id AS result_id, sr.target_id, cm.comment_text, cm.author_name
            FROM sentiment_results sr
            JOIN comments cm ON sr.target_id = cm.id
            WHERE sr.target_type = 'comment'
              AND sr.status = 'pending'
            ORDER BY sr.id
            LIMIT %s
            """,
            (STAGE1_BATCH_LIMIT,),
        )
        items = cursor.fetchall()
        total = len(items)
        if total == 0:
            cursor.close()
            conn.close()
            return 0

        logging.info(f"[*] Tahap 1: memproses batch ({total} komentar pending)...")
        results = analyze_comments_batch([it["comment_text"] or "" for it in items])

        ok_rows, fail_rows = [], []
        counts = {"positive": 0, "negative": 0, "neutral": 0}
        for it, res in zip(items, results):
            if isinstance(res, Exception):
                fail_rows.append((str(res)[:500], it["result_id"]))
                logging.error(f"  [ERROR] Comment ID {it['target_id']}: {res}")
            else:
                label, conf, model_name = res
                counts[label] += 1
                ok_rows.append((label, conf, model_name, it["result_id"]))

        if ok_rows:
            cursor.executemany(
                """
                UPDATE sentiment_results
                SET sentiment = %s, confidence_score = %s, model_name = %s,
                    status = 'completed', analyzed_at = NOW()
                WHERE id = %s
                """,
                ok_rows,
            )
        if fail_rows:
            cursor.executemany(
                """
                UPDATE sentiment_results
                SET status = 'failed', error_message = %s,
                    retry_count = retry_count + 1, analyzed_at = NOW()
                WHERE id = %s
                """,
                fail_rows,
            )
        conn.commit()
        cursor.close()
        conn.close()

        logging.info(
            f"  [SUCCESS] {len(ok_rows)} selesai (pos={counts['positive']}, neg={counts['negative']}, "
            f"neu={counts['neutral']}) | {len(fail_rows)} gagal"
        )
        free_memory()
        return total

    except Error as e:
        logging.error(f"[!] Database Error: {e}")
        return 0


def run_stage1():
    logging.info("🚀 TAHAP 1: sentimen komentar dengan IndoBERT...")
    ensure_comment_queue_exists()
    total_processed = 0
    while True:
        n = process_pending_comment_queue()
        total_processed += n
        if n == 0:
            break
    logging.info(f"=== TAHAP 1 SELESAI | Total Komentar Dianalisis: {total_processed} ===")


# ---------------------------------------------------------
# 5. TAHAP 2: VERIFIKASI QWEN UNTUK 'NEGATIVE'
# ---------------------------------------------------------
QWEN_SYSTEM_PROMPT = (
    "Kamu adalah analis sentimen komentar media sosial berbahasa Indonesia yang teliti. "
    "Nilai nada/sentimen KOMENTAR terhadap pihak atau topik yang dibahas, dengan mempertimbangkan judul konten induknya.\n"
    "Pedoman:\n"
    "- positive: apresiasi, dukungan, doa/harapan baik (amin, semoga, sukses, barakallah), rasa haru, simpati tulus.\n"
    "- negative: kritik, keluhan, kemarahan, hinaan, ketidakpercayaan, atau sindiran/sarkasme.\n"
    "- neutral: pertanyaan, informasi, tag nama, atau komentar tanpa nada memihak.\n"
    "- Kata bernada negatif (mis. 'kasihan', 'miris') bisa muncul dalam komentar yang bersimpati atau mendukung; "
    "jangan menilai dari kata saja.\n"
    "- Waspadai sarkasme: pujian palsu yang bermaksud menyindir adalah negative.\n"
    "Jawab HANYA dengan satu kata: positive, negative, atau neutral."
)


def build_qwen_prompt(parent_title: str, comment_text: str) -> str:
    _, tokenizer, _ = get_qwen()
    title = (parent_title or "-").strip()[:QWEN_MAX_TITLE_CHARS]
    comment = comment_text.strip()[:QWEN_MAX_COMMENT_CHARS]
    user_prompt = (
        f"Judul konten: {title}\n\n"
        f"Komentar:\n{comment}\n\n"
        "Sentimen komentar ini (positive/negative/neutral)?"
    )
    messages = [
        {"role": "system", "content": QWEN_SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]
    return tokenizer.apply_chat_template(
        messages,
        tokenize=False,
        add_generation_prompt=True,
        enable_thinking=False,  # matikan thinking agar token berikutnya langsung jawaban
    )


def qwen_score_prompt(prompt: str) -> dict:
    """
    Satu prefill + 1 token: ambil log-probabilitas token pertama untuk 3 label lalu softmax
    (renormalisasi di antara ketiga label). Hanya posisi terakhir yang dihitung ke vocab,
    jadi hemat memori dibanding memanggil model(...) langsung.
    Return: {"positive": p, "negative": p, "neutral": p}
    """
    model, tokenizer, label_ids = get_qwen()
    logprobs = None
    for resp in mlx_stream_generate(model, tokenizer, prompt=prompt, max_tokens=1):
        logprobs = resp.logprobs
        break
    if logprobs is None:
        raise RuntimeError("mlx_lm tidak mengembalikan logprobs")
    sel = logprobs[mx.array(label_ids)].astype(mx.float32)
    probs = mx.softmax(sel, axis=-1)
    mx.eval(probs)
    return dict(zip(SENTIMENT_LABELS, probs.tolist()))


def qwen_score_with_fallback(prompts: list) -> list:
    """Skor satu per satu (komentar pendek, prefill cepat). Elemen gagal = None."""
    scored = []
    for p in prompts:
        try:
            scored.append(qwen_score_prompt(p))
        except Exception as e:
            logging.warning(f"    [QWEN] Item gagal: {e}")
            free_memory()
            scored.append(None)
    return scored


def detect_parent_fk() -> str:
    """Cari kolom di `comments` yang menunjuk ke contents.id (untuk konteks judul induk)."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT COLUMN_NAME FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'comments'
            """
        )
        cols = {row[0] for row in cursor.fetchall()}
        cursor.close()
        conn.close()
        for cand in PARENT_FK_CANDIDATES:
            if cand in cols:
                logging.info(f"🔗 Konteks induk: comments.{cand} -> contents.id")
                return cand
    except Error as e:
        logging.warning(f"Gagal mendeteksi kolom induk: {e}")
    logging.warning(
        "⚠️ Kolom FK komentar->contents tidak ditemukan; verifikasi tanpa judul induk. "
        "Isi PARENT_FK_CANDIDATES dengan nama kolom yang benar."
    )
    return ""


def process_negative_verification(last_id: int, parent_fk: str, cache: dict) -> tuple:
    """
    Tahap 2: ambil batch komentar negative yang belum diverifikasi (keyset pagination lewat last_id,
    sehingga baris yang gagal tidak diambil berulang dalam satu run).
    Return (jumlah_baris, last_id_baru).
    """
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        if parent_fk:
            select_title = "ct.title AS parent_title"
            join_parent = f"LEFT JOIN contents ct ON ct.id = cm.`{parent_fk}`"
        else:
            select_title = "NULL AS parent_title"
            join_parent = ""

        cursor.execute(
            f"""
            SELECT sr.id AS result_id, sr.target_id, sr.confidence_score AS old_conf,
                   cm.comment_text, {select_title}
            FROM sentiment_results sr
            JOIN comments cm ON sr.target_id = cm.id
            {join_parent}
            WHERE sr.target_type = 'comment'
              AND sr.status = 'completed'
              AND sr.sentiment = 'negative'
              AND sr.model_name = %s
              AND sr.id > %s
            ORDER BY sr.id
            LIMIT %s
            """,
            (INDOBERT_TAG, last_id, STAGE2_BATCH_LIMIT),
        )
        items = cursor.fetchall()
        total = len(items)
        if total == 0:
            cursor.close()
            conn.close()
            return 0, last_id

        new_last_id = items[-1]["result_id"]
        logging.info(f"[*] Tahap 2: verifikasi Qwen untuk {total} komentar negative...")

        # Kumpulkan prompt unik (cache untuk teks yang sama: spam/copy-paste)
        keys = []
        pending = {}  # key -> prompt
        for it in items:
            text = (it["comment_text"] or "").strip()
            key = ((it["parent_title"] or "").strip().lower(), text.lower())
            keys.append(key)
            if text and key not in cache and key not in pending:
                pending[key] = build_qwen_prompt(it["parent_title"], text)

        # Skor prompt unik (urut dari yang terpendek)
        pending_keys = sorted(pending.keys(), key=lambda k: len(pending[k]))
        scored = qwen_score_with_fallback([pending[k] for k in pending_keys])
        for k, probs in zip(pending_keys, scored):
            if probs is not None:
                cache[k] = probs

        updates = []
        flipped = confirmed = skipped = 0
        for it, key in zip(items, keys):
            probs = cache.get(key)
            if probs is None:
                skipped += 1  # dibiarkan 'indobert' -> dicoba lagi pada run berikutnya
                continue

            top_label = max(probs, key=probs.get)
            top_prob = probs[top_label]

            if top_label != "negative" and top_prob >= QWEN_OVERRIDE_MIN_PROB:
                flipped += 1
                updates.append((top_label, round(top_prob, 4), VERIFIED_MODEL_NAME, it["result_id"]))
                logging.info(
                    f"    [QWEN] OVERRIDE negative -> {top_label} (p={top_prob:.2f}) | "
                    f"Comment ID {it['target_id']}: {(it['comment_text'] or '')[:60]!r}"
                )
            else:
                confirmed += 1
                old_conf = float(it["old_conf"]) if it["old_conf"] is not None else round(probs["negative"], 4)
                updates.append(("negative", old_conf, VERIFIED_MODEL_NAME, it["result_id"]))

        if updates:
            cursor.executemany(
                """
                UPDATE sentiment_results
                SET sentiment = %s, confidence_score = %s, model_name = %s, analyzed_at = NOW()
                WHERE id = %s
                """,
                updates,
            )
            conn.commit()
        cursor.close()
        conn.close()

        logging.info(
            f"  [SUCCESS] override={flipped} | negative terkonfirmasi={confirmed} | dilewati={skipped}"
        )
        free_memory()
        return total, new_last_id

    except Error as e:
        logging.error(f"[!] Database Error (tahap 2): {e}")
        return 0, last_id


def run_stage2():
    logging.info(f"🚀 TAHAP 2: verifikasi komentar NEGATIVE dengan {QWEN_MODEL_ID}...")

    # Muat model di depan agar kegagalan (mlx-lm belum terpasang / unduhan gagal) terlihat jelas.
    try:
        get_qwen()
    except Exception as e:
        logging.error(
            f"❌ Qwen tidak bisa dimuat, TAHAP 2 DIBATALKAN (label negative dari IndoBERT dibiarkan apa adanya): {e}"
        )
        return

    parent_fk = detect_parent_fk()
    cache = {}
    last_id = 0
    total_processed = 0
    while True:
        n, last_id = process_negative_verification(last_id, parent_fk, cache)
        total_processed += n
        if n == 0:
            break
    logging.info(f"=== TAHAP 2 SELESAI | Total Komentar Negative Diverifikasi: {total_processed} ===")


# ---------------------------------------------------------
# 6. MAIN EXECUTION
# ---------------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sentimen komentar: IndoBERT + verifikasi Qwen untuk negative")
    parser.add_argument(
        "--stage",
        choices=["1", "2", "all"],
        default="all",
        help="1 = IndoBERT saja, 2 = verifikasi Qwen saja, all = keduanya (default)",
    )
    args = parser.parse_args()

    if args.stage in ("1", "all"):
        run_stage1()
    if args.stage in ("2", "all"):
        run_stage2()