import csv
import html
import json
import re
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText
import xml.etree.ElementTree as ET
from bs4 import BeautifulSoup
from googlenewsdecoder import gnewsdecoder
import mysql.connector
from mysql.connector import Error
import requests
from pathlib import Path
import sys

# Menambahkan root project directory (2 tingkat di atas folder script saat ini) ke sys.path
sys.path.append(str(Path(__file__).resolve().parents[2]))

from config.config import get_db_connection


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "Chrome/122.0.0.0 Safari/537.36"
    )
}

# --- KONFIGURASI SUMBER BERITA ---
# code      : harus sama persis dengan kolom platforms.code di database
# domain    : dipakai untuk query "site:" Google News
# selectors : CSS selector kontainer isi artikel (dicoba semua, diambil yang teksnya terpanjang)
SOURCES = [
    {
        "code": "antaranews",
        "name": "Antara News",
        "domain": "antaranews.com",
        "selectors": ["div.post-content", "div.post-wrapper"],
    },
    {
        "code": "detik",
        "name": "Detikcom",
        "domain": "detik.com",
        "selectors": ["div.detail__body-text", "div#detikdetailtext"],
    },
    {
        "code": "kompas",
        "name": "Kompas",
        "domain": "kompas.com",
        "selectors": ["div.read__content", "div.kcm-read-text"],
    },
    {
        "code": "cnn",
        "name": "CNN Indonesia",
        "domain": "cnnindonesia.com",
        "selectors": ["div.detail-text", "div.detail_text"],
    },
    {
        "code": "tribun",
        "name": "Tribunnews",
        "domain": "tribunnews.com",  # termasuk subdomain *.tribunnews.com
        "selectors": ["div.side-article.txt-article", "div.txt-article"],
    },
    {
        "code": "metrotv",
        "name": "Metro TV News",
        "domain": "metrotvnews.com",
        "selectors": ["div.news-text", "div.news-content"],
    },
    {
        "code": "beritasatu",
        "name": "Berita Satu",
        "domain": "beritasatu.com",
        "selectors": ["div.body-content", "div.article-content", "div.content-detail"],
    },
    {
        "code": "kabarika",
        "name": "Kabarika ID",
        "domain": "kabarika.id",
        "selectors": ["div.entry-content", "div.td-post-content", "div.tdb-block-inner"],
    },
    {
        "code": "republika",
        "name": "Republika",
        "domain": "republika.co.id",
        "selectors": ["div.article-content", "div.artikel"],
    },
    {
        "code": "idntimes",
        "name": "IDN Times",
        "domain": "idntimes.com",
        "selectors": ["div#article-content", "div.content-article", "article"],
    },
    {
        "code": "rri",
        "name": "RRI",
        "domain": "rri.co.id",
        "selectors": ["div.content-detail", "div.detail-content", "div.post-content"],
    },
    {
        "code": "pertanian",
        "name": "Kementerian Pertanian",
        "domain": "pertanian.go.id",
        "selectors": ["div.entry-content", "div.post-content", "div.content-detail"],
        # Situs Kementan punya banyak subdomain dengan struktur URL berbeda.
        # URL harus cocok SALAH SATU pola allow, dan TIDAK cocok pola deny.
        "article_url_patterns": [
            r"/news/view/\d+",                                  # pertanian.go.id/index.php/news/view/8226
            r"/(?:index\.php/)?(?:en/)?(?:berita|artikel)/[^/?#]+",  # brmp, ditjenpkh, bpmsph, tanamanpangan
            r"/post/berita/[^/?#]+",                            # bppsdmp
            r"^https?://[^/]+/[a-z0-9]+(?:-[a-z0-9]+){4,}/?$",  # slug panjang di root (ditjenbun)
        ],
        "deny_url_patterns": [
            r"/(?:download|regulasi|peraturan|informasi-publik)(?:/|$)",
            r"/news/all",
            r"[?&]page=",
        ],
    },
    {
        "code": "viva",
        "name": "VIVA",
        "domain": "viva.co.id",
        "selectors": ["div.main-content-detail", "div.content-detail", "div.article-content"],
    },
]
SOURCE_MAP = {s["code"]: s for s in SOURCES}


def source_has_url_rules(source):
    return bool(
        source
        and (source.get("article_url_patterns") or source.get("deny_url_patterns"))
    )


def check_source_url(source, url):
    """Cek URL terhadap aturan khusus sumber. Return (ok, alasan)."""
    if not source:
        return True, "ok"
    for pat in source.get("deny_url_patterns", []):
        if re.search(pat, url, re.IGNORECASE):
            return False, "URL termasuk halaman non-artikel (deny)"
    allow = source.get("article_url_patterns", [])
    if allow and not any(re.search(pat, url, re.IGNORECASE) for pat in allow):
        return False, "URL tidak cocok pola artikel"
    return True, "ok"

# Selector umum bila selector khusus situs tidak menemukan apa pun
GENERIC_SELECTORS = [
    "[itemprop='articleBody']",
    "div.entry-content",
    "div.post-content",
    "div.article-content",
    "div.detail-text",
    "div.content-detail",
]

# Batas ukuran raw_text (byte UTF-8). 60000 aman untuk kolom TEXT (maks 65535 byte).
MAX_RAW_TEXT_BYTES = 60000


def truncate_text(text, max_bytes=MAX_RAW_TEXT_BYTES):
    """Potong teks agar tidak melebihi max_bytes (UTF-8) tanpa merusak karakter."""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    print(f"[!] raw_text dipotong dari {len(encoded)} ke {max_bytes} byte")
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


# Paragraf sisipan yang bukan isi berita
NOISE_PARAGRAPH = re.compile(
    r"^(baca juga|baca selengkapnya|simak juga|lihat juga|advertisement|scroll to continue|iklan)\b",
    re.IGNORECASE,
)


def decode_google_news_url(rss_url):
    """Mendekode URL terenkripsi Google News menjadi Actual Clean URL."""
    try:
        result = gnewsdecoder(rss_url)

        decoded_url = None
        if isinstance(result, str):
            decoded_url = result
        elif isinstance(result, dict):
            decoded_url = result.get("decoded_url") or result.get("url")
        elif hasattr(result, "decoded_url"):
            decoded_url = getattr(result, "decoded_url")

        if (
            decoded_url
            and isinstance(decoded_url, str)
            and decoded_url.startswith(("http://", "https://"))
        ):
            return decoded_url
    except Exception as e:
        print(f"[!] Gagal decode Google URL: {e}")

    # Fallback manual bila library dekoder gagal
    try:
        response = requests.get(
            rss_url, headers=HEADERS, allow_redirects=True, timeout=6
        )
        if response.url and response.url.startswith(("http://", "https://")):
            return response.url
    except Exception:
        pass

    return rss_url


def _extract_paragraphs(container):
    """Ambil teks paragraf (tidak kosong, bukan sisipan 'Baca juga' dll.) dari sebuah elemen."""
    if not container:
        return []
    result = []
    for p in container.find_all("p"):
        text = p.get_text(" ", strip=True)
        if text and not NOISE_PARAGRAPH.match(text):
            result.append(text)
    return result


def _best_paragraphs(soup, selectors):
    """Coba semua selector, kembalikan paragraf dengan total teks terpanjang."""
    best, best_len = [], 0
    for sel in selectors:
        try:
            container = soup.select_one(sel)
        except Exception:
            continue
        paragraphs = _extract_paragraphs(container)
        total = sum(len(p) for p in paragraphs)
        if total > best_len:
            best, best_len = paragraphs, total
    return best


def scrape_article_detail(clean_url, platform_code):
    """Mengambil teks isi artikel dan URL gambar utama dari Clean URL."""
    raw_text = ""
    media_urls = []

    try:
        res = requests.get(clean_url, headers=HEADERS, timeout=12)
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, "html.parser")

            # 1. Ekstraksi Gambar Utama via OpenGraph Meta Tag
            og_image = soup.find("meta", property="og:image") or soup.find(
                "meta", attrs={"name": "og:image"}
            )
            if og_image and og_image.get("content"):
                media_urls.append(og_image["content"])

            # Buang elemen yang bukan isi berita
            for tag in soup(["script", "style", "noscript", "aside", "figure", "iframe", "form"]):
                tag.decompose()

            # 2. Ekstraksi Paragraf Isi Berita berdasarkan Platform
            source = SOURCE_MAP.get(platform_code)
            paragraphs = _best_paragraphs(soup, source["selectors"]) if source else []

            # Selector umum bila selector khusus situs gagal / hasilnya sedikit
            if len(paragraphs) < 3:
                generic = _best_paragraphs(soup, GENERIC_SELECTORS)
                if sum(len(p) for p in generic) > sum(len(p) for p in paragraphs):
                    paragraphs = generic

            # Fallback Umum
            if not paragraphs:
                all_p = soup.find_all("p")
                paragraphs = [
                    p.get_text(" ", strip=True)
                    for p in all_p
                    if len(p.get_text(strip=True)) > 30
                ]

            raw_text = truncate_text("\n\n".join(paragraphs))

    except Exception as e:
        print(f"[!] Gagal scrape detail isi artikel [{clean_url}]: {e}")

    return raw_text, media_urls


def check_articles_exist_in_db(articles):
    """Mengecek apakah artikel-artikel hasil search sudah ada di database atau belum."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        cursor.execute("SELECT clean_url, title FROM contents")
        existing_rows = cursor.fetchall()

        existing_titles = {row["title"].lower().strip() for row in existing_rows if row["title"]}

        cursor.close()
        conn.close()

        for item in articles:
            if item["title"].lower().strip() in existing_titles:
                item["db_status"] = "Ada di DB"
            else:
                item["db_status"] = "Belum Ada di DB"

    except Exception as e:
        print(f"[!] Gagal mengecek status DB: {e}")
        for item in articles:
            item["db_status"] = "Belum Cek"

    return articles


# --- RSS SCRAPER FUNCTIONS ---
def clean_rss_title(raw_title, rss_source_name):
    """Bersihkan suffix ' - Nama Media' dari judul Google News."""
    title = html.unescape(raw_title or "").strip()
    if rss_source_name and title.endswith(f" - {rss_source_name}"):
        title = title[: -len(f" - {rss_source_name}")]
    else:
        title = re.sub(r"\s+-\s+[^-]+$", "", title)
    return title.strip()


def fetch_rss_news(source, keyword, limit):
    """Ambil RSS Google News (filter site:) untuk satu sumber."""
    articles = []
    query = f"{keyword} site:{source['domain']}"
    rss_url = f"https://news.google.com/rss/search?q={requests.utils.quote(query)}&hl=id&gl=ID&ceid=ID:id"

    try:
        response = requests.get(rss_url, headers=HEADERS, timeout=10)
        if response.status_code == 200:
            root = ET.fromstring(response.content)
            has_rules = source_has_url_rules(source)
            # Bila ada filter pola URL, scan lebih banyak item RSS karena sebagian akan dibuang
            max_scan = limit * 5 if has_rules else limit
            for scanned, item in enumerate(root.findall(".//item")):
                if len(articles) >= limit or scanned >= max_scan:
                    break

                title_elem = item.find("title")
                link_elem = item.find("link")
                source_elem = item.find("source")

                if title_elem is not None and link_elem is not None:
                    link = link_elem.text or ""
                    rss_source_name = source_elem.text if source_elem is not None else None
                    clean_title = clean_rss_title(title_elem.text, rss_source_name)

                    if clean_title and link:
                        entry = {
                            "platform_code": source["code"],
                            "source": source["name"],
                            "title": clean_title,
                            "rss_url": link,
                            "db_status": "Mengecek...",
                        }
                        if has_rules:
                            decoded = decode_google_news_url(link)
                            ok, why = check_source_url(source, decoded)
                            if not ok:
                                print(f"[skip bukan artikel] {source['name']} ({why}): {decoded}")
                                continue
                            entry["clean_url"] = decoded
                        articles.append(entry)
    except Exception as e:
        print(f"[!] Error RSS {source['name']}: {e}")

    return articles


# --- GUI APPLICATION ---
class NewsScraperApp:

    CHECKBOXES_PER_ROW = 5

    def __init__(self, root):
        self.root = root
        self.root.title("Scraper Berita - db_sentimenanalisis_v2")
        self.root.geometry("1020x800")

        self.results_data = []

        # UI Style
        style = ttk.Style()
        style.theme_use("clam")

        # Panel Input
        input_frame = ttk.LabelFrame(
            root, text=" Pengaturan Pencarian ", padding=10
        )
        input_frame.pack(fill="x", padx=10, pady=5)

        ttk.Label(input_frame, text="Kata Kunci:").grid(
            row=0, column=0, sticky="w", padx=5
        )
        self.keyword_entry = ttk.Entry(input_frame, width=30)
        self.keyword_entry.grid(row=0, column=1, padx=5, pady=5, sticky="w")

        ttk.Label(input_frame, text="Batas Maksimal/Portal:").grid(
            row=0, column=2, sticky="w", padx=5
        )
        self.limit_spinbox = ttk.Spinbox(
            input_frame, from_=1, to=100, width=5
        )
        self.limit_spinbox.set(5)
        self.limit_spinbox.grid(row=0, column=3, padx=5, pady=5, sticky="w")

        ttk.Label(input_frame, text="Pilih Sumber:").grid(
            row=1, column=0, sticky="nw", padx=5, pady=5
        )

        # Checkbox sumber dibuat otomatis dari daftar SOURCES
        self.source_vars = {}
        cb_frame = ttk.Frame(input_frame)
        cb_frame.grid(row=1, column=1, columnspan=3, sticky="w")

        for i, source in enumerate(SOURCES):
            var = tk.BooleanVar(value=source["code"] in ("antaranews", "detik", "kompas"))
            self.source_vars[source["code"]] = var
            ttk.Checkbutton(
                cb_frame, text=source["name"], variable=var
            ).grid(
                row=i // self.CHECKBOXES_PER_ROW,
                column=i % self.CHECKBOXES_PER_ROW,
                sticky="w",
                padx=8,
                pady=2,
            )

        select_frame = ttk.Frame(input_frame)
        select_frame.grid(row=2, column=1, columnspan=3, sticky="w", pady=(5, 0))
        ttk.Button(
            select_frame, text="Pilih Semua", command=lambda: self.set_all_sources(True)
        ).pack(side="left", padx=5)
        ttk.Button(
            select_frame, text="Kosongkan", command=lambda: self.set_all_sources(False)
        ).pack(side="left", padx=5)

        # Action Buttons
        btn_frame = ttk.Frame(root, padding=5)
        btn_frame.pack(fill="x", padx=10)

        self.search_btn = ttk.Button(
            btn_frame,
            text="1. Cari Berita (RSS)",
            command=self.start_search_thread,
        )
        self.search_btn.pack(side="left", padx=5)

        self.db_btn = ttk.Button(
            btn_frame,
            text="2. Decode, Scrape Detail & Simpan ke DB",
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

        self.status_label = ttk.Label(
            btn_frame, text="Siap", font=("Arial", 9, "italic")
        )
        self.status_label.pack(side="right", padx=5)

        # Display Table (Treeview)
        table_frame = ttk.Frame(root, padding=10)
        table_frame.pack(fill="both", expand=True)

        columns = ("No", "Source", "Title", "DBStatus", "ProcessStatus")
        self.tree = ttk.Treeview(
            table_frame, columns=columns, show="headings", selectmode="browse"
        )

        self.tree.heading("No", text="No")
        self.tree.heading("Source", text="Portal")
        self.tree.heading("Title", text="Judul Artikel")
        self.tree.heading("DBStatus", text="Status di DB")
        self.tree.heading("ProcessStatus", text="Status Proses")

        self.tree.column("No", width=40, anchor="center")
        self.tree.column("Source", width=120, anchor="center")
        self.tree.column("Title", width=460)
        self.tree.column("DBStatus", width=120, anchor="center")
        self.tree.column("ProcessStatus", width=160, anchor="center")

        scrollbar = ttk.Scrollbar(
            table_frame, orient="vertical", command=self.tree.yview
        )
        self.tree.configure(yscroll=scrollbar.set)

        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        # Live Console / Progress Log Panel
        log_frame = ttk.LabelFrame(root, text=" Live Console / Log ", padding=5)
        log_frame.pack(fill="x", padx=10, pady=5)

        self.log_console = ScrolledText(log_frame, height=8, font=("Consolas", 8))
        self.log_console.pack(fill="both", expand=True)

    def set_all_sources(self, value):
        for var in self.source_vars.values():
            var.set(value)

    def append_log(self, text):
        """Menampilkan teks log ke console GUI secara real-time."""
        self.log_console.insert(tk.END, text + "\n")
        self.log_console.see(tk.END)

    def start_search_thread(self):
        keyword = self.keyword_entry.get().strip()
        if not keyword:
            messagebox.showwarning(
                "Peringatan", "Silakan masukkan kata kunci pencarian."
            )
            return

        selected = [s for s in SOURCES if self.source_vars[s["code"]].get()]
        if not selected:
            messagebox.showwarning(
                "Peringatan", "Pilih minimal satu sumber berita."
            )
            return

        self.search_btn.config(state="disabled")
        self.db_btn.config(state="disabled")
        self.export_btn.config(state="disabled")
        self.status_label.config(text="Mencari feed RSS...")

        self.log_console.delete("1.0", tk.END)
        self.append_log(
            f"[*] Memulai pencarian RSS untuk kata kunci: '{keyword}' "
            f"({len(selected)} sumber)..."
        )

        for item in self.tree.get_children():
            self.tree.delete(item)

        threading.Thread(
            target=self.perform_search, args=(keyword, selected), daemon=True
        ).start()

    def perform_search(self, keyword, selected_sources):
        try:
            limit = int(self.limit_spinbox.get())
        except ValueError:
            limit = 5

        self.results_data = []

        for source in selected_sources:
            found = fetch_rss_news(source, keyword, limit)
            self.results_data.extend(found)
            self.root.after(
                0,
                lambda n=source["name"], c=len(found): self.append_log(
                    f"   [{n}] {c} RSS link"
                ),
            )

        # Cek ketersediaan di Database
        self.root.after(
            0, lambda: self.append_log("[*] Memeriksa status ketersediaan artikel di MySQL...")
        )
        self.results_data = check_articles_exist_in_db(self.results_data)

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
                        item["source"],
                        item["title"],
                        item["db_status"],
                        "Siap Diproses",
                    ),
                )

            self.status_label.config(
                text=f"Total: {len(self.results_data)} | Baru: {new_count} | Ada di DB: {exist_count}"
            )
            self.append_log(
                f"[+] Selesai! Ditemukan {len(self.results_data)} artikel "
                f"({new_count} belum ada di DB, {exist_count} sudah ada di DB)."
            )
            self.db_btn.config(state="normal")
            self.export_btn.config(state="normal")
        else:
            self.status_label.config(text="Artikel tidak ditemukan.")
            self.append_log("[-] Tidak ada artikel ditemukan.")
            messagebox.showinfo("Informasi", "Tidak ada artikel ditemukan.")

        self.search_btn.config(state="normal")

    def start_save_thread(self):
        self.db_btn.config(state="disabled")
        self.search_btn.config(state="disabled")
        self.status_label.config(text="Memproses artikel...")

        threading.Thread(target=self.perform_save_to_db, daemon=True).start()

    def update_tree_status(self, row_idx, db_status=None, process_status=None):
        """Update kolom Status di tabel GUI secara live."""
        current_vals = list(self.tree.item(str(row_idx), "values"))
        if db_status:
            current_vals[3] = db_status
        if process_status:
            current_vals[4] = process_status
        self.tree.item(str(row_idx), values=current_vals)

    def perform_save_to_db(self):
        inserted_count = 0
        skipped_count = 0

        # FILTER: Hanya ambil artikel yang BELUM ADA DI DB
        items_to_process = [
            (idx, item) for idx, item in enumerate(self.results_data)
            if item.get("db_status") == "Belum Ada di DB"
        ]

        total = len(items_to_process)

        if total == 0:
            self.root.after(0, lambda: self.append_log("\n[i] Semua artikel dalam daftar sudah ada di Database. Tidak ada yang perlu diproses."))
            self.root.after(0, lambda: messagebox.showinfo("Informasi", "Semua artikel sudah ada di Database."))
            self.root.after(0, lambda: self.db_btn.config(state="normal"))
            self.root.after(0, lambda: self.search_btn.config(state="normal"))
            return

        self.root.after(0, lambda: self.append_log(f"\n[*] Memproses {total} artikel baru yang belum ada di Database..."))

        try:
            conn = get_db_connection()
            cursor = conn.cursor(dictionary=True)

            cursor.execute("SELECT id, code FROM platforms")
            platform_map = {row["code"]: row["id"] for row in cursor.fetchall()}

            for step, (idx, item) in enumerate(items_to_process, start=1):
                p_code = item["platform_code"]
                p_id = platform_map.get(p_code)

                # Platform belum ada di tabel platforms -> jangan simpan sebagai platform lain
                if not p_id:
                    skipped_count += 1
                    self.root.after(
                        0,
                        lambda i=idx: self.update_tree_status(
                            i, process_status="SKIP: platform belum ada"
                        ),
                    )
                    self.root.after(
                        0,
                        lambda c=p_code: self.append_log(
                            f"\n[!] Platform code '{c}' belum ada di tabel platforms. "
                            f"Jalankan DML insert platforms dulu."
                        ),
                    )
                    continue

                self.root.after(
                    0,
                    lambda i=idx: self.update_tree_status(
                        i, process_status="1/3 Decoding URL..."
                    ),
                )
                self.root.after(
                    0,
                    lambda i=idx, s=step, t=item["title"]: self.append_log(
                        f"\n[{s}/{total}] Memproses: {t[:50]}..."
                    ),
                )

                # Step 1: Decode RSS link ke Clean URL
                rss_url = item["rss_url"]
                clean_url = item.get("clean_url") or decode_google_news_url(rss_url)

                # Validasi akhir: URL harus cocok dengan pola artikel sumbernya (bila ada)
                url_ok, _why = check_source_url(SOURCE_MAP.get(p_code), clean_url)
                if not url_ok:
                    skipped_count += 1
                    self.root.after(
                        0,
                        lambda i=idx: self.update_tree_status(
                            i, process_status="SKIP: bukan artikel"
                        ),
                    )
                    self.root.after(
                        0,
                        lambda u=clean_url: self.append_log(
                            f"   [SKIP] Bukan halaman artikel: {u}"
                        ),
                    )
                    continue

                self.root.after(
                    0,
                    lambda i=idx: self.update_tree_status(
                        i, process_status="2/3 Scraping Isi & Gambar..."
                    ),
                )

                # Step 2: Scrape isi konten dan gambar
                raw_text, media_urls = scrape_article_detail(clean_url, p_code)
                media_urls_json = json.dumps(media_urls) if media_urls else None

                # Step 3: Simpan ke DB
                self.root.after(
                    0,
                    lambda i=idx: self.update_tree_status(
                        i, process_status="3/3 Menyimpan ke DB..."
                    ),
                )
                query = """
                    INSERT IGNORE INTO contents
                    (platform_id, clean_url, title, raw_text, media_urls)
                    VALUES (%s, %s, %s, %s, %s)
                """
                cursor.execute(
                    query,
                    (p_id, clean_url, item["title"], raw_text, media_urls_json),
                )

                if cursor.rowcount > 0:
                    inserted_count += 1
                    content_id = cursor.lastrowid
                    item["db_status"] = "Ada di DB"

                    # Antrikan ke sentiment_results
                    cursor.execute(
                        """
                        INSERT IGNORE INTO sentiment_results
                        (target_type, target_id, execution_source, status)
                        VALUES ('content', %s, 'ml_pipeline', 'pending')
                    """,
                        (content_id,),
                    )
                    conn.commit()
                    self.root.after(
                        0,
                        lambda i=idx: self.update_tree_status(
                            i, db_status="Ada di DB", process_status="SUKSES"
                        ),
                    )
                    self.root.after(
                        0,
                        lambda u=clean_url: self.append_log(
                            f"   [OK] Clean URL: {u}"
                        ),
                    )
                else:
                    skipped_count += 1
                    item["db_status"] = "Ada di DB"
                    self.root.after(
                        0,
                        lambda i=idx: self.update_tree_status(
                            i, db_status="Ada di DB", process_status="SKIPPED"
                        ),
                    )
                    self.root.after(
                        0,
                        lambda: self.append_log(
                            "   [SKIP] Artikel ini sudah ada di database."
                        ),
                    )

            cursor.close()
            conn.close()

            self.root.after(
                0, lambda: self.on_save_finished(inserted_count, skipped_count)
            )

        except Exception as e:
            self.root.after(
                0, lambda err=e: self.append_log(f"\n[ERROR FATAL] {err}")
            )
            self.root.after(
                0,
                lambda err=e: messagebox.showerror(
                    "Database Error", f"Gagal memproses:\n{err}"
                ),
            )
            self.root.after(0, lambda: self.db_btn.config(state="normal"))
            self.root.after(0, lambda: self.search_btn.config(state="normal"))

    def on_save_finished(self, inserted, skipped):
        msg = f"Selesai! {inserted} artikel berhasil disimpan, {skipped} di-skip/sudah ada."
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
        default_filename = f"berita_{keyword}.csv"

        filepath = filedialog.asksaveasfilename(
            defaultextension=".csv",
            initialfile=default_filename,
            filetypes=[("CSV Files", "*.csv"), ("All Files", "*.*")],
        )

        if filepath:
            try:
                fieldnames = ["platform_code", "source", "title", "db_status", "rss_url"]
                with open(
                    filepath, mode="w", newline="", encoding="utf-8"
                ) as file:
                    writer = csv.DictWriter(file, fieldnames=fieldnames)
                    writer.writeheader()
                    writer.writerows(self.results_data)

                messagebox.showinfo(
                    "Berhasil", f"Data berhasil disimpan di:\n{filepath}"
                )
            except Exception as e:
                messagebox.showerror("Error", f"Gagal menyimpan file CSV: {e}")


if __name__ == "__main__":
    root = tk.Tk()
    app = NewsScraperApp(root)
    root.mainloop()