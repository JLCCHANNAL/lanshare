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
// department badge: an icon picked from the name (or its initials) on a colour of its own
const DEPT_KINDS = [
  [/account|บัญชี|finance|การเงิน/i, 'calc', 152], [/sale|ขาย/i, 'chart', 28], [/\bhr\b|^hr|บุคคล|human|people/i, 'users', 268],
  [/common|กลาง|share|ทั่วไป|public|all/i, 'globe', 212], [/\bit\b|^it|ไอที|tech|computer/i, 'monitor', 192],
  [/market|การตลาด|pr\b/i, 'megaphone', 340], [/admin|ธุรการ|office|สำนักงาน/i, 'building', 222],
  [/product|ผลิต|factory|โรงงาน|engineer|วิศว/i, 'factory', 40], [/purchas|จัดซื้อ|procure/i, 'cart', 96], [/legal|กฎหมาย/i, 'scale', 0],
];
const DEPT_HUES = [212, 28, 152, 268, 340, 192, 40, 96, 0, 236];
function deptBadge(name, cls = '') {
  const hit = DEPT_KINDS.find(([re]) => re.test(name));
  let hue = hit ? hit[2] : 0;
  if (!hit) { let h = 0; for (const ch of name) h = (h * 31 + ch.codePointAt(0)) >>> 0; hue = DEPT_HUES[h % DEPT_HUES.length]; }
  const inner = hit ? icon(hit[1]) : `<b class="ini">${esc(monogram(name))}</b>`;
  return `<span class="dept ${cls} h${hue}">${inner}</span>`;
}
const previewable =(name) => /\.(png|jpe?g|gif|webp|bmp|pdf|txt)$/i.test(name);

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
      <label class="field"><span>${esc(label)}</span><input name="v" type="${type}" value="${esc(value)}" autocomplete="off" autofocus required></label>
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
  chat.es?.close(); chat.es = null; chat.convs = []; chat.conv = null;
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

$('#delMeBtn').onclick = () => {
  closePops();
  const me = state.me;
  const { m, close } = modal(`<form><h3>ลบบัญชีของฉัน</h3>
    <p class="desc">บัญชี <b>${esc(me.username)}</b> จะถูกลบถาวรและออกจากระบบทันที เข้าสู่ระบบด้วยบัญชีนี้ไม่ได้อีก ไฟล์ที่คุณอัปโหลดไว้ในโฟลเดอร์ของฝ่ายยังอยู่ครบ</p>
    <label class="field"><span>ยืนยันด้วยรหัสผ่านของคุณ</span><input name="pw" type="password" autocomplete="current-password" required autofocus></label>
    <div class="actions"><button type="button" class="btn" data-close>ยกเลิก</button><button class="btn danger solid">ลบบัญชีถาวร</button></div></form>`);
  $('form', m).onsubmit = async (e) => {
    e.preventDefault();
    try {
      await api('/api/me/delete', { method: 'POST', json: { password: e.target.pw.value } });
      close(); location.hash = ''; showLogin(); toast('ลบบัญชีของคุณแล้ว', 'ok');
    } catch (err) { fail(err); e.target.pw.select(); }
  };
};

// ------------------------------------------------------------------ organisation identity (name, tagline, monogram)
function monogram(name) {
  const words = name.trim().split(/\s+/).filter((w) => /^[A-Za-z0-9]/.test(w));
  if (words.length >= 2) return (words[0][0] + words[1][0]).toUpperCase();
  if (words.length === 1) return words[0].slice(0, 2).toUpperCase();
  return (name.match(/[ก-ฮ]/) || [name.trim()[0] || '•'])[0];   // Thai: first consonant
}
function paintOrg(o) {
  $$('.app-name').forEach((e) => (e.textContent = o.name));
  $$('.org-tag').forEach((e) => (e.textContent = o.tagline));
  // designed mark for names starting with J (Jlc Industry); other names get a clean letter mark
  const mark = /^\s*j/i.test(o.name) ? '<svg class="logo-mark"><use href="#i-logo-j"/></svg>' : `<b class="logo-ini">${esc(monogram(o.name).charAt(0))}</b><i class="logo-dot"></i>`;
  $$('[data-mono]').forEach((e) => (e.innerHTML = mark));
  document.title = o.name;
  state.org = o;
}
paintOrg({ name: $('.app-name').textContent, tagline: $('.org-tag').textContent });
$('#footYear').textContent = new Date().getFullYear();
$('#orgForm').onsubmit = async (e) => {
  e.preventDefault();
  const f = e.target;
  try { paintOrg(await api('/api/settings/org', { method: 'POST', json: { name: f.orgname.value, tagline: f.tagline.value } })); toast('บันทึกข้อมูลองค์กรแล้ว', 'ok'); }
  catch (err) { fail(err); }
};

// ------------------------------------------------------------------ profile picture
const initial = (u) => esc((u.display_name || u.username).trim().charAt(0).toUpperCase());
const avatarInner = (u) => (u.avatar ? `<img src="/api/avatar?id=${u.id}&v=${u.avatar}" alt="">` : initial(u));
function paintMe() {
  const u = state.me;
  $('#meName').textContent = u.display_name;
  $('#meRole').textContent = ROLE[u.role];
  $('#avatar').innerHTML = avatarInner(u);
}
function squareImage(file, size = 256) {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(file), img = new Image();
    img.onload = () => {
      const s = Math.min(img.naturalWidth, img.naturalHeight), c = document.createElement('canvas');
      c.width = c.height = size;
      const g = c.getContext('2d');
      g.fillStyle = '#fff'; g.fillRect(0, 0, size, size);
      g.imageSmoothingQuality = 'high';
      g.drawImage(img, (img.naturalWidth - s) / 2, (img.naturalHeight - s) / 2, s, s, 0, 0, size, size);
      URL.revokeObjectURL(url);
      c.toBlob((b) => (b ? resolve(b) : reject(new Error('แปลงรูปไม่สำเร็จ'))), 'image/jpeg', 0.88);
    };
    img.onerror = () => { URL.revokeObjectURL(url); reject(new Error('เปิดไฟล์รูปนี้ไม่ได้ ลองใช้ JPG หรือ PNG')); };
    img.src = url;
  });
}
$('#avatarBtn').onclick = () => {
  closePops();
  const { m } = modal(`<h3>รูปโปรไฟล์</h3><p class="desc">เลือกรูป JPG หรือ PNG ระบบจะตัดตรงกลางเป็นสี่เหลี่ยมจัตุรัสให้อัตโนมัติ</p>
    <div class="avatar-edit"><span class="avatar xl" id="avPrev"></span>
      <div class="av-side"><b id="avName"></b><small class="muted">แสดงที่มุมขวาบนและในหน้าจัดการผู้ใช้</small>
        <div class="av-btns"><button class="btn primary" id="avPick">${icon('upload')}เลือกรูป</button><button class="btn ghost danger" id="avDel">${icon('trash')}ลบรูป</button></div></div></div>
    <input type="file" id="avFile" accept="image/png,image/jpeg,image/webp,image/gif,image/bmp" hidden>
    <div class="actions"><button class="btn" data-close>เสร็จ</button></div>`);
  const paint = () => { $('#avPrev', m).innerHTML = avatarInner(state.me); $('#avName', m).textContent = state.me.display_name; $('#avDel', m).hidden = !state.me.avatar; paintMe(); };
  paint();
  $('#avPick', m).onclick = () => $('#avFile', m).click();
  $('#avFile', m).onchange = async (e) => {
    const f = e.target.files[0]; e.target.value = '';
    if (!f) return;
    if (f.size > 20 * 1024 * 1024) return toast('ไฟล์รูปใหญ่เกิน 20 MB', 'err');
    try {
      const blob = await squareImage(f);
      const r = await api('/api/me/avatar', { method: 'PUT', body: blob, headers: { 'Content-Type': 'image/jpeg' } });
      state.me = r.user; paint(); toast('เปลี่ยนรูปโปรไฟล์แล้ว', 'ok');
      if (state.view === 'users') loadUsers();
    } catch (err) { fail(err); }
  };
  $('#avDel', m).onclick = async () => {
    try { state.me = (await api('/api/me/avatar/delete', { method: 'POST' })).user; paint(); toast('ลบรูปโปรไฟล์แล้ว', 'ok'); if (state.view === 'users') loadUsers(); } catch (err) { fail(err); }
  };
};

async function start(user) {
  state.me = user;
  $('#login').hidden = true; $('#app').hidden = false;
  paintMe();
  $('#adminNav').hidden = user.role === 'user';
  $('#netNav').hidden = $('#actNav').hidden = $('#chatlogNav').hidden = user.role !== 'superadmin';
  await loadDepts();
  route();
  chatConnect();
  refreshConvs();
}

// ------------------------------------------------------------------ navigation
function route() {
  const h = decodeURIComponent(location.hash.slice(1));
  if (h === 'users' && state.me.role !== 'user') return showView('users');
  if (h === 'recycle' && state.me.role !== 'user') return showView('recycle');
  if (h === 'network' && state.me.role === 'superadmin') return showView('network');
  if (h === 'activity' && state.me.role === 'superadmin') return showView('activity');
  if (h === 'chatlog' && state.me.role === 'superadmin') return showView('chatlog');
  if (h === 'chat' || h.startsWith('chat/')) return showView('chat', h.slice(5) || null);
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
  $('#viewChat').hidden = view !== 'chat';
  $('#viewActivity').hidden = view !== 'activity';
  $('#viewChatlog').hidden = view !== 'chatlog';
  if (view === 'chatlog') { cl.conv = ''; loadChatlog(); }
  clearInterval(act.timer);
  if (view === 'activity') { loadActivity(); act.timer = setInterval(() => { if (!act.more && !document.hidden) loadActivity(); }, 15000); }
  $('#main').classList.toggle('is-chat', view === 'chat');
  if (view !== 'chat') { chat.conv = null; paintUnread(); }
  $('#search').parentElement.hidden = view !== 'files';
  closeNav();
  if (view === 'files') openFolder(path);
  else { state.es?.close(); state.es = null; setLive(chat.es?.readyState === 1); }
  if (view === 'users') loadUsers();
  if (view === 'recycle') loadRecycle();
  if (view === 'network') loadNetwork();
  if (view === 'chat') openChat(path);
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
    ? state.depts.map((d) => `<button class="nav-item" data-view="files" data-path="/${esc(d)}">${deptBadge(d, 'sm')}<span>${esc(d)}</span></button>`).join('')
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
  const head = `<div class="row head"><span></span><button data-sort="name">ชื่อ ${arrow('name')}</button><button data-sort="size" class="size">ขนาด ${arrow('size')}</button><button data-sort="mtime" class="date">แก้ไขล่าสุด ${arrow('mtime')}</button><span></span></div>`;
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
        <div class="name"><button data-open>${state.path === '/' && e.type === 'dir' ? deptBadge(e.name) : `<span class="ficon ${k}">${icon(ic)}${label && k !== 'img' ? `<b>${esc(label)}</b>` : ''}</span>`}<span>${esc(e.name)}</span></button></div>
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
$('#uploadBtn').onclick = (e) => {                          // small menu: files or a whole folder
  e.stopPropagation();
  const r = e.currentTarget.getBoundingClientRect(), ctx = $('#ctx');
  ctx.innerHTML = `<button data-up="file">${icon('file')}อัปโหลดไฟล์</button><button data-up="folder">${icon('folder')}อัปโหลดทั้งโฟลเดอร์</button>`;
  ctx.hidden = false;
  ctx.style.left = Math.max(8, r.right - ctx.offsetWidth) + 'px';
  ctx.style.top = r.bottom + 6 + 'px';
  $$('[data-up]', ctx).forEach((b) => (b.onclick = (ev) => { ev.stopPropagation(); ctx.hidden = true; $(b.dataset.up === 'folder' ? '#folderInput' : '#fileInput').click(); }));
};
document.addEventListener('click', (e) => { if (!e.target.closest('#ctx')) $('#ctx').hidden = true; });
$('#fileInput').onchange = (e) => { upload([...e.target.files]); e.target.value = ''; };
$('#folderInput').onchange = (e) => { upload([...e.target.files].map((f) => ({ file: f, rel: f.webkitRelativePath || f.name }))); e.target.value = ''; };
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
  if (state.view === 'chat') { const f = [...e.dataTransfer.files].find((x) => x.type.startsWith('image/')); if (f) sendImageDialog(f); else toast('ลากมาได้เฉพาะไฟล์รูปภาพ', 'warn'); return; }
  if (state.view !== 'files') return;
  if (!state.canWrite) return toast(state.path === '/' ? 'เปิดโฟลเดอร์ของฝ่ายก่อน แล้วจึงอัปโหลด' : 'คุณไม่มีสิทธิ์อัปโหลดในโฟลเดอร์นี้', 'warn');
  filesFromDrop(e.dataTransfer).then(upload).catch((err) => fail(err));
});
$('#uploadsClose').onclick = () => { $('#uploads').hidden = true; $('#uploadList').innerHTML = ''; };

// files to upload come as {file, rel}: rel = path inside the dropped/picked folder ("Report/2026/a.xlsx")
const JUNK = /(^|\/)(Thumbs\.db|desktop\.ini|\.DS_Store|~\$[^/]*)$/i;
async function filesFromDrop(dt) {
  const entries = [...(dt.items || [])].map((i) => (i.webkitGetAsEntry ? i.webkitGetAsEntry() : null)).filter(Boolean);
  if (!entries.length) return [...dt.files].map((f) => ({ file: f, rel: f.name }));
  const out = [];
  const readAll = (dir) => new Promise((resolve, reject) => {
    const reader = dir.createReader(), all = [];
    const next = () => reader.readEntries((batch) => { if (!batch.length) resolve(all); else { all.push(...batch); next(); } }, reject);
    next();
  });
  const walk = async (ent, prefix) => {
    if (ent.isFile) out.push({ file: await new Promise((res, rej) => ent.file(res, rej)), rel: prefix + ent.name });
    else if (ent.isDirectory) for (const child of await readAll(ent)) await walk(child, prefix + ent.name + '/');
  };
  for (const ent of entries) await walk(ent, '');
  return out;
}
async function upload(list) {
  let items = list.map((x) => (x instanceof File ? { file: x, rel: x.name } : x)).filter((x) => !JUNK.test(x.rel));
  if (!items.length) return toast('ไม่มีไฟล์ให้อัปโหลด (โฟลเดอร์ว่าง)', 'warn');
  const folder = state.path;
  // a folder that already exists here: ask once whether to merge into it
  const tops = [...new Set(items.filter((x) => x.rel.includes('/')).map((x) => x.rel.split('/')[0]))];
  const mergeOk = new Set();
  for (const top of tops) {
    if (!state.entries.some((e) => e.name === top)) continue;
    if (await confirmBox({ title: `มีโฟลเดอร์ “${top}” อยู่แล้ว`, desc: 'รวมไฟล์เข้าไปในโฟลเดอร์เดิมหรือไม่? ไฟล์ที่ชื่อซ้ำจะถูกแทนที่ด้วยไฟล์ใหม่', ok: 'รวมเข้าไป' })) mergeOk.add(top);
    else items = items.filter((x) => x.rel.split('/')[0] !== top);
  }
  $('#uploads').hidden = false;
  let ok = 0, bad = 0;
  for (const { file: f, rel } of items) {
    const nested = rel.includes('/');
    const row = document.createElement('div');
    row.className = 'up';
    row.innerHTML = `<div class="t"><span title="${esc(rel)}">${esc(rel)}</span><span class="pct">0%</span></div><div class="track"><div class="fill"></div></div>`;
    $('#uploadList').prepend(row);
    let overwrite = nested && mergeOk.has(rel.split('/')[0]);
    if (!nested && state.entries.some((e) => e.name === f.name)) {
      overwrite = await confirmBox({ title: `มีไฟล์ “${f.name}” อยู่แล้ว`, desc: 'ต้องการแทนที่ไฟล์เดิมหรือไม่?', ok: 'แทนที่' });
      if (!overwrite) { row.classList.add('fail'); $('.pct', row).textContent = 'ข้าม'; continue; }
    }
    try {
      await new Promise((resolve, reject) => {
        const x = new XMLHttpRequest();
        x.open('PUT', `/api/upload?path=${encodeURIComponent(join(folder, rel))}${overwrite ? '&overwrite=1' : ''}${nested ? '&mkdirs=1' : ''}`);
        x.setRequestHeader('X-LANShare', '1');
        x.upload.onprogress = (ev) => { if (ev.lengthComputable) { const p = Math.round(ev.loaded / ev.total * 100); $('.fill', row).style.width = p + '%'; $('.pct', row).textContent = p + '%'; } };
        x.onload = () => { let d = {}; try { d = JSON.parse(x.responseText); } catch (e) {} x.status < 300 ? resolve() : reject(new Error(d.error || 'อัปโหลดไม่สำเร็จ')); };
        x.onerror = () => reject(new Error(f.size === 0 && !f.type ? 'อ่านไฟล์นี้ไม่ได้ (ถ้าเป็นโฟลเดอร์ ให้ลากมาวาง หรือใช้ อัปโหลด → อัปโหลดทั้งโฟลเดอร์)' : 'การเชื่อมต่อกับเซิร์ฟเวอร์ขาดหาย กรุณาลองใหม่'));
        x.send(f);
      });
      row.classList.add('done'); $('.fill', row).style.width = '100%'; $('.pct', row).textContent = 'เสร็จ'; ok++;
    } catch (e) { row.classList.add('fail'); $('.pct', row).textContent = 'ไม่สำเร็จ'; bad++; if (bad <= 3) fail(new Error(`${rel}: ${e.message}`)); }
  }
  if (ok) toast(`อัปโหลดสำเร็จ ${ok} ไฟล์${bad ? ` · ไม่สำเร็จ ${bad} ไฟล์` : ''}`, bad ? 'warn' : 'ok');
  else if (bad > 3) toast(`อัปโหลดไม่สำเร็จ ${bad} ไฟล์`, 'err');
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
        return `<tr><td><div class="who"><span class="avatar">${avatarInner(u)}</span><div><b>${esc(u.display_name)}</b><small>ชื่อเข้าระบบ <code>${esc(u.username)}</code></small></div></div></td>
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
function newUserInfo(username, display) {
  const url = `${location.protocol}//${location.host}`;
  modal(`<h3>เพิ่มผู้ใช้แล้ว ✓</h3><p class="desc">ส่งข้อมูลนี้ให้ ${esc(display || username)} เพื่อเข้าสู่ระบบ</p>
    <div class="handoff">
      <div><span>เปิดหน้าเว็บ</span><code>${esc(url)}</code></div>
      <div><span>ชื่อผู้ใช้</span><code class="big">${esc(username)}</code></div>
      <div><span>รหัสผ่าน</span><b>รหัสที่คุณเพิ่งตั้ง</b></div>
    </div>
    <p class="readonly-note">${icon('info')}ต้องพิมพ์ <b>ชื่อผู้ใช้</b> ด้านบน (หรือชื่อที่แสดง) ด้วยคีย์บอร์ดภาษาอังกฤษ และแนะนำให้เปลี่ยนรหัสหลังเข้าครั้งแรก</p>
    <div class="actions"><button class="btn primary" data-close>เสร็จ</button></div>`);
}
async function userForm(u) {
  await loadDepts();
  const me = state.me, isNew = !u;
  const roles = me.role === 'superadmin' ? Object.keys(ROLE) : ['user'];
  const { m, close } = modal(`<form><h3>${isNew ? 'เพิ่มผู้ใช้' : 'แก้ไขผู้ใช้ ' + esc(u.username)}</h3>
    <p class="desc">${isNew ? 'ตั้งรหัสผ่านชั่วคราว แล้วแจ้งผู้ใช้ให้เปลี่ยนเองหลังเข้าสู่ระบบ' : 'เว้นช่องรหัสผ่านว่างไว้ถ้าไม่ต้องการเปลี่ยน'}</p>
    ${isNew ? `<label class="field"><span>ชื่อผู้ใช้ (ใช้เข้าสู่ระบบ)</span><input name="username" required autofocus autocomplete="off" autocapitalize="none" spellcheck="false" pattern="[a-z0-9][a-z0-9._-]{1,31}" placeholder="เช่น somchai"><small>a-z, 0-9, จุด, ขีด · ยาว 2–32 ตัว</small></label>` : ''}
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
      close(); loadUsers();
      if (isNew) newUserInfo(f.username.value.trim().toLowerCase(), f.display.value.trim()); else toast('บันทึกแล้ว', 'ok');
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
      : `<tbody><tr><td><div class="empty"><div class="big">${icon('bin')}</div><b>ถังขยะว่าง</b>ไฟล์ที่ถูกลบจะมาอยู่ที่นี่</div></td></tr></tbody>`;
    $$('[data-restore]').forEach((b) => (b.onclick = async () => {
      try { const r = await api('/api/recycle/restore', { method: 'POST', json: { id: b.dataset.restore } }); toast(`กู้คืนไปที่ ${r.path} แล้ว`, 'ok'); loadRecycle(); loadDepts(); } catch (e) { fail(e); }
    }));
    $$('[data-purge]').forEach((b) => (b.onclick = async () => {
      if (!(await confirmBox({ title: 'ลบถาวร?', desc: 'ลบแล้วกู้คืนไม่ได้อีก', ok: 'ลบถาวร', danger: true }))) return;
      try { await api('/api/recycle/purge', { method: 'POST', json: { id: b.dataset.purge } }); toast('ลบถาวรแล้ว', 'ok'); loadRecycle(); } catch (e) { fail(e); }
    }));
  } catch (e) { fail(e); }
}

// ------------------------------------------------------------------ chat (rooms + direct messages, live over SSE)
const chat = { convs: [], people: [], conv: null, msgs: [], more: false, es: null, online: new Set() };
const dmId = (pid) => `dm:${Math.min(pid, state.me.id)}-${Math.max(pid, state.me.id)}`;
const hhmm = (ts) => new Date(ts * 1000).toLocaleTimeString('th-TH', { hour: '2-digit', minute: '2-digit' });
function chatDay(ts) {
  const d = new Date(ts * 1000), t = new Date(); t.setHours(0, 0, 0, 0);
  const diff = Math.round((t - new Date(d.getFullYear(), d.getMonth(), d.getDate())) / 86400000);
  if (diff === 0) return 'วันนี้';
  if (diff === 1) return 'เมื่อวาน';
  return d.toLocaleDateString('th-TH', { weekday: 'short', day: 'numeric', month: 'short', year: d.getFullYear() === t.getFullYear() ? undefined : 'numeric' });
}
const shortWhen = (ts) => (chatDay(ts) === 'วันนี้' ? hhmm(ts) : chatDay(ts).replace(/^[^ ]+ /, ''));
const linkify = (html) => html.replace(/\bhttps?:\/\/[^\s<]+/g, (u) => `<a href="${u}" target="_blank" rel="noopener noreferrer">${u}</a>`);

function chatConnect() {
  chat.es?.close();
  const es = new EventSource('/api/chat/events');
  chat.es = es;
  es.addEventListener('msg', (e) => onChatMsg(JSON.parse(e.data)));
  es.addEventListener('presence', (e) => onPresence(JSON.parse(e.data)));
  es.addEventListener('react', (e) => onReact(JSON.parse(e.data)));
  es.onopen = () => { if (state.view !== 'files') setLive(true); };
  es.onerror = () => { if (state.view !== 'files') setLive(false); };
}
async function refreshConvs() {
  try { const r = await api('/api/chat/convs'); chat.convs = r.convs; chat.people = r.people; chat.online = new Set(r.online); } catch (e) { return; }
  paintUnread();
  if (state.view === 'chat') renderConvs();
}
function paintUnread() {
  const n = chat.convs.reduce((s, c) => s + (state.view === 'chat' && c.id === chat.conv ? 0 : c.unread), 0);
  $('#chatUnread').hidden = !n;
  $('#chatUnread').textContent = n > 99 ? '99+' : n;
  document.title = (n ? `(${n}) ` : '') + state.org.name;
}
function onChatMsg(m) {
  const mine = m.sender === state.me.id;
  const viewing = state.view === 'chat' && chat.conv === m.conv;
  const c = chat.convs.find((x) => x.id === m.conv);
  if (!c) refreshConvs();                                   // first message of a new private chat
  else { c.last = m; if (!mine && !(viewing && !document.hidden)) c.unread++; }
  if (viewing && !chat.msgs.some((x) => x.id === m.id)) { chat.msgs.push(m); renderMsgs(); if (!mine && !document.hidden) markRead(m.conv); }
  if (!mine && !viewing) toast(`💬 ${m.name}: ${m.image && !m.body ? '📷 ส่งรูปภาพ' : m.body.length > 90 ? m.body.slice(0, 90) + '…' : m.body}`, 'info');
  paintUnread();
  if (state.view === 'chat') renderConvs();
}
async function markRead(conv) {
  const c = chat.convs.find((x) => x.id === conv);
  if (c) c.unread = 0;
  paintUnread();
  try { await api('/api/chat/read', { method: 'POST', json: { c: conv } }); } catch (e) { /* ignore */ }
}
document.addEventListener('visibilitychange', () => { if (!document.hidden && state.view === 'chat' && chat.conv) markRead(chat.conv); });

// ---- presence: who has the app open right now (works on the LAN, no internet needed)
const isOnline = (id) => chat.online.has(id);
const withDot = (html, id) => (id ? `<span class="av-wrap">${html}<i class="dot ${isOnline(id) ? 'on' : ''}" title="${isOnline(id) ? 'ออนไลน์' : 'ออฟไลน์'}"></i></span>` : html);
function presenceText(id) {
  if (!id) return 'ข้อความส่วนตัว';
  if (isOnline(id)) return '● ออนไลน์';
  const p = chat.people.find((x) => x.id === id);
  return p?.seen ? `ใช้งานล่าสุด ${fmtDate(p.seen)}` : 'ออฟไลน์';
}
function roomOnline(c) {
  const ids = [state.me.id, ...chat.people.filter((p) => c.kind === 'all' || p.role !== 'user' || p.depts.includes(c.title)).map((p) => p.id)];
  return ids.filter((id) => id === state.me.id || isOnline(id)).length;
}
function onPresence(ev) {
  ev.online ? chat.online.add(ev.id) : chat.online.delete(ev.id);
  const p = chat.people.find((x) => x.id === ev.id);
  if (p && ev.seen) p.seen = ev.seen;
  if (state.view === 'chat') { renderConvs(); if (chat.conv) renderHead(); }
  const list = $('#pplList');
  if (list) $('#pplQ').dispatchEvent(new Event('input'));
}
function onlineStrip() {
  const on = chat.people.filter((p) => isOnline(p.id));
  return `<p class="conv-label">ออนไลน์ตอนนี้ · ${on.length + 1} คน</p>
    <div class="online-strip">${on.length ? on.map((p) => `<button class="online-person" data-pid="${p.id}" title="คุยกับ ${esc(p.display_name)}">${withDot(`<span class="avatar">${avatarInner(p)}</span>`, p.id)}<small>${esc(p.display_name.split(' ')[0])}</small></button>`).join('')
      : '<span class="muted conv-empty">ยังไม่มีคนอื่นออนไลน์</span>'}</div>`;
}

function convInfo(id) {
  let c = chat.convs.find((x) => x.id === id);
  if (!c && id && id.startsWith('dm:')) {
    const [a, b] = id.slice(3).split('-').map(Number), pid = a === state.me.id ? b : a;
    const p = chat.people.find((x) => x.id === pid);
    if (p) c = { id, kind: 'dm', title: p.display_name, peer: p, unread: 0, last: null };
  }
  return c;
}
const convAvatar = (c) => (c.kind === 'dm'
  ? withDot(`<span class="avatar">${c.peer ? avatarInner(c.peer) : '?'}</span>`, c.peer?.id)
  : c.kind === 'dept' ? deptBadge(c.title, 'round') : `<span class="avatar room ${c.kind}">${icon('users')}</span>`);

function renderConvs() {
  const list = [...chat.convs];
  const cur = convInfo(chat.conv);
  if (cur && !list.includes(cur)) list.push(cur);
  const item = (c) => {
    const who = !c.last ? '' : c.last.sender === state.me.id ? 'คุณ: ' : c.kind === 'dm' ? '' : c.last.name + ': ';
    return `<button class="conv ${c.id === chat.conv ? 'on' : ''}" data-conv="${esc(c.id)}">${convAvatar(c)}
      <span class="conv-body"><span class="conv-top"><b>${esc(c.title)}</b>${c.last ? `<small>${shortWhen(c.last.ts)}</small>` : ''}</span>
      <span class="conv-last">${c.last ? esc(who + (c.last.image ? '📷 ' + (c.last.body || 'รูปภาพ') : c.last.body)) : '<i>ยังไม่มีข้อความ</i>'}</span></span>
      ${c.unread && c.id !== chat.conv ? `<span class="pill">${c.unread}</span>` : ''}</button>`;
  };
  const rooms = list.filter((c) => c.kind !== 'dm'), dms = list.filter((c) => c.kind === 'dm');
  $('#convList').innerHTML = `${onlineStrip()}<p class="conv-label">ห้องแชท</p>${rooms.map(item).join('')}
    <p class="conv-label">ข้อความส่วนตัว</p>${dms.map(item).join('') || '<p class="muted conv-empty">กด “แชทใหม่” เพื่อคุยกับเพื่อนร่วมงานหรือผู้ดูแล</p>'}`;
  $$('#convList [data-conv]').forEach((b) => (b.onclick = () => { location.hash = 'chat/' + b.dataset.conv; }));
  $$('#convList [data-pid]').forEach((b) => (b.onclick = () => { location.hash = 'chat/' + dmId(+b.dataset.pid); }));
}
function renderHead() {
  const c = convInfo(chat.conv);
  const sub = c.kind === 'dm' ? presenceText(c.peer?.id) + ' · ผู้ดูแลระบบตรวจสอบได้' : `ออนไลน์ ${roomOnline(c)} คน · ${c.kind === 'all' ? 'ทุกคนในองค์กรเห็นห้องนี้' : `ทุกคนที่เข้าโฟลเดอร์ ${c.title} ได้`}`;
  $('#chatHead').innerHTML = `<button class="icon-btn chat-back" id="chatBack" aria-label="กลับ">${icon('back')}</button>${convAvatar(c)}
    <div class="chat-title"><b>${esc(c.title)}</b><small>${esc(sub)}</small></div>`;
  $('#chatBack').onclick = () => { location.hash = 'chat'; };
}
function renderMsgs(keepTop) {
  const box = $('#chatMsgs');
  const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 120;
  const stick = keepTop === 'stay' ? nearBottom : !keepTop && (nearBottom || keepTop === false);
  const oldH = box.scrollHeight, oldTop = box.scrollTop;
  const dm = convInfo(chat.conv)?.kind === 'dm';
  let html = chat.more ? `<div class="chat-more"><button class="btn ghost" id="chatMore">ดูข้อความก่อนหน้า</button></div>` : '';
  let day = '', prev = null;
  for (const m of chat.msgs) {
    const d = chatDay(m.ts);
    if (d !== day) { html += `<div class="chat-day"><span>${d}</span></div>`; day = d; prev = null; }
    const mine = m.sender === state.me.id;
    const cont = prev && prev.sender === m.sender && m.ts - prev.ts < 300;
    const av = mine ? '' : cont ? '<span class="avatar ghost"></span>'
      : `<span class="avatar">${m.gone ? '?' : avatarInner({ id: m.sender, avatar: m.avatar, display_name: m.name, username: m.name })}</span>`;
    html += `<div class="msg ${mine ? 'mine' : ''} ${cont ? 'cont' : ''} ${Object.keys(m.reactions || {}).length ? 'has-react' : ''}" data-mid="${m.id}">${av}<div class="bubble-wrap">
      ${!mine && !cont && !dm ? `<b class="who">${esc(m.name)}${m.gone ? ' <i>(ลบบัญชีแล้ว)</i>' : ''}</b>` : ''}
      <div class="bubble ${m.image ? 'has-img' : ''}">${m.image ? `<button class="chat-img" data-img="${m.id}" aria-label="ดูรูปขนาดเต็ม"><img src="/api/chat/image?id=${m.id}" alt="รูปภาพ" loading="lazy"></button>` : ''}${m.body ? `<span class="text">${linkify(esc(m.body))}</span>` : ''}<time title="${esc(new Date(m.ts * 1000).toLocaleString('th-TH'))}">${hhmm(m.ts)}</time></div>${reactsHtml(m)}<button class="react-btn" data-react="${m.id}" aria-label="แสดงความรู้สึก" title="แสดงความรู้สึก">${icon('smile')}</button></div></div>`;
    prev = m;
  }
  if (!chat.msgs.length) html += `<div class="empty"><div class="big">${icon('chat')}</div><b>เริ่มบทสนทนา</b>พิมพ์ข้อความด้านล่าง แล้วกด Enter</div>`;
  box.innerHTML = html;
  if (keepTop === 'stay') box.scrollTop = nearBottom ? box.scrollHeight : oldTop;
  else if (keepTop) box.scrollTop = box.scrollHeight - oldH + oldTop;
  else if (nearBottom || keepTop === false) box.scrollTop = box.scrollHeight;
  $('#chatMore')?.addEventListener('click', loadOlder);
  $$('#chatMsgs [data-react]').forEach((b) => (b.onclick = (e) => { e.stopPropagation(); reactPicker(b); }));
  $$('#chatMsgs [data-chip]').forEach((b) => (b.onclick = () => react(+b.dataset.chip, b.dataset.emoji)));
  $$('#chatMsgs [data-img]').forEach((b) => (b.onclick = () => viewChatImage(b.dataset.img)));
  $$('#chatMsgs .chat-img img').forEach((im) => im.addEventListener('load', () => { if (stick) box.scrollTop = box.scrollHeight; }, { once: true }));
}
async function loadOlder() {
  try {
    const r = await api(`/api/chat/messages?c=${encodeURIComponent(chat.conv)}&before=${chat.msgs[0].id}`);
    chat.msgs = [...r.messages, ...chat.msgs]; chat.more = r.more; renderMsgs(true);
  } catch (e) { fail(e); }
}
async function openChat(conv) {
  await refreshConvs();
  if (conv && !convInfo(conv)) { toast('ไม่พบห้องแชทนี้ หรือคุณไม่มีสิทธิ์', 'err'); conv = null; }
  if (!conv && window.innerWidth > 860) conv = convInfo(chat.conv) ? chat.conv : chat.convs[0]?.id;
  chat.conv = conv || null;
  $('.chat').classList.toggle('thread', !!conv);
  renderConvs();
  if (!conv) { $('#chatHead').innerHTML = ''; $('#chatMsgs').innerHTML = ''; return; }
  renderHead();
  chat.msgs = []; chat.more = false;
  $('#chatMsgs').innerHTML = '<p class="muted chat-loading">กำลังโหลด…</p>';
  try { const r = await api('/api/chat/messages?c=' + encodeURIComponent(conv)); chat.msgs = r.messages; chat.more = r.more; }
  catch (e) { fail(e); return; }
  renderMsgs(false);
  markRead(conv);
  if (window.innerWidth > 860) $('#chatText').focus();
}
const growChat = () => { const t = $('#chatText'); t.style.height = 'auto'; t.style.height = Math.min(t.scrollHeight, 160) + 'px'; t.style.overflowY = t.scrollHeight > 160 ? 'auto' : 'hidden'; };
$('#chatText').addEventListener('input', growChat);
$('#chatText').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); $('#chatForm').requestSubmit(); }
});
$('#chatForm').onsubmit = async (e) => {
  e.preventDefault();
  const t = $('#chatText'), body = t.value.trim();
  if (!body || !chat.conv) return;
  t.value = ''; growChat();
  try {
    const { message } = await api('/api/chat/send', { method: 'POST', json: { c: chat.conv, body } });
    addSent(message);
  } catch (err) { fail(err); t.value = body; growChat(); }
};
// ---- reactions (Messenger style): one per person per message, same one again removes it
const REACTIONS = ['👍', '❤️', '😆', '😮', '😢', '😡'];
const whoName = (id) => (id === state.me.id ? 'คุณ' : chat.people.find((p) => p.id === id)?.display_name || 'ผู้ใช้');
function reactsHtml(m) {
  const r = Object.entries(m.reactions || {}).sort((a, b) => b[1].length - a[1].length);
  if (!r.length) return '';
  return `<div class="reacts">${r.map(([e, ids]) => `<button class="react-chip ${ids.includes(state.me.id) ? 'on' : ''}" data-chip="${m.id}" data-emoji="${e}"
    title="${esc(ids.map(whoName).join(', '))}">${e}<b>${ids.length}</b></button>`).join('')}</div>`;
}
function reactPicker(btn) {
  const ctx = $('#ctx'), mid = +btn.dataset.react;
  const m = chat.msgs.find((x) => x.id === mid);
  const mineNow = Object.entries(m?.reactions || {}).find(([, ids]) => ids.includes(state.me.id))?.[0];
  ctx.innerHTML = `<div class="react-pick">${REACTIONS.map((e) => `<button class="${e === mineNow ? 'on' : ''}" data-e="${e}" aria-label="${e}">${e}</button>`).join('')}</div>`;
  ctx.classList.add('react-ctx');
  ctx.hidden = false;
  const r = btn.getBoundingClientRect();
  ctx.style.left = Math.min(window.innerWidth - ctx.offsetWidth - 8, Math.max(8, r.left + r.width / 2 - ctx.offsetWidth / 2)) + 'px';
  ctx.style.top = Math.max(8, r.top - ctx.offsetHeight - 6) + 'px';
  $$('[data-e]', ctx).forEach((b) => (b.onclick = (ev) => { ev.stopPropagation(); ctx.hidden = true; react(mid, b.dataset.e); }));
}
async function react(mid, emoji) {
  try { onReact(await api('/api/chat/react', { method: 'POST', json: { id: mid, emoji } })); } catch (e) { fail(e); }
}
function onReact(ev) {
  const m = chat.msgs.find((x) => x.id === ev.id);
  if (m && chat.conv === ev.conv) { m.reactions = ev.reactions; renderMsgs('stay'); }
  const viewing = state.view === 'chat' && chat.conv === ev.conv && !document.hidden;
  if (ev.owner === state.me.id && ev.by !== state.me.id && ev.emoji && !viewing) toast(`${ev.by_name} แสดงความรู้สึก ${ev.emoji} ต่อข้อความของคุณ`, 'info');
}
document.addEventListener('click', (e) => { if (!e.target.closest('#ctx')) { $('#ctx').classList.remove('react-ctx'); } });

function addSent(message) {
  if (!chat.msgs.some((x) => x.id === message.id)) { chat.msgs.push(message); renderMsgs(false); }
  const c = chat.convs.find((x) => x.id === chat.conv);
  if (c) { c.last = message; renderConvs(); } else refreshConvs();
}
// ---- pictures in chat: pick a file, paste from the clipboard, or drag one in
async function shrinkImage(file) {
  // keep files that are already small enough (GIFs keep their animation); otherwise resize to 1600 px JPEG — fast to make and to send
  const keep = file.type === 'image/gif' || (file.size <= 600 * 1024 && /^image\/(png|jpeg|webp)$/.test(file.type));
  if (keep) return file;
  let bmp;
  try { bmp = await createImageBitmap(file); } catch (e) { throw new Error('เปิดรูปนี้ไม่ได้ ลองใช้ JPG หรือ PNG'); }
  const scale = Math.min(1, 1600 / Math.max(bmp.width, bmp.height));
  const c = document.createElement('canvas');
  c.width = Math.round(bmp.width * scale); c.height = Math.round(bmp.height * scale);
  const g = c.getContext('2d');
  g.fillStyle = '#fff'; g.fillRect(0, 0, c.width, c.height);         // transparent PNGs get a white background in JPEG
  g.drawImage(bmp, 0, 0, c.width, c.height);
  bmp.close?.();
  const jpg = await new Promise((r) => c.toBlob(r, 'image/jpeg', 0.85));
  return jpg && jpg.size < file.size ? jpg : file;
}
function putWithProgress(url, body, onProgress) {
  return new Promise((resolve, reject) => {
    const x = new XMLHttpRequest();
    x.open('PUT', url);
    x.setRequestHeader('X-LANShare', '1');
    x.setRequestHeader('Content-Type', body.type || 'application/octet-stream');
    x.upload.onprogress = (ev) => { if (ev.lengthComputable) onProgress(ev.loaded / ev.total); };
    x.onload = () => { let d = {}; try { d = JSON.parse(x.responseText); } catch (e) { /* ignore */ } x.status < 300 ? resolve(d) : reject(new Error(d.error || `ส่งไม่สำเร็จ (${x.status})`)); };
    x.onerror = () => reject(new Error('การเชื่อมต่อกับเซิร์ฟเวอร์ขาดหาย กรุณาลองใหม่'));
    x.send(body);
  });
}
function sendImageDialog(file) {
  if (!chat.conv) return toast('เลือกห้องแชทก่อน แล้วค่อยส่งรูป', 'warn');
  if (!file || !file.type.startsWith('image/')) return toast('ส่งได้เฉพาะไฟล์รูปภาพ', 'err');
  if (file.size > 40 * 1024 * 1024) return toast('ไฟล์รูปใหญ่เกิน 40 MB', 'err');
  const url = URL.createObjectURL(file), conv = chat.conv;
  const { m, close } = modal(`<form><h3>ส่งรูปภาพ</h3><p class="desc">ถึง ${esc(convInfo(conv)?.title || '')}</p>
    <div class="img-preview"><img src="${url}" alt="ตัวอย่างรูป"></div>
    <p class="img-size muted" id="imgSize">กำลังเตรียมรูป…</p>
    <label class="field"><span>คำอธิบาย (ไม่ใส่ก็ได้)</span><input name="cap" maxlength="2000" value="${esc($('#chatText').value.trim())}" placeholder="พิมพ์ข้อความประกอบรูป" autofocus></label>
    <div class="send-progress" hidden><div class="track"><div class="fill"></div></div><b class="pct">0%</b></div>
    <div class="actions"><button type="button" class="btn" data-close>ยกเลิก</button><button class="btn primary">${icon('send')}<span>ส่งรูป</span></button></div></form>`);
  const ready = shrinkImage(file);                       // start right away, while the person types a caption
  ready.then((b) => { const s = $('#imgSize', m); if (s) s.textContent = b === file ? `ขนาด ${fmtSize(b.size)}` : `ขนาดที่จะส่ง ${fmtSize(b.size)} (ย่อจาก ${fmtSize(file.size)})`; })
    .catch(() => { const s = $('#imgSize', m); if (s) s.textContent = ''; });
  const f = $('form', m);
  f.onsubmit = async (e) => {
    e.preventDefault();
    const btn = $('.btn.primary', m), cap = f.cap.value.trim(), bar = $('.send-progress', m);
    const show = (p) => { $('.fill', bar).style.width = Math.round(p * 100) + '%'; $('.pct', bar).textContent = Math.round(p * 100) + '%'; btn.lastChild.textContent = `กำลังส่ง ${Math.round(p * 100)}%`; };
    btn.disabled = true; btn.lastChild.textContent = 'กำลังเตรียมรูป…'; bar.hidden = false; show(0);
    try {
      const blob = await ready;
      const r = await putWithProgress(`/api/chat/image?c=${encodeURIComponent(conv)}&caption=${encodeURIComponent(cap)}`, blob, show);
      show(1);
      if (cap && cap === $('#chatText').value.trim()) { $('#chatText').value = ''; growChat(); }
      close(); URL.revokeObjectURL(url);
      if (chat.conv === conv) addSent(r.message);
    } catch (err) { fail(err); btn.disabled = false; btn.lastChild.textContent = 'ส่งรูป'; bar.hidden = true; }
  };
}function viewChatImage(id) {
  modal(`<div class="preview"><img src="/api/chat/image?id=${encodeURIComponent(id)}" alt="รูปภาพ"></div>
    <div class="actions"><a class="btn" href="/api/chat/image?id=${encodeURIComponent(id)}" download="chat-image-${encodeURIComponent(id)}">${icon('download')}บันทึกรูป</a><button class="btn primary" data-close>ปิด</button></div>`, { wide: true });
}
$('#chatAttach').onclick = () => $('#chatFile').click();
$('#chatFile').onchange = (e) => { const f = e.target.files[0]; e.target.value = ''; if (f) sendImageDialog(f); };
document.addEventListener('paste', (e) => {
  if (state.view !== 'chat' || !chat.conv || !$('#modalRoot').hidden) return;
  const item = [...(e.clipboardData?.items || [])].find((i) => i.kind === 'file' && i.type.startsWith('image/'));
  if (!item) return;
  e.preventDefault();
  sendImageDialog(item.getAsFile());
});

$('#newChatBtn').onclick = () => {
  const { m, close } = modal(`<h3>แชทใหม่</h3><p class="desc">เลือกคนที่ต้องการคุยด้วย ข้อความส่วนตัวแสดงเฉพาะคุณสองคน (ผู้ดูแลระบบตรวจสอบได้ตามนโยบายองค์กร)</p>
    <label class="search people-search">${icon('search')}<input id="pplQ" placeholder="ค้นหาชื่อ" autofocus></label>
    <div class="people" id="pplList"></div><div class="actions"><button class="btn" data-close>ปิด</button></div>`);
  const paint = () => {
    const q = $('#pplQ', m).value.trim().toLowerCase();
    $('#pplList', m).innerHTML = chat.people.filter((p) => !q || `${p.display_name} ${p.username}`.toLowerCase().includes(q))
      .map((p) => `<button class="person" data-pid="${p.id}">${withDot(`<span class="avatar">${avatarInner(p)}</span>`, p.id)}<span><b>${esc(p.display_name)}</b><small>${presenceText(p.id)} · ${ROLE[p.role]}</small></span>${icon('chat')}</button>`).join('')
      || '<p class="muted">ไม่พบชื่อนี้</p>';
    $$('[data-pid]', m).forEach((b) => (b.onclick = () => { close(); location.hash = 'chat/' + dmId(+b.dataset.pid); }));
  };
  $('#pplQ', m).oninput = paint;
  paint();
};

// ------------------------------------------------------------------ activity log (super admin)
const ACT = {
  upload: ['อัปโหลด', 'upload', 'ok'], download: ['ดาวน์โหลด', 'download', ''], preview: ['เปิดดู', 'eye', ''],
  mkdir: ['สร้างโฟลเดอร์', 'plus-folder', 'ok'], rename: ['เปลี่ยนชื่อ', 'edit', ''], delete: ['ลบ', 'trash', 'bad'],
  restore: ['กู้คืน', 'recycle', 'ok'], purge: ['ลบถาวร', 'trash', 'bad'], 'smb-write': ['สร้าง/แก้ไขไฟล์', 'file', 'ok'],
  login: ['เข้าสู่ระบบ', 'key', ''], 'login-failed': ['เข้าระบบไม่สำเร็จ', 'alert', 'bad'], logout: ['ออกจากระบบ', 'logout', ''],
  'user-create': ['เพิ่มผู้ใช้', 'users', 'ok'], 'user-update': ['แก้ไขผู้ใช้', 'users', ''], 'user-delete': ['ลบผู้ใช้', 'users', 'bad'],
  'self-delete': ['ลบบัญชีตัวเอง', 'users', 'bad'], 'change-password': ['เปลี่ยนรหัสผ่าน', 'key', ''], avatar: ['เปลี่ยนรูปโปรไฟล์', 'image', ''],
  'org-settings': ['ตั้งค่าองค์กร', 'gear', ''], 'net-save': ['ตั้งค่าเครือข่าย', 'wifi', ''], 'net-apply': ['สลับเครือข่าย', 'wifi', ''],
  'net-dhcp': ['เครือข่ายอัตโนมัติ', 'wifi', ''], 'view-chatlog': ['เปิดดูบันทึกแชท', 'eye', ''],
};
const act = { items: [], more: false, timer: null };
function uaShort(ua) {
  const b = /Edg\//.test(ua) ? 'Edge' : /Chrome\//.test(ua) ? 'Chrome' : /Firefox\//.test(ua) ? 'Firefox' : /Safari\//.test(ua) ? 'Safari' : '';
  const o = /Android/.test(ua) ? 'Android' : /iPhone|iPad/.test(ua) ? 'iPhone/iPad' : /Windows/.test(ua) ? 'Windows' : /Mac OS/.test(ua) ? 'Mac' : /Linux/.test(ua) ? 'Linux' : '';
  return [b, o].filter(Boolean).join(' บน ') || ua;
}
function actParams(extra = {}) {
  const f = $('#actFilters'), p = new URLSearchParams();
  for (const k of ['q', 'user', 'group', 'source']) if (f[k].value) p.set(k, f[k].value);
  for (const [k, v] of Object.entries(extra)) p.set(k, v);
  return p.toString();
}
async function loadActivity(more = false) {
  try {
    const r = await api('/api/activity?' + actParams(more && act.items.length ? { before: act.items[act.items.length - 1].id } : {}));
    act.items = more ? [...act.items, ...r.items] : r.items;
    act.more = r.more;
    const sel = $('#actFilters').user, cur = sel.value;
    sel.innerHTML = '<option value="">ทุกคน</option>' + r.users.map((u) => `<option ${u === cur ? 'selected' : ''}>${esc(u)}</option>`).join('');
    renderActivity();
  } catch (e) { fail(e); }
}
function renderActivity() {
  $('#actTable').innerHTML = act.items.length
    ? `<thead><tr><th>เวลา</th><th>ผู้ใช้</th><th>การกระทำ</th><th>รายการ</th><th>ช่องทาง</th></tr></thead><tbody>` + act.items.map((a) => {
      const [label, ic, tone] = ACT[a.action] || [a.action, 'info', ''];
      const when = new Date(a.ts * 1000);
      const detail = a.action === 'login' ? uaShort(a.detail) : a.detail;
      return `<tr><td class="act-when" title="${esc(when.toLocaleString('th-TH'))}"><b>${when.toLocaleTimeString('th-TH', { hour: '2-digit', minute: '2-digit' })}</b><small>${chatDay(a.ts)}</small></td>
        <td><b>${esc(a.username)}</b></td><td><span class="act-badge ${tone}">${icon(ic)}${label}</span></td>
        <td class="act-detail">${esc(detail)}</td>
        <td><span class="src ${esc(a.source)}">${a.source === 'smb' ? 'ไดรฟ์ Windows' : 'หน้าเว็บ'}</span><small class="muted">${esc(a.ip)}</small></td></tr>`;
    }).join('') + '</tbody>'
    : `<tbody><tr><td><div class="empty"><div class="big">${icon('activity')}</div><b>ไม่พบกิจกรรม</b>ลองเปลี่ยนตัวกรองหรือคำค้นหา</div></td></tr></tbody>`;
  $('#actMore').hidden = !act.more;
}
let actDebounce;
$('#actFilters').oninput = () => { clearTimeout(actDebounce); actDebounce = setTimeout(() => loadActivity(), 300); };
$('#actFilters').onsubmit = (e) => e.preventDefault();
$('#actRefresh').onclick = () => loadActivity();
$('#actMore').onclick = () => loadActivity(true);
$('#actCsv').onclick = () => { location.href = '/api/activity/csv?' + actParams(); };

// ------------------------------------------------------------------ chat log (super admin)
const cl = { items: [], more: false, conv: '' };
const CL_KIND = { dm: ['ข้อความส่วนตัว', 'key'], all: ['ห้องทุกคน', 'users'], dept: ['ห้องฝ่าย', 'folder'] };
async function loadChatlog(more = false) {
  const f = $('#clFilters'), p = new URLSearchParams();
  if (f.user.value) p.set('user', f.user.value);
  if (f.kind.value) p.set('kind', f.kind.value);
  if (cl.conv) p.set('conv', cl.conv);
  if (more && cl.items.length) p.set('before', cl.items[cl.items.length - 1].id);
  try {
    const r = await api('/api/chatlog?' + p);
    cl.items = more ? [...cl.items, ...r.items] : r.items; cl.more = r.more;
    const cur = f.user.value;
    f.user.innerHTML = '<option value="">ทุกคน</option>' + r.users.map((u) => `<option value="${u.id}" ${String(u.id) === cur ? 'selected' : ''}>${esc(u.name)}</option>`).join('');
    $('#clTable').innerHTML = cl.items.length
      ? `<thead><tr><th>เวลา</th><th>ผู้ส่ง</th><th>ถึง</th><th>เนื้อหา</th></tr></thead><tbody>` + cl.items.map((it) => {
        const [kindLabel, ic] = CL_KIND[it.kind], when = new Date(it.ts * 1000);
        const what = `${it.image ? `<button class="cl-img" data-img="${it.id}" title="ดูรูป"><img src="/api/chat/image?id=${it.id}" alt="รูปภาพ" loading="lazy"></button>` : ''}${esc(it.body || '')}`;
        return `<tr><td class="act-when" title="${esc(when.toLocaleString('th-TH'))}"><b>${when.toLocaleTimeString('th-TH', { hour: '2-digit', minute: '2-digit' })}</b><small>${chatDay(it.ts)}</small></td>
          <td><b>${esc(it.from)}</b></td>
          <td><span class="act-badge ${it.kind === 'dm' ? 'bad' : ''}">${icon(ic)}${kindLabel}</span><button class="cl-to" data-conv="${esc(it.conv)}" title="ดูเฉพาะบทสนทนานี้">${it.kind === 'dm' ? 'ถึง ' : ''}${esc(it.to)}</button></td>
          <td class="act-detail">${what}</td></tr>`;
      }).join('') + '</tbody>'
      : `<tbody><tr><td><div class="empty"><div class="big">${icon('chat')}</div><b>ยังไม่มีข้อความ</b>ลองเปลี่ยนตัวกรอง</div></td></tr></tbody>`;
    $('#clMore').hidden = !cl.more;
    $('#clConv').hidden = !cl.conv;
    $('#clConv b').textContent = cl.conv ? (cl.items[0]?.kind === 'dm' ? `${cl.items[0].from} ↔ ${cl.items[0].to}` : cl.items[0]?.to || cl.conv) : '';
    $$('#clTable [data-conv]').forEach((b) => (b.onclick = () => { cl.conv = b.dataset.conv; loadChatlog(); }));
    $$('#clTable [data-img]').forEach((b) => (b.onclick = () => viewChatImage(b.dataset.img)));
  } catch (e) { fail(e); }
}
$('#clFilters').oninput = () => loadChatlog();
$('#clFilters').onsubmit = (e) => e.preventDefault();
$('#clRefresh').onclick = () => loadChatlog();
$('#clMore').onclick = () => loadChatlog(true);
$('#clConvClear').onclick = () => { cl.conv = ''; loadChatlog(); };

// ------------------------------------------------------------------ network settings (super admin)
const IPV4 = '(25[0-5]|2[0-4]\\d|1?\\d?\\d)(\\.(25[0-5]|2[0-4]\\d|1?\\d?\\d)){3}';
const mask = (n) => [0, 1, 2, 3].map((i) => 256 - 2 ** (8 - Math.max(0, Math.min(8, n - 8 * i)))).map((v) => (v === 256 ? 0 : v)).join('.');
const netUrls = (host) => { const p = state.net.port; return `http://${host}${p == 80 ? '' : ':' + p}`; };

async function loadNetwork() {
  const of = $('#orgForm');
  of.orgname.value = state.org.name; of.tagline.value = state.org.tagline;
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
    <div class="net-mode">
      <button class="card mode-card ${st.dhcp ? 'on' : ''}" data-mode="dhcp"><span class="ficon net ${st.dhcp ? 'on' : ''}">${icon('wifi')}</span>
        <span><b>อัตโนมัติ <em>แนะนำ</em></b><small>รับ IP จาก Wi-Fi/LAN ที่ต่ออยู่เอง ย้ายเครื่องไปที่ไหนก็ใช้ได้ทันที เปิดด้วยชื่อ ${esc(st.name)}.local</small></span>
        ${st.dhcp ? '<i class="badge superadmin">ใช้อยู่</i>' : ''}</button>
      <button class="card mode-card ${st.dhcp ? '' : 'on'}" data-mode="static"><span class="ficon net ${st.dhcp ? '' : 'on'}">${icon('gear')}</span>
        <span><b>กำหนด IP เอง</b><small>IP คงที่ตามเครือข่ายที่บันทึกไว้ด้านล่าง เหมาะกับคนที่ต้องจำตัวเลข IP</small></span>
        ${st.dhcp ? '' : '<i class="badge superadmin">ใช้อยู่</i>'}</button>
    </div>
    ${st.notice ? `<p class="readonly-note warn">${icon('alert')}${esc(st.notice)}</p>` : ''}
    <div class="card net-hero">
      <div class="net-now">
        <span class="muted">เปิดหน้าเว็บนี้ได้ที่</span>
        <a class="net-url" href="${esc(byName)}">${esc(byName)}</a>
        <span class="muted">หรือ <a href="${esc(byIp)}">${esc(byIp)}</a> · ไดรฟ์ Windows <code>\\\\${esc(st.name)}.local</code></span>
      </div>
      <div class="net-side">
        <label class="switch" ${st.dhcp ? 'hidden' : ''}><input type="checkbox" id="netAuto" ${st.auto ? 'checked' : ''}><i></i><span><b>สลับเครือข่ายที่บันทึกไว้ให้เอง</b><small>ทุกครั้งที่เปิดเครื่อง จะหาเครือข่ายที่บันทึกไว้ซึ่งตรงกับที่อยู่ปัจจุบันให้เอง</small></span></label>
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
  $$('.mode-card').forEach((b) => (b.onclick = async () => {
    if (b.dataset.mode === 'dhcp') {
      if (st.dhcp) return;
      const byName = netUrls(`${st.name}.local`);
      if (!(await confirmBox({ title: 'เปลี่ยนเป็นโหมดอัตโนมัติ?',
        desc: `เซิร์ฟเวอร์จะขอ IP จากเราเตอร์ของ Wi-Fi/LAN ที่ต่ออยู่ IP อาจเปลี่ยนจากเดิม หน้านี้จะหลุดสักครู่ แล้วเปิดใหม่ที่ ${byName} (ถ้าไม่ได้รับ IP ภายใน 45 วินาที ระบบจะกลับไปใช้ค่าเดิมเอง)`, ok: 'เปลี่ยนเป็นอัตโนมัติ' }))) return;
      try { await api('/api/net/dhcp', { method: 'POST' }); } catch (e) { return fail(e); }
      state.es?.close(); state.es = null;
      modal(`<h3>กำลังเปลี่ยนเป็นโหมดอัตโนมัติ</h3><p class="desc">รอประมาณ 20 วินาที แล้วเปิดหน้าเว็บที่</p>
        <p class="net-new"><a class="net-url" href="${esc(byName)}">${esc(byName)}</a><span class="muted">ดู IP ใหม่ได้ที่หน้านี้ หรือที่หน้าจอ VMware ของ Proxmox</span></p>
        <div class="actions"><button class="btn primary" data-close>ตกลง</button></div>`);
    } else if (st.dhcp) {
      if (st.profiles.length) toast('เลือกเครือข่ายในตารางด้านล่าง แล้วกด “ใช้ตอนนี้”', 'info');
      else netForm(null);
    }
  }));
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
