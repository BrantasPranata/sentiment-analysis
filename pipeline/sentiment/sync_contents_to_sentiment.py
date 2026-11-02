import logging
import mysql.connector
from mysql.connector import Error
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


def sync_missing_contents_to_sentiment_queue():
    """
    Memindai tabel `contents` dan menambahkan seluruh `content_id` 
    yang belum terdaftar di `sentiment_results` dengan status 'pending'.
    """
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        logging.info("[*] Memeriksa data di tabel 'contents' vs 'sentiment_results'...")

        # Query untuk mencari ID di contents yang BELUM ada di sentiment_results (target_type = 'content')
        query_find_missing = """
            SELECT c.id, c.title, c.platform_id
            FROM contents c
            LEFT JOIN sentiment_results sr 
                   ON c.id = sr.target_id AND sr.target_type = 'content'
            WHERE sr.id IS NULL
        """
        cursor.execute(query_find_missing)
        missing_contents = cursor.fetchall()

        total_missing = len(missing_contents)

        if total_missing == 0:
            logging.info("[+] Semua data di 'contents' sudah terdaftar di 'sentiment_results'. Tidak ada yang perlu ditambahkan.")
            cursor.close()
            conn.close()
            return

        logging.info(f"[*] Ditemukan {total_missing} konten yang belum masuk antrean sentimen. Memasukkan ke antrean 'pending'...")

        # Insert batch ke sentiment_results
        query_insert_queue = """
            INSERT INTO sentiment_results (target_type, target_id, execution_source, status)
            VALUES ('content', %s, 'ml_pipeline', 'pending')
        """

        inserted_count = 0
        for item in missing_contents:
            content_id = item["id"]
            cursor.execute(query_insert_queue, (content_id,))
            inserted_count += 1

        conn.commit()

        logging.info("=" * 60)
        logging.info("[*] SINKRONISASI ANTREAN SENTIMEN SELESAI!")
        logging.info(f"[+] Total Berhasil Ditambahkan ke Pending : {inserted_count} record")
        logging.info("=" * 60)

        cursor.close()
        conn.close()

    except Error as e:
        logging.error(f"[!] Database Error: {e}")


if __name__ == "__main__":
    sync_missing_contents_to_sentiment_queue()