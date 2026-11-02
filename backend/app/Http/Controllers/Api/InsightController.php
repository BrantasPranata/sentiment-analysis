<?php

namespace App\Http\Controllers\Api;

use App\Http\Controllers\Controller;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\Cache;
use Illuminate\Support\Facades\DB;

/**
 * GET /api/insight?top=10&nocache=1  (dulu: api_sentimen/insight.php)
 * Data grafik insight dashboard.
 */
class InsightController extends Controller
{
    // Daftar topik (dipakai QUERY 2 & QUERY 4)
    private const TOPICS = ['mahal', 'langka', 'mafia', 'swasembada', 'andi amran sulaiman', 'subsidi', 'menteri pertanian'];

    // Cache hasil asosiasi topik (proses ini membaca seluruh teks komentar yang cocok)
    private const CACHE_TTL = 300;

    // Kata umum yang diabaikan (stopword Indonesia + bahasa gaul media sosial)
    private const STOPWORDS =
        'yang dan di ke dari ini itu untuk dengan pada adalah tidak nya lah kah pun dong deh sih kok nih tuh lho yah ' .
        'aja saja juga sudah udah belum akan bisa ada saya aku kita kami kamu anda dia mereka lebih sangat atau karena ' .
        'jadi kalau kalo bukan hanya cuma sama para oleh dalam agar supaya sebagai saat ketika masih harus mau lagi apa ' .
        'siapa bagaimana gimana kenapa mengapa yg dgn utk dr krn tdk jg sdh blm udh bgt tapi namun serta tentang semua ' .
        'setiap banyak sedikit orang hal kan gak ngga nggak enggak tak ndak bgitu begitu begini gini gitu sebuah suatu ' .
        'seperti sampai hingga bahwa jika maka sehingga tetapi dapat bisa lalu terus kayak kaya macam pak bapak bu ibu ' .
        'mas mbak bang kak wkwk wkwkwk haha hehe hihi rt amp via https http www com dgn dng sbg spt tsb yaitu yakni ' .
        'ketika setelah sebelum sedang telah pernah selalu sering kadang mungkin memang malah bahkan padahal walaupun ' .
        'meskipun kembali lagi sih banget bgt udah dah lah ya yaa iya yaudah ok oke kok ko sdg dlm pd ' .
        'dia nya ku mu kau engkau kalian kalian beliau ia mereka antara atas bawah luar dalam sini situ sana mana ' .
        'ini itu tersebut hari tahun bulan kali ada adanya adalah ialah merupakan menjadi membuat buat bikin ' .
        'melakukan lakukan dilakukan terhadap kepada daripada demi untuk soal masalah lain lainnya baik ' .
        'cukup kurang paling ter amat bagi bagaimanapun apakah apalagi apabila mari ayo yuk coba';

    public function index(Request $request): JsonResponse
    {
        $topN = (int) ($this->queryText($request, 'top') ?: 10);
        if ($topN < 3) { $topN = 3; }
        if ($topN > 15) { $topN = 15; }

        $cacheKey = 'topic_assoc_' . md5(implode('|', self::TOPICS) . '|' . $topN);
        if ($this->queryText($request, 'nocache') !== '') {
            Cache::forget($cacheKey);
        }
        $associations = Cache::remember($cacheKey, self::CACHE_TTL, fn () => $this->topicAssociations($topN));

        return $this->respond([
            "status" => true,
            "message" => "Berhasil mengambil data insight dashboard",
            "data" => [
                "keyword_clustering" => $this->keywordClustering(),
                "topic_sentiment_insight" => $this->topicSentimentInsight(),
                "quadrant_analysis" => $this->quadrantAnalysis(),
                "topic_associations" => $associations,
            ],
        ]);
    }

    /**
     * QUERY 1: Klaster Positif & Negatif Ekstrem (Keyword Matching)
     */
    private function keywordClustering(): array
    {
        $rows = $this->rows(
            "SELECT
                k.keyword AS kata_kunci,
                k.sentimen,
                COUNT(c.id) AS jumlah_kemunculan
            FROM (
                SELECT 'mantap' AS keyword, 'Positif' AS sentimen UNION ALL
                SELECT 'bravo' AS keyword, 'Positif' AS sentimen UNION ALL
                SELECT 'jaya' AS keyword, 'Positif' AS sentimen UNION ALL
                SELECT 'keren' AS keyword, 'Positif' AS sentimen UNION ALL
                SELECT 'unggul' AS keyword, 'Positif' AS sentimen UNION ALL
                SELECT 'swasembada' AS keyword, 'Positif' AS sentimen UNION ALL
                SELECT 'sukses' AS keyword, 'Positif' AS sentimen UNION ALL
                SELECT 'mahal' AS keyword, 'Negatif' AS sentimen UNION ALL
                SELECT 'langka' AS keyword, 'Negatif' AS sentimen UNION ALL
                SELECT 'susah' AS keyword, 'Negatif' AS sentimen UNION ALL
                SELECT 'mafia' AS keyword, 'Negatif' AS sentimen UNION ALL
                SELECT 'kecewa' AS keyword, 'Negatif' AS sentimen UNION ALL
                SELECT 'jelek' AS keyword, 'Negatif' AS sentimen UNION ALL
                SELECT 'palsu' AS keyword, 'Negatif' AS sentimen UNION ALL
                SELECT 'ribet' AS keyword, 'Negatif' AS sentimen
            ) k
            JOIN comments c ON c.comment_text LIKE CONCAT('%', k.keyword, '%')
            GROUP BY k.keyword, k.sentimen
            ORDER BY jumlah_kemunculan DESC"
        );

        return array_map(fn ($row) => [
            "kata_kunci" => $row['kata_kunci'],
            "sentimen" => $row['sentimen'],
            "jumlah_kemunculan" => (int) $row['jumlah_kemunculan'],
        ], $rows);
    }

    /**
     * QUERY 2: Insight Topik vs Sentimen AI & Confidence Score
     */
    private function topicSentimentInsight(): array
    {
        // Daftar topik di sini samakan dengan self::TOPICS bila diubah
        $rows = $this->rows(
            "SELECT
                tk.keyword AS topik_pembicaraan,
                sr.sentiment AS hasil_sentimen_ai,
                COUNT(c.id) AS jumlah_komentar,
                ROUND(AVG(sr.confidence_score), 2) AS rata_rata_confidence,
                MAX(sr.confidence_score) AS max_confidence,
                MIN(sr.confidence_score) AS min_confidence
            FROM (
                SELECT 'mahal' AS keyword UNION ALL
                SELECT 'langka' AS keyword UNION ALL
                SELECT 'mafia' AS keyword UNION ALL
                SELECT 'swasembada' AS keyword UNION ALL
                SELECT 'andi amran sulaiman' AS keyword UNION ALL
                SELECT 'subsidi' AS keyword UNION ALL
                SELECT 'menteri pertanian' AS keyword
            ) AS tk
            JOIN comments c ON c.comment_text LIKE CONCAT('%', tk.keyword, '%')
            JOIN sentiment_results sr ON sr.target_id = c.id AND sr.target_type = 'comment'
            GROUP BY tk.keyword, sr.sentiment
            ORDER BY tk.keyword, jumlah_komentar DESC"
        );

        return array_map(fn ($row) => [
            "topik_pembicaraan" => $row['topik_pembicaraan'],
            "hasil_sentimen_ai" => $row['hasil_sentimen_ai'],
            "jumlah_komentar" => (int) $row['jumlah_komentar'],
            "rata_rata_confidence" => (float) $row['rata_rata_confidence'],
            "max_confidence" => (float) $row['max_confidence'],
            "min_confidence" => (float) $row['min_confidence'],
        ], $rows);
    }

    /**
     * QUERY 3: Kuadran Analisis (Strong/Weak Sentiments)
     */
    private function quadrantAnalysis(): array
    {
        $rows = $this->rows(
            "SELECT
                CASE
                    WHEN sentiment = 'positive' AND confidence_score >= 0.75 THEN 'Q1 - Strong Positive (Apresiasi Jelas)'
                    WHEN sentiment = 'positive' AND confidence_score < 0.75 THEN 'Q2 - Weak Positive (Ambigu / Sarkas Halus)'
                    WHEN sentiment = 'negative' AND confidence_score >= 0.75 THEN 'Q3 - Strong Negative (Krisis / Keluhan Valid)'
                    WHEN sentiment = 'negative' AND confidence_score < 0.75 THEN 'Q4 - Weak Negative (Sentimen Campuran / Kurang Relevan)'
                    ELSE 'Netral / Tidak Terklasifikasi'
                END AS kuadran_analisis,
                COUNT(1) AS total_kasus,
                ROUND(AVG(confidence_score), 3) AS rata_rata_confidence
            FROM sentiment_results
            WHERE sentiment IN ('positive', 'negative')
              AND confidence_score IS NOT NULL
            GROUP BY kuadran_analisis
            ORDER BY kuadran_analisis"
        );

        return array_map(fn ($row) => [
            "kuadran_analisis" => $row['kuadran_analisis'],
            "total_kasus" => (int) $row['total_kasus'],
            "rata_rata_confidence" => (float) $row['rata_rata_confidence'],
        ], $rows);
    }

    /**
     * QUERY 4: Keterkaitan Topik -> Kata Lain (co-occurrence)
     * Untuk tiap topik pembicaraan: kata apa saja yang paling sering muncul BERSAMA topik itu
     * dalam komentar yang sama (mis. mafia -> pangan, beras, impor, ...), beserta sentimen
     * komentar tempat kata itu muncul. Dihitung lewat satu pass tabel comments.
     */
    private function topicAssociations(int $topN): array
    {
        @set_time_limit(60);

        $stop = array_flip(explode(' ', self::STOPWORDS));
        $topics = self::TOPICS;

        $likes = [];
        $bindings = [];
        $own = [];
        $counts = [];
        $totals = [];
        foreach ($topics as $t) {
            $likes[] = "c.comment_text LIKE ?";
            $bindings[] = '%' . $t . '%';
            $own[$t] = array_flip(preg_split('/\s+/u', $t));   // kata penyusun topik itu sendiri tidak ditampilkan
            $counts[$t] = [];
            $totals[$t] = 0;
        }

        $sql = "SELECT c.comment_text, LOWER(sr.sentiment) AS sentiment
                FROM comments c
                LEFT JOIN sentiment_results sr ON sr.target_id = c.id AND sr.target_type = 'comment'
                WHERE " . implode(' OR ', $likes);

        // cursor(): baris dibaca satu per satu (hemat memori)
        foreach (DB::cursor($sql, $bindings) as $row) {
            $text = mb_strtolower((string) $row->comment_text, 'UTF-8');

            $hit = [];
            foreach ($topics as $t) {
                if (str_contains($text, $t)) { $hit[] = $t; }
            }
            if (!$hit) { continue; }
            foreach ($hit as $t) { $totals[$t]++; }

            $clean = preg_replace('~https?://\S+|www\.\S+|@\w+~u', ' ', $text);
            if ($clean === null) { $clean = $text; }
            if (!preg_match_all('/\p{L}{3,20}/u', $clean, $m)) { continue; }
            $uniq = array_unique($m[0]);          // 1 kata dihitung 1x per komentar
            $s = $row->sentiment;
            $idx = ($s === 'positive') ? 1 : (($s === 'negative') ? 2 : (($s === 'neutral') ? 3 : 0));

            foreach ($hit as $t) {
                foreach ($uniq as $w) {
                    if (isset($stop[$w]) || isset($own[$t][$w])) { continue; }
                    if (!isset($counts[$t][$w])) { $counts[$t][$w] = [0, 0, 0, 0]; }
                    $counts[$t][$w][0]++;
                    if ($idx) { $counts[$t][$w][$idx]++; }
                }
            }
        }

        $out = [];
        foreach ($topics as $t) {
            $words = $counts[$t];
            uasort($words, fn ($a, $b) => $b[0] - $a[0]);
            $list = [];
            foreach ($words as $w => $c) {
                if ($c[0] < 2) { break; }              // minimal muncul di 2 komentar
                $list[] = [
                    "kata" => (string) $w,
                    "jumlah" => $c[0],
                    "positif" => $c[1],
                    "negatif" => $c[2],
                    "netral" => $c[3],
                ];
                if (count($list) >= $topN) { break; }
            }
            $out[] = [
                "topik_pembicaraan" => $t,
                "total_komentar" => $totals[$t],
                "kata_terkait" => $list,
            ];
        }

        return $out;
    }
}
