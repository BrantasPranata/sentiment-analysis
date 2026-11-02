<?php

namespace App\Http\Middleware;

use Closure;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\DB;
use Symfony\Component\HttpFoundation\Response;

/**
 * Middleware token Bearer (token dibuat oleh AuthController@login).
 *
 * Pemakaian di routes:  ->middleware('auth.token')        semua role yang sudah login
 *                       ->middleware('auth.token:admin')  hanya role tertentu
 *
 * Data user yang sedang login tersedia di $request->attributes->get('auth_user').
 */
class AuthenticateToken
{
    public function handle(Request $request, Closure $next, string ...$allowedRoles): Response
    {
        $auth = (string) $request->header('Authorization', '');

        if (!preg_match('/^Bearer\s+([a-f0-9]{64})$/i', trim($auth), $m)) {
            return $this->fail(401, "Token tidak ditemukan atau format salah. Gunakan header 'Authorization: Bearer <token>'.");
        }

        $tokenHash = hash('sha256', strtolower($m[1]));

        $user = DB::selectOne(
            "SELECT id, username, full_name, role, is_active, token_expires_at
             FROM users
             WHERE api_token_hash = ?
             LIMIT 1",
            [$tokenHash]
        );
        $user = $user ? (array) $user : null;

        if (!$user || (int) $user['is_active'] !== 1) {
            return $this->fail(401, "Token tidak valid.");
        }

        if ($user['token_expires_at'] === null || strtotime($user['token_expires_at']) <= time()) {
            return $this->fail(401, "Token sudah kedaluwarsa, silakan login ulang.");
        }

        if (!empty($allowedRoles) && !in_array($user['role'], $allowedRoles, true)) {
            return $this->fail(403, "Anda tidak memiliki akses untuk aksi ini (khusus: " . implode(', ', $allowedRoles) . ").");
        }

        $request->attributes->set('auth_user', $user);

        return $next($request);
    }

    private function fail(int $code, string $message): JsonResponse
    {
        return response()->json(
            ["status" => false, "message" => $message],
            $code,
            ['WWW-Authenticate' => 'Bearer'],
            JSON_UNESCAPED_UNICODE
        );
    }
}
