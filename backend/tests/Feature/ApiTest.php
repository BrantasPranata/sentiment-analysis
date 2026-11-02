<?php

namespace Tests\Feature;

use Tests\TestCase;

class ApiTest extends TestCase
{
    public function test_protected_endpoints_require_bearer_token(): void
    {
        foreach (['contents', 'comments', 'insight', 'keywords'] as $endpoint) {
            $this->getJson("/api/{$endpoint}")
                ->assertStatus(401)
                ->assertHeader('WWW-Authenticate', 'Bearer')
                ->assertJson(['status' => false]);
        }
    }

    public function test_login_requires_username_and_password(): void
    {
        $this->postJson('/api/login', [])
            ->assertStatus(400)
            ->assertJson(['status' => false]);
    }

    public function test_login_only_accepts_post(): void
    {
        $this->getJson('/api/login')->assertStatus(405);
    }
}
