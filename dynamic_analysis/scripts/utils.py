import csv
from decimal import Decimal, InvalidOperation
from brownie.exceptions import VirtualMachineError
from brownie import Contract, chain,network
from scripts import v3_swap
from typing import Callable, Optional


def load_holders(csv_path, token_address, limit=None):
    """
    Stream holder CSV and return up to `limit` holders for the given token.

    Args:
        csv_path (str | Path): Path to the holder CSV file.
        token_address (str): Target token address (case-insensitive).
        limit (int, optional): Maximum number of holder rows to return.

    Returns:
        list[dict]: Holder dictionaries with keys:
            - holder_address (str)
            - balance_decimal (Decimal)
            - relative_share (Decimal)
            - raw_balance (str)
    """
    if not csv_path:
        return []

    normalized = token_address.lower()
    holders = []

    with open(csv_path, "r", encoding="utf-8") as csv_file:
        reader = csv.DictReader(csv_file)
        if reader.fieldnames is None:
            return []

        required = {"token_address", "holder_address", "balance", "rel_to_total"}
        if not required.issubset(set(reader.fieldnames)):
            raise ValueError(f"Missing holder CSV columns: {required - set(reader.fieldnames)}")

        for row in reader:
            token_value = (row.get("token_address") or "").lower()
            if token_value != normalized:
                continue

            balance_str = row.get("balance", "0") or "0"
            rel_str = row.get("rel_to_total", "0") or "0"

            try:
                balance_dec = Decimal(balance_str)
            except (InvalidOperation, TypeError):
                balance_dec = Decimal(0)

            try:
                rel_dec = Decimal(rel_str)
            except (InvalidOperation, TypeError):
                rel_dec = Decimal(0)

            holders.append(
                {
                    "holder_address": row.get("holder_address"),
                    "balance_decimal": balance_dec,
                    "relative_share": rel_dec,
                    "raw_balance": balance_str,
                }
            )

            if limit is not None and len(holders) >= limit:
                break

    return holders

def buy_tokens_from_pool(analyzer, buyer, amount):
    """
    Buy tokens from liquidity pool (V2 or V3)

    Args:
        analyzer: ScamAnalyzer instance
        buyer: buyer account
        amount: target token amount to buy

    Returns:
        bool: purchase success
    """
    if not analyzer.pair_address:
        print("[warn] Pool address not found")
        return False

    if analyzer.dex_version == "v3":
        return v3_swap.buy_tokens_v3(analyzer, buyer, amount)

    if not analyzer.router:
        print("[warn] Router contract not found")
        return False

    try:
        router = analyzer.router
        weth_address = router.WETH()
        deadline = chain.time() + 300

        quote_addr = analyzer.quote_token_address or weth_address
        is_weth = quote_addr.lower() == weth_address.lower()

        # Set buy path: Quote -> Token
        path = [quote_addr, analyzer.token.address]

        # Calculate required quote amount
        try:
            amounts = router.getAmountsIn(amount, path)
            quote_needed = amounts[0]
        except Exception as exc:
            print(f"[warn] Failed to calculate required quote amount: {exc}")
            return False

        if is_weth:
            # If quote is WETH, use swapETHForExactTokens
            max_eth = int(quote_needed * 1.2)  # 20% buffer
            tx = router.swapETHForExactTokens(
                amount,
                path,
                buyer.address,
                deadline,
                {"from": buyer, "value": max_eth}
            )
            network.web3.provider.make_request("anvil_mine", [1])
            tx.wait(1)
        else:
            # If quote is other ERC20 (DAI, USDT, etc.), prepare quote tokens first
            quote_token = analyzer.quote_token

            # Calculate ETH needed for WETH -> Quote swap with buffer
            try:
                weth_needed_for_quote = router.getAmountsIn(quote_needed, [weth_address, quote_addr])[0]
                eth_amount = int(weth_needed_for_quote * 1.5)  # 50% buffer
            except Exception:
                eth_amount = int(quote_needed * 2)  # Fallback: 2x buffer

            # Deposit ETH -> WETH
            weth = Contract.from_abi("WETH", weth_address, analyzer.weth.abi)
            weth.deposit({"from": buyer, "value": eth_amount})

            # Swap WETH -> Quote token (use swapTokensForExactTokens for precise amount)
            weth.approve(analyzer.router_addr, eth_amount, {"from": buyer})
            tx = router.swapTokensForExactTokens(
                quote_needed,
                eth_amount,
                [weth_address, quote_addr],
                buyer.address,
                deadline,
                {"from": buyer}
            )
            
            network.web3.provider.make_request("anvil_mine", [1])
            tx.wait(1)

            # Verify quote balance
            quote_balance = quote_token.balanceOf(buyer.address)
            if quote_balance < quote_needed:
                print(f"[warn] Insufficient quote tokens: {quote_balance} < {quote_needed}")
                return False

            # Swap Quote -> Target token
            quote_token.approve(analyzer.router_addr, quote_needed, {"from": buyer})
            tx = router.swapExactTokensForTokensSupportingFeeOnTransferTokens(
                quote_needed,
                0,
                path,
                buyer.address,
                deadline,
                {"from": buyer}
            )
            network.web3.provider.make_request("anvil_mine", [1])
            tx.wait(1)
            
        # Verify purchase
        tokens_bought = analyzer.token.balanceOf(buyer.address)
        decimals = analyzer.token.decimals()
        print(f"✅ User bought {tokens_bought / 10**decimals:.6f} tokens")
        return tokens_bought >= amount * 0.95  # Allow 5% slippage

    except Exception as exc:
        print(f"[warn] Token purchase failed: {exc}")
        return False


def sell_tokens_to_pool(analyzer, seller, amount):
    """
    Sell tokens to liquidity pool (V2 or V3)

    Args:
        analyzer: ScamAnalyzer instance
        seller: seller account
        amount: token amount to sell

    Returns:
        tuple: (success: bool, quote_received: int)
    """
    if not analyzer.pair_address:
        print("[warn] Pool address not found")
        return False, 0

    if analyzer.dex_version == "v3":
        return v3_swap.sell_tokens_v3(analyzer, seller, amount)

    if not analyzer.router:
        print("[warn] Router contract not found")
        return False, 0

    try:
        router = analyzer.router
        weth_address = router.WETH()
        deadline = chain.time() + 300

        quote_addr = analyzer.quote_token_address or weth_address
        is_weth = quote_addr.lower() == weth_address.lower()

        # Set sell path: Token -> Quote
        path = [analyzer.token.address, quote_addr]

        # Approve router to spend tokens
        analyzer.token.approve(analyzer.router_addr, amount, {"from": seller})

        if is_weth:
            # If quote is WETH, use swapExactTokensForETH
            eth_before = seller.balance()

            tx = router.swapExactTokensForETHSupportingFeeOnTransferTokens(
                amount,
                0,
                path,
                seller.address,
                deadline,
                {"from": seller}
            )
            
            network.web3.provider.make_request("anvil_mine", [1])
            tx.wait(1)
            
            eth_after = seller.balance()
            gas_cost = tx.gas_used * tx.gas_price
            eth_received = eth_after - eth_before + gas_cost

            decimals = analyzer.token.decimals()
            print(f"✅ User sold {amount / 10**decimals:.6f} tokens for {eth_received / 10**18:.6f} ETH")
            return True, eth_received
        else:
            # If quote is other ERC20 (DAI, USDT, etc.)
            quote_token = analyzer.quote_token
            quote_before = quote_token.balanceOf(seller.address)

            tx = router.swapExactTokensForTokensSupportingFeeOnTransferTokens(
                amount,
                0,
                path,
                seller.address,
                deadline,
                {"from": seller}
            )
            
            network.web3.provider.make_request("anvil_mine", [1])
            tx.wait(1)

            quote_after = quote_token.balanceOf(seller.address)
            quote_received = quote_after - quote_before

            decimals = analyzer.token.decimals()
            quote_decimals = getattr(analyzer, "quote_token_decimals", 18)
            print(f"✅ User sold {amount / 10**decimals:.6f} tokens for {quote_received / 10**quote_decimals:.6f} quote tokens")
            return True, quote_received

    except (VirtualMachineError, ValueError, Exception) as e:
        error_msg = str(e) if str(e) else "Transaction reverted"
        print(f"  ❌ sell failed | reason: {error_msg}")
        # Debug: confirm exception was caught
        print(f"  [DEBUG] Exception type: {type(e).__name__}")
        return False, 0
    # except Exception as exc:
    #     print(f"[warn] Token sale failed: {exc}")
    #     return False, 0


def ensure_eth_usd_price(router, weth_address, quote_tokens, quote_token_decimals, cache_obj=None):
    """
    Fetch 1 WETH -> USD stable price using the provided router; optionally cache on cache_obj.eth_usd_price.
    """
    cached = getattr(cache_obj, "eth_usd_price", None) if cache_obj else None
    if cached is not None:
        return cached

    stable_paths = [
        quote_tokens.get("USDC"),
        quote_tokens.get("USDT"),
    ]
    amount_in = 10 ** 18  # 1 WETH

    for stable_addr in stable_paths:
        if not stable_addr:
            continue
        try:
            amounts = router.getAmountsOut(amount_in, [weth_address, stable_addr])
            decimals = quote_token_decimals.get(
                next((k for k, v in quote_tokens.items() if v == stable_addr), ""),
                18,
            )
            price = amounts[-1] / (10 ** decimals)
            if price > 0:
                if cache_obj is not None:
                    setattr(cache_obj, "eth_usd_price", price)
                return price
        except Exception:
            continue

    if cache_obj is not None:
        setattr(cache_obj, "eth_usd_price", None)
    return None


def quote_value_in_usd(quote_symbol, quote_reserve, quote_decimals, price_provider: Optional[Callable[[], Optional[float]]] = None):
    """
    Convert quote reserve into USD-equivalent value for fair pool comparison.

    Args:
        quote_symbol (str): symbol of the quote asset (e.g., WETH, USDT)
        quote_reserve (int): raw reserve amount
        quote_decimals (int): decimals for the quote asset
        price_provider (callable | None): returns ETH price in USD when quote_symbol is WETH
    """
    if quote_reserve is None:
        return 0

    amount = quote_reserve / (10 ** quote_decimals)

    if quote_symbol.upper() == "WETH":
        eth_price = price_provider() if callable(price_provider) else None
        if eth_price:
            return amount * eth_price

    # Stablecoins or fallback
    return amount


def measure_tax_via_trade(analyzer, buyer, quote_amount_to_spend):
    """
    Measure actual buy and sell tax by performing real trades (V2 or V3)

    Args:
        analyzer: ScamAnalyzer instance
        buyer: buyer account
        quote_amount_to_spend: amount of quote token to spend for buying

    Returns:
        dict: {
            "buy_tax": float or None,
            "sell_tax": float or None,
            "buy_received": int,
            "sell_received": int,
            "quote_received": int,
            "error": str or None
        }
    """
    if analyzer.dex_version == "v3":
        return v3_swap.measure_tax_v3(analyzer, buyer, quote_amount_to_spend)

    result = {
        "buy_tax": None,
        "sell_tax": None,
        "buy_received": 0,
        "sell_received": 0,
        "quote_received": 0,
        "error": None,
    }

    try:
        router = analyzer.router
        weth_address = router.WETH()
        quote_addr = analyzer.quote_token_address or weth_address
        is_weth = quote_addr.lower() == weth_address.lower()

        path_buy = [quote_addr, analyzer.token_address]
        path_sell = [analyzer.token_address, quote_addr]

        # Step 1: Buy tokens and measure buy tax
        balance_before = analyzer.token.balanceOf(buyer.address)

        if is_weth:
            # WETH quote: use ETH directly
            router.swapExactETHForTokensSupportingFeeOnTransferTokens(
                0,
                path_buy,
                buyer.address,
                chain.time() + 300,
                {"from": buyer, "value": quote_amount_to_spend}
            )
        else:
            # Non-WETH quote: prepare quote tokens first
            quote_token = analyzer.quote_token
            weth = analyzer.weth

            # Calculate ETH needed for quote amount with buffer
            try:
                weth_needed = router.getAmountsIn(quote_amount_to_spend, [weth_address, quote_addr])[0]
                eth_amount = int(weth_needed * 1.5)  # 50% buffer
            except Exception:
                # Fallback: use conservative estimate (assume 1 WETH = 1 quote token for safety)
                eth_amount = int(quote_amount_to_spend * 3)  # 3x buffer for safety

            # Ensure buyer has enough ETH
            if buyer.balance() < eth_amount:
                raise Exception(f"Insufficient ETH balance: {buyer.balance()} < {eth_amount}")

            # Deposit ETH -> WETH
            weth.deposit({"from": buyer, "value": eth_amount})

            # Swap WETH -> Quote token
            weth.approve(analyzer.router_addr, eth_amount, {"from": buyer})
            router.swapTokensForExactTokens(
                quote_amount_to_spend,
                eth_amount,
                [weth_address, quote_addr],
                buyer.address,
                chain.time() + 300,
                {"from": buyer}
            )

            # Verify quote balance
            quote_balance = quote_token.balanceOf(buyer.address)
            if quote_balance < quote_amount_to_spend:
                result["error"] = f"Insufficient quote tokens: {quote_balance} < {quote_amount_to_spend}"
                return result

            # Swap Quote -> Token
            quote_token.approve(analyzer.router_addr, quote_amount_to_spend, {"from": buyer})
            router.swapExactTokensForTokensSupportingFeeOnTransferTokens(
                quote_amount_to_spend,
                0,
                path_buy,
                buyer.address,
                chain.time() + 300,
                {"from": buyer}
            )

        balance_after = analyzer.token.balanceOf(buyer.address)
        tokens_received = balance_after - balance_before
        result["buy_received"] = tokens_received

        # Calculate buy tax
        try:
            amounts = router.getAmountsOut(quote_amount_to_spend, path_buy)
            expected_tokens = amounts[-1]

            if expected_tokens > 0:
                tax_rate = (expected_tokens - tokens_received) / expected_tokens
                result["buy_tax"] = max(0.0, min(1.0, tax_rate))  # Clamp between 0 and 1
        except Exception:
            result["buy_tax"] = 0.0

    except Exception as exc:
        result["error"] = f"Buy trade failed: {exc}"
        return result

    # Step 2: Sell 50% of tokens and measure sell tax
    if tokens_received > 0:
        try:
            sell_amount = tokens_received // 2

            # Approve router
            analyzer.token.approve(analyzer.router_addr, sell_amount, {"from": buyer})

            if is_weth:
                # WETH quote: sell for ETH
                eth_before = buyer.balance()

                tx = router.swapExactTokensForETHSupportingFeeOnTransferTokens(
                    sell_amount,
                    0,
                    path_sell,
                    buyer.address,
                    chain.time() + 300,
                    {"from": buyer}
                )

                eth_after = buyer.balance()
                gas_cost = tx.gas_used * tx.gas_price
                quote_received = eth_after - eth_before + gas_cost
                result["sell_received"] = quote_received
                result["quote_received"] = quote_received
            else:
                # Non-WETH quote: sell for quote token
                quote_token = analyzer.quote_token
                quote_before = quote_token.balanceOf(buyer.address)

                router.swapExactTokensForTokensSupportingFeeOnTransferTokens(
                    sell_amount,
                    0,
                    path_sell,
                    buyer.address,
                    chain.time() + 300,
                    {"from": buyer}
                )

                quote_after = quote_token.balanceOf(buyer.address)
                quote_received = quote_after - quote_before
                result["sell_received"] = quote_received
                result["quote_received"] = quote_received

            # Calculate sell tax
            try:
                amounts = router.getAmountsOut(sell_amount, path_sell)
                expected_quote = amounts[-1]

                if expected_quote > 0:
                    tax_rate = (expected_quote - quote_received) / expected_quote
                    result["sell_tax"] = max(0.0, min(1.0, tax_rate))  # Clamp between 0 and 1
            except Exception:
                result["sell_tax"] = 0.0

        except Exception as exc:
            result["error"] = f"Sell trade failed: {exc}" if not result["error"] else result["error"]

    return result
