/* SparkHome launcher: a tvOS-style Home Screen for the daemon API in SPEC.md.
   In:  GET /api/tiles|state|stats, /api/settings/<section>, SSE /api/events (state, stats, nav, focus, select, back,
        home, voice, toast, tiles, switcher, swipe, tilt, pointer, launch, sleep, settings, display, open_settings).
   Out: POST /api/ui (focus and splash report for agents), /api/launch, /api/action, /api/voice, /api/close (the app
        switcher), /api/settings/<key>, /api/display/apply|confirm|revert, /api/power/<action>, /api/sleep/now|wake.
   Focus lives in JS (row r, index i), never in DOM :focus, and tile geometry is arithmetic,
   so key handlers never read layout. Window.SH exposes the focus state for CDP tests. */
'use strict';
const $ = s => document.querySelector(s);
const stage = $('#stage'), page = $('#page'), shelf = $('#shelf');
// 1920x1080 stage (ui-session.md A.2/A.6): 6 x 260x156 icons + 5 x 40 gaps = the 1760 px safe width
const SIZE = {dock: [260, 156], icons: [260, 156], posters: [260, 390], widgets: [410, 246]};
const GAP = 40, X0 = 80, SAFE_W = 1760, DOCK_Y = 720, PEEK_Y = 980, TITLE = 56, ANCHOR = 250, DOCK_TOP = 150;
const IDLE_MS = 5000, AWAY_MS = 6000, SLEEP_MS = 10 * 60000;
const DIRS = {ArrowUp: 'up', ArrowDown: 'down', ArrowLeft: 'left', ArrowRight: 'right'};

let rows = [], f = {r: 0, i: 0}, H = 1080, info = {}, stats = {}, running = new Set(), tabs = new Set();
let screen = 'home', away = false, talking = false, shelfId = null;
const failed = new Set();   // art URLs that 404'd: faces re-render without them

const esc = s => String(s ?? '').replace(/[&<>"']/g, c => `&#${c.charCodeAt(0)};`);
const color = t => typeof t.color === 'string' && CSS.supports('color', t.color) ? t.color : t.type === 'widget' ? '#76b900' : '#3a3a3c';
const cur = () => rows[f.r]?.tiles[f.i];
const curEl = () => rows[f.r]?.els[f.i];
const tileById = id => rows.flatMap(R => R.tiles).find(t => t.id === id);
async function get(p) { try { const r = await fetch(p, {cache: 'no-store'}); return r.ok ? await r.json() : null; } catch { return null; } }
async function post(p, body) {
  try {
    const r = await fetch(p, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
    return await r.json();
  } catch { return {ok: false, error: 'SparkHome is not answering'}; }
}
function lum(c) {   // WCAG relative luminance of #rgb/#rrggbb (null for other colour syntaxes)
  const m = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(c || ''); if (!m) return null;
  const h = m[1].length === 3 ? [...m[1]].map(x => x + x).join('') : m[1];
  const [r, g, b] = [0, 2, 4].map(i => { const v = parseInt(h.slice(i, i + 2), 16) / 255; return v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4; });
  return .2126 * r + .7152 * g + .0722 * b;
}
// brand face variants: wordmark in brand colour on black (ink) / on white (paper), else white text on a brand fill.
// Each needs >= 3:1 contrast for large text: on black L >= .12, on white L <= .30; fills lighter than .30 get dark text.
// ponytail: flavours for known service ids only; a new service gets the generic fill until it is added here
const INK = new Set(['netflix', 'hulu', 'spotify', 'plex', 'appletv', 'peacock', 'bigpicture']), PAPER = new Set(['youtube', 'youtubetv']);
function variant(t) {
  const l = lum(t.color);
  return l === null ? '' : INK.has(t.id) && l >= .12 ? 'ink' : PAPER.has(t.id) && l <= .3 ? 'paper' : l > .3 ? 'light' : '';
}

/* ---------- tile faces: Steam art layers (hero + logo, capsule) or brand colour + wordmark ---------- */
const art = (t, k) => { const u = t.art?.[k]; return typeof u === 'string' && u && !failed.has(u) ? u : null; };
const img = (u, cls = '') => `<img class="${cls}" src="${esc(u)}" data-u="${esc(u)}" alt="" decoding="async">`;
const ctx2d = document.createElement('canvas').getContext('2d');
let FONT = '';
function wm(t, kind) {   // sized by character count, then shrunk until the longest word fits the face (one word can't wrap:
  // "SuperTuxKart" clipped), and a switcher card's until it all fits the two lines it keeps under a state pill (clamped,
  // a line ended off-centre: Chrome centres it before adding the ellipsis; and the font in use may cut a word). 1.8
  // lines' width: balanced lines run short. ponytail: canvas measureText ignores CSS uppercase/letter-spacing (fine for
  // today's titles)
  const s = String(t.title || ''), n = s.length;
  let fs = kind === 'posters' ? (n <= 8 ? 52 : n <= 13 ? 42 : 34) : (n <= 5 ? 50 : n <= 7 ? 44 : n <= 9 ? 38 : n <= 12 ? 32 : 27);
  FONT ||= getComputedStyle(document.documentElement).getPropertyValue('--font');
  ctx2d.font = `800 ${fs}px ${FONT}`;
  const w = Math.max(0, ...s.split(/\s+/).map(x => ctx2d.measureText(x).width)), room = kind === 'posters' ? 212 : 222;
  const all = kind === 'card' ? ctx2d.measureText(s).width / (1.8 * room) : 0;
  if (w > room || all > 1) fs = Math.floor(fs / Math.max(w / room, all));
  return `<div class="wm" style="--fs:${fs}px">${esc(t.title)}</div>`;
}
function faceHTML(t, kind) {
  if (t.type === 'widget') return widgetHTML(t.widget);
  const hero = art(t, 'hero'), logo = art(t, 'logo'), cap = art(t, 'capsule'), head = art(t, 'header');
  let base = '', fg = '';
  // ponytail: Steam capsules already carry the title, so a capsule poster is one layer (+ shine); hero+logo is the true 2-layer image
  if (kind === 'posters' && cap) base = img(cap);
  else if (hero) { base = img(hero, 'hero'); fg = logo ? img(logo) : wm(t, kind); }
  else if (head && kind !== 'posters') base = img(head);
  else if (cap) base = img(cap);
  else fg = `<div>${wm(t, kind)}${kind === 'posters' && t.subtitle ? `<span class="bsub">${esc(t.subtitle)}</span>` : ''}</div>`;
  return `<div class="layer l0${base ? '' : ' brand'}">${base}</div>${fg ? `<div class="layer l1 fg">${fg}</div>` : ''}`;
}
const GEAR = '<svg class="gear" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"><circle cx="12" cy="12" r="3.2"/><path d="M10.3 2.8h3.4l.5 2.4 1.6.9 2.3-.8 1.7 2.9-1.8 1.7v1.8l1.8 1.7-1.7 2.9-2.3-.8-1.6.9-.5 2.4h-3.4l-.5-2.4-1.6-.9-2.3.8-1.7-2.9 1.8-1.7v-1.8L3.7 9.2l1.7-2.9 2.3.8 1.6-.9z"/></svg>';
function widgetHTML(w) {
  const body = {
    gpu: '<div class="ring"><svg viewBox="0 0 100 100"><circle class="trk" cx="50" cy="50" r="44"/><circle class="val" cx="50" cy="50" r="44" pathLength="100" stroke-dasharray="0 100" data-ring/></svg>' +
      '<div class="rv"><b><span data-s="gpu.util">–</span><small>%</small></b><small>util</small></div></div>' +
      '<div class="wcol"><span class="wt">GPU</span><span class="num"><b data-s="gpu.temp">–</b><small> °C</small></span><span class="num"><b data-s="gpu.power">–</b><small> W</small></span></div>',
    memory: '<span class="wt">Memory</span><div class="big"><b data-s="mem.used_gb">–</b><small> GB</small></div><div class="bar"><i data-bar></i></div>' +
      '<span class="wsub">of <b data-s="mem.total_gb">–</b> GB unified · load <b data-s="load1">–</b></span>',
    models: '<span class="wt">Models</span><ul data-models><li class="none">–</li></ul>',
    settings: `${GEAR}<span class="wname">Settings</span><span class="wsub">Display · Sound · Sleep</span>`,
  }[w] ?? `<span class="wt">${esc(w)}</span>`;
  return `<div class="layer l0 wface ${esc(w)}">${body}</div>`;
}
const OPT = '<span class="opt">Optimized for Spark</span>';   // a GameSpark launch profile (t.profile, its summary)
function tileHTML(t, kind) {
  return `<div class="lift"><div class="shadow"></div><div class="tilt"><div class="face">${faceHTML(t, kind)}` +
    `<div class="spec"></div><div class="shade"></div><span class="run">Running</span>${t.profile != null ? OPT : ''}</div></div></div>` +
    `<div class="label"><b>${esc(t.title)}<span class="rn"> · Running</span></b>${t.note ? `<span class="note">${esc(t.note)}</span>` : ''}</div>`;
}

/* ---------- rows ---------- */
function render(d) {
  const keepRow = rows[f.r]?.id, keepId = cur()?.id;
  const was = Object.fromEntries(rows.map(R => [R.id, {k: R.k, last: R.tiles[R.last]?.id}]));   // scroll + memory survive a refetch
  info = d || {}; rows = []; page.textContent = '';
  const src = (Array.isArray(info.rows) ? info.rows : []).filter(s => s && Array.isArray(s.tiles) && s.tiles.some(t => t?.id));
  let y = src[0]?.style === 'dock' ? DOCK_Y : 150;
  src.forEach((s, n) => {
    const tiles = s.tiles.filter(t => t?.id);
    let style = SIZE[s.style] ? s.style : 'icons';
    if (style === 'dock' && n) style = 'icons';   // only a first row can be the Dock
    const [w, h] = SIZE[style], kind = style === 'dock' ? 'icons' : style;
    const R = {id: s.id, style, kind, tiles, w, h, k: was[s.id]?.k || 0, last: Math.max(0, tiles.findIndex(t => t.id === was[s.id]?.last))};
    if (style === 'dock') { R.top = R.tilesTop = DOCK_Y; y = PEEK_Y - TITLE - 16; }   // next row's icons peek at y=980
    else { R.top = y; R.tilesTop = y + TITLE + Math.round(h * .1); y = R.tilesTop + h + Math.round(h * .1) + 112; }   // focus growth above + label/note below
    const el = document.createElement('section');
    el.className = `row ${style}${n === 1 && rows[0]?.style === 'dock' ? ' peek' : ''}`;
    el.style.top = R.top + 'px';
    el.innerHTML = (style === 'dock' ? '' : `<h2>${esc(s.title)}</h2>`) + `<div class="strip" style="top:${R.tilesTop - R.top}px">` +
      tiles.map(t => `<div class="tile ${kind} ${variant(t)}" data-id="${esc(t.id)}" style="--c:${esc(color(t))}">${tileHTML(t, kind)}</div>`).join('') + '</div>';
    R.el = el; R.strip = el.lastChild; R.els = [...R.strip.children];
    R.els.forEach((e, j) => { e._t = tiles[j]; e._k = kind; });
    place(R); rows.push(R); page.append(el);
  });
  H = y;
  running = new Set(rows.flatMap(R => R.tiles.filter(t => t.running).map(t => t.id)));
  paintRunning(); paintStats();
  if (!rows.length) return;
  const r0 = rows.findIndex(R => R.id === keepRow), r = r0 < 0 ? Math.min(f.r, rows.length - 1) : r0;
  const i = rows[r].tiles.findIndex(t => t.id === keepId);
  focus(r, i < 0 ? f.i : i, true);   // a vanished tile/row: stay in place (focus() clamps), not the row start / Dock
}
const tx = (R, j) => X0 + (j - R.k) * (R.w + GAP);   // on-screen x of tile j in row R
const cols = R => Math.max(1, Math.floor((SAFE_W + GAP) / (R.w + GAP)));   // fully visible tiles per row
function place(R) {
  R.k = Math.max(0, Math.min(R.k, R.tiles.length - cols(R)));
  R.strip.style.transform = `translate3d(${-R.k * (R.w + GAP)}px,0,0)`;
}
function scrollRow(R) {   // whole-column steps keep the 40 px peeks symmetric on both sides
  if (f.i < R.k) R.k = f.i; else if (f.i >= R.k + cols(R)) R.k = f.i - cols(R) + 1;
  place(R);
}
function layout() {   // vertical page scroll; the Top Shelf scrolls away with the Dock
  // first row under the Dock: the Dock stays fully visible at the top (tvOS grid); deeper rows: title at ANCHOR
  const R = rows[f.r], y = !R || R.style === 'dock' ? 0
    : Math.max(0, Math.min(f.r === 1 && rows[0].style === 'dock' ? DOCK_Y - DOCK_TOP : R.top - ANCHOR, H - 1080));
  page.style.transform = `translate3d(0,${away && !y ? 460 : -y}px,0)`;
  for (const T of rows) T.el.classList.toggle('gone', T.tilesTop + T.h - y < 200);   // rows scrolled under the status chips fade out
  shelf.style.transform = `translate3d(0,${-Math.min(y, 1080)}px,0)`;
}

/* ---------- focus engine ---------- */
let lastMove = 0, fastT = 0;
function focus(r, i, quiet) {
  const R = rows[r]; if (!R) return;
  i = Math.max(0, Math.min(i, R.tiles.length - 1));
  const old = curEl(); if (old && old !== R.els[i]) old.classList.remove('focused', 'ptr');
  f = {r, i}; R.last = i;
  R.els[i].classList.add('focused');
  scrollRow(R);
  stage.classList.toggle('ondock', R.style === 'dock');
  if (R.style === 'dock') shelfFor(R.tiles[i]);
  layout(); report();
  if (!quiet) wake();
}
function move(dir) {
  const R = rows[f.r]; if (!sw && !set && (!R || screen !== 'home')) return;
  unpoint();
  const now = performance.now();   // fast repeats shorten every focus/scroll transition (~90 ms)
  if (now - lastMove < 190) { stage.classList.add('fast'); clearTimeout(fastT); fastT = setTimeout(() => stage.classList.remove('fast'), 260); }
  lastMove = now;
  if (sw) return swMove(dir);
  if (set) return setMove(dir);
  if (dir === 'left' || dir === 'right') {
    const i = f.i + (dir === 'right' ? 1 : -1);
    return i < 0 || i >= R.tiles.length ? bump(dir) : focus(f.r, i);   // no wrap
  }
  const T = rows[f.r + (dir === 'down' ? 1 : -1)];
  return T ? focus(rows.indexOf(T), nearest(R, T)) : bump(dir);
}
function nearest(R, T) {
  // Norigin core 4.1.2 scoring, "center" distance: adjacent = x-overlap >= 20 % of the focused tile's width;
  // priority = adjacent ? (5*dy + dx + 1)/5 : 5*dx + dy + 1. The row's last-focused tile wins while it is still adjacent.
  const a = tx(R, f.i), b = a + R.w, c = (a + b) / 2, dy = Math.abs(T.tilesTop - R.tilesTop);
  let best = 0, bp = Infinity;
  T.tiles.forEach((_, j) => {
    const x0 = tx(T, j), x1 = x0 + T.w, dx = Math.abs((x0 + x1) / 2 - c);
    const adj = Math.min(b, x1) - Math.max(a, x0) >= .2 * R.w;
    const p = adj && j === T.last ? -1 : adj ? (5 * dy + dx + 1) / 5 : 5 * dx + dy + 1;
    if (p < bp) { bp = p; best = j; }
  });
  return best;
}
const NUDGE = {left: '-14px 0', right: '14px 0', up: '0 -12px', down: '0 12px'};
function bump(dir, el = curEl()) {   // focus can't move: a short nudge, like tvOS
  el?.firstChild.animate([{translate: '0 0'}, {translate: NUDGE[dir]}, {translate: '0 0'}], {duration: 260, easing: 'ease-out'});
  wake();
}
function focusId(id) {   // the daemon opening a launcher widget (Settings): a launch's splash goes too, or the select
  // after it would be ignored (activate)
  let at = null;
  rows.forEach((R, r) => { const i = R.tiles.findIndex(t => t.id === id); if (i >= 0 && (!at || r === f.r)) at = {r, i}; });
  if (at) { closeOverlays(); shrink(); focus(at.r, at.i); }
}
function locate(el) { for (let r = 0; r < rows.length; r++) { const i = rows[r].els.indexOf(el); if (i >= 0) return {r, i}; } return null; }
let repT = 0;
function report() {   // launch: the splash up (its tile's id), which only the page knows (a Back the daemon never
  // hears takes it down: the IR remote's Escape, the pad's B, coming back into view): the daemon's "quit it" (front_key)
  clearTimeout(repT);
  repT = setTimeout(() => post('/api/ui', {launch: launched?.id ?? null, ...sw ? {focused: sw.cards[sw.i]?.key ?? null,
    row: 'switcher', screen} : set ? {focused: `settings:${SECTIONS[set.sec].id}`, row: 'settings', screen}
    : {focused: cur()?.id ?? null, row: rows[f.r]?.id ?? null, screen}}), 100);
}

/* ---------- actions ---------- */
function ok() { if (sw) return swOk(); if (set) return setOk(); activate(); }
async function activate() {   // never under a launch's splash (nor is a click), nor just after OK on Home's card: a
  // second OK, meant for a card's switch or the switcher, opened the launcher's focused tile instead
  const t = cur(); if (!t || screen !== 'home' || launched || performance.now() < swOkT) return;
  wake();
  if (t.type === 'widget') {
    curEl().firstChild.animate([{scale: 1}, {scale: .84}, {scale: 1}], {duration: 240, easing: 'ease-out'});   // pressed = unfocused size
    if (t.widget === 'settings') setOpen();
    return;
  }
  const grown = grow(t, curEl()), me = launched;
  if (t.type === 'web') await grown;   // let the zoom finish before Chrome switches to the tab; Steam takes seconds anyway
  if (launched !== me) return;   // Home (or TV TV) during the zoom took it back: nothing opens
  const r = await post('/api/launch', {id: t.id});
  if (!r?.ok) { shrink(); toast(r?.error || `Couldn’t open ${t.title}`, 'error'); }
}

/* ---------- app launch: the tile grows to fill the screen, Home shrinks it back into its tile (tvOS) ---------- */
const launchEl = $('#launch'), EASE = 'cubic-bezier(.32,.72,0,1)', still = matchMedia('(prefers-reduced-motion: reduce)');
let launched = null, prevActive = null;   // launched {id, at, steam, game}: the tile the full-screen splash grew from
function faceRect(el) {   // the tile face's on-screen box, in stage pixels
  const s = stage.getBoundingClientRect(), k = s.width / 1920, b = el.querySelector('.face').getBoundingClientRect();
  return {x: (b.left - s.left) / k, y: (b.top - s.top) / k, w: b.width / k, h: b.height / k};
}
const boxTo = (r, w, h) => `translate(${r.x}px,${r.y}px) scale(${r.w / w},${r.h / h})`;
function cover(w, h) { const k = Math.max(1920 / w, 1080 / h); return `translate(${(1920 - w * k) / 2}px,${(1080 - h * k) / 2}px) scale(${k})`; }
function grow(t, el) {   // a game's splash then follows its launch's stages (SSE launch); a resume opens in its up look
  const kind = el._k, {w, h} = {w: el.offsetWidth, h: el.offsetHeight}, r = faceRect(el), hero = art(t, 'hero'), logo = art(t, 'logo');
  const verb = t.type === 'steam' ? 'Starting' : 'Opening', resume = running.has(t.id) || tabs.has(t.id);
  launchEl.innerHTML = `<div class="zoom tile ${kind} ${variant(t)}" style="--c:${esc(color(t))};width:${w}px;height:${h}px">` +
    `<div class="face">${faceHTML(t, kind)}</div></div><div class="app${hero ? '' : ' plain'}">` +
    (hero ? `${img(hero, 'hero')}<div class="scrim"></div>` : '') +
    `<div class="who">${hero ? (logo ? img(logo, 'logo') : `<h1 class="wm">${esc(t.title)}</h1>`) : ''}${resume ? '' : sumHTML(t)}` +
    `<div class="st"><span class="bar"><i></i></span><span class="msg">${resume ? upText(t.type === 'steam') : `${verb} ${esc(t.title)}…`}</span></div></div></div>`;
  launchEl.hidden = false; launchEl.classList.toggle('up', resume);
  launched = {id: t.id, at: performance.now(), steam: t.type === 'steam', game: String(t.id).startsWith('steam:')};
  report();
  const z = launchEl.firstChild, app = launchEl.lastChild, ms = still.matches ? 0 : 520;
  z.animate([{transform: boxTo(r, w, h), borderRadius: '14px'}, {transform: cover(w, h), borderRadius: '0px'}],
            {duration: ms, easing: EASE, fill: 'forwards'});
  return app.animate([{opacity: 0}, {opacity: 0, offset: .45}, {opacity: 1}], {duration: ms, fill: 'forwards'}).finished.catch(() => {});
}
const upText = steam => `${steam ? 'Playing' : 'Open'} · Home comes back here`;
const sumHTML = t => t?.profile == null ? '' : `<p class="sum"><b>Optimized for Spark</b>${t.profile ? ` · ${esc(t.profile)}` : ''}</p>`;
function growId(id) {   // a launch the page didn't start (voice, the phone, an agent, a card): its tile's splash, the one
  // up before (another launch, still loading behind it) gone
  let at = null; rows.forEach((R, r) => { const i = R.tiles.findIndex(x => x.id === id); if (i >= 0 && !at) at = {r, i}; });
  if (at) { shrink(); focus(at.r, at.i, true); grow(rows[at.r].tiles[at.i], rows[at.r].els[at.i]); }
}
function shrink() {   // back on the launcher: the splash returns into its tile (or fades if that tile is gone)
  if (!launched) return false;
  const id = launched.id; launched = null; report();
  let at = null; rows.forEach((R, r) => { const i = R.tiles.findIndex(x => x.id === id); if (i >= 0 && !at) at = {r, i}; });
  const z = launchEl.firstChild, app = launchEl.lastChild, ms = still.matches ? 0 : 440;
  if (at) { stage.classList.add('snap'); focus(at.r, at.i, true); void stage.offsetWidth; stage.classList.remove('snap'); }
  const el = at && rows[at.r].els[at.i];
  app?.animate([{opacity: 1}, {opacity: 0}], {duration: ms * .4, fill: 'forwards'});
  const a = el && z ? z.animate([{transform: cover(z.offsetWidth, z.offsetHeight), borderRadius: '0px'},
                                 {transform: boxTo(faceRect(el), z.offsetWidth, z.offsetHeight), borderRadius: '14px'}],
                                {duration: ms, easing: EASE, fill: 'forwards'})
                    : launchEl.animate([{opacity: 1}, {opacity: 0}], {duration: ms * .6, fill: 'forwards'});
  a.finished.catch(() => {}).then(() => { if (!launched) { launchEl.hidden = true; launchEl.replaceChildren(); launchEl.getAnimations().forEach(x => x.cancel()); } });
  return true;
}
function back(user) {
  wake();
  if (sw) return user && hush() || swBack();   // a voice request over it first (it went Menu, into `from`, and ran on)
  if (set) return set.sheet ? (user && hush(), setBack()) : user && hush() || setBack();   // (a sheet's safe pill at once,
  // the reply with it: "set the resolution to 4k" shows one for the first 5 s of the Keep sheet's 15, as the footer
  // says "No picture? Press Back")
  if (shrink() || closeOverlays(user)) return;
  if (rows[0]?.style === 'dock' && f.r) focus(0, rows[0].last);
}
function home() {   // back from an app (Settings is one): its tile keeps the focus (tvOS); otherwise the Dock
  if (sw && performance.now() - sw.at < SW_GRACE) return;   // a late duplicate of the Home press that opened the switcher
  const was = !!set;
  closeOverlays(); wake(); unpoint();
  if (!shrink() && rows.length && !was) focus(0, rows[0].last);
}
function closeOverlays(user) {   // user Back also dismisses/cancels voice; daemon home/focus events must not cancel a running request
  let did = swClose();
  did = setClose() || did;
  if (screen === 'command') { closeCmd(); did = true; }
  return user && hush() || did;
}
function hush() {   // a user's Back on the voice panel: a request listening or thinking is cancelled (README), a reply dismissed
  if (!stage.classList.contains('talk')) return false;
  const ph = $('#voice').dataset.phase;
  if (talking) ptt(false); else if (ph === 'listening' || ph === 'thinking') { post('/api/voice', {action: 'cancel'}); dismissed = true; }
  hideVoice(); return true;
}
function openCmd() { stage.classList.add('cmd'); screen = 'command'; const i = $('#cmdin'); i.value = ''; i.focus(); report(); }
function closeCmd() { stage.classList.remove('cmd'); $('#cmdin').blur(); screen = 'home'; report(); }
$('#cmd').addEventListener('submit', e => {
  e.preventDefault();
  const text = $('#cmdin').value.trim(); closeCmd();
  if (text) { voice({phase: 'thinking', transcript: text}); post('/api/voice', {text}); }
});

/* ---------- app switcher (DESIGN.md §7): SSE switcher {open, apps, from} opens it (TV twice, Guide twice or held, Home
   twice, View, the phone's Apps, "show me the apps"); {apps} refreshes it, keyed. Home + every open app as a 16:9 card,
   most recent first, focus on the app just left. OK switches, Menu goes back to it; a Siri flick up (SSE swipe) or Up
   then OK quits; force only through the Cancel-first sheet. Geometry is arithmetic: handlers never read layout. */
const swEl = $('#switcher'), swRow = $('#swrow');
const SW_GRACE = 500, SW_GUARD = 1200, SW_ARM = 4000;   // ms: late duplicate Home (or Back, or OK after Home's); one gesture = one close; armed timeout
const SW_KIND = {game: 'Game', tab: 'Web app', app: 'App', system: 'Steam', window: 'Window'};
let sw = null, swT = 0, swByeT = 0, swOkT = 0;
// sw: {cards, els, i, k, size: [w, h, gap], from, armed, sheet: null|{key, i, why}, at, guard, backT, armT, voice}
// ponytail: the gaps keep the focused card's ring (x1.12, its 8 px outline too) 24 px or more clear of its neighbours: it
// grows 52 px a side at 720 px (96, not 48), 40 at 520 (72), 32 at 380 (56); at 48/40 it all but touched them. Five or
// more keep 40 (13 px clear at 300 px)
const swSize = n => n <= 2 ? [720, 405, 96] : n === 3 ? [520, 293, 72] : n === 4 ? [380, 214, 56] : [300, 169, 40];
const swFit = ([w, , g]) => Math.floor((SAFE_W + g) / (w + g));   // cards fully visible at once
const swCur = () => sw.els[sw.i];
const swTile = c => c.tile && typeof c.tile === 'object' ? c.tile : {id: c.key, title: c.title, type: c.kind};
const swVerb = c => c.kind === 'tab' || c.kind === 'window' ? 'close' : 'quit';
const swSnapUrl = c => typeof c.snap === 'string' && c.snap.startsWith('/api/snap/') ? c.snap : null;
const swApps = d => (Array.isArray(d.apps) ? d.apps : []).filter(c => c && typeof c.key === 'string' && c.key !== 'home' && typeof c.title === 'string');
const txt = (el, v) => { if (el && el.textContent !== v) el.textContent = v; };
const swSay = s => txt($('#swlive'), s);   // the polite live region: "Netflix, Web app, 2 of 4", "Closed Netflix"
function swNote(c) {   // the focused card's detail line (force only where it can be: never Big Picture, Steam, a
  // window; stuck once it took the quit, c.quitting: still exiting, maybe with its launcher still open)
  if (c.kind === 'home') return sw.cards.length > 1 ? '' : 'Nothing else is open';
  if (sw.armed === c.key) return `OK to ${swVerb(c)} · Down to cancel`;
  const force = c.can_force ? ', ' + 'or quit again to force quit'.replaceAll(' ', '\u00a0') : '';   // (wraps before "or")
  return {starting: 'Starting…', closing: 'Quitting…',
          stuck: !c.quitting ? 'Open it to see why' + force : c.can_force ? 'Quit again to force quit' : 'It may be saving'}[c.state]
    ?? (c.windows === 0 ? 'Running without a window' : (SW_KIND[c.kind] || 'App') + (c.windows > 1 ? ` · ${c.windows} windows` : ''));
}
const swWhere = () => { const c = sw.cards[sw.i]; return [c.title, swNote(c), `${sw.i + 1} of ${sw.cards.length}`].filter(Boolean).join(', '); };
function swCard(c) {   // the tile stack: art face, the snapshot over it and a state pill; above it, the pointer's quit
  const t = swTile(c), el = document.createElement('div'), u = swSnapUrl(c), home = c.kind === 'home';
  const face = home   // Home: the wallpaper and a miniature Dock in its first six tiles' colours
    ? `<div class="layer l0 homeface"><div class="minidock">${(rows[0]?.style === 'dock' ? rows[0].tiles.slice(0, 6) : [])
        .map(d => `<i class="brand" style="--c:${esc(color(d))}"></i>`).join('')}</div></div>`
    : faceHTML(t, 'card') + (u ? `<img class="snap" src="${esc(u)}" alt="">` : '') +
      '<span class="st"><span class="ld"><i></i></span><b></b></span>';
  const x = home ? '' : `<span class="x"><i>×</i>${swVerb(c) === 'close' ? 'Close' : 'Quit'}</span>`;
  el.className = `tile card ${home ? 'home' : variant(t)}`; el.dataset.id = t.id; el.setAttribute('role', 'option');
  el.style.setProperty('--c', color(t));
  el.innerHTML = `<div class="lift"><div class="shadow"></div><div class="tilt"><div class="face">${face}<div class="spec"></div>` +
    `<div class="shade"></div></div></div>${x}</div><div class="label"><b>${esc(c.title).replace(/ (?=\d)/g, '&nbsp;')}</b>` +
    '<span class="note"></span></div>';   // (a title wraps, a number stays with its word: "The Witcher 3: / Wild Hunt")
  el._t = t; el._k = 'card';   // (what the art error handler and grow() read)
  return el;
}
function swPlace() {   // the visible run of cards centred on the stage, paged by whole cards (scrollRow's rule). Cards
  // centre on y 490: the focused one (x1.12) with its label (.1h + 92 px under it: title, one detail line) then centres
  // on 540 at every size
  const [w, h, g] = sw.size, n = sw.cards.length, vis = Math.min(n, swFit(sw.size));
  if (sw.i < sw.k) sw.k = sw.i; else if (sw.i >= sw.k + vis) sw.k = sw.i - vis + 1;
  sw.k = Math.max(0, Math.min(sw.k, n - vis));
  const x0 = (1920 - vis * (w + g) + g) / 2 - sw.k * (w + g);
  sw.els.forEach((el, j) => {   // (a card peeking past the run: no label, it would be cut by the stage edge)
    el.style.transform = `translate3d(${x0 + j * (w + g)}px,${490 - h / 2}px,0)`;
    el.classList.toggle('peek', j < sw.k || j >= sw.k + vis);
  });
}
function swPaint() {
  sw.els.forEach((el, j) => {
    const c = sw.cards[j], on = j === sw.i;
    el.classList.toggle('focused', on); el.setAttribute('aria-selected', on);
    if (!on) el.classList.remove('ptr');
    el.classList.toggle('armed', sw.armed === c.key);
    for (const s of ['starting', 'closing', 'stuck']) el.classList.toggle(s, c.state === s);
    txt(el.querySelector('.st b'), c.state === 'stuck' ? (c.quitting ? 'Still quitting' : 'Didn’t quit') : c.state === 'starting' ? 'Starting…' : 'Quitting…');
    txt(el.querySelector('.note'), swNote(c));
  });
  swEl.classList.toggle('sheet', !!sw.sheet); swEl.classList.toggle('armed', !!sw.armed);   // (the hint says OK opens)
}
function swSizeVars() { const [w, h] = sw.size; swRow.style.cssText = `--tw:${w}px;--th:${h}px;--z:${(w / 260).toFixed(3)}`; }
function swOpen(d) {
  closeOverlays(); shrink();   // a tile's or card's switch still zooming is taken back, as Home takes it (the phone's
  // Apps, the pad's View, voice): its tab came up over the switcher, which then closed
  const ph = $('#voice').dataset.phase;   // a reply still up: the person moved on (it would cover the hint for 5 s)
  if (ph === 'done' || ph === 'error') hideVoice();
  const cards = [{key: 'home', kind: 'home', title: 'Home'}, ...swApps(d)], j = cards.findIndex(c => c.key === d.from);
  sw = {cards, i: j > 0 ? j : Math.min(1, cards.length - 1), k: 0, size: swSize(cards.length), from: j > 0 ? d.from : null,
        armed: null, sheet: null, at: performance.now(), guard: 0, voice: ph === 'listening' || ph === 'thinking'};
  clearTimeout(swT); stage.classList.remove('swgo'); swEl.classList.remove('go');
  swSizeVars(); sw.els = cards.map(swCard); swRow.replaceChildren(...sw.els);
  swPlace(); swPaint(); swSay(swWhere());
  if (!still.matches) sw.els.forEach((el, j) => el.animate([{opacity: 0, translate: '0 36px'}, {opacity: 1, translate: '0 0'}],
    {duration: 320, easing: 'cubic-bezier(.2,.8,.2,1)', delay: 30 * Math.min(6, Math.abs(j - sw.i)), fill: 'backwards'}));
  stage.classList.add('sw'); screen = 'switcher'; report(); wake();
}
function swRefresh(d) {   // keyed: cards keep their elements, vanished ones fly off, focus keeps its key, else its index
  if (!sw || !Array.isArray(d.apps)) return;
  const was = new Map(sw.cards.map((c, j) => [c.key, {c, el: sw.els[j]}])), key = sw.cards[sw.i].key;
  const cards = [sw.cards[0], ...swApps(d)];
  for (const [k, o] of was) if (!cards.some(c => c.key === k)) swBye(o.el);
  let prev = null;
  sw.els = cards.map(c => {
    const o = was.get(c.key);
    if (!o) {   // a newcomer fades in at its place
      const el = swCard(c); prev.after(el);
      if (!still.matches) el.animate({opacity: [0, 1]}, 320);
      return prev = el;
    }
    if (c.state === 'stuck' && o.c.state !== 'stuck') swSay(`${c.title} ${c.quitting ? 'is still quitting' : 'didn’t quit'}${c.can_force ? '; force quit is available' : ''}`);
    if (c.kind !== 'home') {   // a snapshot that arrived (or changed) since: it cross-fades in on load
      const im = o.el.querySelector('img.snap'), u = swSnapUrl(c);
      if (im && im.getAttribute('src') !== u) im.remove();
      if (u && (!im || !im.isConnected)) o.el.querySelector('.st').insertAdjacentHTML('beforebegin', `<img class="snap" src="${esc(u)}" alt="">`);
    }
    return prev = o.el;
  });
  const j = cards.findIndex(c => c.key === key);
  sw.i = j >= 0 ? j : Math.min(sw.i, cards.length - 1);   // the closed card's index: the next older one, else left
  // ...under the close guard: a flick, OK or × aimed at the card that just went (it quit by itself, or its Quitting…
  // ended) must not land on its neighbour (a re-flick on a stuck card would quit the next app)
  if (j < 0) sw.guard = Math.max(sw.guard, performance.now() + SW_GUARD);
  sw.cards = cards;
  if (!cards.some(c => c.key === sw.armed && c.state !== 'closing')) swDisarm(true);
  if (sw.sheet && !cards.some(c => c.key === sw.sheet.key)) swSheetClose();   // it quit by itself (the guard too)
  // the size is fixed at open, unless they don't fit or Home is left alone: it fades out as it was and in at the size it
  // opens with alone once the last card has flown off ("Nothing else is open" was 425 or 806 px, by how many had been open)
  if (cards.length === 1 && sw.size[0] !== swSize(1)[0]) {
    const old = sw.els[0], el = sw.els[0] = swCard(cards[0]);
    old.style.cssText += swRow.style.cssText; old.ariaHidden = 'true'; old.after(el);
    old.animate({opacity: [1, 0]}, still.matches ? 0 : 200).finished.catch(() => {}).then(() => old.remove());
    if (!still.matches) el.animate([{opacity: 0, translate: '0 36px'}, {opacity: 1, translate: '0 0'}],
      {duration: 320, delay: 280, easing: 'cubic-bezier(.2,.8,.2,1)', fill: 'backwards'});
  }
  if (cards.length > swFit(sw.size) || cards.length === 1) { sw.size = swSize(cards.length); swSizeVars(); }
  swPlace(); swPaint();
  if (key !== sw.cards[sw.i].key) { swSay(swWhere()); report(); }   // (the daemon's "it": the card focused now)
}
function swBye(el) {   // fly-off, or a fade with reduced motion; the neighbours glide into its place once it's gone
  el.classList.add('bye'); el.setAttribute('aria-hidden', 'true');   // (as it was: its focus scale and armed lift,
  el.style.cssText += swRow.style.cssText;   // its size too: a lone Home's refit of the row doesn't balloon it mid-flight)
  if (!still.matches) {   // (swRefresh places them right after: their move waits out the 280 ms fly-off)
    swRow.classList.add('settle'); clearTimeout(swByeT); swByeT = setTimeout(() => swRow.classList.remove('settle'), 280);
  }
  el.animate(still.matches ? {opacity: [1, 0]} : {translate: ['0 0', '0 -170px'], opacity: [1, 0]},
             {duration: still.matches ? 150 : 280, easing: 'cubic-bezier(.4,0,1,1)', fill: 'forwards'}).finished.catch(() => {}).then(() => el.remove());
}
function swClose(go) {   // go: a switch (the cards vanish in the frame the zoom starts); otherwise a 180 ms fade
  if (!sw) return false;
  clearTimeout(sw.armT); sw = null;
  for (const t of [...$('#toasts').children].slice(1)) t.remove();   // the older toasts it hid stay gone
  swEl.classList.toggle('go', !!go); stage.classList.remove('sw'); screen = 'home'; report();
  wake();   // (an input closed it: no Top Shelf sliding in a moment later, the away timer having waited on the switcher)
  clearTimeout(swT); swT = setTimeout(() => sw || swRow.replaceChildren(), 250);   // hidden cards must not sway on (CPU)
  return true;
}
function swFocus(j) {
  if (j === sw.i) return;
  sw.i = j; swDisarm(true); swPlace(); swPaint(); swSay(swWhere()); report(); wake();
}
function swMove(dir) {
  if (sw.sheet) { if (dir === 'left' || dir === 'right') swSheetFocus(dir === 'right' ? 1 : 0); return; }
  if (dir === 'up') return swArm();
  if (dir === 'down') return sw.armed ? swDisarm() : bump(dir, swCur());
  const j = sw.i + (dir === 'right' ? 1 : -1);
  if (j < 0 || j >= sw.cards.length) { swDisarm(); return bump(dir, swCur()); }   // no wrap
  swFocus(j);
}
function swArm() {   // Up, X or Delete: the card lifts and asks for OK (repeats change nothing; disarms after 4 s or a move)
  const c = sw.cards[sw.i];
  if (sw.sheet || performance.now() < sw.guard || sw.armed === c.key) return;
  if (c.kind === 'home' || c.state === 'closing') return bump('up', swCur());
  sw.armed = c.key; clearTimeout(sw.armT);
  sw.armT = setTimeout(() => sw?.armed === c.key && swDisarm(), SW_ARM);
  swPaint(); swSay(swNote(c)); wake();
}
function swDisarm(quiet) { if (!sw.armed) return; sw.armed = null; clearTimeout(sw.armT); if (!quiet) swPaint(); }
function swOk() {
  if (sw.sheet) return swSheetPress(sw.sheet.i);
  if (performance.now() < sw.guard) return;   // the close guard covers OK too: a double-tapped confirm opens nothing
  const c = sw.cards[sw.i];
  if (sw.armed === c.key) return swCloseCard(c);
  if (c.kind === 'home') { swOkT = performance.now() + SW_GRACE; return swClose(); }   // Home: stay on the launcher
  swSwitch(c);
}
function swBack() {   // Menu: the sheet, else back to the app you came from (tvOS), else just close
  if (sw.sheet) return swSheetClose();
  if (performance.now() < sw.backT) return;   // a second Back (a double press) just after the sheet went: not into `from`
  const c = sw.cards.find((x, j) => j && x.key === sw.from && x.state !== 'closing');
  c ? swSwitch(c) : swClose();
}
async function swCloseCard(c) {   // graceful first; force only through the sheet; one close per gesture (the guard). A
  // launch is asked too: the daemon ends one that has started nothing yet, else says it's still starting (force: the sheet)
  if (!c || c.kind === 'home' || c.state === 'closing' || performance.now() < sw.guard) return;
  if (c.kind !== 'tab' && c.state !== 'starting' && !(c.can_close && c.state === 'open')) {
    swDisarm();
    return c.can_force ? swSheet(c) : toast(`Close ${c.title} from the app itself.`);
  }
  swDisarm(true);   // (quietly: it keeps its lift until it flies off, or the reply repaints it)
  sw.guard = performance.now() + SW_GUARD;
  const r = await post('/api/close', {key: c.key});
  if (!sw) return;
  if (r?.ok) return swDone(c, r.state);
  swPaint();
  const now = sw.cards.find(x => x.key === c.key);
  if (r?.force && now) return swSheet(now);
  toast(r?.error || `Couldn’t ${swVerb(c)} ${c.title}`, 'error');
}
function swDone(c, state) {   // the daemon's reply: a tab is gone at once; anything else shows Quitting… until a refresh drops it.
  // A refresh that came first is newer (a force quit that failed at once: its card back as it was) and stays
  swRefresh({apps: sw.cards.slice(1).filter(x => state !== 'closed' || x.key !== c.key).map(x => x === c ? {...x, state: 'closing'} : x)});
  if (state === 'closed') swSay(`Closed ${c.title}`);   // (after the refresh's own announcement)
}
const SW_WHY = {stuck: 'It didn’t quit when asked. Anything unsaved will be lost.', windowless: 'It has no window to ask. Anything unsaved will be lost.',
  starting: 'It’s still starting.', quitting: 'It’s still quitting. Anything unsaved will be lost.',
  unaskable: 'It can’t be asked to quit safely: quit it from its own menu to save first. Anything unsaved will be lost.'};
function swSheet(c) {   // Force Quit? Cancel starts focused; swipes are ignored while it's up. (unaskable: a window that
  // can't be asked, as Wine's desktop: asking would end every process in it at once)
  const why = c.state === 'starting' ? 'starting' : c.state === 'open' && !(c.windows && c.can_close)
    ? (c.windows ? 'unaskable' : 'windowless') : c.quitting ? 'quitting' : 'stuck';
  sw.sheet = {key: c.key, i: 0, why};
  $('#swst').innerHTML = `Force&nbsp;quit <span>${esc(c.title)}?</span>`; txt($('#swsb'), SW_WHY[why]);
  swSheetFocus(0); swPaint(); swSay(`Force quit ${c.title}? ${SW_WHY[why]} Cancel.`); wake();
}
function swSheetFocus(b) { sw.sheet.i = b; swEl.querySelectorAll('#swsheet .pill').forEach((p, j) => p.classList.toggle('on', j === b)); }
// The sheet goes (Cancel, Force Quit, Menu, a click outside it, its card gone) under the close guard: a double OK or
// click then neither opens the stuck app behind it nor goes Menu back into it; nor does a second Back for 0.5 s
function swSheetClose() { const t = performance.now(); sw.sheet = null; sw.guard = t + SW_GUARD; sw.backT = t + SW_GRACE; swPaint(); }
async function swSheetPress(b) {
  const s = sw.sheet, c = sw.cards.find(x => x.key === s.key);
  swSheetClose();
  if (!b || !c) return;
  const r = await post('/api/close', {key: c.key, force: true});
  if (!sw) return;
  r?.ok ? swDone(c, r.state) : toast(r?.error || `Couldn’t force quit ${c.title}`, 'error');
}
async function swSwitch(c) {   // the card zooms into the app through the launch splash; the cards go in that frame
  const el = sw.els[sw.cards.indexOf(c)], snap = el.querySelector('img.snap.on');
  stage.classList.add('swgo');
  const grown = grow(swTile(c), el), z = launchEl.firstChild, me = launched;
  z.style.setProperty('--z', (sw.size[0] / 260).toFixed(3));
  if (snap) z.firstChild.insertAdjacentHTML('beforeend', `<img class="snap on" src="${esc(snap.getAttribute('src'))}" alt="">`);
  swClose(true);
  grown.then(() => stage.classList.remove('swgo'));
  if (c.kind === 'tab') await grown;   // as activate(): the zoom finishes before Chrome switches to the tab
  if (launched !== me) return;   // Home, TV TV or Menu during the zoom took it back: no switch
  const r = await post('/api/launch', {id: c.key});
  if (!r?.ok) { shrink(); toast(r?.error || `Couldn’t open ${c.title}`, 'error'); }
}
function swClick(e) {   // pointer: a card opens, its × quits at once, a sheet button presses, anywhere else is Menu
  const card = e.target.closest?.('#swrow .tile'), j = sw.els.indexOf(card), pill = e.target.closest?.('#swsheet .pill');
  if (sw.sheet) return pill ? swSheetPress(+pill.dataset.b) : e.target.closest?.('#swsheet') || swBack();
  if (j < 0) return card || performance.now() < sw.guard || swBack();   // a card flying off, or a double click's
  // second half (the guard): nothing, never Menu's switch
  if (e.target.closest('.x')) return swCloseCard(sw.cards[j]);
  swFocus(j); swOk();
}
document.addEventListener('load', e => {   // a snapshot cross-fades in over the card's art (160 ms); 'error' drops a failed one
  const im = e.target;
  if (im.classList?.contains('snap') && !im.classList.contains('on')) { im.classList.add('on'); im.animate({opacity: [0, 1]}, still.matches ? 0 : 160); }
}, true);

/* ---------- Settings (SPEC.md §2, tvOS Settings): the full stage, opaque over the dimmed wallpaper (no backdrop-filter:
   the TV's Chrome may composite in software). Sections on the left, the focused one's rows on the right: sliders (OK
   takes one, then Left and Right step it, OK or Back lets go: a value changes only after OK, as tvOS lists, so a Siri
   slide that coasts into Sound's Volume, or Left to go back, changes nothing), toggles, pickers (OK pushes their list),
   actions and info. Power and a new resolution ask on a sheet,
   the safe pill focused first. Values: GET /api/settings/<section> once the list rests on a section, SSE settings live;
   changes POST /api/settings/<key> (a resolution: /api/display/apply, then the Keep sheet SSE display drives). Rows sit
   84 px apart and focus lives here, as on the launcher: handlers never read layout. */
const setEl = $('#settings'), setList = $('#setlist'), setRows = $('#setrows'), setFoot = $('#setfoot');
const ROW = 84, VIEW = 820, HEAD = 80;   // px: a row and its gap; the pane (y 200 to the safe area); a pushed list's title
const FOLLOW = 150, HOLD = 500, REFRESH = 5000;   // ms: the pane follows the list once it rests; a local change outlives
// older values echoed back (SSE, a GET already on its way); About, Controllers and Sleep refetch while shown
const ICONS = {   // 24 px stroke drawings, as the Settings tile's gear
  display: '<rect x="2.5" y="4" width="19" height="13" rx="2"/><path d="M8.5 20.5h7"/>',
  sound: '<path d="M4 9.5h3.5L12 5.5v13l-4.5-4H4z"/><path d="M15.5 9a4 4 0 0 1 0 6M18 6.5a7.5 7.5 0 0 1 0 11"/>',
  voice: '<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21M8.5 21h7"/>',
  sleep: '<path d="M20 14.5A8 8 0 1 1 9.5 4a6.5 6.5 0 0 0 10.5 10.5z"/>',
  controllers: '<path d="M7 7.5h10a4 4 0 0 1 3.9 3.1l.9 4.3a2.3 2.3 0 0 1-4 2l-2.3-2.4h-7l-2.3 2.4a2.3 2.3 0 0 1-4-2l.9-4.3A4 4 0 0 1 7 7.5z"/>' +
    '<path d="M8 10v3.5M6.25 11.75h3.5M15.5 11h.01M17.5 13h.01"/>',
  network: '<path d="M2.5 9a14 14 0 0 1 19 0M5.5 12.5a9.5 9.5 0 0 1 13 0M8.5 16a5 5 0 0 1 7 0M12 19.5h.01"/>',
  power: '<path d="M12 3v8M7 6.3a7.5 7.5 0 1 0 10 0"/>',
  about: '<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7.5h.01"/>'};
const SIRI = {connected: 'Connected', 'not connected': 'Not connected', off: 'Off'};
const MIC = {ready: 'Ready', 'no helper': 'Helper not running (deploy/install.sh root)', off: 'Off'};
const ASR = {ready: 'Ready', 'first use': 'Starts on first use', 'not set up': 'Not set up'};
const POWER = {   // the confirm sheets: title, why, the pill that acts (Cancel comes first, focused)
  restart_sparkhome: ['Restart SparkHome?', 'Apps and games keep running; the launcher reloads.', 'Restart'],
  reboot: ['Restart the Spark?', 'Games and apps close without saving. SparkHome comes back on the TV after the restart.', 'Restart'],
  shutdown: ['Shut down the Spark?', 'Games and apps close without saving. To turn the Spark on again, press its power button. ' +
    'The TV stays on: switch it off with its own remote.', 'Shut Down']};
const num = v => typeof v === 'number' && isFinite(v);
function dur(s) {   // 290 → "5 minutes", 8040 → "2 hours 14 minutes", 266400 → "3 days 2 hours"
  if (!num(s) || s < 0) return null;
  const n = (v, w) => `${v} ${w}${v === 1 ? '' : 's'}`, m = Math.ceil(s / 60), h = Math.floor(m / 60);
  return s < 60 ? n(Math.round(s), 'second') : m < 60 ? n(m, 'minute') : h < 24 ? n(h, 'hour') + (m % 60 ? ` ${n(m % 60, 'minute')}` : '')
    : n(Math.floor(h / 24), 'day') + (h % 24 ? ` ${n(h % 24, 'hour')}` : '');
}
const gb = v => num(v) ? v >= 1000 ? `${(v / 1000).toFixed(1)} TB` : `${Math.round(v)} GB` : '–';
function sleepLine(s) {   // the Sleep section's info (the daemon's sleep_state): the picture goes 10 s after the warning
  if (s.state === 'asleep') return 'Asleep';
  if (s.state === 'awake' && !num(s.in_s)) return 'Off';
  const why = {voice: 'you’re talking to SparkHome', launch: 'a game is loading', playing: 'a video or music is playing',
               game: 'a game or app is in front'}[s.held];
  return why ? `Waiting: ${why}` : num(s.in_s) ? `Sleeps in ${dur(s.in_s + (s.state === 'warning' ? 0 : 10))}` : null;
}
// Rows: {key, type: 'slider'|'toggle'|'pick'|'modes'|'action'|'info', label, ...}. A key's value is the daemon's (GET,
// SSE); an info row's value is text, or a function of the section's info. A section's rows are a function of its data
// when the daemon lists them (devices, addresses): "Loading…" until it answers. Choices (pickers) are the daemon's too.
const SECTIONS = [
  {id: 'display', label: 'Display', rows: [
    {key: 'display.mode', type: 'modes', label: 'Resolution',
     foot: ['A new resolution shows for 15 seconds: choose Keep, or it switches back. No picture? Press Back, or wait.']},
    {key: 'display.brightness', type: 'slider', label: 'Brightness', min: 50, max: 150, step: 10},
    {key: 'display.color_range', type: 'pick', label: 'Color range'},
    {id: 'pattern', type: 'action', label: 'Test pattern', more: true}],
   foot: ['The Spark can’t change the TV’s backlight over HDMI. For a brighter picture, use the TV’s own picture settings ' +
          '(Backlight, Picture Mode). Brightness here changes the Spark’s own picture: above 100\u00a0% it lifts dark and ' +
          'middle tones (black and white stay put); below 100\u00a0% it dims everything.',
          'Color range must match the TV’s. Black looks grey: choose Full. Dark detail lost in black: choose Limited. The ' +
          'test pattern shows which.']},
  {id: 'sound', label: 'Sound', rows: [
    {key: 'sound.volume', type: 'slider', label: 'Volume', min: 0, max: 100, step: 5},
    {key: 'sound.muted', type: 'toggle', label: 'Mute'},
    {key: 'sound.output', type: 'pick', label: 'Output'}],   // (one sink, today's: it reads as info)
   foot: ['This is the Spark’s level, before the TV’s own volume.']},
  {id: 'voice', label: 'Voice', rows: [
    {key: 'tts.enabled', type: 'toggle', label: 'Spoken replies'},
    {key: 'tts.voice', type: 'pick', label: 'Voice', preview: true, none: 'Not installed'},
    {key: 'tts.volume', type: 'slider', label: 'Speech volume', min: 10, max: 100, step: 10, settle: 600, preview: true},
    {label: 'Siri Remote', value: i => SIRI[i.siri_remote]},
    {label: 'Remote microphone', value: i => MIC[i.remote_mic]},
    {label: 'Speech recognition', value: i => ASR[i.asr]}],
   foot: ['Replies from the assistant are spoken; quick commands only show on screen. Nothing is spoken while a game is in front.']},
  {id: 'sleep', label: 'Sleep', rows: [
    {key: 'sleep.minutes', type: 'pick', label: 'Sleep after'},
    {key: 'sleep.game_minutes', type: 'pick', label: 'During games and apps'},
    {key: 'sleep.signal', type: 'pick', label: 'When asleep'},
    {id: 'sleep', type: 'action', label: 'Sleep Now'},
    {label: 'Status', value: sleepLine}],
   foot: ['The picture turns off after this long with no button pressed on a remote, pad, phone, keyboard or mouse, and no ' +
          'voice request. It waits while a video or music plays, while you talk to SparkHome, while a game loads, and while a ' +
          'game or app is in front (unless set above). Any button wakes it; that press does nothing else.',
          'The Spark can’t switch the TV itself off: that needs an HDMI-CEC adapter. With the signal off, the TV shows No signal ' +
          'and may switch itself off, if its own settings say so.']},
  {id: 'controllers', label: 'Controllers & Bluetooth', rows: d => {
    const i = d.info || {};
    return [...(Array.isArray(i.devices) ? i.devices : []).filter(x => x?.name).map(x => ({label: x.name, value: x.connected
              ? `Connected${num(x.battery) ? ` · ${x.battery} %` : ''}` : 'Not connected'})),
            {label: 'Bluetooth', value: {on: 'On', off: 'Off', unavailable: 'Unavailable'}[i.bluetooth]},
            {id: 'help', type: 'action', label: 'How to use the remotes', more: true}];
  }, foot: ['A paired controller reconnects when you switch it on. To pair a new one, use deploy/pair.sh (see the README).']},
  {id: 'network', label: 'Network', rows: d => {
    const i = d.info || {}, p = i.phone || {}, pin = /^\d{6}$/.test(p.pin ?? '') ? p.pin : '';   // (loopback only)
    return [{label: 'Name', value: i.hostname},
            ...(Array.isArray(i.addresses) ? i.addresses : []).filter(a => a?.address).map(a => ({label: a.kind, value: a.address})),
            {label: 'Phone remote', value: p.url || 'Off'},
            ...p.url ? [{label: 'PIN', value: pin ? `${pin.slice(0, 3)} ${pin.slice(3)}` : 'Shown on the TV', code: !!pin}] : [],
            {label: 'Paired phones', value: p.paired > 0 ? String(p.paired) : 'None'},
            {label: 'Agents (MCP)', value: i.mcp}];
  }, foot: i => [i.phone?.url ? 'To pair a phone, open the phone remote’s address on it and enter the PIN.'
                 : 'The phone remote is off: set "lan": true in sparkhome.json and restart SparkHome.',
                 'Agents connect over MCP from this machine only; from elsewhere, use an ssh tunnel.']},
  {id: 'power', label: 'Power', rows: [
    {id: 'restart_sparkhome', type: 'action', label: 'Restart SparkHome'},
    {id: 'reboot', type: 'action', label: 'Restart the Spark'},
    {id: 'shutdown', type: 'action', label: 'Shut Down'}]},
  {id: 'about', label: 'About', rows: d => {
    const i = d.info || {}, g = i.gpu || {}, m = i.mem || {}, s = i.storage || {};
    return [{label: 'SparkHome', value: i.build && `${i.build}${i.built ? ` · ${i.built}` : ''}`},
            {label: 'Device', value: i.device},
            {label: 'Software', value: [i.os, i.kernel].filter(Boolean).join(' · ')},
            {label: 'NVIDIA driver', value: i.driver},
            {label: 'Uptime', value: dur(i.uptime_s)},
            {label: 'SparkHome uptime', value: dur(i.sparkhome_uptime_s)},
            {label: 'GPU', value: [num(g.temp) && `${Math.round(g.temp)} °C`, num(g.util) && `${Math.round(g.util)} % in use`,
                                   num(g.power) && `${Math.round(g.power)} W`].filter(Boolean).join(' · ')},
            {label: 'Memory', value: num(m.total_gb) && `${m.used_gb} of ${m.total_gb} GB`},
            {label: 'Storage', value: num(s.total_gb) && `${gb(s.used_gb)} of ${gb(s.total_gb)} used`},
            {label: 'AI models loaded', value: Array.isArray(i.models) ? i.models.map(shortModel).join(', ') || 'None' : null}];
  }}];
const HELP = '<dl class="help">' + [   // Controllers › How to use the remotes (the old side panel's list)
  ['Remote', 'Arrows · OK opens · Back goes to the Dock · Home'],
  ['Siri Remote', 'Slide to move · click to open · Menu goes back · TV is Home, twice for open apps · hold Siri to talk'],
  ['Gamepad', 'D-pad or stick · A opens · B goes back · hold Y to talk · Guide is Home'],
  ['Keyboard', '<kbd>←↑↓→</kbd> · <kbd>Enter</kbd> · <kbd>Esc</kbd> · <kbd>Home</kbd> · hold <kbd>V</kbd> to talk · <kbd>/</kbd> to type'],
  ['Voice', '“Open Netflix” · “search YouTube for lo-fi” · “volume down”. No mic? Use the phone’s text box.'],
  ['Open apps', 'TV or Guide twice · hold Guide · phone Apps']].map(([t, d]) => `<dt>${t}</dt><dd>${d}</dd>`).join('') + '</dl>';
const PATTERN = (() => {   // §2.6: black level 0-24 in steps of 2, a ramp, white level 230-254 in steps of 4 and 255
  const bars = vs => vs.map(v => `<i style="background:rgb(${v},${v},${v})"><b>${v}</b></i>`).join('');
  return `<div class="pb">${bars([...Array(13).keys()].map(j => 2 * j))}</div><div class="pr"></div>` +
    `<div class="pw">${bars([230, 234, 238, 242, 246, 250, 254, 255])}</div><p>Every bar should look different from its ` +
    'neighbours. Dark bars merge into black: try Limited. Black looks grey: try Full. Back to return.</p>';
})();
let set = null, keepT = 0;   // set (open): {sec, at: 'list'|'pane', row, k, mem: {section: row}, list, shown, stack:
// null|{key, label, choices, i, modes, preview, foot}|{help, label, i}, sheet: null|{kind, i, action|mode}, pattern,
// grab (the focused slider takes Left and Right), followT, refT}. keepT: the Keep sheet's countdown. ponytail: one level
// of push (a picker's list, the help page); a stack array once a pushed list opens another
const setVals = {}, setData = {}, setBusy = {}, setWait = {}, setTouched = {}, setSeen = {};   // key → value; section →
// its GET; key → its request in flight; key → a slider's settle timer; key → its last local change, its last SSE (ms)
const OWNER = {display: 'display', sound: 'sound', tts: 'voice', sleep: 'sleep'};   // a key's section, by its prefix
const rowsOf = (S, d) => typeof S.rows === 'function' ? S.rows(d || {}) : S.rows;
const kind = (R, d) => (R.type === 'pick' || R.type === 'modes') && !(d?.choices?.[R.key]?.length > 1) ? 'info' : R.type || 'info';
function shown(R, d) {   // a row's value as it reads: "100 %", "On", a choice's label, an info row's text ('–' unknown)
  if (!R.key) { const v = typeof R.value === 'function' ? R.value(d?.info || {}) : R.value; return v || v === 0 ? String(v) : R.type ? '' : '–'; }
  const v = setVals[R.key], c = d?.choices?.[R.key];
  if (Array.isArray(c) && !c.length && R.none) return R.none;
  if (v === undefined || v === null) return '–';
  return R.type === 'slider' ? `${v} %` : R.type === 'toggle' ? v ? 'On' : 'Off' : c?.find?.(x => x.value === v)?.label ?? String(v);
}
function rowHTML(R, d) {   // (a slider's value and fill are set in place, setFill: its row stays, so the fill glides)
  const k = kind(R, d), lab = `<span class="lab">${esc(R.label)}</span>`;
  if (k === 'slider') return `<div class="srow slider" role="option">${lab}<span class="trk"><i></i></span><span class="val"></span></div>`;
  const more = k === 'pick' || k === 'modes' || R.more;
  return `<div class="srow${more ? ' more' : ''}${R.code ? ' code' : ''}" role="option">${lab}<span class="val">${esc(shown(R, d))}</span></div>`;
}
function setFill(d) {
  set.list.forEach((R, j) => {
    const el = setRows.children[j], n = setVals[R.key];
    if (!el?.classList.contains('slider')) return;
    txt(el.lastChild, shown(R, d));
    el.querySelector('.trk i').style.transform = `scaleX(${num(n) ? Math.max(0, Math.min(1, (n - R.min) / (R.max - R.min))).toFixed(3) : 0})`;
  });
}
const choiceHTML = (c, on) => `<div class="srow choice${on ? ' chk' : ''}" role="option" aria-selected="${on}"><span class="lab">` +
  `${esc(c.label ?? c.value)}</span>${c.detail ? `<span class="val">${esc(c.detail)}</span>` : ''}</div>`;
function setOpen(sec, confirm, deep) {   // the tile's OK; SSE open_settings {section, confirm} (deep: focus in its pane,
  // with the confirm sheet over it); SSE display pending while it was closed (Display)
  const i = SECTIONS.findIndex(S => S.id === sec);
  if (!set) {
    closeOverlays(); shrink();
    const ph = $('#voice').dataset.phase;   // a reply still up: the person moved on (it sat over the rows for 5 s)
    if (ph === 'done' || ph === 'error') hideVoice();
    set = {sec: 0, at: 'list', row: 0, k: 0, mem: {}, list: [], shown: null, stack: null, sheet: null, pattern: false,
           grab: false, followT: 0, refT: setInterval(setRefresh, REFRESH)};
    setList.innerHTML = SECTIONS.map(S => '<div class="srow" role="option"><svg viewBox="0 0 24 24" fill="none" ' +
      `stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">${ICONS[S.id]}</svg>` +
      `<span class="lab">${esc(S.label)}</span></div>`).join('');
    stage.classList.add('set'); screen = 'settings';
  } else {   // already open: the pattern, a pushed list and a Power sheet give way (a new resolution's Keep sheet stays),
    // unless the person is already in that section's rows (Resolution's list, as the daemon opens Display to apply)
    setPattern(false);
    if (set.sheet?.kind === 'power') setSheetClose();
    if (deep && !confirm && i === set.sec && set.shown === sec && (set.at === 'pane' || set.stack)) return;
  }
  if (i >= 0) set.sec = i;
  const S = SECTIONS[set.sec], j = confirm && Array.isArray(S.rows) ? S.rows.findIndex(R => R.id === confirm) : -1;
  if (deep) set.mem[S.id] = Math.max(0, j);   // (its first row, or the action asked about)
  set.at = deep ? 'pane' : 'list'; set.shown = null; set.grab = false;
  setShow();
  if (deep && j >= 0 && POWER[confirm] && !set.sheet) setPowerSheet(confirm);
  setPaint(); setSay(setWhere()); report(); wake();
}
function setClose() {   // Back from the list, Home, or anything that takes the launcher (a launch, the switcher, focus):
  // the launcher comes back with the Settings tile focused; Home during a new resolution's Keep sheet reverts it, and
  // during a build's, discards (the builder waits on that sheet: never on one that isn't there)
  if (!set) return false;
  if (set.sheet?.kind === 'keep') post('/api/display/revert', {});
  if (set.sheet?.kind === 'build') post('/api/build/discard', {});
  clearTimeout(set.followT); clearInterval(set.refT); clearInterval(keepT);
  set = null; screen = 'home';
  stage.classList.remove('set', 'pat'); setEl.classList.remove('sheet'); $('#pattern').hidden = $('#setbye').hidden = true;
  if (cur()?.widget !== 'settings') {
    let at = null; rows.forEach((R, r) => { const i = R.tiles.findIndex(t => t.widget === 'settings'); if (i >= 0 && !at) at = {r, i}; });
    if (at) focus(at.r, at.i, true);
  }
  report(); wake();
  return true;
}
function setShow() {   // the pane shows the list's section now, from what it knows, and asks the daemon afresh
  clearTimeout(set.followT); set.followT = 0;
  setRender(); setLoad(set.shown);
}
async function setLoad(id) {   // (a value SSE brought while the answer was on its way is newer than the answer's)
  const t0 = performance.now(), d = await get(`/api/settings/${id}`);
  if (d?.ok) {
    for (const [k, v] of Object.entries(d.values || {})) if (!(setSeen[k] > t0)) setMerge(k, v);
    setData[id] = d;
  } else if (!setData[id]?.ok) setData[id] = {error: 'SparkHome didn’t answer'};
  if (set?.shown === id) setRender();
}
function setRefresh() {   // every 5 s while open: the sections whose values move by themselves
  if (set && !set.stack && !set.sheet && !set.pattern && ['about', 'controllers', 'sleep'].includes(set.shown)) setLoad(set.shown);
}
const setHeld = k => !!(setBusy[k] || setWait[k]) || performance.now() - (setTouched[k] ?? -1e9) < HOLD;
function setMerge(k, v) { if (!setHeld(k)) setVals[k] = v; }   // the daemon's value, unless the person is changing it
function setValues(vals) {   // SSE settings {values}: a change from voice, an agent, the remote's Vol±, another page
  if (!vals || typeof vals !== 'object') return;
  for (const [k, v] of Object.entries(vals)) { setSeen[k] = performance.now(); setMerge(k, v); }
  if (set) setRender();
}
function setRender() {   // the pane's rows (or the pushed list) and footer, rebuilt only when their text changed. A new
  // section: its own row (remembered), from its top
  const S = SECTIONS[set.sec], d = setData[S.id];
  if (set.shown !== S.id) { set.shown = S.id; set.stack = null; set.k = 0; set.row = set.mem[S.id] ?? 0; set.grab = false; }
  const st = set.stack, snap = set.view !== (set.view = `${S.id} ${st?.key ?? st?.label ?? ''}`);   // (other rows: the
  let html = '', foot = [];                                                            // scroll jumps, never glides)
  if (st?.help) { set.list = []; html = HELP; }
  else if (st) { set.list = st.choices; html = st.choices.map(c => choiceHTML(c, c.value === setVals[st.key])).join(''); foot = st.foot || []; }
  else if (typeof S.rows === 'function' && !d?.ok) { set.list = []; foot = [d?.error ? `Couldn’t read these settings: ${d.error}.` : 'Loading…']; }
  else {
    set.list = rowsOf(S, d); html = set.list.map(R => rowHTML(R, d)).join('');
    foot = d?.error ? [`Couldn’t read these settings: ${d.error}.`] : typeof S.foot === 'function' ? S.foot(d?.info || {}) : S.foot || [];
  }
  txt($('#sethead'), st ? `‹ ${st.label}` : '');
  $('#setpane').classList.toggle('pushed', !!st);
  if (setRows._h !== html) { setRows._h = html; setRows.innerHTML = html; }
  const fh = foot.map(p => `<p>${esc(p)}</p>`).join('');
  if (setFoot._h !== fh) { setFoot._h = fh; setFoot.innerHTML = fh; }
  if (st && !st.help) st.i = Math.max(0, Math.min(st.i, set.list.length - 1));
  else if (!st) { set.row = Math.max(0, Math.min(set.row, set.list.length - 1)); setFill(d); }
  setScroll(snap); setPaint();
}
function setScroll(snap) {   // a long pane (About, the voices) scrolls by whole rows so the focused one stays in view
  const n = set.list.length, i = set.stack ? set.stack.i : set.row, vis = Math.floor((VIEW - (set.stack ? HEAD : 0)) / ROW);
  const body = $('#setbody');
  if (i < set.k) set.k = i; else if (i >= set.k + vis) set.k = i - vis + 1;
  set.k = Math.max(0, Math.min(set.k, n - vis));
  if (snap) body.style.transition = 'none';
  body.style.transform = `translate3d(0,${-set.k * ROW}px,0)`;
  if (snap) { void body.offsetWidth; body.style.transition = ''; }
}
function setPaint() {   // the focused row is a white pill; its section keeps a faint one while focus is in the pane
  const deep = set.at === 'pane' || !!set.stack, fi = set.stack ? set.stack.help ? -1 : set.stack.i : deep ? set.row : -1;
  [...setList.children].forEach((el, j) => {
    el.classList.toggle('on', !deep && j === set.sec); el.classList.toggle('sel', deep && j === set.sec);
    el.setAttribute('aria-selected', j === set.sec);
  });
  [...setRows.children].forEach((el, j) => { el.classList.toggle('on', j === fi); el.classList.toggle('grab', j === fi && set.grab); });
  setEl.classList.toggle('sheet', !!set.sheet);
}
const setSay = s => txt($('#setlive'), s);   // the polite live region: "Display, Brightness, 100 percent"
function setWhere() {
  const S = SECTIONS[set.sec], st = set.stack;
  if (set.pattern) return 'Test pattern. Back to return.';
  if (set.sheet) return [$('#setst').textContent, $('#setsb').textContent, $('#setsheet .pill.on')?.textContent].join(' ');
  if (st?.help) return `${st.label}.`;
  if (st) { const c = st.choices[st.i]; return [st.label, c?.label, c?.detail, c && c.value === setVals[st.key] && 'selected'].filter(Boolean).join(', '); }
  const R = set.at === 'pane' && set.list[set.row], how = R && kind(R, setData[S.id]) === 'slider' &&
    (set.grab ? 'Left or Right to adjust, OK when done' : 'press OK to adjust');
  return R ? [S.label, R.label, shown(R, setData[S.id]).replace(/ %/g, ' percent'), how].filter(Boolean).join(', ') : S.label;
}
function setMove(dir) {
  wake();
  if (set.pattern) return;
  if (set.sheet) { if (dir === 'left' || dir === 'right') setSheetFocus(dir === 'right' ? 1 : 0); return; }
  const step = dir === 'down' || dir === 'right' ? 1 : -1, ud = dir === 'up' || dir === 'down';
  if (set.stack) {
    if (dir === 'left') return setPop();
    const j = set.stack.i + step;
    return !ud || set.stack.help || j < 0 || j >= set.list.length ? setBump(dir) : setFocus(j);
  }
  if (set.at === 'list') {
    if (!ud) return dir === 'right' ? setEnter() : setBump(dir);
    const j = set.sec + step;   // no wrap, as tvOS
    return j < 0 || j >= SECTIONS.length ? setBump(dir) : setSec(j);
  }
  const R = set.list[set.row];
  if (!ud) return set.grab && R && kind(R, setData[set.shown]) === 'slider' ? setSlide(R, step) : dir === 'left' ? setToList()
    : setBump(dir);   // (a slider not taken: as any other row)
  const j = set.row + step;
  return j < 0 || j >= set.list.length ? setBump(dir) : setFocus(j);
}
function setOk() {
  wake();
  if (set.pattern) return setPattern(false);
  if (set.sheet) return setSheetPress(set.sheet.i);
  if (set.stack) return set.stack.help ? setBump() : setChoose(set.stack.i);
  if (set.at === 'list') return setEnter();
  const R = set.list[set.row], k = R && kind(R, setData[set.shown]);
  return k === 'slider' ? setGrab(!set.grab) : k === 'toggle' ? setToggle(R) : k === 'pick' || k === 'modes' ? setPush(R)
    : k === 'action' ? setAction(R) : setBump();
}
function setBack() {   // the pattern, a sheet (Cancel, Revert), a pushed list, a slider taken, the pane, then Settings
  if (set.pattern) return setPattern(false);
  if (set.sheet) return setSheetPress(0);
  if (set.stack) return setPop();
  if (set.grab) return setGrab(false);
  if (set.at === 'pane') return setToList();
  setClose();
}
function setGrab(on) { set.grab = on; setPaint(); setSay(setWhere()); }   // a slider taken (OK) or let go (OK, Back)
function setBump(dir) {   // focus can't go that way (a nudge, as the launcher's), or OK means nothing here (a press)
  const el = set.sheet ? $('#setsheet .pill.on') : set.stack || set.at === 'pane'
    ? setRows.children[set.stack?.help ? 0 : set.stack ? set.stack.i : set.row] : setList.children[set.sec];
  el?.animate(dir ? [{translate: '0 0'}, {translate: NUDGE[dir]}, {translate: '0 0'}] : [{scale: 1}, {scale: .97}, {scale: 1}],
              {duration: dir ? 260 : 200, easing: 'ease-out'});
}
function setSec(j) {   // the list's focus; the pane follows once it rests
  set.sec = j; setPaint(); setSay(setWhere()); report();
  clearTimeout(set.followT); set.followT = setTimeout(setShow, FOLLOW);
}
function setEnter() {   // list → pane, on the row the section had (its first, the first time)
  if (set.shown !== SECTIONS[set.sec].id) setShow();
  if (!set.list.length) return setBump('right');
  set.at = 'pane'; set.row = Math.min(set.mem[set.shown] ?? 0, set.list.length - 1); set.grab = false;
  setScroll(); setPaint(); setSay(setWhere());
}
function setToList() { set.mem[set.shown] = set.row; set.at = 'list'; set.grab = false; setPaint(); setSay(setWhere()); }
function setFocus(j) {   // a row of the pane (a slider taken is let go), or of the pushed list
  if (set.stack) set.stack.i = j; else { set.grab &&= j === set.row; set.mem[set.shown] = set.row = j; }
  setScroll(); setPaint(); setSay(setWhere());
}
function setSlide(R, step) {   // Left/Right on a slider taken: one step, clamped (a bump at its ends)
  const v0 = setVals[R.key], v = num(v0) ? Math.max(R.min, Math.min(R.max, v0 + step * R.step)) : v0;
  if (v === v0) return setBump(step > 0 ? 'right' : 'left');
  setSet(R, v);
}
function setSet(R, v) {   // a person's change: shown at once, then sent (a slider that settles first: Speech volume's
  // preview plays once, at the level it rests on)
  setVals[R.key] = v; setTouched[R.key] = performance.now();
  setRender(); setSay(setWhere());
  if (!R.settle) return setSend(R.key, v, R.preview);
  clearTimeout(setWait[R.key]);
  setWait[R.key] = setTimeout(() => { delete setWait[R.key]; setSend(R.key, setVals[R.key], R.preview); }, R.settle);
}
async function setSend(key, value, preview) {   // one request in flight per key, the latest value going next
  if (setBusy[key]) { setBusy[key].next = {value, preview}; return; }
  const me = setBusy[key] = {next: {value, preview}};
  let r = {ok: true};
  while (me.next && r?.ok) {
    const n = me.next; me.next = null;
    r = await post(`/api/settings/${key}`, n.preview ? {value: n.value, preview: true} : {value: n.value});
  }
  delete setBusy[key];
  const sec = OWNER[key.split('.')[0]];
  if (!r?.ok) {   // the daemon's value back (the checkmark, the slider), and why not
    toast(r?.error || 'SparkHome didn’t make that change', 'error');
    setTouched[key] = -1e9; if (set) setLoad(sec);
  } else {
    setTouched[key] = performance.now();
    if (sec === 'sleep' && set?.shown === 'sleep') setLoad('sleep');   // (its status line)
  }
}
function setToggle(R) { const v = setVals[R.key]; return typeof v === 'boolean' ? setSet(R, !v) : setBump(); }
function setSlideIn(from) {   // a pushed list comes in from the right, the rows it leaves for from the left (tvOS)
  if (!still.matches) $('#setpane').animate([{translate: `${from * 80}px 0`, opacity: 0}, {translate: '0 0', opacity: 1}], {duration: 260, easing: EASE});
}
function setPush(R) {   // a picker's list, focused on the value it has
  const c = setData[set.shown]?.choices?.[R.key];
  if (!Array.isArray(c) || c.length < 2) return setBump();
  set.mem[set.shown] = set.row;
  set.stack = {key: R.key, label: R.label, choices: c, i: Math.max(0, c.findIndex(x => x.value === setVals[R.key])),
               modes: R.type === 'modes', preview: !!R.preview, foot: R.foot};
  setRender(); setSlideIn(1); setSay(setWhere());
}
function setPop() { set.stack = null; setRender(); setSlideIn(-1); setSay(setWhere()); }
async function setChoose(j) {   // OK on a choice: the checkmark moves and it's sent; a resolution asks to keep it (SSE)
  const st = set.stack, c = st.choices[j];
  if (!c) return;
  if (st.modes) {
    if (c.value === setVals[st.key]) return setBump();
    const r = await post('/api/display/apply', {mode: c.value});
    return r?.ok || toast(r?.error || 'The resolution didn’t change', 'error');
  }
  if (c.value === setVals[st.key] && !st.preview) return setBump();   // (a voice again: its preview again)
  setSet({key: st.key, preview: st.preview}, c.value);
}
function setAction(R) {
  if (R.id === 'pattern') return setPattern(true);
  if (R.id === 'help') { set.mem[set.shown] = set.row; set.stack = {help: true, label: 'How to use the remotes', i: 0}; setRender(); setSlideIn(1); return setSay(setWhere()); }
  if (R.id === 'sleep') return Promise.resolve(nudged).then(() => post('/api/sleep/now', {})).then(r => r?.ok ||   // (after
    toast(r?.error || 'The screen didn’t go to sleep', 'error'));   // the keepalive its own key sent: 2 in 20 woke it)
  if (POWER[R.id]) setPowerSheet(R.id);
}
function setPattern(on) {   // the test pattern over the whole stage: OK or Back closes it
  if (!set || set.pattern === on) return;
  set.pattern = on;
  const p = $('#pattern');
  if (on && !p.firstChild) p.innerHTML = PATTERN;
  p.hidden = !on; stage.classList.toggle('pat', on); setSay(setWhere());
}
function setSheet(s, title, body, pills) {   // a modal question, the safe pill (Cancel, Revert) focused first
  set.sheet = {...s, i: 0};
  txt($('#setst'), title); txt($('#setsb'), body);
  $('#setsheet').querySelectorAll('.pill').forEach((p, j) => { p.textContent = pills[j]; p.classList.toggle('force', j === 1 && /^(reboot|shutdown)$/.test(s.action)); });
  setSheetFocus(0); setPaint(); setSay(setWhere());
}
function setSheetFocus(b) { set.sheet.i = b; $('#setsheet').querySelectorAll('.pill').forEach((p, j) => p.classList.toggle('on', j === b)); setSay(setWhere()); }
function setSheetClose() { clearInterval(keepT); set.sheet = null; setPaint(); setSay(setWhere()); }
function setSheetPress(b) {
  const s = set.sheet;
  setSheetClose();
  if (s.kind === 'keep') return post(b ? '/api/display/confirm' : '/api/display/revert', {}).then(r => r?.ok ||
    toast(r?.error || 'The resolution didn’t change', 'error'));
  if (s.kind === 'build') return post(b ? '/api/build/keep' : '/api/build/discard', {}).then(r => r?.ok ||
    toast(r?.error || 'The builder didn’t answer', 'error'));
  if (b) setPower(s.action);
}
function setPowerSheet(action) { const [t, why, go] = POWER[action]; setSheet({kind: 'power', action}, t, why, ['Cancel', go]); }
async function setPower(action) {   // reboot and shutdown say so on the whole stage (the demo only toasts), up before
  // the daemon's toast saying the same, which comes before its answer; it acts a second later, and a failure then is
  // an error toast, which takes the screen down
  const bye = {reboot: 'Restarting the Spark…', shutdown: 'Shutting down…'}[action], b = $('#setbye');
  if (bye && !info.demo) { b.textContent = bye; b.hidden = false; }
  const r = await post(`/api/power/${action}`, {});
  if (!r?.ok) { b.hidden = true; toast(r?.error || 'SparkHome couldn’t do that', 'error'); }
}
function modeLabel(id) {   // "3840x2160@59.940" → its choice's label ("4K · 59.94 Hz"), else the same from the id itself
  const c = setData.display?.choices?.['display.mode'];
  const hit = Array.isArray(c) && c.find(x => x.value === id), m = /^(\d+)x(\d+)@([\d.]+)$/.exec(String(id));
  if (hit) return hit.label;
  if (!m) return String(id ?? 'the new resolution');
  return `${{'3840x2160': '4K', '1920x1080': '1080p', '1280x720': '720p'}[`${m[1]}x${m[2]}`] ?? `${m[1]}×${m[2]}`} · ` +
    `${(+m[3]).toFixed(2).replace(/\.?0+$/, '')} Hz`;
}
function setKeep(d) {   // SSE display pending: "Keep this resolution?" counting down; Revert focused. The person may not
  // see it (no picture): waiting or Back are both safe. The daemon's timer reverts; its word closes the sheet
  if (!set) setOpen('display');
  setPattern(false);
  const end = performance.now() + Math.max(1, +d.seconds || 15) * 1000, lab = modeLabel(d.mode);
  const body = () => { const n = Math.ceil((end - performance.now()) / 1000); return `${lab}. ${n > 0 ? `Switching back in ${n} s.` : 'Switching back…'}`; };
  setSheet({kind: 'keep', mode: d.mode}, 'Keep this resolution?', body(), ['Revert', 'Keep']);
  clearInterval(keepT);
  keepT = setInterval(() => {
    if (set?.sheet?.kind !== 'keep') return clearInterval(keepT);
    txt($('#setsb'), body());
    if (performance.now() > end + 5000) { setSheetClose(); setLoad('display'); }   // its word never came (a restart)
  }, 250);
}
function setBuild(d) {   // SSE build (builder.py): keep is "Keep what it made?", Discard focused; only a person's press
  // keeps (the daemon takes it from this page alone). Any other state closes the sheet: kept, discarded, stopped
  if (d.state === 'keep') {
    if (!set) setOpen('about');
    return setSheet({kind: 'build'}, d.title || 'Keep what the builder made?', d.why || '', ['Discard', 'Keep']);
  }
  if (set?.sheet?.kind === 'build') setSheetClose();
}
function setDisplay(d) {   // SSE display: pending (the sheet), kept, reverted (a toast says to what), failed (the
  // daemon's own error toast says why)
  if (d.state === 'pending') return setKeep(d);
  if (set?.sheet?.kind === 'keep') setSheetClose();
  if (d.state === 'reverted') toast(`Switched back to ${modeLabel(d.mode)}.`);
  if (set) setLoad('display');
}
function setClick(e) {   // pointer: a section or row is focus + OK (on a slider's track: the value there), a pushed list's
  // title goes back, a sheet's pill presses and anywhere else cancels it; a click after the keys moved the focus is OK
  if (stale || set.pattern) return setOk();
  if (set.sheet) { const p = e.target.closest?.('#setsheet .pill'); return p ? setSheetPress(+p.dataset.b) : e.target.closest?.('#setsheet') || setBack(); }
  if (e.target.closest?.('#sethead')) return setPop();
  const sec = e.target.closest?.('#setlist .srow'), row = e.target.closest?.('#setrows .srow');
  if (sec) {
    const j = [...setList.children].indexOf(sec);
    set.at = 'list';
    if (j !== set.sec || set.stack) { set.sec = j; set.stack = null; setShow(); }
    return setOk();
  }
  if (!row) return;
  const j = [...setRows.children].indexOf(row);
  if (set.stack) { set.stack.i = j; setPaint(); return setOk(); }
  clearTimeout(set.followT); set.sec = SECTIONS.findIndex(S => S.id === set.shown);   // (the rows shown, not the list's
  set.at = 'pane'; setFocus(j);                                                      // focus that hadn't moved them yet)
  const R = set.list[j], trk = e.target.closest('.trk');
  if (!trk || kind(R, setData[set.shown]) !== 'slider') return setOk();
  const b = trk.getBoundingClientRect(), v = R.min + Math.round(Math.max(0, Math.min(1, (e.clientX - b.left) / b.width)) * (R.max - R.min) / R.step) * R.step;
  if (v !== setVals[R.key]) setSet(R, v);
}

/* ---------- Top Shelf ---------- */
const PLAY = '<svg viewBox="0 0 10 12"><path d="M0 0v12l10-6z" fill="currentColor"/></svg>';
function paneHTML(t) {
  if (t.type === 'widget') return `<div class="art wbg"></div><div class="scrim"></div><div class="info">${widgetHTML(t.widget)}</div>`;
  const hero = art(t, 'hero'), logo = art(t, 'logo');
  return (hero ? `<div class="art">${img(hero, 'flip')}${img(hero, 'hero')}</div>` : `<div class="art brand"><div class="mark">${esc(t.title)}</div></div>`) + '<div class="scrim"></div>' +
    `<div class="info">${logo ? img(logo, 'logo') : `<h1 class="wm">${esc(t.title)}</h1>`}${t.subtitle ? `<p class="sub">${esc(t.subtitle)}</p>` : ''}` +
    `<div class="acts"><span class="hint">${PLAY}<span class="go">${t.type === 'steam' ? 'Play' : 'Open'}</span><span class="re">Resume</span></span>` +
    `<span class="badge run">Running</span>${t.profile != null ? OPT.replace('"opt"', '"badge opt"') : ''}` +
    `${t.note ? `<span class="badge">${esc(t.note)}</span>` : ''}</div></div>`;
}
let shelfT = 0;
function shelfFor(t) { clearTimeout(shelfT); shelfT = setTimeout(() => showPane(t), shelfId ? 140 : 0); }   // settle before crossfading
function showPane(t) {
  if (!t || (t.id === shelfId && shelf.querySelector('.pane.on'))) return;
  shelfId = t.id;
  const p = document.createElement('div');
  p.className = `pane ${variant(t)}`; p.dataset.id = t.id; p.style.setProperty('--c', color(t)); p.innerHTML = paneHTML(t);
  shelf.append(p);
  paintRunning(); paintStats();
  Promise.all([...p.querySelectorAll('img')].map(im => im.decode().catch(() => {}))).then(() => requestAnimationFrame(() => requestAnimationFrame(() => {
    if (shelfId !== t.id || !p.isConnected) return p.remove();
    for (const o of shelf.querySelectorAll('.pane.on')) { o.classList.remove('on'); setTimeout(() => o.remove(), 900); }
    p.classList.add('on');
  })));
}
document.addEventListener('error', e => {   // a missing art file: drop it and re-render that face / pane
  const im = e.target;
  if (im.classList?.contains('snap')) return im.remove();   // a switcher card's snapshot: its art face stays
  if (!(im instanceof HTMLImageElement) || !im.dataset.u || failed.has(im.dataset.u)) return;
  failed.add(im.dataset.u);
  for (const el of [...rows.flatMap(R => R.els), ...sw?.els || []]) if (el.querySelector(`img[data-u="${CSS.escape(im.dataset.u)}"]`)) {
    el.querySelectorAll('.layer').forEach(x => x.remove());
    el.querySelector('.face').insertAdjacentHTML('afterbegin', faceHTML(el._t, el._k));
  }
  const p = im.closest('.pane');
  if (p && p.dataset.id === shelfId) { shelfId = null; showPane(tileById(p.dataset.id)); }
}, true);

/* ---------- live data ---------- */
function paintRunning() {
  for (const R of rows) for (const el of R.els) { el.classList.toggle('running', running.has(el._t.id)); el.classList.toggle('tab', tabs.has(el._t.id)); }
  for (const p of shelf.querySelectorAll('.pane')) { p.classList.toggle('running', running.has(p.dataset.id)); p.classList.toggle('tab', tabs.has(p.dataset.id)); }
}
function setState(s) {
  if (!s || typeof s !== 'object') return;
  running = new Set(Array.isArray(s.running) ? s.running : []); tabs = new Set(Array.isArray(s.tabs) ? s.tabs : []);
  paintRunning();
  if (sw && s.surface !== 'launcher') swClose();   // the switcher only sits over the launcher (a game's quit prompt came up)
  const act = s.surface === 'web' || s.surface === 'app' ? s.active : null;   // opened by the agent, the phone or another screen:
  if (act && act !== prevActive && !launched && window.SH?.ready && screen === 'home') growId(act);   // grow it from its tile too
  prevActive = act;
  if (launched && !launched.game) {   // the app is up (the TV shows it, or in the demo this splash does), or it never came
    // up. A game's splash follows its launch's stages instead (on 'launch'): Steam logs its process long before it draws
    const up = running.has(launched.id) || tabs.has(launched.id), m = launchEl.querySelector('.msg');
    if (up && m) { launchEl.classList.add('up'); m.textContent = upText(launched.steam); }
    else if (!up && s.surface === 'launcher' && performance.now() - launched.at > 45000) shrink();
  } else if (launched && launchEl.classList.contains('up') && !running.has(launched.id) && s.surface === 'launcher') {
    shrink();   // a game's splash once its game is up (a resume, one opened elsewhere, its 'up'): the game quit
  } else if (!launched) launchEl.classList.remove('up');
  if ((s.voice === 'listening' || s.voice === 'thinking') && !dismissed && !stage.classList.contains('talk')) voice({phase: s.voice});
}
const DIGITS = {'mem.used_gb': 1, load1: 2};
function val(path) { const v = path.split('.').reduce((o, k) => o?.[k], stats); return typeof v === 'number' && isFinite(v) ? v.toFixed(DIGITS[path] ?? 0) : '–'; }
const shortModel = n => { const [a, b] = String(n).split(':'); return b ? `${a}:${b.split('-')[0]}` : a; };
function paintStats() {
  for (const el of document.querySelectorAll('[data-s]')) { const v = val(el.dataset.s); if (el.textContent !== v) el.textContent = v; }
  const u = Math.max(0, Math.min(100, Number(stats.gpu?.util) || 0)), m = stats.mem || {};
  for (const el of document.querySelectorAll('[data-ring]')) { el.setAttribute('stroke-dasharray', `${u} 100`); el.style.strokeOpacity = u >= 1 ? 1 : 0; }
  for (const el of document.querySelectorAll('[data-bar]')) el.style.transform = `scaleX(${m.total_gb > 0 ? Math.min(1, m.used_gb / m.total_gb) : 0})`;
  const models = Array.isArray(stats.models) ? stats.models : null;
  const list = !models ? '<li class="none">–</li>' : !models.length ? '<li class="none">None loaded</li>'
    : models.slice(0, 3).map(n => `<li>${esc(shortModel(n))}</li>`).join('') + (models.length > 3 ? `<li class="none">+${models.length - 3} more</li>` : '');
  for (const el of document.querySelectorAll('[data-models]')) if (el._h !== list) { el._h = list; el.innerHTML = list; }
  const temp = stats.gpu?.temp, chip = $('#chip-gpu');
  chip.hidden = typeof temp !== 'number'; if (!chip.hidden) chip.textContent = `GPU ${Math.round(temp)}°`;
  const mc = $('#chip-model'); mc.hidden = !models?.length;
  if (!mc.hidden) mc.lastChild.textContent = shortModel(models[0]) + (models.length > 1 ? ` +${models.length - 1}` : '');
}
function tick() {
  $('#chip-clock').textContent = new Date().toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'}).replace(/\s?[AP]M$/i, '');
  setTimeout(tick, 60000 - Date.now() % 60000 + 50);
}

/* ---------- overlays: toasts, voice ---------- */
function toast(text, kind) {
  wake();   // #sleep sits above the overlays: a toast must not show under the long-idle dim
  const el = document.createElement('div'), box = $('#toasts');
  el.className = 'toast' + (kind === 'error' ? ' err' : kind === 'warn' ? ' warn' : ''); el.textContent = String(text).slice(0, 240);
  box.prepend(el);
  while (box.children.length > 3) box.lastChild.remove();
  requestAnimationFrame(() => requestAnimationFrame(() => el.classList.add('on')));
  setTimeout(() => { el.classList.remove('on'); setTimeout(() => el.remove(), 450); }, kind === 'error' || kind === 'warn' ? 6000 : 3500);
}
let vT = 0, dismissed = false;   // dismissed: Back closed a listening/thinking overlay; 'state' echoes of it stay closed
const PHASE = {listening: 'Listening…', thinking: 'Thinking…', error: 'Couldn’t do that'};
function voice(d) {
  const ph = d?.phase || 'idle';
  clearTimeout(vT); dismissed = false;
  if (ph === 'idle') return hideVoice();
  if (sw?.voice && ph !== 'thinking') { sw.voice = false; if (ph === 'done') return hideVoice(); }   // the reply of the
  // request that opened the switcher ("show me the apps") is the switcher itself: up 5 s, it would cover the hint
  const v = $('#voice'), tr = $('#vtrans'), re = $('#vreply');
  if (ph === 'listening' || v.dataset.phase === 'idle') { tr.textContent = ''; re.textContent = ''; }
  v.dataset.phase = ph;
  $('#vphase').textContent = PHASE[ph] || '';
  if (d.transcript) tr.textContent = d.transcript;
  if (d.reply) re.textContent = d.reply;
  if (d.error) re.textContent = d.error;
  const said = s => String(s).replace(/[.…]$/, '');   // the reply says it: no toast saying it too ("Closed Netflix")
  if (d.reply) for (const t of [...$('#toasts').children]) if (said(t.textContent) === said(d.reply)) t.remove();
  stage.classList.add('talk'); wake();   // hotkey/pad push-to-talk and agent replies never pass through keydown
  // done/error fade on their own; a stuck listening/thinking phase is dropped after 30 s
  vT = setTimeout(hideVoice, ph === 'done' ? 5000 : ph === 'error' ? 7000 : 30000);
}
function hideVoice() { clearTimeout(vT); stage.classList.remove('talk'); $('#voice').dataset.phase = 'idle'; }
function ptt(on) {   // push-to-talk: the daemon records while held
  if (on === talking) return;
  talking = on;
  post('/api/voice', {action: on ? 'start' : 'stop'});
  if (on) voice({phase: 'listening'});
}

/* ---------- idle: dim unfocused tiles, slide the Dock away, finally dim the screen ---------- */
let idleT = 0, awayT = 0, sleepT = 0;
function wake() {
  if (stage.classList.contains('asleep')) wokeAt = performance.now();
  stage.classList.remove('idle', 'asleep');
  if (away) { away = false; stage.classList.remove('away'); layout(); }
  clearTimeout(idleT); clearTimeout(awayT); clearTimeout(sleepT);
  idleT = setTimeout(() => stage.classList.add('idle'), IDLE_MS);
  awayT = setTimeout(goAway, AWAY_MS);
  // ponytail: plain dim after 10 min instead of a screensaver; add an aerial/photo saver if the TV is OLED and sits on Home
  sleepT = setTimeout(() => stage.classList.add('asleep'), SLEEP_MS);
}

/* ---------- sleep, the daemon's (SPEC.md §3): SSE sleep warning dims the launcher with a countdown (drowsy, over any
   app the daemon's gamma dim covers), asleep blacks it out (dark: the signal goes 1.5 s later), awake lifts both. Its
   own inputs (keys, the Chrome gamepad, the pointer) never pass through the daemon: while dimming or dark, and for 0.7
   s after a wake, they only wake it (V and Y still talk), and they tell the daemon someone is here, POST
   /api/sleep/wake: every 0.3 s at most then, every 15 s otherwise. What comes over SSE is never dropped here: the
   daemon has decided. The 10-minute launcher dim above stays as it is. */
let dozeWoke = -1e9, dozeT = 0, nudgeAt = -1e9, nudged = null;   // nudged: the last POST /api/sleep/wake
const dozing = () => stage.classList.contains('drowsy') || stage.classList.contains('dark') || performance.now() - dozeWoke < 700;
function nudge() {   // the page's own input: true when it only wakes the screen
  const d = dozing(), now = performance.now();
  if (now - nudgeAt >= (d ? 300 : 15000)) { nudgeAt = now; nudged = post('/api/sleep/wake', {}); }
  return d;
}
function doze(d) {   // SSE sleep {state, in?} (on connect: the daemon's whole sleep state, in_s); Settings › Sleep's
  // Status row says it at once, not at its next 5 s refresh
  const was = stage.classList.contains('drowsy') || stage.classList.contains('dark');
  clearInterval(dozeT);
  if (d.state === 'warning') {
    let n = Math.max(1, Math.round(+(d.in ?? d.in_s) || 10));
    txt($('#sleepn'), String(n));
    dozeT = setInterval(() => txt($('#sleepn'), String(n = Math.max(0, n - 1))), 1000);
    stage.classList.add('asleep', 'drowsy'); stage.classList.remove('dark');
  } else if (d.state === 'asleep') {
    stage.classList.add('asleep', 'dark'); stage.classList.remove('drowsy'); hideVoice();
  } else if (was) { stage.classList.remove('drowsy', 'dark'); dozeWoke = wokeAt = performance.now(); wake(); }
  if (set?.shown === 'sleep') setLoad('sleep');
}

function goAway() {   // full-screen Top Shelf; waits out an open panel / voice reply instead of giving up
  if (rows[f.r]?.style !== 'dock') return;
  if (screen !== 'home' || stage.classList.contains('talk')) { awayT = setTimeout(goAway, 2000); return; }
  away = true; stage.classList.add('away'); layout();
}

/* ---------- inputs: keyboard / remote, gamepad, pointer, daemon SSE ---------- */
addEventListener('keydown', e => {
  if (e.repeat && dozing()) return e.preventDefault();   // a held key is one press: held past X's 500 ms delay on Sleep
  // Now, its repeats would wake the screen it just put to sleep
  if (nudge() && e.key !== 'v' && e.key !== 'V') return e.preventDefault();   // the screen dimming or dark: it only wakes
  if (screen === 'command') { if (e.key === 'Escape') { e.preventDefault(); closeCmd(); } return; }
  const d = DIRS[e.key], k = e.key, slept = stage.classList.contains('asleep');
  wake();
  if (d) { if (!e.repeat || performance.now() - lastMove >= 110) move(d); }   // X repeats at 33/s: pace like the pad
  else if (k === 'Enter' || k === ' ') { if (!e.repeat && !slept) ok(); }   // OK on the dimmed screen only wakes it
  else if (k === 'Escape' || k === 'Backspace' || k === 'BrowserBack' || k === 'GoBack') { if (!e.repeat) back(true); }   // (held: one Back)
  else if (k === 'Home' || k === 'BrowserHome' || k === 'GoHome') {   // twice within 300 ms on the launcher: the switcher
    if (!e.repeat && !sw && screen === 'home' && e.timeStamp - homeAt <= 300) { homeAt = -1e9; post('/api/action', {name: 'app_switcher'}); }
    else { if (!e.repeat) homeAt = e.timeStamp; home(); }
  }
  else if (k === 'Delete') { if (sw && !e.repeat) swArm(); }
  else if (k === 'v' || k === 'V') { if (!e.repeat) ptt(true); }
  else if (k === '/') { if (screen === 'home') openCmd(); }   // over Settings, closing the bar would strand the panel
  else if (k === 'MediaPlayPause') post('/api/action', {name: 'media', args: {action: 'toggle'}});
  else return;
  e.preventDefault();
});
let homeAt = -1e9;   // the last Home keydown (e.timeStamp): a press after a double is a first press again
addEventListener('keyup', e => { if ((e.key === 'v' || e.key === 'V') && talking) ptt(false); });
addEventListener('blur', () => { if (talking) ptt(false); });

let padOn = false;
const pads = new Map();
function padLoop(now) {
  let any = false;
  const slept = stage.classList.contains('asleep');
  for (const gp of navigator.getGamepads?.() || []) {
    if (!gp || gp.id.toLowerCase().includes('28de')) continue;   // Steam's virtual pad mirrors the real one
    any = true;
    let st = pads.get(gp.index);
    // ponytail: a new pad's first press counts (Chrome only reveals a pad on a press), so a pad first seen as the kiosk
    // returns from a game with A still held acts on it; note the focus-return time and skip those if that ever bites
    if (!st) pads.set(gp.index, st = {prev: {}, rep: {}});
    if (!document.hasFocus()) { st.prev = null; continue; }   // a game is in front: buttons held on return don't count
    const btn = i => !!gp.buttons[i]?.pressed, ax = gp.axes;
    const dirs = {up: btn(12) || ax[1] < -.5, down: btn(13) || ax[1] > .5, left: btn(14) || ax[0] < -.5, right: btn(15) || ax[0] > .5};
    if (!st.prev) {   // first frame with focus: buttons still held from the game must not count as presses here
      st.prev = {a: btn(0), b: btn(1), guide: btn(16), y: btn(3), x: btn(2), view: btn(8)};
      for (const d in dirs) st.rep[d] = dirs[d] ? Infinity : 0;
    }
    for (const d in dirs) {   // repeat: first after 380 ms, then every 110 ms (the sticks too: the daemon hears them)
      if (!dirs[d]) st.rep[d] = 0;
      else if (!st.rep[d]) { st.rep[d] = now + 380; nudge() || move(d); }
      else if (now >= st.rep[d]) { st.rep[d] = now + 110; nudge() || move(d); }
    }
    const edge = (name, down, onDown, onUp) => {   // (Y talks even on a dark screen: push-to-talk wakes it)
      if (down === !!st.prev[name]) return;
      st.prev[name] = down;
      if (!down) onUp?.(); else if (!nudge() || name === 'y') { wake(); onDown(); }
    };
    edge('a', btn(0), () => slept || ok()); edge('b', btn(1), () => back(true)); edge('guide', btn(16), home);
    edge('y', btn(3), () => ptt(true), () => ptt(false));
    edge('x', btn(2), () => sw && swArm());   // View (the two-windows glyph) opens the switcher; in it, it is B
    edge('view', btn(8), () => sw ? back(true) : screen === 'home' && post('/api/action', {name: 'app_switcher'}));
  }
  padOn = any;
  if (any) requestAnimationFrame(padLoop);
}
addEventListener('gamepadconnected', () => { if (!padOn) { padOn = true; requestAnimationFrame(padLoop); } });
addEventListener('gamepaddisconnected', e => {   // a pad lost while Y is held must not leave push-to-talk recording
  const st = pads.get(e.gamepad?.index); pads.delete(e.gamepad?.index); if (st?.prev?.y) ptt(false);
});

let lastXY = '', mouseT = 0, ptr = null, stale = null, wokeAt = 0;
function unpoint() { stale = ptr; document.body.classList.remove('mouse'); }   // the focus moved without the pointer
addEventListener('mousemove', e => {
  const xy = `${e.screenX},${e.screenY}`; if (xy === lastXY) return;   // ignore synthetic moves from scrolling
  lastXY = xy; ptr = {x: e.screenX, y: e.screenY};
  if (nudge()) return;
  if (stale && Math.hypot(ptr.x - stale.x, ptr.y - stale.y) < 24) return;   // a trackpad tap's jitter keeps the focus
  stale = null;
  document.body.classList.add('mouse'); clearTimeout(mouseT); mouseT = setTimeout(() => document.body.classList.remove('mouse'), 3000);
  wake();
  const el = e.target.closest?.('.tile'), at = el && !sw && screen === 'home' && locate(el), j = sw && !sw.sheet ? sw.els.indexOf(el) : -1;
  if (at) { if (at.r !== f.r || at.i !== f.i) focus(at.r, at.i); }
  else if (j >= 0) swFocus(j);   // a hovered card takes the focus
  else return;
  const b = el.getBoundingClientRect();
  el.style.setProperty('--px', Math.max(-1, Math.min(1, (e.clientX - b.left) / b.width * 2 - 1)).toFixed(3));
  el.style.setProperty('--py', Math.max(-1, Math.min(1, (e.clientY - b.top) / b.height * 2 - 1)).toFixed(3));
  el.classList.add('ptr');
}, {passive: true});
document.addEventListener('mouseout', e => { const el = e.target.closest?.('.tile'); if (el && !el.contains(e.relatedTarget)) el.classList.remove('ptr'); });
addEventListener('click', e => {
  if (nudge()) return;
  if (stage.classList.contains('asleep') || performance.now() - wokeAt < 700) return wake();   // like OK: the dimmed screen only wakes
  if (set) return setClick(e);
  if (sw) return stale ? swOk() : swClick(e);
  if (launched) return;
  if (stale) { if (screen === 'home') ok(); return; }   // the focus moved since the pointer did: a click means OK on it
  const el = e.target.closest?.('.tile'), at = el && locate(el);
  if (at && screen === 'home') { focus(at.r, at.i); activate(); } else wake();
});
addEventListener('contextmenu', e => e.preventDefault());   // a two-finger tap must not open Chrome's menu on the TV

let wheelT = 0;   // a wheel or the phone trackpad's two-finger scroll: one step per 150 ms through rows and tiles
addEventListener('wheel', e => {
  if (nudge() || screen !== 'home' && !sw && !set || e.timeStamp < wheelT || !(e.deltaX || e.deltaY)) return;
  wheelT = e.timeStamp + 150; wake();
  if (sw) return sw.sheet || move((Math.abs(e.deltaX) > Math.abs(e.deltaY) ? e.deltaX : e.deltaY) > 0 ? 'right' : 'left');   // never Up: no arming
  if (set) return set.sheet || move((Math.abs(e.deltaX) > Math.abs(e.deltaY) ? e.deltaX : e.deltaY) > 0 ? 'down' : 'up');
  move(Math.abs(e.deltaY) >= Math.abs(e.deltaX) ? (e.deltaY > 0 ? 'down' : 'up') : (e.deltaX > 0 ? 'right' : 'left'));
}, {passive: true});

let es = null, retry = 1000, offT = 0, lost = false;
function connect() {
  es = new EventSource('/api/events');
  es.onopen = () => {
    clearTimeout(offT); $('#chip-off').hidden = true; retry = 1000;
    if (lost) { lost = false; load(); }   // the daemon may have restarted: new tiles, new PIN
  };
  es.onerror = () => {
    if (!lost) { lost = true; offT = setTimeout(() => { $('#chip-off').hidden = false; }, 3000); }
    if (es.readyState === EventSource.CLOSED) { setTimeout(connect, retry); retry = Math.min(retry * 2, 30000); }
  };
  const on = (type, fn) => es.addEventListener(type, e => {
    let d; try { d = JSON.parse(e.data); } catch { return; }
    try { fn(d && typeof d === 'object' ? d : {}); } catch (err) { console.error('sse', type, err); }
  });
  on('state', setState);
  on('stats', d => { stats = d; paintStats(); });
  on('nav', d => {
    if (!['up', 'down', 'left', 'right'].includes(d.dir)) return;
    if (screen === 'command') closeCmd();
    const up = sw && d.dir === 'up';   // in the switcher Siri up-steps never arm (its close is the flick), and Up arms once
    if (up && d.touch) return;
    for (let n = up ? 1 : Math.max(1, Math.min(20, Math.trunc(d.count) || 1)); n--;) move(d.dir);
  });
  on('switcher', d => d.open ? swOpen(d) : swRefresh(d));
  on('swipe', d => {   // the Siri Remote's fast flick up, once per touch: quit the focused card (never Home, never the sheet)
    if (!sw || sw.sheet || d.dir !== 'up') return;
    if (stage.classList.contains('asleep')) return wake();   // the dimmed screen only wakes, as OK does
    swCloseCard(sw.cards[sw.i]);
  });
  on('focus', d => focusId(d.id));
  on('select', () => stage.classList.contains('asleep') ? wake() : ok());   // the phone's OK on the dimmed screen only wakes it
  on('back', () => back());
  on('home', home);
  on('pointer', demoPointer);
  on('tilt', d => {   // the Siri Remote: a resting finger leans the focused tile toward it (tvOS); 0,0 settles it
    const el = sw ? swCur() : curEl(), x = +d.x || 0, y = +d.y || 0;
    if (!el || (sw ? sw.sheet : screen !== 'home')) return;
    if ((x || y) && stage.classList.contains('asleep')) return;   // no wake: the click that follows would open the tile
    el.classList.toggle('ptr', !!(x || y)); el.style.setProperty('--px', (x * 1.6).toFixed(3)); el.style.setProperty('--py', (-y * 1.6).toFixed(3));
    if (sw) el.style.setProperty('--ly', sw.cards[sw.i].kind === 'home' ? 0 : Math.round(Math.max(0, y) * 40));   // previews the throw
    if (x || y) wake();
  });   // --demo only: no X server, so the launcher draws the phone trackpad's pointer
  on('voice', voice);
  on('toast', d => {   // (the goodbye screen says what the daemon's toast would; its error takes the screen down)
    const bye = $('#setbye');
    if (d.kind === 'error') bye.hidden = true; else if (!bye.hidden) return;
    if (d.text) toast(d.text, d.kind);
  });
  on('sleep', doze);
  on('settings', d => setValues(d.values));
  on('display', setDisplay);
  on('build', setBuild);
  on('open_settings', d => setOpen(typeof d.section === 'string' ? d.section : 'display', d.confirm, true));
  on('launch', d => {   // a game's launch (LAUNCH-DESIGN.md §1.4): starting (grown here if the page didn't, over the
    // switcher or Settings too, which go as on Home), prompt (Steam waits on its window, over the splash), loading (its
    // process), up (it drew: the TV shows it), failed. Before up, the splash has its launch look and summary: OK on the
    // game again while it loads grew it as a resume (Steam's log already lists it as running)
    if (d.stage === 'starting' && launched?.id !== d.id && window.SH?.ready) { closeOverlays(); growId(d.id); }
    if (launched?.id !== d.id) return;
    if (d.stage === 'failed') return shrink();
    const t = tileById(d.id), m = launchEl.querySelector('.msg'), up = d.stage === 'up';
    launchEl.classList.toggle('up', up);
    if (!up && !launchEl.querySelector('.sum')) launchEl.querySelector('.st')?.insertAdjacentHTML('beforebegin', sumHTML(t));
    if (m) m.textContent = {starting: `Starting ${t?.title ?? ''}…`, prompt: 'Waiting for Steam', loading: `Loading ${t?.title ?? ''}…`,
                            up: upText(true)}[d.stage] ?? m.textContent;
  });
  on('tiles', load);
}

async function load() {
  let r = null;
  try { r = await fetch('/api/tiles', {cache: 'no-store'}); } catch {}
  if (r?.status === 401) { $('#lock').hidden = false; return; }
  const d = r?.ok ? await r.json().catch(() => null) : null;
  if (!d) { setTimeout(load, 3000); return; }
  $('#lock').hidden = true;
  render(d);
  setState(await get('/api/state'));
  const s = await get('/api/stats'); if (s) { stats = s; paintStats(); }
}

let vp = {x: 960, y: 540}, vpT = 0;
function demoPointer(d) {
  const c = $('#vcur'), cl = (v, hi) => Math.max(0, Math.min(hi, v));
  vp = {x: cl(vp.x + (d.dx | 0), 1919), y: cl(vp.y + (d.dy | 0), 1079)};
  c.style.transform = `translate(${vp.x}px,${vp.y}px)`; c.hidden = false; clearTimeout(vpT); vpT = setTimeout(() => { c.hidden = true; }, 3000);
  const s = stage.getBoundingClientRect(), k = s.width / 1920, cx = s.left + vp.x * k, cy = s.top + vp.y * k;
  const el = document.elementFromPoint(cx, cy) || document.body, o = {bubbles: true, clientX: cx, clientY: cy, screenX: vp.x, screenY: vp.y};
  if (d.dx || d.dy) el.dispatchEvent(new MouseEvent('mousemove', o));
  for (const [n, ax] of [[d.scroll_y | 0, 'deltaY'], [d.scroll_x | 0, 'deltaX']]) for (let i = Math.abs(n); i--;) el.dispatchEvent(new WheelEvent('wheel', {...o, [ax]: Math.sign(n) * 100}));
  if (d.click === 'left') el.dispatchEvent(new MouseEvent('click', o));
}

function fit() { stage.style.setProperty('--k', Math.min(innerWidth / 1920, innerHeight / 1080)); }
addEventListener('resize', fit);
document.addEventListener('visibilitychange', () => { if (!document.hidden) { shrink(); wake(); get('/api/state').then(setState); } });
window.SH = {ready: false, f: () => ({r: f.r, i: f.i, row: rows[f.r]?.id, id: cur()?.id, k: rows[f.r]?.k, screen}),
  launch: () => launched && {id: launched.id, msg: launchEl.querySelector('.msg')?.textContent ?? null,
                             up: launchEl.classList.contains('up'), sum: launchEl.querySelector('.sum')?.textContent ?? null},
  shelf: () => shelfId, rows: () => rows.map(R => ({id: R.id, n: R.tiles.length, k: R.k, last: R.last})), vp: () => vp,
  sw: () => sw && {keys: sw.cards.map(c => c.key), i: sw.i, armed: sw.armed, sheet: sw.sheet && {...sw.sheet}, from: sw.from,
                   states: sw.cards.map(c => c.state ?? null)}, swOpen, swRefresh,
  settings: () => set && {sec: SECTIONS[set.sec].id, at: set.at, shown: set.shown, loaded: !!setData[set.shown]?.ok, row: set.row, k: set.k,
                          rows: set.list.map(R => R.key ?? R.id ?? R.label ?? R.value), pattern: set.pattern,
                          stack: set.stack && {key: set.stack.key ?? null, i: set.stack.i, help: !!set.stack.help},
                          sheet: set.sheet && {kind: set.sheet.kind, i: set.sheet.i, action: set.sheet.action ?? null},
                          vals: {...setVals}, live: $('#setlive').textContent},
  doze: () => ['drowsy', 'dark', 'asleep'].filter(c => stage.classList.contains(c)), setOpen};
fit(); tick(); connect();
load().then(() => { wake(); window.SH.ready = true; });
