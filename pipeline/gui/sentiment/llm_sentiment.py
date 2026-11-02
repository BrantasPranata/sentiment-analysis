"""
llm_sentiment.py
----------------
Engine sentimen memakai LLM open-source yang jalan LOKAL.
Dua backend (dipilih otomatis dari nama model):
  - MLX   : model dengan "mlx" di namanya (mis. mlx-community/Qwen3.5-9B-OptiQ-4bit).
            Native Apple Silicon (M1-M4), tanpa PyTorch. Butuh:  pip install -U mlx-lm
  - Torch : model Hugging Face biasa (transformers) di CUDA / MPS / CPU.

Cara kerja (cepat & stabil):
  - Prompt instruksi (aturan framing berita Indonesia) + judul + deskripsi + transkrip.
  - TIDAK menunggu LLM "mengarang" jawaban. Cukup 1x forward pass, lalu bandingkan
    probabilitas token pertama untuk 3 label: positive / neutral / negative.
  - Confidence = probabilitas label terpilih (bukan angka karangan LLM).

Signature evaluate_content_llm() sama dengan evaluate_content_multimodal()
di process_sentiment_db.py, jadi bisa dipasang sebagai pengganti (drop-in).

Model (ubah lewat env LLM_MODEL atau argumen):
  - mlx-community/Qwen3.5-9B-OptiQ-4bit : DEFAULT di Apple Silicon bila mlx-lm terpasang
                                          (4-bit campuran, ~6.6-8.2 GB, muat di Mac 16 GB)
  - Qwen/Qwen3.5-4B            : default Torch untuk GPU/MPS (~8-9 GB di bf16)
  - Qwen/Qwen3.5-9B            : lebih akurat, ~18 GB -> butuh Mac >= 32 GB / GPU >= 24 GB
  - Qwen/Qwen2.5-7B-Instruct   : ~15 GB, butuh Mac >= 24 GB / GPU >= 16 GB
  - Qwen/Qwen2.5-3B-Instruct   : ringan & cepat, aman untuk Mac 8-16 GB
  - Qwen/Qwen2.5-1.5B-Instruct : paling ringan, default CPU (~6 GB RAM fp32)

Env opsional:
  LLM_MODEL       nama model HF
  LLM_BATCH_SIZE  (backend Torch saja) jumlah potongan transkrip per forward pass
                  (default 1 = paling aman). Backend MLX selalu 1 prompt per forward pass.

Catatan Qwen3.5: model ini default-nya "thinking". Karena kita membaca token pertama
sebagai label, thinking WAJIB dimatikan (enable_thinking=False di chat template).
Butuh transformers / mlx-lm terbaru:  pip install -U transformers accelerate mlx-lm
"""
import gc
import importlib.util
import logging
import math
import os
import platform
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

# Operasi yang belum didukung MPS jatuh ke CPU alih-alih error (harus sebelum import torch)
os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")

import torch
from transformers import AutoModelForCausalLM, AutoTokenizer

# ---------------------------------------------------------
# Deteksi device & backend
# ---------------------------------------------------------
# Device untuk backend Torch (prioritas: CUDA -> MPS (Mac) -> CPU)
if torch.cuda.is_available():
    DEVICE = "cuda"
elif getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
    DEVICE = "mps"
else:
    DEVICE = "cpu"

IS_APPLE_SILICON = sys.platform == "darwin" and platform.machine() == "arm64"
MLX_LM_AVAILABLE = importlib.util.find_spec("mlx_lm") is not None
MLX_DEFAULT_MODEL = "mlx-community/Qwen3.5-9B-OptiQ-4bit"


def is_mlx_model(name: str) -> bool:
    """Model MLX dikenali dari kata 'mlx' di nama repo (mlx-community/...)."""
    return "mlx" in (name or "").lower()


def _default_model() -> str:
    if os.getenv("LLM_MODEL"):
        return os.getenv("LLM_MODEL")
    if IS_APPLE_SILICON and MLX_LM_AVAILABLE:
        return MLX_DEFAULT_MODEL
    if DEVICE in ("cuda", "mps"):
        return "Qwen/Qwen3.5-4B"
    return "Qwen/Qwen2.5-1.5B-Instruct"


DEFAULT_MODEL = _default_model()


def _pick_dtype():
    # fp16 di Qwen rawan overflow (NaN); bf16 jauh lebih aman di MPS/CUDA modern
    if DEVICE == "cuda":
        return torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
    if DEVICE == "mps":
        return torch.bfloat16
    return torch.float32


# Batas panjang input (kata) supaya cepat
MAX_TITLE_WORDS = 40
MAX_DESC_WORDS = 100
MAX_BODY_WORDS = 380

# Penilaian transkrip per potongan
BODY_CHUNK_WORDS = 150   # panjang tiap potongan (kata)
MAX_BODY_CHUNKS = 16     # batas potongan per konten (16 x 150 = 2400 kata); lebih dari itu disampling merata
HEAD_WEIGHT = 0.5        # bobot judul+deskripsi vs rata-rata transkrip

# Jumlah potongan per forward pass (backend Torch). 1 = sekuensial (paling aman).
BATCH_SIZE = max(1, int(os.getenv("LLM_BATCH_SIZE", "1")))

LABELS = ["positive", "neutral", "negative"]

COMMENT_SYSTEM_PROMPT = (
    "Kamu adalah analis sentimen untuk komentar media sosial berbahasa Indonesia "
    "(bisa memakai bahasa gaul, singkatan, typo, dan emoji). "
    "Nilai SENTIMEN/sikap penulis komentar.\n"
    "Aturan:\n"
    "- positive: pujian, dukungan, doa baik, rasa senang/haru, terima kasih, apresiasi.\n"
    "- negative: kritik, hujatan, marah, kecewa, mengeluh, mengejek, menyindir.\n"
    "- neutral: pertanyaan, informasi, atau komentar tanpa sikap yang jelas.\n"
    "- Jika ada sarkasme atau ironi, nilai makna sebenarnya (pujian palsu = negative).\n"
    "- Doa atau simpati untuk korban bukan negatif; nilai sikap penulis, bukan topik kejadiannya.\n"
    "Jawab HANYA dengan satu kata: positive, neutral, atau negative."
)

SYSTEM_PROMPT = (
    "Kamu adalah analis sentimen untuk konten berita/video berbahasa Indonesia. "
    "Nilai SENTIMEN (nada/framing) pemberitaan terhadap peristiwanya, bukan sekadar topiknya.\n"
    "Aturan:\n"
    "- positive: kabar baik, keberhasilan, prestasi, kepedulian/bantuan, haru, apresiasi, solusi, "
    "swasembada/surplus.\n"
    "- negative: kritik, kecaman, korupsi, kegagalan, krisis, bencana, kerugian, kemarahan, "
    "penipuan, konflik, kelangkaan.\n"
    "- neutral: informatif/datar tanpa nada emosional yang jelas, atau pro-kontra seimbang.\n"
    "- Kata topik seperti miskin, sakit, katarak, buta, disabilitas BUKAN otomatis negatif. "
    "Nilai sikap pemberitaannya (contoh: berita bantuan untuk orang miskin = positive).\n"
    "- Jika ada sarkasme atau ironi, nilai makna sebenarnya.\n"
    "Jawab HANYA dengan satu kata: positive, neutral, atau negative."
)

# ---------------------------------------------------------
# Pembersihan teks ringan (mirip clean_text/split_raw_text di script pemroses)
# ---------------------------------------------------------
URL_RE = re.compile(r"https?://\S+|www\.\S+")
NOISE_BRACKET_RE = re.compile(
    r"\[(?:musik|music|tepuk tangan|applause|tertawa|laughter|sorak[^\]]*|suara[^\]]*)\]", re.I
)
TRANS_MARK_RE = re.compile(
    r"transkrip\s*/\s*ringkasan\s*:|\[\s*transkrip\s+lengkap\s*\]\s*:?", re.I
)
DESC_MARK_RE = re.compile(r"deskripsi\s*:", re.I)


def clean_text(text: str) -> str:
    if not text:
        return ""
    text = URL_RE.sub(" ", text)
    text = NOISE_BRACKET_RE.sub(" ", text)
    text = DESC_MARK_RE.sub(" ", text)
    text = TRANS_MARK_RE.sub(" ", text)
    text = text.replace("#", " ")
    return re.sub(r"\s+", " ", text).strip()


def split_raw_text(raw_text: str):
    raw = raw_text or ""
    m = TRANS_MARK_RE.search(raw)
    if m:
        head, body = raw[: m.start()], raw[m.end():]
    else:
        head, body = "", raw
    return clean_text(head), clean_text(body)


def _limit_words(text: str, n: int) -> str:
    w = text.split()
    return " ".join(w[:n])


def _sample_body(text: str, n: int) -> str:
    """Kalau transkrip panjang: ambil awal (40%) + tengah (20%) + akhir (40%)."""
    w = text.split()
    if len(w) <= n:
        return " ".join(w)
    a, m, z = int(n * 0.4), int(n * 0.2), int(n * 0.4)
    mid = len(w) // 2
    return " ".join(w[:a] + ["..."] + w[mid - m // 2: mid + m // 2] + ["..."] + w[-z:])


def _free_memory():
    """Lepas cache GPU/MPS/MLX supaya memori tidak membengkak di proses panjang."""
    try:
        if DEVICE == "mps":
            torch.mps.empty_cache()
        elif DEVICE == "cuda":
            torch.cuda.empty_cache()
    except Exception:
        pass
    if "mlx.core" in sys.modules:
        try:
            import mlx.core as mx
            if hasattr(mx, "clear_cache"):
                mx.clear_cache()
            else:
                mx.metal.clear_cache()
        except Exception:
            pass


def _get_generate_step():
    """generate_step dari mlx-lm (lokasi modul berbeda antar versi)."""
    try:
        from mlx_lm.generate import generate_step
    except ImportError:
        from mlx_lm.utils import generate_step
    return generate_step


# ---------------------------------------------------------
# Engine
# ---------------------------------------------------------
class LLMSentiment:
    def __init__(self, model_name: str = None):
        self.model_name = model_name or DEFAULT_MODEL
        self.backend = "mlx" if is_mlx_model(self.model_name) else "torch"
        if self.backend == "mlx" and not (IS_APPLE_SILICON and MLX_LM_AVAILABLE):
            raise RuntimeError(
                f"Model MLX '{self.model_name}' butuh Apple Silicon + mlx-lm "
                "(pip install -U mlx-lm). Pilih model Hugging Face biasa jika bukan di Mac M-series."
            )
        self.tok = None        # tokenizer (Torch: HF tokenizer; MLX: TokenizerWrapper)
        self.hf_tok = None     # tokenizer HF murni (untuk chat template & encode)
        self.model = None
        self.label_ids = None  # list[list[int]] token pertama tiap label (varian huruf)
        self._prompt_checked = False
        self._exec = None      # MLX: semua operasi dijalankan di 1 thread khusus

    # -- thread khusus MLX -------------------------------------------
    def _on_mlx_thread(self, fn, *args):
        """
        MLX terikat ke thread tempat model dimuat. Karena GUI membuat thread baru untuk tiap
        proses ulang, seluruh load + inference dikirim ke satu thread tetap ini.
        """
        if self._exec is None:
            self._exec = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mlx")
        return self._exec.submit(fn, *args).result()

    # -- loading ------------------------------------------------------
    def _from_pretrained(self, dtype, **kw):
        """Muat model Torch; coba 'dtype' (transformers baru) lalu 'torch_dtype' (lama).
        Qwen3.5 adalah checkpoint multimodal; kalau AutoModelForCausalLM menolak,
        coba AutoModelForImageTextToText (tetap dipakai sebagai teks-saja)."""
        classes = [AutoModelForCausalLM]
        try:
            from transformers import AutoModelForImageTextToText
            classes.append(AutoModelForImageTextToText)
        except ImportError:
            pass

        last_err = None
        for cls in classes:
            for key in ("dtype", "torch_dtype"):
                try:
                    return cls.from_pretrained(self.model_name, **{key: dtype}, **kw)
                except TypeError as e:      # nama argumen tidak dikenal -> coba yang lain
                    last_err = e
                    continue
                except ValueError as e:     # kelas Auto tidak cocok -> coba kelas berikut
                    last_err = e
                    break
        raise last_err

    def _load_torch(self):
        self.tok = AutoTokenizer.from_pretrained(self.model_name)
        self.tok.padding_side = "left"  # wajib untuk batching: token terakhir = akhir prompt
        if self.tok.pad_token is None:
            self.tok.pad_token = self.tok.eos_token
        self.hf_tok = self.tok

        dtype = _pick_dtype()
        try:
            # muat langsung ke device (hindari salinan ganda di RAM)
            self.model = self._from_pretrained(dtype, device_map={"": DEVICE})
        except (RuntimeError, ImportError, ValueError, TypeError) as e:
            logging.warning(f"device_map gagal ({e}); fallback ke load biasa + .to({DEVICE})")
            self.model = self._from_pretrained(dtype)
            self.model.to(DEVICE)
        self.model.eval()
        return dtype

    def _load_mlx(self):
        from mlx_lm import load as mlx_load
        self.model, self.tok = mlx_load(self.model_name)
        # TokenizerWrapper membungkus tokenizer HF di atribut _tokenizer
        self.hf_tok = getattr(self.tok, "_tokenizer", self.tok)

    def _build_label_ids(self):
        self.label_ids = []
        for lab in LABELS:
            ids = {self.hf_tok.encode(v, add_special_tokens=False)[0]
                   for v in (lab, lab.capitalize(), lab.upper())}
            self.label_ids.append(sorted(ids))
        flat = [i for grp in self.label_ids for i in grp]
        if len(flat) != len(set(flat)):
            raise RuntimeError(
                f"Token awal label tumpang tindih di tokenizer {self.model_name}; "
                "pakai model lain atau ganti label."
            )

    def load(self):
        if self.model is not None:
            return
        where = "MLX / Apple Silicon" if self.backend == "mlx" else f"Torch, device={DEVICE}"
        logging.info(f"⏳ Memuat LLM lokal: {self.model_name} ({where}) ...")
        if self.backend == "mlx":
            self._on_mlx_thread(self._load_mlx)
            info = "backend=mlx, 1 prompt/forward pass"
        else:
            dtype = self._load_torch()
            info = f"dtype={dtype}, batch={BATCH_SIZE}"
        self._build_label_ids()
        logging.info(f"✅ LLM siap ({info}).")

    def unload(self):
        """Lepas model dari memori (dipakai saat ganti model)."""
        self.model = None
        self.tok = None
        self.hf_tok = None
        if self._exec is not None:
            self._exec.shutdown(wait=False)
            self._exec = None
        gc.collect()
        _free_memory()

    # -- prompt -------------------------------------------------------
    def _build_prompt(self, title, desc, body, system=SYSTEM_PROMPT,
                      body_label="Isi/Transkrip") -> str:
        parts = []
        if title:
            parts.append(f"Judul: {_limit_words(title, MAX_TITLE_WORDS)}")
        if desc:
            parts.append(f"Deskripsi: {_limit_words(desc, MAX_DESC_WORDS)}")
        if body:
            parts.append(f"{body_label}: {_sample_body(body, MAX_BODY_WORDS)}")
        user = "\n".join(parts) + "\n\nSentimen (positive/neutral/negative)?"
        msgs = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        # enable_thinking=False: Qwen3.5 tidak boleh "berpikir" dulu, karena kita membaca
        # token pertama sebagai label. Template lama (Qwen2.5) mengabaikan argumen ini.
        try:
            prompt = self.hf_tok.apply_chat_template(
                msgs, tokenize=False, add_generation_prompt=True, enable_thinking=False
            )
        except TypeError:
            prompt = self.hf_tok.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True)

        if not self._prompt_checked:
            self._prompt_checked = True
            tail = prompt.rstrip()
            logging.info(f"Ujung prompt: {tail[-60:]!r}")
            if tail.endswith("<think>"):
                logging.warning(
                    "Prompt berakhir di '<think>' yang terbuka: mode thinking masih aktif, "
                    "hasil label tidak valid. Cek versi transformers / chat template model."
                )
        return prompt

    # -- inference ----------------------------------------------------
    @staticmethod
    def _scores_to_probs(scores: list) -> dict:
        """scores = log-prob gabungan per label -> softmax hanya di 3 label."""
        m = max(scores)
        exps = [math.exp(s - m) for s in scores]
        z = sum(exps)
        return {l: round(e / z, 3) for l, e in zip(LABELS, exps)}

    @torch.inference_mode()
    def _probs_batch_torch(self, prompts: list) -> list:
        """Forward pass Torch untuk beberapa prompt; return list[dict label->prob]."""
        inputs = self.tok(prompts, return_tensors="pt", padding=len(prompts) > 1).to(DEVICE)
        try:
            out = self.model(**inputs, logits_to_keep=1)  # hanya logit posisi terakhir
        except TypeError:
            out = self.model(**inputs)
        logits = out.logits[:, -1].float()                      # (B, vocab)
        logp = torch.log_softmax(logits, dim=-1)
        scores = torch.stack(
            [torch.logsumexp(logp[:, ids], dim=-1) for ids in self.label_ids], dim=-1
        )                                                       # (B, 3)
        probs = torch.softmax(scores, dim=-1).tolist()          # normalisasi hanya di 3 label
        return [{l: round(p, 3) for l, p in zip(LABELS, row)} for row in probs]

    def _mlx_probs_one(self, prompt: str) -> dict:
        """Forward pass MLX untuk 1 prompt (dijalankan di thread MLX khusus)."""
        import mlx.core as mx
        import numpy as np

        ids = self.hf_tok.encode(prompt, add_special_tokens=False)
        arr = mx.array(ids)

        logprobs = None
        try:
            # generate_step memproses prompt per potongan dan hanya menghitung logit token terakhir
            # (hemat memori untuk prompt panjang). Ambil log-prob token pertama saja.
            generate_step = _get_generate_step()
            for _tok, lp in generate_step(arr, self.model, max_tokens=1):
                logprobs = lp
                break
        except Exception as e:
            logging.debug(f"generate_step gagal ({e}); fallback ke forward langsung")
            logprobs = None

        if logprobs is None:
            logits = self.model(arr[None])[:, -1, :]
            logprobs = logits - mx.logsumexp(logits, axis=-1, keepdims=True)

        lp = np.array(logprobs.astype(mx.float32)).reshape(-1)
        scores = [float(np.logaddexp.reduce(lp[grp])) for grp in self.label_ids]
        return self._scores_to_probs(scores)

    def _probs_batch(self, prompts: list) -> list:
        if self.backend == "mlx":
            return [self._on_mlx_thread(self._mlx_probs_one, p) for p in prompts]
        return self._probs_batch_torch(prompts)

    def classify(self, title, desc, body, system=SYSTEM_PROMPT, body_label="Isi/Transkrip"):
        """Return (label, confidence, probs_dict)."""
        self.load()
        prompt = self._build_prompt(title, desc, body, system, body_label)
        probs = self._probs_batch([prompt])[0]
        best = max(LABELS, key=lambda l: probs[l])
        return best, round(probs[best], 4), probs

    def classify_many(self, items: list, system=SYSTEM_PROMPT, body_label="Isi/Transkrip") -> list:
        """items = [(title, desc, body), ...] -> list[probs_dict], diproses per BATCH_SIZE."""
        self.load()
        prompts = [self._build_prompt(t, d, b, system, body_label) for t, d, b in items]
        results = []
        step = 1 if self.backend == "mlx" else BATCH_SIZE
        for i in range(0, len(prompts), step):
            results.extend(self._probs_batch(prompts[i:i + step]))
        return results


def _chunk_words(text: str) -> list:
    """Potong teks jadi potongan ~BODY_CHUNK_WORDS kata. Sisa pendek digabung ke potongan terakhir."""
    w = (text or "").split()
    if not w:
        return []
    chunks = [w[i:i + BODY_CHUNK_WORDS] for i in range(0, len(w), BODY_CHUNK_WORDS)]
    if len(chunks) > 1 and len(chunks[-1]) < 20:
        chunks[-2].extend(chunks.pop())
    if len(chunks) > MAX_BODY_CHUNKS:  # sampling merata sepanjang teks
        step = len(chunks) / MAX_BODY_CHUNKS
        chunks = [chunks[int(i * step)] for i in range(MAX_BODY_CHUNKS)]
    return [" ".join(c) for c in chunks]


def _mean_probs(plist: list) -> dict:
    return {l: sum(p[l] for p in plist) / len(plist) for l in LABELS}


def classify_full(eng: "LLMSentiment", title: str, desc: str, body: str):
    """
    Nilai judul+deskripsi (1 panggilan) dan SELURUH transkrip per potongan,
    lalu gabungkan: HEAD_WEIGHT * head + (1-HEAD_WEIGHT) * rata-rata potongan.
    Return (label, confidence, probs, details).
    """
    head = eng.classify(title, desc, "")[2] if (title or desc) else None
    chunks = _chunk_words(body)
    body_probs = eng.classify_many([("", "", c) for c in chunks]) if chunks else []
    body_mean = _mean_probs(body_probs) if body_probs else None

    if head and body_mean:
        probs = {l: HEAD_WEIGHT * head[l] + (1 - HEAD_WEIGHT) * body_mean[l] for l in LABELS}
    else:
        probs = head or body_mean
    best = max(LABELS, key=lambda l: probs[l])
    rounded = {l: round(p, 3) for l, p in probs.items()}
    details = {
        "probs": rounded,
        "head": {l: round(p, 3) for l, p in head.items()} if head else None,
        "body": {l: round(p, 3) for l, p in body_mean.items()} if body_mean else None,
        "chunks": len(chunks),
    }
    _free_memory()
    return best, round(probs[best], 4), rounded, details


_engine = None
_engine_lock = threading.Lock()


def get_engine(model_name: str = None) -> LLMSentiment:
    """Singleton; model dimuat ulang kalau nama model berubah."""
    global _engine
    with _engine_lock:
        name = model_name or DEFAULT_MODEL
        if _engine is None or _engine.model_name != name:
            if _engine is not None:       # lepas model lama dulu sebelum memuat yang baru
                _engine.unload()
                _engine = None
                gc.collect()
                _free_memory()
            _engine = LLMSentiment(name)
        _engine.load()
        return _engine


def evaluate_content_llm(title: str, raw_text: str, media_urls_json: str = None,
                         model_name: str = None) -> tuple:
    """
    Drop-in untuk evaluate_content_multimodal():
    return (sentiment_label, confidence_score, model_name_db, details)
    Catatan: LLM teks-saja, thumbnail/OCR/CLIP tidak dipakai.
    """
    eng = get_engine(model_name)
    desc, body = split_raw_text(raw_text or "")
    title_c = clean_text(title or "")
    if not (title_c or desc or body):
        return "neutral", 0.0, _db_name(eng.model_name), {"probs": None, "note": "teks kosong"}
    label, conf, _, details = classify_full(eng, title_c, desc, body)
    return label, conf, _db_name(eng.model_name), details


def _db_name(model_name: str) -> str:
    # dipendekkan supaya aman untuk kolom model_name varchar kecil
    return ("llm:" + model_name.split("/")[-1])[:40]


def clean_comment(text: str) -> str:
    """Bersihkan URL & spasi berlebih; emoji dan huruf besar/kecil dipertahankan."""
    if not text:
        return ""
    text = URL_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def evaluate_comment_llm(comment_text: str, model_name: str = None) -> tuple:
    """
    Sentimen 1 komentar (target_type='comment').
    return (sentiment_label, confidence_score, model_name_db, details)
    """
    eng = get_engine(model_name)
    text = clean_comment(comment_text)
    if not text:
        return "neutral", 0.0, _db_name(eng.model_name), {"probs": None, "note": "komentar kosong"}
    label, conf, probs = eng.classify("", "", text, system=COMMENT_SYSTEM_PROMPT,
                                      body_label="Komentar")
    return label, conf, _db_name(eng.model_name), {"probs": probs}


# ---------------------------------------------------------
# Tes cepat:  python llm_sentiment.py
# ---------------------------------------------------------
if __name__ == "__main__":
    import time

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    samples = [
        ("Pemerintah salurkan bantuan operasi katarak gratis untuk warga miskin", "", "positive"),
        ("Korupsi dana desa rugikan negara miliaran rupiah, warga marah", "", "negative"),
        ("Harga beras hari ini di pasar induk Cipinang", "", "neutral"),
    ]
    t0 = time.time()
    for title, raw, expected in samples:
        label, conf, model, det = evaluate_content_llm(title, raw)
        ok = "OK " if label == expected else "?? "
        print(f"{ok}{label:8s} conf={conf:.3f} probs={det['probs']} | {title[:60]}")
    for c in ["Semoga cepat sembuh ya, aamiin 🙏", "Katanya sih bagus... bagus buat kantong pejabat 🙄"]:
        label, conf, model, det = evaluate_comment_llm(c)
        print(f"   {label:8s} conf={conf:.3f} probs={det['probs']} | {c}")
    eng = get_engine()
    print(f"model={model} backend={eng.backend} device={DEVICE} waktu={time.time() - t0:.1f}s")