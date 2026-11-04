#!/usr/bin/python3
"""
Basic Swap Test Scenario
Run swap tests using existing pool liquidity without adding new liquidity.
"""

from decimal import Decimal

from brownie import accounts, chain, Contract, network,web3
from brownie.exceptions import VirtualMachineError

TRADE_RATIOS = {
    "SMALL": Decimal("0.005"),  # 0.5%
    "MEDIUM": Decimal("0.02"),  # 2%
    "LARGE": Decimal("0.05"),   # 5%
}

BASE_ETH_DEPOSIT = Decimal("10")


def run_scenario(detector):
    """Execute swap scenarios based on the current pool state."""
    results = []
    block_num = detector.blocknum
    block_gas_limit = web3.eth.get_block(block_num)["gasLimit"]
    quote_addr = detector.quote_token_address or detector.weth.address
    quote_symbol, quote_decimals = identify_quote(detector, quote_addr)
    token_decimals = detector.results.get("token_info", {}).get("decimals", 18)

    token_reserve_raw = get_token_reserve(detector)
    token_reserve_dec = Decimal(token_reserve_raw) / (Decimal(10) ** token_decimals)

    # Prepare all test accounts upfront (ETH -> WETH -> Quote conversion once per account)
    prepared_accounts = []
    for idx in range(len(TRADE_RATIOS)):
        test_account = accounts[idx]
        quote_token, quote_balance_raw = prepare_account_quote(detector, test_account, quote_addr, quote_symbol,block_gas_limit)
        prepared_accounts.append((test_account, quote_token, quote_balance_raw))

    for idx, (label, ratio) in enumerate(TRADE_RATIOS.items()):
        test_account, quote_token, quote_balance_raw = prepared_accounts[idx]
        result = test_buy_sell(
            detector,
            test_account,
            label,
            ratio,
            quote_token,
            quote_balance_raw,
            quote_symbol,
            quote_decimals,
            token_reserve_dec,
            token_decimals,
            block_gas_limit
        )
        results.append(result)

    # Results will be analyzed and printed by ScamAnalyzer.analyze_results()

    return results


def prepare_account_quote(detector, account, quote_addr, quote_symbol,block_gas_limit):
    ensure_eth_balance(account, BASE_ETH_DEPOSIT)
    eth_amount_wei = to_wei(BASE_ETH_DEPOSIT)
    print(f"\n--- initialise account {account.address} ---")
    print(f"  deposit ETH: {BASE_ETH_DEPOSIT:.6f}")
    detector.weth.deposit({"from": account, "value": eth_amount_wei, 'gas_limit':block_gas_limit})

    deadline = chain.time() + 300

    if quote_addr.lower() == detector.weth.address.lower():
        quote_token = detector.weth
        quote_balance = quote_token.balanceOf(account.address)
        print(f"  quote balance: {from_wei(quote_balance):.6f} WETH")
        return quote_token, quote_balance

    print(f"  swap WETH -> {quote_symbol}")

    if detector.dex_version == "v3":
        detector.weth.approve(detector.v3_router_addr, eth_amount_wei, {"from": account,'gas_limit':block_gas_limit})

        params = (
            detector.weth.address,
            detector.quote_token_address,
            3000,
            account.address,
            deadline,
            eth_amount_wei,
            0,
            0
        )

        detector.v3_router.exactInputSingle(params, {"from": account,"gas_limit": block_gas_limit})
    else:
        detector.weth.approve(detector.router_addr, eth_amount_wei, {"from": account,'gas_limit':block_gas_limit})
        detector.router.swapExactTokensForTokensSupportingFeeOnTransferTokens(
            eth_amount_wei,
            0,
            [detector.weth.address, detector.quote_token_address],
            account.address,
            deadline,
            {"from": account,"gas_limit": block_gas_limit}
        )

    quote_token = detector.quote_token
    quote_balance = quote_token.balanceOf(account.address)
    print(f"  quote balance (raw): {quote_balance}")
    return quote_token, quote_balance


def test_buy_sell(
    detector,
    test_account,
    label,
    ratio,
    quote_token,
    quote_balance_raw,
    quote_symbol,
    quote_decimals,
    token_reserve_dec,
    token_decimals,
    block_gas_limit
):
    target_tokens_dec = token_reserve_dec * ratio
    print(f"\n{'='*60}")
    print(f"{label} Test | target tokens: {target_tokens_dec:.6f}")
    print(f"{'='*60}")

    quote_addr = detector.weth.address if quote_symbol == "WETH" else detector.quote_token_address
    path_quote_to_token = [quote_addr, detector.token_address]
    token_target_raw = int(target_tokens_dec * (Decimal(10) ** token_decimals))

    try:
        if detector.dex_version == "v3":
            quote_result = detector.v3_quoter.quoteExactOutputSingle(
                quote_addr,
                detector.token_address,
                token_target_raw,
                detector.fee_tier,
                0
            )
            quote_needed_raw = quote_result[0]
        else:
            quote_needed_raw = detector.router.getAmountsIn(token_target_raw, path_quote_to_token)[0]
    except Exception:
        # Fallback: estimate from recorded reserves (prevents hard stop)
        liq_info = detector.results.get("liquidity_info", {})
        quote_reserve_raw = liq_info.get("quote_reserve") or 0
        token_reserve_raw = liq_info.get("token_reserve") or 0
        if quote_reserve_raw > 0 and token_reserve_raw > 0:
            # simple proportion with 20% buffer
            quote_needed_raw = int(token_target_raw * quote_reserve_raw / token_reserve_raw * 1.2)
            print("⚠️  Quoter failed; using reserve-based estimate.")
        else:
            print("⚠️  Failed to determine required quote amount. Skipping test.")
            return {
                "test_name": label,
                "quote_symbol": quote_symbol,
                "quote_spent": 0.0,
                "quote_received": 0.0,
                "tokens_target": float(target_tokens_dec),
                "tokens_received": 0.0,
                "recovery_rate": None,
                "buy_success": False,
                "sell_success": False,
                "sell_fail":2,
                "messages": ["Quote calculation failed"],
            }

    # If required quote exceeds available balance, scale token target down to fit balance
    if quote_needed_raw > quote_balance_raw and quote_balance_raw > 0:
        scale = Decimal(quote_balance_raw) / Decimal(quote_needed_raw)
        token_target_raw = max(1, int(token_target_raw * float(scale) * 0.98))  # 2% buffer
        target_tokens_dec = Decimal(token_target_raw) / (Decimal(10) ** token_decimals)
        quote_needed_raw = quote_balance_raw
        print(
            f"⚠️  Target reduced to fit balance: {target_tokens_dec:.6f} tokens using "
            f"{Decimal(quote_needed_raw) / (Decimal(10) ** quote_decimals):.6f} {quote_symbol}"
        )

    quote_needed_raw = min(quote_needed_raw, quote_balance_raw)
    quote_needed_dec = Decimal(quote_needed_raw) / (Decimal(10) ** quote_decimals)
    print(f"Quote required: {quote_needed_dec:.6f} {quote_symbol}")

    deadline = chain.time() + 300

    print("[1/2] Buy token")

    # Try to buy tokens - catch revert
    sell_fail = 0
    buy_success = False
    token_balance = 0
    token_balance_dec = Decimal(0)

    try:
        if detector.dex_version == "v3":
            quote_token.approve(detector.v3_router_addr, quote_needed_raw, {"from": test_account, 'gas_limit':block_gas_limit})

            params = (
                quote_addr,
                detector.token_address,
                detector.fee_tier,
                test_account.address,
                deadline,
                quote_needed_raw,
                0,
                0
            )

            tx = detector.v3_router.exactInputSingle(params, {"from": test_account, 'gas_limit':block_gas_limit})
        else:
            quote_token.approve(detector.router_addr, quote_needed_raw, {"from": test_account, 'gas_limit':block_gas_limit})
            tx = detector.router.swapExactTokensForTokensSupportingFeeOnTransferTokens(
                    quote_needed_raw,
                    0,
                    path_quote_to_token,
                    test_account.address,
                    deadline,
                    {"from": test_account ,'gas_limit':block_gas_limit}
                )

        network.web3.provider.make_request("anvil_mine", [1])
        tx.wait(1)

        token_balance = detector.token.balanceOf(test_account.address)
        token_balance_dec = Decimal(token_balance) / (Decimal(10) ** token_decimals)
        print(f"  ✅ buy complete | token balance: {token_balance_dec:.6f}")
        buy_success = True

    except (VirtualMachineError, ValueError, Exception) as e:
        error_msg = str(e) if str(e) else "Transaction reverted"
        sell_fail = 1
        print(f"  ❌ buy failed | reason: {error_msg}")
        return {
            "test_name": label,
            "quote_symbol": quote_symbol,
            "quote_spent": float(quote_needed_dec),
            "quote_received": 0.0,
            "tokens_target": float(target_tokens_dec),
            "tokens_received": 0.0,
            "recovery_rate": None,
            "buy_success": False,
            "sell_success": False,
            "sell_fail":sell_fail,
            "messages": [f"Buy transaction failed: {error_msg}"],
        }

    # Measure Quote token balance before sell (to calculate actual proceeds excluding gas)
    quote_balance_before_sell = quote_token.balanceOf(test_account.address)

    sell_path = [detector.token_address]
    if quote_symbol != "WETH":
        sell_path.append(detector.quote_token_address)
    else:
        sell_path.append(detector.weth.address)

    print(f"[2/2] Sell token -> {quote_symbol}")

    # Try to sell tokens - catch revert for honeypot detection
    sell_success = True
    quote_received_dec = Decimal(0)
    recovery_ratio = None

    try:
        if detector.dex_version == "v3":
            detector.token.approve(detector.v3_router_addr, token_balance, {"from": test_account, 'gas_limit':block_gas_limit})

            params = (
                detector.token_address,
                quote_addr,
                detector.fee_tier,
                test_account.address,
                deadline,
                token_balance,
                0,
                0
            )

            tx = detector.v3_router.exactInputSingle(params, {"from": test_account, 'gas_limit':block_gas_limit})
        else:
            detector.token.approve(detector.router_addr, token_balance, {"from": test_account, 'gas_limit':block_gas_limit})

            tx = detector.router.swapExactTokensForTokensSupportingFeeOnTransferTokens(
                token_balance,
                0,
                sell_path,
                test_account.address,
                deadline,
                {"from": test_account, 'gas_limit':block_gas_limit}
            )

        network.web3.provider.make_request("anvil_mine", [1])
        tx.wait(1)

        quote_balance_after_sell = quote_token.balanceOf(test_account.address)
        quote_received_raw = quote_balance_after_sell - quote_balance_before_sell
        quote_received_dec = Decimal(quote_received_raw) / (Decimal(10) ** quote_decimals)
        sell_tax = (1 - quote_received_raw/quote_needed_raw) * 100
        print(f"sell tax:{sell_tax}")

        if (quote_received_dec) <= 0.000009:
            sell_success = False
            sell_fail = 2           
            print(f"❌ sell failed | received: {quote_received_dec}")

        if  sell_tax >= 50:
            sell_success = False
            sell_fail = 3
            print(f"  ❌ sell failed | {quote_symbol} received: {quote_received_dec:.6f}")
        else:
            print(f"  ✅ sell complete | {quote_symbol} received: {quote_received_dec:.6f}")

        # Calculate recovery rate based on Quote token (to exclude gas costs)
        if quote_needed_dec > 0:
            recovery_ratio = quote_received_dec / quote_needed_dec
        else:
            recovery_ratio = None

    except (VirtualMachineError, ValueError, Exception) as e:
        sell_success = False
        sell_fail = 4
        quote_received_dec = Decimal(0)
        recovery_ratio = None
        error_msg = str(e) if str(e) else "Transaction reverted"
        print(f"  ❌ sell failed | reason: {error_msg}")
        # Debug: confirm exception was caught
        print(f"  [DEBUG] Exception type: {type(e).__name__}")

    return {
        "test_name": label,
        "quote_symbol": quote_symbol,
        "quote_spent": float(quote_needed_dec),
        "quote_received": float(quote_received_dec),
        "tokens_target": float(target_tokens_dec),
        "tokens_received": float(token_balance_dec),
        "recovery_rate": float(recovery_ratio) if recovery_ratio is not None else None,
        "buy_success": buy_success,
        "sell_success": sell_success,
        "sell_fail": sell_fail,
        "messages": [
            f"quote_spent={quote_needed_dec:.6f} {quote_symbol}",
            f"quote_received={quote_received_dec:.6f} {quote_symbol}",
            f"tokens_target={target_tokens_dec:.6f}",
            f"tokens_received={token_balance_dec:.6f}",
            f"recovery={recovery_ratio*100:.2f}%" if recovery_ratio else "recovery=N/A",
        ] if sell_success else [
            f"quote_spent={quote_needed_dec:.6f} {quote_symbol}",
            f"tokens_target={target_tokens_dec:.6f}",
            f"tokens_received={token_balance_dec:.6f}",
            "Sell transaction reverted - possible honeypot",
        ],
    }


def ensure_eth_balance(account, minimum_eth: Decimal):
    current = Decimal(account.balance()) / (Decimal(10) ** 18)
    if current >= minimum_eth:
        return
    top_up = minimum_eth - current
    top_up_wei = to_wei(top_up)
    print(f"  top up ETH: {top_up:.6f}")
    accounts[0].transfer(account, top_up_wei)


def identify_quote(detector, quote_addr):
    if quote_addr.lower() == detector.weth.address.lower():
        return "WETH", 18
    try:
        symbol = detector.quote_token.symbol()
    except Exception:
        symbol = "QUOTE"
    decimals = getattr(detector, "quote_token_decimals", 18)
    return symbol, decimals


def to_wei(amount_eth: Decimal) -> int:
    return int(amount_eth * (Decimal(10) ** 18))


def from_wei(amount_wei: int) -> Decimal:
    return Decimal(amount_wei) / (Decimal(10) ** 18)


def get_token_reserve(detector):
    """
    Get token reserve from pool (V2 or V3 compatible)
    For V3, use actual pool token balance (more reliable than liquidity estimate)
    """
    if detector.dex_version == "v2":
        pair = Contract.from_abi("IUniswapV2Pair", detector.pair_address, detector.pair_abi)
        reserves = pair.getReserves()
        token0 = pair.token0().lower()
        token_reserve = reserves[0] if token0 == detector.token_address.lower() else reserves[1]
        return token_reserve
    elif detector.dex_version == "v3":
        try:
            token_reserve = detector.token.balanceOf(detector.pair_address)
            return token_reserve
        except Exception:
            return 0
    else:
        return 0

