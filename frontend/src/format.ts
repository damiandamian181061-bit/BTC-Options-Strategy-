export const finite = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v);

export function escape(value: unknown): string {
  return String(value ?? '—').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]!);
}

export function money(v: unknown, signed = false): string {
  if (!finite(v)) return '—';
  const text = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 2, minimumFractionDigits: 2 }).format(Math.abs(v));
  return (v < 0 ? '−' : signed && v > 0 ? '+' : '') + text;
}

export const num = (v: unknown, digits = 2) => finite(v) ? v.toFixed(digits) : '—';
/** Small Greeks keep significant digits instead of rounding to zero. */
export const sig = (v: unknown) => !finite(v) ? '—' : v === 0 || Math.abs(v) >= 1e-3 ? v.toFixed(4) : v.toExponential(1);
export const pct = (v: unknown, digits = 1) => finite(v) ? `${(v * 100).toFixed(digits)}%` : '—';

export function when(v: unknown, time = true): string {
  const d = new Date(String(v));
  if (Number.isNaN(d.getTime())) return '—';
  return d.toLocaleString('en-GB', { day: 'numeric', month: 'short', ...(time ? { hour: '2-digit', minute: '2-digit' } : {}) });
}

const MONTHS: Record<string, string> = { JAN: 'Jan', FEB: 'Feb', MAR: 'Mar', APR: 'Apr', MAY: 'May', JUN: 'Jun', JUL: 'Jul', AUG: 'Aug', SEP: 'Sep', OCT: 'Oct', NOV: 'Nov', DEC: 'Dec' };

/** "BTC_USDC-23OCT26-90000-P" -> { expiry: "23 Oct", strike: "90k", type: "Put" }. */
export function parseLeg(name: string) {
  const [, date = '', strike = '', kind = ''] = String(name).split('-');
  const m = /^(\d{1,2})([A-Z]{3})(\d{2})$/.exec(date);
  const k = Number(strike);
  return {
    expiry: m ? `${m[1]} ${MONTHS[m[2]] ?? m[2]}` : date,
    strike: Number.isFinite(k) && k > 0 ? (k % 1000 === 0 ? `${k / 1000}k` : k.toLocaleString('en-US')) : strike,
    type: kind === 'P' ? 'Put' : kind === 'C' ? 'Call' : kind,
  };
}
export const legLabel = (name: string) => { const l = parseLeg(name); return `${l.strike} ${l.type}`; };

export const strategyName = (s: string) => ({ bull_put: 'Bull put spread', bear_call: 'Bear call spread' } as Record<string, string>)[s] ?? s;
