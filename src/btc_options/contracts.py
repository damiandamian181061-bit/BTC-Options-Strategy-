"""Contract payoff units. Execution accepts BTC-underlying USDC linear options only."""


def expiry_payoff(strike, delivery_price, option_type, settlement="USDC"):
    if min(strike, delivery_price) <= 0 or option_type not in ("call", "put"):
        raise ValueError("invalid payoff domain")
    value = (
        max(delivery_price - strike, 0)
        if option_type == "call"
        else max(strike - delivery_price, 0)
    )
    if settlement == "USDC":
        return value
    if settlement == "BTC":
        return value / delivery_price
    raise ValueError("unsupported settlement currency")
