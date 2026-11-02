<?php

use App\Http\Controllers\Api\AuthController;
use App\Http\Controllers\Api\CommentController;
use App\Http\Controllers\Api\ContentController;
use App\Http\Controllers\Api\InsightController;
use App\Http\Controllers\Api\KeywordController;
use Illuminate\Support\Facades\Route;

/*
| Semua route di file ini otomatis berawalan /api (lihat bootstrap/app.php).
|
|   POST   /api/login      login, mengembalikan Bearer token        (dulu login.php)
|   GET    /api/contents   daftar konten + sentimen + ringkasan     (dulu index.php)
|   GET    /api/comments   komentar per konten (?content_id=)       (dulu komen_id.php)
|   GET    /api/insight    data grafik insight                      (dulu insight.php)
|   GET    /api/keywords   keyword per platform (?platform_id=)     (dulu platform_id.php)
|   POST|PUT|DELETE /api/keywords  kelola keyword (khusus admin)
*/

Route::post('login', [AuthController::class, 'login']);

// WAJIB LOGIN (semua role yang aktif boleh membaca)
Route::middleware('auth.token')->group(function () {
    Route::get('contents', [ContentController::class, 'index']);
    Route::get('comments', [CommentController::class, 'index']);
    Route::get('insight', [InsightController::class, 'index']);
    Route::get('keywords', [KeywordController::class, 'index']);
});

// WAJIB LOGIN sebagai admin
Route::middleware('auth.token:admin')->group(function () {
    Route::post('keywords', [KeywordController::class, 'store']);
    Route::put('keywords', [KeywordController::class, 'update']);
    Route::delete('keywords', [KeywordController::class, 'destroy']);
});
