import { expect, test } from 'bun:test';
import { escape, legLabel, money, parseLeg } from './format';
import {
  blockedReason, chart, entryBlock, greekGrid, liveStatus, optionsView, portfolioView, positionSheet, rangeSeries, reviewSheet,
  settingsView, topBar, tradeView, type Chain, type State,
} from './view';

const greeks = { iv: 0.52, delta: 0.0021, gamma: 0.0000004, theta: 0.31, vega: -0.42 };
const candidate = {
  id: 'c1', strategy: 'bull_put', long: 'BTC_USDC-30OCT26-89000-P', short: 'BTC_USDC-30OCT26-90000-P', amount: 0.02,
  width: 1000, credit: 4.4, worst_loss: 16.4, partial_loss: 3.8, costs: 0.8, conservative_pnl: 0.9, created_at: '2026-10-02T12:00:00Z', greeks,
};
function fixture(): State {
  return {
    session: { id: 'session-1', mode: 'paper', created: '2026-09-01T00:00:00Z', initial: 10000, cash: 10010, halt: null, risk: { per_trade: 0.0025, daily_loss: 0.01, drawdown: 0.1 }, max_holding_hours: 24, take_profit_fraction: 0.5, stop_credit_multiple: 2 },
    equity: 10012.5,
    equity_series: [['2026-09-01T00:00:00Z', 10000], ['2026-10-01T12:00:00Z', 10005], ['2026-10-02T11:00:00Z', 10010], ['2026-10-02T12:00:00Z', 10012.5]],
    worker: { state: 'running', responsive: true }, controls: { auto_trade: false, jev_gate: true, enabled: true },
    jev: { available: true, model: 'jev-1.13.0' },
    market: { asof: '2026-10-02T12:00:00Z', age_seconds: 2, fresh: true, source: 'production', index: 84500, quotes: 112 },
    portfolio: { greeks, missing: [] },
    positions: [{ instrument: candidate.short, amount: -0.02, bid: 230, ask: 250, mark: 240, mid_iv: 0.5, greeks }],
    spreads: [{ id: 'sp1', status: 'OPEN', opened: '2026-10-02T10:00:00Z', reserved: 16.4, candidate, held: { [candidate.short]: -0.02 }, greeks, pnl: -1.25 }],
    scan: { timestamp: '2026-10-02T11:00:00Z', count: 1, candidates: [{ ...candidate, jev: { action: 'TRADE', status: 'valid', confidence: 0.86, reasons: [] } }] },
    activity: [{ timestamp: '2026-10-02T11:30:00Z', kind: 'entry_blocked', reason: 'forecast <warming>' }],
    server_time: '2026-10-02T12:00:00Z',
  };
}

test('formatting keeps missing values missing, escapes text and reads Deribit names', () => {
  expect(money(null)).toBe('—');
  expect(money(-3)).toBe('−$3.00');
  expect(money(3, true)).toBe('+$3.00');
  expect(escape('<script>"')).toBe('&lt;script&gt;&quot;');
  expect(parseLeg('BTC_USDC-30OCT26-90000-P')).toEqual({ expiry: '30 Oct', strike: '90k', type: 'Put' });
  expect(legLabel('BTC_USDC-3OCT26-74500-C')).toBe('74,500 Call');
});

test('only fresh production data is labelled live', () => {
  const s = fixture();
  expect(liveStatus(s, '').kind).toBe('live');
  s.market!.fresh = false;
  expect(liveStatus(s, '').text).toContain('stale');
  s.market!.source = 'synthetic';
  expect(liveStatus(s, '')).toEqual({ text: 'SYNTHETIC DATA — not live', kind: 'bad' });
  s.market = null;
  expect(liveStatus(s, '').kind).toBe('bad');
});

test('greeks render in trader units with gamma per $1k', () => {
  const html = greekGrid(greeks);
  expect(html).toContain('52.0%');
  expect(html).toContain('0.0021');
  expect(html).toContain('4.0e-4');
  expect(html).toContain('+$0.31');
  expect(greekGrid(null)).toContain('—');
});

test('portfolio shows balance, ranges and tappable positions', () => {
  const s = fixture();
  const html = portfolioView(s, '1D', null);
  expect(html).toContain('$10,012.50');
  expect(html).toContain('data-spread="sp1"');
  expect(html).toContain('aria-pressed="true">1D');
  expect(rangeSeries(s, '1D')).toHaveLength(3);
  expect(rangeSeries(s, 'All')).toHaveLength(4);
  expect(portfolioView(s, '1D', 0)).toContain('$10,005.00');
  expect(chart([['a', 1]])).toContain('appears after');
});

test('trade ideas show credit and Jev verdict; review blocks while a spread is open', () => {
  const s = fixture();
  expect(tradeView(s)).toContain('Jev ✓ 86%');
  expect(tradeView(s)).toContain('data-idea="c1"');
  expect(reviewSheet(s, s.scan.candidates[0])).not.toContain('data-slide="execute"');
  expect(entryBlock(s)).toContain('One spread at a time');
  s.spreads = [];
  expect(reviewSheet(s, s.scan.candidates[0])).toContain('data-slide="execute"');
  s.controls.enabled = false;
  expect(reviewSheet(s, s.scan.candidates[0])).not.toContain('data-slide="execute"');
});

test('a missing Jev key points to .env', () => {
  const s = fixture();
  s.jev = { available: false, model: 'jev-1.13.0' };
  expect(tradeView(s)).toContain('TYPESAFE_API_KEY to .env');
  expect(settingsView(s, null, '')).toContain('add TYPESAFE_API_KEY to .env');
});

test('position sheet offers slide to close and shows legs', () => {
  const s = fixture();
  const html = positionSheet(s, s.spreads[0]);
  expect(html).toContain('data-slide="close"');
  expect(html).toContain('Short 90k Put');
  expect(html).toContain('−$1.25');
});

test('blocked reasons after the latest scan are shown and escaped', () => {
  const s = fixture();
  expect(blockedReason(s)).toBe('forecast <warming>');
  expect(tradeView(s)).toContain('forecast &lt;warming&gt;');
  s.activity[0].timestamp = '2026-10-02T10:00:00Z';
  expect(blockedReason(s)).toBeUndefined();
});

test('top bar flags halts, stopped workers and exchange modes', () => {
  const s = fixture();
  s.session.halt = 'daily loss limit';
  s.worker = { state: 'stopped', responsive: false };
  s.session.mode = 'live';
  const html = topBar(s, '', 'Portfolio');
  expect(html).toContain('Halted — daily loss limit');
  expect(html).toContain('worker is stopped');
  expect(html).toContain('chip bad">LIVE');
});

test('option chain shows one expiry, filters by side and marks the strike nearest spot', () => {
  const opt = (expiry: string, strike: number, type: 'call' | 'put') => ({
    instrument: `${expiry}-${strike}-${type}`, expiry, strike, type, bid: 100, ask: 110, mark: 105, iv: 0.5, delta: 0.3, valid: true,
  });
  const chain: Chain = { asof: '2026-10-02T12:00:00Z', options: [
    opt('2026-10-09T08:00:00Z', 80000, 'put'), opt('2026-10-09T08:00:00Z', 85000, 'put'), opt('2026-10-09T08:00:00Z', 85000, 'call'),
    opt('2026-10-30T08:00:00Z', 90000, 'put'),
  ] };
  const html = optionsView(chain, fixture(), null, 'put');
  expect(html).toContain('data-expiry="2026-10-30T08:00:00Z"');
  expect(html.match(/class="chain-row(?! head)/g)?.length).toBe(2);
  expect(html).toContain('chain-row atm"><span><strong>85,000');
  expect(optionsView(chain, null, '2026-10-30T08:00:00Z', 'all')).toContain('90,000');
  expect(optionsView({ asof: null, options: [] }, null, null, 'put')).toContain('No option quotes');
});

test('settings offer a theme choice with the current one pressed', () => {
  const html = settingsView(null, null, '', 'dark');
  expect(html).toContain('data-theme="dark" aria-pressed="true"');
  expect(html).toContain('data-theme="system" aria-pressed="false"');
});
