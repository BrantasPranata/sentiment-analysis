<?php

namespace App\Http\Controllers;

use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\DB;

abstract class Controller
{
    /**
     * Balas JSON dengan format yang sama seperti API lama (tanpa escape unicode & slash).
     */
    protected function respond(array $data, int $status = 200): JsonResponse
    {
        return response()->json($data, $status, [], JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE);
    }

    protected function fail(int $status, string $message): JsonResponse
    {
        return $this->respond(["status" => false, "message" => $message], $status);
    }

    /**
     * Ambil parameter query string sebagai teks yang sudah di-trim ('' jika tidak ada / bukan teks).
     */
    protected function queryText(Request $request, string $key): string
    {
        $value = $request->query($key);

        return is_scalar($value) ? trim((string) $value) : '';
    }

    /**
     * Jalankan query (prepared statement) lalu kembalikan semua baris sebagai array asosiatif.
     */
    protected function rows(string $sql, array $bindings = []): array
    {
        return array_map(fn ($row) => (array) $row, DB::select($sql, $bindings));
    }
}
