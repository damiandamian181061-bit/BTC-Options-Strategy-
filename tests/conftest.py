from datetime import datetime, timezone, timedelta

import pytest

from btc_options import demo
from btc_options.config import Settings
from btc_options.domain import SpreadCandidate
from btc_options.storage import Ledger
from btc_options.strategy import fee


@pytest.fixture(autouse=True)
def no_real_jev_key(tmp_path, monkeypatch):
    """The project .env holds a real key; tests must never reach TypeSafe with it."""
    monkeypatch.setenv("BTC_ENV_FILE", str(tmp_path / "absent.env"))
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)


@pytest.fixture
def now():
    return datetime(2026, 10, 2, 12, tzinfo=timezone.utc)


@pytest.fixture
def settings(tmp_path):
    return Settings(runtime_dir=tmp_path, jev_mode="off", evaluation_seconds=1)


@pytest.fixture
def market(now):
    return demo.snapshot(now)


@pytest.fixture
def ledger(settings, now):
    value = Ledger(settings.runtime_dir / "ledger.sqlite")
    yield value
    value.close()


@pytest.fixture
def session(ledger, settings, now):
    return ledger.start(settings, now=now)


@pytest.fixture
def candidate(market, settings):
    names = [
        n
        for n, i in market.instruments.items()
        if i.option_type == "put" and i.strike in (89000, 90000)
    ]
    names = sorted(
        names, key=lambda n: (market.instruments[n].expiry, market.instruments[n].strike)
    )[:2]
    long, short = names
    lq, sq = market.quotes[long], market.quotes[short]
    amount = 0.01
    credit = (sq.bid - lq.ask) * amount
    costs = sum(fee(p, sq.index, amount, settings) for p in (lq.bid, lq.ask, sq.bid, sq.ask))
    return SpreadCandidate(
        snapshot_id=market.id,
        strategy="bull_put",
        long=long,
        short=short,
        amount=amount,
        width=1000,
        credit=credit,
        worst_loss=10 - credit + costs,
        partial_loss=lq.ask * amount + costs,
        costs=costs,
        expected_pnl=0.2,
        conservative_pnl=0.1,
        tail_loss=10 - credit,
        delta_notional=10,
        vega=0.5,
        model_versions={"fixture": "synthetic-test"},
        created_at=market.asof,
    )


def advance(snapshot, seconds=1):
    next_time = snapshot.asof + timedelta(seconds=seconds)
    quotes = {
        n: q.model_copy(update={"receive_time": next_time, "exchange_time": next_time})
        for n, q in snapshot.quotes.items()
    }
    return snapshot.model_copy(update={"asof": next_time, "quotes": quotes})
