<script>
  import { onMount } from 'svelte';
  // Dashboard lama dipindahkan apa adanya: markup di dashboard.html, logika di dashboard.js.
  // Bagian-bagiannya bisa dipecah menjadi komponen Svelte secara bertahap.
  import markup from './legacy/dashboard.html?raw';
  import dashboardScript from './legacy/dashboard.js?raw';

  onMount(() => {
    // Dijalankan sebagai <script> biasa agar fungsi-fungsinya global (dipakai onclick="..." di markup)
    const script = document.createElement('script');
    script.textContent = dashboardScript;
    document.body.appendChild(script);
    window.initDashboard();
  });
</script>

{@html markup}
