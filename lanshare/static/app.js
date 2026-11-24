// LANShare front end — plain JavaScript, no external libraries (works fully offline).
'use strict';

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const ROLE = { superadmin: 'ผู้ดูแลสูงสุด', editor: 'ผู้แก้ไข', user: 'ผู้ใช้ทั่วไป' };
const state = { me: null, path: '/', entries: [], canWrite: false, sel: new Set(), sort: { key: 'name', dir: 1 },
  layout: 'list', es: null, known: new Set(), depts: [], view: 'files' };

// ------------------------------------------------------------------ helpers
const icon = (id) => `<svg><use href="#i-${id}"/></svg>`;
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const join = (dir, name) => (dir === '/' ? '' : dir) + '/' + name;
const parts = (p) => p.split('/').filter(Boolean);

function fmtSize(n) {
  if (n < 1024) return n + ' B';
  const u = ['KB', 'MB', 'GB', 'TB']; let i = -1;
  do { n /= 1024; i++; } while (n >= 1024 && i < u.length - 1);
  return n.toFixed(n < 10 ? 1 : 0) + ' ' + u[i];
}
const rtf = new Intl.RelativeTimeFormat('th', { numeric: 'auto' });
function fmtDate(ts) {
  const d = Date.now() / 1000 - ts;
  if (d < 60) return 'เมื่อสักครู่';
  if (d < 3600) return rtf.format(-Math.floor(d / 60), 'minute');
  if (d < 86400) return rtf.format(-Math.floor(d / 3600), 'hour');
  if (d < 7 * 86400) return rtf.format(-Math.floor(d / 86400), 'day');
  return new Date(ts * 1000).toLocaleDateString('th-TH', { day: 'numeric', month: 'short', year: 'numeric' });
}
function kind(name) {
  const ext = (name.split('.').pop() || '').toLowerCase();
  if (['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp'].includes(ext)) return ['img', 'image', ext];
  if (ext === 'pdf') return ['pdf', 'file', 'PDF'];
  if (['doc', 'docx', 'odt', 'rtf', 'txt', 'md'].includes(ext)) return ['doc', 'file', ext.toUpperCase().slice(0, 4)];
  if (['xls', 'xlsx', 'ods', 'csv'].includes(ext)) return ['sheet', 'file', ext.toUpperCase().slice(0, 4)];
  if (['zip', 'rar', '7z', 'tar', 'gz'].includes(ext)) return ['zip', 'file', ext.toUpperCase().slice(0, 3)];
  return ['other', 'file', ext.slice(0, 4).toUpperCase()];
}
const previewable = (name) => /\.(png|jpe?g|gif|webp|bmp|pdf|txt)$/i.test(name);

async function api(url, opts = {}) {
  const res = await fetch(url, { credentials: 'same-origin', ...opts,
    headers: { 'X-LANShare': '1', ...(opts.json ? { 'Content-Type': 'application/json' } : {}), ...(opts.headers || {}) },
    body: opts.json ? JSON.stringify(opts.json) : opts.body });
  let data = {};
  try { data = await res.json(); } catch (e) { /* non-JSON */ }
  if (res.status === 401 && !url.endsWith('/login')) { showLogin(); throw new Error(data.error || 'กรุณาเข้าสู่ระบบ'); }
  if (!res.ok) throw new Error(data.error || `เกิดข้อผิดพลาด (${res.status})`);
  return data;
}

// ------------------------------------------------------------------ toasts (auto-hide)
function toast(msg, type = 'info') {
  const life = type === 'err' ? 6000 : 3500;
  const ic = { ok: 'check', err: 'alert', warn: 'alert', info: 'info' }[type];
  const el = document.createElement('div');
  el.className = `toast ${type}`;
  el.setAttribute('role', type === 'err' ? 'alert' : 'status');
  el.innerHTML = `${icon(ic)}<p>${esc(msg)}</p><button class="icon-btn" aria-label="ปิด">${icon('x')}</button><i class="bar"></i>`;
  el.querySelector('.bar').style.setProperty('--life', life + 'ms');
  $('#toasts').append(el);
  let left = life, start = Date.now(), timer;
  const close = () => { clearTimeout(timer); el.classList.add('out'); setTimeout(() => el.remove(), 200); };
  const run = () => { start = Date.now(); timer = setTimeout(close, left); };
  el.addEventListener('mouseenter', () => { clearTimeout(timer); left -= Date.now() - start; });
  el.addEventListener('mouseleave', run);
  el.querySelector('button').onclick = close;
  run();
  while ($$('.toast').length > 4) $('.toast').remove();
}
const fail = (e) => toast(e.message || String(e), 'err');

// ------------------------------------------------------------------ modals
function modal(html, { wide = false } = {}) {
  const root = $('#modalRoot');
  root.innerHTML = `<div class="modal ${wide ? 'wide' : ''}" role="dialog" aria-modal="true">${html}</div>`;
  root.hidden = false;
  const m = root.firstElementChild;
  const close = () => { root.hidden = true; root.innerHTML = ''; };
  root.onclick = (e) => { if (e.target === root) close(); };
  m.addEventListener('keydown', (e) => { if (e.key === 'Escape') close(); });
  setTimeout(() => (m.querySelector('[autofocus]') || m.querySelector('input,button'))?.focus(), 30);
  $$('[data-close]', m).forEach((b) => (b.onclick = close));
  return { m, close };
}
function ask({ title, desc = '', label, value = '', ok = 'ตกลง', type = 'text' }) {
  return new Promise((resolve) => {
    const { m, close } = modal(`<form><h3>${esc(title)}</h3>${desc ? `<p class="desc">${esc(desc)}</p>` : ''}
      <label class="field"><span>${esc(label)}</span><input name="v" type="${type}" value="${esc(value)}" autofocus required></label>
      <div class="actions"><button type="button" class="btn" data-close>ยกเลิก</button><button class="btn primary">${esc(ok)}</button></div></form>`);
    const inp = $('input', m);
    if (value && type === 'text') { const dot = value.lastIndexOf('.'); setTimeout(() => inp.setSelectionRange(0, dot > 0 ? dot : value.length), 40); }
    $('form', m).onsubmit = (e) => { e.preventDefault(); close(); resolve(inp.value.trim()); };
    $('[data-close]', m).addEventListener('click', () => resolve(null));
  });
}
function confirmBox({ title, desc, ok = 'ยืนยัน', danger = false }) {
  return new Promise((resolve) => {
    const { m, close } = modal(`<h3>${esc(title)}</h3><p class="desc">${esc(desc)}</p>
      <div class="actions"><button class="btn" data-close>ยกเลิก</button><button class="btn ${danger ? 'danger solid' : 'primary'}" id="okBtn">${esc(ok)}</button></div>`);
    $('[data-close]', m).addEventListener('click', () => resolve(false));
    $('#okBtn', m).onclick = () => { close(); resolve(true); };
  });
}

// ------------------------------------------------------------------ look (theme + mode)
function look(update) {
  let p = {};
  try { p = JSON.parse(localStorage.getItem('ls-look') || '{}'); } catch (e) { /* ignore */ }
  const html = document.documentElement;
  if (update) {
    Object.assign(p, update);
    try { localStorage.setItem('ls-look', JSON.stringify(p)); } catch (e) { /* ignore */ }
    if (update.mode) html.dataset.mode = update.mode;
    if (update.accent) html.dataset.accent = update.accent;
  }
  $$('#modeSeg button').forEach((b) => b.classList.toggle('on', b.dataset.mode === html.dataset.mode));
  $$('#swatches button').forEach((b) => b.classList.toggle('on', b.dataset.accent === html.dataset.accent));
  state.layout = p.layout || 'list';
  $('#files').dataset.layout = state.layout;
  $$('#viewSeg button').forEach((b) => b.classList.toggle('on', b.dataset.layout === state.layout));
}

// ------------------------------------------------------------------ auth
function showLogin() {
  $('#app').hidden = true; $('#login').hidden = false;
  state.es?.close(); state.es = null;
  setTimeout(() => $('#lgUser').focus(), 30);
}
$('#loginForm').onsubmit = async (e) => {
  e.preventDefault();
  try {
    const r = await api('/api/login', { method: 'POST', json: { username: $('#lgUser').value, password: $('#lgPass').value } });
    $('#lgPass').value = '';
    start(r.user);
  } catch (err) { fail(err); }
};
$('#logoutBtn').onclick = async () => { await api('/api/logout', { method: 'POST' }).catch(() => {}); location.hash = ''; showLogin(); toast('ออกจากระบบแล้ว', 'ok'); };
$('#pwBtn').onclick = () => {
  closePops();
  const { m, close } = modal(`<form><h3>เปลี่ยนรหัสผ่าน</h3><p class="desc">รหัสผ่านใหม่ต้องยาวอย่างน้อย 8 ตัว</p>
    <label class="field"><span>รหัสผ่านปัจจุบัน</span><input name="cur" type="password" autocomplete="current-password" required autofocus></label>
    <label class="field"><span>รหัสผ่านใหม่</span><input name="new" type="password" autocomplete="new-password" minlength="8" required></label>
    <label class="field"><span>ยืนยันรหัสผ่านใหม่</span><input name="new2" type="password" autocomplete="new-password" minlength="8" required></label>
    <div class="actions"><button type="button" class="btn" data-close>ยกเลิก</button><button class="btn primary">บันทึก</button></div></form>`);
  $('form', m).onsubmit = async (e) => {
    e.preventDefault();
    const f = e.target;
    if (f.new.value !== f.new2.value) return toast('รหัสผ่านใหม่ทั้งสองช่องไม่ตรงกัน', 'err');
    try { await api('/api/me/password', { method: 'POST', json: { current: f.cur.value, new: f.new.value } }); close(); toast('เปลี่ยนรหัสผ่านแล้ว', 'ok'); }
    catch (err) { fail(err); }
  };
};

async function start(user) {
  state.me = user;
  $('#login').hidden = true; $('#app').hidden = false;
  $('#meName').textContent = user.display_name;
  $('#meRole').textContent = ROLE[user.role];
  $('#avatar').textContent = (user.display_name || user.username).trim().charAt(0).toUpperCase();
  $('#adminNav').hidden = user.role === 'user';
  $('#netNav').hidden = user.role !== 'superadmin';
  await loadDepts();
  route();
}

// ------------------------------------------------------------------ navigation
function route() {
  const h = decodeURIComponent(location.hash.slice(1));
  if (h === 'users' && state.me.role !== 'user') return showView('users');
  if (h === 'recycle' && state.me.role !== 'user') return showView('recycle');
  if (h === 'network' && state.me.role === 'superadmin') return showView('network');
  showView('files', h.startsWith('/') ? h : '/');
}
window.addEventListener('hashchange', () => state.me && route());
function go(path) { location.hash = encodeURIComponent(path).replace(/%2F/g, '/'); }

function showView(view, path) {
  state.view = view;
  $('#viewFiles').hidden = view !== 'files';
  $('#viewUsers').hidden = view !== 'users';
  $('#viewRecycle').hidden = view !== 'recycle';
  $('#viewNetwork').hidden = view !== 'network';
  $('#search').parentElement.hidden = view !== 'files';
  closeNav();
  if (view === 'files') openFolder(path);
  else { state.es?.close(); state.es = null; setLive(false); }
  if (view === 'users') loadUsers();
  if (view === 'recycle') loadRecycle();
  if (view === 'network') loadNetwork();
  markNav();
}
function markNav() {
  $$('.nav-item').forEach((b) => {
    const on = state.view === 'files' ? b.dataset.view === 'files' && (b.dataset.path === '/' ? state.path === '/' : state.path === b.dataset.path || state.path.startsWith(b.dataset.path + '/')) : b.dataset.view === state.view;
    b.classList.toggle('on', on);
  });
}
async function loadDepts() {
  try { state.depts = (await api('/api/depts')).depts; } catch (e) { state.depts = []; }
  $('#deptNav').innerHTML = state.depts.length
    ? state.depts.map((d) => `<button class="nav-item" data-view="files" data-path="/${esc(d)}">${icon('folder')}<span>${esc(d)}</span></button>`).join('')
    : `<p class="muted nav-label">ยังไม่มีโฟลเดอร์</p>`;
  markNav();
}
document.addEventListener('click', (e) => {
  const n = e.target.closest('.nav-item');
  if (!n) return;
  if (n.dataset.view === 'files') go(n.dataset.path); else location.hash = n.dataset.view;
});

// ------------------------------------------------------------------ files
async function openFolder(path, { quiet = false } = {}) {
  const moved = path !== state.path;
  try {
    const r = await api('/api/list?path=' + encodeURIComponent(path));
    const old = state.known;
    state.known = new Set(r.entries.map((e) => e.name));
    state.fresh = quiet && !moved ? r.entries.filter((e) => !old.has(e.name)).map((e) => e.name) : [];
    state.path = r.path || '/'; state.entries = r.entries; state.canWrite = r.canWrite;
    if (moved) { state.sel.clear(); $('#search').value = ''; }
    render();
    if (moved || !state.es) subscribe();
    markNav();
  } catch (e) {
    fail(e);
    if (!quiet && path !== '/') go('/');
  }
}
function render() {
  const p = parts(state.path);
  $('#crumbs').innerHTML = `<button data-go="/">${icon('home')}หน้าแรก</button>` +
    p.map((seg, i) => `<span class="sep">›</span><button data-go="/${p.slice(0, i + 1).map(esc).join('/')}">${esc(seg)}</button>`).join('');
  $$('#crumbs [data-go]').forEach((b) => (b.onclick = () => go(b.dataset.go)));
  const top = state.path === '/';
  $('#uploadBtn').hidden = $('#newFolderBtn').hidden = !state.canWrite;
  $('#readonlyNote').hidden = !(top && !state.canWrite);
  $('#bulkDelete').hidden = !(state.sel.size && state.canWrite);
  $('#bulkDelete span').textContent = `ลบที่เลือก (${state.sel.size})`;

  const q = $('#search').value.trim().toLowerCase();
  const { key, dir } = state.sort;
  const list = state.entries.filter((e) => !q || e.name.toLowerCase().includes(q)).sort((a, b) =>
    (a.type !== b.type ? (a.type === 'dir' ? -1 : 1) : 0) ||
    (key === 'name' ? a.name.localeCompare(b.name, 'th', { numeric: true }) : (a[key] - b[key])) * dir);
  const arrow = (k) => (state.sort.key === k ? (state.sort.dir > 0 ? '↑' : '↓') : '');
  const head = `<div class="row head"><span></span><button data-sort="name">ชื่อ ${arrow('name')}</button><button data-sort="size">ขนาด ${arrow('size')}</button><button data-sort="mtime">แก้ไขล่าสุด ${arrow('mtime')}</button><span></span></div>`;
  if (!list.length) {
    const msg = q ? ['ไม่พบไฟล์ที่ค้นหา', `ไม่มีชื่อที่มีคำว่า “${q}” ในโฟลเดอร์นี้`]
      : top ? ['ยังไม่มีโฟลเดอร์', state.canWrite ? 'กด “โฟลเดอร์ใหม่” เพื่อสร้างโฟลเดอร์ของฝ่าย' : 'ติดต่อผู้ดูแลเพื่อขอสิทธิ์เข้าโฟลเดอร์ของฝ่าย']
      : ['โฟลเดอร์นี้ยังว่าง', 'ลากไฟล์มาวางตรงนี้ หรือกด “อัปโหลด”'];
    $('#files').innerHTML = head + `<div class="empty"><div class="big">${icon(q ? 'search' : 'folder')}</div><b>${esc(msg[0])}</b>${esc(msg[1])}</div>`;
  } else {
    $('#files').innerHTML = head + list.map((e) => {
      const [k, ic, label] = e.type === 'dir' ? ['dir', 'folder', ''] : kind(e.name);
      const vp = join(state.path, e.name);
      return `<div class="row item ${state.sel.has(vp) ? 'sel' : ''} ${state.fresh?.includes(e.name) ? 'fresh' : ''}" data-path="${esc(vp)}" data-type="${e.type}" tabindex="0">
        <input type="checkbox" class="check" ${state.sel.has(vp) ? 'checked' : ''} aria-label="เลือก ${esc(e.name)}" ${state.canWrite ? '' : 'disabled tabindex="-1"'}>
        <div class="name"><button data-open>${`<span class="ficon ${k}">${icon(ic)}${label && k !== 'img' ? `<b>${esc(label)}</b>` : ''}</span>`}<span>${esc(e.name)}</span></button></div>
        <span class="size">${e.type === 'dir' ? '—' : fmtSize(e.size)}</span>
        <span class="date" title="${new Date(e.mtime * 1000).toLocaleString('th-TH')}">${fmtDate(e.mtime)}</span>
        <button class="icon-btn more" aria-label="ตัวเลือก">${icon('dots')}</button></div>`;
    }).join('');
  }
  $$('#files [data-sort]').forEach((b) => (b.onclick = () => {
    state.sort = { key: b.dataset.sort, dir: state.sort.key === b.dataset.sort ? -state.sort.dir : 1 }; render();
  }));
  state.fresh = [];
}
$('#search').addEventListener('input', () => render());
$('#files').addEventListener('click', (e) => {
  const row = e.target.closest('.row.item'); if (!row) return;
  const vp = row.dataset.path;
  if (e.target.closest('.check')) { e.target.checked ? state.sel.add(vp) : state.sel.delete(vp); return render(); }
  if (e.target.closest('.more')) return openCtx(e, row);
  if (e.target.closest('[data-open]')) return openItem(row);
});
$('#files').addEventListener('dblclick', (e) => { const row = e.target.closest('.row.item'); if (row && !e.target.closest('.check,.more')) openItem(row); });
$('#files').addEventListener('contextmenu', (e) => { const row = e.target.closest('.row.item'); if (row) { e.preventDefault(); openCtx(e, row); } });
$('#files').addEventListener('keydown', (e) => {
  const row = e.target.closest('.row.item'); if (!row) return;
  if (e.key === 'Enter') openItem(row);
  if (e.key === 'Delete' && state.canWrite) del([row.dataset.path]);
  if (e.key === 'F2' && state.canWrite) { e.preventDefault(); rename(row.dataset.path); }
});
function openItem(row) {
  const vp = row.dataset.path;
  if (row.dataset.type === 'dir') return go(vp);
  if (previewable(vp)) return preview(vp);
  download(vp);
}
function download(vp) {
  const a = document.createElement('a');
  a.href = '/api/download?path=' + encodeURIComponent(vp); a.download = '';
  document.body.append(a); a.click(); a.remove();
}
function preview(vp) {
  const name = parts(vp).pop();
  const url = '/api/download?inline=1&path=' + encodeURIComponent(vp);
  const body = /\.(pdf|txt)$/i.test(name) ? `<iframe src="${url}" title="${esc(name)}"></iframe>` : `<img src="${url}" alt="${esc(name)}">`;
  const { m } = modal(`<h3>${esc(name)}</h3><div class="preview">${body}</div>
    <div class="actions"><button class="btn" data-close>ปิด</button><button class="btn primary" id="dl">${icon('download')}ดาวน์โหลด</button></div>`, { wide: true });
  $('#dl', m).onclick = () => download(vp);
}

// context menu
function openCtx(e, row) {
  const vp = row.dataset.path, isDir = row.dataset.type === 'dir';
  const items = [[isDir ? 'folder' : (previewable(vp) ? 'eye' : 'download'), isDir ? 'เปิด' : (previewable(vp) ? 'ดูตัวอย่าง' : 'ดาวน์โหลด'), () => openItem(row)]];
  if (!isDir && previewable(vp)) items.push(['download', 'ดาวน์โหลด', () => download(vp)]);
  if (state.canWrite) {
    items.push(['edit', 'เปลี่ยนชื่อ', () => rename(vp)]);
    items.push(['trash', 'ลบ', () => del([vp]), 'danger']);
  }
  const c = $('#ctx');
  c.innerHTML = items.map(([ic, t, , cls], i) => `<button data-i="${i}" class="${cls || ''}">${icon(ic)}${t}</button>`).join('');
  c.hidden = false;
  const r = c.getBoundingClientRect();
  c.style.left = Math.min(e.clientX, innerWidth - r.width - 8) + 'px';
  c.style.top = Math.min(e.clientY, innerHeight - r.height - 8) + 'px';
  $$('button', c).forEach((b) => (b.onclick = () => { c.hidden = true; items[b.dataset.i][2](); }));
}
document.addEventListener('click', (e) => { if (!e.target.closest('#ctx, .more')) $('#ctx').hidden = true; });

async function rename(vp) {
  const old = parts(vp).pop();
  const name = await ask({ title: 'เปลี่ยนชื่อ', label: 'ชื่อใหม่', value: old, ok: 'เปลี่ยนชื่อ' });
  if (!name || name === old) return;
  try { await api('/api/rename', { method: 'POST', json: { path: vp, newName: name } }); toast(`เปลี่ยนชื่อเป็น “${name}” แล้ว`, 'ok'); if (state.path === '/') loadDepts(); openFolder(state.path, { quiet: true }); }
  catch (e) { fail(e); }
}
async function del(paths) {
  const top = state.path === '/';
  const what = paths.length === 1 ? `“${parts(paths[0]).pop()}”` : `${paths.length} รายการ`;
  const ok = await confirmBox({ title: `ลบ ${what}?`, danger: true, ok: 'ลบ',
    desc: top ? 'นี่คือโฟลเดอร์ของฝ่าย ไฟล์ทั้งหมดข้างในจะถูกย้ายไปถังขยะด้วย ผู้ดูแลกู้คืนได้จากเมนู “ถังขยะ”'
      : 'รายการจะถูกย้ายไปถังขยะ ผู้ดูแลหรือผู้แก้ไขกู้คืนได้' });
  if (!ok) return;
  try { const r = await api('/api/delete', { method: 'POST', json: { paths } }); state.sel.clear(); toast(`ลบ ${r.deleted} รายการแล้ว`, 'ok'); if (top) loadDepts(); openFolder(state.path, { quiet: true }); }
  catch (e) { fail(e); }
}
$('#bulkDelete').onclick = () => del([...state.sel]);
$('#newFolderBtn').onclick = async () => {
  const top = state.path === '/';
  const name = await ask({ title: top ? 'สร้างโฟลเดอร์ฝ่ายใหม่' : 'โฟลเดอร์ใหม่', label: 'ชื่อโฟลเดอร์', ok: 'สร้าง',
    desc: top ? 'โฟลเดอร์ที่หน้าแรกคือโฟลเดอร์ของฝ่าย จากนั้นไปที่ “จัดการผู้ใช้” เพื่อเลือกว่าใครเข้าได้' : '' });
  if (!name) return;
  try { await api('/api/mkdir', { method: 'POST', json: { path: state.path, name } }); toast(`สร้างโฟลเดอร์ “${name}” แล้ว`, 'ok'); if (top) loadDepts(); openFolder(state.path, { quiet: true }); }
  catch (e) { fail(e); }
};

// ------------------------------------------------------------------ upload (button + drag & drop)
$('#uploadBtn').onclick = () => $('#fileInput').click();
$('#fileInput').onchange = (e) => { upload([...e.target.files]); e.target.value = ''; };
let dragDepth = 0;
const dragHasFiles = (e) => [...(e.dataTransfer?.types || [])].includes('Files');
window.addEventListener('dragenter', (e) => {
  if (!dragHasFiles(e) || state.view !== 'files') return;
  e.preventDefault(); dragDepth++;
  $('#dropTarget').textContent = state.canWrite ? 'ไปที่ ' + (state.path === '/' ? 'หน้าแรก' : state.path) : 'โฟลเดอร์นี้อัปโหลดไม่ได้';
  $('#dropOverlay').hidden = false;
});
window.addEventListener('dragover', (e) => { if (dragHasFiles(e)) e.preventDefault(); });
window.addEventListener('dragleave', () => { if (--dragDepth <= 0) { dragDepth = 0; $('#dropOverlay').hidden = true; } });
window.addEventListener('drop', (e) => {
  if (!dragHasFiles(e)) return;
  e.preventDefault(); dragDepth = 0; $('#dropOverlay').hidden = true;
  if (state.view !== 'files') return;
  if (!state.canWrite) return toast(state.path === '/' ? 'เปิดโฟลเดอร์ของฝ่ายก่อน แล้วจึงอัปโหลด' : 'คุณไม่มีสิทธิ์อัปโหลดในโฟลเดอร์นี้', 'warn');
  upload([...e.dataTransfer.files]);
});
$('#uploadsClose').onclick = () => { $('#uploads').hidden = true; $('#uploadList').innerHTML = ''; };

async function upload(files) {
  if (!files.length) return;
  const folder = state.path;
  $('#uploads').hidden = false;
  let ok = 0;
  for (const f of files) {
    const row = document.createElement('div');
    row.className = 'up';
    row.innerHTML = `<div class="t"><span>${esc(f.name)}</span><span class="pct">0%</span></div><div class="track"><div class="fill"></div></div>`;
    $('#uploadList').prepend(row);
    const exists = state.entries.some((e) => e.name === f.name);
    let overwrite = false;
    if (exists) {
      overwrite = await confirmBox({ title: `มีไฟล์ “${f.name}” อยู่แล้ว`, desc: 'ต้องการแทนที่ไฟล์เดิมหรือไม่?', ok: 'แทนที่' });
      if (!overwrite) { row.classList.add('fail'); $('.pct', row).textContent = 'ข้าม'; continue; }
    }
    try {
      await new Promise((resolve, reject) => {
        const x = new XMLHttpRequest();
        x.open('PUT', `/api/upload?path=${encodeURIComponent(join(folder, f.name))}${overwrite ? '&overwrite=1' : ''}`);
        x.setRequestHeader('X-LANShare', '1');
        x.upload.onprogress = (ev) => { if (ev.lengthComputable) { const p = Math.round(ev.loaded / ev.total * 100); $('.fill', row).style.width = p + '%'; $('.pct', row).textContent = p + '%'; } };
        x.onload = () => { let d = {}; try { d = JSON.parse(x.responseText); } catch (e) {} x.status < 300 ? resolve() : reject(new Error(d.error || 'อัปโหลดไม่สำเร็จ')); };
        x.onerror = () => reject(new Error('การเชื่อมต่อขาดหาย'));
        x.send(f);
      });
      row.classList.add('done'); $('.fill', row).style.width = '100%'; $('.pct', row).textContent = 'เสร็จ'; ok++;
    } catch (e) { row.classList.add('fail'); $('.pct', row).textContent = 'ไม่สำเร็จ'; fail(new Error(`${f.name}: ${e.message}`)); }
  }
  if (ok) toast(`อัปโหลดสำเร็จ ${ok} ไฟล์`, 'ok');
  if (state.path === folder) openFolder(folder, { quiet: true });
  setTimeout(() => { if (!$('#uploadList .up:not(.done):not(.fail)')) { $('#uploads').hidden = true; $('#uploadList').innerHTML = ''; } }, 4000);
}

// ------------------------------------------------------------------ real-time updates (Server-Sent Events)
function setLive(on) { $('#live').classList.toggle('on', on); $('#live').title = on ? 'อัปเดตอัตโนมัติ: เชื่อมต่อแล้ว' : 'อัปเดตอัตโนมัติ: กำลังเชื่อมต่อ…'; }
let refreshTimer;
function subscribe() {
  state.es?.close();
  const es = new EventSource('/api/events?path=' + encodeURIComponent(state.path));
  state.es = es;
  es.addEventListener('ready', () => setLive(true));
  es.addEventListener('change', () => {
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(() => { openFolder(state.path, { quiet: true }); if (state.path === '/') loadDepts(); }, 250);
  });
  es.onerror = () => setLive(false);
}

// ------------------------------------------------------------------ users
const ROLE_HELP = {
  superadmin: 'ทำได้ทุกอย่าง: จัดการไฟล์ทุกที่ (รวมหน้าแรก) เพิ่ม/แก้/ลบผู้ใช้ และเปลี่ยนบทบาท',
  editor: 'แก้ไขไฟล์และโฟลเดอร์ได้ทุกที่ (รวมหน้าแรก) เพิ่มผู้ใช้และรีเซ็ตรหัสได้ แต่ลบผู้ใช้ไม่ได้',
  user: 'ใช้ไฟล์ได้เฉพาะโฟลเดอร์ฝ่ายที่ได้รับสิทธิ์ สร้างอะไรที่หน้าแรกไม่ได้',
};
async function loadUsers() {
  $('#roleLegend').innerHTML = Object.keys(ROLE).map((r) => `<div><b><span class="badge ${r}">${ROLE[r]}</span></b>${ROLE_HELP[r]}</div>`).join('');
  try {
    const { users } = await api('/api/users');
    const me = state.me;
    $('#usersTable').innerHTML = `<thead><tr><th>ผู้ใช้</th><th>บทบาท</th><th>โฟลเดอร์ที่เข้าได้</th><th>สถานะ</th><th></th></tr></thead><tbody>` +
      users.map((u) => {
        const canEdit = me.role === 'superadmin' || u.role === 'user';
        const depts = u.role === 'user' ? (u.depts.length ? u.depts.map((d) => `<span class="chip">${esc(d)}</span>`).join('') : '<span class="muted">ยังไม่มี</span>') : '<span class="muted">ทุกโฟลเดอร์</span>';
        return `<tr><td><div class="who"><span class="avatar">${esc((u.display_name || u.username).charAt(0).toUpperCase())}</span><div><b>${esc(u.display_name)}</b><small>${esc(u.username)}</small></div></div></td>
          <td><span class="badge ${u.role}">${ROLE[u.role]}</span></td><td><div class="chips">${depts}</div></td>
          <td>${u.active ? '<span class="badge user">ใช้งาน</span>' : '<span class="badge off">ปิดใช้งาน</span>'}</td>
          <td class="actions">${canEdit ? `<button class="btn ghost" data-edit="${u.id}">${icon('edit')}แก้ไข</button>` : ''}
          ${me.role === 'superadmin' && u.id !== me.id ? `<button class="icon-btn" data-del="${u.id}" aria-label="ลบผู้ใช้">${icon('trash')}</button>` : ''}</td></tr>`;
      }).join('') + '</tbody>';
    $$('[data-edit]').forEach((b) => (b.onclick = () => userForm(users.find((u) => u.id == b.dataset.edit))));
    $$('[data-del]').forEach((b) => (b.onclick = async () => {
      const u = users.find((x) => x.id == b.dataset.del);
      if (!(await confirmBox({ title: `ลบผู้ใช้ “${u.username}”?`, desc: 'บัญชีจะถูกลบถาวร ไฟล์ที่ผู้ใช้นี้อัปโหลดไว้ยังอยู่ครบ', ok: 'ลบผู้ใช้', danger: true }))) return;
      try { await api(`/api/users/${u.id}/delete`, { method: 'POST' }); toast(`ลบผู้ใช้ ${u.username} แล้ว`, 'ok'); loadUsers(); } catch (e) { fail(e); }
    }));
  } catch (e) { fail(e); }
}
$('#addUserBtn').onclick = () => userForm(null);
async function userForm(u) {
  await loadDepts();
  const me = state.me, isNew = !u;
  const roles = me.role === 'superadmin' ? Object.keys(ROLE) : ['user'];
  const { m, close } = modal(`<form><h3>${isNew ? 'เพิ่มผู้ใช้' : 'แก้ไขผู้ใช้ ' + esc(u.username)}</h3>
    <p class="desc">${isNew ? 'ตั้งรหัสผ่านชั่วคราว แล้วแจ้งผู้ใช้ให้เปลี่ยนเองหลังเข้าสู่ระบบ' : 'เว้นช่องรหัสผ่านว่างไว้ถ้าไม่ต้องการเปลี่ยน'}</p>
    ${isNew ? `<label class="field"><span>ชื่อผู้ใช้ (ใช้เข้าสู่ระบบ)</span><input name="username" required autofocus pattern="[a-z0-9][a-z0-9._-]{1,31}" placeholder="เช่น somchai"><small>a-z, 0-9, จุด, ขีด · ยาว 2–32 ตัว</small></label>` : ''}
    <label class="field"><span>ชื่อที่แสดง</span><input name="display" value="${esc(u?.display_name || '')}" placeholder="เช่น สมชาย ใจดี" ${isNew ? '' : 'autofocus'}></label>
    <label class="field"><span>${isNew ? 'รหัสผ่าน' : 'ตั้งรหัสผ่านใหม่'}</span><input name="password" type="password" autocomplete="new-password" minlength="8" ${isNew ? 'required' : ''}><small>อย่างน้อย 8 ตัว</small></label>
    <label class="field"><span>บทบาท</span><select name="role" ${roles.length === 1 ? 'disabled' : ''}>${roles.map((r) => `<option value="${r}" ${(u?.role || 'user') === r ? 'selected' : ''}>${ROLE[r]}</option>`).join('')}</select><small id="roleHelp"></small></label>
    <div class="field" id="deptBox"><span>โฟลเดอร์ที่เข้าได้</span><div class="checks">${state.depts.map((d) => `<label><input type="checkbox" name="dept" value="${esc(d)}" ${u?.depts.includes(d) ? 'checked' : ''}>${esc(d)}</label>`).join('') || '<span class="muted">ยังไม่มีโฟลเดอร์ฝ่าย</span>'}</div></div>
    ${!isNew && u.id !== me.id ? `<label class="field"><span>สถานะ</span><select name="active"><option value="1" ${u.active ? 'selected' : ''}>ใช้งาน</option><option value="0" ${u.active ? '' : 'selected'}>ปิดใช้งาน</option></select></label>` : ''}
    <div class="actions"><button type="button" class="btn" data-close>ยกเลิก</button><button class="btn primary">${isNew ? 'เพิ่มผู้ใช้' : 'บันทึก'}</button></div></form>`);
  const f = $('form', m);
  const syncRole = () => { $('#roleHelp', m).textContent = ROLE_HELP[f.role.value]; $('#deptBox', m).hidden = f.role.value !== 'user'; };
  f.role.onchange = syncRole; syncRole();
  f.onsubmit = async (e) => {
    e.preventDefault();
    const body = { display_name: f.display.value, depts: $$('input[name=dept]:checked', m).map((i) => i.value) };
    if (f.password.value) body.password = f.password.value;
    if (!f.role.disabled) body.role = f.role.value;
    if (f.active) body.active = f.active.value === '1';
    try {
      if (isNew) await api('/api/users', { method: 'POST', json: { ...body, username: f.username.value.trim().toLowerCase(), role: f.role.value } });
      else await api(`/api/users/${u.id}/update`, { method: 'POST', json: body });
      close(); toast(isNew ? 'เพิ่มผู้ใช้แล้ว' : 'บันทึกแล้ว', 'ok'); loadUsers();
    } catch (err) { fail(err); }
  };
}

// ------------------------------------------------------------------ recycle bin
async function loadRecycle() {
  try {
    const { items } = await api('/api/recycle');
    $('#recycleTable').innerHTML = items.length ? `<thead><tr><th>รายการ</th><th>ลบโดย</th><th>เมื่อ</th><th></th></tr></thead><tbody>` +
      items.map((it) => `<tr><td><div class="who"><span class="ficon ${it.type === 'dir' ? 'dir' : kind(it.path)[0]}">${icon(it.type === 'dir' ? 'folder' : kind(it.path)[1])}</span><div><b>${esc(parts(it.path).pop())}</b><small>${esc(it.path)}</small></div></div></td>
        <td>${esc(it.by)}</td><td class="muted">${fmtDate(it.ts)}</td>
        <td class="actions"><button class="btn ghost" data-restore="${it.id}">${icon('recycle')}กู้คืน</button>
        ${state.me.role === 'superadmin' ? `<button class="icon-btn" data-purge="${it.id}" aria-label="ลบถาวร">${icon('trash')}</button>` : ''}</td></tr>`).join('') + '</tbody>'
      : `<tbody><tr><td><div class="empty"><div class="big">${icon('recycle')}</div><b>ถังขยะว่าง</b>ไฟล์ที่ถูกลบจะมาอยู่ที่นี่</div></td></tr></tbody>`;
    $$('[data-restore]').forEach((b) => (b.onclick = async () => {
      try { const r = await api('/api/recycle/restore', { method: 'POST', json: { id: b.dataset.restore } }); toast(`กู้คืนไปที่ ${r.path} แล้ว`, 'ok'); loadRecycle(); loadDepts(); } catch (e) { fail(e); }
    }));
    $$('[data-purge]').forEach((b) => (b.onclick = async () => {
      if (!(await confirmBox({ title: 'ลบถาวร?', desc: 'ลบแล้วกู้คืนไม่ได้อีก', ok: 'ลบถาวร', danger: true }))) return;
      try { await api('/api/recycle/purge', { method: 'POST', json: { id: b.dataset.purge } }); toast('ลบถาวรแล้ว', 'ok'); loadRecycle(); } catch (e) { fail(e); }
    }));
  } catch (e) { fail(e); }
}

// ------------------------------------------------------------------ network settings (super admin)
const IPV4 = '(25[0-5]|2[0-4]\\d|1?\\d?\\d)(\\.(25[0-5]|2[0-4]\\d|1?\\d?\\d)){3}';
const mask = (n) => [0, 1, 2, 3].map((i) => 256 - 2 ** (8 - Math.max(0, Math.min(8, n - 8 * i)))).map((v) => (v === 256 ? 0 : v)).join('.');
const netUrls = (host) => { const p = state.net.port; return `http://${host}${p == 80 ? '' : ':' + p}`; };

async function loadNetwork() {
  try { state.net = await api('/api/net'); } catch (e) { $('#netBody').innerHTML = ''; return fail(e); }
  renderNet();
}
async function saveNet(patch) { state.net = await api('/api/net/save', { method: 'POST', json: patch }); renderNet(); }

function renderNet() {
  const st = state.net, seen = state.netSeen || {};
  $('#addNetBtn').hidden = !st.available;
  if (!st.available) {
    $('#netBody').innerHTML = `<div class="card net-off">${icon('info')}<div><b>ยังไม่ได้ติดตั้งตัวช่วยเครือข่ายบนเครื่องหลัก</b>
      <p class="muted">หน้านี้ใช้เปลี่ยน IP ของเซิร์ฟเวอร์และเปิดชื่อ <code>fileserver.local</code> ต้องติดตั้งบนเครื่อง Proxmox ก่อนด้วยคำสั่ง <code>bash deploy/netd-install.sh</code></p></div></div>`;
    return;
  }
  const ip = st.current.ip || (st.current.address || '').split('/')[0];
  const byName = netUrls(`${st.name}.local`), byIp = netUrls(ip);
  $('#netBody').innerHTML = `
    <div class="card net-hero">
      <div class="net-now">
        <span class="muted">เปิดหน้าเว็บนี้ได้ที่</span>
        <a class="net-url" href="${esc(byName)}">${esc(byName)}</a>
        <span class="muted">หรือ <a href="${esc(byIp)}">${esc(byIp)}</a> · ไดรฟ์ Windows <code>\\\\${esc(st.name)}.local</code></span>
      </div>
      <div class="net-side">
        <label class="switch"><input type="checkbox" id="netAuto" ${st.auto ? 'checked' : ''}><i></i><span><b>เลือกเครือข่ายให้อัตโนมัติ</b><small>ทุกครั้งที่เปิดเครื่อง จะหาเครือข่ายที่บันทึกไว้ซึ่งตรงกับที่อยู่ปัจจุบันให้เอง</small></span></label>
        <div class="net-btns">
          <button class="btn ghost" id="netNameBtn">${icon('edit')}เปลี่ยนชื่อเครื่อง</button>
          <button class="btn ghost" id="netProbeBtn">${icon('search')}ตรวจหาเครือข่าย</button>
        </div>
      </div>
    </div>
    <div class="card table-wrap"><table class="table">
      <thead><tr><th>เครือข่าย</th><th>IP ของเซิร์ฟเวอร์</th><th>Gateway (เราเตอร์)</th><th></th></tr></thead><tbody>
      ${st.profiles.map((p) => `<tr><td><div class="who"><span class="ficon net ${p.id === st.active ? 'on' : ''}">${icon('wifi')}</span><div><b>${esc(p.label)}</b>
          <small>${p.id === st.active ? '<span class="badge superadmin">ใช้อยู่ตอนนี้</span>' : ''} ${seen[p.id] ? '<span class="badge ok">พบเครือข่ายนี้</span>' : ''} ${st.applying === p.id ? '<span class="badge editor">กำลังเปลี่ยน…</span>' : ''}</small></div></div></td>
        <td><code>${esc(p.address.split('/')[0])}</code> <span class="muted">/${esc(p.address.split('/')[1])}</span></td><td><code>${esc(p.gateway)}</code></td>
        <td class="actions">${p.id !== st.active ? `<button class="btn ghost" data-use="${p.id}">${icon('check')}ใช้ตอนนี้</button>` : ''}
          <button class="btn ghost" data-nedit="${p.id}">${icon('edit')}แก้ไข</button>
          <button class="icon-btn" data-ndel="${p.id}" aria-label="ลบเครือข่าย">${icon('trash')}</button></td></tr>`).join('')
        || `<tr><td colspan="4"><div class="empty"><div class="big">${icon('wifi')}</div><b>ยังไม่มีเครือข่ายที่บันทึกไว้</b>กด “เพิ่มเครือข่าย” เพื่อเริ่ม</div></td></tr>`}
      </tbody></table></div>
    <div class="net-tips">
      <div>${icon('info')}<p><b>ก่อนย้ายเครื่องไปที่อื่น</b> เพิ่มเครือข่ายปลายทางไว้ล่วงหน้า (เช่น “ที่บ้าน”) แล้วเปิด “เลือกเครือข่ายให้อัตโนมัติ” ไว้ ไปเปิดเครื่องที่ไหน เซิร์ฟเวอร์จะเลือกเครือข่ายที่ตรงกันเอง</p></div>
      <div>${icon('search')}<p><b>หาค่า Gateway</b> ใช้คอม Windows ที่ต่อ Wi-Fi วงนั้น เปิด Command Prompt พิมพ์ <code>ipconfig</code> ดูบรรทัด “Default Gateway”</p></div>
      <div>${icon('shield')}<p><b>ถ้าเข้าหน้าเว็บไม่ได้</b> เปิดหน้าจอ VMware ของ Proxmox ล็อกอิน root แล้วพิมพ์ <code>lanshare-net list</code> และ <code>lanshare-net use ชื่อเครือข่าย</code></p></div>
    </div>`;
  $('#netAuto').onchange = async (e) => {
    try { await saveNet({ auto: e.target.checked }); toast(e.target.checked ? 'เปิดการเลือกเครือข่ายอัตโนมัติแล้ว' : 'ปิดการเลือกเครือข่ายอัตโนมัติแล้ว', 'ok'); } catch (err) { fail(err); e.target.checked = !e.target.checked; }
  };
  $('#netNameBtn').onclick = async () => {
    const v = await ask({ title: 'ชื่อเครื่องในเครือข่าย', desc: 'ใช้เปิดหน้าเว็บด้วยชื่อแทนตัวเลข ใช้ได้ a-z, 0-9 และขีด (-)', label: 'ชื่อ (ต่อท้ายด้วย .local ให้เอง)', value: st.name, ok: 'บันทึก' });
    if (!v) return;
    try { await saveNet({ name: v.toLowerCase().replace(/\.local$/, '') }); toast(`ตั้งชื่อเป็น ${state.net.name}.local แล้ว`, 'ok'); } catch (err) { fail(err); }
  };
  $('#netProbeBtn').onclick = async (e) => {
    const b = e.currentTarget; b.disabled = true; b.lastChild.textContent = 'กำลังตรวจ…';
    try {
      state.netSeen = (await api('/api/net/probe', { method: 'POST' })).reachable;
      const n = Object.values(state.netSeen).filter(Boolean).length;
      toast(n ? `พบ ${n} เครือข่ายที่ใช้ได้ตอนนี้` : 'ไม่พบเครือข่ายที่บันทึกไว้ในที่นี้', n ? 'ok' : 'warn');
      renderNet();
    } catch (err) { fail(err); b.disabled = false; b.lastChild.textContent = 'ตรวจหาเครือข่าย'; }
  };
  $$('[data-use]').forEach((b) => (b.onclick = () => useNet(st.profiles.find((p) => p.id === b.dataset.use))));
  $$('[data-nedit]').forEach((b) => (b.onclick = () => netForm(st.profiles.find((p) => p.id === b.dataset.nedit))));
  $$('[data-ndel]').forEach((b) => (b.onclick = async () => {
    const p = st.profiles.find((x) => x.id === b.dataset.ndel);
    if (!(await confirmBox({ title: `ลบเครือข่าย “${p.label}”?`, desc: p.id === st.active ? 'นี่คือเครือข่ายที่ใช้อยู่ตอนนี้ การลบไม่เปลี่ยน IP ปัจจุบัน แค่ลบออกจากรายการ' : 'ลบออกจากรายการที่บันทึกไว้', ok: 'ลบ', danger: true }))) return;
    try { await saveNet({ profiles: st.profiles.filter((x) => x.id !== p.id) }); toast('ลบแล้ว', 'ok'); } catch (err) { fail(err); }
  }));
}
$('#addNetBtn').onclick = () => netForm(null);

function netForm(p) {
  const [addr, pre = '24'] = (p?.address || '').split('/');
  const prefixes = [...new Set([24, 23, 22, 21, 20, 16, 25, 26, 27, 28, 8, +pre])];
  const { m, close } = modal(`<form><h3>${p ? 'แก้ไขเครือข่าย' : 'เพิ่มเครือข่าย'}</h3>
    <p class="desc">ใส่ค่าของวง Wi-Fi/LAN ที่จะนำเซิร์ฟเวอร์ไปใช้ ค่าจะมีผลเมื่อกด “ใช้ตอนนี้” หรือเมื่อเปิดเครื่องในวงนั้น (ถ้าเปิดการเลือกอัตโนมัติ)</p>
    <label class="field"><span>ชื่อเรียก</span><input name="label" required maxlength="40" value="${esc(p?.label || '')}" placeholder="เช่น ที่บ้าน" autofocus></label>
    <label class="field"><span>Gateway (IP ของเราเตอร์)</span><input name="gateway" required inputmode="decimal" pattern="${IPV4}" value="${esc(p?.gateway || '')}" placeholder="เช่น 192.168.1.1"><small>ดูจาก ipconfig → Default Gateway บนคอมที่ต่อ Wi-Fi วงนั้น</small></label>
    <div class="field-row">
      <label class="field"><span>IP ของเซิร์ฟเวอร์</span><input name="ip" required inputmode="decimal" pattern="${IPV4}" value="${esc(addr || '')}" placeholder="เช่น 192.168.1.250"><small>เลขท้ายที่ไม่ชนกับเครื่องอื่น เช่น .250</small></label>
      <label class="field"><span>Subnet mask</span><select name="prefix">${prefixes.map((n) => `<option value="${n}" ${String(n) === pre ? 'selected' : ''}>${mask(n)}</option>`).join('')}</select><small>ส่วนใหญ่เป็น 255.255.255.0</small></label>
    </div>
    <div class="actions"><button type="button" class="btn" data-close>ยกเลิก</button><button class="btn primary">บันทึก</button></div></form>`);
  const f = $('form', m);
  f.gateway.addEventListener('change', () => {
    if (!f.ip.value && new RegExp(`^${IPV4}$`).test(f.gateway.value.trim())) f.ip.value = f.gateway.value.trim().replace(/\.\d+$/, '.250');
  });
  f.onsubmit = async (e) => {
    e.preventDefault();
    const item = { id: p?.id, label: f.label.value.trim(), address: `${f.ip.value.trim()}/${f.prefix.value}`, gateway: f.gateway.value.trim() };
    const list = p ? state.net.profiles.map((x) => (x.id === p.id ? item : x)) : [...state.net.profiles, item];
    try { await saveNet({ profiles: list }); close(); toast(p ? 'บันทึกแล้ว' : `เพิ่ม “${item.label}” แล้ว`, 'ok'); } catch (err) { fail(err); }
  };
}

async function useNet(p) {
  const ip = p.address.split('/')[0], byName = netUrls(`${state.net.name}.local`), byIp = netUrls(ip);
  if (!(await confirmBox({ title: `เปลี่ยนไปใช้ “${p.label}”?`,
    desc: `เซิร์ฟเวอร์จะเปลี่ยน IP เป็น ${ip} ทันที และหน้านี้จะหลุดการเชื่อมต่อ ถ้าตอนนี้ยังไม่ได้อยู่ในวง “${p.label}” จะเข้าไม่ได้จนกว่าจะย้ายเครื่องไปที่นั่น`, ok: 'เปลี่ยนเลย' }))) return;
  try { await api('/api/net/apply', { method: 'POST', json: { id: p.id } }); } catch (e) { return fail(e); }
  state.es?.close(); state.es = null;
  modal(`<h3>กำลังเปลี่ยนเป็น “${esc(p.label)}”</h3>
    <p class="desc">รอประมาณ 10 วินาที แล้วเปิดหน้าเว็บที่อยู่ใหม่</p>
    <p class="net-new"><a class="net-url" href="${esc(byName)}">${esc(byName)}</a><span class="muted">หรือ <a href="${esc(byIp)}">${esc(byIp)}</a></span></p>
    <div class="actions"><button class="btn primary" data-close>ตกลง</button></div>`);
}

// ------------------------------------------------------------------ chrome: popovers, nav drawer, look
function closePops() { $$('.pop').forEach((p) => (p.hidden = true)); }
$('#lookBtn').onclick = (e) => { e.stopPropagation(); const p = $('#lookPop'); const h = p.hidden; closePops(); p.hidden = !h; };
$('#userBtn').onclick = (e) => { e.stopPropagation(); const p = $('#userPop'); const h = p.hidden; closePops(); p.hidden = !h; };
document.addEventListener('click', (e) => { if (!e.target.closest('.pop')) closePops(); });
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') { closePops(); $('#ctx').hidden = true; } });
$$('#modeSeg button').forEach((b) => (b.onclick = () => look({ mode: b.dataset.mode })));
$$('#swatches button').forEach((b) => (b.onclick = () => look({ accent: b.dataset.accent })));
$$('#viewSeg button').forEach((b) => (b.onclick = () => { look({ layout: b.dataset.layout }); render(); }));
function closeNav() { $('#sidebar').classList.remove('open'); $('#scrim').hidden = true; }
$('#navToggle').onclick = () => { $('#sidebar').classList.add('open'); $('#scrim').hidden = false; };
$('#scrim').onclick = closeNav;

// ------------------------------------------------------------------ boot
look();
api('/api/me').then((r) => start(r.user)).catch(() => showLogin());
