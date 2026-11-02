import logging
import mysql.connector
from mysql.connector import Error
import argostranslate.package
import argostranslate.translate
from pathlib import Path
import sys
import ssl

ssl._create_default_https_context = ssl._create_unverified_context

# Menambahkan root project directory (1 tingkat di atas folder script saat ini) ke sys.path
sys.path.append(str(Path(__file__).resolve().parent.parent))

from config.config import get_db_connection

# --- LOGGING SETUP ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)


def setup_offline_translator():
    """
    Mengunduh dan memasang paket bahasa (English -> Indonesian) secara otomatis
    jika belum terinstal di mesin lokal.
    """
    logging.info("[*] Memeriksa paket bahasa offline (English -> Indonesian)...")
    
    # Update index paket bahasa
    
    argostranslate.package.update_package_index()
    available_packages = argostranslate.package.get_available_packages()

    # Cari paket en -> id
    package_to_install = next(
        (pkg for pkg in available_packages if pkg.from_code == "en" and pkg.to_code == "id"),
        None
    )

    if package_to_install:
        # Cek apakah sudah terinstall
        installed_packages = argostranslate.package.get_installed_packages()
        is_installed = any(
            pkg.from_code == "en" and pkg.to_code == "id" for pkg in installed_packages
        )

        if not is_installed:
            logging.info("[*] Mengunduh paket terjemahan en -> id (dilakukan sekali saja)...")
            download_path = package_to_install.download()
            argostranslate.package.install_from_path(download_path)
            logging.info("[+] Paket bahasa berhasil diinstall!")
        else:
            logging.info("[+] Paket bahasa en -> id sudah siap digunakan.")
    else:
        logging.error("[!] Paket terjemahan English -> Indonesian tidak ditemukan di indeks repository.")


def translate_text_offline(text: str) -> str:
    """Menerjemahkan teks dari Bahasa Inggris ke Bahasa Indonesia secara offline."""
    if not text or not text.strip():
        return text

    try:
        translated_text = argostranslate.translate.translate(text, "en", "id")
        return translated_text
    except Exception as e:
        logging.warning(f"Gagal menerjemahkan teks offline: {e}")
        return text


def is_mostly_english(text: str) -> bool:
    """Mendeteksi apakah teks dominan Bahasa Inggris."""
    english_words = {
        "the", "and", "is", "in", "to", "of", "for", "with", "on", 
        "at", "by", "from", "description", "summary", "video", "transcript"
    }
    indonesian_words = {
        "yang", "dan", "di", "ke", "dari", "untuk", "dengan", "ini", 
        "itu", "deskripsi", "ringkasan", "transkrip"
    }

    words = set(text.lower().split())
    en_score = len(words.intersection(english_words))
    id_score = len(words.intersection(indonesian_words))

    return en_score > id_score


def process_translate_youtube_contents():
    """Memindai record YouTube di DB dan menerjemahkan `raw_text` ke Bahasa Indonesia."""
    # Persiapkan model penerjemah offline
    setup_offline_translator()

    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # Ambil semua konten YouTube (platform_id = 5)
        query_select = """
            SELECT id, clean_url, title, raw_text 
            FROM contents 
            WHERE platform_id = 5 
              AND raw_text IS NOT NULL 
              AND raw_text != ''
        """
        cursor.execute(query_select)
        rows = cursor.fetchall()

        total = len(rows)
        if total == 0:
            logging.info("[+] Tidak ada konten YouTube ditemukan di database.")
            cursor.close()
            conn.close()
            return

        logging.info(f"[*] Ditemukan {total} data YouTube di DB. Memulai pemrosesan offline...")

        translated_count = 0
        skipped_count = 0

        for idx, row in enumerate(rows, start=1):
            content_id = row["id"]
            title_snippet = row["title"][:50] if row["title"] else row["clean_url"]
            raw_text = row["raw_text"]

            if is_mostly_english(raw_text):
                logging.info(f"[{idx}/{total}] Menerjemahkan ID {content_id}: {title_snippet}...")
                
                # Terjemahkan secara lokal tanpa internet/delay/rate limit
                translated_text = translate_text_offline(raw_text)

                if translated_text and translated_text != raw_text:
                    update_query = "UPDATE contents SET raw_text = %s WHERE id = %s"
                    cursor.execute(update_query, (translated_text, content_id))
                    conn.commit()

                    translated_count += 1
                    logging.info("   [SUCCESS] Berhasil diterjemahkan (Offline).")
                else:
                    skipped_count += 1
                    logging.info("   [SKIP] Hasil terjemahan sama atau gagal.")
            else:
                skipped_count += 1
                logging.info(f"[{idx}/{total}] [SKIP] ID {content_id} sudah berbahasa Indonesia.")

        cursor.close()
        conn.close()

        logging.info("=" * 60)
        logging.info("[*] PROSES TRANSLATE OFFLINE SELESAI!")
        logging.info(f"[+] Total Berhasil Diterjemahkan : {translated_count} record")
        logging.info(f"[-] Total Dilewati                : {skipped_count} record")
        logging.info("=" * 60)

    except Error as e:
        logging.error(f"[!] Database Error: {e}")


if __name__ == "__main__":
    process_translate_youtube_contents()