"""
YouTube Re-Summarizer GUI
-------------------------
Mencari konten YouTube (platform_id = 5) di tabel `contents` yang raw_text-nya masih
berformat pendek/jelek (ringkasan 1 kalimat, hanya deskripsi, atau transkrip terpotong),
lalu memprosesnya ulang: subtitle -> Whisper -> summarizer (Qwen) dan meng-UPDATE raw_text.

Letakkan file ini di folder yang SAMA dengan auto_yt_scraper.py (versi baru).
"""
import logging
import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scrapers"))

REQUIRED = ("run_db", "fetch_subtitle_text", "process_video_text", "truncate_bytes",
            "MAX_RAW_TEXT_BYTES", "yt_dlp")
try:
    import auto_yt_scraper as core
    _missing = [n for n in REQUIRED if not hasattr(core, n)]
    if _missing:
        raise ImportError(
            "auto_yt_scraper.py di folder ini bukan versi baru (tidak ada: " + ", ".join(_missing) + ")."
        )
except Exception as _e:
    _r = tk.Tk()
    _r.withdraw()
    messagebox.showerror("Gagal memuat auto_yt_scraper.py", str(_e))
    sys.exit(1)

MARK_SUMMARY = "[RINGKASAN VIDEO > 5 MIN]:"
MARK_DESC = "[DESKRIPSI VIDEO]:"
MARK_TRUNC = "[TRANSKRIP (TERPOTONG)]:"
SPLIT = "Transkrip/Ringkasan:\n"


# ---------------------------------------------------------------- helpers
def classify(raw_text: str, threshold: int):
    """Return (kategori | None, panjang isi hasil olahan)."""
    raw_text = raw_text or ""
    body = raw_text.split(SPLIT, 1)[1] if SPLIT in raw_text else raw_text
    body = body.strip()

    if body.startswith(MARK_SUMMARY):
        content = body[len(MARK_SUMMARY):].strip()
        return ("Ringkasan pendek" if len(content) < threshold else None), len(content)
    if body.startswith(MARK_DESC):
        return "Hanya deskripsi", len(body) - len(MARK_DESC)
    if body.startswith(MARK_TRUNC):
        return "Transkrip terpotong", len(body) - len(MARK_TRUNC)
    return None, len(body)


def old_description(raw_text: str) -> str:
    marker = "\n\n" + SPLIT
    idx = raw_text.find(marker)
    if raw_text.startswith("Deskripsi: ") and idx > 0:
        return raw_text[len("Deskripsi: "):idx].strip()
    return ""


def fetch_candidates(conn):
    cur = conn.cursor(dictionary=True)
    cur.execute(
        """
        SELECT id, original_id, title, clean_url, raw_text
        FROM contents
        WHERE platform_id = 5
          AND (raw_text LIKE %s OR raw_text LIKE %s OR raw_text LIKE %s)
        ORDER BY id DESC
        """,
        (f"%{MARK_SUMMARY}%", f"%{MARK_DESC}%", f"%{MARK_TRUNC}%"),
    )
    rows = cur.fetchall()
    cur.close()
    return rows


def get_video_info(url: str) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "socket_timeout": 30,
        "retries": 3,
    }
    with core.yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


def reprocess(rec: dict, max_audio_min: int, reset_sentiment: bool) -> str:
    """Proses ulang satu video dan UPDATE raw_text. Return keterangan status."""
    url = rec["clean_url"]
    video_id = rec["original_id"] or url.split("v=")[-1]

    info = get_video_info(url)
    duration = int(info.get("duration") or 0)
    title = info.get("title") or rec["title"] or "N/A"

    desc = (info.get("description") or "").replace("\n", " ").strip()
    if not desc:
        desc = old_description(rec["raw_text"])
    if not desc or desc == "Tidak ada deskripsi.":
        desc = "Tidak ada deskripsi."
    desc = desc[:2000]

    # Proses berat: TANPA koneksi DB terbuka
    processed = core.process_video_text(video_id, url, duration, title, desc, max_audio_min)

    raw_text = f"Deskripsi: {desc}\n\nTranskrip/Ringkasan:\n{processed}"
    raw_text = core.truncate_bytes(raw_text, core.MAX_RAW_TEXT_BYTES)

    def _update(conn):
        cur = conn.cursor()
        cur.execute("UPDATE contents SET raw_text = %s WHERE id = %s", (raw_text, rec["id"]))
        if reset_sentiment:
            cur.execute(
                "UPDATE sentiment_results SET status = 'pending' "
                "WHERE target_type = 'content' AND target_id = %s",
                (rec["id"],),
            )
        cur.close()

    core.run_db(_update)
    rec["raw_text"] = raw_text

    if processed.startswith(MARK_DESC):
        return "Selesai (tanpa transkrip)"
    return "Selesai"


class QueueLogHandler(logging.Handler):
    def __init__(self, q):
        super().__init__()
        self.q = q

    def emit(self, record):
        try:
            self.q.put(("log", self.format(record)))
        except Exception:
            pass


# ---------------------------------------------------------------- GUI
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("YouTube Re-Summarizer — db_sentimenanalisis_v2")
        self.geometry("1180x800")

        self.q = queue.Queue()
        self.records = {}
        self.busy = False
        self.stop_event = threading.Event()

        self.threshold = tk.IntVar(value=600)
        self.max_audio = tk.IntVar(value=0)
        self.reset_sent = tk.BooleanVar(value=False)
        self.count_var = tk.StringVar(value="Memuat daftar...")

        self._build_ui()

        handler = QueueLogHandler(self.q)
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
        logging.getLogger().addHandler(handler)

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._poll)
        self.after(300, self.load_list)

    # ---- layout
    def _build_ui(self):
        top = ttk.Frame(self, padding=8)
        top.pack(fill="x")

        ttk.Label(top, text="Dianggap pendek jika < ").grid(row=0, column=0, sticky="w")
        ttk.Spinbox(top, from_=100, to=5000, increment=50, width=7, textvariable=self.threshold).grid(row=0, column=1)
        ttk.Label(top, text="karakter").grid(row=0, column=2, padx=(4, 16), sticky="w")

        ttk.Label(top, text="Lewati Whisper jika video >").grid(row=0, column=3, sticky="w")
        ttk.Spinbox(top, from_=0, to=600, increment=5, width=6, textvariable=self.max_audio).grid(row=0, column=4)
        ttk.Label(top, text="menit (0 = tanpa batas)").grid(row=0, column=5, padx=(4, 16), sticky="w")

        ttk.Checkbutton(top, text="Set sentiment_results ke 'pending' setelah update",
                        variable=self.reset_sent).grid(row=0, column=6, sticky="w")

        btns = ttk.Frame(self, padding=(8, 0))
        btns.pack(fill="x")
        self.btn_reload = ttk.Button(btns, text="Muat Ulang Daftar", command=self.load_list)
        self.btn_all = ttk.Button(btns, text="Pilih Semua", command=self.select_all)
        self.btn_sel = ttk.Button(btns, text="Summarize Terpilih", command=lambda: self.start(True))
        self.btn_every = ttk.Button(btns, text="Summarize Semua di Daftar", command=lambda: self.start(False))
        self.btn_stop = ttk.Button(btns, text="Stop (setelah video ini)", command=self.stop, state="disabled")
        for b in (self.btn_reload, self.btn_all, self.btn_sel, self.btn_every, self.btn_stop):
            b.pack(side="left", padx=(0, 6), pady=6)
        self.action_buttons = (self.btn_reload, self.btn_all, self.btn_sel, self.btn_every)
        ttk.Label(btns, textvariable=self.count_var).pack(side="right")

        # daftar
        frame = ttk.Frame(self, padding=(8, 0))
        frame.pack(fill="both", expand=True)
        cols = ("id", "kategori", "panjang", "status", "judul")
        self.tree = ttk.Treeview(frame, columns=cols, show="headings", selectmode="extended", height=12)
        for c, w, t in (("id", 70, "ID"), ("kategori", 140, "Kategori"), ("panjang", 80, "Panjang"),
                        ("status", 190, "Status"), ("judul", 600, "Judul")):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="w")
        sb = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Control-a>", lambda e: (self.select_all(), "break")[1])

        # preview
        pv = ttk.LabelFrame(self, text="Isi raw_text (baris terpilih)", padding=4)
        pv.pack(fill="both", expand=True, padx=8, pady=(8, 0))
        self.preview = tk.Text(pv, height=9, wrap="word")
        pv_sb = ttk.Scrollbar(pv, orient="vertical", command=self.preview.yview)
        self.preview.configure(yscrollcommand=pv_sb.set, state="disabled")
        self.preview.pack(side="left", fill="both", expand=True)
        pv_sb.pack(side="right", fill="y")

        # progress + log
        self.progress = ttk.Progressbar(self, mode="determinate")
        self.progress.pack(fill="x", padx=8, pady=6)

        lg = ttk.LabelFrame(self, text="Log", padding=4)
        lg.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.log = tk.Text(lg, height=9, wrap="word", state="disabled")
        lg_sb = ttk.Scrollbar(lg, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=lg_sb.set)
        self.log.pack(side="left", fill="both", expand=True)
        lg_sb.pack(side="right", fill="y")

    # ---- util UI
    def _log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _set_busy(self, busy: bool):
        self.busy = busy
        for b in self.action_buttons:
            b.configure(state="disabled" if busy else "normal")
        self.btn_stop.configure(state="normal" if busy else "disabled")

    def _show_preview(self, text):
        self.preview.configure(state="normal")
        self.preview.delete("1.0", "end")
        self.preview.insert("1.0", text)
        self.preview.configure(state="disabled")

    def _on_select(self, _event=None):
        sel = self.tree.selection()
        if len(sel) == 1 and sel[0] in self.records:
            self._show_preview(self.records[sel[0]]["raw_text"])

    def select_all(self):
        self.tree.selection_set(self.tree.get_children())

    def _on_close(self):
        if self.busy and not messagebox.askyesno(
                "Keluar", "Proses sedang berjalan. Keluar sekarang? (video yang sedang diproses dibatalkan)"):
            return
        self.stop_event.set()
        self.destroy()

    # ---- load list
    def load_list(self):
        if self.busy:
            return
        self._set_busy(True)
        self.count_var.set("Memuat daftar...")
        threading.Thread(target=self._load_worker, args=(self.threshold.get(),), daemon=True).start()

    def _load_worker(self, threshold):
        try:
            rows = core.run_db(fetch_candidates)
            recs = []
            for r in rows:
                cat, length = classify(r["raw_text"], threshold)
                if cat:
                    r["kategori"], r["panjang"] = cat, length
                    recs.append(r)
            self.q.put(("loaded", recs))
        except Exception as e:
            self.q.put(("error", f"Gagal memuat daftar: {e}"))
        self.q.put(("done",))

    def _populate(self, recs):
        self.tree.delete(*self.tree.get_children())
        self.records = {}
        for r in recs:
            iid = str(r["id"])
            self.records[iid] = r
            self.tree.insert("", "end", iid=iid,
                             values=(r["id"], r["kategori"], r["panjang"], "", r["title"]))
        self.count_var.set(f"{len(recs)} konten perlu diringkas ulang")
        self._show_preview("")

    def _refresh_row(self, iid):
        rec = self.records.get(iid)
        if not rec:
            return
        cat, length = classify(rec["raw_text"], self.threshold.get())
        self.tree.set(iid, "kategori", cat or "Sudah panjang")
        self.tree.set(iid, "panjang", length)
        sel = self.tree.selection()
        if len(sel) == 1 and sel[0] == iid:
            self._show_preview(rec["raw_text"])

    # ---- processing
    def start(self, only_selected: bool):
        if self.busy:
            return
        if only_selected:
            chosen = set(self.tree.selection())
            iids = [i for i in self.tree.get_children() if i in chosen]
        else:
            iids = list(self.tree.get_children())
        if not iids:
            messagebox.showinfo("Info", "Tidak ada baris yang dipilih / tersedia.")
            return
        if not messagebox.askyesno(
                "Konfirmasi",
                f"Proses ulang {len(iids)} video?\n\nraw_text di tabel contents akan di-UPDATE "
                f"(download audio + Whisper + summarizer bisa memakan waktu lama)."):
            return

        self.stop_event.clear()
        self._set_busy(True)
        self.progress["maximum"] = len(iids)
        self.progress["value"] = 0
        threading.Thread(
            target=self._worker,
            args=(iids, self.max_audio.get(), self.reset_sent.get()),
            daemon=True,
        ).start()

    def stop(self):
        self.stop_event.set()
        self._log("[!] Stop diminta, berhenti setelah video yang sedang diproses selesai...")

    def _worker(self, iids, max_audio_min, reset_sentiment):
        total = len(iids)
        for n, iid in enumerate(iids, 1):
            if self.stop_event.is_set():
                self.q.put(("log", "[!] Proses dihentikan."))
                break
            rec = self.records[iid]
            self.q.put(("status", iid, "Memproses..."))
            logging.info(f"[{n}/{total}] ID {rec['id']}: {str(rec['title'])[:60]}")
            try:
                status = reprocess(rec, max_audio_min, reset_sentiment)
                self.q.put(("status", iid, status))
                self.q.put(("refresh", iid))
            except Exception as e:
                logging.error(f"   Gagal ID {rec['id']}: {e}")
                self.q.put(("status", iid, f"Gagal: {str(e)[:50]}"))
            self.q.put(("progress", n, total))
        self.q.put(("done",))

    # ---- queue poller (main thread)
    def _poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                kind = msg[0]
                if kind == "log":
                    self._log(msg[1])
                elif kind == "status":
                    if self.tree.exists(msg[1]):
                        self.tree.set(msg[1], "status", msg[2])
                elif kind == "refresh":
                    self._refresh_row(msg[1])
                elif kind == "progress":
                    self.progress["maximum"] = msg[2]
                    self.progress["value"] = msg[1]
                elif kind == "loaded":
                    self._populate(msg[1])
                elif kind == "error":
                    messagebox.showerror("Error", msg[1])
                elif kind == "done":
                    self._set_busy(False)
        except queue.Empty:
            pass
        self.after(100, self._poll)


if __name__ == "__main__":
    App().mainloop()