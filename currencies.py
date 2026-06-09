"""Load and cache per-exchange currency metadata: networks, contracts, fees, deposit/withdraw status."""
import asyncio


async def load_currencies(ex):
    """Returns dict: base_code -> {networks: {net_name: {contract, withdraw_fee, deposit, withdraw}}}.
    Returns {} if exchange doesn't support fetch_currencies or call fails."""
    try:
        if not ex.has.get("fetchCurrencies"):
            return {}
        data = await ex.fetch_currencies()
    except Exception:
        return {}

    out = {}
    if not data:
        return {}
    for code, c in data.items():
        if not isinstance(c, dict):
            continue
        nets = {}
        networks = c.get("networks") or {}
        for net_name, n in networks.items():
            if not isinstance(n, dict):
                continue
            info = n.get("info") or {}
            contract = (
                n.get("contract")
                or n.get("address")
                or info.get("contract")
                or info.get("contractAddress")
                or info.get("contract_address")
                or ""
            )
            if isinstance(contract, str):
                contract = contract.lower().strip()
            withdraw_fee = n.get("fee")
            if withdraw_fee is None:
                withdraw_fee = (n.get("limits", {}) or {}).get("withdraw", {}).get("fee")
            deposit = n.get("deposit")
            withdraw = n.get("withdraw")
            active = n.get("active", True)
            nets[net_name.upper()] = {
                "contract": contract,
                "withdraw_fee": withdraw_fee,
                "deposit": deposit if deposit is not None else active,
                "withdraw": withdraw if withdraw is not None else active,
            }
        # Top-level deposit/withdraw flags as fallback
        top_deposit = c.get("deposit", True)
        top_withdraw = c.get("withdraw", True)
        top_fee = c.get("fee")
        out[code.upper()] = {
            "networks": nets,
            "deposit": top_deposit,
            "withdraw": top_withdraw,
            "fee": top_fee,
        }
    return out


async def load_all_currencies(exchanges):
    """Returns dict: ex_id -> currencies dict."""
    results = await asyncio.gather(
        *(load_currencies(ex) for ex in exchanges), return_exceptions=True
    )
    out = {}
    for ex, res in zip(exchanges, results):
        if isinstance(res, Exception):
            out[ex.id] = {}
        else:
            out[ex.id] = res
    return out


def can_transfer(currencies_map, ex_buy, ex_sell, base_code):
    """Check if base coin can be withdrawn from ex_buy AND deposited to ex_sell.
    Returns (ok, reason, common_networks_with_contracts)."""
    base_code = base_code.upper()
    cb = currencies_map.get(ex_buy, {}).get(base_code)
    cs = currencies_map.get(ex_sell, {}).get(base_code)
    if not cb or not cs:
        return None, "no_currency_data", []  # unknown — don't reject

    if cb.get("withdraw") is False:
        return False, f"{ex_buy} withdraw disabled", []
    if cs.get("deposit") is False:
        return False, f"{ex_sell} deposit disabled", []

    nets_b = cb.get("networks", {}) or {}
    nets_s = cs.get("networks", {}) or {}

    # Find common networks where both sides are operational
    common = []
    for net_name, nb in nets_b.items():
        ns = nets_s.get(net_name)
        if not ns:
            continue
        if nb.get("withdraw") is False or ns.get("deposit") is False:
            continue
        common.append((net_name, nb, ns))

    if not common and (nets_b or nets_s):
        return False, "no common operational network", []

    return True, "ok", common


def best_withdraw_fee(common_networks):
    """Pick cheapest withdraw fee across common networks. Returns (fee, network) or (None, None)."""
    best = None
    best_net = None
    for net_name, nb, _ns in common_networks:
        fee = nb.get("withdraw_fee")
        if fee is None:
            continue
        try:
            fee = float(fee)
        except (TypeError, ValueError):
            continue
        if best is None or fee < best:
            best = fee
            best_net = net_name
    return best, best_net


def contracts_match(common_networks):
    """For anti-fake: does at least one common network have matching non-empty contract addresses?
    Returns (matched: bool|None, evidence: str). None = unknown (no contract data)."""
    saw_any_contract = False
    for net_name, nb, ns in common_networks:
        cb = (nb.get("contract") or "").lower().strip()
        cs = (ns.get("contract") or "").lower().strip()
        if cb and cs:
            saw_any_contract = True
            if cb == cs:
                return True, f"contract match on {net_name}: {cb[:10]}..."
    if not saw_any_contract:
        return None, "no contract data"
    return False, "contracts differ on all common networks"
