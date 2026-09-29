"""
Data layer for the BTC options study.

Two sources:
1. generate_synthetic_market() — builds a realistic "market" implied vol
   surface by simulating prices from a Bates model with known ground-truth
   parameters plus pricing noise. This lets you validate that calibration
   actually recovers sensible parameters before trusting it on real data,
   and it's what this project runs on by default (no internet access
   required, fully reproducible).

2. fetch_deribit_data() — a template for pulling REAL BTC options data from
   Deribit's public market-data API (no auth needed). Run this on your own
   machine (this sandboxed environment has no general internet access) to
   swap in live data. Deribit is the dominant BTC options venue, so this
   is the standard source for a project like this.
"""
import numpy as np
import pandas as pd
from models.bates import bates_prices_for_strikes
from models.black_scholes import implied_vol


# ---------------------------------------------------------------------
# 1. Synthetic market (default — runs anywhere, no internet needed)
# ---------------------------------------------------------------------
TRUE_PARAMS = dict(
    v0=0.70 ** 2, kappa=3.0, theta=0.65 ** 2, sigma_v=0.9, rho=-0.6,
    lambda_j=0.6, mu_j=-0.06, delta_j=0.12,
)
MATURITIES = [7 / 365, 30 / 365, 90 / 365]           # 1w, 1m, 3m
MONEYNESS = [0.8, 0.9, 0.95, 1.0, 1.05, 1.1, 1.2]    # K / S0


def generate_synthetic_market(S0=60000, r=0.0, seed=7):
    """
    Simulate a BTC options 'market' using Bates as the true data-generating
    process. Ground-truth parameters are chosen to look like real BTC vol
    dynamics: high vol-of-vol, negative spot-vol correlation (leverage
    effect), and a modest negative jump component (crash risk), which
    together produce a realistic negative-skewed implied vol smile.
    r=0.0 because BTC options are coin-margined on Deribit (quoted and
    settled in BTC), so there's no conventional fiat risk-free carry term.

    Uses OTM options only, matching real market convention: puts for
    strikes below spot, calls for strikes above spot. This isn't just
    realism for its own sake — it's numerically necessary. Deep ITM options
    are almost pure intrinsic value with very little vega, so inverting
    their price back to an implied vol is unstable (tiny price noise ->
    huge IV swings); OTM options are where the market's actual volatility
    information lives, which is why every real exchange quotes vol
    surfaces this way.
    """
    rng = np.random.default_rng(seed)
    rows = []
    for T in MATURITIES:
        K_arr = S0 * np.array(MONEYNESS)
        option_types = np.where(np.array(MONEYNESS) < 1.0, "put", "call")

        call_mask = option_types == "call"
        put_mask = ~call_mask
        true_prices = np.empty_like(K_arr)
        if call_mask.any():
            true_prices[call_mask] = bates_prices_for_strikes(
                S0, K_arr[call_mask], T, r, option_type="call", **TRUE_PARAMS)
        if put_mask.any():
            true_prices[put_mask] = bates_prices_for_strikes(
                S0, K_arr[put_mask], T, r, option_type="put", **TRUE_PARAMS)

        noise = rng.normal(0, np.maximum(true_prices * 0.003, 1.0))
        mkt_prices = np.maximum(true_prices + noise, 0.01)
        for m, K, price, otype in zip(MONEYNESS, K_arr, mkt_prices, option_types):
            iv = implied_vol(price, S0, K, T, r, otype)
            if not np.isnan(iv):
                rows.append(dict(T=T, K=K, moneyness=m, option_type=otype,
                                  market_price=price, market_iv=iv))

    return pd.DataFrame(rows), S0, r, TRUE_PARAMS


# ---------------------------------------------------------------------
# 2. Real data (run locally — this sandbox can't reach deribit.com)
# ---------------------------------------------------------------------
def fetch_deribit_market(currency="BTC", min_days=1, max_days=180):
    """
    Pull a LIVE BTC options chain (calls and puts) from Deribit's public API
    and return it in the exact same shape as generate_synthetic_market():
    (df, S0, r) with df columns [T, K, moneyness, option_type, market_price,
    market_iv, instrument] — so it's a drop-in replacement in main.py.

    Run this on a machine with normal internet access (this sandbox can't
    reach deribit.com). No API key needed — it's a public endpoint.

    min_days/max_days filter out same-day-expiry (noisy, huge gamma) and
    very long-dated (thin, wide-spread) instruments, which is standard
    practice for this kind of calibration.
    """
    import requests
    from datetime import datetime, timezone

    url = "https://www.deribit.com/api/v2/public/get_book_summary_by_currency"
    resp = requests.get(url, params={"currency": currency, "kind": "option"}, timeout=15)
    resp.raise_for_status()
    result = resp.json()["result"]

    now = datetime.now(timezone.utc)
    parsed = []
    for entry in result:
        # instrument names look like "BTC-27JUN26-70000-C" (C=call, P=put)
        parts = entry["instrument_name"].split("-")
        if len(parts) != 4 or parts[3] not in ("C", "P"):
            continue
        try:
            expiry = datetime.strptime(parts[1], "%d%b%y").replace(
                hour=8, tzinfo=timezone.utc)  # Deribit settles 08:00 UTC
        except ValueError:
            continue
        T = (expiry - now).total_seconds() / (365.25 * 24 * 3600)
        if not (min_days / 365.25 <= T <= max_days / 365.25):
            continue

        K = float(parts[2])
        option_type = "call" if parts[3] == "C" else "put"
        mark_price_btc = entry.get("mark_price")
        underlying = entry.get("underlying_price")
        if mark_price_btc is None or underlying is None or mark_price_btc <= 0:
            continue
        parsed.append(dict(instrument=entry["instrument_name"], T=T, K=K,
                            option_type=option_type, underlying_price=underlying,
                            market_price=mark_price_btc * underlying))

    if not parsed:
        raise RuntimeError("No usable instruments returned — check currency/day filters.")

    raw = pd.DataFrame(parsed)
    S0 = raw["underlying_price"].median()  # single reference spot for the whole snapshot
    r = 0.0  # coin-margined, no fiat risk-free carry — see note in generate_synthetic_market

    rows = []
    for row in raw.itertuples():
        iv = implied_vol(row.market_price, S0, row.K, row.T, r, row.option_type)
        if not np.isnan(iv):
            rows.append(dict(T=row.T, K=row.K, moneyness=row.K / S0,
                              option_type=row.option_type, market_price=row.market_price,
                              market_iv=iv, instrument=row.instrument))

    return pd.DataFrame(rows), S0, r


def fetch_deribit_data(currency="BTC"):
    """
    Raw (unparsed) pull of the same endpoint, kept for reference / debugging.
    Prefer fetch_deribit_market() above, which returns T already computed
    and is a drop-in replacement for generate_synthetic_market() in main.py.
    """
    import requests

    url = "https://www.deribit.com/api/v2/public/get_book_summary_by_currency"
    resp = requests.get(url, params={"currency": currency, "kind": "option"}, timeout=15)
    resp.raise_for_status()
    result = resp.json()["result"]

    rows = []
    for entry in result:
        # instrument names look like "BTC-27JUN26-70000-C" (C=call, P=put)
        parts = entry["instrument_name"].split("-")
        if len(parts) != 4 or parts[3] not in ("C", "P"):
            continue
        K = float(parts[2])
        option_type = "call" if parts[3] == "C" else "put"
        mark_price_btc = entry.get("mark_price")
        underlying = entry.get("underlying_price")
        if mark_price_btc is None or underlying is None:
            continue
        rows.append(dict(
            instrument=entry["instrument_name"],
            K=K,
            option_type=option_type,
            underlying_price=underlying,
            market_price=mark_price_btc * underlying,  # Deribit quotes options in BTC
        ))
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------
# 3. Manual UI export (Deribit's website table, copy-pasted or downloaded
#    as .tsv/.csv/.xlsx) — no API access needed at all, just the browser.
#    This is what actually got used for this project's real-data results:
#    the sandbox this was built in has no network route to deribit.com,
#    but a person can always just visit the site and export the table.
# ---------------------------------------------------------------------
_MANUAL_EXPORT_COLS = [
    "instrument", "volume", "open_interest", "ext_value", "rho", "theta", "vega",
    "gamma", "ndelta", "delta_pct", "last", "size1", "iv_bid", "bid", "mark",
    "ask", "iv_ask", "size2",
]


def _estimate_forward(df):
    """Interpolate the delta-implied forward price for one expiry: the strike
    where call delta crosses 0.5. Deliberately NOT the same as spot — BTC
    forwards carry a cost-of-carry/funding premium that grows with tenor
    (this project saw ~$84k near-term vs ~$95k a year out on the same day),
    so each expiry needs its own forward, not one shared spot."""
    calls = df[df["instrument"].str.endswith("-C")].copy()
    calls["K"] = calls["instrument"].str.split("-").str[2].astype(float)
    calls["delta_pct"] = pd.to_numeric(calls["delta_pct"], errors="coerce")
    calls = calls.dropna(subset=["delta_pct"]).sort_values("K")
    above = calls[calls.delta_pct >= 0.5].tail(1)
    below = calls[calls.delta_pct < 0.5].head(1)
    if len(above) and len(below):
        k1, d1 = above.K.values[0], above.delta_pct.values[0]
        k2, d2 = below.K.values[0], below.delta_pct.values[0]
        return k1 + (0.5 - d1) * (k2 - k1) / (d2 - d1)
    return calls["K"].median()


def load_manual_export(path, expiry_utc, now_utc=None, r=0.0):
    """
    Parse ONE expiry's worth of Deribit's UI-exported options table (.tsv,
    .csv, or .xlsx — same 18-column layout the site exports) into the same
    shape generate_synthetic_market() and fetch_deribit_market() produce:
    (df, S0, r), with df columns [T, K, moneyness, option_type, market_price,
    market_iv], ready to hand straight to calibrate.py / report.py.

    expiry_utc: a timezone-aware datetime for the expiry (08:00 UTC is
    Deribit's standard settlement time).
    now_utc: snapshot time; defaults to datetime.now(timezone.utc).

    Applies the OTM convention (puts below the forward, calls above) for
    the same numerical-stability reason as elsewhere in this project, and
    prices against the DELTA-IMPLIED FORWARD for this expiry (see
    _estimate_forward), not a shared spot price.
    """
    from datetime import datetime, timezone

    if now_utc is None:
        now_utc = datetime.now(timezone.utc)

    if str(path).lower().endswith(".xlsx"):
        raw = pd.read_excel(path)
    else:
        raw = pd.read_csv(path, sep="\t")
    raw.columns = _MANUAL_EXPORT_COLS
    for c in ["mark", "ask", "bid", "iv_bid", "iv_ask", "delta_pct"]:
        raw[c] = pd.to_numeric(raw[c], errors="coerce")

    S0 = _estimate_forward(raw)
    T = (expiry_utc - now_utc).total_seconds() / (365.25 * 24 * 3600)

    parts = raw["instrument"].str.split("-", expand=True)
    raw["K"] = parts[2].astype(float)
    raw["option_type"] = parts[3].map({"C": "call", "P": "put"})
    raw["price_btc"] = raw["mark"].fillna(raw["ask"])
    raw["market_price"] = raw["price_btc"] * S0
    raw["moneyness"] = raw["K"] / S0
    keep = ((raw.moneyness < 1) & (raw.option_type == "put")) | \
           ((raw.moneyness >= 1) & (raw.option_type == "call"))
    otm = raw[keep].copy()

    rows = []
    for row in otm.itertuples():
        iv = implied_vol(row.market_price, S0, row.K, T, r, row.option_type)
        if not np.isnan(iv):
            rows.append(dict(T=T, K=row.K, moneyness=row.moneyness, option_type=row.option_type,
                              market_price=row.market_price, market_iv=iv))

    return pd.DataFrame(rows), S0, r


def load_multi_expiry(paths_and_expiries, now_utc=None, r=0.0):
    """
    Combine several load_manual_export() calls into ONE multi-maturity
    DataFrame with a per-row 'S0' column (the delta-implied forward for
    that row's own expiry) — the exact shape calibrate.py and report.py
    expect for a real term-structure study spanning multiple expiries with
    different forwards.

    paths_and_expiries: list of (path, expiry_utc) tuples.
    Returns: (df, r) — no single S0, since each maturity carries its own.
    """
    frames = []
    for path, expiry_utc in paths_and_expiries:
        df, S0, _ = load_manual_export(path, expiry_utc, now_utc=now_utc, r=r)
        df["S0"] = S0
        frames.append(df)
    return pd.concat(frames, ignore_index=True), r
