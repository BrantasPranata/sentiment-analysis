"""
Sentiment Manager GUI
---------------------
Letakkan file ini di folder yang SAMA dengan llm_sentiment.py (folder "gui/sentiment").
Script pemroses lama (process_sentiment_db.py) hanya dibutuhkan kalau memakai engine
"Pipeline lama". Jalankan dari root project:

    python gui/sentiment/sentiment_manager.py

Fitur:
  - Melihat seluruh tabel sentiment_results (+ judul konten / isi komentar), dengan filter.
    Mendukung target_type 'content' (tabel contents) dan 'comment' (tabel comments,
    kolom comment_text; sentiment_results.target_id = comments.id).
  - Kolom "Published At" (contents.published_at) untuk melihat mana berita yang benar-benar baru.
    Klik header kolom mana pun untuk sort (klik lagi untuk membalik arah). Sort dilakukan di
    database sehingga berlaku untuk SELURUH data, bukan hanya baris yang tampil. Nilai kosong
    (NULL, mis. komentar) selalu di paling bawah.
  - Tombol 1 "ROLLBACK -> PENDING"  : status dikembalikan ke 'pending' supaya
                                       diproses ulang oleh batch auto berikutnya.
  - Tombol 2 "SENTIMEN ULANG LOKAL" : langsung dihitung ulang sekarang juga
                                       memakai model lokal (LLM lokal atau pipeline lama).

Model LLM: ID Hugging Face yang mengandung "mlx" (mis. RolanDorisTech/Qwen3.8-9B-Distill-MLX-5bit
atau mlx-community/Qwen3.5-9B-OptiQ-4bit) otomatis memakai backend MLX (Apple Silicon,
butuh: pip install -U mlx-lm). Model lain memakai backend Torch (transformers).
CATATAN: pastikan deteksi "mlx" di llm_sentiment.py tidak peka huruf besar/kecil
(pakai model_id.lower()), karena ID model default memakai "MLX" huruf besar.

Kompatibel macOS: tombol berwarna diganti ttk.Button, font menyesuaikan OS, dan Cmd+A
untuk pilih semua.
"""
import importlib
import queue
import os
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

# Root project (1 tingkat di atas folder script ini), sama seperti script pemroses
sys.path.append(str(Path(__file__).resolve().parents[2]))
sys.path.append(str(Path(__file__).resolve().parent))

from config.config import get_db_connection
from mysql.connector import Error

os.environ["PURE_PYTHON_REGEX"] = "1"
os.environ["TRANSFORMERS_NO_ADVISORY_WARNINGS"] = "1"
os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
# HF_TOKEN (opsional) dibaca dari environment / file .env, tidak ditulis di kode.
# Izinkan fallback ke CPU untuk operasi yang belum didukung MPS
os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")

# Nama modul script pemroses (tanpa .py). Diimpor LAZY saat tombol 2 ditekan,
# karena saat di-import ia langsung memuat semua model ML (berat).
PROCESSOR_MODULE = "sentiment.process_sentiment_db"  # pipeline/sentiment/process_sentiment_db.py

CHUNK = 500  # jumlah ID per query UPDATE

# Penyesuaian tampilan per OS (macOS mengabaikan warna tk.Button dan tidak punya Segoe UI/Consolas)
IS_MAC = sys.platform == "darwin"
FONT_UI = "Helvetica Neue" if IS_MAC else "Segoe UI"
FONT_MONO = "Menlo" if IS_MAC else "Consolas"


def make_button(parent, text, command, bg, active_bg):
    """Di Mac pakai ttk.Button (warna bg tidak didukung aqua); di OS lain tombol berwarna."""
    if IS_MAC:
        return ttk.Button(parent, text=text, command=command)
    return tk.Button(parent, text=text, command=command, bg=bg, fg="white",
                     activebackground=active_bg, activeforeground="white",
                     font=(FONT_UI, 10, "bold"), padx=10, pady=4)


# Engine sentimen ulang lokal
ENGINE_LLM = "LLM lokal (teks)"
ENGINE_OLD = "Pipeline lama (IndoBERT+CLIP)"
ENGINE_OMNI = "OmniRoute (API)"
# Model OmniRoute: dropdown hanya menampilkan daftar pilihan di bawah (urut kira-kira dari
# termurah ke termahal). Klik "Muat semua model" untuk memuat SELURUH daftar dari server.
# Nama lain tetap bisa diketik langsung di kotak model.
# Env: OMNI_API_KEY (wajib), OMNI_BASE_URL (default http://192.0.2.20:20128/v1), OMNI_MODEL.
OMNI_PRESETS = [
    # Tier 0: jalan di server sendiri (tanpa biaya token)
    "lmstudio/qwen3.8-9b-distill",
    "lmstudio/qwen3.8-14b-instruct-turbo",
    # Tier 1: sangat murah
    "openai/gpt-5-nano",
    "gemini/gemini-2.5-flash-lite",
    "openai/gpt-4.1-nano",
    "openai/gpt-4o-mini",
    "openai/gpt-5.4-nano",
    "gemini/gemini-3.1-flash-lite",
    "openai/gpt-5-mini",
    # Tier 2: menengah
    "gemini/gemini-2.5-flash",
    "openai/gpt-4.1-mini",
    "gemini/gemini-3-flash-preview",
    "openai/gpt-5.4-mini",
    # Tier 3: mahal
    "gemini/gemini-3.1-pro-preview",
    "openai/gpt-4o",
    "openai/gpt-5.4",
]
OMNI_DEFAULT = "gemini/gemini-2.5-flash-lite"  # terpilih otomatis saat engine OmniRoute dipilih
OMNI_ALL = []  # diisi tombol "Muat semua model"
LLM_PRESETS = [
    "RolanDorisTech/Qwen3.8-9B-Distill-MLX-5bit",  # default Mac Apple Silicon: MLX 5-bit (~6 GB), butuh mlx-lm. Model komunitas (distilasi), bukan rilis resmi Qwen
    "mlx-community/Qwen3.5-9B-OptiQ-4bit",  # MLX 4-bit (~6.6-8.2 GB), pembanding berlisensi resmi Qwen, butuh mlx-lm
    "Qwen/Qwen3.5-4B",              # backend Torch (MPS), ~8-9 GB di bf16
    "Qwen/Qwen2.5-3B-Instruct",     # ringan & cepat, pembanding yang aman
    "Qwen/Qwen2.5-1.5B-Instruct",   # paling ringan (CPU / RAM kecil)
    "Qwen/Qwen2.5-7B-Instruct",     # ~15 GB, butuh Mac >= 24 GB
    "Qwen/Qwen3.5-9B",              # ~18 GB, butuh Mac >= 32 GB
]

COLUMNS = (
    ("id", "ID", 60),
    ("type", "Tipe", 70),
    ("target", "Target ID", 75),
    ("title", "Judul / Komentar", 420),
    ("sentiment", "Sentimen", 85),
    ("conf", "Conf", 65),
    ("model", "Model", 130),
    ("status", "Status", 85),
    ("retry", "Retry", 50),
    ("published", "Published At", 140),
    ("analyzed", "Analyzed At", 140),
)

# Kolom tampilan -> ekspresi SQL untuk ORDER BY (sort dilakukan di database)
SORT_SQL = {
    "id": "sr.id",
    "type": "sr.target_type",
    "target": "sr.target_id",
    "title": "COALESCE(c.title, cm.comment_text)",
    "sentiment": "sr.sentiment",
    "conf": "sr.confidence_score",
    "model": "sr.model_name",
    "status": "sr.status",
    "retry": "sr.retry_count",
    "published": "c.published_at",
    "analyzed": "sr.analyzed_at",
}
# Klik pertama pada kolom-kolom ini langsung urut dari yang TERBARU/TERBESAR
DESC_FIRST = {"id", "published", "analyzed", "conf", "retry", "target"}

STATUS_COLORS = {
    "pending": "#fff4cc",
    "failed": "#ffd6d6",
    "completed": "#e3f6e3",
}

# SELECT dasar (dipakai refresh() dan _refresh_row())
BASE_SELECT = """
    SELECT sr.id, sr.target_type, sr.target_id,
           COALESCE(c.title, cm.comment_text) AS title, sr.sentiment,
           sr.confidence_score, sr.model_name, sr.status, sr.retry_count,
           c.published_at AS published_at, sr.analyzed_at
    FROM sentiment_results sr
    LEFT JOIN contents c
           ON sr.target_type = 'content' AND sr.target_id = c.id
    LEFT JOIN comments cm
           ON sr.target_type = 'comment' AND sr.target_id = cm.id
"""


# ---------------------------------------------------------
# DB helpers
# ---------------------------------------------------------
def db_fetch_all(query, params=None):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(dictionary=True)
        cur.execute(query, params or ())
        rows = cur.fetchall()
        cur.close()
        return rows
    finally:
        if conn is not None and conn.is_connected():
            conn.close()


def db_execute(query, params=None):
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(query, params or ())
        conn.commit()
        n = cur.rowcount
        cur.close()
        return n
    except Error:
        if conn is not None and conn.is_connected():
            conn.rollback()
        raise
    finally:
        if conn is not None and conn.is_connected():
            conn.close()


UPDATE_OK = """
    UPDATE sentiment_results
    SET sentiment = %s, confidence_score = %s, model_name = %s,
        status = 'completed', analyzed_at = NOW()
    WHERE id = %s
"""
UPDATE_FAIL = """
    UPDATE sentiment_results
    SET status = 'failed', error_message = %s,
        retry_count = retry_count + 1, analyzed_at = NOW()
    WHERE id = %s
"""


def evaluate_comment_old(psd, text):
    """Komentar dengan pipeline lama: model teks IndoBERT + lexicon (tanpa gambar)."""
    t = psd.clean_text(text or "")
    if not t:
        return "neutral", 0.0, "indobert+lex", {"note": "komentar kosong"}
    score = psd.classify_texts([t])[0]
    lex, p_hits, n_hits = psd.lexicon_score(t)
    present = {"body": score}
    fused = score
    if lex is not None:
        present["lex"] = lex
        fused = 0.7 * score + 0.3 * lex
    wsum = sum(psd.COMPONENT_WEIGHTS[k] for k in present)
    label = psd.decide_label(fused)
    conf = psd.calibrated_confidence(label, fused, present, wsum)
    return label, conf, "indobert+lex", {
        "fused": round(fused, 4), "model": round(score, 3),
        "lex_hits": {"pos": p_hits, "neg": n_hits},
    }


# ---------------------------------------------------------
# GUI
# ---------------------------------------------------------
class SentimentManager(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Sentiment Manager - Rollback & Sentimen Ulang")
        self.geometry("1420x720")
        self.minsize(1000, 560)

        self.ui_q = queue.Queue()
        self.processor = None
        self.running = False
        self.stop_flag = threading.Event()
        # Sort aktif (server-side). Default sama seperti sebelumnya: ID terbaru dulu.
        self._sort_key = "id"
        self._sort_desc = True

        self._build_ui()
        self._update_heading_arrows()
        self.after(100, self._poll_queue)
        self.refresh()

    # ---------------- UI ----------------
    def _build_ui(self):
        # --- Filter bar ---
        f = ttk.LabelFrame(self, text="Filter")
        f.pack(fill="x", padx=8, pady=(8, 4))

        self.var_type = tk.StringVar(value="semua")
        self.var_status = tk.StringVar(value="semua")
        self.var_sent = tk.StringVar(value="semua")
        self.var_kw = tk.StringVar()
        self.var_limit = tk.IntVar(value=1000)

        def add(label, widget, col):
            ttk.Label(f, text=label).grid(row=0, column=col * 2, padx=(8, 2), pady=6, sticky="e")
            widget.grid(row=0, column=col * 2 + 1, padx=(0, 6), pady=6, sticky="w")

        add("Tipe", ttk.Combobox(f, textvariable=self.var_type, width=9, state="readonly",
                                 values=["semua", "content", "comment"]), 0)
        add("Status", ttk.Combobox(f, textvariable=self.var_status, width=10, state="readonly",
                                   values=["semua", "pending", "completed", "failed"]), 1)
        add("Sentimen", ttk.Combobox(f, textvariable=self.var_sent, width=10, state="readonly",
                                     values=["semua", "positive", "neutral", "negative"]), 2)
        ent = ttk.Entry(f, textvariable=self.var_kw, width=28)
        ent.bind("<Return>", lambda e: self.refresh())
        add("Cari judul/komentar/ID", ent, 3)
        add("Limit", ttk.Spinbox(f, from_=50, to=100000, increment=500,
                                 textvariable=self.var_limit, width=8), 4)
        ttk.Button(f, text="Refresh", command=self.refresh).grid(row=0, column=10, padx=8)

        # --- Summary ---
        self.lbl_summary = ttk.Label(self, text="", foreground="#444")
        self.lbl_summary.pack(fill="x", padx=10)

        # --- Tree ---
        wrap = ttk.Frame(self)
        wrap.pack(fill="both", expand=True, padx=8, pady=4)
        self.tree = ttk.Treeview(wrap, columns=[c[0] for c in COLUMNS],
                                 show="headings", selectmode="extended")
        for key, text, width in COLUMNS:
            self.tree.heading(key, text=text, command=lambda k=key: self._sort_by(k))
            self.tree.column(key, width=width, anchor="w" if key == "title" else "center",
                             stretch=(key == "title"))
        vs = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        hs = ttk.Scrollbar(wrap, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vs.grid(row=0, column=1, sticky="ns")
        hs.grid(row=1, column=0, sticky="ew")
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)
        for st, color in STATUS_COLORS.items():
            self.tree.tag_configure(st, background=color)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._update_sel_label())
        self.tree.bind("<Control-a>", lambda e: (self.select_all(), "break")[1])
        self.tree.bind("<Command-a>", lambda e: (self.select_all(), "break")[1])  # macOS

        # --- Engine sentimen ulang lokal ---
        ef = ttk.LabelFrame(self, text="Engine untuk tombol 2 (sentimen ulang lokal)")
        ef.pack(fill="x", padx=8, pady=(0, 4))
        self.var_engine = tk.StringVar(value=ENGINE_LLM)
        self.var_model = tk.StringVar(value=LLM_PRESETS[0])
        ttk.Label(ef, text="Engine").grid(row=0, column=0, padx=(8, 2), pady=6)
        cb_engine = ttk.Combobox(ef, textvariable=self.var_engine, width=30, state="readonly",
                                 values=[ENGINE_LLM, ENGINE_OLD, ENGINE_OMNI])
        cb_engine.grid(row=0, column=1, padx=(0, 10))
        cb_engine.bind("<<ComboboxSelected>>", lambda e: self._on_engine_change())
        ttk.Label(ef, text="Model (HF ID; 'mlx' = backend MLX. OmniRoute: nama model/combo)").grid(
            row=0, column=2, padx=(8, 2))
        self.cb_model = ttk.Combobox(ef, textvariable=self.var_model, width=42, values=LLM_PRESETS)
        self.cb_model.grid(row=0, column=3, padx=(0, 8))
        ttk.Button(ef, text="Muat semua model", command=self._load_omni_models).grid(
            row=0, column=4, padx=(0, 8))

        # --- Action bar ---
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=8, pady=(2, 4))
        ttk.Button(bar, text="Pilih Semua", command=self.select_all).pack(side="left")
        ttk.Button(bar, text="Hapus Pilihan", command=lambda: self.tree.selection_remove(
            self.tree.selection())).pack(side="left", padx=4)
        self.lbl_sel = ttk.Label(bar, text="Dipilih: 0")
        self.lbl_sel.pack(side="left", padx=10)

        self.var_reset = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="Reset retry_count & error_message saat rollback",
                        variable=self.var_reset).pack(side="left", padx=10)

        self.btn_stop = ttk.Button(bar, text="Stop", command=self.stop_run, state="disabled")
        self.btn_stop.pack(side="right")
        self.btn_rerun = make_button(bar, "2. SENTIMEN ULANG LOKAL (sekarang)",
                                     self.rerun_local, "#1f6feb", "#1858c0")
        self.btn_rerun.pack(side="right", padx=6)
        self.btn_rollback = make_button(bar, "1. ROLLBACK -> PENDING",
                                        self.rollback_pending, "#d9822b", "#b36a1f")
        self.btn_rollback.pack(side="right", padx=6)

        # --- Progress + log ---
        self.progress = ttk.Progressbar(self, mode="determinate")
        self.progress.pack(fill="x", padx=8)
        self.log = tk.Text(self, height=7, state="disabled", bg="#111", fg="#ddd",
                           font=(FONT_MONO, 10))
        self.log.pack(fill="x", padx=8, pady=(4, 8))

    def _on_engine_change(self):
        eng = self.var_engine.get()
        if eng == ENGINE_OMNI:
            self.cb_model.config(values=OMNI_ALL or OMNI_PRESETS)
            self.var_model.set(os.getenv("OMNI_MODEL", "") or OMNI_DEFAULT)
        elif eng == ENGINE_LLM:
            self.cb_model.config(values=LLM_PRESETS)
            self.var_model.set(LLM_PRESETS[0])

    def _load_omni_models(self):
        """Ambil daftar model dari OmniRoute di thread; hasil dikirim lewat queue (aman untuk Tk)."""
        self._log("[OmniRoute] Memuat daftar model ...")

        def work():
            try:
                import omniroute_sentiment as omni
                self.ui_q.put(("omni_models", omni.list_models()))
            except Exception as e:
                self.ui_q.put(("log", f"[ERROR] Gagal memuat daftar model OmniRoute: {e}"))
        threading.Thread(target=work, daemon=True).start()

    def _set_omni_models(self, names):
        OMNI_ALL[:] = names
        self.var_engine.set(ENGINE_OMNI)
        self.cb_model.config(values=names)
        if names and self.var_model.get() not in names:
            self.var_model.set(names[0])
        self._log(f"[OmniRoute] {len(names)} model dimuat.")

    # ---------------- Data ----------------
    def _build_where(self):
        where, params = ["1=1"], []
        if self.var_type.get() != "semua":
            where.append("sr.target_type = %s")
            params.append(self.var_type.get())
        if self.var_status.get() != "semua":
            where.append("sr.status = %s")
            params.append(self.var_status.get())
        if self.var_sent.get() != "semua":
            where.append("sr.sentiment = %s")
            params.append(self.var_sent.get())
        kw = self.var_kw.get().strip()
        if kw:
            if kw.isdigit():
                where.append("(COALESCE(c.title, cm.comment_text) LIKE %s OR sr.target_id = %s OR sr.id = %s)")
                params += [f"%{kw}%", int(kw), int(kw)]
            else:
                where.append("COALESCE(c.title, cm.comment_text) LIKE %s")
                params.append(f"%{kw}%")
        return " AND ".join(where), params

    def _order_by_sql(self):
        """ORDER BY dari kolom yang sedang di-sort. NULL selalu di paling bawah."""
        expr = SORT_SQL.get(self._sort_key, "sr.id")
        direction = "DESC" if self._sort_desc else "ASC"
        # (expr IS NULL) = 0 untuk data ada, 1 untuk NULL -> NULL selalu terakhir
        return f"(({expr}) IS NULL), {expr} {direction}, sr.id DESC"

    def refresh(self):
        where, params = self._build_where()
        try:
            limit = max(1, int(self.var_limit.get()))
        except Exception:
            limit = 1000
        query = f"""
            {BASE_SELECT}
            WHERE {where}
            ORDER BY {self._order_by_sql()}
            LIMIT {limit}
        """
        try:
            rows = db_fetch_all(query, params)
            counts = db_fetch_all(
                "SELECT status, COUNT(*) AS n FROM sentiment_results GROUP BY status"
            )
        except Error as e:
            messagebox.showerror("Database Error", str(e))
            return

        self.tree.delete(*self.tree.get_children())
        for r in rows:
            self.tree.insert("", "end", iid=str(r["id"]), values=self._row_values(r),
                             tags=(r["status"] or "",))
        summary = " | ".join(f"{c['status']}: {c['n']}" for c in counts)
        self.lbl_summary.config(
            text=f"Ditampilkan {len(rows)} baris  •  Total di DB -> {summary}"
        )
        self._update_sel_label()

    @staticmethod
    def _fmt_dt(v):
        if v is None or v == "":
            return "-"
        return str(v)[:19]  # YYYY-MM-DD HH:MM:SS

    @staticmethod
    def _row_values(r):
        conf = r.get("confidence_score")
        return (
            r["id"], r["target_type"], r["target_id"],
            (r.get("title") or "")[:200],
            r.get("sentiment") or "-",
            f"{float(conf):.2f}" if conf is not None else "-",
            r.get("model_name") or "-",
            r.get("status") or "-",
            r.get("retry_count") if r.get("retry_count") is not None else 0,
            SentimentManager._fmt_dt(r.get("published_at")),
            str(r.get("analyzed_at") or "-"),
        )

    # ---------------- Sort (klik header) ----------------
    def _sort_by(self, key):
        """Klik header: sort di database (berlaku untuk semua data, bukan hanya baris tampil)."""
        if key == self._sort_key:
            self._sort_desc = not self._sort_desc      # klik lagi -> balik arah
        else:
            self._sort_key = key
            self._sort_desc = key in DESC_FIRST        # kolom tanggal/angka: terbaru dulu
        self._update_heading_arrows()
        self.refresh()

    def _update_heading_arrows(self):
        for key, text, _w in COLUMNS:
            arrow = ""
            if key == self._sort_key:
                arrow = "  ▼" if self._sort_desc else "  ▲"
            self.tree.heading(key, text=text + arrow)

    def select_all(self):
        self.tree.selection_set(self.tree.get_children(""))

    def _update_sel_label(self):
        self.lbl_sel.config(text=f"Dipilih: {len(self.tree.selection())}")

    def _selected_ids(self):
        return [int(i) for i in self.tree.selection()]

    # ---------------- Tombol 1: Rollback ----------------
    def rollback_pending(self):
        if self.running:
            return
        ids = self._selected_ids()
        if not ids:
            messagebox.showinfo("Info", "Pilih minimal 1 baris dulu.")
            return
        if not messagebox.askyesno(
            "Konfirmasi Rollback",
            f"Set {len(ids)} hasil sentimen kembali ke 'pending'?\n\n"
            "Baris ini akan dihitung ulang oleh batch auto berikutnya "
            "(python process_sentiment_db.py).",
        ):
            return

        extra = ", error_message = NULL, retry_count = 0" if self.var_reset.get() else ""
        total = 0
        try:
            for i in range(0, len(ids), CHUNK):
                part = ids[i:i + CHUNK]
                ph = ",".join(["%s"] * len(part))
                total += db_execute(
                    f"UPDATE sentiment_results SET status = 'pending'{extra} "
                    f"WHERE id IN ({ph})", part
                )
        except Error as e:
            messagebox.showerror("Database Error", str(e))
            return

        self._log(f"[ROLLBACK] {total} baris di-set ke 'pending'.")
        self.refresh()
        messagebox.showinfo("Selesai", f"{total} baris dikembalikan ke 'pending'.")

    # ---------------- Tombol 2: Sentimen ulang lokal ----------------
    def rerun_local(self):
        if self.running:
            return
        ids = self._selected_ids()
        if not ids:
            messagebox.showinfo("Info", "Pilih minimal 1 baris dulu.")
            return
        if not messagebox.askyesno(
            "Konfirmasi Sentimen Ulang",
            f"Hitung ulang {len(ids)} baris sekarang memakai model lokal?\n\n"
            "Pertama kali akan memuat model ML (bisa beberapa menit). "
            "Hasil langsung ditimpa ke database (status 'completed').\n"
            "Pastikan batch auto tidak sedang berjalan.",
        ):
            return
        self._set_running(True)
        self.progress.config(maximum=len(ids), value=0)
        self.stop_flag.clear()
        engine = self.var_engine.get()
        model = self.var_model.get().strip()
        threading.Thread(target=self._rerun_worker, args=(ids, engine, model),
                         daemon=True).start()

    def stop_run(self):
        self.stop_flag.set()
        self._log("[!] Stop diminta, menunggu item berjalan selesai ...")

    def _set_running(self, flag: bool):
        self.running = flag
        self.btn_rerun.config(state="disabled" if flag else "normal")
        self.btn_rollback.config(state="disabled" if flag else "normal")
        self.btn_stop.config(state="normal" if flag else "disabled")

    def _get_processor(self):
        if self.processor is None:
            self.ui_q.put(("log", "⏳ Memuat engine ML (process_sentiment_db) ..."))
            self.processor = importlib.import_module(PROCESSOR_MODULE)
            self.ui_q.put(("log", "✅ Engine ML siap."))
        return self.processor

    def _rerun_worker(self, ids, engine, model):
        ok = fail = skipped = 0
        try:
            if engine == ENGINE_LLM:
                self.ui_q.put(("log", f"⏳ Memuat LLM lokal: {model} (pertama kali bisa lama, "
                                      "model diunduh dari Hugging Face) ..."))
                import llm_sentiment
                eng = llm_sentiment.get_engine(model)
                self.ui_q.put(("log", f"✅ LLM siap (backend: {eng.backend})."))

                def evaluate(title, raw_text, media_urls):
                    return llm_sentiment.evaluate_content_llm(
                        title, raw_text, media_urls, model_name=model)

                def evaluate_comment(text):
                    return llm_sentiment.evaluate_comment_llm(text, model_name=model)
            elif engine == ENGINE_OMNI:
                self.ui_q.put(("log", f"⏳ Menguji koneksi OmniRoute, model: {model} ..."))
                import omniroute_sentiment as omni
                omni.classify(model, "Jawab satu kata: positive, neutral, atau negative.",
                              "Bagus sekali!")  # tes cepat: gagal di sini = config salah
                self.ui_q.put(("log", "✅ OmniRoute siap (inferensi di server, bukan lokal)."))

                def evaluate(title, raw_text, media_urls):
                    return omni.evaluate_content_omni(title, raw_text, media_urls, model_name=model)

                def evaluate_comment(text):
                    return omni.evaluate_comment_omni(text, model_name=model)
            else:
                psd = self._get_processor()
                evaluate = psd.evaluate_content_multimodal

                def evaluate_comment(text):
                    return evaluate_comment_old(psd, text)
        except Exception as e:
            self.ui_q.put(("log", f"[ERROR] Gagal memuat engine ML: {e}"))
            self.ui_q.put(("done", (0, 0, 0)))
            return

        total = len(ids)
        for n, rid in enumerate(ids, start=1):
            if self.stop_flag.is_set():
                self.ui_q.put(("log", "[!] Dihentikan oleh pengguna."))
                break
            try:
                rows = db_fetch_all(
                    """
                    SELECT sr.id AS result_id, sr.target_type, sr.target_id,
                           c.title, c.raw_text, c.media_urls, cm.comment_text
                    FROM sentiment_results sr
                    LEFT JOIN contents c
                           ON sr.target_type = 'content' AND sr.target_id = c.id
                    LEFT JOIN comments cm
                           ON sr.target_type = 'comment' AND sr.target_id = cm.id
                    WHERE sr.id = %s
                    """,
                    (rid,),
                )
                if not rows:
                    skipped += 1
                    self.ui_q.put(("log", f"[{n}/{total}] ID {rid} tidak ditemukan, dilewati."))
                    self.ui_q.put(("progress", n))
                    continue
                item = rows[0]
                ttype = item["target_type"]
                if ttype == "content":
                    empty = item["title"] is None and item["raw_text"] is None
                    snippet = (item["title"] or f"Content {item['target_id']}")
                elif ttype == "comment":
                    empty = item["comment_text"] is None
                    snippet = (item["comment_text"] or f"Comment {item['target_id']}")
                else:
                    empty, snippet = True, ""
                if empty:
                    skipped += 1
                    self.ui_q.put((
                        "log",
                        f"[{n}/{total}] ID {rid} dilewati: tipe '{ttype}' tidak didukung "
                        f"atau data sumber (contents/comments) tidak ditemukan.",
                    ))
                    self.ui_q.put(("progress", n))
                    continue

                snippet = snippet[:50].replace("\n", " ")
                self.ui_q.put(("log", f"[{n}/{total}] Menghitung ID {rid} ({ttype}): '{snippet}' ..."))

                try:
                    if ttype == "comment":
                        label, conf, model_name, details = evaluate_comment(item["comment_text"])
                    else:
                        label, conf, model_name, details = evaluate(
                            item["title"], item["raw_text"], item["media_urls"]
                        )
                except Exception as ml_err:
                    fail += 1
                    db_execute(UPDATE_FAIL, (str(ml_err)[:500], rid))
                    self.ui_q.put(("log", f"   [ERROR ML] {ml_err}"))
                    self.ui_q.put(("rowfix", rid))
                    self.ui_q.put(("progress", n))
                    continue

                db_execute(UPDATE_OK, (label, conf, model_name, rid))
                ok += 1
                self.ui_q.put((
                    "log",
                    f"   [OK] {label} | conf={conf} | {model_name} | {details}",
                ))
                self.ui_q.put(("rowfix", rid))
            except Error as db_err:
                fail += 1
                self.ui_q.put(("log", f"   [DB ERROR] ID {rid}: {db_err}"))
            except Exception as e:  # jaga worker tetap hidup
                fail += 1
                self.ui_q.put(("log", f"   [ERROR] ID {rid}: {e}"))
            self.ui_q.put(("progress", n))

        self.ui_q.put(("done", (ok, fail, skipped)))

    # ---------------- Queue / log ----------------
    def _log(self, msg):
        self.log.config(state="normal")
        self.log.insert("end", msg + "\n")
        self.log.see("end")
        self.log.config(state="disabled")

    def _refresh_row(self, rid):
        """Ambil ulang 1 baris dari DB lalu update tampilan tanpa reload semua."""
        iid = str(rid)
        if not self.tree.exists(iid):
            return
        rows = db_fetch_all(f"{BASE_SELECT} WHERE sr.id = %s", (rid,))
        if rows:
            self.tree.item(iid, values=self._row_values(rows[0]), tags=(rows[0]["status"] or "",))

    def _poll_queue(self):
        try:
            while True:
                kind, payload = self.ui_q.get_nowait()
                if kind == "log":
                    self._log(payload)
                elif kind == "progress":
                    self.progress.config(value=payload)
                elif kind == "omni_models":
                    self._set_omni_models(payload)
                elif kind == "rowfix":
                    try:
                        self._refresh_row(payload)
                    except Error:
                        pass
                elif kind == "done":
                    ok, fail, skipped = payload
                    self._set_running(False)
                    self._log(f"=== SELESAI | berhasil: {ok} | gagal: {fail} | dilewati: {skipped} ===")
                    self.refresh()
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)


if __name__ == "__main__":
    SentimentManager().mainloop()