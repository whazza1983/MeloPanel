'use strict';

// ---------- Helpers ----------

const $ = (sel, root = document) => root.querySelector(sel);
const view = $('#view');

const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
}[c]));

// Names the server uses for system actors, shown in the panel language
// (the German values come from older versions and may still be stored)
const SYSTEM_NAMES = {
  'Schedule': 'sys.schedule', 'Zeitplan': 'sys.schedule',
  'deleted user': 'sys.deletedUser', 'gelöschter Benutzer': 'sys.deletedUser',
};
const who = (name) => (SYSTEM_NAMES[name] ? t(SYSTEM_NAMES[name]) : name);

function fmtTime(sec) {
  sec = Math.max(0, Math.floor(sec || 0));
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = String(sec % 60).padStart(2, '0');
  return h ? `${h}:${String(m).padStart(2, '0')}:${s}` : `${m}:${s}`;
}

const locale = () => (LANG === 'de' ? 'de-DE' : 'en-GB');

const coverUrl = (id, size = 300) => (id ? `/api/cover/${encodeURIComponent(id)}?size=${size}` : '');
const coverImg = (id, size, cls = '') => (id
  ? `<img class="${cls}" src="${coverUrl(id, size)}" loading="lazy" alt="">`
  : `<div class="ph ${cls}"></div>`);

// Colored tile for radio stations without their own image
function radioColors(name) {
  let hash = 0;
  for (const ch of String(name)) hash = (hash * 31 + ch.codePointAt(0)) >>> 0;
  const hue = hash % 360;
  return `--tile-a:hsl(${hue} 70% 45%);--tile-b:hsl(${(hue + 50) % 360} 75% 35%)`;
}

function radioInitials(name) {
  const words = String(name).replace(/[^\p{L}\p{N} ]/gu, ' ').trim().split(/\s+/).filter(Boolean);
  if (!words.length) return '📻';
  return (words.length === 1 ? words[0].slice(0, 3) : words.slice(0, 2).map((w) => w[0]).join('')).toUpperCase();
}

function radioTile(r, cls = '') {
  if (r.coverArt) return coverImg(r.coverArt, 300, cls);
  return `<div class="radio-tile ${cls}" style="${radioColors(r.name)}">${esc(cls ? radioInitials(r.name) : r.name)}</div>`;
}

function toast(message, isError = false) {
  const el = document.createElement('div');
  el.className = 'toast' + (isError ? ' error' : '');
  el.textContent = message;
  $('#toasts').appendChild(el);
  setTimeout(() => el.remove(), isError ? 6000 : 3000);
}

async function api(path, options = {}) {
  const opts = { ...options, headers: { 'X-Lang': LANG, ...(options.headers || {}) } };
  if (opts.body !== undefined && typeof opts.body !== 'string') {
    opts.body = JSON.stringify(opts.body);
    opts.headers['Content-Type'] = 'application/json';
  }
  const res = await fetch(path, opts);
  if (res.status === 401) {
    location.href = '/login';
    throw new Error(t('notLoggedIn'));
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    let msg = data.detail || t('errorStatus', { status: res.status });
    if (Array.isArray(msg)) msg = msg.map((d) => d.msg).join(', ');
    throw new Error(msg);
  }
  return data;
}

const post = (path, body = {}) => api(path, { method: 'POST', body });
const fail = (err) => toast(err.message, true);

// ---------- State ----------

let me = null;
let state = null;
let stateAt = 0;
let seeking = false;
let pageData = {};   // songs of the current view, for "Play all"

// ---------- Player bar ----------

function currentPosition() {
  if (!state || !state.current) return 0;
  const elapsed = state.paused ? 0 : (performance.now() - stateAt) / 1000;
  return Math.min(state.position + elapsed, state.current.duration || Infinity);
}

function renderPlayer() {
  if (!state) return;
  const t0 = state.current;
  const live = Boolean(t0 && t0.radio);
  $('#pb-title').textContent = t0 ? t0.title : t('pb.nothing');
  $('#pb-artist').textContent = !t0 ? ''
    : live ? (t0.nowPlaying || t('liveRadio'))
      : `${t0.artist}${t0.album ? ' · ' + t0.album : ''}`;
  const img = $('#pb-cover');
  const empty = $('#pb-cover-empty');
  if (t0 && t0.coverArt) {
    const src = coverUrl(t0.coverArt, 120);
    if (img.getAttribute('src') !== src) img.src = src;
    img.hidden = false;
    empty.hidden = true;
  } else {
    img.hidden = true;
    empty.hidden = false;
    if (live) {
      empty.className = 'radio-tile player';
      empty.setAttribute('style', radioColors(t0.title));
      empty.textContent = radioInitials(t0.title);
    } else {
      empty.className = 'pb-cover pb-cover-empty';
      empty.removeAttribute('style');
      empty.textContent = '';
    }
  }
  $('.pb-progress').classList.toggle('is-live', live);
  $('#pb-live').hidden = !live;

  $('#play-icon').innerHTML = (t0 && !state.paused)
    ? '<path d="M6 5h4v14H6zm8 0h4v14h-4z"/>'
    : '<path d="M8 5v14l11-7z"/>';

  const loopBtn = $('#loop-btn');
  loopBtn.classList.toggle('active', state.loop !== 'off');
  $('#loop-one').hidden = state.loop !== 'track';
  loopBtn.title = t(`pb.loop.${state.loop}`);

  if (document.activeElement !== $('#volume')) $('#volume').value = state.volume;
  $('#pb-seek').max = t0 ? t0.duration : 0;
  $('#pb-seek').disabled = !t0 || live;
  $('#pb-dur').textContent = fmtTime(t0 ? t0.duration : 0);
  tickProgress();

  const prog = state.program;
  const progEl = $('#pb-program');
  progEl.hidden = !prog;
  if (prog) {
    progEl.textContent = prog.type === 'radio'
      ? t('pb.programRadio', { name: prog.name })
      : `🔁 ${prog.name}${prog.index ? ` · ${prog.index}/${prog.total}` : ''}${prog.shuffle ? ` · ${t('pb.shuffled')}` : ''}`;
  }

  $('#queue-count').textContent = state.queue.length;
  $('#voice-dot').classList.toggle('on', state.connected);
  if (state.channel) $('#channel-select').value = state.channel.id;
}

function tickProgress() {
  if (seeking) return;
  const pos = currentPosition();
  $('#pb-seek').value = pos;
  $('#pb-pos').textContent = fmtTime(pos);
}
setInterval(tickProgress, 500);

function applyState(newState) {
  state = newState;
  stateAt = performance.now();
  renderPlayer();
  if (route().name === 'queue') renderQueue();
  markCurrent();
}

// ---------- Live connection ----------

function connectSocket() {
  const ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws`);
  let ping;
  ws.onopen = () => { ping = setInterval(() => ws.send('ping'), 25000); };
  ws.onmessage = (e) => applyState(JSON.parse(e.data));
  ws.onclose = (e) => {
    clearInterval(ping);
    if (e.code === 4401) { location.href = '/login'; return; }
    setTimeout(connectSocket, 3000);
  };
}

// ---------- Actions ----------

async function addTracks(body, label) {
  const program = state?.program;
  try {
    const res = await post('/api/player/add', body);
    const what = label || t('toast.nTracks', { n: res.added });
    const key = body.mode === 'now' ? 'toast.playing' : body.mode === 'next' ? 'toast.next' : 'toast.added';
    toast(t(key, { what }));
    // While the nonstop program is running, explain when the request will play
    if (program && body.mode !== 'now') {
      toast(program.type === 'radio' ? t('toast.wishRadio') : t('toast.wishProgram', { name: program.name }));
    }
    if (!res.connected) toast(t('voice.notConnected'), true);
  } catch (err) { fail(err); }
}

async function startProgram(type, id, shuffle, label) {
  try {
    const res = await post('/api/player/program', { type, id, shuffle });
    let name = label || res.program?.name || '';
    if (type === 'favlist') name = t(shuffle ? 'toast.programShuffle' : 'toast.programOrder', { name });
    toast(t('toast.program', { name }));
    if (!res.connected) toast(t('voice.notConnected'), true);
  } catch (err) { fail(err); }
}

async function control(action) {
  try {
    applyState(await post('/api/player/control', { action }));
  } catch (err) { fail(err); }
}

document.addEventListener('click', (e) => {
  const ctl = e.target.closest('[data-control]');
  if (ctl) { control(ctl.dataset.control); return; }

  const btn = e.target.closest('[data-act]');
  if (!btn) return;
  e.preventDefault();
  e.stopPropagation();
  const { act, id, mode, label, index } = btn.dataset;
  switch (act) {
    case 'song': addTracks({ type: 'song', id, mode }, label); break;
    case 'album': addTracks({ type: 'album', id, mode }, label); break;
    case 'playlist': addTracks({ type: 'playlist', id, mode }, label); break;
    case 'radio': addTracks({ type: 'radio', id, mode }, label); break;
    case 'all': addTracks(pageData.source
      ? { ...pageData.source, mode }
      : { type: 'song', ids: (pageData.songs || []).map((s) => s.id), mode }, label); break;
    case 'random': addTracks({ type: 'random', count: 25, mode }, t('albums.randomToast')); break;
    case 'q-remove': post('/api/player/queue/remove', { index: +index }).catch(fail); break;
    case 'q-jump': post('/api/player/queue/jump', { index: +index }).catch(fail); break;
    case 'q-up': post('/api/player/queue/move', { src: +index, dst: +index - 1 }).catch(fail); break;
    case 'q-down': post('/api/player/queue/move', { src: +index, dst: +index + 1 }).catch(fail); break;
    case 'q-clear': control('clear'); break;
    case 'q-shuffle': control('shuffle'); break;
    case 'h-clear': control('clear_history'); break;
    case 'fav': openFavMenu(btn, { type: btn.dataset.ftype || 'song', id }); break;
    case 'prog': startProgram(btn.dataset.ptype, id, btn.dataset.shuffle === '1', label); break;
    case 'prog-stop': post('/api/player/program/stop').catch(fail); break;
    case 'prog-shuffle': post('/api/player/program/shuffle', { shuffle: btn.dataset.val === '1' }).catch(fail); break;
    default: break;
  }
});

$('#loop-btn').addEventListener('click', () => {
  const next = { off: 'queue', queue: 'track', track: 'off' }[state?.loop || 'off'];
  post('/api/player/loop', { mode: next }).catch(fail);
});

let volTimer;
$('#volume').addEventListener('input', (e) => {
  clearTimeout(volTimer);
  volTimer = setTimeout(() => post('/api/player/volume', { volume: +e.target.value }), 120);
});

const seekEl = $('#pb-seek');
seekEl.addEventListener('input', () => { seeking = true; $('#pb-pos').textContent = fmtTime(seekEl.value); });
seekEl.addEventListener('change', async () => {
  seeking = false;
  try { await post('/api/player/seek', { position: +seekEl.value }); } catch (err) { fail(err); }
});

// Voice channel
async function loadChannels() {
  try {
    const channels = await api('/api/channels');
    const sel = $('#channel-select');
    sel.innerHTML = channels.length
      ? channels.map((c) => `<option value="${esc(c.id)}">🔊 ${esc(c.name)}${c.listeners ? ` (${c.listeners})` : ''}</option>`).join('')
      : `<option value="">${esc(t('voice.none'))}</option>`;
    if (state?.channel) sel.value = state.channel.id;
  } catch (err) {
    $('#channel-select').innerHTML = `<option value="">${esc(err.message)}</option>`;
  }
}

$('#join-btn').addEventListener('click', async () => {
  const id = $('#channel-select').value;
  if (!id) return;
  try {
    applyState(await post('/api/player/join', { channel_id: id }));
    toast(t('voice.joined'));
  } catch (err) { fail(err); }
});

// Leaving requires a second click – the button sits where users often expect "Log out"
let leaveTimer;
$('#leave-btn').addEventListener('click', (e) => {
  const btn = e.currentTarget;
  if (btn.dataset.confirm !== '1') {
    btn.dataset.confirm = '1';
    btn.textContent = t('voice.confirmLeave');
    btn.classList.add('btn-danger');
    clearTimeout(leaveTimer);
    leaveTimer = setTimeout(() => resetLeave(btn), 4000);
    return;
  }
  resetLeave(btn);
  control('leave');
});

function resetLeave(btn) {
  clearTimeout(leaveTimer);
  btn.dataset.confirm = '';
  btn.textContent = t('voice.leave');
  btn.classList.remove('btn-danger');
}
$('#channel-select').addEventListener('focus', loadChannels);

$('#logout').addEventListener('click', async () => {
  await post('/api/logout');
  location.href = '/login';
});

$('#menu-toggle').addEventListener('click', () => $('#sidebar').classList.toggle('open'));

// Switch language (sidebar and "My account")
async function changeLang(lang) {
  if (lang === LANG) return;
  setLang(lang);
  applyI18n();
  renderPlayer();
  loadChannels();
  render();
  try { await post('/api/me/lang', { lang }); } catch (err) { fail(err); }
}

document.addEventListener('click', (e) => {
  const b = e.target.closest('[data-lang-btn]');
  if (b) changeLang(b.dataset.langBtn);
});

// ---------- Building blocks ----------

const ICON = {
  play: '<svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>',
  next: '<svg viewBox="0 0 24 24"><path d="M3 10h11v2H3zm0-4h11v2H3zm0 8h7v2H3zm13-1v8l6-4z"/></svg>',
  add: '<svg viewBox="0 0 24 24"><path d="M19 13h-6v6h-2v-6H5v-2h6V5h2v6h6z"/></svg>',
  remove: '<svg viewBox="0 0 24 24"><path d="M19 6.4 17.6 5 12 10.6 6.4 5 5 6.4 10.6 12 5 17.6 6.4 19 12 13.4 17.6 19 19 17.6 13.4 12z"/></svg>',
  up: '<svg viewBox="0 0 24 24"><path d="m7.4 15.4 4.6-4.6 4.6 4.6L18 14l-6-6-6 6z"/></svg>',
  down: '<svg viewBox="0 0 24 24"><path d="M7.4 8.6 12 13.2l4.6-4.6L18 10l-6 6-6-6z"/></svg>',
  heart: '<svg viewBox="0 0 24 24"><path d="M12 21.4 10.6 20C5.4 15.4 2 12.3 2 8.5 2 5.4 4.4 3 7.5 3c1.7 0 3.4.8 4.5 2.1C13.1 3.8 14.8 3 16.5 3 19.6 3 22 5.4 22 8.5c0 3.8-3.4 6.9-8.6 11.5L12 21.4z"/></svg>',
};

const favButton = (id, ftype = 'song') =>
  `<button class="icon-btn fav-btn" title="${esc(t('act.addFav'))}" data-act="fav" data-ftype="${ftype}" data-id="${esc(id)}">${ICON.heart}</button>`;

function songActions(s) {
  const label = esc(`${s.title} – ${s.artist}`);
  return `
    <button class="icon-btn" title="${esc(t('act.playNow'))}" data-act="song" data-mode="now" data-id="${esc(s.id)}" data-label="${label}">${ICON.play}</button>
    <button class="icon-btn" title="${esc(t('act.playNext'))}" data-act="song" data-mode="next" data-id="${esc(s.id)}" data-label="${label}">${ICON.next}</button>
    <button class="icon-btn" title="${esc(t('act.addQueue'))}" data-act="song" data-mode="end" data-id="${esc(s.id)}" data-label="${label}">${ICON.add}</button>
    ${favButton(s.id)}`;
}

function songTable(songs, { numbered = false, showAlbum = true, extraActs = null } = {}) {
  if (!songs.length) return `<p class="empty">${t('songs.none')}</p>`;
  return `<table class="songs"><tbody>${songs.map((s, i) => `
    <tr data-song="${esc(s.id)}">
      <td class="num">${numbered ? (s.track || i + 1) : coverImg(s.coverArt, 80, 'thumb')}</td>
      <td><div class="title">${esc(s.title)}</div>
          <div class="sub">${s.artistId ? `<a href="#/artist/${esc(s.artistId)}">${esc(s.artist)}</a>` : esc(s.artist)}${showAlbum && s.album ? ` · <a href="#/album/${esc(s.albumId)}">${esc(s.album)}</a>` : ''}</div></td>
      <td class="dur hide-sm">${fmtTime(s.duration)}</td>
      <td class="acts">${songActions(s)}${extraActs ? extraActs(s, i) : ''}</td>
    </tr>`).join('')}</tbody></table>`;
}

function albumGrid(albums) {
  if (!albums.length) return `<p class="empty">${t('albums.none')}</p>`;
  return `<div class="grid">${albums.map((a) => `
    <a class="card" href="#/album/${esc(a.id)}">
      ${coverImg(a.coverArt, 300)}
      <div class="t">${esc(a.name)}</div>
      <div class="s">${esc(a.artist)}${a.year ? ' · ' + a.year : ''}</div>
    </a>`).join('')}</div>`;
}

function playAllButtons(label, fav = null) {
  return `<div class="actions">
    <button class="btn btn-primary" data-act="all" data-mode="now" data-label="${esc(label)}">${ICON.play} ${t('act.play')}</button>
    <button class="btn" data-act="all" data-mode="next" data-label="${esc(label)}">${t('act.playNext')}</button>
    <button class="btn" data-act="all" data-mode="end" data-label="${esc(label)}">${t('act.append')}</button>
    ${fav ? `<button class="btn" data-act="fav" data-ftype="${fav.type}" data-id="${esc(fav.id)}">${t('act.toFavs')}</button>` : ''}
  </div>`;
}

const artistLink = (a) => `
  <a href="#/artist/${esc(a.id)}"><span>${esc(a.name)}</span><span class="muted">${t('albumsCount', { n: a.albumCount })}</span></a>`;

// ---------- Favorites menu ----------

let favMenu = null;

function closeFavMenu() {
  if (favMenu) { favMenu.remove(); favMenu = null; }
}

async function addToFavlist(listId, listName, payload) {
  try {
    const res = await post(`/api/favlists/${listId}/songs`, payload);
    if (!res.added) toast(t('fm.already', { name: listName }));
    else toast(t(res.skipped ? 'fm.addedSkipped' : 'fm.added', { n: res.added, name: listName, skipped: res.skipped }));
    if (route().name === 'fav' && route().id === String(listId)) render();
  } catch (err) { fail(err); }
}

async function openFavMenu(anchor, payload) {
  closeFavMenu();
  let lists;
  try {
    lists = (await api('/api/favlists')).filter((f) => f.canEdit);
  } catch (err) { fail(err); return; }

  favMenu = document.createElement('div');
  favMenu.className = 'fav-menu';
  favMenu.innerHTML = `
    <div class="fm-title">${t('fm.title')}</div>
    ${lists.map((f) => `<button class="fm-item" data-list="${f.id}" data-name="${esc(f.name)}">
        <span>${esc(f.name)}</span><span class="muted">${f.songCount}</span></button>`).join('')
      || `<div class="fm-title">${t('fm.none')}</div>`}
    <form><input name="name" placeholder="${esc(t('fm.new'))}" maxlength="80" required><button class="btn btn-sm btn-primary">＋</button></form>`;
  document.body.appendChild(favMenu);

  const r = anchor.getBoundingClientRect();
  const w = favMenu.offsetWidth;
  const h = favMenu.offsetHeight;
  favMenu.style.left = `${Math.max(8, Math.min(r.right - w, window.innerWidth - w - 8))}px`;
  favMenu.style.top = `${r.bottom + h + 8 > window.innerHeight ? Math.max(8, r.top - h - 4) : r.bottom + 4}px`;

  favMenu.addEventListener('click', (e) => {
    e.stopPropagation();
    const item = e.target.closest('[data-list]');
    if (!item) return;
    closeFavMenu();
    addToFavlist(item.dataset.list, item.dataset.name, payload);
  });
  favMenu.querySelector('form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const name = new FormData(e.target).get('name').trim();
    if (!name) return;
    closeFavMenu();
    try {
      const fav = await post('/api/favlists', { name });
      await addToFavlist(fav.id, fav.name, payload);
    } catch (err) { fail(err); }
  });
}

document.addEventListener('click', (e) => {
  if (favMenu && !favMenu.contains(e.target) && !e.target.closest('[data-act="fav"]')) closeFavMenu();
});
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeFavMenu(); });
$('.main').addEventListener('scroll', closeFavMenu);

function markCurrent() {
  const id = state?.current?.id;
  const radioId = state?.current?.radioId;
  document.querySelectorAll('tr[data-song]').forEach((tr) => tr.classList.toggle('current', tr.dataset.song === id));
  document.querySelectorAll('[data-radio]').forEach((el) => el.classList.toggle('playing', el.dataset.radio === radioId));
}

const durLabel = (s) => (s.radio ? '<span class="live-badge">LIVE</span>' : fmtTime(s.duration));
const subLabel = (s) => (s.radio ? esc(s.nowPlaying || t('liveRadio')) : esc(s.artist));

// ---------- Views ----------

const ALBUM_LISTS = [
  ['newest', 'albums.newest'], ['recent', 'albums.recent'], ['frequent', 'albums.frequent'],
  ['random', 'albums.randomList'], ['alphabeticalByName', 'albums.az'], ['starred', 'albums.starred'],
];

async function viewAlbums(params) {
  const type = params.get('type') || 'newest';
  let offset = 0;
  view.innerHTML = `
    <div class="view-head"><h1>${t('albums.title')}</h1>
      <button class="btn" data-act="random" data-mode="end" title="${esc(t('albums.randomTitle'))}">${t('albums.random')}</button></div>
    <div class="tabs">${ALBUM_LISTS.map(([k, l]) => `<a class="btn btn-sm ${k === type ? 'active' : ''}" href="#/albums?type=${k}">${t(l)}</a>`).join('')}</div>
    <div id="album-grid"><p class="empty">${t('loading')}</p></div>
    <p style="text-align:center"><button class="btn" id="more" hidden>${t('albums.more')}</button></p>`;
  const target = $('#album-grid');
  const all = [];
  async function load(size = 60) {
    const albums = await api(`/api/albums?type=${type}&offset=${offset}&size=${size}`);
    all.push(...albums);
    offset += albums.length;
    target.innerHTML = albumGrid(all);
    $('#more').hidden = albums.length < size;
    return albums.length === size;
  }
  $('#more').addEventListener('click', async () => {
    await load();
    rememberView({ loaded: all.length });
  });
  // When navigating back, load as many albums as before (in batches of up to 200)
  const wanted = Math.max(60, navState.loaded || 0);
  while (all.length < wanted && await load(Math.min(200, wanted - all.length))) { /* continue */ }
}

async function viewAlbum(id) {
  const a = await api(`/api/albums/${encodeURIComponent(id)}`);
  pageData.songs = a.songs;
  pageData.source = { type: 'album', id: a.id };
  view.innerHTML = `
    <div class="hero">
      ${coverImg(a.coverArt, 400)}
      <div><div class="muted">${t('album.label')}</div><h1>${esc(a.name)}</h1>
        <div>${a.artistId ? `<a href="#/artist/${esc(a.artistId)}"><b>${esc(a.artist)}</b></a>` : esc(a.artist)}
          <span class="muted"> · ${a.year || ''} · ${t('tracks', { n: a.songs.length })} · ${fmtTime(a.duration)}</span></div>
        ${playAllButtons(a.name, { type: 'album', id: a.id })}</div>
    </div>
    ${songTable(a.songs, { numbered: true, showAlbum: false })}`;
}

async function viewArtists() {
  const artists = await api('/api/artists');
  view.innerHTML = `<h1>${t('artists.title')}</h1>
    <input class="searchbox" id="artist-filter" placeholder="${esc(t('artists.filter'))}" style="margin:12px 0">
    <div class="list" id="artist-list"></div>`;
  const show = (q) => {
    const list = artists.filter((a) => a.name.toLowerCase().includes(q.toLowerCase()));
    $('#artist-list').innerHTML = list.map(artistLink).join('') || `<p class="empty">${t('artists.none')}</p>`;
  };
  $('#artist-filter').addEventListener('input', (e) => show(e.target.value));
  show('');
}

async function viewArtist(id) {
  const a = await api(`/api/artists/${encodeURIComponent(id)}`);
  view.innerHTML = `<div class="muted">${t('artist.label')}</div><h1>${esc(a.name)}</h1>
    <h2>${t('albums.title')}</h2>${albumGrid(a.albums)}`;
}

async function viewPlaylists() {
  const lists = await api('/api/playlists');
  view.innerHTML = `<h1>${t('playlists.title')}</h1><div style="height:16px"></div>` + (lists.length
    ? `<div class="grid">${lists.map((p) => `
        <a class="card" href="#/playlist/${esc(p.id)}">
          ${coverImg(p.coverArt, 300)}
          <div class="t">${esc(p.name)}</div>
          <div class="s">${t('tracks', { n: p.songCount })} · ${fmtTime(p.duration)}</div>
        </a>`).join('')}</div>`
    : `<p class="empty">${t('playlists.none')}</p>`);
}

async function viewPlaylist(id) {
  const p = await api(`/api/playlists/${encodeURIComponent(id)}`);
  pageData.songs = p.songs;
  pageData.source = { type: 'playlist', id: p.id };
  view.innerHTML = `
    <div class="hero">${coverImg(p.coverArt, 400)}
      <div><div class="muted">${t('playlist.label')}</div><h1>${esc(p.name)}</h1>
        <div class="muted">${t('tracks', { n: p.songs.length })}</div>${playAllButtons(p.name, { type: 'playlist', id: p.id })}</div>
    </div>
    ${songTable(p.songs)}`;
}

async function viewRadio() {
  const stations = await api('/api/radios');
  const form = (r = {}) => `
    <form class="radio-form" data-radio-id="${esc(r.id || '')}">
      <div class="row">
        <label>${t('radio.name')}<input name="name" required maxlength="100" value="${esc(r.name || '')}"></label>
        <label>${t('radio.url')}<input name="streamUrl" type="url" required placeholder="https://…/stream.mp3" value="${esc(r.streamUrl || '')}"></label>
      </div>
      <label>${t('radio.homepage')}<input name="homePageUrl" type="url" value="${esc(r.homePageUrl || '')}"></label>
      <div class="row"><button class="btn btn-primary">${r.id ? t('save') : t('radio.add')}</button>
        ${r.id ? `<button type="button" class="btn btn-ghost" data-cancel>${t('cancel')}</button>` : ''}</div>
    </form>`;

  view.innerHTML = `
    <div class="view-head"><h1>${t('radio.title')}</h1></div>
    <p class="muted">${t('radio.intro')}</p>
    ${stations.length ? `<div class="grid" style="margin-top:16px">${stations.map((r) => `
      <div class="card" data-radio="${esc(r.id)}">
        <div data-act="radio" data-mode="now" data-id="${esc(r.id)}" data-label="${esc(r.name)}" title="${esc(t('radio.listenNow'))}">${radioTile(r)}</div>
        <div class="t">${esc(r.name)}</div>
        <div class="card-acts">
          <button class="btn btn-sm btn-primary" data-act="radio" data-mode="now" data-id="${esc(r.id)}" data-label="${esc(r.name)}" title="${esc(t('radio.listenTitle'))}">${ICON.play} ${t('radio.listen')}</button>
          <button class="btn btn-sm" data-act="prog" data-ptype="radio" data-id="${esc(r.id)}" data-label="${esc(r.name)}" title="${esc(t('radio.nonstopTitle'))}">${t('radio.nonstop')}</button>
          ${(r.homePageUrl || me.is_admin) ? `<div class="admin-links">
            ${r.homePageUrl ? `<a class="muted" href="${esc(r.homePageUrl)}" target="_blank" rel="noopener noreferrer">${t('website')}</a>` : ''}
            ${me.is_admin ? `<button data-redit="${esc(r.id)}">${t('edit')}</button>
              <button class="danger" data-rdel="${esc(r.id)}" data-name="${esc(r.name)}">${t('delete')}</button>` : ''}
          </div>` : ''}
        </div>
      </div>`).join('')}</div>`
    : `<p class="empty">${t('radio.none')}</p>`}
    ${me.is_admin ? `<div class="panel" style="margin-top:28px" id="radio-editor">
        <h2 style="margin-top:0" id="radio-editor-title">${t('radio.new')}</h2>${form()}</div>` : ''}`;

  if (!me.is_admin) return;

  const editor = $('#radio-editor');
  const bindForm = () => {
    const f = editor.querySelector('form');
    f.addEventListener('submit', async (e) => {
      e.preventDefault();
      const data = Object.fromEntries(new FormData(f));
      const id = f.dataset.radioId;
      try {
        await api(id ? `/api/radios/${encodeURIComponent(id)}` : '/api/radios',
          { method: id ? 'PUT' : 'POST', body: data });
        toast(t(id ? 'radio.saved' : 'radio.added'));
        viewRadio();
      } catch (err) { fail(err); }
    });
    const cancel = f.querySelector('[data-cancel]');
    if (cancel) cancel.addEventListener('click', () => viewRadio());
  };
  bindForm();

  view.querySelectorAll('[data-redit]').forEach((b) => b.addEventListener('click', () => {
    const r = stations.find((s) => s.id === b.dataset.redit);
    $('#radio-editor-title').textContent = t('radio.editTitle', { name: r.name });
    editor.querySelector('form').outerHTML = form(r);
    bindForm();
    editor.scrollIntoView({ behavior: 'smooth' });
  }));

  view.querySelectorAll('[data-rdel]').forEach((b) => b.addEventListener('click', async () => {
    if (b.dataset.confirm !== '1') {
      b.dataset.confirm = '1';
      b.textContent = t('really');
      return;
    }
    try {
      await api(`/api/radios/${encodeURIComponent(b.dataset.rdel)}`, { method: 'DELETE' });
      toast(t('deleted', { name: b.dataset.name }));
      viewRadio();
    } catch (err) { fail(err); }
  }));
}

const favTile = (f) => (f.coverArt ? coverImg(f.coverArt, 300) : '<div class="fav-tile">♥</div>');

async function viewFavs() {
  const lists = await api('/api/favlists');
  view.innerHTML = `
    <div class="view-head"><h1>${t('favs.title')}</h1></div>
    <p class="muted">${t('favs.intro')}</p>
    <form class="row" id="new-fav" style="margin:16px 0;max-width:520px">
      <label>${t('favs.newLabel')}<input name="name" required maxlength="80" placeholder="${esc(t('favs.newPlaceholder'))}"></label>
      <button class="btn btn-primary">${t('create')}</button>
    </form>
    ${lists.length ? `<div class="grid">${lists.map((f) => `
      <a class="card" href="#/fav/${f.id}">
        ${favTile(f)}
        <div class="t">${esc(f.name)}</div>
        <div class="s">${t('tracks', { n: f.songCount })} · ${t('by', { name: esc(who(f.owner)) })}</div>
      </a>`).join('')}</div>`
    : `<p class="empty">${t('favs.none')}</p>`}`;

  $('#new-fav').addEventListener('submit', async (e) => {
    e.preventDefault();
    try {
      const fav = await post('/api/favlists', { name: new FormData(e.target).get('name').trim() });
      location.hash = `#/fav/${fav.id}`;
    } catch (err) { fail(err); }
  });
}

async function viewFav(id) {
  const f = await api(`/api/favlists/${encodeURIComponent(id)}`);
  pageData.songs = f.songs;
  pageData.source = { type: 'favlist', id: String(f.id) };
  const editActs = f.canEdit ? (s, i) => `
    <button class="icon-btn" title="${esc(t('act.up'))}" data-fmove="${i}" data-dir="-1" ${i === 0 ? 'disabled' : ''}>${ICON.up}</button>
    <button class="icon-btn" title="${esc(t('act.down'))}" data-fmove="${i}" data-dir="1" ${i === f.songs.length - 1 ? 'disabled' : ''}>${ICON.down}</button>
    <button class="icon-btn" title="${esc(t('fav.removeTitle'))}" data-fremove="${esc(s.id)}">${ICON.remove}</button>` : null;

  view.innerHTML = `
    <div class="hero">
      ${favTile(f)}
      <div><div class="muted">${t('fav.label', { owner: esc(who(f.owner)) })}</div>
        <h1 id="fav-name">${esc(f.name)}</h1>
        <div class="muted">${t('tracks', { n: f.songs.length })} · ${fmtTime(f.duration)}</div>
        ${f.songs.length ? playAllButtons(f.name) : ''}
        ${f.songs.length ? `<div class="actions">
            <button class="btn" data-act="prog" data-ptype="favlist" data-id="${f.id}" data-shuffle="0" data-label="${esc(f.name)}">${t('fav.nonstop')}</button>
            <button class="btn" data-act="prog" data-ptype="favlist" data-id="${f.id}" data-shuffle="1" data-label="${esc(f.name)}">${t('fav.nonstopShuffle')}</button>
          </div>` : ''}
        ${f.canEdit ? `<div class="actions">
            <button class="btn btn-sm btn-ghost" id="fav-rename">${t('fav.rename')}</button>
            <button class="btn btn-sm btn-ghost btn-danger" id="fav-delete">${t('fav.delete')}</button></div>` : ''}
      </div>
    </div>
    ${f.songs.length ? songTable(f.songs, { extraActs: editActs }) : `<p class="empty">${t('fav.empty')}</p>`}`;

  if (!f.canEdit) return;

  $('#fav-rename').addEventListener('click', () => {
    const h1 = $('#fav-name');
    h1.innerHTML = `<form class="row" id="rename-form"><input name="name" maxlength="80" required value="${esc(f.name)}">
      <button class="btn btn-sm btn-primary">${t('save')}</button></form>`;
    const input = h1.querySelector('input');
    input.focus();
    input.select();
    $('#rename-form').addEventListener('submit', async (e) => {
      e.preventDefault();
      try {
        await api(`/api/favlists/${f.id}`, { method: 'PUT', body: { name: input.value.trim() } });
        render();
      } catch (err) { fail(err); }
    });
  });

  $('#fav-delete').addEventListener('click', async (e) => {
    const b = e.currentTarget;
    if (b.dataset.confirm !== '1') {
      b.dataset.confirm = '1';
      b.textContent = t('reallyDelete');
      return;
    }
    try {
      await api(`/api/favlists/${f.id}`, { method: 'DELETE' });
      toast(t('fav.deleted', { name: f.name }));
      location.hash = '#/favs';
    } catch (err) { fail(err); }
  });

  view.querySelectorAll('[data-fremove]').forEach((b) => b.addEventListener('click', async (e) => {
    e.stopPropagation();
    try {
      await post(`/api/favlists/${f.id}/remove`, { song_id: b.dataset.fremove });
      render();
    } catch (err) { fail(err); }
  }));

  view.querySelectorAll('[data-fmove]').forEach((b) => b.addEventListener('click', async (e) => {
    e.stopPropagation();
    const src = +b.dataset.fmove;
    try {
      await post(`/api/favlists/${f.id}/move`, { src, dst: src + +b.dataset.dir });
      render();
    } catch (err) { fail(err); }
  }));
}

async function viewSearch(params) {
  const q = params.get('q') || '';
  view.innerHTML = `<h1>${t('search.title')}</h1>
    <input class="searchbox" id="search-input" placeholder="${esc(t('search.placeholder'))}" value="${esc(q)}" style="margin:12px 0">
    <div id="search-results"></div>`;
  const input = $('#search-input');
  input.focus();
  input.setSelectionRange(q.length, q.length);
  let timer;
  input.addEventListener('input', () => {
    clearTimeout(timer);
    timer = setTimeout(() => {
      history.replaceState(history.state, '', `#/search?q=${encodeURIComponent(input.value)}`);
      runSearch(input.value);
    }, 300);
  });
  if (q) await runSearch(q);
}

let searchSeq = 0;
async function runSearch(q) {
  const seq = ++searchSeq;
  const target = $('#search-results');
  if (q.trim().length < 2) { target.innerHTML = ''; return; }
  target.innerHTML = `<p class="empty">${t('search.searching')}</p>`;
  const r = await api(`/api/search?q=${encodeURIComponent(q)}`);
  if (seq !== searchSeq || !$('#search-results')) return;
  pageData.songs = r.songs;
  target.innerHTML = (!r.songs.length && !r.albums.length && !r.artists.length)
    ? `<p class="empty">${t('search.nothing')}</p>`
    : `${r.artists.length ? `<h2>${t('search.artists')}</h2><div class="list">${r.artists.map(artistLink).join('')}</div>` : ''}
       ${r.albums.length ? `<h2>${t('search.albums')}</h2>${albumGrid(r.albums)}` : ''}
       ${r.songs.length ? `<h2>${t('search.songs')}</h2>${songTable(r.songs)}` : ''}`;
  markCurrent();
}

function renderQueue() {
  if (!state) return;
  const q = state.queue;
  const cur = state.current;
  const h = state.history || [];
  view.innerHTML = `
    <div class="view-head"><h1>${t('queue.title')}</h1>
      <div class="actions" style="display:flex;gap:8px">
        <button class="btn btn-sm" data-act="q-shuffle">${t('queue.shuffle')}</button>
        <button class="btn btn-sm btn-danger" data-act="q-clear">${t('queue.clear')}</button>
      </div></div>
    <h2>${t('queue.now')}</h2>
    ${cur ? `<table class="songs"><tbody><tr class="current">
        <td class="num">${cur.radio ? radioTile(cur.coverArt ? cur : { name: cur.title }, 'small') : coverImg(cur.coverArt, 80, 'thumb')}</td>
        <td><div class="title">${esc(cur.title)}</div><div class="sub">${subLabel(cur)}${cur.album ? ' · ' + esc(cur.album) : ''}</div></td>
        <td class="dur hide-sm">${durLabel(cur)}</td>
        <td class="acts muted">${t('addedBy', { name: esc(who(cur.added_by) || '?') })} ${cur.radio ? '' : favButton(cur.id)}</td></tr></tbody></table>`
      : `<p class="empty">${t('queue.nothing')}</p>`}
    ${programBox(state.program)}
    <h2>${t('queue.next', { n: q.length })}</h2>
    ${q.length ? `<table class="songs"><tbody>${q.map((s, i) => `
      <tr>
        <td class="num">${i + 1}</td>
        <td><div class="title">${s.radio ? '📻 ' : ''}${esc(s.title)}</div><div class="sub">${s.radio ? t('liveRadio') : esc(s.artist)} · <span class="muted">${t('addedBy', { name: esc(who(s.added_by) || '?') })}</span></div></td>
        <td class="dur hide-sm">${durLabel(s)}</td>
        <td class="acts">
          <button class="icon-btn" title="${esc(t('act.playNow'))}" data-act="q-jump" data-index="${i}">${ICON.play}</button>
          <button class="icon-btn" title="${esc(t('act.up'))}" data-act="q-up" data-index="${i}" ${i === 0 ? 'disabled' : ''}>${ICON.up}</button>
          <button class="icon-btn" title="${esc(t('act.down'))}" data-act="q-down" data-index="${i}" ${i === q.length - 1 ? 'disabled' : ''}>${ICON.down}</button>
          <button class="icon-btn" title="${esc(t('act.remove'))}" data-act="q-remove" data-index="${i}">${ICON.remove}</button>
        </td></tr>`).join('')}</tbody></table>`
      : `<p class="empty">${t('queue.empty')}</p>`}
    <div class="view-head"><h2>${t('queue.history', { n: h.length })}</h2>
      ${h.length ? `<button class="btn btn-sm btn-ghost" data-act="h-clear">${t('queue.clearHistory')}</button>` : ''}</div>
    ${h.length ? `<table class="songs"><tbody>${h.map((s) => `
      <tr data-song="${esc(s.id)}">
        <td class="num">${s.radio ? radioTile(s.coverArt ? s : { name: s.title }, 'small') : coverImg(s.coverArt, 80, 'thumb')}</td>
        <td><div class="title">${esc(s.title)}</div>
            <div class="sub">${s.radio ? t('liveRadio') : esc(s.artist)} · <span class="history-time">${playedAt(s.played_at)}</span></div></td>
        <td class="dur hide-sm">${durLabel(s)}</td>
        <td class="acts">${s.radio
          ? `<button class="icon-btn" title="${esc(t('queue.listenAgain'))}" data-act="radio" data-mode="now" data-id="${esc(s.radioId)}" data-label="${esc(s.title)}">${ICON.play}</button>`
          : songActions(s)}</td>
      </tr>`).join('')}</tbody></table>`
      : `<p class="empty">${t('queue.noHistory')}</p>`}`;
}

function programBox(p) {
  if (!p) return '';
  const tile = p.type === 'radio' ? radioTile({ name: p.name }, 'small') : '<div class="fav-tile">♥</div>';
  const detail = p.type === 'radio'
    ? t('prog.radio')
    : `${p.index ? t('prog.trackOf', { index: p.index, total: p.total }) : t('tracks', { n: p.total })} · ${t('prog.round', { n: p.round })}`;
  return `
    <h2>${t('prog.title')}</h2>
    <div class="program-box">
      ${tile}
      <div class="pg-info">
        <div class="pg-name">🔁 ${p.type === 'favlist' ? `<a href="#/fav/${esc(p.id)}">${esc(p.name)}</a>` : esc(p.name)}</div>
        <div class="muted">${detail} · ${t('prog.startedBy', { name: esc(p.by) })}</div>
        <div class="muted" style="font-size:.85rem">${t('prog.hint')}</div>
      </div>
      <div class="pg-acts">
        ${p.type === 'favlist' ? `<div class="seg">
            <button data-act="prog-shuffle" data-val="0" class="${p.shuffle ? '' : 'active'}">${t('prog.order')}</button>
            <button data-act="prog-shuffle" data-val="1" class="${p.shuffle ? 'active' : ''}">${t('prog.shuffle')}</button>
          </div>` : ''}
        <button class="btn btn-sm btn-danger" data-act="prog-stop">${t('prog.stop')}</button>
      </div>
    </div>`;
}

function playedAt(ts) {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  const time = d.toLocaleTimeString(locale(), { hour: '2-digit', minute: '2-digit' });
  return d.toDateString() === new Date().toDateString()
    ? t('queue.playedAt', { time })
    : t('queue.playedOn', { date: d.toLocaleDateString(locale()), time });
}

async function viewAccount() {
  view.innerHTML = `<h1>${t('account.title')}</h1>
    <p class="muted">${t('account.loggedIn', { name: esc(me.username) })}${me.is_admin ? ' (Admin)' : ''}</p>
    <div class="panel" style="margin-bottom:16px">
      <h2 style="margin-top:0">${t('account.language')}</h2>
      <div class="seg lang-switch"><button data-lang-btn="de">Deutsch</button><button data-lang-btn="en">English</button></div>
    </div>
    <div class="panel"><form id="pw-form">
      <h2 style="margin-top:0">${t('account.changePw')}</h2>
      <label>${t('account.currentPw')}<input type="password" name="old_password" required autocomplete="current-password"></label>
      <label>${t('account.newPw')}<input type="password" name="new_password" minlength="8" required autocomplete="new-password"></label>
      <label>${t('account.repeatPw')}<input type="password" name="repeat" minlength="8" required autocomplete="new-password"></label>
      <div><button class="btn btn-primary">${t('save')}</button></div>
    </form></div>`;
  applyI18n(view);
  $('#pw-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const f = Object.fromEntries(new FormData(e.target));
    if (f.new_password !== f.repeat) { toast(t('account.mismatch'), true); return; }
    try {
      await post('/api/me/password', { old_password: f.old_password, new_password: f.new_password });
      toast(t('account.pwChanged'));
      e.target.reset();
    } catch (err) { fail(err); }
  });
}

async function viewAdmin() {
  if (!me.is_admin) { view.innerHTML = `<p class="empty">${t('users.adminOnly')}</p>`; return; }
  const users = await api('/api/users');
  const lastLogin = (u) => t('users.lastLogin', { date: u.last_login
    ? esc(new Date(u.last_login.replace(' ', 'T') + 'Z').toLocaleString(LANG === 'en' ? 'en-GB' : 'de-DE', { dateStyle: 'short', timeStyle: 'short' }))
    : t('users.never') });
  view.innerHTML = `<h1>${t('users.title')}</h1>
    <div class="panel" style="margin:16px 0">
      <form id="new-user">
        <h2 style="margin-top:0">${t('users.new')}</h2>
        <div class="row">
          <label>${t('users.username')}<input name="username" required pattern="[A-Za-z0-9_.\\-]{3,32}"></label>
          <label>${t('users.password')}<input name="password" type="password" minlength="8" required autocomplete="new-password"></label>
        </div>
        <label class="check"><input type="checkbox" name="is_admin"> ${t('users.adminRights')}</label>
        <div><button class="btn btn-primary">${t('create')}</button></div>
      </form>
    </div>
    <div class="panel">
      <table class="users"><tbody>${users.map((u) => `
        <tr>
          <td><b>${esc(u.username)}</b> <span class="tag ${u.is_admin ? 'admin' : ''}">${t(u.is_admin ? 'users.admin' : 'users.user')}</span>
            <div class="login-sm">${lastLogin(u)}</div></td>
          <td class="muted hide-sm">${t('users.since', { date: esc(u.created_at.slice(0, 10)) })}</td>
          <td class="login-col">${lastLogin(u)}</td>
          <td class="u-actions">
            ${u.id !== me.id ? `
              <button class="btn btn-sm" data-user="${u.id}" data-uact="admin" data-val="${u.is_admin ? 0 : 1}">${t(u.is_admin ? 'users.revokeAdmin' : 'users.makeAdmin')}</button>
              <button class="btn btn-sm" data-user="${u.id}" data-uact="pw">${t('users.pwButton')}</button>
              <button class="btn btn-sm btn-danger" data-user="${u.id}" data-uact="del" data-name="${esc(u.username)}">${t('delete')}</button>`
              : `<span class="muted">${t('users.itsYou')}</span>`}
          </td>
        </tr>
        <tr hidden id="pw-row-${u.id}"><td colspan="4">
          <form class="row" data-pwform="${u.id}">
            <label>${t('users.newPwFor', { name: esc(u.username) })}<input type="password" name="password" minlength="8" required autocomplete="new-password"></label>
            <button class="btn btn-primary">${t('users.set')}</button>
          </form></td></tr>`).join('')}</tbody></table>
    </div>`;

  $('#new-user').addEventListener('submit', async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    try {
      await post('/api/users', { username: f.get('username'), password: f.get('password'), is_admin: f.get('is_admin') === 'on' });
      toast(t('users.created'));
      viewAdmin();
    } catch (err) { fail(err); }
  });

  view.querySelectorAll('[data-uact]').forEach((b) => b.addEventListener('click', async () => {
    const id = b.dataset.user;
    try {
      if (b.dataset.uact === 'admin') {
        await post(`/api/users/${id}/admin`, { is_admin: b.dataset.val === '1' });
        viewAdmin();
      } else if (b.dataset.uact === 'pw') {
        $(`#pw-row-${id}`).hidden = !$(`#pw-row-${id}`).hidden;
      } else if (b.dataset.uact === 'del') {
        if (b.dataset.confirm !== '1') {
          b.dataset.confirm = '1';
          b.textContent = t('reallyDelete');
          return;
        }
        await api(`/api/users/${id}`, { method: 'DELETE' });
        toast(t('deleted', { name: b.dataset.name }));
        viewAdmin();
      }
    } catch (err) { fail(err); }
  }));

  view.querySelectorAll('[data-pwform]').forEach((f) => f.addEventListener('submit', async (e) => {
    e.preventDefault();
    try {
      await post(`/api/users/${f.dataset.pwform}/password`, { password: new FormData(f).get('password') });
      toast(t('users.pwSet'));
      f.reset();
      f.closest('tr').hidden = true;
    } catch (err) { fail(err); }
  }));
}

async function viewBot() {
  if (!me.is_admin) { view.innerHTML = `<p class="empty">${t('users.adminOnly')}</p>`; return; }
  const data = await api('/api/bot/settings');
  const s = data.settings;
  const de = LANG === 'de';
  const cats = ['play', 'nonstop', 'control', 'info'];
  const cmdName = (c) => `/${de ? c.name_de : c.name}`;

  view.innerHTML = `
    <h1>${t('bot.title')}</h1>
    <p class="muted">${t('bot.intro')}</p>

    <div class="settings-grid">
      <div class="panel">
        <h2 style="margin-top:0">${t('bot.djRole')}</h2>
        <select id="dj-role">
          <option value="">${t('bot.djNone')}</option>
          ${data.roles.map((r) => `<option value="${esc(r.id)}" ${r.id === s.dj_role_id ? 'selected' : ''}>@${esc(r.name)}</option>`).join('')}
        </select>
        <div class="muted" style="font-size:.88rem">${t('bot.djHint')}</div>
      </div>
      <div class="panel">
        <h2 style="margin-top:0">${t('bot.channel')}</h2>
        <select id="bot-channel">
          <option value="">${t('bot.channelAll')}</option>
          ${data.channels.map((c) => `<option value="${esc(c.id)}" ${c.id === s.channel_id ? 'selected' : ''}>#${esc(c.name)}</option>`).join('')}
        </select>
        <div class="muted" style="font-size:.88rem">${t('bot.channelHint')}</div>
      </div>
      <div class="panel">
        <h2 style="margin-top:0">${t('bot.playback')}</h2>
        <label class="switch-row"><input type="checkbox" class="switch" id="opt-normalize" ${s.normalize ? 'checked' : ''}>
          <span><b>${t('bot.normalize')}</b><br><span class="muted">${t('bot.normalizeHint')}</span></span></label>
        <label class="switch-row"><input type="checkbox" class="switch" id="opt-presence" ${s.presence ? 'checked' : ''}>
          <span><b>${t('bot.presence')}</b><br><span class="muted">${t('bot.presenceHint')}</span></span></label>
      </div>
    </div>

    <div class="panel" style="max-width:900px">
      <h2 style="margin-top:0">${t('bot.commands')}</h2>
      <p class="muted" id="dj-inactive" style="font-size:.88rem" ${s.dj_role_id ? 'hidden' : ''}>${t('bot.djInactive')}</p>
      <table class="cmd-table">
        <thead><tr><th>${t('bot.cmdCol')}</th><th class="desc"></th><th class="c">${t('bot.onCol')}</th><th class="c">${t('bot.djCol')}</th></tr></thead>
        <tbody>${cats.map((cat) => {
          const cmds = data.commands.filter((c) => c.category === cat);
          if (!cmds.length) return '';
          return `<tr class="cat"><td colspan="4">${t(`bot.cat.${cat}`)}</td></tr>` + cmds.map((c) => `
            <tr data-cmd="${esc(c.name)}" class="${s.disabled.includes(c.name) ? 'off' : ''}">
              <td><code>${esc(cmdName(c))}</code></td>
              <td class="desc muted">${esc(de ? c.description_de : c.description)}</td>
              <td class="c"><input type="checkbox" class="switch" data-on ${s.disabled.includes(c.name) ? '' : 'checked'}></td>
              <td class="c"><input type="checkbox" class="dj-box" data-dj ${s.dj_commands.includes(c.name) ? 'checked' : ''}></td>
            </tr>`).join('');
        }).join('')}</tbody>
      </table>
    </div>

    <div class="save-bar"><button class="btn btn-primary" id="bot-save">${t('save')}</button></div>`;

  view.querySelectorAll('[data-on]').forEach((cb) => cb.addEventListener('change', () => {
    cb.closest('tr').classList.toggle('off', !cb.checked);
  }));
  $('#dj-role').addEventListener('change', (e) => { $('#dj-inactive').hidden = Boolean(e.target.value); });

  $('#bot-save').addEventListener('click', async (e) => {
    const btn = e.currentTarget;
    const rows = [...view.querySelectorAll('tr[data-cmd]')];
    const body = {
      disabled: rows.filter((r) => !r.querySelector('[data-on]').checked).map((r) => r.dataset.cmd),
      dj_commands: rows.filter((r) => r.querySelector('[data-dj]').checked).map((r) => r.dataset.cmd),
      dj_role_id: $('#dj-role').value || null,
      channel_id: $('#bot-channel').value || null,
      normalize: $('#opt-normalize').checked,
      presence: $('#opt-presence').checked,
    };
    const resync = JSON.stringify([...body.disabled].sort()) !== JSON.stringify([...s.disabled].sort());
    btn.disabled = true;
    try {
      const res = await api('/api/bot/settings', { method: 'PUT', body });
      Object.assign(s, res.settings);
      toast(t(resync ? 'bot.savedSync' : 'bot.saved'));
    } catch (err) { fail(err); } finally { btn.disabled = false; }
  });
}

// ---------- Statistics ----------

const STATS_RANGES = [[7, 'stats.days7'], [30, 'stats.days30'], [365, 'stats.days365'], [0, 'stats.all']];

function rankList(items, render) {
  if (!items.length) return `<p class="muted">–</p>`;
  const max = items[0].count || 1;
  return `<ol class="rank">${items.map((it) => `
    <li><div class="rank-row">${render(it)}<span class="muted">${t('stats.times', { n: it.count })}</span></div>
      <div class="rank-bar"><span style="width:${Math.max(4, Math.round((it.count / max) * 100))}%"></span></div></li>`).join('')}</ol>`;
}

async function viewStats(params) {
  const days = params.has('days') ? Number(params.get('days')) : 30;
  const s = await api(`/api/stats?days=${days}`);
  const hours = s.seconds / 3600;
  view.innerHTML = `<h1>${t('stats.title')}</h1>
    <div class="tabs">${STATS_RANGES.map(([d, l]) => `<a class="btn btn-sm ${d === days ? 'active' : ''}" href="#/stats?days=${d}">${t(l)}</a>`).join('')}</div>
    ${!s.plays && !s.radios.length ? `<p class="empty">${t('stats.none')}</p>` : `
    <div class="stat-tiles">
      <div class="panel stat-tile"><b>${s.plays.toLocaleString(locale())}</b><span class="muted">${t('stats.plays')}</span></div>
      <div class="panel stat-tile"><b>${hours.toLocaleString(locale(), { maximumFractionDigits: hours < 10 ? 1 : 0 })}</b><span class="muted">${t('stats.hours')}</span></div>
    </div>
    <h2>${t('stats.topSongs')}</h2>
    ${s.songs.length ? `<table class="songs"><tbody>${s.songs.map((song, i) => `
      <tr data-song="${esc(song.id)}">
        <td class="num">${i + 1}</td>
        <td class="hide-sm" style="width:52px">${coverImg(song.coverArt, 80, 'thumb')}</td>
        <td><div class="title">${esc(song.title)}</div><div class="sub">${esc(song.artist)}</div></td>
        <td class="dur">${t('stats.times', { n: song.count })}</td>
        <td class="acts">${songActions(song)}</td>
      </tr>`).join('')}</tbody></table>` : `<p class="muted">–</p>`}
    <div class="stats-grid">
      <div class="panel"><h2 style="margin-top:0">${t('stats.topArtists')}</h2>${rankList(s.artists, (a) => `<span>${esc(a.name)}</span>`)}</div>
      <div class="panel"><h2 style="margin-top:0">${t('stats.topUsers')}</h2>
        <p class="muted" style="font-size:.85rem;margin-top:-6px">${t('stats.usersHint')}</p>${rankList(s.users, (u) => `<span>${esc(u.name)}</span>`)}</div>
      <div class="panel"><h2 style="margin-top:0">${t('stats.topRadios')}</h2>${rankList(s.radios, (r) => `<span>📻 ${esc(r.name)}</span>`)}</div>
    </div>`}`;
}

// ---------- Schedule ----------

async function viewSchedule() {
  if (!me.is_admin) { view.innerHTML = `<p class="empty">${t('users.adminOnly')}</p>`; return; }
  const [data, favlists, radios] = await Promise.all([api('/api/schedule'), api('/api/favlists'), api('/api/radios')]);
  let entries = data.entries.map((e) => ({ ...e }));
  let dirty = false;
  const dayNames = t('sched.days').split(',');

  const targetOptions = (e) => {
    const items = e.action === 'favlist' ? favlists.map((f) => [String(f.id), f.name])
      : e.action === 'radio' ? radios.map((r) => [String(r.id), r.name]) : [];
    return `<option value="">—</option>` + items.map(([id, name]) =>
      `<option value="${esc(id)}" ${String(e.target) === id ? 'selected' : ''}>${esc(name)}</option>`).join('');
  };

  function draw() {
    view.innerHTML = `<h1>${t('sched.title')}</h1>
      <p class="muted">${t('sched.intro')}</p>
      <p class="muted" style="font-size:.88rem">🕒 ${esc(t('sched.now', { time: data.now, tz: data.timezone }))}</p>
      <div id="sched-list">${entries.length ? entries.map((e, i) => `
        <div class="panel sched-entry ${e.enabled ? '' : 'off'}" data-i="${i}">
          <div class="sched-top">
            <input type="checkbox" class="switch" data-f="enabled" ${e.enabled ? 'checked' : ''}>
            <input type="time" data-f="time" value="${esc(e.time)}" required>
            <select data-f="action">${['favlist', 'radio', 'stop'].map((a) =>
              `<option value="${a}" ${e.action === a ? 'selected' : ''}>${t(`sched.action.${a}`)}</option>`).join('')}</select>
            ${e.action !== 'stop' ? `<select data-f="target">${targetOptions(e)}</select>` : ''}
            ${e.action === 'favlist' ? `<label class="check"><input type="checkbox" data-f="shuffle" ${e.shuffle ? 'checked' : ''}> ${t('sched.shuffle')}</label>` : ''}
          </div>
          <div class="sched-bottom">
            <div class="seg days">${dayNames.map((d, di) =>
              `<button type="button" data-day="${di}" class="${e.days.includes(di) ? 'active' : ''}">${esc(d)}</button>`).join('')}</div>
            <span class="sched-acts">
              <button class="btn btn-sm" data-run>${t('sched.run')}</button>
              <button class="btn btn-sm btn-danger" data-del>${t('delete')}</button>
            </span>
          </div>
        </div>`).join('') : `<p class="empty">${t('sched.empty')}</p>`}</div>
      <div class="save-bar">
        <button class="btn" id="sched-add">${t('sched.add')}</button>
        <button class="btn btn-primary" id="sched-save">${t('save')}</button>
      </div>`;
  }

  draw();

  // A single listener for everything, because the list is redrawn on changes
  view.onchange = (ev) => {
    const box = ev.target.closest('[data-i]');
    const f = ev.target.dataset.f;
    if (!box || !f) return;
    const e = entries[+box.dataset.i];
    if (f === 'enabled' || f === 'shuffle') e[f] = ev.target.checked;
    else e[f] = ev.target.value;
    if (f === 'action') { e.target = ''; e.shuffle = false; }
    dirty = true;
    if (f === 'action' || f === 'enabled') draw();
  };

  view.onclick = async (ev) => {
    const box = ev.target.closest('[data-i]');
    const e = box && entries[+box.dataset.i];
    if (ev.target.closest('#sched-add')) {
      entries.push({ enabled: true, days: [0, 1, 2, 3, 4, 5, 6], time: '18:00', action: 'favlist', target: '', shuffle: true });
      dirty = true;
      draw();
    } else if (ev.target.closest('[data-day]') && e) {
      const day = +ev.target.closest('[data-day]').dataset.day;
      e.days = e.days.includes(day) ? e.days.filter((d) => d !== day) : [...e.days, day].sort();
      dirty = true;
      draw();
    } else if (ev.target.closest('[data-del]') && e) {
      entries.splice(+box.dataset.i, 1);
      dirty = true;
      draw();
    } else if (ev.target.closest('[data-run]') && e) {
      if (!e.id || dirty) { toast(t('sched.saveFirst'), true); return; }
      try { await post(`/api/schedule/${encodeURIComponent(e.id)}/run`); toast(t('sched.ran')); } catch (err) { fail(err); }
    } else if (ev.target.closest('#sched-save')) {
      if (entries.some((x) => x.action !== 'stop' && !x.target)) { toast(t('sched.noTarget'), true); return; }
      const btn = ev.target.closest('#sched-save');
      btn.disabled = true;
      try {
        const res = await api('/api/schedule', { method: 'PUT', body: { entries } });
        entries = res.entries.map((x) => ({ ...x }));
        dirty = false;
        draw();
        toast(t('sched.saved'));
      } catch (err) { fail(err); btn.disabled = false; }
    }
  };
}

// ---------- Activity log ----------

function activityLabel(action) {
  if (action.startsWith('cmd:')) return t('log.cmd', { cmd: `/${action.slice(4)}` });
  const key = `log.${action}`;
  if (t(key) !== key) return t(key);
  const base = `log.${action.split(':')[0]}`;
  return t(base) !== base ? t(base) : action;
}

async function viewActivity() {
  if (!me.is_admin) { view.innerHTML = `<p class="empty">${t('users.adminOnly')}</p>`; return; }
  const fmt = new Intl.DateTimeFormat(locale(), { dateStyle: 'short', timeStyle: 'short' });
  const rows = (items) => items.map((a) => `
    <tr class="${a.action === 'login_failed' || a.action.startsWith('schedule_failed') ? 'warn' : ''}">
      <td class="muted nowrap">${esc(fmt.format(new Date(a.at * 1000)))}</td>
      <td><b>${esc(who(a.user) || '–')}</b> <span class="tag src-${esc(a.source)}">${t(`log.src.${a.source}`)}</span></td>
      <td>${esc(activityLabel(a.action))}${a.detail ? `<div class="muted log-detail">${esc(a.detail)}</div>` : ''}</td>
    </tr>`).join('');

  const first = await api('/api/activity');
  view.innerHTML = `<h1>${t('log.title')}</h1>
    <p class="muted">${t('log.intro')}</p>
    ${first.length ? `<div class="panel"><table class="log"><tbody id="log-rows">${rows(first)}</tbody></table></div>
      <p style="text-align:center"><button class="btn" id="log-more" ${first.length < 100 ? 'hidden' : ''}>${t('log.more')}</button></p>`
      : `<p class="empty">${t('log.empty')}</p>`}`;

  let last = first.length ? first[first.length - 1].id : null;
  const more = $('#log-more');
  if (more) more.addEventListener('click', async () => {
    more.disabled = true;
    try {
      const items = await api(`/api/activity?before=${last}`);
      $('#log-rows').insertAdjacentHTML('beforeend', rows(items));
      if (items.length) last = items[items.length - 1].id;
      more.hidden = items.length < 100;
    } catch (err) { fail(err); } finally { more.disabled = false; }
  });
}

// ---------- Remember scroll position ----------
// Each browser history entry stores its scroll position (and e.g. the number of
// loaded albums) in history.state. It is restored when navigating back;
// new pages (empty state) start at the top.

const mainEl = $('.main');
let navState = {};       // state of the history entry the current view should restore
let rendering = false;   // don't save while rendering (content is briefly empty)
let scrollTimer;

function rememberView(extra = {}) {
  if (rendering) return;
  try {
    history.replaceState({ ...(history.state || {}), scroll: mainEl.scrollTop, ...extra }, '');
  } catch (e) { /* browser throttles replaceState – harmless */ }
}

mainEl.addEventListener('scroll', () => {
  clearTimeout(scrollTimer);
  scrollTimer = setTimeout(rememberView, 150);
});

// Save the current position immediately before an internal link is followed
document.addEventListener('click', (e) => {
  if (e.target.closest('a[href^="#"]')) {
    clearTimeout(scrollTimer);
    rememberView();
  }
}, true);

// ---------- Router ----------

function route() {
  const hash = location.hash.replace(/^#\/?/, '') || 'albums';
  const [path, query = ''] = hash.split('?');
  const [name, id] = path.split('/');
  return { name, id: id ? decodeURIComponent(id) : undefined, params: new URLSearchParams(query) };
}

async function render() {
  const { name, id, params } = route();
  clearTimeout(scrollTimer);
  rendering = true;
  navState = history.state || {};
  pageData = {};
  view.onchange = null;   // drop page-specific handlers (schedule)
  view.onclick = null;
  $('#sidebar').classList.remove('open');
  document.querySelectorAll('.nav a').forEach((a) => {
    const nav = a.dataset.nav;
    a.classList.toggle('active', nav === name || (nav === 'albums' && name === 'album')
      || (nav === 'artists' && name === 'artist') || (nav === 'playlists' && name === 'playlist')
      || (nav === 'favs' && name === 'fav'));
  });
  view.innerHTML = `<p class="empty">${t('loading')}</p>`;
  try {
    switch (name) {
      case 'albums': await viewAlbums(params); break;
      case 'album': await viewAlbum(id); break;
      case 'artists': await viewArtists(); break;
      case 'artist': await viewArtist(id); break;
      case 'playlists': await viewPlaylists(); break;
      case 'playlist': await viewPlaylist(id); break;
      case 'radio': await viewRadio(); break;
      case 'favs': await viewFavs(); break;
      case 'fav': await viewFav(id); break;
      case 'search': await viewSearch(params); break;
      case 'queue': renderQueue(); break;
      case 'account': await viewAccount(); break;
      case 'admin': await viewAdmin(); break;
      case 'bot': await viewBot(); break;
      case 'stats': await viewStats(params); break;
      case 'schedule': await viewSchedule(); break;
      case 'activity': await viewActivity(); break;
      default: location.hash = '#/albums'; return;
    }
    markCurrent();
    mainEl.scrollTop = navState.scroll || 0;
  } catch (err) {
    view.innerHTML = `<p class="empty">${esc(t('error', { msg: err.message }))}</p>`;
  } finally {
    rendering = false;
  }
}

window.addEventListener('hashchange', render);

// ---------- Start ----------

(async function init() {
  applyI18n();
  me = await api('/api/me');
  // The user's saved language takes precedence; on first use, store the current one
  if (me.lang === 'de' || me.lang === 'en') {
    if (me.lang !== LANG) { setLang(me.lang); applyI18n(); }
  } else {
    post('/api/me/lang', { lang: LANG }).catch(() => {});
  }
  $('#me-name').textContent = me.username;
  document.querySelectorAll('[data-admin]').forEach((a) => { a.hidden = !me.is_admin; });
  applyState(await api('/api/player'));
  await loadChannels();
  if (state?.channel) $('#channel-select').value = state.channel.id;
  connectSocket();
  render();
})();
