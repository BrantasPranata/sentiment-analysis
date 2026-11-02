<?php

namespace App\Http\Controllers\Api;

use App\Http\Controllers\Controller;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;

/**
 * GET /api/comments?content_id=681  (dulu: api_sentimen/komen_id.php)
 * Daftar komentar milik satu konten beserta hasil sentimennya.
 */
class CommentController extends Controller
{
    public function index(Request $request): JsonResponse
    {
        $contentId = (int) $this->queryText($request, 'content_id');

        if ($contentId <= 0) {
            return $this->fail(400, "Parameter 'content_id' wajib diisi!");
        }

        // Query JOIN Komentar & Sentimen Komentar
        $rows = $this->rows(
            "SELECT
                cm.id AS comment_id,
                cm.content_id,
                cm.comment_text,
                cm.author_name,
                cm.posted_at,
                cm.crawled_at,
                sr.sentiment,
                sr.confidence_score,
                sr.model_name,
                sr.status AS analysis_status
            FROM comments cm
            LEFT JOIN sentiment_results sr
                ON sr.target_id = cm.id AND sr.target_type = 'comment'
            WHERE cm.content_id = ?
            ORDER BY cm.id DESC",
            [$contentId]
        );

        $comments = [];
        foreach ($rows as $row) {
            $comments[] = [
                "id" => (int) $row['comment_id'],
                "content_id" => (int) $row['content_id'],
                "author_name" => $row['author_name'] ?: 'Anonim',
                "comment_text" => $row['comment_text'],
                "posted_at" => $row['posted_at'],
                "crawled_at" => $row['crawled_at'],
                "sentiment_analysis" => [
                    "status" => $row['analysis_status'] ?? 'pending',
                    "sentiment" => $row['sentiment'] ?? 'neutral',
                    "confidence_score" => $row['confidence_score'] !== null ? (float) $row['confidence_score'] : 0.0,
                    "model_name" => $row['model_name'] ?? '-',
                ],
            ];
        }

        return $this->respond([
            "status" => true,
            "content_id" => $contentId,
            "total_comments" => count($comments),
            "data" => $comments,
        ]);
    }
}
