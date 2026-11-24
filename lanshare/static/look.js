// Apply the saved theme and light/dark mode before first paint, so the page never flashes.
// URL options override for this visit only, e.g. /?mode=dark&accent=violet
try {
  const p = JSON.parse(localStorage.getItem('ls-look') || '{}');
  const q = new URLSearchParams(location.search);
  const mode = q.get('mode') || p.mode;
  const accent = q.get('accent') || p.accent;
  if (['light', 'dark', 'auto'].includes(mode)) document.documentElement.dataset.mode = mode;
  if (['blue', 'violet', 'teal', 'green', 'orange', 'rose'].includes(accent)) document.documentElement.dataset.accent = accent;
} catch (e) { /* storage blocked: keep defaults */ }
