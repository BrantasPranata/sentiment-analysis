# Backend API (Laravel 13, PHP 8.5) - nginx + php-fpm dalam satu container, listen di port 8080.

# ---- Tahap 1: install dependency Composer ----
FROM composer:2 AS vendor
WORKDIR /app
COPY composer.json composer.lock ./
RUN composer install --no-dev --no-scripts --no-autoloader --prefer-dist --no-interaction --no-progress
COPY . .
RUN composer dump-autoload --no-dev --optimize --classmap-authoritative --no-scripts

# ---- Tahap 2: runtime ----
FROM serversideup/php:8.5-fpm-nginx-alpine-v5.0.5

# AUTORUN: jalankan "php artisan optimize" saat container start (migrasi DIMATIKAN:
# database dipakai bersama dengan pipeline Python, skema tidak dikelola Laravel).
ENV PHP_OPCACHE_ENABLE=1 \
    PHP_DATE_TIMEZONE=Asia/Jakarta \
    AUTORUN_ENABLED=true \
    AUTORUN_LARAVEL_MIGRATION=false \
    AUTORUN_LARAVEL_STORAGE_LINK=false \
    AUTORUN_LARAVEL_VIEW_CACHE=false \
    LOG_CHANNEL=stderr

COPY --chown=www-data:www-data --from=vendor /app /var/www/html
RUN php artisan package:discover --ansi

EXPOSE 8080
