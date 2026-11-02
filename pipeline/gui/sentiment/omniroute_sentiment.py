"""
omniroute_sentiment.py
----------------------
Engine sentimen lewat OmniRoute (gateway LLM, API gaya OpenAI).
Signature sama dengan llm_sentiment.py, jadi bisa dipasang di sentiment_manager.py:

    evaluate_content_omni(title, raw_text, media_urls_json=None, model_name=None)
    evaluate_comment_omni(comment_text, model_name=None)
    -> (label, confidence, model_name_db, details)

Konfigurasi (environment variable, JANGAN taruh API key di dalam kode):
    OMNI_BASE_URL  default: http://192.0.2.20:20128/v1
    OMNI_API_KEY   wajib
    OMNI_MODEL     nama model/combo di OmniRoute (default untuk GUI)

Catatan confidence:
  - Kalau model/provider mengembalikan logprobs, confidence = probabilitas label
    (dinormalisasi di 3 label), sama maknanya dengan engine lokal.
  - Kalau tidak, confidence = OMNI_FALLBACK_CONF (default 0.80), bukan angka asli.
"""
import logging
import math
import os
import re
import time

import requests

# Memakai ulang prompt & pembersih teks dari engine lokal (hanya impor helper,
# model lokal TIDAK dimuat).
from llm_sentiment import (
    COMMENT_SYSTEM_PROMPT,
    LABELS,
    MAX_DESC_WORDS,
    MAX_TITLE_WORDS,
    SYSTEM_PROMPT,
    _limit_words,
    _sample_body,
    clean_comment,
    clean_text,
    split_raw_text,
)

BASE_URL = (os.getenv("OMNI_BASE_URL") or "http://192.0.2.20:20128/v1").rstrip("/")
API_KEY = os.getenv("OMNI_API_KEY", "")  # wajib di-set lewat environment / file .env
DEFAULT_MODEL = os.getenv("OMNI_MODEL", "")
FALLBACK_CONF = float(os.getenv("OMNI_FALLBACK_CONF", "0.80"))
TIMEOUT = int(os.getenv("OMNI_TIMEOUT", "90"))

# LLM via API punya konteks besar -> 1x panggilan untuk seluruh teks (disampling kalau panjang)
MAX_BODY_WORDS_API = 1500

_session = requests.Session()
_session.trust_env = False  # abaikan proxy sistem/VPN: server OmniRoute ada di LAN
_label_re = re.compile(r"\b(positive|neutral|negative)\b", re.I)


def _headers():
    if not API_KEY:
        raise RuntimeError("OMNI_API_KEY belum di-set (environment variable).")
    return {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}


def _extract_ids(payload) -> list:
    """Terima {"data":[...]}, {"models":[...]}, atau list langsung; item bisa str / dict."""
    items = payload
    if isinstance(payload, dict):
        items = []
        for k in ("data", "models", "result", "items"):
            if isinstance(payload.get(k), list):
                items = payload[k]
                break
    ids = []
    for it in items if isinstance(items, list) else []:
        if isinstance(it, str):
            ids.append(it)
        elif isinstance(it, dict):
            v = it.get("id") or it.get("name") or it.get("model")
            if v:
                ids.append(str(v))
    return sorted(set(ids))


def list_models() -> list:
    """Ambil daftar model/combo dari OmniRoute. Mencoba beberapa path; error menjelaskan semuanya."""
    root = BASE_URL[:-3] if BASE_URL.endswith("/v1") else BASE_URL
    urls = list(dict.fromkeys([f"{BASE_URL}/models", f"{root}/v1/models",
                               f"{root}/models", f"{root}/api/v1/models"]))
    errors = []
    for u in urls:
        try:
            r = _session.get(u, headers=_headers(), timeout=20)
        except requests.RequestException as e:
            errors.append(f"{u} -> {e}")
            continue
        if r.status_code != 200:
            errors.append(f"{u} -> HTTP {r.status_code}: {r.text[:150]!r}")
            continue
        try:
            ids = _extract_ids(r.json())
        except ValueError:
            errors.append(f"{u} -> bukan JSON: {r.text[:150]!r}")
            continue
        if ids:
            return ids
        errors.append(f"{u} -> HTTP 200 tapi daftar kosong: {r.text[:150]!r}")
    raise RuntimeError(" | ".join(errors))


def _post(payload: dict, tries: int = 3) -> dict:
    last = None
    for attempt in range(tries):
        try:
            r = _session.post(f"{BASE_URL}/chat/completions", headers=_headers(),
                              json=payload, timeout=TIMEOUT)
            if r.status_code == 400 and "logprobs" in payload:
                # provider tidak mendukung logprobs -> ulangi tanpa itu
                payload = {k: v for k, v in payload.items()
                           if k not in ("logprobs", "top_logprobs")}
                continue
            if r.status_code in (429, 500, 502, 503, 504):
                last = RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
                time.sleep(2 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            last = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"OmniRoute gagal: {last}")


def _probs_from_logprobs(choice):
    try:
        top = choice["logprobs"]["content"][0]["top_logprobs"]
    except (KeyError, IndexError, TypeError):
        return None
    mass = {l: 0.0 for l in LABELS}
    for t in top:
        tok = (t.get("token") or "").strip().lower()
        if not tok:
            continue
        hits = [l for l in LABELS if l.startswith(tok)]
        if len(hits) == 1:  # token ambigu (mis. "ne") dilewati
            mass[hits[0]] += math.exp(t["logprob"])
    total = sum(mass.values())
    if total <= 0:
        return None
    return {l: round(v / total, 3) for l, v in mass.items()}


def classify(model: str, system: str, user: str):
    """Return (label, confidence, probs_dict_or_None)."""
    if not model:
        raise RuntimeError("Nama model OmniRoute kosong.")
    data = _post({
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": 0,
        "max_tokens": 16,
        "logprobs": True,
        "top_logprobs": 10,
    })
    choice = data["choices"][0]
    content = (choice.get("message", {}).get("content") or "").strip()
    m = _label_re.search(content)
    if not m:
        raise RuntimeError(f"Jawaban model tidak dikenali: {content[:80]!r}")
    label = m.group(1).lower()
    probs = _probs_from_logprobs(choice)
    if probs:
        # pakai label hasil logprobs bila ada, supaya label & confidence konsisten
        label = max(probs, key=probs.get)
        return label, round(probs[label], 4), probs
    return label, FALLBACK_CONF, None


def _db_name(model: str) -> str:
    return ("omni:" + model.split("/")[-1])[:40]


def evaluate_content_omni(title: str, raw_text: str, media_urls_json: str = None,
                          model_name: str = None) -> tuple:
    model = model_name or DEFAULT_MODEL
    desc, body = split_raw_text(raw_text or "")
    title_c = clean_text(title or "")
    if not (title_c or desc or body):
        return "neutral", 0.0, _db_name(model), {"probs": None, "note": "teks kosong"}
    parts = []
    if title_c:
        parts.append(f"Judul: {_limit_words(title_c, MAX_TITLE_WORDS)}")
    if desc:
        parts.append(f"Deskripsi: {_limit_words(desc, MAX_DESC_WORDS)}")
    if body:
        parts.append(f"Isi/Transkrip: {_sample_body(body, MAX_BODY_WORDS_API)}")
    user = "\n".join(parts) + "\n\nSentimen (positive/neutral/negative)?"
    label, conf, probs = classify(model, SYSTEM_PROMPT, user)
    return label, conf, _db_name(model), {"probs": probs, "remote": True}


def evaluate_comment_omni(comment_text: str, model_name: str = None) -> tuple:
    model = model_name or DEFAULT_MODEL
    text = clean_comment(comment_text)
    if not text:
        return "neutral", 0.0, _db_name(model), {"probs": None, "note": "komentar kosong"}
    user = f"Komentar: {text}\n\nSentimen (positive/neutral/negative)?"
    label, conf, probs = classify(model, COMMENT_SYSTEM_PROMPT, user)
    return label, conf, _db_name(model), {"probs": probs, "remote": True}


if __name__ == "__main__":
    # Tes cepat dari terminal:  python omniroute_sentiment.py [nama-model]
    import sys
    print("BASE_URL:", BASE_URL, "| API key di-set:", bool(API_KEY))
    try:
        names = list_models()
        print(f"{len(names)} model:")
        for n in names:
            print("  -", n)
    except Exception as e:
        print("GAGAL memuat model:", e)
        names = []
    mdl = sys.argv[1] if len(sys.argv) > 1 else (names[0] if names else "")
    if mdl:
        print("Tes klasifikasi dengan", mdl, "->", classify(mdl, SYSTEM_PROMPT, "Judul: Bantuan sembako disalurkan untuk warga miskin"))