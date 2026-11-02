-- Dijalankan setelah dump diimpor (urutan nama file): perbarui statistik index.
-- Tanpa ini optimizer MySQL mengira tabel hampir kosong dan memilih full scan (query jadi sangat lambat).
ANALYZE TABLE comments, contents, keywords, platforms, sentiment_results, users;
