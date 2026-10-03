import { escape as e, finite, money, num, parseLeg, pct, sig, strategyName, when } from './format';

export type Greeks = { iv: number | null; delta: number; gamma: number; theta: number; vega: number };
export type Judgment = { action: string; status: string; confidence: number | null; reasons: string[] };
export type Candidate = {
  id: string; strategy: string; long: string; short: string; amount: number; width: number;
  credit: number; worst_loss: number; partial_loss: number; costs: number; conservative_pnl: number;
  created_at: string; greeks: Greeks | null; jev?: Judgment | null;
};
export type Spread = {
  id: string; status: string; opened: string; reserved: number; candidate: Candidate;
  held: Record<string, number>; greeks: Greeks | null; pnl: number | null;
};
export type Leg = {
  instrument: string; amount: number; bid: number | null; ask: number | null; mark: number | null;
  mid_iv: number | null; greeks: Greeks | null;
};
export type Activity = { timestamp: string; kind: string; [key: string]: any };
export type State = {
  session: { id: string; mode: string; created: string; initial: number; cash: number; halt: string | null; risk: Record<string, number>; max_holding_hours: number; take_profit_fraction: number; stop_credit_multiple: number };
  equity: number; equity_series: [string, number][];
  worker: { state: string; responsive: boolean } | null;
  controls: { auto_trade: boolean; jev_gate: boolean; enabled: boolean };
  jev: { available: boolean; model: string } | null;
  market: { asof: string; age_seconds: number; fresh: boolean; source: string; index: number | null; quotes: number } | null;
  portfolio: { greeks: Greeks; missing: string[] };
  positions: Leg[]; spreads: Spread[];
  scan: { timestamp: string | null; count: number; candidates: Candidate[] };
  activity: Activity[]; server_time: string;
};
export type Setup = {
  managed: boolean; controls_enabled: boolean;
  services: Record<string, { state: string; responsive: boolean }>;
  market: { fresh: boolean; quotes: number; age_seconds?: number };
  history: { completed_days: number; required_days: number; ready: boolean; reason?: string };
  history_import: { name: string; observations: number } | null;
  jev: { configured: boolean; model: string };
};
export type Tab = 'portfolio' | 'trade' | 'options' | 'settings';
export type Option = {
  instrument: string; expiry: string; strike: number; type: 'call' | 'put';
  bid: number; ask: number; mark: number; iv: number; delta: number; valid: boolean;
};
export type Chain = { asof: string | null; options: Option[] };
export type Theme = 'system' | 'light' | 'dark';
export const THEMES: [Theme, string][] = [['system', 'Auto'], ['light', 'Light'], ['dark', 'Dark']];
export const RANGES = ['1D', '1W', '1M', 'All'] as const;

const ICONS: Record<string, string> = {
  portfolio: '<path d="M3 17l5-5 4 4 8-9"/><path d="M14 7h6v6"/>',
  options: '<path d="M8 6h12M8 12h12M8 18h12"/><path d="M4 6h.01M4 12h.01M4 18h.01"/>',
  trade: '<path d="M7 4v16M7 4L3 8M7 4l4 4"/><path d="M17 20V4M17 20l-4-4M17 20l4-4"/>',
  settings: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>',
  up: '<path d="M7 17L17 7M9 7h8v8"/>',
  down: '<path d="M7 7l10 10M17 9v8H9"/>',
  arrow: '<path d="M5 12h14M13 6l6 6-6 6"/>',
  check: '<path d="M5 12l5 5L20 7"/>',
  chevron: '<path d="M9 6l6 6-6 6"/>',
};
export const icon = (name: string) => `<svg class="icon" viewBox="0 0 24 24" aria-hidden="true">${ICONS[name] ?? ''}</svg>`;
const tone = (v: unknown) => finite(v) ? (v > 0 ? 'up' : v < 0 ? 'down' : '') : '';
const chip = (text: string, kind = '') => `<span class="chip ${kind}">${e(text)}</span>`;
const row = (label: string, value: string) => `<div class="kv"><span>${e(label)}</span><strong>${value}</strong></div>`;

/** Gamma is shown as the delta change for a $1,000 move, which is readable for BTC. */
export const GREEKS: [string, string, (g: Greeks) => string][] = [
  ['IV', 'implied vol', g => pct(g.iv)],
  ['Δ', 'delta · BTC', g => sig(g.delta)],
  ['Γ', 'gamma · per $1k', g => sig(g.gamma * 1000)],
  ['Θ', 'theta · $/day', g => `<span class="${tone(g.theta)}">${money(g.theta, true)}</span>`],
  ['V', 'vega · $/vol pt', g => money(g.vega, true)],
];
export function greekGrid(g: Greeks | null | undefined): string {
  return `<div class="greeks">${GREEKS.map(([sym, label, f]) => `<div><span class="sym">${sym}</span><strong>${g ? f(g) : '—'}</strong><small>${label}</small></div>`).join('')}</div>`;
}

export function liveStatus(s: State | null, error: string): { text: string; kind: string } {
  if (error && !s) return { text: error, kind: 'bad' };
  const m = s?.market;
  if (!m) return { text: 'No market data', kind: 'bad' };
  if (m.source !== 'production') return { text: `${m.source.toUpperCase()} DATA — not live`, kind: 'bad' };
  if (!m.fresh) return { text: `Deribit stale · ${num(m.age_seconds, 0)}s`, kind: 'warn' };
  return { text: `Live · Deribit · ${num(m.age_seconds, 0)}s`, kind: 'live' };
}

export function topBar(s: State | null, error: string, title: string): string {
  const live = liveStatus(s, error);
  return `<header class="top"><h1>${e(title)}</h1><div class="pills">
    ${s && s.session.mode !== 'paper' ? chip(s.session.mode.toUpperCase(), 'bad') : chip('Paper', 'glass')}
    <span class="chip glass ${live.kind}"><i class="dot"></i>${e(live.text)}</span></div></header>
    ${s?.session.halt ? `<div class="banner bad glass">Halted — ${e(s.session.halt)}. No new entries this session.</div>` : ''}
    ${s && !s.worker?.responsive ? `<div class="banner warn glass">The paper worker is ${e(s.worker?.state ?? 'not running')}. Showing the last recorded state.</div>` : ''}`;
}

export function rangeSeries(s: State, range: string): [string, number][] {
  const days = ({ '1D': 1, '1W': 7, '1M': 30 } as Record<string, number>)[range];
  if (!days) return s.equity_series;
  const cutoff = Date.parse(s.server_time) - days * 86400000;
  const inRange = s.equity_series.filter(p => Date.parse(p[0]) >= cutoff);
  return inRange.length > 1 ? inRange : s.equity_series.slice(-2);
}

export function chart(points: [string, number][]): string {
  const values = points.map(p => p[1]);
  if (values.length < 2) return '<div class="chart empty-chart">Your equity curve appears after a few minutes of trading.</div>';
  const lo = Math.min(...values), hi = Math.max(...values), span = hi - lo || 1;
  // A flat curve sits mid-chart rather than on the floor.
  const y = (v: number) => hi === lo ? 90 : 8 + (1 - (v - lo) / span) * 164;
  const path = values.map((v, i) => `${i ? 'L' : 'M'}${(i / (values.length - 1) * 1000).toFixed(1)},${y(v).toFixed(1)}`).join('');
  return `<div class="chart" id="chart"><svg viewBox="0 0 1000 180" preserveAspectRatio="none" role="img" aria-label="Equity from ${money(values[0])} to ${money(values.at(-1))}">
    <line class="base" x1="0" x2="1000" y1="${y(values[0]).toFixed(1)}" y2="${y(values[0]).toFixed(1)}"/>
    <path class="line" d="${path}"/><line class="cursor" id="cursor" x1="-10" x2="-10" y1="0" y2="180"/></svg></div>`;
}

export function hero(s: State, points: [string, number][], scrub: number | null): string {
  const base = points[0]?.[1] ?? s.session.initial;
  const value = scrub !== null && points[scrub] ? points[scrub][1] : s.equity;
  const change = value - base, rel = base ? change / base : 0;
  const label = scrub !== null && points[scrub] ? when(points[scrub][0]) : 'in this period';
  return `<section class="hero"><span class="label">Portfolio · USDC</span><div class="balance">${money(value)}</div>
    <div class="delta ${tone(change)}">${icon(change >= 0 ? 'up' : 'down')}${money(change, true)} <span>(${pct(rel, 2)})</span> <small>${e(label)}</small></div></section>`;
}

function spreadIcon(strategy: string) {
  return `<span class="avatar ${strategy === 'bull_put' ? 'up' : 'down'}">${icon(strategy === 'bull_put' ? 'up' : 'down')}</span>`;
}
function spreadTitle(c: Candidate) {
  const s = parseLeg(c.short), l = parseLeg(c.long);
  return { title: `${strategyName(c.strategy)}`, sub: `${s.expiry} · ${s.strike} / ${l.strike} ${s.type} · ${num(c.amount, 2)} BTC` };
}

export function portfolioView(s: State, range: string, scrub: number | null): string {
  const points = rangeSeries(s, range), open = s.spreads.filter(x => x.status !== 'CLOSED'), closed = s.spreads.filter(x => x.status === 'CLOSED');
  const positionRows = open.map(sp => {
    const t = spreadTitle(sp.candidate);
    return `<button class="item" data-spread="${e(sp.id)}">${spreadIcon(sp.candidate.strategy)}<span class="main"><strong>${e(t.title)}</strong><small>${e(t.sub)}</small></span>
      <span class="side"><strong class="${tone(sp.pnl)}">${money(sp.pnl, true)}</strong><small>${sp.status === 'OPEN' ? `Θ ${money(sp.greeks?.theta, true)}/day` : e(sp.status.toLowerCase())}</small></span>${icon('chevron')}</button>`;
  }).join('');
  return `${hero(s, points, scrub)}
  ${chart(points)}
  <div class="segmented glass" role="tablist">${RANGES.map(r => `<button data-range="${r}" aria-pressed="${r === range}">${r}</button>`).join('')}</div>
  <section class="card glass"><div class="card-head"><h2>Greeks</h2><small>portfolio · live marks</small></div>${greekGrid(s.portfolio.greeks)}
    ${s.portfolio.missing.length ? `<p class="note warn">No quote for ${e(s.portfolio.missing.join(', '))}.</p>` : ''}</section>
  <section class="card glass"><div class="card-head"><h2>Positions</h2><small>${money(s.session.cash)} cash</small></div>
    ${positionRows || `<div class="empty">No open positions.<button class="link" data-goto="trade">Find a trade ${icon('arrow')}</button></div>`}</section>
  ${closed.length ? `<section class="card glass"><div class="card-head"><h2>Closed</h2></div>${closed.slice(0, 6).map(sp => { const t = spreadTitle(sp.candidate); return `<div class="item static">${spreadIcon(sp.candidate.strategy)}<span class="main"><strong>${e(t.title)}</strong><small>${e(t.sub)}</small></span><span class="side"><strong class="${tone(sp.pnl)}">${money(sp.pnl, true)}</strong><small>${when(sp.opened, false)}</small></span></div>`; }).join('')}</section>` : ''}
  ${activityCard(s)}`;
}

export function jevChip(j: Judgment | null | undefined): string {
  if (!j) return chip('Jev —', 'glass');
  const conf = finite(j.confidence) ? ` ${pct(j.confidence, 0)}` : '';
  return chip(`Jev ${j.action === 'TRADE' ? '✓' : '✕'}${conf}`, j.action === 'TRADE' ? 'live' : 'warn');
}

export function blockedReason(s: State): string | undefined {
  const scanned = Date.parse(s.scan.timestamp ?? '1970-01-01');
  return s.activity.find(a => a.kind === 'entry_blocked' && Date.parse(a.timestamp) >= scanned)?.reason;
}

export function entryBlock(s: State): string | null {
  if (!s.controls.enabled) return 'Trading controls are off for this dashboard.';
  if (s.session.halt) return 'This session is halted.';
  if (s.spreads.some(x => x.status !== 'CLOSED')) return 'One spread at a time — close the open position first.';
  return null;
}

export function tradeView(s: State): string {
  const c = s.controls, blocked = blockedReason(s);
  const toggle = (key: string, on: boolean, label: string, help: string) => `<label class="switch-row"><span><strong>${label}</strong><small>${help}</small></span>
    <input type="checkbox" role="switch" data-control="${key}" ${on ? 'checked' : ''} ${c.enabled ? '' : 'disabled'}><i class="switch"></i></label>`;
  const ideas = s.scan.candidates.map(x => {
    const t = spreadTitle(x);
    return `<button class="item" data-idea="${e(x.id)}">${spreadIcon(x.strategy)}<span class="main"><strong>${e(t.title)}</strong><small>${e(t.sub)}</small></span>
      <span class="side"><strong class="up">${money(x.credit, true)}</strong>${jevChip(x.jev)}</span>${icon('chevron')}</button>`;
  }).join('');
  return `<section class="card glass">
    ${toggle('auto_trade', c.auto_trade, 'Auto-trade', 'Enter the best idea automatically each scan')}
    ${toggle('jev_gate', c.jev_gate, 'Require Jev approval', 'Every entry needs a confident TRADE from Jev')}
    ${c.jev_gate && !s.jev?.available ? '<p class="note warn">No Jev key loaded — add TYPESAFE_API_KEY to .env. Gated entries are skipped until then.</p>' : ''}
  </section>
  <section class="card glass"><div class="card-head"><h2>Ideas</h2><small>${s.scan.timestamp ? `${s.scan.count} passed · ${when(s.scan.timestamp)}` : 'waiting for the first scan'}</small></div>
    ${blocked ? `<p class="note warn">${e(blocked)}</p>` : ''}
    ${ideas || '<div class="empty">No spread passes the strategy and risk filters right now.</div>'}
    <p class="note">Credit shown is what you receive. Execute re-prices on live quotes, asks Jev if approval is on, then runs the risk check and buys protection first.</p>
  </section>
  ${activityCard(s, ['command', 'jev', 'risk'])}`;
}

export function slider(action: string, label: string, kind = '', disabled: string | null = null): string {
  if (disabled) return `<p class="note center">${e(disabled)}</p>`;
  return `<div class="slider ${kind}" data-slide="${e(action)}" tabindex="0" role="button" aria-label="${e(label)} (press Enter to confirm)">
    <span class="slider-label">${e(label)}</span><span class="knob">${icon('arrow')}</span></div>`;
}

export function reviewSheet(s: State, c: Candidate): string {
  const sh = parseLeg(c.short), lo = parseLeg(c.long);
  return `<div class="sheet-head"><small>Review order</small><button class="close" data-dismiss aria-label="Close">✕</button></div>
    <div class="sheet-hero">${spreadIcon(c.strategy)}<div><strong>${e(strategyName(c.strategy))}</strong><small>${e(sh.expiry)} expiry · ${num(c.amount, 2)} BTC</small></div></div>
    <div class="big up">${money(c.credit, true)}<small>credit received</small></div>
    <div class="legs"><div><span class="tag sell">Sell</span>${e(sh.strike)} ${e(sh.type)}</div><div><span class="tag buy">Buy</span>${e(lo.strike)} ${e(lo.type)} <small>protection</small></div></div>
    <div class="kvs">${row('Max loss', money(c.worst_loss))}${row('Est. costs', money(c.costs))}${row('Edge after costs', money(c.conservative_pnl, true))}${row('Exit rules', `${pct(s.session.take_profit_fraction, 0)} profit · ${num(s.session.stop_credit_multiple, 1)}× stop · ${s.session.max_holding_hours}h`)}</div>
    ${greekGrid(c.greeks)}
    <div class="jev-box"><div>${jevChip(c.jev)}<strong>${c.jev ? (c.jev.action === 'TRADE' ? 'Jev approves this idea' : 'Jev would skip this idea') : 'Not reviewed by Jev yet'}</strong></div>
      <small>${s.controls.jev_gate ? 'Approval required: Jev re-checks on execution.' : 'Advisory only — approval is not required.'} Confidence is not a win probability.</small></div>
    ${slider('execute', 'Slide to execute', '', entryBlock(s))}`;
}

export function positionSheet(s: State, sp: Spread): string {
  const c = sp.candidate, sh = parseLeg(c.short), lo = parseLeg(c.long);
  const legs = s.positions.filter(p => p.instrument in sp.held);
  return `<div class="sheet-head"><small>Position</small><button class="close" data-dismiss aria-label="Close">✕</button></div>
    <div class="sheet-hero">${spreadIcon(c.strategy)}<div><strong>${e(strategyName(c.strategy))}</strong><small>${e(sh.expiry)} · ${e(sh.strike)} / ${e(lo.strike)} ${e(sh.type)} · ${num(c.amount, 2)} BTC</small></div></div>
    <div class="big ${tone(sp.pnl)}">${money(sp.pnl, true)}<small>profit &amp; loss · ${e(sp.status.toLowerCase())}</small></div>
    <div class="kvs">${row('Credit', money(c.credit))}${row('Max loss', money(c.worst_loss))}${row('Reserved risk', money(sp.reserved))}${row('Opened', when(sp.opened))}</div>
    ${greekGrid(sp.greeks)}
    ${legs.map(p => { const l = parseLeg(p.instrument); return `<div class="kv"><span>${p.amount < 0 ? 'Short' : 'Long'} ${e(l.strike)} ${e(l.type)}</span><strong>${money(p.bid)} / ${money(p.ask)} <small>IV ${pct(p.greeks?.iv)} · mid ${pct(p.mid_iv)}</small></strong></div>`; }).join('')}
    ${sp.status === 'OPEN' || sp.status === 'OPENING' ? slider('close', 'Slide to close', 'danger', s.controls.enabled ? null : 'Trading controls are off for this dashboard.') : '<p class="note center">Closing — the short leg is bought back first.</p>'}`;
}

const LABELS: Record<string, string> = {
  command: 'Request', risk: 'Risk check', entry_blocked: 'Entry blocked', jev: 'Jev', jev_advisory: 'Jev on position',
  management_error: 'Management error', settlement: 'Settlement', forecast_unavailable: 'Forecast',
};
function activityText(a: Activity): string {
  if (a.kind === 'command') return `${a.action} ${a.status}${a.reason ? ` — ${a.reason}` : ''}`;
  if (a.kind === 'risk') return a.approved ? 'approved' : `rejected — ${(a.reasons ?? []).join(', ')}`;
  if (a.kind === 'jev' || a.kind === 'jev_advisory') return `${a.action} · ${a.status}${finite(a.confidence) ? ` · ${pct(a.confidence, 0)}` : ''}`;
  return a.reason ?? a.instrument ?? '';
}
export function activityCard(s: State, kinds?: string[]): string {
  const items = s.activity.filter(a => !kinds || kinds.includes(a.kind)).slice(0, 8);
  if (!items.length) return '';
  return `<section class="card glass"><div class="card-head"><h2>Activity</h2></div>${items.map(a => `<div class="act"><span class="main"><strong>${e(LABELS[a.kind] ?? a.kind)}</strong><small>${e(activityText(a))}</small></span><time>${when(a.timestamp)}</time></div>`).join('')}</section>`;
}

/** One expiry at a time; Deribit's IV and mark are quoted as given. */
export function optionsView(chain: Chain | null, s: State | null, expiry: string | null, side: string): string {
  if (!chain) return '<div class="empty pad">Loading options…</div>';
  if (!chain.options.length) return '<div class="empty pad">No option quotes yet. They appear once market data is recorded.</div>';
  const expiries = [...new Set(chain.options.map(o => o.expiry))];
  const pick = expiry && expiries.includes(expiry) ? expiry : expiries[0];
  const rows = chain.options.filter(o => o.expiry === pick && (side === 'all' || o.type === side));
  const spot = s?.market?.index;
  const atm = finite(spot) && rows.length ? rows.reduce((a, b) => Math.abs(b.strike - spot) < Math.abs(a.strike - spot) ? b : a).instrument : null;
  const sides = [['put', 'Puts'], ['call', 'Calls'], ['all', 'All']];
  return `<div class="scroller" role="tablist" aria-label="Expiry">${expiries.map(x => `<button class="chip glass" data-expiry="${e(x)}" aria-pressed="${x === pick}">${when(x, false)}</button>`).join('')}</div>
  <div class="segmented glass" role="tablist">${sides.map(([k, label]) => `<button data-side="${k}" aria-pressed="${k === side}">${label}</button>`).join('')}</div>
  <section class="card glass chain"><div class="card-head"><h2>${when(pick, false)}</h2><small>${rows.length} options${finite(spot) ? ` · BTC ${money(spot)}` : ''}</small></div>
    <div class="chain-row head"><span>Strike</span><span>Bid</span><span>Ask</span><span>IV</span></div>
    ${rows.map(o => `<div class="chain-row${o.instrument === atm ? ' atm' : ''}${o.valid ? '' : ' stale'}"><span><strong>${e(o.strike.toLocaleString('en-US'))}</strong>${side === 'all' ? ` <small>${o.type === 'put' ? 'P' : 'C'}</small>` : ''}</span><span>${money(o.bid)}</span><span>${money(o.ask)}</span><span>${pct(o.iv)}</span></div>`).join('')}
  </section>
  <p class="note center">Prices in USDC · updated ${when(chain.asof)}</p>`;
}

export function settingsView(s: State | null, d: Setup | null, feedback: string, theme: Theme = 'system'): string {
  const status = (ok: boolean, label: string, text: string) => `<div class="act"><span class="main"><strong><i class="dot ${ok ? 'ok' : 'off'}"></i>${e(label)}</strong><small>${e(text)}</small></span></div>`;
  const live = liveStatus(s, '');
  const h = d?.history, svc = (n: string) => d?.services[n];
  return `<section class="card glass"><div class="card-head"><h2>Appearance</h2></div>
    <div class="segmented glass wide">${THEMES.map(([k, label]) => `<button data-theme="${k}" aria-pressed="${k === theme}">${label}</button>`).join('')}</div></section>
  <section class="card glass"><div class="card-head"><h2>Connections</h2></div>
    ${status(live.kind === 'live', 'Deribit market data', s?.market ? `${s.market.source} · ${s.market.quotes} options · BTC ${money(s.market.index)}` : 'no snapshot yet')}
    ${status(Boolean(s?.worker?.responsive), 'Paper worker', s?.worker?.state ?? 'not started')}
    ${status(Boolean(s?.jev?.available ?? d?.jev.configured), 'Jev', s?.jev?.available ? `${s.jev.model} · key from .env` : 'add TYPESAFE_API_KEY to .env, then restart')}
    ${h ? status(h.ready, 'Forecast history', h.ready ? `${h.completed_days} days of Deribit BTC history` : `${h.completed_days} / ${h.required_days} days${h.reason ? ` — ${h.reason}` : ''}`) : ''}
    ${d?.managed && svc('collector') ? status(Boolean(svc('collector')?.responsive), 'Recorder', svc('collector')!.state) : ''}
  </section>
  ${s ? `<section class="card glass"><div class="card-head"><h2>Session</h2><small>${e(s.session.mode)} · ${e(s.session.id.slice(0, 8))}</small></div>
    <div class="kvs">${row('Started with', money(s.session.initial))}${row('Risk per trade', pct(s.session.risk.per_trade, 2))}${row('Daily loss halt', pct(s.session.risk.daily_loss, 1))}${row('Drawdown halt', pct(s.session.risk.drawdown, 0))}</div>
    ${s.session.halt ? '' : slider('halt', 'Slide to halt & flatten', 'danger', s.controls.enabled ? null : 'Trading controls are off for this dashboard.')}</section>` : ''}
  ${d?.controls_enabled ? `<section class="card glass"><div class="card-head"><h2>Extra history</h2><small>optional</small></div>
    <p class="note">History is backfilled from Deribit automatically. You can also add a trusted UTF-8 <code>timestamp,price</code> CSV.</p>
    <label class="file glass">Choose CSV<input id="history-file" type="file" accept=".csv,text/csv"></label>
    ${d.history_import ? `<p class="note">Last import: ${e(d.history_import.name)} · ${d.history_import.observations} rows</p>` : ''}</section>` : ''}
  ${feedback ? `<p class="note center">${e(feedback)}</p>` : ''}`;
}

export function dock(tab: Tab): string {
  const items: [Tab, string][] = [['portfolio', 'Portfolio'], ['trade', 'Trade'], ['options', 'Options'], ['settings', 'Settings']];
  return `<nav class="dock glass">${items.map(([id, label]) => `<a href="#${id}" ${id === tab ? 'aria-current="page"' : ''}>${icon(id)}<span>${label}</span></a>`).join('')}</nav>`;
}
