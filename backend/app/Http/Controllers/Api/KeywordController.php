<?php

namespace App\Http\Controllers\Api;

use App\Http\Controllers\Controller;
use Illuminate\Database\QueryException;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\DB;

/**
 * /api/keywords  (dulu: api_sentimen/platform_id.php)
 *  - GET                : semua role yang aktif
 *  - POST, PUT, DELETE  : hanya role 'admin' (diatur di routes/api.php)
 */
class KeywordController extends Controller
{
    /**
     * READ - GET ?platform_id=1  atau  ?platform_code=x
     */
    public function index(Request $request): JsonResponse
    {
        $platformId = (int) $this->queryText($request, 'platform_id');
        $platformCode = $this->queryText($request, 'platform_code');

        if ($platformId <= 0 && $platformCode === '') {
            return $this->fail(400, "Parameter 'platform_id' atau 'platform_code' wajib diisi!");
        }

        $sql = "SELECT
                    p.id AS platform_id,
                    p.code AS platform_code,
                    p.name AS platform_name,
                    p.rss_url,
                    k.id AS keyword_id,
                    k.keyword,
                    k.is_active,
                    k.created_at,
                    k.updated_at
                FROM platforms p
                LEFT JOIN keywords k ON p.id = k.platform_id";

        $rows = $platformId > 0
            ? $this->rows($sql . " WHERE p.id = ?", [$platformId])
            : $this->rows($sql . " WHERE p.code = ?", [$platformCode]);

        if (!$rows) {
            return $this->fail(404, "Platform tidak ditemukan.");
        }

        $platformInfo = [
            "id" => (int) $rows[0]['platform_id'],
            "code" => $rows[0]['platform_code'],
            "name" => $rows[0]['platform_name'],
            "rss_url" => $rows[0]['rss_url'],
        ];

        $keywords = [];
        foreach ($rows as $row) {
            if ($row['keyword_id'] !== null) {
                $keywords[] = [
                    "id" => (int) $row['keyword_id'],
                    "keyword" => $row['keyword'],
                    "is_active" => (bool) $row['is_active'],
                    "created_at" => $row['created_at'],
                    "updated_at" => $row['updated_at'],
                ];
            }
        }

        return $this->respond([
            "status" => true,
            "platform" => $platformInfo,
            "total_keywords" => count($keywords),
            "data" => $keywords,
        ]);
    }

    /**
     * CREATE - POST  Body: { "platform_id": 1, "keyword": "pertanian organik", "is_active": 1 }
     */
    public function store(Request $request): JsonResponse
    {
        $platformId = (int) $request->input('platform_id', 0);
        $keyword = trim((string) $request->input('keyword', ''));
        $isActive = (int) $request->input('is_active', 1);

        if ($platformId <= 0 || $keyword === '') {
            return $this->fail(400, "'platform_id' dan 'keyword' wajib diisi!");
        }

        try {
            $id = DB::table('keywords')->insertGetId([
                'platform_id' => $platformId,
                'keyword' => $keyword,
                'is_active' => $isActive,
            ]);
        } catch (QueryException $e) {
            return $this->fail(500, "Gagal menambahkan keyword: " . ($e->errorInfo[2] ?? $e->getMessage()));
        }

        return $this->respond([
            "status" => true,
            "message" => "Keyword berhasil ditambahkan.",
            "data" => [
                "id" => $id,
                "platform_id" => $platformId,
                "keyword" => $keyword,
                "is_active" => (bool) $isActive,
            ],
        ], 201);
    }

    /**
     * UPDATE - PUT  Body: { "id": 5, "keyword": "panen raya", "is_active": 1 }
     */
    public function update(Request $request): JsonResponse
    {
        $id = (int) $request->input('id', 0);
        $keyword = trim((string) $request->input('keyword', ''));
        $isActive = $request->input('is_active') !== null ? (int) $request->input('is_active') : null;

        if ($id <= 0) {
            return $this->fail(400, "Parameter 'id' keyword wajib diisi!");
        }

        // Update dinamis tergantung field mana yang dikirim
        $fields = [];
        if ($keyword !== '') { $fields['keyword'] = $keyword; }
        if ($isActive !== null) { $fields['is_active'] = $isActive; }

        if (!$fields) {
            return $this->fail(400, "Tidak ada data yang diperbarui ('keyword' atau 'is_active').");
        }

        try {
            $affected = DB::table('keywords')->where('id', $id)->update($fields);
        } catch (QueryException $e) {
            return $this->fail(500, "Gagal memperbarui keyword: " . ($e->errorInfo[2] ?? $e->getMessage()));
        }

        return $affected > 0
            ? $this->respond(["status" => true, "message" => "Keyword berhasil diperbarui."])
            : $this->fail(404, "Keyword tidak ditemukan atau tidak ada perubahan data.");
    }

    /**
     * DELETE - Query Parameter: ?id=5  atau Body JSON: { "id": 5 }
     */
    public function destroy(Request $request): JsonResponse
    {
        $id = (int) ($this->queryText($request, 'id') ?: $request->input('id', 0));

        if ($id <= 0) {
            return $this->fail(400, "Parameter 'id' keyword wajib diisi!");
        }

        try {
            $affected = DB::table('keywords')->where('id', $id)->delete();
        } catch (QueryException $e) {
            return $this->fail(500, "Gagal menghapus keyword: " . ($e->errorInfo[2] ?? $e->getMessage()));
        }

        return $affected > 0
            ? $this->respond(["status" => true, "message" => "Keyword berhasil dihapus."])
            : $this->fail(404, "Keyword tidak ditemukan.");
    }
}
