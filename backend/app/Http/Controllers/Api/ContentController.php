<?php

namespace App\Http\Controllers\Api;

use App\Http\Controllers\Controller;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;

/**
 * GET /api/contents  (dulu: api_sentimen/index.php)
 *
 * Daftar konten + hasil sentimen, dengan filter & pagination di server.
 * Query: search, platform, sentiment, status, analyzed_from/to, published_from/to,
 *        page, limit, summary=1 (ringkasan seluruh data sesuai filter).
 */
class ContentController extends Controller
{
    private const DEFAULT_LIMIT = 30;
    private const MAX_LIMIT = 200;

    private const FROM_SQL = " FROM contents c
             INNER JOIN platforms p ON c.platform_id = p.id
             LEFT JOIN sentiment_results sr
                ON sr.target_id = c.id AND sr.target_type = 'content'";

    public function index(Request $request): JsonResponse
    {
        // ===================== PARAMETER =====================
        $limit = (int) $this->queryText($request, 'limit') ?: self::DEFAULT_LIMIT;
        if ($limit < 1) { $limit = self::DEFAULT_LIMIT; }
        if ($limit > self::MAX_LIMIT) { $limit = self::MAX_LIMIT; }

        $page = (int) $this->queryText($request, 'page');
        if ($page < 1) { $page = 1; }
        $offset = ($page - 1) * $limit;

        // summary=1 -> hitung total konten, total komentar, & rekap per platform (dipanggil saat filter berubah saja)
        $summaryParam = $this->queryText($request, 'summary');
        $withSummary = $summaryParam !== '' && $summaryParam !== '0';

        [$whereSql, $bindings] = $this->buildFilters($request);

        // ===================== DAFTAR KONTEN (SATU HALAMAN) =====================
        // NULL pada published_at otomatis berada di akhir untuk DESC di MySQL, sehingga
        // ORDER BY ini bisa memakai index (published_at, id).
        $sql = "SELECT
                    c.id AS content_id,
                    p.name AS platform_name,
                    p.code AS platform_code,
                    c.clean_url,
                    c.original_id,
                    c.title,
                    c.raw_text,
                    c.media_urls,
                    c.published_at,
                    c.crawled_at,
                    sr.sentiment,
                    sr.confidence_score,
                    sr.model_name,
                    sr.status AS analysis_status,
                    sr.analyzed_at"
                . self::FROM_SQL . $whereSql .
                " ORDER BY c.published_at DESC, c.id DESC
                  LIMIT ? OFFSET ?";

        // Ambil limit+1 baris untuk mengetahui apakah masih ada halaman berikutnya
        $rows = $this->rows($sql, array_merge($bindings, [$limit + 1, $offset]));
        $hasMore = count($rows) > $limit;
        if ($hasMore) { $rows = array_slice($rows, 0, $limit); }

        $commentMap = $this->commentSummaries($rows);

        $results = [];
        foreach ($rows as $row) {
            $mediaArray = !empty($row['media_urls']) ? json_decode($row['media_urls'], true) : [];
            if (!is_array($mediaArray)) { $mediaArray = []; }

            $cid = (int) $row['content_id'];
            $cr = $commentMap[$cid] ?? null;

            $results[] = [
                "id" => $cid,
                "platform" => [
                    "code" => $row['platform_code'],
                    "name" => $row['platform_name'],
                ],
                "title" => $row['title'],
                "clean_url" => $row['clean_url'],
                "original_id" => $row['original_id'],
                "raw_text" => $row['raw_text'],
                "media_urls" => $mediaArray,
                "sentiment_analysis" => [
                    "status" => $row['analysis_status'] ?? 'pending',
                    "sentiment" => $row['sentiment'],
                    "confidence_score" => $row['confidence_score'] !== null ? (float) $row['confidence_score'] : null,
                    "model_name" => $row['model_name'],
                    "analyzed_at" => $row['analyzed_at'],
                ],
                "comments_summary" => [
                    "total" => $cr ? (int) $cr['total_comments'] : 0,
                    "positive" => $cr ? (int) $cr['positive_comments'] : 0,
                    "negative" => $cr ? (int) $cr['negative_comments'] : 0,
                    "neutral" => $cr ? (int) $cr['neutral_comments'] : 0,
                ],
                "published_at" => $row['published_at'],
                "crawled_at" => $row['crawled_at'],
            ];
        }

        $summary = $withSummary ? $this->summary($whereSql, $bindings) : null;
        $total = $summary !== null ? $summary["contents"]["total"] : null;

        // ===================== OUTPUT =====================
        $response = [
            "status" => true,
            "total_data" => count($results),
            "pagination" => [
                "page" => $page,
                "limit" => $limit,
                "has_more" => $hasMore,
                "total" => $total,
                "total_pages" => $total !== null ? max(1, (int) ceil($total / $limit)) : null,
            ],
            "data" => $results,
        ];
        if ($summary !== null) { $response["summary"] = $summary; }

        return $this->respond($response);
    }

    /**
     * Bangun klausa WHERE dari query string. Semua filter dikerjakan di server
     * supaya total & grafik mencakup SELURUH data yang cocok, bukan hanya satu halaman.
     *
     * @return array{0: string, 1: array}
     */
    private function buildFilters(Request $request): array
    {
        $where = [];
        $bindings = [];

        // Pencarian judul / isi
        $search = $this->queryText($request, 'search');
        if ($search !== '') {
            $like = '%' . $this->likeEscape($search) . '%';
            $where[] = "(c.title LIKE ? OR c.raw_text LIKE ?)";
            array_push($bindings, $like, $like);
        }

        // Platform (kode/nama mengandung kata kunci; 'x' dicocokkan persis / twitter)
        $platform = strtolower($this->queryText($request, 'platform'));
        if ($platform !== '') {
            if ($platform === 'x') {
                $where[] = "(LOWER(p.code) = 'x' OR LOWER(p.name) = 'x' OR LOWER(p.code) LIKE '%twitter%' OR LOWER(p.name) LIKE '%twitter%')";
            } else {
                $like = '%' . $this->likeEscape($platform) . '%';
                $where[] = "(LOWER(p.code) LIKE ? OR LOWER(p.name) LIKE ?)";
                array_push($bindings, $like, $like);
            }
        }

        // Sentimen konten
        $sentiment = strtolower($this->queryText($request, 'sentiment'));
        if (in_array($sentiment, ['positive', 'negative', 'neutral'], true)) {
            $where[] = "LOWER(sr.sentiment) = ?";
            $bindings[] = $sentiment;
        }

        // Status analisis (konten tanpa baris sentiment_results dianggap 'pending')
        $status = strtolower($this->queryText($request, 'status'));
        if ($status === 'pending') {
            $where[] = "(sr.status IS NULL OR LOWER(sr.status) = 'pending')";
        } elseif (in_array($status, ['completed', 'failed'], true)) {
            $where[] = "LOWER(sr.status) = ?";
            $bindings[] = $status;
        }

        // Rentang tanggal analisis & tanggal terbit (YYYY-MM-DD, inklusif)
        $ranges = [
            ['analyzed_from',  'analyzed_to',  'sr.analyzed_at'],
            ['published_from', 'published_to', 'c.published_at'],
        ];
        foreach ($ranges as [$fromKey, $toKey, $column]) {
            $from = $this->queryText($request, $fromKey);
            $to = $this->queryText($request, $toKey);
            if ($from !== '' && $this->validDate($from)) {
                $where[] = $column . " >= ?";
                $bindings[] = $from . ' 00:00:00';
            }
            if ($to !== '' && $this->validDate($to)) {
                $where[] = $column . " < DATE_ADD(?, INTERVAL 1 DAY)";
                $bindings[] = $to;
            }
        }

        $whereSql = $where ? ' WHERE ' . implode(' AND ', $where) : '';

        return [$whereSql, $bindings];
    }

    /**
     * Rekap komentar HANYA untuk konten di halaman ini (bukan seluruh tabel comments).
     */
    private function commentSummaries(array $rows): array
    {
        if (!$rows) {
            return [];
        }

        $ids = array_map(fn ($r) => (int) $r['content_id'], $rows);
        $placeholders = implode(',', array_fill(0, count($ids), '?'));

        $sql = "SELECT
                    cm.content_id,
                    COUNT(cm.id) AS total_comments,
                    SUM(LOWER(csr.sentiment) = 'positive') AS positive_comments,
                    SUM(LOWER(csr.sentiment) = 'negative') AS negative_comments,
                    SUM(LOWER(csr.sentiment) = 'neutral')  AS neutral_comments
                 FROM comments cm
                 LEFT JOIN sentiment_results csr
                    ON csr.target_id = cm.id AND csr.target_type = 'comment'
                 WHERE cm.content_id IN ($placeholders)
                 GROUP BY cm.content_id";

        $map = [];
        foreach ($this->rows($sql, $ids) as $cr) {
            $map[(int) $cr['content_id']] = $cr;
        }

        return $map;
    }

    /**
     * Ringkasan seluruh data (sesuai filter): total konten, total komentar, & rekap per platform.
     */
    private function summary(string $whereSql, array $bindings): array
    {
        // Konten per platform
        $contentRows = $this->rows(
            "SELECT p.id AS pid, p.code, p.name,
                    COUNT(*) AS total,
                    SUM(LOWER(sr.sentiment) = 'positive') AS positive,
                    SUM(LOWER(sr.sentiment) = 'negative') AS negative,
                    SUM(LOWER(sr.sentiment) = 'neutral')  AS neutral"
            . self::FROM_SQL . $whereSql .
            " GROUP BY p.id, p.code, p.name",
            $bindings
        );

        // Komentar per platform (hanya komentar milik konten yang lolos filter)
        $commentRows = $this->rows(
            "SELECT p.id AS pid,
                    COUNT(cm.id) AS total,
                    SUM(LOWER(csr.sentiment) = 'positive') AS positive,
                    SUM(LOWER(csr.sentiment) = 'negative') AS negative,
                    SUM(LOWER(csr.sentiment) = 'neutral')  AS neutral"
            . self::FROM_SQL .
            " INNER JOIN comments cm ON cm.content_id = c.id
              LEFT JOIN sentiment_results csr
                 ON csr.target_id = cm.id AND csr.target_type = 'comment'"
            . $whereSql .
            " GROUP BY p.id",
            $bindings
        );

        $tc = ["total" => 0, "positive" => 0, "negative" => 0, "neutral" => 0];
        $tm = ["total" => 0, "positive" => 0, "negative" => 0, "neutral" => 0];
        $platforms = [];

        foreach ($contentRows as $r) {
            $pid = (int) $r['pid'];
            $platforms[$pid] = [
                "code" => $r['code'],
                "name" => $r['name'],
                "total" => (int) $r['total'],
                "positive" => (int) $r['positive'],
                "negative" => (int) $r['negative'],
                "neutral" => (int) $r['neutral'],
                "c_total" => 0, "c_positive" => 0, "c_negative" => 0, "c_neutral" => 0,
            ];
            foreach (array_keys($tc) as $k) { $tc[$k] += (int) $r[$k]; }
        }
        foreach ($commentRows as $r) {
            $pid = (int) $r['pid'];
            if (!isset($platforms[$pid])) { continue; }
            foreach (array_keys($tm) as $k) {
                $platforms[$pid]["c_" . $k] = (int) $r[$k];
                $tm[$k] += (int) $r[$k];
            }
        }

        return [
            "contents" => $tc,
            "comments" => $tm,
            "platforms" => array_values($platforms),
        ];
    }

    // Escape karakter spesial LIKE agar input user dibaca apa adanya
    private function likeEscape(string $s): string
    {
        return str_replace(['\\', '%', '_'], ['\\\\', '\\%', '\\_'], $s);
    }

    private function validDate(string $s): bool
    {
        if (!preg_match('/^(\d{4})-(\d{2})-(\d{2})$/', $s, $m)) { return false; }

        return checkdate((int) $m[2], (int) $m[3], (int) $m[1]);
    }
}
