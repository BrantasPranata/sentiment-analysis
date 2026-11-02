import json
import logging
import re
import mysql.connector
from mysql.connector import Error
import requests
from bs4 import BeautifulSoup
from pathlib import Path
import sys

# Menambahkan root project directory (1 tingkat di atas folder script saat ini) ke sys.path
sys.path.append(str(Path(__file__).resolve().parent.parent))

from config.config import get_db_connection

# --- LOGGING SETUP ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "Chrome/122.0.0.0 Safari/537.36"
    )
}


def scrape_x_media_urls(clean_url: str) -> list:
    """
    Mengambil URL Gambar dari Post X via OpenGraph Meta Tag (og:image / twitter:image).
    """
    media_urls = []
    try:
        res = requests.get(clean_url, headers=HEADERS, timeout=8)
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, "html.parser")

            # Cari meta tag image
            og_image = (
                soup.find("meta", property="og:image")
                or soup.find("meta", attrs={"name": "twitter:image"})
                or soup.find("meta", attrs={"name": "og:image"})
            )

            if og_image and og_image.get("content"):
                img_url = og_image["content"]
                # Abaikan avatar profil bawaan X/Twitter
                if (
                    "profile_images" not in img_url
                    and "default_profile" not in img_url
                ):
                    media_urls.append(img_url)
    except Exception as e:
        logging.warning(f"Gagal scrape media dari [{clean_url}]: {e}")

    return media_urls


def update_existing_x_media():
    """Memindai record X yang media_urls-nya kosong, lalu meng-update ke database."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # 1. Ambil semua konten X (platform_id = 4) yang media_urls nya NULL atau '[]'
        query_select = """
            SELECT id, clean_url, title 
            FROM contents 
            WHERE platform_id = 4 
              AND (media_urls IS NULL OR media_urls = '[]' OR media_urls = '')
        """
        cursor.execute(query_select)
        rows = cursor.fetchall()

        total = len(rows)
        if total == 0:
            logging.info(
                "[+] Tidak ada record X yang perlu diperbarui (semua sudah terisi atau tidak berniat update)."
            )
            cursor.close()
            conn.close()
            return

        logging.info(
            f"[*] Ditemukan {total} data X di DB yang media_urls-nya kosong. Memulai pengerukan gambar..."
        )

        updated_count = 0
        no_media_count = 0

        for idx, row in enumerate(rows, start=1):
            content_id = row["id"]
            clean_url = row["clean_url"]
            title_snippet = row["title"][:50] if row["title"] else clean_url

            logging.info(
                f"[{idx}/{total}] Checking Media ID {content_id}: {title_snippet}..."
            )

            # Scrape media gambar
            media_urls = scrape_x_media_urls(clean_url)

            if media_urls:
                media_urls_json = json.dumps(media_urls)

                # Update baris di MySQL
                update_query = """
                    UPDATE contents 
                    SET media_urls = %s 
                    WHERE id = %s
                """
                cursor.execute(update_query, (media_urls_json, content_id))
                conn.commit()

                updated_count += 1
                logging.info(
                    f"   [UPDATED] Berhasil menambahkan {len(media_urls)} gambar -> {media_urls[0]}"
                )
            else:
                no_media_count += 1
                logging.info(
                    "   [NO MEDIA] Tweet ini tidak mengandung gambar/media."
                )

        cursor.close()
        conn.close()

        logging.info("=" * 60)
        logging.info(f"[*] PROSES UPDATE SELESAI!")
        logging.info(
            f"[+] Total Diperbarui dengan Gambar : {updated_count} record"
        )
        logging.info(
            f"[-] Tweet Tanpa Gambar           : {no_media_count} record"
        )
        logging.info("=" * 60)

    except Error as e:
        logging.error(f"[!] Database Error: {e}")


if __name__ == "__main__":
    update_existing_x_media()