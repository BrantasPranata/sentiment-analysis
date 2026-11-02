"""
yt_fix_published_at.py
----------------------
Helper tanpa GUI untuk melengkapi kolom `published_at` konten YouTube yang masih NULL.

Alur:
  1. Ambil semua baris `contents` (platform_id = 5 / YouTube) dengan published_at IS NULL.
  2. Scraping ulang tiap video untuk mendapatkan tanggal upload:
       a) yt-dlp (akurat: timestamp / upload_date)         -> sumber utama
       b) scrapetube search by judul (teks relatif, ~perkiraan) -> fallback
  3. UPDATE contents.published_at di DB.

Contoh pakai:
  python yt_fix_published_at.py                 # proses semua
  python yt_fix_published_at.py --dry-run       # cek saja, tidak menulis ke DB
  python yt_fix_published_at.py --limit 50      # maksimal 50 video
  python yt_fix_published_at.py --sleep 2       # jeda 2 detik antar video
"""

import argparse
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import scrapetube
import yt_dlp

# Root project (1 tingkat di atas folder script ini), sama seperti yt_scraper_v2.py
sys.path.append(str(Path(__file__).resolve().parent.parent))

from config.config import get_db_connection  # noqa: E402

YOUTUBE_PLATFORM_ID = 5
DT_FORMAT = "%Y-%m-%d %H:%M:%S"


# --------------------------------------------------------------------------- #
# Helper parsing
# --------------------------------------------------------------------------- #

def extract_video_id(original_id, clean_url):
    """Ambil video id dari kolom original_id, atau dari clean_url sebagai cadangan."""
    if original_id and re.fullmatch(r"[\w-]{11}", original_id.strip()):
        return original_id.strip()

    if clean_url:
        parsed = urlparse(clean_url)
        if parsed.hostname and "youtu.be" in parsed.hostname:
            vid = parsed.path.strip("/")
            if vid:
                return vid
        vid = parse_qs(parsed.query).get("v", [None])[0]
        if vid:
            return vid
        m = re.search(r"/(?:shorts|embed|live)/([\w-]{11})", parsed.path)
        if m:
            return m.group(1)
    return None


def parse_relative_time_to_datetime(published_text):
    """Konversi teks relatif YouTube ('2 days ago', '1 bulan yang lalu') ke DATETIME string."""
    if not published_text or published_text == "N/A":
        return None

    now = datetime.now()
    text = published_text.lower().strip()

    match = re.search(
        r"(\d+)\s*(second|minute|hour|day|week|month|year|detik|menit|jam|hari|minggu|bulan|tahun)",
        text,
    )
    if not match:
        return None

    val = int(match.group(1))
    unit = match.group(2)

    if unit in ("second", "detik"):
        dt = now - timedelta(seconds=val)
    elif unit in ("minute", "menit"):
        dt = now - timedelta(minutes=val)
    elif unit in ("hour", "jam"):
        dt = now - timedelta(hours=val)
    elif unit in ("day", "hari"):
        dt = now - timedelta(days=val)
    elif unit in ("week", "minggu"):
        dt = now - timedelta(weeks=val)
    elif unit in ("month", "bulan"):
        dt = now - timedelta(days=val * 30)
    elif unit in ("year", "tahun"):
        dt = now - timedelta(days=val * 365)
    else:
        dt = now

    return dt.strftime(DT_FORMAT)


# --------------------------------------------------------------------------- #
# Sumber tanggal
# --------------------------------------------------------------------------- #

def fetch_published_yt_dlp(video_id):
    """Sumber utama: yt-dlp (tanpa download). Return (datetime_str | None, error | None).

    Waktu dikonversi ke zona waktu lokal mesin (konsisten dengan datetime.now()
    yang dipakai yt_scraper_v2.py).
    """
    url = f"https://www.youtube.com/watch?v={video_id}"
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:  # video private/dihapus/region lock, dsb.
        return None, str(e).splitlines()[0][:200]

    ts = info.get("timestamp") or info.get("release_timestamp")
    if ts:
        return datetime.fromtimestamp(ts).strftime(DT_FORMAT), None

    upload_date = info.get("upload_date")  # 'YYYYMMDD'
    if upload_date:
        try:
            return datetime.strptime(upload_date, "%Y%m%d").strftime(DT_FORMAT), None
        except ValueError:
            pass

    return None, "yt-dlp tidak mengembalikan tanggal upload"


def fetch_published_scrapetube(video_id, title):
    """Fallback: cari berdasarkan judul via scrapetube, ambil teks relatif (perkiraan)."""
    if not title:
        return None
    try:
        for video in scrapetube.get_search(query=title, limit=20, results_type="video"):
            if video.get("videoId") == video_id:
                text = video.get("publishedTimeText", {}).get("simpleText", "")
                return parse_relative_time_to_datetime(text)
    except Exception:
        return None
    return None


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main():
    parser = argparse.ArgumentParser(description="Isi published_at YouTube yang NULL di tabel contents.")
    parser.add_argument("--limit", type=int, default=0, help="Maksimal video diproses (0 = semua).")
    parser.add_argument("--sleep", type=float, default=1.0, help="Jeda detik antar video (default 1.0).")
    parser.add_argument("--dry-run", action="store_true", help="Hanya tampilkan hasil, tidak update DB.")
    parser.add_argument("--no-fallback", action="store_true", help="Matikan fallback scrapetube.")
    parser.add_argument("--platform-id", type=int, default=YOUTUBE_PLATFORM_ID, help="ID platform YouTube (default 5).")
    args = parser.parse_args()

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    query = (
        "SELECT id, original_id, clean_url, title "
        "FROM contents WHERE platform_id = %s AND published_at IS NULL ORDER BY id"
    )
    params = [args.platform_id]
    if args.limit > 0:
        query += " LIMIT %s"
        params.append(args.limit)

    cursor.execute(query, params)
    rows = cursor.fetchall()
    total = len(rows)
    print(f"[*] Ditemukan {total} konten YouTube dengan published_at NULL.")
    if args.dry_run:
        print("[*] MODE DRY-RUN: DB tidak akan diubah.")

    updated = failed = 0

    try:
        for no, row in enumerate(rows, start=1):
            content_id = row["id"]
            video_id = extract_video_id(row.get("original_id"), row.get("clean_url"))
            label = f"[{no}/{total}] id={content_id} video={video_id}"

            if not video_id:
                print(f"{label} -> GAGAL: video id tidak bisa ditentukan")
                failed += 1
                continue

            published_at, err = fetch_published_yt_dlp(video_id)
            source = "yt-dlp"

            if not published_at and not args.no_fallback:
                published_at = fetch_published_scrapetube(video_id, row.get("title"))
                source = "scrapetube (perkiraan)"

            if not published_at:
                print(f"{label} -> GAGAL: {err or 'tanggal tidak ditemukan'}")
                failed += 1
                time.sleep(args.sleep)
                continue

            if args.dry_run:
                print(f"{label} -> {published_at} via {source} (dry-run)")
                updated += 1
            else:
                try:
                    conn.ping(reconnect=True, attempts=3, delay=2)
                    cursor = conn.cursor(dictionary=True)
                    cursor.execute(
                        "UPDATE contents SET published_at = %s WHERE id = %s AND published_at IS NULL",
                        (published_at, content_id),
                    )
                    conn.commit()
                    print(f"{label} -> {published_at} via {source} [OK]")
                    updated += 1
                except Exception as e:
                    print(f"{label} -> GAGAL update DB: {e}")
                    failed += 1

            time.sleep(args.sleep)

    except KeyboardInterrupt:
        print("\n[!] Dihentikan oleh pengguna.")
    finally:
        try:
            cursor.close()
            conn.close()
        except Exception:
            pass

    print(f"\n[+] Selesai. Berhasil: {updated} | Gagal: {failed} | Total: {total}")
    if failed:
        print("[i] Yang gagal tetap NULL, jalankan ulang script ini untuk mencoba lagi.")


if __name__ == "__main__":
    main()