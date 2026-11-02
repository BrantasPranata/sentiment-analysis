<?php

namespace App\Http\Controllers\Api;

use App\Http\Controllers\Controller;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\DB;

/**
 * POST /api/login  (dulu: api_sentimen/login.php)
 * Body (JSON atau form-data): { "username": "...", "password": "..." }
 */
class AuthController extends Controller
{
    // Konfigurasi Login
    private const MAX_ATTEMPTS = 5;    // Maksimal gagal login berturut-turut
    private const LOCK_MINUTES = 15;   // Lama akun terkunci (menit)
    private const TOKEN_LIFETIME = 8;  // Masa berlaku token (jam)

    public function login(Request $request): JsonResponse
    {
        $username = is_scalar($request->input('username')) ? trim((string) $request->input('username')) : '';
        $password = is_scalar($request->input('password')) ? (string) $request->input('password') : '';

        if ($username === '' || $password === '') {
            return $this->fail(400, "Parameter 'username' dan 'password' wajib diisi!");
        }

        // Cari user berdasarkan username
        $user = DB::selectOne(
            "SELECT id, username, password_hash, full_name, role, is_active, failed_attempts, locked_until
             FROM users
             WHERE username = ?
             LIMIT 1",
            [$username]
        );
        $user = $user ? (array) $user : null;

        // Pesan generik agar tidak membocorkan apakah username terdaftar
        $invalidMsg = "Username atau password salah!";

        if (!$user || (int) $user['is_active'] !== 1) {
            return $this->fail(401, $invalidMsg);
        }

        // Cek apakah akun sedang terkunci
        if ($user['locked_until'] !== null && strtotime($user['locked_until']) > time()) {
            return $this->respond([
                "status" => false,
                "message" => "Akun terkunci sementara karena terlalu banyak percobaan gagal. Coba lagi nanti.",
                "locked_until" => $user['locked_until'],
            ], 429);
        }

        // Verifikasi password (bcrypt)
        if (!password_verify($password, $user['password_hash'])) {
            $attempts = (int) $user['failed_attempts'] + 1;
            $lockedUntil = null;
            if ($attempts >= self::MAX_ATTEMPTS) {
                $lockedUntil = date('Y-m-d H:i:s', time() + (self::LOCK_MINUTES * 60));
                $attempts = 0;
            }

            DB::update(
                "UPDATE users SET failed_attempts = ?, locked_until = ? WHERE id = ?",
                [$attempts, $lockedUntil, $user['id']]
            );

            return $this->fail(401, $invalidMsg);
        }

        // Login sukses: generate token, simpan hash-nya saja di database
        $token = bin2hex(random_bytes(32));
        $tokenHash = hash('sha256', $token);
        $now = date('Y-m-d H:i:s');
        $expiresAt = date('Y-m-d H:i:s', time() + (self::TOKEN_LIFETIME * 3600));

        DB::update(
            "UPDATE users
             SET api_token_hash = ?, token_expires_at = ?, last_login_at = ?, failed_attempts = 0, locked_until = NULL
             WHERE id = ?",
            [$tokenHash, $expiresAt, $now, $user['id']]
        );

        return $this->respond([
            "status" => true,
            "message" => "Login berhasil",
            "data" => [
                "token" => $token,
                "token_type" => "Bearer",
                "expires_at" => $expiresAt,
                "expires_in" => self::TOKEN_LIFETIME * 3600, // detik
                "user" => [
                    "id" => (int) $user['id'],
                    "username" => $user['username'],
                    "full_name" => $user['full_name'],
                    "role" => $user['role'],
                ],
            ],
        ]);
    }
}
