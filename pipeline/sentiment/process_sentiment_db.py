import json
import logging
import os
import re
import ssl
import sys
import time
from io import BytesIO
from pathlib import Path

# ---------------------------------------------------------
# 1. BYPASS ENVIRONMENT & SETUP (SSL FIX)
# ---------------------------------------------------------
# Bypass SSL Certificate Verification untuk unduhan otomatis model EasyOCR / urllib
ssl._create_default_https_context = ssl._create_unverified_context

os.environ["PURE_PYTHON_REGEX"] = "1"
os.environ["TRANSFORMERS_NO_ADVISORY_WARNINGS"] = "1"
os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
# HF_TOKEN TIDAK ditulis di kode. Semua model yang dipakai publik, jadi token tidak wajib.
# Kalau suatu saat butuh, set lewat environment: export HF_TOKEN=...
# Izinkan fallback ke CPU untuk operasi yang belum didukung MPS
os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")

# Menambahkan root project directory (1 tingkat di atas folder script saat ini) ke sys.path
sys.path.append(str(Path(__file__).resolve().parent.parent))

from config.config import DATA_DIR, LOG_DIR, get_db_connection

try:
    import regex
except ImportError:
    sys.modules["regex"] = re

import easyocr
import mysql.connector
from mysql.connector import Error
from PIL import Image
import requests
import torch
from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    CLIPModel,
    CLIPProcessor,
    pipeline,
)

# MLX untuk verifikator Qwen (format model MLX tidak bisa dimuat lewat transformers/PyTorch)
try:
    import mlx.core as mx
    from mlx_lm import load as mlx_load, generate as mlx_generate
    MLX_IMPORT_OK = True
except Exception as _mlx_err:
    mx = None
    MLX_IMPORT_OK = False
    _MLX_IMPORT_ERR = _mlx_err

# Logging Setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_DIR / "sentiment_processor.log", encoding="utf-8"),
    ],
)

# Sembunyikan log verbositas request HTTP dari httpx/urllib3 agar log bersih
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)

# Device Configuration (CUDA -> Apple MPS -> CPU)
if torch.cuda.is_available():
    device = "cuda"
elif torch.backends.mps.is_available():
    device = "mps"
else:
    device = "cpu"
logging.info(f"🚀 Running ML Engine on Device: {device.upper()}")


def free_memory():
    """Bebaskan cache memori accelerator setelah tiap item."""
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
# 2. INISIALISASI MODEL ML
# ---------------------------------------------------------
logging.info("⏳ Memuat Model IndoBERT Sentiment...")
text_classifier = pipeline(
    "sentiment-analysis",
    model="agufsamudra/indo-sentiment-analysis",
    device=device,
)

logging.info("⏳ Memuat Model CLIP (OpenAI)...")
clip_model = CLIPModel.from_pretrained("openai/clip-vit-base-patch32").to(device)
clip_processor = CLIPProcessor.from_pretrained("openai/clip-vit-base-patch32")

logging.info("⏳ Memuat Model EasyOCR...")
# EasyOCR dijalankan di CPU kecuali CUDA (paling aman di Mac)
ocr_reader = easyocr.Reader(["id", "en"], gpu=(device == "cuda"))

# --- Moondream2 (VLM sarkasme): DIMATIKAN default ---
# fp32 memakan ~7.5 GB RAM; Qwen sudah menangani sarkasme lewat system prompt-nya.
USE_VLM = False
vlm_available = False
vlm_model = None
vlm_tokenizer = None
if USE_VLM:
    logging.info("⏳ Memuat Model VLM (Moondream2)...")
    try:
        vlm_model_id = "vikhyatk/moondream2"
        vlm_model = AutoModelForCausalLM.from_pretrained(
            vlm_model_id,
            trust_remote_code=True,
            dtype=torch.float16,
        ).to(device)
        vlm_tokenizer = AutoTokenizer.from_pretrained(vlm_model_id)
        vlm_available = True
        logging.info("✅ VLM Moondream2 Berhasil Dimuat!")
    except Exception as e:
        logging.warning(f"⚠️ VLM Gagal dimuat: {e}")
        vlm_available = False

# --- Qwen3.8-9B Distill (MLX 5-bit): verifikator konteks untuk hasil 'negative' ---
# Model komunitas (distilasi empero-ai di atas arsitektur Qwen3.5-9B), BUKAN rilis resmi Qwen.
# Butuh: pip install -U mlx-lm   (unduhan pertama ± 6 GB)
QWEN_MODEL_ID = "RolanDorisTech/Qwen3.8-9B-Distill-MLX-5bit"
QWEN_MODEL_TAG = "qwen3.8-9b-5bit"  # label yang disimpan di kolom model_name
QWEN_MAX_INPUT_TOKENS = 8000        # ≈ 30.000 karakter; turunkan ke 4000-5000 jika RAM/kecepatan berat
QWEN_MAX_NEW_TOKENS = 400           # lebih longgar agar sisa 'thinking' tidak memotong JSON

qwen_model = None
qwen_tokenizer = None
qwen_available = False

logging.info(f"⏳ Memuat Model {QWEN_MODEL_ID} (verifikator negatif)...")
if not MLX_IMPORT_OK:
    logging.warning(
        f"⚠️ mlx / mlx-lm tidak bisa di-import ({_MLX_IMPORT_ERR}). "
        f"Verifikasi negatif DILEWATI. Jalankan: pip install -U mlx-lm"
    )
else:
    try:
        qwen_model, qwen_tokenizer = mlx_load(QWEN_MODEL_ID)
        qwen_available = True
        logging.info("✅ Qwen MLX Berhasil Dimuat!")
    except Exception as e:
        logging.warning(f"⚠️ Qwen gagal dimuat, verifikasi negatif dilewati: {e}")
        qwen_available = False

logging.info("✅ Semua Model Siap Digunakan!\n")


# ---------------------------------------------------------
# 3. EXTRACTION & ANALYSIS HELPER FUNCTIONS
# ---------------------------------------------------------

def download_temp_image(image_url: str, save_path: str = str(DATA_DIR / "temp_media" / "temp_img.jpg")) -> str:
    """Mengunduh gambar dari URL media_urls secara sementara."""
    if not image_url or not image_url.strip():
        return None
    try:
        os.makedirs(os.path.dirname(save_path), exist_ok=True)
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        res = requests.get(image_url, headers=headers, timeout=10)
        if res.status_code == 200:
            img = Image.open(BytesIO(res.content)).convert("RGB")
            img.save(save_path)
            return save_path
    except Exception as e:
        logging.warning(f"Gagal mengunduh gambar [{image_url}]: {e}")
    return None


def extract_image_ocr(image_path: str) -> str:
    """Ekstraksi teks dari gambar menggunakan EasyOCR."""
    if not image_path or not os.path.exists(image_path):
        return ""
    try:
        results = ocr_reader.readtext(image_path, detail=0)
        return " ".join(results)
    except Exception:
        return ""


def analyze_image_sentiment(image_path: str) -> float:
    """Analisis polaritas visual menggunakan Zero-Shot CLIP."""
    if not image_path or not os.path.exists(image_path):
        return 0.0
    try:
        img = Image.open(image_path).convert("RGB")
        labels = ["foto positif atau menyenangkan", "foto negatif, sedih, atau merusak"]
        inputs = clip_processor(
            text=labels, images=img, return_tensors="pt", padding=True
        ).to(device)
        with torch.no_grad():
            outputs = clip_model(**inputs)
        probs = outputs.logits_per_image.softmax(dim=1)[0]
        pos_score, neg_score = probs[0].item(), probs[1].item()
        return pos_score - neg_score
    except Exception:
        return 0.0


def analyze_long_text_sentiment(text: str) -> dict:
    """Handling teks panjang (>350 kata) dengan teknik paragraph chunking."""
    paragraphs = [p.strip() for p in text.split("\n") if len(p.strip()) > 10]
    if not paragraphs:
        words = text.split()
        paragraphs = [" ".join(words[i : i + 300]) for i in range(0, len(words), 300)]

    scores = []
    for p in paragraphs:
        res = text_classifier(p, truncation=True, max_length=512)[0]
        score = (
            res["score"]
            if res["label"].upper() in ["LABEL_1", "POSITIVE"]
            else -res["score"]
        )
        scores.append(score)

    avg_score = sum(scores) / len(scores) if scores else 0.0
    return {
        "label": "positive" if avg_score > 0 else "negative",
        "polarity": avg_score,
    }


def analyze_text_sentiment(text: str) -> dict:
    """Analisis sentimen teks menggunakan IndoBERT."""
    if not text or not text.strip():
        return {"polarity": 0.0, "label": "neutral"}

    word_count = len(text.split())
    if word_count > 350:
        return analyze_long_text_sentiment(text)

    try:
        res = text_classifier(text, truncation=True, max_length=512)[0]
        score = (
            res["score"]
            if res["label"].upper() in ["LABEL_1", "POSITIVE"]
            else -res["score"]
        )
        return {
            "label": res["label"],
            "polarity": score,
        }
    except Exception:
        return analyze_long_text_sentiment(text)


def analyze_vlm_sarcasm(image_path: str, text_context: str) -> str:
    """Vision-Language Model Review untuk mendeteksi konflik sarkasme/ironi (opsional)."""
    if not vlm_available or not image_path or not os.path.exists(image_path):
        return "NON-SARKASME"

    try:
        image = Image.open(image_path).convert("RGB")
        prompt = (
            f"Analyze if there is sarcasm or irony between this image and the caption text: '{text_context[:500]}'. "
            f"Is the caption mocking or being sarcastic? Answer in 1 short sentence."
        )
        enc_image = vlm_model.encode_image(image)
        return vlm_model.answer_question(enc_image, prompt, vlm_tokenizer)
    except Exception as e:
        return f"VLM Error: {e}"


def truncate_for_qwen(text: str) -> str:
    """
    Potong teks berdasarkan jumlah TOKEN (bukan karakter).
    Teks yang muat dikirim utuh; jika terlalu panjang ambil 70% awal + 30% akhir.
    """
    ids = qwen_tokenizer.encode(text, add_special_tokens=False)
    if len(ids) <= QWEN_MAX_INPUT_TOKENS:
        return text
    head_n = int(QWEN_MAX_INPUT_TOKENS * 0.7)
    tail_n = QWEN_MAX_INPUT_TOKENS - head_n
    head = qwen_tokenizer.decode(ids[:head_n])
    tail = qwen_tokenizer.decode(ids[-tail_n:])
    return f"{head}\n[...bagian tengah dipotong...]\n{tail}"


def verify_negative_with_qwen(title: str, full_text: str, image_note: str = "") -> dict:
    """
    Verifikasi ulang hasil 'negative' memakai Qwen (MLX) dengan konteks utuh.

    Return dict:
      {"sentiment": "positive|negative|neutral", "confidence": float 0-1,
       "reason": str, "ok": bool}
    ok=False artinya Qwen gagal/tidak bisa di-parse -> pemanggil sebaiknya
    mempertahankan label awal.
    """
    fallback = {"sentiment": "negative", "confidence": 0.0, "reason": "", "ok": False}
    if not qwen_available or not full_text or not full_text.strip():
        return fallback

    text = truncate_for_qwen(full_text.strip())

    system_prompt = (
        "Kamu adalah analis sentimen berbahasa Indonesia yang teliti. "
        "Tugasmu menilai nada/sentimen keseluruhan sebuah konten (berita, artikel, caption, atau transkrip) "
        "dengan memahami konteks utuh, bukan sekadar kata-kata negatif yang muncul.\n"
        "Perhatikan hal-hal berikut:\n"
        "- Konten yang MELAPORKAN masalah/bencana/kejahatan secara netral atau informatif belum tentu bersentimen negatif.\n"
        "- Kata negatif bisa muncul dalam konteks positif (mis. keberhasilan mengatasi masalah, kritik yang membangun, "
        "apresiasi atas penanganan krisis).\n"
        "- Waspadai sarkasme/ironi: kalimat yang tampak positif tapi bermaksud menyindir adalah negatif.\n"
        "- Pilih 'neutral' bila konten murni informatif tanpa nada memihak.\n"
        "Jawab HANYA dengan satu objek JSON valid tanpa teks lain, format: "
        '{"sentiment": "positive|negative|neutral", "confidence": 0.0-1.0, "reason": "alasan singkat 1 kalimat"}'
    )

    user_prompt = f"Judul: {title or '-'}\n\nIsi konten:\n{text}\n"
    if image_note:
        user_prompt += f"\nCatatan visual: {image_note}\n"
    user_prompt += "\nBagaimana sentimen keseluruhan konten ini?"

    try:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        prompt_text = qwen_tokenizer.apply_chat_template(
            messages,
            tokenize=False,
            add_generation_prompt=True,
            enable_thinking=False,  # matikan mode thinking agar langsung JSON
        )

        # mlx_lm.generate: greedy secara default (setara do_sample=False)
        raw_out = mlx_generate(
            qwen_model,
            qwen_tokenizer,
            prompt=prompt_text,
            max_tokens=QWEN_MAX_NEW_TOKENS,
            verbose=False,
        ).strip()

        # Buang blok <think>...</think> bila tetap muncul
        raw_out = re.sub(r"<think>.*?</think>", "", raw_out, flags=re.DOTALL).strip()

        # Ambil objek JSON TERAKHIR (jika model sempat menulis contoh/format di awal)
        matches = re.findall(r"\{[^{}]*\}", raw_out, re.DOTALL)
        if not matches:
            logging.warning(f"    [QWEN] Output bukan JSON: {raw_out[:120]}")
            return fallback

        data = json.loads(matches[-1])
        label = str(data.get("sentiment", "")).strip().lower()
        if label not in ("positive", "negative", "neutral"):
            logging.warning(f"    [QWEN] Label tidak valid: {label}")
            return fallback

        try:
            conf = float(data.get("confidence", 0.0))
        except (TypeError, ValueError):
            conf = 0.0
        conf = max(0.0, min(1.0, conf))

        return {
            "sentiment": label,
            "confidence": conf,
            "reason": str(data.get("reason", ""))[:300],
            "ok": True,
        }
    except Exception as e:
        logging.warning(f"    [QWEN] Gagal verifikasi: {e}")
        return fallback


# ---------------------------------------------------------
# 4. MULTIMODAL FUSION PIPELINE ENGINE
# ---------------------------------------------------------

def evaluate_content_multimodal(raw_text: str, media_urls_json: str, title: str = "") -> tuple:
    """
    Menggabungkan modalitas Teks + Gambar (OCR & CLIP) [+ VLM Sarcasm Review opsional]
    + verifikasi konteks Qwen (MLX) HANYA untuk hasil 'negative'.

    Bobot fusion:
    - Teks artikel/caption + OCR: 80%
    - Gambar/visual CLIP: 20%

    Menghasilkan (sentiment_label, confidence_score, model_name).
    """
    TEXT_WEIGHT = 0.80
    IMAGE_WEIGHT = 0.20

    text_corpus = [raw_text] if raw_text else []
    text_score = None
    visual_score = None

    image_path = None
    if media_urls_json:
        try:
            urls = json.loads(media_urls_json)
            if isinstance(urls, list) and len(urls) > 0:
                first_url = urls[0]
                image_path = download_temp_image(first_url)
        except Exception:
            pass

    try:
        # 1. OCR + Visual Analysis
        if image_path and os.path.exists(image_path):
            ocr_text = extract_image_ocr(image_path)
            if ocr_text:
                text_corpus.append(ocr_text)

            visual_score = analyze_image_sentiment(image_path)

        # 2. Text Analysis (Raw Text + OCR Text)
        full_text = "\n".join([t for t in text_corpus if t])
        if full_text.strip():
            text_sens = analyze_text_sentiment(full_text)
            text_score = text_sens["polarity"]

        if text_score is None and visual_score is None:
            return "neutral", 0.0, "indobert+clip"

        # 3. Weighted Multimodal Fusion
        if text_score is not None and visual_score is not None:
            avg_polarity = (
                (text_score * TEXT_WEIGHT) +
                (visual_score * IMAGE_WEIGHT)
            )
        elif text_score is not None:
            avg_polarity = text_score
        else:
            avg_polarity = visual_score

        available_scores = [
            score for score in (text_score, visual_score)
            if score is not None
        ]
        score_variance = (
            max(available_scores) - min(available_scores)
            if len(available_scores) > 1 else 0
        )

        # 4. Penentuan Sentiment Label ('positive', 'negative', 'neutral')
        if avg_polarity > 0.05:
            final_sentiment = "positive"
        elif avg_polarity < -0.05:
            final_sentiment = "negative"
        else:
            final_sentiment = "neutral"

        used_model = "indobert+clip"

        # 5. Conflict / Sarcasm Review via VLM (hanya jika VLM aktif)
        vlm_res = ""
        if (
            vlm_available
            and image_path
            and len(available_scores) > 1
            and score_variance > 1.2
        ):
            vlm_res = analyze_vlm_sarcasm(image_path, full_text)
            used_model = "indobert+clip+moondream2"

            if any(
                w in vlm_res.upper()
                for w in ["SARCASM", "IRONY", "SARKASME"]
            ):
                final_sentiment = "negative"

        confidence_score = round(abs(avg_polarity), 4)

        # 6. Verifikasi ulang hasil NEGATIVE dengan Qwen (konteks utuh)
        #    Hanya jalan bila label akhir 'negative'; positive/neutral langsung disimpan.
        if final_sentiment == "negative" and qwen_available:
            image_note = ""
            if vlm_res and not vlm_res.startswith("VLM Error") and vlm_res != "NON-SARKASME":
                image_note = f"Review gambar-vs-teks (VLM): {vlm_res[:200]}"

            qwen_res = verify_negative_with_qwen(title, full_text, image_note)

            if qwen_res["ok"]:
                used_model = f"{used_model}+{QWEN_MODEL_TAG}"
                if qwen_res["sentiment"] != "negative":
                    logging.info(
                        f"    [QWEN] OVERRIDE negative -> {qwen_res['sentiment']} "
                        f"(conf={qwen_res['confidence']}) | {qwen_res['reason']}"
                    )
                    final_sentiment = qwen_res["sentiment"]
                    # Confidence mengikuti penilaian Qwen karena label berubah
                    confidence_score = round(qwen_res["confidence"], 4)
                else:
                    logging.info(
                        f"    [QWEN] Negative terkonfirmasi (conf={qwen_res['confidence']}) | {qwen_res['reason']}"
                    )

        logging.info(
            f"    [FUSION] Text={text_score if text_score is not None else 'N/A'} "
            f"(80%) | Image={visual_score if visual_score is not None else 'N/A'} "
            f"(20%) | Final={round(avg_polarity, 4)} | Label={final_sentiment}"
        )

        return final_sentiment, confidence_score, used_model

    finally:
        # Cleanup temp image file
        if image_path and os.path.exists(image_path):
            try:
                os.remove(image_path)
            except Exception:
                pass


# ---------------------------------------------------------
# 5. DATABASE BATCH PROCESSOR
# ---------------------------------------------------------

def process_pending_sentiment_queue():
    """Mengambil antrean status 'pending' di sentiment_results dalam batch kecil dan mengembalikannya ke loop."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        query_queue = """
            SELECT sr.id AS result_id, sr.target_id, c.title, c.raw_text, c.media_urls
            FROM sentiment_results sr
            JOIN contents c ON sr.target_id = c.id
            WHERE sr.target_type = 'content' 
              AND sr.status = 'pending'
            LIMIT 20
        """
        cursor.execute(query_queue)
        pending_items = cursor.fetchall()

        cursor.close()
        conn.close()

        total = len(pending_items)
        if total == 0:
            return 0

        logging.info(f"[*] Memproses batch ({total} konten pending)...")

        for idx, item in enumerate(pending_items, start=1):
            result_id = item["result_id"]
            target_id = item["target_id"]
            title_snippet = (
                item["title"][:40]
                if item["title"]
                else f"Content ID {target_id}"
            )

            logging.info(
                f"[{idx}/{total}] Memproses Sentimen ID {target_id}: '{title_snippet}'..."
            )

            try:
                sentiment, confidence_score, model_name = (
                    evaluate_content_multimodal(
                        item["raw_text"], item["media_urls"], item["title"] or ""
                    )
                )

                conn = get_db_connection()
                cursor = conn.cursor()

                update_query = """
                    UPDATE sentiment_results
                    SET sentiment = %s,
                        confidence_score = %s,
                        model_name = %s,
                        status = 'completed',
                        analyzed_at = NOW()
                    WHERE id = %s
                """
                cursor.execute(
                    update_query,
                    (sentiment, confidence_score, model_name, result_id),
                )
                conn.commit()

                cursor.close()
                conn.close()

                logging.info(
                    f"    [SUCCESS] Sentiment: '{sentiment}' | Confidence Score: {confidence_score} | Model: {model_name}"
                )

            except Exception as item_err:
                logging.error(
                    f"    [ERROR] Gagal memproses item ID {result_id}: {item_err}"
                )

                try:
                    conn = get_db_connection()
                    cursor = conn.cursor()
                    error_query = """
                        UPDATE sentiment_results
                        SET status = 'failed',
                            error_message = %s,
                            retry_count = retry_count + 1,
                            analyzed_at = NOW()
                        WHERE id = %s
                    """
                    cursor.execute(error_query, (str(item_err)[:500], result_id))
                    conn.commit()

                    cursor.close()
                    conn.close()
                except Exception:
                    pass

            finally:
                free_memory()

        return total

    except Error as e:
        logging.error(f"[!] Database Error: {e}")
        return 0


if __name__ == "__main__":
    logging.info("🚀 Memulai pemrosesan seluruh antrean sentimen...")
    total_processed = 0

    while True:
        processed_in_batch = process_pending_sentiment_queue()
        total_processed += processed_in_batch

        if processed_in_batch == 0:
            break

    logging.info(
        f"=== SELURUH ANTREAN SELESAI | Total Konten Diterjemahkan/Dianalisis: {total_processed} record ==="
    )