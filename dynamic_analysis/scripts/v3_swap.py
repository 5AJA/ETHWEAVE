"""
Uniswap V3 swap operations for token testing
"""

from brownie import Contract, chain, network

# Maximum fraction of pool quote reserve we allow for a single test swap when the quoter
# is unavailable. This prevents oversized inputs from exhausting thin liquidity pools.
RESERVE_CAP_RATIO = 0.2


def _estimate_quote_from_reserves(analyzer, amount):
    """
    Estimate quote needed from stored liquidity info when the quoter fails.

    Returns:
        (estimated_quote, quote_reserve) tuple where either can be None if unavailable.
    """
    try:
        liq_info = analyzer.results.get("liquidity_info", {}) if hasattr(analyzer, "results") else {}
        quote_reserve = liq_info.get("quote_reserve") or 0
        token_reserve = liq_info.get("token_reserve") or 0
        if quote_reserve and token_reserve:
            estimated = int(amount * quote_reserve / token_reserve)
            # Ensure non-zero to avoid zero-value swaps
            estimated = max(estimated, 1)
            return estimated, int(quote_reserve)
    except Exception:
        pass
    return None, None


def buy_tokens_v3(analyzer, buyer, amount):
    """
    Buy exact amount of tokens using V3 SwapRouter

    Args:
        analyzer: ScamAnalyzer instance with V3 setup
        buyer: buyer account
        amount: exact token amount to buy

    Returns:
        bool: purchase success
    """
    try:
        router = analyzer.v3_router
        quoter = analyzer.v3_quoter
        weth_address = analyzer.weth.address
        deadline = chain.time() + 300

        quote_addr = analyzer.quote_token_address or weth_address
        is_weth = quote_addr.lower() == weth_address.lower()
        # Reserve-based fallback in case quoter fails or returns nonsense
        reserve_estimate, quote_reserve = _estimate_quote_from_reserves(analyzer, amount)

        if is_weth:
            try:
                quote_result = quoter.quoteExactOutputSingle(
                    quote_addr,
                    analyzer.token.address,
                    amount,
                    analyzer.fee_tier,
                    0
                )
                estimated_quote = quote_result[0]
            except Exception:
                estimated_quote = reserve_estimate or amount * 2

            # Cap estimated input to a safe fraction of the pool reserve when known
            if quote_reserve:
                reserve_cap = int(quote_reserve * RESERVE_CAP_RATIO)
                if reserve_cap > 0:
                    estimated_quote = min(estimated_quote, reserve_cap)

            max_eth = int(estimated_quote * 1.5)
            if quote_reserve:
                reserve_cap = int(quote_reserve * RESERVE_CAP_RATIO)
                if reserve_cap > 0:
                    max_eth = min(max_eth, max(int(estimated_quote * 1.2), 1))
                    max_eth = min(max_eth, reserve_cap)

            available_eth = buyer.balance()
            if available_eth <= 0:
                print("[warn] Buyer has no ETH balance for swap")
                return False
            if max_eth > available_eth:
                max_eth = int(available_eth * 0.9)
                if max_eth <= 0:
                    print("[warn] Insufficient ETH balance for swap")
                    return False

            params = (
                quote_addr,
                analyzer.token.address,
                analyzer.fee_tier,
                buyer.address,
                deadline,
                max_eth,
                0,
                0
            )

            router.exactInputSingle(params, {"from": buyer, "value": max_eth})

        else:
            quote_token = analyzer.quote_token
            weth = analyzer.weth

            try:
                quote_result = quoter.quoteExactOutputSingle(
                    quote_addr,
                    analyzer.token.address,
                    amount,
                    analyzer.fee_tier,
                    0
                )
                estimated_quote = quote_result[0]
            except Exception:
                estimated_quote = reserve_estimate or amount * 2

            if quote_reserve:
                reserve_cap = int(quote_reserve * RESERVE_CAP_RATIO)
                if reserve_cap > 0:
                    estimated_quote = min(estimated_quote, reserve_cap)

            try:
                weth_result = quoter.quoteExactOutputSingle(
                    weth_address,
                    quote_addr,
                    estimated_quote,
                    3000,
                    0
                )
                weth_needed = weth_result[0]
            except Exception:
                weth_needed = estimated_quote * 2

            eth_amount = int(weth_needed * 1.5)
            if quote_reserve:
                reserve_cap = int(quote_reserve * RESERVE_CAP_RATIO)
                if reserve_cap > 0:
                    # Rough cap scaled back to avoid draining thin pools
                    eth_amount = min(eth_amount, max(int(reserve_cap * 1.2), 1))

            available_eth = buyer.balance()
            if available_eth <= 0:
                print("[warn] Buyer has no ETH balance for swap")
                return False
            if eth_amount > available_eth:
                eth_amount = int(available_eth * 0.9)
                if eth_amount <= 0:
                    print("[warn] Insufficient ETH balance for swap")
                    return False

            weth.deposit({"from": buyer, "value": eth_amount})
            weth.approve(analyzer.v3_router_addr, eth_amount, {"from": buyer})

            weth_to_quote_params = (
                weth_address,
                quote_addr,
                3000,
                buyer.address,
                deadline,
                eth_amount,
                0,
                0
            )

            router.exactInputSingle(weth_to_quote_params, {"from": buyer})

            quote_balance = quote_token.balanceOf(buyer.address)
            quote_token.approve(analyzer.v3_router_addr, quote_balance, {"from": buyer})

            quote_to_token_params = (
                quote_addr,
                analyzer.token.address,
                analyzer.fee_tier,
                buyer.address,
                deadline,
                quote_balance,
                0,
                0
            )

            router.exactInputSingle(quote_to_token_params, {"from": buyer})

        tokens_bought = analyzer.token.balanceOf(buyer.address)
        decimals = analyzer.token.decimals()
        print(f"✅ User bought {tokens_bought / 10**decimals:.6f} tokens (V3)")
        return tokens_bought >= 0

    except Exception as exc:
        print(f"[warn] V3 token purchase failed: {exc}")
        import traceback
        traceback.print_exc()
        return False


def sell_tokens_v3(analyzer, seller, amount):
    """
    Sell exact amount of tokens using V3 SwapRouter

    Args:
        analyzer: ScamAnalyzer instance with V3 setup
        seller: seller account
        amount: token amount to sell

    Returns:
        tuple: (success: bool, quote_received: int)
    """
    try:
        router = analyzer.v3_router
        weth_address = analyzer.weth.address
        deadline = chain.time() + 300

        quote_addr = analyzer.quote_token_address or weth_address
        is_weth = quote_addr.lower() == weth_address.lower()

        analyzer.token.approve(analyzer.v3_router_addr, amount, {"from": seller})

        if is_weth:
            params = (
                analyzer.token.address,
                quote_addr,
                analyzer.fee_tier,
                analyzer.v3_router_addr,
                deadline,
                amount,
                0,
                0
            )

            eth_before = seller.balance()
            tx = router.exactInputSingle(params, {"from": seller})
            
            network.web3.provider.make_request("anvil_mine", [1])
            tx.wait(1)

            if hasattr(router, 'unwrapWETH9'):
                weth_balance = analyzer.weth.balanceOf(analyzer.v3_router_addr)
                if weth_balance > 0:
                    router.unwrapWETH9(weth_balance, seller.address, {"from": seller})

            eth_after = seller.balance()
            gas_cost = tx.gas_used * tx.gas_price
            eth_received = eth_after - eth_before + gas_cost

            decimals = analyzer.token.decimals()
            print(f"✅ User sold {amount / 10**decimals:.6f} tokens for {eth_received / 10**18:.6f} ETH (V3)")
            return True, eth_received

        else:
            quote_token = analyzer.quote_token
            quote_before = quote_token.balanceOf(seller.address)

            params = (
                analyzer.token.address,
                quote_addr,
                analyzer.fee_tier,
                seller.address,
                deadline,
                amount,
                0,
                0
            )

            router.exactInputSingle(params, {"from": seller})

            quote_after = quote_token.balanceOf(seller.address)
            quote_received = quote_after - quote_before

            decimals = analyzer.token.decimals()
            quote_decimals = analyzer.quote_token_decimals
            print(f"✅ User sold {amount / 10**decimals:.6f} tokens for {quote_received / 10**quote_decimals:.6f} quote tokens (V3)")
            return True, quote_received

    except Exception as exc:
        print(f"[warn] V3 token sale failed: {exc}")
        import traceback
        traceback.print_exc()
        return False, 0


def measure_tax_v3(analyzer, buyer, quote_amount_to_spend):
    """
    Measure buy and sell tax via actual V3 trades

    Args:
        analyzer: ScamAnalyzer instance with V3 setup
        buyer: buyer account
        quote_amount_to_spend: amount of quote token to spend

    Returns:
        dict: tax measurement results
    """
    result = {
        "buy_tax": None,
        "sell_tax": None,
        "buy_received": 0,
        "sell_received": 0,
        "quote_received": 0,
        "error": None,
    }

    try:
        router = analyzer.v3_router
        quoter = analyzer.v3_quoter
        weth_address = analyzer.weth.address
        quote_addr = analyzer.quote_token_address or weth_address
        is_weth = quote_addr.lower() == weth_address.lower()
        deadline = chain.time() + 300

        balance_before = analyzer.token.balanceOf(buyer.address)

        if is_weth:
            params = (
                quote_addr,
                analyzer.token.address,
                analyzer.fee_tier,
                buyer.address,
                deadline,
                quote_amount_to_spend,
                0,
                0
            )

            router.exactInputSingle(params, {"from": buyer, "value": quote_amount_to_spend})

        else:
            quote_token = analyzer.quote_token
            weth = analyzer.weth

            try:
                weth_result = quoter.quoteExactOutputSingle(
                    weth_address,
                    quote_addr,
                    quote_amount_to_spend,
                    3000,
                    0
                )
                weth_needed = weth_result[0]
            except Exception:
                weth_needed = quote_amount_to_spend * 3

            eth_amount = int(weth_needed * 1.5)

            if buyer.balance() < eth_amount:
                result["error"] = f"Insufficient ETH balance: {buyer.balance()} < {eth_amount}"
                return result

            weth.deposit({"from": buyer, "value": eth_amount})
            weth.approve(analyzer.v3_router_addr, eth_amount, {"from": buyer})

            weth_to_quote_params = (
                weth_address,
                quote_addr,
                3000,
                buyer.address,
                deadline,
                quote_amount_to_spend,
                eth_amount,
                0
            )

            router.exactOutputSingle(weth_to_quote_params, {"from": buyer})

            quote_balance = quote_token.balanceOf(buyer.address)
            if quote_balance < quote_amount_to_spend:
                result["error"] = f"Insufficient quote tokens: {quote_balance} < {quote_amount_to_spend}"
                return result

            quote_token.approve(analyzer.v3_router_addr, quote_amount_to_spend, {"from": buyer})

            quote_to_token_params = (
                quote_addr,
                analyzer.token.address,
                analyzer.fee_tier,
                buyer.address,
                deadline,
                quote_amount_to_spend,
                0,
                0
            )

            router.exactInputSingle(quote_to_token_params, {"from": buyer})

        balance_after = analyzer.token.balanceOf(buyer.address)
        tokens_received = balance_after - balance_before
        result["buy_received"] = tokens_received

        try:
            quote_result = quoter.quoteExactInputSingle(
                quote_addr,
                analyzer.token.address,
                quote_amount_to_spend,
                analyzer.fee_tier,
                0
            )
            expected_tokens = quote_result[0]

            if expected_tokens > 0:
                tax_rate = (expected_tokens - tokens_received) / expected_tokens
                result["buy_tax"] = max(0.0, min(1.0, tax_rate))
        except Exception:
            result["buy_tax"] = 0.0

    except Exception as exc:
        result["error"] = f"Buy trade failed: {exc}"
        return result

    if tokens_received > 0:
        try:
            sell_amount = tokens_received // 2
            analyzer.token.approve(analyzer.v3_router_addr, sell_amount, {"from": buyer})

            if is_weth:
                params = (
                    analyzer.token.address,
                    quote_addr,
                    analyzer.fee_tier,
                    analyzer.v3_router_addr,
                    deadline,
                    sell_amount,
                    0,
                    0
                )

                eth_before = buyer.balance()
                tx = router.exactInputSingle(params, {"from": buyer})

                if hasattr(router, 'unwrapWETH9'):
                    weth_balance = analyzer.weth.balanceOf(analyzer.v3_router_addr)
                    if weth_balance > 0:
                        router.unwrapWETH9(weth_balance, buyer.address, {"from": buyer})

                eth_after = buyer.balance()
                gas_cost = tx.gas_used * tx.gas_price
                quote_received = eth_after - eth_before + gas_cost

            else:
                quote_token = analyzer.quote_token
                quote_before = quote_token.balanceOf(buyer.address)

                params = (
                    analyzer.token.address,
                    quote_addr,
                    analyzer.fee_tier,
                    buyer.address,
                    deadline,
                    sell_amount,
                    0,
                    0
                )

                router.exactInputSingle(params, {"from": buyer})

                quote_after = quote_token.balanceOf(buyer.address)
                quote_received = quote_after - quote_before

            result["sell_received"] = quote_received
            result["quote_received"] = quote_received

            try:
                quote_result = quoter.quoteExactInputSingle(
                    analyzer.token.address,
                    quote_addr,
                    sell_amount,
                    analyzer.fee_tier,
                    0
                )
                expected_quote = quote_result[0]

                if expected_quote > 0:
                    tax_rate = (expected_quote - quote_received) / expected_quote
                    result["sell_tax"] = max(0.0, min(1.0, tax_rate))
            except Exception:
                result["sell_tax"] = 0.0

        except Exception as exc:
            result["error"] = f"Sell trade failed: {exc}" if not result["error"] else result["error"]

    return result
