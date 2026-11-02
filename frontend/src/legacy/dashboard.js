// Semua request lewat frontend (/api/...), lalu diteruskan ke backend API secara internal.
// window.SENTIMENT_API_BASE diisi oleh src/main.js.
const API_ROOT = window.SENTIMENT_API_BASE;
const API_BASE_URL = `${API_ROOT}/contents`;
const COMMENTS_API_URL = `${API_ROOT}/comments`;
const KEYWORDS_API_URL = `${API_ROOT}/keywords`;
const INSIGHT_API_URL = `${API_ROOT}/insight`;
const LOGIN_API_URL = `${API_ROOT}/login`;

// ===================== HELPER KEAMANAN =====================
// Escape teks dari luar (judul, komentar, nama penulis, dll) sebelum dimasukkan ke innerHTML
function esc(s) {
    return String(s ?? '').replace(/[&<>"']/g, c =>
        ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

// Hanya izinkan URL http/https (cegah javascript: dsb.)
function safeUrl(u) {
    const s = String(u || '').trim();
    return /^https?:\/\//i.test(s) ? s : '';
}

// ===================== AUTH =====================
const AUTH_STORAGE_KEY = "sentimen_auth";
let appStarted = false;

// Kirim header "Authorization: Bearer ..." ke semua endpoint data.
// Endpoint backend (/api/contents, /api/comments, /api/keywords, /api/insight) sudah memvalidasi token.
const SEND_AUTH_HEADER = true;

function getAuth() {
    try {
        const auth = JSON.parse(localStorage.getItem(AUTH_STORAGE_KEY));
        if (auth && auth.token && auth.expires_ts > Date.now()) return auth;
    } catch (e) { /* storage kosong / rusak */ }
    try { localStorage.removeItem(AUTH_STORAGE_KEY); } catch (e) {}
    return null;
}

function saveAuth(data) {
    const ttl = (Number(data.expires_in) || 8 * 3600) * 1000;
    const auth = { token: data.token, user: data.user, expires_ts: Date.now() + ttl };
    try { localStorage.setItem(AUTH_STORAGE_KEY, JSON.stringify(auth)); } catch (e) {}
    return auth;
}

// Role admin boleh tambah/ubah/hapus keyword; role lain hanya melihat
function isAdmin() {
    const a = getAuth();
    return !!(a && a.user && a.user.role === 'admin');
}

// Pengganti fetch(): otomatis kirim Bearer token & logout bila server membalas 401
async function authFetch(url, options = {}) {
    const auth = getAuth();
    if (!auth) { showLogin('Sesi berakhir, silakan login kembali.'); throw new Error('Belum login'); }
    const headers = Object.assign({}, options.headers);
    if (SEND_AUTH_HEADER) headers['Authorization'] = 'Bearer ' + auth.token;
    const response = await fetch(url, Object.assign({}, options, { headers }));
    if (response.status === 401) {
        try { localStorage.removeItem(AUTH_STORAGE_KEY); } catch (e) {}
        showLogin('Sesi tidak valid atau sudah berakhir, silakan login kembali.');
        throw new Error('Unauthorized');
    }
    return response;
}

function showLogin(message) {
    appStarted = false; // supaya setelah login ulang data dimuat kembali
    document.getElementById('login-overlay').classList.remove('hidden');
    const alertBox = document.getElementById('login-alert');
    if (message) {
        alertBox.textContent = message;
        alertBox.classList.remove('hidden');
    } else {
        alertBox.classList.add('hidden');
    }
    document.getElementById('login-password').value = '';
    document.getElementById('login-username').focus();
}

function hideLogin() {
    document.getElementById('login-overlay').classList.add('hidden');
    document.getElementById('login-alert').classList.add('hidden');
}

function applyAuthUser(auth) {
    const user = (auth && auth.user) || {};
    document.getElementById('auth-user-name').textContent = user.full_name || user.username || '-';
    document.getElementById('auth-user-role').textContent = user.role || '-';
}

function togglePasswordVisibility() {
    const input = document.getElementById('login-password');
    const icon = document.getElementById('login-pw-icon');
    const show = input.type === 'password';
    input.type = show ? 'text' : 'password';
    icon.className = show ? 'ph-bold ph-eye-slash' : 'ph-bold ph-eye';
}

async function handleLogin(e) {
    e.preventDefault();
    const username = document.getElementById('login-username').value.trim();
    const password = document.getElementById('login-password').value;
    const btn = document.getElementById('login-submit');
    const btnText = document.getElementById('login-submit-text');
    const btnIcon = document.getElementById('login-submit-icon');

    btn.disabled = true;
    btn.classList.add('opacity-60', 'cursor-not-allowed');
    btnText.textContent = 'Memproses...';
    btnIcon.className = 'ph-bold ph-spinner animate-spin text-lg';

    try {
        const response = await fetch(LOGIN_API_URL, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ username, password })
        });
        const result = await response.json().catch(() => ({}));

        if (!response.ok || !result.status) {
            showLogin(result.message || 'Login gagal (HTTP ' + response.status + ')');
            return;
        }

        const auth = saveAuth(result.data);
        applyAuthUser(auth);
        hideLogin();
        startApp();
    } catch (err) {
        showLogin('Tidak dapat terhubung ke server login: ' + err.message);
    } finally {
        btn.disabled = false;
        btn.classList.remove('opacity-60', 'cursor-not-allowed');
        btnText.textContent = 'Login';
        btnIcon.className = 'ph-bold ph-sign-in text-lg';
    }
}

function logout() {
    try { localStorage.removeItem(AUTH_STORAGE_KEY); } catch (e) {}
    // Reload agar seluruh data & grafik di memori ikut bersih
    window.location.reload();
}

function startApp() {
    if (appStarted) return;
    appStarted = true;
    loadDataFromAPI();
    loadInsightData();
}
// =================== END AUTH ===================

let rawDataCache = [];
let currentContentId = null;
let debounceTimer;

let barChartInstance = null;
let pieChartInstance = null;
let quadrantChartInstance = null;
let topicChartInstance = null;

// State pagination + ringkasan seluruh data (sesuai filter) dari server
let pageState = { page: 1, limit: 30, total: 0, totalPages: 1, hasMore: false, all: false, loaded: 0 };
let lastSummary = null;
let loadSeq = 0; // penanda permintaan terbaru (abaikan respons yang sudah usang)

// Mode diagram batang: 'konten' atau 'komentar' + cache data terakhir agar toggle tidak perlu fetch ulang
let barMode = 'konten';
let lastBreakdown = {};

let currentKeywordsState = Array(10).fill(null).map(() => ({ id: null, keyword: '' }));

// Ambil bagian tanggal (YYYY-MM-DD) dari "YYYY-MM-DD HH:MM:SS" maupun ISO "YYYY-MM-DDTHH:MM:SSZ"
function dateOnly(val) {
    return String(val || '').slice(0, 10);
}

// Bersihkan semua filter (kecuali Limit) lalu muat ulang data
function resetFilters() {
    ['filter-search', 'filter-date-from', 'filter-date-to', 'filter-pub-from', 'filter-pub-to',
     'filter-platform', 'filter-sentiment', 'filter-status'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.value = '';
    });
    clearTimeout(debounceTimer);
    loadDataFromAPI();
}

function debounceLoad() {
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(() => loadDataFromAPI(), 300);
}

function formatDetailPercent(score) {
    const num = parseFloat(score) || 0;
    const pct = num * 100;
    return pct.toFixed(2) + '%';
}

// ===================== PAGINATION & PEMUATAN DATA =====================
// Semua filter diproses di SERVER (index.php). Total, kartu angka, dan grafik mencakup
// seluruh data yang cocok, sedangkan kartu konten hanya memuat satu halaman per permintaan.
function getFilterValues() {
    const v = id => (document.getElementById(id).value || '').trim();
    return {
        limit: v('filter-limit') || '30',
        search: v('filter-search'),
        platform: v('filter-platform').toLowerCase(),
        sentiment: v('filter-sentiment').toLowerCase(),
        status: v('filter-status').toLowerCase(),
        analyzed_from: v('filter-date-from'),
        analyzed_to: v('filter-date-to'),
        published_from: v('filter-pub-from'),
        published_to: v('filter-pub-to')
    };
}

// Muat dari halaman 1 + hitung ulang total & grafik (dipanggil saat filter berubah / Refresh)
function loadDataFromAPI() {
    return fetchContents(1, true);
}

// Pindah halaman (total & grafik tidak dihitung ulang)
function goToPage(page) {
    const target = Math.max(1, Math.min(Number(page) || 1, pageState.totalPages || 1));
    document.getElementById('content-grid').scrollIntoView({ behavior: 'smooth', block: 'start' });
    return fetchContents(target, false);
}

async function fetchContents(page, withSummary) {
    const seq = ++loadSeq;
    const grid = document.getElementById('content-grid');
    grid.innerHTML = `
        <div class="col-span-full py-16 text-center text-slate-400">
            <i class="ph-bold ph-spinner animate-spin text-4xl text-indigo-500 mb-2"></i>
            <p class="text-sm">Mengambil data dari Sentimen API...</p>
        </div>
    `;

    const f = getFilterValues();
    // Mode "Semua Data": server membatasi 200 baris per permintaan, jadi dimuat bertahap per 200
    const allMode = f.limit === 'all';
    const chunk = allMode ? 200 : (Number(f.limit) || 30);
    const buildUrl = (pg, sum) => {
        const params = new URLSearchParams({ limit: String(chunk), page: String(pg) });
        if (sum) params.append('summary', '1');
        ['search', 'platform', 'sentiment', 'status',
         'analyzed_from', 'analyzed_to', 'published_from', 'published_to'].forEach(k => {
            if (f[k]) params.append(k, f[k]);
        });
        return `${API_BASE_URL}?${params.toString()}`;
    };

    try {
        const response = await authFetch(buildUrl(page, withSummary));
        const result = await response.json();
        if (seq !== loadSeq) return; // sudah ada permintaan yang lebih baru
        if (!result || result.status !== true) {
            throw new Error((result && result.message) || 'Respons tidak valid');
        }

        const items = Array.isArray(result.data) ? result.data : [];
        const pg = result.pagination || {};

        pageState.page = Number(pg.page) || page;
        pageState.limit = Number(pg.limit) || chunk;
        pageState.all = allMode;
        pageState.hasMore = !!pg.has_more;

        if (withSummary && result.summary) {
            lastSummary = result.summary;
            pageState.total = Number(pg.total) || Number(result.summary.contents && result.summary.contents.total) || 0;
            applySummary(result.summary);
        }
        pageState.totalPages = Math.max(1, Math.ceil(pageState.total / pageState.limit));

        rawDataCache = items;
        renderCards(items);
        pageState.loaded = items.length;
        renderPagination();

        // Lanjutkan memuat halaman berikutnya sampai semua data tampil
        if (allMode) {
            let next = page + 1;
            let more = pageState.hasMore;
            while (more) {
                const r2 = await authFetch(buildUrl(next, false));
                const j2 = await r2.json();
                if (seq !== loadSeq) return;
                if (!j2 || j2.status !== true) throw new Error((j2 && j2.message) || 'Respons tidak valid');
                const more_items = Array.isArray(j2.data) ? j2.data : [];
                rawDataCache = rawDataCache.concat(more_items);
                renderCards(more_items, true);
                pageState.loaded += more_items.length;
                more = !!(j2.pagination && j2.pagination.has_more) && more_items.length > 0;
                next++;
                renderPagination();
            }
        }

    } catch (err) {
        if (seq !== loadSeq) return;
        console.error("API Fetch Error:", err);
        if (err && err.message === 'Unauthorized') return; // layar login sudah ditampilkan
        grid.innerHTML = `
            <div class="col-span-full py-12 text-center text-rose-400">
                <i class="ph-bold ph-warning-circle text-3xl mb-1"></i>
                <p>Gagal terhubung ke API Endpoint Sentimen.</p>
            </div>
        `;
    }
}

function renderPagination() {
    const bar = document.getElementById('pagination-bar');
    const info = document.getElementById('pagination-info');
    const box = document.getElementById('pagination-buttons');
    const { page, limit, total, totalPages } = pageState;

    if (!total) { bar.classList.add('hidden'); return; }
    bar.classList.remove('hidden');

    const fmt = n => Number(n).toLocaleString('id-ID');
    if (pageState.all) {
        box.replaceChildren();
        info.textContent = pageState.loaded < total
            ? `Memuat ${fmt(pageState.loaded)} dari ${fmt(total)} konten...`
            : `Menampilkan semua ${fmt(total)} konten`;
        return;
    }
    const start = (page - 1) * limit + 1;
    const end = Math.min(page * limit, total);
    info.textContent = `Menampilkan ${fmt(start)}–${fmt(end)} dari ${fmt(total)} konten · Halaman ${fmt(page)} dari ${fmt(totalPages)}`;

    box.replaceChildren();
    const mk = (label, target, opts = {}) => {
        const b = document.createElement('button');
        b.type = 'button';
        b.textContent = label;
        b.className = 'min-w-[2.25rem] px-3 py-1.5 text-xs font-semibold rounded-lg border transition ' +
            (opts.active ? 'bg-indigo-600 border-indigo-500 text-white'
                         : 'bg-slate-800 border-slate-700 text-slate-300 hover:bg-slate-700') +
            (opts.disabled ? ' opacity-40 cursor-not-allowed pointer-events-none' : '');
        if (opts.title) b.title = opts.title;
        b.disabled = !!opts.disabled;
        if (!opts.disabled && !opts.active) b.addEventListener('click', () => goToPage(target));
        box.appendChild(b);
    };

    mk('«', 1, { disabled: page <= 1, title: 'Halaman pertama' });
    mk('‹ Sebelumnya', page - 1, { disabled: page <= 1 });

    const nums = [];
    const add = p => { if (p >= 1 && p <= totalPages && !nums.includes(p)) nums.push(p); };
    add(1);
    for (let p = page - 2; p <= page + 2; p++) add(p);
    add(totalPages);
    nums.sort((a, b) => a - b);

    let prev = 0;
    nums.forEach(p => {
        if (p - prev > 1) {
            const gap = document.createElement('span');
            gap.textContent = '…';
            gap.className = 'px-1 text-slate-500 text-xs';
            box.appendChild(gap);
        }
        mk(fmt(p), p, { active: p === page });
        prev = p;
    });

    mk('Berikutnya ›', page + 1, { disabled: page >= totalPages });
    mk('»', totalPages, { disabled: page >= totalPages, title: 'Halaman terakhir' });
}

// ===== REKAP KOMENTAR (comments_summary dari /api/contents) =====
function getCommentsSummary(item) {
    const c = (item && item.comments_summary) || {};
    const positive = Number(c.positive) || 0;
    const negative = Number(c.negative) || 0;
    const neutral = Number(c.neutral) || 0;
    const total = Math.max(Number(c.total) || 0, positive + negative + neutral);
    return { total, positive, negative, neutral };
}

// Batang proporsi positif / negatif / netral (hanya angka -> aman dimasukkan ke innerHTML)
function commentsBarHTML(cs) {
    const pct = n => (cs.total > 0 ? (n / cs.total * 100) : 0).toFixed(2);
    return `
        <div class="flex h-1.5 rounded-full overflow-hidden bg-slate-700/70" title="Proporsi komentar positif / negatif / netral">
            <div class="bg-emerald-500" style="width:${pct(cs.positive)}%"></div>
            <div class="bg-rose-500" style="width:${pct(cs.negative)}%"></div>
            <div class="bg-amber-500" style="width:${pct(cs.neutral)}%"></div>
        </div>`;
}

// Versi ringkas untuk kartu konten
function commentsSummaryCompactHTML(cs) {
    return `
        <div class="mt-3 pt-3 border-t border-slate-700/50">
            <div class="flex items-center justify-between text-[11px] mb-1.5">
                <span class="flex items-center space-x-1 text-slate-400">
                    <i class="ph-bold ph-chat-circle-dots text-sm"></i>
                    <span>${cs.total.toLocaleString('id-ID')} komentar</span>
                </span>
                <span class="flex items-center space-x-2.5 font-semibold">
                    <span class="flex items-center space-x-0.5 text-emerald-400" title="Komentar positif"><i class="ph-bold ph-thumbs-up"></i><span>${cs.positive.toLocaleString('id-ID')}</span></span>
                    <span class="flex items-center space-x-0.5 text-rose-400" title="Komentar negatif"><i class="ph-bold ph-thumbs-down"></i><span>${cs.negative.toLocaleString('id-ID')}</span></span>
                    <span class="flex items-center space-x-0.5 text-amber-400" title="Komentar netral"><i class="ph-bold ph-minus"></i><span>${cs.neutral.toLocaleString('id-ID')}</span></span>
                </span>
            </div>
            ${commentsBarHTML(cs)}
        </div>`;
}

// Versi lengkap untuk modal detail
function commentsSummaryDetailHTML(cs) {
    const pct = n => (cs.total > 0 ? Math.round(n / cs.total * 100) : 0);
    const box = (label, n, color, bg) => `
        <div class="${bg} border border-slate-700/50 rounded-lg px-3 py-2 text-center">
            <div class="text-lg font-bold ${color}">${n.toLocaleString('id-ID')}</div>
            <div class="text-[10px] text-slate-400">${label}${cs.total > 0 ? ' · ' + pct(n) + '%' : ''}</div>
        </div>`;
    return `
        <div class="flex items-center justify-between mb-2">
            <span class="text-xs font-bold text-slate-200 flex items-center space-x-1.5">
                <i class="ph-bold ph-chart-bar-horizontal text-indigo-400"></i>
                <span>Rekap Komentar</span>
            </span>
            <span class="text-xs text-slate-400">Total ${cs.total.toLocaleString('id-ID')}</span>
        </div>
        <div class="grid grid-cols-3 gap-2 mb-2">
            ${box('Positif', cs.positive, 'text-emerald-400', 'bg-emerald-500/10')}
            ${box('Negatif', cs.negative, 'text-rose-400', 'bg-rose-500/10')}
            ${box('Netral', cs.neutral, 'text-amber-400', 'bg-amber-500/10')}
        </div>
        ${commentsBarHTML(cs)}
        ${cs.total === 0 ? '<p class="text-[11px] text-slate-500 mt-2">Belum ada komentar yang terekam untuk konten ini.</p>' : ''}`;
}

function renderCards(items, append = false) {
    const grid = document.getElementById('content-grid');
    if (items.length === 0) {
        if (append) return;
        grid.innerHTML = `<div class="col-span-full py-16 text-center text-slate-500">Tidak ada data yang cocok dengan filter ini.</div>`;
        return;
    }

    const html = items.map(item => {
        const sa = item.sentiment_analysis || {};
        const sentiment = (sa.sentiment || item.sentiment || 'neutral').toLowerCase();
        
        let badgeColor = "bg-amber-500/10 text-amber-400 border-amber-500/20";
        let iconClass = "ph-bold ph-smiley-meh";

        if (sentiment === 'positive') {
            badgeColor = "bg-emerald-500/10 text-emerald-400 border-emerald-500/20";
            iconClass = "ph-bold ph-smiley";
        } else if (sentiment === 'negative') {
            badgeColor = "bg-rose-500/10 text-rose-400 border-rose-500/20";
            iconClass = "ph-bold ph-smiley-sad";
        }

        const rawScore = (sa.confidence_score !== undefined) ? sa.confidence_score : item.confidence_score;
        const confidenceStr = formatDetailPercent(rawScore);

        let platformName = 'Platform';
        if (item.platform) {
            platformName = typeof item.platform === 'object' 
                ? (item.platform.name || item.platform.code || 'Platform')
                : item.platform;
        }

        const cs = getCommentsSummary(item);
        const hasMedia = item.media_urls && item.media_urls.length > 0;
        const mediaThumb = hasMedia ? safeUrl(item.media_urls[0]) : '';

        return `
            <div class="bg-slate-800/80 border border-slate-700/70 hover:border-slate-600 rounded-2xl overflow-hidden shadow-lg transition duration-200 flex flex-col justify-between">
                <div>
                    ${mediaThumb ? `<img src="${esc(mediaThumb)}" class="w-full h-36 object-cover bg-slate-900" onerror="this.style.display='none'">` : ''}
                    <div class="p-5">
                        <div class="flex justify-between items-center mb-3">
                            <span class="px-2.5 py-1 bg-slate-900 border border-slate-700 text-slate-300 text-[10px] font-semibold uppercase tracking-wider rounded-lg">
                                ${esc(platformName)}
                            </span>
                            <span class="px-2.5 py-1 rounded-lg text-xs font-semibold border flex items-center space-x-1 ${badgeColor}">
                                <i class="ph ${iconClass} text-sm"></i>
                                <span class="capitalize">${esc(sentiment)} (${confidenceStr})</span>
                            </span>
                        </div>
                        <h4 class="text-white font-semibold text-sm line-clamp-2 leading-snug mb-2" title="${esc(item.title)}">${esc(item.title) || 'Tanpa Judul'}</h4>
                        <p class="text-slate-400 text-xs line-clamp-3 leading-relaxed">${esc(item.raw_text) || 'Tidak ada teks preview.'}</p>
                        ${commentsSummaryCompactHTML(cs)}
                    </div>
                </div>
                <div class="px-5 py-3.5 bg-slate-900/60 border-t border-slate-700/50 flex justify-between items-center">
                    <div class="text-[11px] text-slate-500 leading-relaxed">
                        <div class="flex items-center space-x-1.5" title="Tanggal terbit (published_at)">
                            <i class="ph-bold ph-calendar-check text-emerald-400/80"></i>
                            <span class="text-slate-400">Terbit:</span>
                            <span class="text-slate-300">${esc(item.published_at || '-')}</span>
                        </div>
                        <div class="flex items-center space-x-1.5" title="Waktu analisis sentimen (analyzed_at)">
                            <i class="ph-bold ph-brain text-indigo-400/80"></i>
                            <span class="text-slate-400">Dianalisis:</span>
                            <span>${esc(sa.analyzed_at || item.analyzed_at || '-')}</span>
                        </div>
                    </div>
                    <button onclick="openModal(${Number(item.id) || 0})" class="text-indigo-400 hover:text-indigo-300 text-xs font-semibold flex items-center space-x-1">
                        <span>Detail</span>
                        <i class="ph-bold ph-caret-right"></i>
                    </button>
                </div>
            </div>
        `;
    }).join('');
    if (append) grid.insertAdjacentHTML('beforeend', html);
    else grid.innerHTML = html;
}

// Warna per sumber (hindari merah/hijau/kuning agar tidak bentrok dengan warna sentimen)
const PLATFORM_COLORS = {
    youtube: '#a78bfa',
    x:       '#38bdf8',
    kompas:  '#60a5fa',
    detik:   '#f472b6',
    antara:  '#c084fc',
    antaranews: '#c084fc',
    instagram: '#e879f9',
    cnn:        '#22d3ee',
    tribun:     '#818cf8',
    metrotv:    '#2dd4bf',
    beritasatu: '#7dd3fc',
    kabarika:   '#f9a8d4',
    republika:  '#fdba74',
    idntimes:   '#d946ef',
    rri:        '#94a3b8',
    pertanian:  '#a8a29e',
    viva:       '#0ea5e9'
};
const PLATFORM_FALLBACK = ['#22d3ee', '#818cf8', '#2dd4bf', '#94a3b8', '#fb7185'];

// Terapkan ringkasan SELURUH data (sesuai filter) dari server ke kartu angka & grafik
function applySummary(s) {
    const n = v => Number(v) || 0;
    const fmt = v => n(v).toLocaleString('id-ID');
    const c = s.contents || {};
    const cm = s.comments || {};

    document.getElementById('stat-total').innerText = fmt(c.total);
    document.getElementById('stat-positive').innerText = fmt(c.positive);
    document.getElementById('stat-negative').innerText = fmt(c.negative);
    document.getElementById('stat-neutral').innerText = fmt(c.neutral);

    document.getElementById('stat-c-total').innerText = fmt(cm.total);
    document.getElementById('stat-c-positive').innerText = fmt(cm.positive);
    document.getElementById('stat-c-negative').innerText = fmt(cm.negative);
    document.getElementById('stat-c-neutral').innerText = fmt(cm.neutral);

    // Breakdown per sumber untuk diagram batang
    const breakdown = {};
    (s.platforms || []).forEach(p => {
        const code = String(p.code || p.name || 'lainnya').toLowerCase();
        const b = {
            name: p.name || p.code || 'Lainnya',
            positive: n(p.positive), negative: n(p.negative), neutral: n(p.neutral),
            cPositive: n(p.c_positive), cNegative: n(p.c_negative), cNeutral: n(p.c_neutral)
        };
        const sum = b.positive + b.negative + b.neutral + b.cPositive + b.cNegative + b.cNeutral;
        if (sum === 0) return;
        if (breakdown[code]) {
            Object.keys(b).forEach(k => { if (k !== 'name') breakdown[code][k] += b[k]; });
        } else {
            breakdown[code] = b;
        }
    });

    renderCharts(n(c.positive), n(c.negative), n(c.neutral), breakdown, {
        total: n(cm.total), positive: n(cm.positive), negative: n(cm.negative), neutral: n(cm.neutral)
    });
}

function colorWithAlpha(color, alpha) {
    const m = /^#([0-9a-f]{6})$/i.exec(color || '');
    if (!m) return color;
    const n = parseInt(m[1], 16);
    return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`;
}

// Plugin kecil: angka di dalam segmen batang + total di atas tiap tumpukan
const barValueLabelsPlugin = {
    id: 'barValueLabels',
    afterDatasetsDraw(chart) {
        const ctx = chart.ctx;
        const tops = {}; // `${stack}|${index}` -> { total, y, x }
        ctx.save();
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        chart.data.datasets.forEach((ds, di) => {
            if (!chart.isDatasetVisible(di)) return;
            chart.getDatasetMeta(di).data.forEach((bar, i) => {
                const v = Number(ds.data[i]) || 0;
                if (v <= 0) return;
                const p = bar.getProps(['x', 'y', 'base'], true);
                const h = Math.abs(p.base - p.y);
                if (h >= 13) {
                    ctx.font = '600 10px "Plus Jakarta Sans", sans-serif';
                    ctx.fillStyle = '#0f172a';
                    ctx.fillText(v.toLocaleString('id-ID'), p.x, (p.y + p.base) / 2);
                }
                const key = ds.stack + '|' + i;
                if (!tops[key]) tops[key] = { total: 0, y: p.y, x: p.x, isComment: !!ds.isComment };
                tops[key].total += v;
                tops[key].y = Math.min(tops[key].y, p.y);
            });
        });
        ctx.font = '700 11px "Plus Jakarta Sans", sans-serif';
        Object.values(tops).forEach(t => {
            ctx.fillStyle = t.isComment ? '#fbbf24' : '#e2e8f0';
            ctx.fillText(t.total.toLocaleString('id-ID'), t.x, t.y - 9);
        });
        ctx.restore();
    }
};

// Plugin kecil: angka di tiap irisan doughnut
const doughnutValueLabelsPlugin = {
    id: 'doughnutValueLabels',
    afterDatasetsDraw(chart) {
        const ctx = chart.ctx;
        ctx.save();
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        ctx.font = '700 11px "Plus Jakarta Sans", sans-serif';
        ctx.fillStyle = '#0f172a';
        chart.data.datasets.forEach((ds, di) => {
            const sum = ds.data.reduce((a, b) => a + (Number(b) || 0), 0);
            if (!sum) return;
            chart.getDatasetMeta(di).data.forEach((arc, i) => {
                const v = Number(ds.data[i]) || 0;
                if (v <= 0 || v / sum < 0.06) return;
                const pt = arc.getCenterPoint();
                ctx.fillText(v.toLocaleString('id-ID'), pt.x, pt.y);
            });
        });
        ctx.restore();
    }
};

// Ganti tampilan diagram batang antara Konten dan Komentar (ukuran/tata letak tetap sama)
function setBarMode(mode) {
    barMode = (mode === 'komentar') ? 'komentar' : 'konten';
    renderBarChart();
}

// Atur gaya tombol toggle + teks keterangan sesuai mode aktif
function updateBarModeUI() {
    const active = 'bg-indigo-600 text-white shadow';
    const inactive = 'text-slate-400 hover:text-slate-200';
    ['konten', 'komentar'].forEach(m => {
        const btn = document.getElementById('bar-mode-' + m);
        if (!btn) return;
        btn.className = 'px-2.5 py-0.5 text-[11px] leading-4 font-semibold rounded-md transition flex items-center space-x-1 '
            + (m === barMode ? active : inactive);
        btn.setAttribute('aria-pressed', String(m === barMode));
    });
    const sub = document.getElementById('bar-subtitle');
    if (sub) {
        sub.innerHTML = (barMode === 'konten')
            ? 'Jumlah konten per sumber (seluruh data sesuai filter) &middot; angka di atas batang = total'
            : 'Jumlah komentar per sumber (seluruh data sesuai filter) &middot; angka di atas batang = total';
    }
}

// 1. Bar Chart: per sumber, menampilkan SALAH SATU: konten atau komentar
function renderBarChart() {
    updateBarModeUI();
    if (barChartInstance) barChartInstance.destroy();

    const breakdown = lastBreakdown || {};
    const isComment = barMode === 'komentar';
    const valuesOf = b => isComment
        ? [b.cPositive, b.cNegative, b.cNeutral]
        : [b.positive, b.negative, b.neutral];
    const totalOf = b => valuesOf(b).reduce((a, v) => a + v, 0);

    // Hanya sumber yang punya data pada mode terpilih, diurutkan dari yang terbanyak
    const platformCodes = Object.keys(breakdown)
        .filter(c => totalOf(breakdown[c]) > 0)
        .sort((x, y) => totalOf(breakdown[y]) - totalOf(breakdown[x]));

    const barDatasets = platformCodes.map((code, idx) => {
        const b = breakdown[code];
        const color = PLATFORM_COLORS[code] || PLATFORM_FALLBACK[idx % PLATFORM_FALLBACK.length];
        return {
            label: b.name,
            stack: 'sumber',
            isComment: isComment,
            data: valuesOf(b),
            backgroundColor: color,
            borderColor: '#1e293b',
            borderWidth: 1,
            borderRadius: 4,
            borderSkipped: false
        };
    });

    const ctxBar = document.getElementById('sentimentBarChart').getContext('2d');
    barChartInstance = new Chart(ctxBar, {
        type: 'bar',
        data: {
            labels: ['Positif', 'Negatif', 'Netral'],
            datasets: barDatasets
        },
        plugins: [barValueLabelsPlugin],
        options: {
            responsive: true,
            maintainAspectRatio: false,
            interaction: { mode: 'index', intersect: false },
            plugins: {
                legend: {
                    position: 'bottom',
                    labels: { color: '#94a3b8', font: { size: 11 }, boxWidth: 12 }
                },
                tooltip: {
                    backgroundColor: '#0f172a',
                    borderColor: '#334155',
                    borderWidth: 1,
                    titleColor: '#f1f5f9',
                    bodyColor: '#cbd5e1',
                    filter: t => t.parsed.y > 0,
                    callbacks: {
                        footer: tipItems => {
                            const sum = tipItems.reduce((a, t) => a + t.parsed.y, 0);
                            return (isComment ? 'Total komentar: ' : 'Total konten: ') + sum.toLocaleString('id-ID');
                        }
                    }
                }
            },
            scales: {
                x: { stacked: true, ticks: { color: '#94a3b8' }, grid: { display: false } },
                y: { stacked: true, grace: '12%', ticks: { color: '#94a3b8', precision: 0 }, grid: { color: '#334155' } }
            }
        }
    });
}

function renderCharts(positive, negative, neutral, breakdown = {}, cm = { positive: 0, negative: 0, neutral: 0 }) {
    // Destroy instance lama jika ada
    if (pieChartInstance) pieChartInstance.destroy();

    // 1. Bar Chart (konten / komentar sesuai toggle)
    lastBreakdown = breakdown;
    renderBarChart();

    // 2. Doughnut: cincin luar = konten, cincin dalam = komentar
    const ctxPie = document.getElementById('sentimentPieChart').getContext('2d');
    pieChartInstance = new Chart(ctxPie, {
        type: 'doughnut',
        data: {
            labels: ['Positif', 'Negatif', 'Netral'],
            datasets: [
                {
                    label: 'Konten',
                    data: [positive, negative, neutral],
                    backgroundColor: [
                        'rgba(16, 185, 129, 0.85)',
                        'rgba(244, 63, 94, 0.85)',
                        'rgba(245, 158, 11, 0.85)'
                    ],
                    borderWidth: 2,
                    borderColor: '#1e293b'
                },
                {
                    label: 'Komentar',
                    data: [cm.positive, cm.negative, cm.neutral],
                    backgroundColor: [
                        'rgba(16, 185, 129, 0.5)',
                        'rgba(244, 63, 94, 0.5)',
                        'rgba(245, 158, 11, 0.5)'
                    ],
                    borderWidth: 2,
                    borderColor: '#1e293b'
                }
            ]
        },
        plugins: [doughnutValueLabelsPlugin],
        options: {
            responsive: true,
            maintainAspectRatio: false,
            cutout: '30%',
            plugins: {
                legend: {
                    position: 'bottom',
                    labels: { color: '#94a3b8', font: { size: 11 } }
                },
                tooltip: {
                    backgroundColor: '#0f172a',
                    borderColor: '#334155',
                    borderWidth: 1,
                    titleColor: '#f1f5f9',
                    bodyColor: '#cbd5e1',
                    callbacks: {
                        label: t => {
                            const sum = t.dataset.data.reduce((a, b) => a + (Number(b) || 0), 0);
                            const pct = sum ? (t.parsed / sum * 100).toFixed(1) : '0.0';
                            return `${t.dataset.label} ${t.label}: ${t.parsed.toLocaleString('id-ID')} (${pct}%)`;
                        }
                    }
                }
            }
        }
    });
}

// ===== INSIGHT DASHBOARD (/api/insight) =====
const INSIGHT_COLORS = {
    positive: { bg: 'rgba(16, 185, 129, 0.8)', border: '#10b981' },
    negative: { bg: 'rgba(244, 63, 94, 0.8)',  border: '#f43f5e' },
    neutral:  { bg: 'rgba(245, 158, 11, 0.8)', border: '#f59e0b' },
    unknown:  { bg: 'rgba(100, 116, 139, 0.7)', border: '#64748b' }
};

const INSIGHT_TOOLTIP = {
    backgroundColor: '#0f172a',
    borderColor: '#334155',
    borderWidth: 1,
    titleColor: '#f1f5f9',
    bodyColor: '#cbd5e1'
};

function toggleAllCharts() {
    const bodies = ['simple-charts-body', 'insight-body'].map(id => document.getElementById(id));
    const btn = document.getElementById('charts-toggle-btn');
    const willOpen = bodies[0].classList.contains('hidden');

    bodies.forEach(el => el.classList.toggle('hidden', !willOpen));
    btn.setAttribute('aria-expanded', String(willOpen));
    document.getElementById('charts-toggle-icon').className =
        'ph-bold text-sm ' + (willOpen ? 'ph-caret-up' : 'ph-caret-down');
    document.getElementById('charts-toggle-label').innerText =
        willOpen ? 'Tutup Semua Grafik' : 'Buka Semua Grafik';

    // Chart.js perlu dihitung ulang ukurannya setelah container tampil kembali
    if (willOpen) {
        [barChartInstance, pieChartInstance, quadrantChartInstance, topicChartInstance]
            .forEach(c => { if (c) c.resize(); });
    }
}

function showInsightError(msg) {
    document.getElementById('insight-error-msg').innerText = msg;
    document.getElementById('insight-error').classList.remove('hidden');
}

async function loadInsightData() {
    document.getElementById('insight-error').classList.add('hidden');
    try {
        const response = await authFetch(INSIGHT_API_URL);
        const result = await response.json();
        if (!result || result.status !== true || !result.data) {
            throw new Error((result && result.message) || 'Respons tidak valid');
        }
        renderKeywordCluster(result.data.keyword_clustering || []);
        renderQuadrantChart(result.data.quadrant_analysis || []);
        renderTopicChart(result.data.topic_sentiment_insight || []);
        renderTopicLinks(result.data.topic_associations || []);
    } catch (err) {
        console.error("Insight API Error:", err);
        if (err && err.message === 'Unauthorized') return; // layar login sudah ditampilkan
        showInsightError('Gagal memuat data insight: ' + err.message);
    }
}

// Hitung posisi bubble (murni matematika, tanpa DOM) untuk ukuran area W x H
function layoutKeywordNodes(rows, W, H) {
    const side = W / H > 1.2; // area lebar: dua klaster berdampingan, area sempit: ditumpuk
    const centers = side
        ? { Positif: { x: W * 0.34, y: H * 0.52 }, Negatif: { x: W * 0.76, y: H * 0.52 } }
        : { Positif: { x: W / 2, y: H * 0.30 }, Negatif: { x: W / 2, y: H * 0.76 } };
    const rMax = side ? Math.min(H * 0.15, W * 0.11) : Math.min(W * 0.115, H * 0.115);
    const rMin = Math.max(10, rMax * 0.28);
    const maxVal = Math.max(...rows.map(r => Number(r.jumlah_kemunculan) || 0), 1);

    const nodes = rows.map(r => {
        const val = Number(r.jumlah_kemunculan) || 0;
        return {
            word: String(r.kata_kunci),
            group: /positif|positive/i.test(String(r.sentimen)) ? 'Positif' : 'Negatif',
            val, r: Math.max(rMin, Math.sqrt(val / maxVal) * rMax), x: 0, y: 0
        };
    });

    // Posisi awal: spiral golden-angle, bubble terbesar di tengah klaster
    ['Positif', 'Negatif'].forEach(g => {
        nodes.filter(n => n.group === g).sort((a, b) => b.val - a.val).forEach((n, i) => {
            const ang = i * 2.399963, dist = rMax * 0.2 * Math.sqrt(i);
            n.x = centers[g].x + Math.cos(ang) * dist;
            n.y = centers[g].y + Math.sin(ang) * dist;
        });
    });

    // Relaksasi tabrakan (bubble besar bergeser lebih sedikit) + tarikan ke pusat klaster
    for (let it = 0; it < 460; it++) {
        if (it < 400) {
            nodes.forEach(n => {
                n.x += (centers[n.group].x - n.x) * 0.06;
                n.y += (centers[n.group].y - n.y) * 0.06;
            });
        }
        for (let i = 0; i < nodes.length; i++) {
            for (let j = i + 1; j < nodes.length; j++) {
                const a = nodes[i], b = nodes[j];
                let dx = b.x - a.x, dy = b.y - a.y;
                const d = Math.hypot(dx, dy) || 0.01;
                const min = a.r + b.r + (a.group === b.group ? 3 : rMax * 0.5);
                if (d < min) {
                    const m = (min - d) / d;
                    const wa = (b.r * b.r) / (a.r * a.r + b.r * b.r), wb = 1 - wa;
                    a.x -= dx * m * wa; a.y -= dy * m * wa;
                    b.x += dx * m * wb; b.y += dy * m * wb;
                }
            }
        }
        nodes.forEach(n => {
            n.x = Math.min(W - n.r - 4, Math.max(n.r + 4, n.x));
            n.y = Math.min(H - n.r - 4, Math.max(n.r + 22, n.y));
        });
    }

    // Halo per klaster; geser seluruh klaster bila halo keluar dari area
    const halos = [];
    ['Positif', 'Negatif'].forEach(g => {
        const grp = nodes.filter(n => n.group === g);
        if (!grp.length) return;
        const calc = () => {
            const cx = grp.reduce((a, n) => a + n.x, 0) / grp.length;
            const cy = grp.reduce((a, n) => a + n.y, 0) / grp.length;
            const R = Math.max(...grp.map(n => Math.hypot(n.x - cx, n.y - cy) + n.r)) + 10;
            return { cx, cy, R };
        };
        let h = calc();
        const dx = Math.max(4 - (h.cx - h.R), 0) + Math.min((W - 4) - (h.cx + h.R), 0);
        const dy = Math.max(22 - (h.cy - h.R), 0) + Math.min((H - 4) - (h.cy + h.R), 0);
        if (dx || dy) { grp.forEach(n => { n.x += dx; n.y += dy; }); h = calc(); }
        halos.push({ group: g, ...h, total: grp.reduce((a, n) => a + n.val, 0) });
    });

    return { nodes, halos };
}

let keywordRowsCache = [];
let keywordObserver = null;
let keywordDrawnSize = '';

function renderKeywordCluster(rows) {
    keywordRowsCache = rows;
    keywordDrawnSize = '';
    const host = document.getElementById('keywordCluster');
    // Gambar ulang otomatis saat ukuran berubah (termasuk saat panel dibuka kembali)
    if (!keywordObserver && 'ResizeObserver' in window) {
        let timer = null;
        keywordObserver = new ResizeObserver(() => {
            clearTimeout(timer);
            timer = setTimeout(() => drawKeywordCluster(false), 120);
        });
        keywordObserver.observe(host);
    }
    drawKeywordCluster(true);
}

function drawKeywordCluster(animate) {
    const host = document.getElementById('keywordCluster');
    const W = Math.round(host.clientWidth), H = Math.round(host.clientHeight);
    if (W < 50 || H < 50) return; // panel sedang tertutup; digambar saat tampil lagi
    if (!animate && keywordDrawnSize === W + 'x' + H) return;
    keywordDrawnSize = W + 'x' + H;

    host.replaceChildren();
    host.style.overflow = 'hidden';

    if (!keywordRowsCache.length) {
        const empty = document.createElement('div');
        empty.style.cssText = 'height:100%;display:flex;align-items:center;justify-content:center;color:#64748b;font-size:13px;';
        empty.textContent = 'Belum ada data kata kunci';
        host.appendChild(empty);
        return;
    }

    const { nodes, halos } = layoutKeywordNodes(keywordRowsCache, W, H);
    const THEME = {
        Positif: { c1: '#34d399', c2: '#047857', stroke: '#6ee7b7', halo: '16,185,129', hex: '#10b981' },
        Negatif: { c1: '#fb7185', c2: '#9f1239', stroke: '#fda4af', halo: '244,63,94', hex: '#f43f5e' }
    };

    halos.forEach(h => {
        const t = THEME[h.group];
        const ring = document.createElement('div');
        ring.style.cssText = `position:absolute;left:${h.cx - h.R}px;top:${h.cy - h.R}px;width:${h.R * 2}px;height:${h.R * 2}px;` +
            `border-radius:50%;border:1px dashed rgba(${t.halo},0.45);background:rgba(${t.halo},0.07);pointer-events:none;`;
        host.appendChild(ring);

        const label = document.createElement('div');
        label.style.cssText = `position:absolute;left:${h.cx - 100}px;width:200px;top:${Math.max(2, h.cy - h.R - 18)}px;` +
            `text-align:center;font-size:11px;font-weight:700;letter-spacing:.08em;color:${t.hex};pointer-events:none;`;
        label.textContent = `${h.group.toUpperCase()} · ${h.total.toLocaleString('id-ID')}`;
        host.appendChild(label);
    });

    const tip = document.createElement('div');
    tip.style.cssText = 'display:none;position:absolute;z-index:10;pointer-events:none;background:#0f172a;border:1px solid #334155;' +
        'border-radius:8px;padding:8px 12px;font-size:12px;color:#cbd5e1;box-shadow:0 10px 25px rgba(0,0,0,.4);max-width:190px;';

    const groupTotals = {};
    nodes.forEach(n => { groupTotals[n.group] = (groupTotals[n.group] || 0) + n.val; });
    const fsCap = Math.max(12, Math.max(...nodes.map(n => n.r)) * 0.3);
    const els = [];

    nodes.forEach((n, idx) => {
        const t = THEME[n.group];
        const el = document.createElement('div');
        el.className = 'kw-node';
        el.style.cssText = `position:absolute;left:${n.x - n.r}px;top:${n.y - n.r}px;width:${n.r * 2}px;height:${n.r * 2}px;border-radius:50%;` +
            `background:radial-gradient(circle at 35% 30%, ${t.c1}, ${t.c2});border:1.5px solid ${t.stroke};box-sizing:border-box;` +
            'display:flex;align-items:center;justify-content:center;overflow:hidden;color:#fff;font-weight:600;line-height:1;white-space:nowrap;' +
            (animate ? `animation-delay:${Math.min(idx * 40, 600)}ms;` : 'animation:none;');
        const fs = Math.min(fsCap, (n.r * 1.7) / (Math.max(n.word.length, 3) * 0.58));
        if (fs >= 7) {
            el.style.fontSize = fs + 'px';
            el.textContent = n.word;
        }
        els.push(el);
        host.appendChild(el);

        el.addEventListener('mouseenter', () => {
            els.forEach(o => { o.style.opacity = o === el ? '1' : '0.3'; });
            tip.replaceChildren();
            const title = document.createElement('div');
            title.style.cssText = 'font-weight:600;color:#fff;margin-bottom:2px;';
            title.textContent = n.word;
            const l1 = document.createElement('div');
            l1.textContent = `Sentimen: ${n.group}`;
            const l2 = document.createElement('div');
            l2.textContent = `${n.val.toLocaleString('id-ID')} kemunculan`;
            const l3 = document.createElement('div');
            l3.style.color = '#64748b';
            l3.textContent = `${(n.val / groupTotals[n.group] * 100).toFixed(1)}% dari klaster ${n.group}`;
            tip.append(title, l1, l2, l3);
            tip.style.display = 'block';
        });
        el.addEventListener('mousemove', e => {
            const rect = host.getBoundingClientRect();
            let left = e.clientX - rect.left + 14;
            let top = e.clientY - rect.top + 14;
            if (left > rect.width - 200) left = e.clientX - rect.left - 200;
            if (top > rect.height - 90) top = e.clientY - rect.top - 90;
            tip.style.left = Math.max(0, left) + 'px';
            tip.style.top = Math.max(0, top) + 'px';
        });
        el.addEventListener('mouseleave', () => {
            els.forEach(o => { o.style.opacity = '1'; });
            tip.style.display = 'none';
        });
    });

    host.appendChild(tip);
}

function renderQuadrantChart(rows) {
    if (quadrantChartInstance) quadrantChartInstance.destroy();

    // Warna per kuadran (Q1..Q4)
    const palette = [
        { bg: 'rgba(16, 185, 129, 0.75)', border: '#10b981' },  // Q1 strong positive
        { bg: 'rgba(110, 231, 183, 0.6)', border: '#6ee7b7' },  // Q2 weak positive
        { bg: 'rgba(244, 63, 94, 0.75)', border: '#f43f5e' },   // Q3 strong negative
        { bg: 'rgba(251, 146, 60, 0.65)', border: '#fb923c' }   // Q4 weak negative
    ];
    const total = rows.reduce((a, r) => a + (Number(r.total_kasus) || 0), 0) || 1;
    const maxCases = Math.max(...rows.map(r => Number(r.total_kasus) || 0), 1);

    const points = rows.map((r, i) => {
        const name = String(r.kuadran_analisis);
        const qMatch = name.match(/^Q(\d)/i);
        const qIdx = qMatch ? Math.min(Math.max(parseInt(qMatch[1], 10) - 1, 0), 3) : (i % 4);
        const isPositive = /positive|positif/i.test(name);
        const cases = Number(r.total_kasus) || 0;
        return {
            x: isPositive ? 0.5 : -0.5,
            y: Number(r.rata_rata_confidence) || 0,
            r: Math.max(24, Math.sqrt(cases / maxCases) * 80),
            qIdx, name, cases,
            pct: cases / total * 100,
            short: name.split(' (')[0].split(' - ')[0]
        };
    });

    // Plugin: latar belakang 2x2 (negatif kiri / positif kanan) + garis pemisah + label bubble
    const quadrantPlugin = {
        id: 'quadrantBackdrop',
        beforeDatasetsDraw(chart) {
            const { ctx, chartArea: ca, scales: { x, y } } = chart;
            const midX = x.getPixelForValue(0);
            const midY = y.getPixelForValue((y.min + y.max) / 2);
            ctx.save();
            ctx.fillStyle = 'rgba(244, 63, 94, 0.06)';
            ctx.fillRect(ca.left, ca.top, midX - ca.left, ca.bottom - ca.top);
            ctx.fillStyle = 'rgba(16, 185, 129, 0.06)';
            ctx.fillRect(midX, ca.top, ca.right - midX, ca.bottom - ca.top);
            ctx.strokeStyle = 'rgba(148, 163, 184, 0.35)';
            ctx.setLineDash([5, 5]);
            ctx.beginPath();
            ctx.moveTo(midX, ca.top); ctx.lineTo(midX, ca.bottom);
            ctx.moveTo(ca.left, midY); ctx.lineTo(ca.right, midY);
            ctx.stroke();
            ctx.setLineDash([]);
            ctx.fillStyle = 'rgba(148, 163, 184, 0.55)';
            ctx.font = '600 12px "Plus Jakarta Sans", sans-serif';
            ctx.textAlign = 'center';
            ctx.fillText('NEGATIF', (ca.left + midX) / 2, ca.top + 16);
            ctx.fillText('POSITIF', (midX + ca.right) / 2, ca.top + 16);
            ctx.restore();
        },
        afterDatasetsDraw(chart) {
            const { ctx } = chart;
            chart.getDatasetMeta(0).data.forEach((el, i) => {
                const p = points[i];
                ctx.save();
                ctx.textAlign = 'center';
                ctx.textBaseline = 'middle';
                ctx.fillStyle = '#ffffff';
                ctx.font = '700 16px "Plus Jakarta Sans", sans-serif';
                ctx.fillText('Q' + (p.qIdx + 1), el.x, el.y - 9);
                ctx.font = '500 13px "Plus Jakarta Sans", sans-serif';
                ctx.fillText(p.cases.toLocaleString('id-ID'), el.x, el.y + 10);
                ctx.restore();
            });
        }
    };

    quadrantChartInstance = new Chart(document.getElementById('quadrantChart').getContext('2d'), {
        type: 'bubble',
        data: {
            datasets: [{
                label: 'Kuadran',
                data: points,
                backgroundColor: points.map(p => palette[p.qIdx].bg),
                borderColor: points.map(p => palette[p.qIdx].border),
                borderWidth: 2,
                hoverBorderWidth: 3
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            layout: { padding: { top: 6, right: 6 } },
            plugins: {
                legend: { display: false },
                tooltip: {
                    ...INSIGHT_TOOLTIP,
                    callbacks: {
                        title: items => points[items[0].dataIndex].name,
                        label: ctx => {
                            const p = points[ctx.dataIndex];
                            return [
                                ` Total kasus: ${p.cases.toLocaleString('id-ID')} (${p.pct.toFixed(1)}%)`,
                                ` Rata-rata confidence: ${formatDetailPercent(p.y)}`
                            ];
                        }
                    }
                }
            },
            scales: {
                x: {
                    min: -1, max: 1,
                    grid: { display: false },
                    border: { display: false },
                    ticks: { display: false }
                },
                y: {
                    min: 0.3, max: 1.15,
                    title: { display: true, text: 'Rata-rata Confidence', color: '#64748b', font: { size: 10 } },
                    ticks: {
                        color: '#94a3b8',
                        stepSize: 0.2,
                        callback: v => v <= 1 ? Math.round(v * 100) + '%' : ''
                    },
                    grid: { color: 'rgba(51, 65, 85, 0.6)' }
                }
            }
        },
        plugins: [quadrantPlugin]
    });

    // Kartu rincian per kuadran
    const detail = document.getElementById('quadrant-detail');
    detail.replaceChildren();
    points.forEach(p => {
        const card = document.createElement('div');
        card.className = 'bg-slate-900/50 border border-slate-700/60 rounded-xl p-3';
        card.style.borderLeft = '3px solid ' + palette[p.qIdx].border;

        const name = document.createElement('div');
        name.className = 'text-[11px] text-slate-400 leading-snug mb-1';
        name.textContent = p.name;

        const count = document.createElement('div');
        count.className = 'text-xl font-bold text-white';
        count.textContent = p.cases.toLocaleString('id-ID');

        const meta = document.createElement('div');
        meta.className = 'text-[11px] text-slate-500 mt-0.5';
        meta.textContent = `${p.pct.toFixed(1)}% dari total · confidence ${formatDetailPercent(p.y)}`;

        card.append(name, count, meta);
        detail.appendChild(card);
    });
}

function renderTopicChart(rows) {
    if (topicChartInstance) topicChartInstance.destroy();

    const topics = [...new Set(rows.map(r => r.topik_pembicaraan))];
    const countFor = (topic, key) => rows
        .filter(r => r.topik_pembicaraan === topic && (r.hasil_sentimen_ai === key))
        .reduce((a, r) => a + (Number(r.jumlah_komentar) || 0), 0);

    // Urutkan topik berdasarkan total komentar terbanyak
    const totalFor = t => rows.filter(r => r.topik_pembicaraan === t).reduce((a, r) => a + (Number(r.jumlah_komentar) || 0), 0);
    topics.sort((a, b) => totalFor(b) - totalFor(a));

    const series = [
        { key: 'positive', label: 'Positif', color: INSIGHT_COLORS.positive },
        { key: 'negative', label: 'Negatif', color: INSIGHT_COLORS.negative },
        { key: 'neutral',  label: 'Netral',  color: INSIGHT_COLORS.neutral },
        { key: null,       label: 'Belum Dianalisis', color: INSIGHT_COLORS.unknown }
    ];

    topicChartInstance = new Chart(document.getElementById('topicSentimentChart').getContext('2d'), {
        type: 'bar',
        data: {
            labels: topics.map(t => t.replace(/\b\w/g, c => c.toUpperCase())),
            datasets: series.map(s => ({
                label: s.label,
                data: topics.map(t => countFor(t, s.key)),
                backgroundColor: s.color.bg,
                borderColor: s.color.border,
                borderWidth: 1
            }))
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            interaction: { mode: 'index', intersect: false },
            plugins: {
                legend: { position: 'bottom', labels: { color: '#94a3b8', font: { size: 11 } } },
                tooltip: {
                    ...INSIGHT_TOOLTIP,
                    callbacks: {
                        footer: items => 'Total: ' + items.reduce((a, i) => a + i.parsed.y, 0).toLocaleString('id-ID')
                    }
                }
            },
            scales: {
                x: { stacked: true, ticks: { color: '#cbd5e1' }, grid: { display: false } },
                y: { stacked: true, ticks: { color: '#94a3b8' }, grid: { color: '#334155' } }
            }
        }
    });
}

// ===== Keterkaitan Topik -> Kata Lain (diagram alir) =====
let topicLinkRows = [];
let topicLinkSelected = null;

function renderTopicLinks(rows) {
    topicLinkRows = Array.isArray(rows) ? rows : [];
    const chips = document.getElementById('topicLinkChips');
    const body = document.getElementById('topicLinkBody');
    chips.innerHTML = '';
    body.innerHTML = '';

    if (!topicLinkRows.length) {
        body.innerHTML = '<p class="text-xs text-slate-500 py-6 text-center">Belum ada data keterkaitan topik.</p>';
        return;
    }

    // Topik dengan komentar terbanyak tampil lebih dulu
    topicLinkRows = [...topicLinkRows].sort((a, b) => (b.total_komentar || 0) - (a.total_komentar || 0));
    if (!topicLinkRows.some(r => r.topik_pembicaraan === topicLinkSelected)) {
        topicLinkSelected = topicLinkRows[0].topik_pembicaraan;
    }

    topicLinkRows.forEach(r => {
        const b = document.createElement('button');
        b.type = 'button';
        b.dataset.topic = r.topik_pembicaraan;
        b.className = 'px-3 py-1.5 rounded-full text-xs font-medium border transition-colors';
        b.textContent = r.topik_pembicaraan.replace(/\b\w/g, c => c.toUpperCase()) +
            ' (' + (Number(r.total_komentar) || 0).toLocaleString('id-ID') + ')';
        b.addEventListener('click', () => { topicLinkSelected = r.topik_pembicaraan; drawTopicLinks(); });
        chips.appendChild(b);
    });
    drawTopicLinks();
}

function drawTopicLinks() {
    const chips = document.getElementById('topicLinkChips');
    const body = document.getElementById('topicLinkBody');
    chips.querySelectorAll('button').forEach(b => {
        const on = b.dataset.topic === topicLinkSelected;
        b.className = 'px-3 py-1.5 rounded-full text-xs font-medium border transition-colors ' + (on
            ? 'bg-indigo-500/20 border-indigo-400 text-indigo-200'
            : 'bg-slate-900/50 border-slate-700 text-slate-400 hover:text-slate-200 hover:border-slate-500');
    });

    const row = topicLinkRows.find(r => r.topik_pembicaraan === topicLinkSelected);
    body.innerHTML = '';
    const words = row && Array.isArray(row.kata_terkait) ? row.kata_terkait : [];
    if (!words.length) {
        body.innerHTML = '<p class="text-xs text-slate-500 py-6 text-center">Belum ada kata yang cukup sering muncul bersama topik ini.</p>';
        return;
    }

    const NS = 'http://www.w3.org/2000/svg';
    const mk = (tag, attrs, text) => {
        const el = document.createElementNS(NS, tag);
        Object.entries(attrs || {}).forEach(([k, v]) => el.setAttribute(k, v));
        if (text !== undefined) el.textContent = text;
        return el;
    };
    const colorOf = w => {
        const p = w.positif || 0, n = w.negatif || 0, t = w.netral || 0, u = Math.max(0, (w.jumlah || 0) - p - n - t);
        const m = Math.max(p, n, t, u);
        if (m === 0 || m === u) return INSIGHT_COLORS.unknown.border;
        if (m === n) return INSIGHT_COLORS.negative.border;
        if (m === p) return INSIGHT_COLORS.positive.border;
        return INSIGHT_COLORS.neutral.border;
    };

    const W = 780, rowH = 34, padY = 14;
    const H = padY * 2 + words.length * rowH;
    const hubX = 20, hubW = 170, hubH = 54, hubY = H / 2 - hubH / 2;
    const wordX = 470, maxCount = Math.max(...words.map(w => w.jumlah || 0), 1);

    const svg = mk('svg', { viewBox: `0 0 ${W} ${H}`, width: '100%', role: 'img', style: 'min-width:560px;max-width:100%;display:block' });
    svg.appendChild(mk('title', {}, 'Kata yang sering muncul bersama topik ' + topicLinkSelected));

    // Garis penghubung (digambar dulu agar di belakang node)
    words.forEach((w, i) => {
        const y = padY + i * rowH + rowH / 2;
        const x1 = hubX + hubW, y1 = H / 2, x2 = wordX - 6;
        const sw = 2 + 14 * ((w.jumlah || 0) / maxCount);
        const path = mk('path', {
            d: `M${x1},${y1} C${x1 + 130},${y1} ${x2 - 130},${y} ${x2},${y}`,
            fill: 'none', stroke: colorOf(w), 'stroke-opacity': '0.45', 'stroke-width': sw.toFixed(1), 'stroke-linecap': 'round'
        });
        path.appendChild(mk('title', {}, `${row.topik_pembicaraan} → ${w.kata}: ${w.jumlah} komentar (positif ${w.positif}, negatif ${w.negatif}, netral ${w.netral})`));
        svg.appendChild(path);
    });

    // Node topik
    svg.appendChild(mk('rect', { x: hubX, y: hubY, width: hubW, height: hubH, rx: 12, fill: '#312e81', stroke: '#818cf8', 'stroke-width': 1.5 }));
    svg.appendChild(mk('text', { x: hubX + hubW / 2, y: hubY + 24, 'text-anchor': 'middle', fill: '#e0e7ff', 'font-size': 13, 'font-weight': 700 },
        row.topik_pembicaraan.length > 20 ? row.topik_pembicaraan.slice(0, 19) + '…' : row.topik_pembicaraan));
    svg.appendChild(mk('text', { x: hubX + hubW / 2, y: hubY + 42, 'text-anchor': 'middle', fill: '#a5b4fc', 'font-size': 10.5 },
        (Number(row.total_komentar) || 0).toLocaleString('id-ID') + ' komentar'));

    // Node kata + jumlah
    words.forEach((w, i) => {
        const y = padY + i * rowH + rowH / 2;
        svg.appendChild(mk('circle', { cx: wordX, cy: y, r: 5, fill: colorOf(w) }));
        svg.appendChild(mk('text', { x: wordX + 12, y: y + 4, fill: '#e2e8f0', 'font-size': 12.5, 'font-weight': 600 }, w.kata));
        svg.appendChild(mk('text', { x: W - 10, y: y + 4, 'text-anchor': 'end', fill: '#94a3b8', 'font-size': 11 },
            (w.jumlah || 0).toLocaleString('id-ID') + ' komentar'));
    });

    body.appendChild(svg);
}

function openModal(id) {
    const item = rawDataCache.find(i => i.id === id);
    if (!item) return;

    currentContentId = item.id;
    const sa = item.sentiment_analysis || {};

    document.getElementById('modal-comments-section').classList.add('hidden');
    document.getElementById('modal-comments-list').innerHTML = '';
    document.getElementById('btn-comments-text').innerText = 'Daftar Komen';

    let platformName = 'Platform';
    if (item.platform) {
        platformName = typeof item.platform === 'object' 
            ? (item.platform.name || item.platform.code || 'Platform')
            : item.platform;
    }

    const rawScore = (sa.confidence_score !== undefined) ? sa.confidence_score : item.confidence_score;
    const confidenceStr = formatDetailPercent(rawScore);

    document.getElementById('modal-comments-summary').innerHTML = commentsSummaryDetailHTML(getCommentsSummary(item));
    document.getElementById('modal-platform').innerText = platformName;
    document.getElementById('modal-title').innerText = item.title || 'Tanpa Judul';
    document.getElementById('modal-raw-text').innerText = item.raw_text || 'Tidak ada teks.';
    document.getElementById('modal-model').innerText = sa.model_name || item.model_name || '-';
    document.getElementById('modal-confidence').innerText = confidenceStr;
    document.getElementById('modal-analyzed').innerText = sa.analyzed_at || item.analyzed_at || '-';
    document.getElementById('modal-published').innerText = item.published_at || '-';
    document.getElementById('modal-link').href = safeUrl(item.clean_url) || '#';

    const mediaContainer = document.getElementById('modal-media-container');
    const mediaImg = document.getElementById('modal-media-img');
    const modalMedia = (item.media_urls && item.media_urls.length > 0) ? safeUrl(item.media_urls[0]) : '';
    if (modalMedia) {
        mediaImg.src = modalMedia;
        mediaContainer.classList.remove('hidden');
    } else {
        mediaContainer.classList.add('hidden');
    }

    document.getElementById('detail-modal').classList.remove('hidden');
}

async function toggleComments() {
    const section = document.getElementById('modal-comments-section');
    const list = document.getElementById('modal-comments-list');
    const btnText = document.getElementById('btn-comments-text');

    if (!section.classList.contains('hidden')) {
        section.classList.add('hidden');
        btnText.innerText = 'Daftar Komen';
        return;
    }

    section.classList.remove('hidden');
    btnText.innerText = 'Tutup Komen';
    list.innerHTML = `
        <div class="py-4 text-center text-slate-400 text-xs">
            <i class="ph-bold ph-spinner animate-spin text-lg mb-1"></i>
            <p>Memuat komentar dari server...</p>
        </div>
    `;

    try {
        const response = await authFetch(`${COMMENTS_API_URL}?content_id=${encodeURIComponent(currentContentId)}`);
        const result = await response.json();

        if (result.status && result.data && result.data.length > 0) {
            document.getElementById('modal-comments-count').innerText = `${result.total_comments} Komentar`;
            
            list.innerHTML = result.data.map(c => {
                const cSa = c.sentiment_analysis || {};
                const cSent = (cSa.sentiment || 'neutral').toLowerCase();
                
                let badgeClr = "bg-amber-500/10 text-amber-400 border-amber-500/20";
                if (cSent === 'positive') badgeClr = "bg-emerald-500/10 text-emerald-400 border-emerald-500/20";
                if (cSent === 'negative') badgeClr = "bg-rose-500/10 text-rose-400 border-rose-500/20";

                return `
                    <div class="bg-slate-950 p-3 rounded-xl border border-slate-800 text-xs">
                        <div class="flex justify-between items-center mb-1">
                            <span class="font-semibold text-slate-200">${esc(c.author_name)}</span>
                            <span class="px-2 py-0.5 rounded text-[10px] font-medium border uppercase ${badgeClr}">
                                ${esc(cSent)} (${formatDetailPercent(cSa.confidence_score)})
                            </span>
                        </div>
                        <p class="text-slate-300 leading-relaxed">${esc(c.comment_text)}</p>
                        <span class="text-[10px] text-slate-500 mt-1 block">${esc(c.posted_at || c.crawled_at || '')}</span>
                    </div>
                `;
            }).join('');
        } else {
            document.getElementById('modal-comments-count').innerText = '0 Komentar';
            list.innerHTML = `<div class="py-4 text-center text-slate-500 text-xs">Belum ada komentar untuk konten ini.</div>`;
        }
    } catch (err) {
        console.error(err);
        if (err && err.message === 'Unauthorized') return;
        list.innerHTML = `<div class="py-4 text-center text-rose-400 text-xs">Gagal mengambil data komentar.</div>`;
    }
}

function closeModal() {
    document.getElementById('detail-modal').classList.add('hidden');
}

// KELOLA KEYWORD MODAL LOGIC
function openKeywordModal() {
    document.getElementById('keyword-modal').classList.remove('hidden');
    fetchKeywordsForPlatform();
}

function closeKeywordModal() {
    document.getElementById('keyword-modal').classList.add('hidden');
    hideKwAlert();
}

function showKwAlert(msg, isSuccess = true) {
    const alertBox = document.getElementById('kw-alert');
    alertBox.classList.remove('hidden', 'bg-emerald-500/10', 'text-emerald-400', 'border-emerald-500/30', 'bg-rose-500/10', 'text-rose-400', 'border-rose-500/30');
    alertBox.classList.add('border');
    if (isSuccess) {
        alertBox.classList.add('bg-emerald-500/10', 'text-emerald-400', 'border-emerald-500/30');
    } else {
        alertBox.classList.add('bg-rose-500/10', 'text-rose-400', 'border-rose-500/30');
    }
    alertBox.innerText = msg;
}

function hideKwAlert() {
    document.getElementById('kw-alert').classList.add('hidden');
}

async function fetchKeywordsForPlatform() {
    hideKwAlert();
    const platformId = document.getElementById('kw-platform-select').value;
    const container = document.getElementById('kw-inputs-container');
    const loading = document.getElementById('kw-loading-indicator');

    container.classList.add('hidden');
    loading.classList.remove('hidden');

    currentKeywordsState = Array(10).fill(null).map(() => ({ id: null, keyword: '' }));

    try {
        const response = await authFetch(`${KEYWORDS_API_URL}?platform_id=${encodeURIComponent(platformId)}`);
        const result = await response.json();

        if (result.status && Array.isArray(result.data)) {
            result.data.slice(0, 10).forEach((kwObj, idx) => {
                currentKeywordsState[idx] = {
                    id: kwObj.id,
                    keyword: kwObj.keyword
                };
            });
        }
    } catch (err) {
        console.error("Gagal mengambil keyword:", err);
    } finally {
        loading.classList.add('hidden');
        container.classList.remove('hidden');
        render10KeywordInputs();
    }
}

function render10KeywordInputs() {
    const canEdit = isAdmin();
    const container = document.getElementById('kw-inputs-container');

    container.innerHTML = currentKeywordsState.map((slot, index) => {
        const hasExistingId = slot.id !== null;

        let actionBtn = '';
        if (canEdit) {
            actionBtn = hasExistingId ? `
                <button type="button" onclick="deleteSingleKeyword(${index})" title="Hapus Keyword" class="p-2 bg-rose-500/10 hover:bg-rose-500/20 text-rose-400 border border-rose-500/30 rounded-xl text-xs transition">
                    <i class="ph-bold ph-trash text-sm"></i>
                </button>
            ` : `
                <button type="button" onclick="clearInputSlot(${index})" title="Bersihkan Input" class="p-2 bg-slate-800 hover:bg-slate-700 text-slate-400 rounded-xl text-xs transition">
                    <i class="ph-bold ph-backspace text-sm"></i>
                </button>
            `;
        }

        return `
            <div class="flex items-center space-x-2">
                <span class="text-xs font-mono text-slate-500 w-6 text-right">${index + 1}.</span>
                <input type="text" id="kw-input-${index}" value="${esc(slot.keyword)}" placeholder="Kolom ${index + 1} (kosongkan jika tidak dipakai)" ${canEdit ? '' : 'disabled'}
                    class="flex-1 px-3.5 py-2 bg-slate-950 border border-slate-700 rounded-xl text-white text-xs focus:outline-none focus:border-amber-500 transition disabled:opacity-60 disabled:cursor-not-allowed">
                ${actionBtn}
            </div>
        `;
    }).join('');

    // Role non-admin: hanya bisa melihat
    document.getElementById('kw-save-btn').classList.toggle('hidden', !canEdit);
    if (!canEdit) {
        showKwAlert('Akun Anda hanya bisa melihat keyword. Tambah, ubah, dan hapus khusus admin.', false);
    }
}

function clearInputSlot(index) {
    const input = document.getElementById(`kw-input-${index}`);
    if (input) input.value = '';
}

async function deleteSingleKeyword(index) {
    if (!isAdmin()) { showKwAlert('Hanya admin yang boleh menghapus keyword.', false); return; }

    const slot = currentKeywordsState[index];
    if (!slot.id) return;

    if (!confirm(`Hapus keyword "${slot.keyword}" dari database?`)) return;

    try {
        const response = await authFetch(`${KEYWORDS_API_URL}?id=${encodeURIComponent(slot.id)}`, {
            method: 'DELETE'
        });
        const result = await response.json().catch(() => ({}));

        if (response.ok && result.status) {
            showKwAlert("Keyword berhasil dihapus!", true);
            currentKeywordsState[index] = { id: null, keyword: '' };
            render10KeywordInputs();
        } else if (response.status === 403) {
            showKwAlert("Akses ditolak: hanya admin yang boleh menghapus keyword.", false);
        } else {
            showKwAlert("Gagal menghapus: " + (result.message || ('HTTP ' + response.status)), false);
        }
    } catch (err) {
        console.error(err);
        if (err && err.message === 'Unauthorized') return;
        showKwAlert("Terjadi kesalahan sistem saat menghapus.", false);
    }
}

async function saveKeywords(e) {
    e.preventDefault();
    hideKwAlert();

    if (!isAdmin()) {
        showKwAlert('Hanya admin yang boleh mengubah keyword.', false);
        return;
    }

    const platformId = parseInt(document.getElementById('kw-platform-select').value);
    const saveBtn = document.getElementById('kw-save-btn');
    saveBtn.disabled = true;
    saveBtn.classList.add('opacity-50');

    let promises = [];

    for (let i = 0; i < 10; i++) {
        const inputVal = document.getElementById(`kw-input-${i}`).value.trim();
        const initialSlot = currentKeywordsState[i];

        if (inputVal !== '' && initialSlot.id === null) {
            promises.push(
                authFetch(KEYWORDS_API_URL, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        platform_id: platformId,
                        keyword: inputVal,
                        is_active: 1
                    })
                })
            );
        }
        else if (inputVal !== '' && initialSlot.id !== null && inputVal !== initialSlot.keyword) {
            promises.push(
                authFetch(KEYWORDS_API_URL, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        id: initialSlot.id,
                        keyword: inputVal,
                        is_active: 1
                    })
                })
            );
        }
        else if (inputVal === '' && initialSlot.id !== null) {
            promises.push(
                authFetch(`${KEYWORDS_API_URL}?id=${encodeURIComponent(initialSlot.id)}`, {
                    method: 'DELETE'
                })
            );
        }
    }

    if (promises.length === 0) {
        showKwAlert("Tidak ada perubahan keyword yang perlu disimpan.", true);
        saveBtn.disabled = false;
        saveBtn.classList.remove('opacity-50');
        return;
    }

    try {
        const responses = await Promise.all(promises);
        const failed = responses.filter(r => !r.ok);

        // Dipanggil dulu karena fungsi ini menyembunyikan alert; pesan hasil ditampilkan setelahnya
        await fetchKeywordsForPlatform();

        if (failed.length === 0) {
            showKwAlert("Perubahan keyword berhasil disimpan!", true);
        } else if (failed.some(r => r.status === 403)) {
            showKwAlert("Akses ditolak: hanya admin yang boleh mengubah keyword.", false);
        } else {
            showKwAlert(failed.length + " operasi gagal disimpan.", false);
        }
    } catch (err) {
        console.error(err);
        if (err && err.message === 'Unauthorized') return;
        showKwAlert("Beberapa operasi gagal disimpan.", false);
    } finally {
        saveBtn.disabled = false;
        saveBtn.classList.remove('opacity-50');
    }
}

// Dipanggil oleh App.svelte (onMount) setelah markup dashboard dirender
window.initDashboard = () => {
    updateBarModeUI();
    const auth = getAuth();
    if (auth) {
        applyAuthUser(auth);
        hideLogin();
        startApp();
    } else {
        showLogin();
    }
};
