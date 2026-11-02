<?php

use Illuminate\Support\Facades\Route;

Route::get('/', fn () => response()->json([
    'status' => true,
    'message' => 'Sentiment Analysis API. Lihat routes/api.php untuk daftar endpoint.',
]));
