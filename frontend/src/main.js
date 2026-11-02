import { mount } from 'svelte';
import App from './App.svelte';

// Alamat API relatif terhadap lokasi aplikasi, sehingga tetap benar saat dipasang
// di sub-path (mis. /sentiment-analysis/api). nginx meneruskan /api ke backend secara internal.
window.SENTIMENT_API_BASE = new URL('api', document.baseURI).href;

mount(App, { target: document.getElementById('app') });
