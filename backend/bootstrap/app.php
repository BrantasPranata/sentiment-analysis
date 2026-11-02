<?php

use App\Http\Middleware\AuthenticateToken;
use Illuminate\Foundation\Application;
use Illuminate\Foundation\Configuration\Exceptions;
use Illuminate\Foundation\Configuration\Middleware;
use Illuminate\Http\Request;

$app = Application::configure(basePath: dirname(__DIR__))
    ->withRouting(
        web: __DIR__.'/../routes/web.php',
        api: __DIR__.'/../routes/api.php',
        commands: __DIR__.'/../routes/console.php',
        health: '/up',
    )
    ->withMiddleware(function (Middleware $middleware): void {
        $middleware->alias([
            'auth.token' => AuthenticateToken::class,
        ]);
    })
    ->withExceptions(function (Exceptions $exceptions): void {
        $exceptions->shouldRenderJsonWhen(
            fn (Request $request) => $request->is('api/*') || $request->expectsJson(),
        );
    })->create();

// Env global di root project (default.env / .env.prod) dipakai bila backend/.env tidak ada.
// Di Docker file ini tidak ada; konfigurasi datang dari environment variable container.
if (! file_exists(dirname(__DIR__).'/.env')) {
    $app->useEnvironmentPath(dirname(__DIR__, 2))
        ->loadEnvironmentFrom(getenv('APP_ENV_FILE') ?: 'default.env');
}

return $app;
