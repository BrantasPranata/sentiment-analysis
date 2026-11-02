# config/config.py
# Konfigurasi bersama untuk seluruh script pipeline.
# Semua nilai rahasia dibaca dari environment variable / file .env (lihat .env.example),
# JANGAN menulis password atau token langsung di kode.
import os
from pathlib import Path

import mysql.connector
from mysql.connector import Error

# Root folder pipeline (folder yang berisi config/, scrapers/, sentiment/, ...)
PIPELINE_DIR = Path(__file__).resolve().parent.parent

# Muat env global di root project (default.env untuk lokal, APP_ENV_FILE=.env.prod untuk produksi).
# Environment variable yang sudah ada (mis. dari docker compose) tidak akan ditimpa.
try:
    from dotenv import load_dotenv

    load_dotenv(PIPELINE_DIR.parent / os.getenv("APP_ENV_FILE", "default.env"))
except ImportError:
    pass


def _env_path(name: str, default: Path) -> Path:
    path = Path(os.getenv(name) or default)
    path.mkdir(parents=True, exist_ok=True)
    return path


# Folder runtime (dibuat otomatis). Di Docker folder ini di-mount sebagai volume.
LOG_DIR = _env_path("LOG_DIR", PIPELINE_DIR / "logs")          # file *.log
SESSION_DIR = _env_path("SESSION_DIR", PIPELINE_DIR / "sessions")  # sesi login X / Instagram (Playwright)
DATA_DIR = _env_path("DATA_DIR", PIPELINE_DIR / "data")         # file sementara & data yang gagal masuk DB

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "127.0.0.1"),
    "port": int(os.getenv("DB_PORT", "3306")),
    "user": os.getenv("DB_USERNAME", "root"),
    "password": os.getenv("DB_PASSWORD", ""),
    "database": os.getenv("DB_DATABASE", "db_sentimenanalisis_v2"),
    "raise_on_warnings": True,
    "connect_timeout": 60,
    "autocommit": True
}


def get_db_connection():
    """Mengembalikan koneksi database MySQL."""
    try:
        return mysql.connector.connect(**DB_CONFIG)
    except Error as e:
        print(f"[!] Gagal terhubung ke Database: {e}")
        raise e
