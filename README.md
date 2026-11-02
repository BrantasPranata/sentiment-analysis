# Sentiment Analysis

Sistem monitoring sentimen publik (artikel berita, X/Twitter, Instagram, YouTube beserta komentarnya).
Gabungan project `FE+BE` (dashboard & API PHP) dan `SentimentAnalysisPSDTN2` (scraper & analisis sentimen Python).

```
                    ┌──────────────── jaringan internal Docker ────────────────┐
 browser ──► frontend (nginx + Svelte SPA) ── /api/* ──► backend (Laravel 13, PHP 8.5)
                                                              │
                                     pipeline (Python) ──► MySQL (lokal: XAMPP, server: DB prod) ◄┘
```

| Folder      | Isi                                                                  |
|-------------|----------------------------------------------------------------------|
| `frontend/` | Svelte 5 + Vite SPA, disajikan nginx; `/api/*` diteruskan ke backend |
| `backend/`  | Laravel 13 (PHP 8.5): REST API, login token Bearer                    |
| `pipeline/` | Python: scraper otomatis, analisis sentimen, helper, tool GUI        |
| `database/` | `init/`: dump database produksi + `ANALYZE TABLE` untuk database lokal |
| `deploy/`   | Contoh konfigurasi nginx server (reverse proxy)                      |

Browser hanya berbicara dengan **frontend**; backend tidak dibuka ke publik.

## Konfigurasi (env global)

Satu file env di root dipakai oleh backend, frontend, dan pipeline:

| File                | Untuk                                                         | Di-commit |
|---------------------|---------------------------------------------------------------|-----------|
| `default.env`       | lokal/testing: MariaDB XAMPP di komputer sendiri             | ya        |
| `.env.prod`         | produksi: database server (salin dari `.env.prod.example`)    | **tidak** |

- Docker: pilih file dengan `--env-file`.
- Tanpa Docker: backend (`php artisan serve`) dan pipeline (`python ...`) otomatis membaca `../default.env`;
  set `APP_ENV_FILE=.env.prod` untuk memakai file produksi.

## Menjalankan lokal (testing)

Database lokal = MariaDB XAMPP (bukan container); container mengaksesnya lewat `host.docker.internal`.

1. Jalankan MySQL di XAMPP Control Panel, lalu impor salinan database produksi (sekali saja):

   ```bash
   mysql -uroot -e "CREATE DATABASE db_sentimenanalisis_v2 CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci"
   zcat database/init/01-prod-dump.sql.gz | mysql -uroot db_sentimenanalisis_v2
   mysql -uroot db_sentimenanalisis_v2 < database/init/99-analyze-tables.sql   # wajib, kalau tidak query sangat lambat
   ```

2. Jalankan aplikasi:

   ```bash
   docker compose --env-file default.env up -d --build
   ```

- Dashboard: <http://localhost:8000>, login memakai akun yang sama dengan produksi.
- Agar lebih cepat, naikkan `innodb_buffer_pool_size` di `C:\xampp\mysql\bin\my.ini` (default XAMPP 16M, mis. jadi 256M).
- Mengambil ulang salinan database produksi (read-only), lalu ulangi langkah 1 (DROP database lokal dulu):

  ```bash
  docker run --rm -e MYSQL_PWD='<password>' mysql:5.7 mysqldump -h 192.0.2.10 -u root \
    --single-transaction --quick --routines --triggers --no-tablespaces --set-gtid-purged=OFF \
    db_sentimenanalisis_v2 | gzip > database/init/01-prod-dump.sql.gz
  ```

Mengembangkan frontend dengan hot reload (stack Docker di atas tetap jalan, `/api` diteruskan ke port 8000):

```bash
cd frontend && npm install && npx vite
```

## Pipeline: opsional

Secara default hanya **frontend + backend** yang dijalankan/di-deploy. Pipeline saat ini berjalan di komputer kantor
(langsung dengan Python, menulis ke database; set `APP_ENV_FILE=.env.prod` untuk database produksi).

### Deploy lengkap (3 service) - sudah disiapkan

| File                       | Fungsi                                                                 |
|----------------------------|------------------------------------------------------------------------|
| `.env.prod.full.example`   | template `.env.prod` dengan `COMPOSE_PROFILES=pipeline` & `TORCH_INDEX_URL` |
| `docker-compose.gpu.yml`   | pipeline memakai PyTorch CUDA + akses GPU NVIDIA                        |

Manual di server:

```bash
cp .env.prod.full.example .env.prod      # isi DB_*, APP_KEY, PROXY_NETWORK
docker compose --env-file .env.prod -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.gpu.yml up -d --build
# Server tanpa GPU: hilangkan "-f docker-compose.gpu.yml"
```

Syarat server GPU: driver NVIDIA + NVIDIA Container Toolkit (`docker run --rm --gpus all ubuntu nvidia-smi`).
Jangan lupa salin `ig_session.json` & `x_session.json` ke `pipeline/sessions/` di server.

## Produksi

```bash
cp .env.prod.example .env.prod   # isi DB_*, APP_KEY, APP_URL, PROXY_NETWORK
docker compose --env-file .env.prod -f docker-compose.yml -f docker-compose.prod.yml up -d
```

`docker-compose.prod.yml` menutup port host frontend dan memasukkannya ke network nginx server
(alias `sentiment-frontend`); pasang [deploy/nginx/sentiment-analysis.conf](deploy/nginx/sentiment-analysis.conf)
di nginx server. Database produksi diatur lewat `DB_*` di `.env.prod` (server database terpisah).


## Backend API

Semua endpoint (kecuali login) wajib header `Authorization: Bearer <token>`. Format JSON sama seperti API PHP lama:

| Method              | Endpoint        | Dulu (`api_sentimen/`) | Keterangan                              |
|---------------------|-----------------|------------------------|-----------------------------------------|
| POST                | `/api/login`    | `login.php`            | `{username, password}` → token (8 jam)  |
| GET                 | `/api/contents` | `index.php`            | filter, pagination, `summary=1`         |
| GET                 | `/api/comments` | `komen_id.php`         | `?content_id=`                          |
| GET                 | `/api/insight`  | `insight.php`          | `?top=10&nocache=1`, cache 5 menit      |
| GET                 | `/api/keywords` | `platform_id.php`      | `?platform_id=` / `?platform_code=`     |
| POST / PUT / DELETE | `/api/keywords` | `platform_id.php`      | khusus role `admin`                     |

Kode: [routes/api.php](backend/routes/api.php), [Controllers/Api](backend/app/Http/Controllers/Api),
[AuthenticateToken.php](backend/app/Http/Middleware/AuthenticateToken.php). Skema database tidak dikelola Laravel.

## Frontend

Dashboard lama dipindahkan apa adanya ke SPA Svelte: markup di
[src/legacy/dashboard.html](frontend/src/legacy/dashboard.html), logika di
[src/legacy/dashboard.js](frontend/src/legacy/dashboard.js), dirender oleh [App.svelte](frontend/src/App.svelte).
Bagian-bagiannya bisa dipecah menjadi komponen Svelte secara bertahap.

## Pipeline (Python)

```
pipeline/
├── config/config.py      koneksi DB & folder runtime (dari env global)
├── scrapers/             scraper otomatis: artikel, X, Instagram, YouTube, komentar
├── sentiment/            sentimen konten & komentar (IndoBERT, CLIP, EasyOCR, Qwen)
├── helpers/              terjemah deskripsi YT, tanggal terbit YT, media X
├── gui/                  tool desktop (tkinter) - tidak ikut ke image Docker
├── docker/scheduler.sh   entrypoint container: jalankan pipeline tiap N menit
├── run_pipeline.sh / .command / .bat
├── sessions/             sesi login X & Instagram (rahasia, tidak di-commit)
```

Lokal tanpa Docker: `pip install -r requirements.txt` (Mac: `requirements-mac.txt`), `playwright install chromium`,
lalu `./run_pipeline.sh` atau satu script, mis. `python scrapers/auto_x_scraper.py -l 5`.

- **Login X / Instagram** butuh browser tampil, lakukan di desktop (`python scrapers/auto_ig_scraper.py --login`,
  `python scrapers/comments_scraper.py --login x`), lalu salin `*_session.json` ke `pipeline/sessions/` di server.
- **Verifikator Qwen (MLX)** hanya jalan di Mac Apple Silicon; di Docker/Linux otomatis dilewati.
- Cache model tersimpan di volume `pipeline-cache`. Jeda antar putaran: `PIPELINE_INTERVAL_MINUTES`.
