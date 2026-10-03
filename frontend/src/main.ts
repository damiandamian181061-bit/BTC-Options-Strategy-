import './style.css';
import { escape } from './format';
import {
  dock, hero, optionsView, portfolioView, positionSheet, rangeSeries, reviewSheet, settingsView, topBar, tradeView,
  type Candidate, type Chain, type Setup, type State, type Tab, type Theme,
} from './view';

const TITLES: Record<Tab, string> = { portfolio: 'Portfolio', trade: 'Trade', options: 'Options', settings: 'Settings' };
const app = document.querySelector<HTMLDivElement>('#app')!;
const tabFromHash = (): Tab => (location.hash.slice(1) in TITLES ? location.hash.slice(1) : 'portfolio') as Tab;

let tab = tabFromHash(), state: State | null = null, setup: Setup | null = null, error = '';
let range = '1W', scrub: number | null = null, feedback = '';
let sheet: { kind: 'idea' | 'spread'; id: string; candidate?: Candidate } | null = null, sheetFresh = false;
let toast: { text: string; kind: string } | null = null, toastTimer = 0, pending: string | null = null;
let interacting = false;
let chain: Chain | null = null, expiry: string | null = null, side = 'put';

/** The choice is remembered per browser; "system" follows the OS setting. */
let theme: Theme = 'system';
try { theme = (localStorage.getItem('theme') as Theme) || 'system'; } catch { /* storage blocked */ }
function applyTheme() {
  if (theme === 'system') document.documentElement.removeAttribute('data-theme');
  else document.documentElement.dataset.theme = theme;
}
applyTheme();

function sheetContent(): string {
  if (!sheet || !state) return '';
  if (sheet.kind === 'idea') {
    const c = state.scan.candidates.find(x => x.long === sheet!.candidate?.long && x.short === sheet!.candidate?.short) ?? sheet.candidate;
    return c ? reviewSheet(state, c) : '';
  }
  const sp = state.spreads.find(x => x.id === sheet!.id);
  return sp ? positionSheet(state, sp) : '';
}

function render() {
  const body = !state
    ? tab === 'settings' ? settingsView(null, setup, feedback, theme) : tab === 'options' ? optionsView(chain, null, expiry, side) : `<div class="empty pad">${error ? 'No paper session yet. Start the stack, or run <code>uv run btc-options paper</code>.' : 'Loading…'}</div>`
    : tab === 'portfolio' ? portfolioView(state, range, scrub) : tab === 'trade' ? tradeView(state) : tab === 'options' ? optionsView(chain, state, expiry, side) : settingsView(state, setup, feedback, theme);
  const inner = sheetContent();
  app.innerHTML = `${toast ? `<div class="toast glass ${toast.kind}" role="status">${escape(toast.text)}</div>` : ''}
    <main>${topBar(state, error, TITLES[tab])}${body}</main>${dock(tab)}
    ${inner ? `<div class="scrim ${sheetFresh ? 'enter' : ''}" data-dismiss></div><section class="sheet glass ${sheetFresh ? 'enter' : ''}" role="dialog" aria-modal="true"><i class="grab"></i>${inner}</section>` : ''}`;
  sheetFresh = false;
  bindChart();
  bindSliders();
}

function notify(text: string, kind = 'ok', ms = 6000) {
  toast = { text, kind };
  clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => { toast = null; render(); }, ms);
  render();
}

async function post(url: string, body: unknown, headers: Record<string, string> = { 'Content-Type': 'application/json' }) {
  const response = await fetch(url, { method: 'POST', headers, body: body instanceof File ? body : JSON.stringify(body ?? {}), signal: AbortSignal.timeout(60000) });
  const result = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : `Request failed (${response.status})`);
  return result;
}

async function request(url: string, body: unknown, message: string) {
  try {
    const result = await post(url, body);
    pending = result.id ?? null;
    sheet = null;
    notify(message, 'ok', 20000);
  } catch (err) { notify(err instanceof Error ? err.message : 'Request failed', 'bad'); }
  void refresh();
}

async function refresh() {
  try {
    const response = await fetch('/api/state', { signal: AbortSignal.timeout(8000) });
    if (!response.ok) throw new Error(response.status === 404 ? 'No session' : 'Dashboard API unavailable');
    state = await response.json();
    error = '';
  } catch (err) { error = err instanceof Error ? err.message : 'Dashboard API unavailable'; }
  if (tab === 'settings') {
    try { const r = await fetch('/api/setup', { signal: AbortSignal.timeout(8000) }); if (r.ok) setup = await r.json(); } catch { /* keep last */ }
  }
  if (tab === 'options') {
    try { const r = await fetch('/api/chain', { signal: AbortSignal.timeout(8000) }); if (r.ok) chain = await r.json(); } catch { /* keep last */ }
  }
  const done = pending && state?.activity.find(a => a.kind === 'command' && a.id === pending);
  if (done) {
    pending = null;
    notify(done.status === 'accepted' ? `Done — ${done.action === 'enter' ? 'order placed, protection first' : done.action === 'close' ? 'closing, short leg first' : 'session halted'}` : `Not executed — ${done.reason}`, done.status === 'accepted' ? 'ok' : 'bad', 9000);
    return;
  }
  if (!interacting) render();
}

/** Scrubbing the chart updates the balance in place, like a brokerage app. */
function bindChart() {
  const box = document.getElementById('chart');
  if (!box || !state) return;
  const points = rangeSeries(state, range);
  const move = (event: PointerEvent) => {
    const rect = box.getBoundingClientRect(), x = Math.min(Math.max(event.clientX - rect.left, 0), rect.width);
    scrub = Math.round(x / rect.width * (points.length - 1));
    interacting = true;
    document.querySelector('.hero')!.outerHTML = hero(state!, points, scrub);
    document.getElementById('cursor')?.setAttribute('x1', String(scrub / (points.length - 1) * 1000));
    document.getElementById('cursor')?.setAttribute('x2', String(scrub / (points.length - 1) * 1000));
  };
  box.addEventListener('pointermove', move);
  box.addEventListener('pointerdown', move);
  box.addEventListener('pointerleave', () => { scrub = null; interacting = false; render(); });
}

function bindSliders() {
  document.querySelectorAll<HTMLElement>('[data-slide]').forEach(track => {
    const knob = track.querySelector<HTMLElement>('.knob')!;
    let start = 0, offset = 0, max = 0;
    const done = () => { void confirmSlide(track.dataset.slide!); };
    knob.addEventListener('pointerdown', event => {
      interacting = true; start = event.clientX; max = track.clientWidth - knob.offsetWidth - 8;
      knob.setPointerCapture(event.pointerId); track.classList.add('dragging');
    });
    knob.addEventListener('pointermove', event => {
      if (!track.classList.contains('dragging')) return;
      offset = Math.min(Math.max(event.clientX - start, 0), max);
      knob.style.transform = `translateX(${offset}px)`;
      track.style.setProperty('--fill', `${offset + knob.offsetWidth}px`);
    });
    knob.addEventListener('pointerup', () => {
      track.classList.remove('dragging'); interacting = false;
      if (offset >= max * 0.88) { knob.style.transform = `translateX(${max}px)`; track.classList.add('done'); done(); }
      else { knob.style.transform = ''; track.style.removeProperty('--fill'); offset = 0; }
    });
    track.addEventListener('keydown', event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); done(); } });
  });
}

async function confirmSlide(action: string) {
  if (!state) return;
  if (action === 'execute' && sheet?.candidate) {
    const c = state.scan.candidates.find(x => x.long === sheet!.candidate!.long && x.short === sheet!.candidate!.short) ?? sheet.candidate;
    await request('/api/trade', { long: c.long, short: c.short }, state.controls.jev_gate ? 'Sent — Jev and risk are checking on live quotes…' : 'Sent — risk check on live quotes…');
  } else if (action === 'close' && sheet?.kind === 'spread') {
    await request('/api/close', { spread: sheet.id }, 'Close requested…');
  } else if (action === 'halt') {
    await request('/api/halt', {}, 'Halt requested…');
  }
}

app.addEventListener('click', event => {
  const el = (event.target as HTMLElement).closest<HTMLElement>('button, [data-dismiss]');
  if (!el) return;
  if (el.hasAttribute('data-dismiss')) { sheet = null; render(); return; }
  const { range: r, idea, spread, goto, theme: t, expiry: x, side: sd } = el.dataset;
  if (r) { range = r; render(); }
  if (t) { theme = t as Theme; try { localStorage.setItem('theme', theme); } catch { /* storage blocked */ } applyTheme(); render(); }
  if (x) { expiry = x; render(); }
  if (sd) { side = sd; render(); }
  if (goto) location.hash = goto;
  if (idea && state) { const c = state.scan.candidates.find(x => x.id === idea); if (c) { sheet = { kind: 'idea', id: idea, candidate: c }; sheetFresh = true; render(); } }
  if (spread) { sheet = { kind: 'spread', id: spread }; sheetFresh = true; render(); }
});

app.addEventListener('change', event => {
  const el = event.target as HTMLInputElement;
  if (el.dataset.control && state) {
    const next = { auto_trade: state.controls.auto_trade, jev_gate: state.controls.jev_gate, [el.dataset.control]: el.checked };
    post('/api/controls', next)
      .then(() => notify(`Auto-trade ${next.auto_trade ? 'on' : 'off'} · Jev approval ${next.jev_gate ? 'required' : 'off'}`))
      .catch(err => notify(err.message, 'bad'))
      .finally(() => void refresh());
  }
  if (el.id === 'history-file' && el.files?.[0]) {
    const file = el.files[0];
    if (file.size > 16 * 1024 * 1024) { notify('Choose a CSV smaller than 16 MiB.', 'bad'); return; }
    post('/api/setup/history', file, { 'Content-Type': 'text/csv', 'X-File-Name': encodeURIComponent(file.name) })
      .then(r => { feedback = `Imported ${r.observations} observations.`; notify(feedback); })
      .catch(err => notify(err.message, 'bad'));
  }
});

document.addEventListener('keydown', event => { if (event.key === 'Escape' && sheet) { sheet = null; render(); } });
window.addEventListener('hashchange', () => { tab = tabFromHash(); sheet = null; scrub = null; window.scrollTo(0, 0); render(); void refresh(); });
render();
void refresh();
setInterval(() => { if (!document.hidden) void refresh(); }, 4000);
