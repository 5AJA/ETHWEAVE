#!/usr/bin/python3
"""
Standalone exterior function call tracer for ERC-20 transfers.

This script performs a token transfer from a specified Brownie account, then
prints a call-trace similar to scripts/trace_erc20_calls.py. If the transfer
reverts, the trace is still requested so external calls can be inspected.
"""

import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

from brownie import accounts, history, web3, Contract, chain,network
from brownie.exceptions import VirtualMachineError
from eth_utils import to_checksum_address
from scripts import utils

# Ensure the project root is importable when the script runs directly.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

PRECOMPILES = {f"0x{n:040x}" for n in range(1, 10)}  # 0x1 ~ 0x9
EIP1967_IMPLEMENTATION_SLOT = int("0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc", 16)

# Cache proxy implementation lookups to avoid repeated storage reads
_IMPLEMENTATION_CACHE = {}
# Hints injected by analyzer (e.g., already discovered implementation address)
_IMPLEMENTATION_HINTS = {}

# Known DEX addresses (routers and factories) - not suspicious
KNOWN_DEX_ADDRESSES = {
    # Routers
    "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D",  # UniswapV2
    "0xf164fC0Ec4E93095b804a4795bBe1e041497b92a",  # UniswapV2_Old
    "0xe592427a0aece92de3edee1f18e0157c05861564",  # UniswapV3
    "0xd9e1cE17f2641f24aE83637ab66a2cca9C378B9F",  # SushiswapV2
    "0x03f7724180AA6b939894B5Ca4314783B0b36b329",  # ShibaswapV1
    "0xEfF92A263d31888d860bD50809A8D171709b7b1c",  # PancakeV2
    "0xC14d550632db8592D1243Edc8B95b0Ad06703867",  # Fraxswap
    "0x463672ffdED540f7613d3e8248e3a8a51bAF7217",  # Whiteswap
    # Factories
    "0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f",  # UniswapV2
    "0x1F98431c8aD98523631AE4a59f267346ea31F984",  # UniswapV3
    "0xC0AEe478e3658e2610c5F7A4A2E1777cE9e4f2Ac",  # SushiswapV2
    "0x1097053Fd2ea711dad45caCcc45EfF7548fCB362",  # PancakeV2
    "0x115934131916C8b277DD010Ee02de363c09d037c",  # ShibaswapV1
    "0x43eC799eAdd63848443E2347C49f5f52e8Fe0F6f",  # Fraxswap
    "0x69bd16aE6F507bd3Fc9eCC984d50b04F029EF677",  # Whiteswap
	# Major Tokens
	"0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2", # WETH
	"0xdAC17F958D2ee523a2206206994597C13D831ec7", # USDT
    "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", # USDC
    "0x6B175474E89094C44Da98b954EedeAC495271d0F", # DAI
}


def _get_proxy_impl(token_address: str):
    """Return implementation address for EIP-1967 proxy if present (cached)."""
    key = token_address.lower()
    hinted = _IMPLEMENTATION_HINTS.get(key)
    if hinted:
        return hinted
    if key in _IMPLEMENTATION_CACHE:
        return _IMPLEMENTATION_CACHE[key]

    impl = None
    try:
        raw = web3.eth.get_storage_at(token_address, EIP1967_IMPLEMENTATION_SLOT)
        if raw and any(raw):
            candidate = "0x" + raw[-20:].hex()
            if candidate.lower() != "0x" + "0" * 40:
                impl = to_checksum_address(candidate)
    except Exception:
        impl = None

    _IMPLEMENTATION_CACHE[key] = impl
    return impl


def is_suspicious_external_call(addr: str, token_address: str, impl_address: str = None, exclude_addresses=None) -> bool:
    """
    Check if a call to the given address is suspicious.

    Args:
        addr: Address being called
        token_address: The token contract address (to exclude self-calls)
        exclude_addresses: Additional addresses to skip (e.g., selected pair)

    Returns:
        True if the call is suspicious, False otherwise

    Excludes:
        - Self-calls (token calling itself)
        - Proxy implementation self-calls (EIP-1967)
        - Addresses explicitly excluded by the analyzer (e.g., liquidity pool)
        - Known DEX routers and factories
        - Precompiled contracts
    """
    checksum = to_checksum_address(addr)
    token_checksum = to_checksum_address(token_address)
    impl_checksum = impl_address or _get_proxy_impl(token_checksum) or token_checksum

    skip_set = {token_checksum.lower(), impl_checksum.lower()}
    if exclude_addresses:
        for extra in exclude_addresses:
            if not extra:
                continue
            try:
                skip_set.add(to_checksum_address(extra).lower())
            except Exception:
                continue

    # 1. Exclude self-calls
    if checksum.lower() in skip_set:
        return False

    # 2. Exclude known DEX addresses
    if checksum in KNOWN_DEX_ADDRESSES:
        return False

    # 3. Check if it's a contract
    code = web3.eth.get_code(checksum)
    return len(code or b"") > 0

def trace_tx(txhash: str, tracer_type="callTracer"):
    """
    Request a trace via debug_traceTransaction.

    Args:
        txhash: Transaction hash
        tracer_type: "callTracer" for call tree, "prestateTracer" for state,
                     or None for opcode-level trace
    """
    if tracer_type:
        params = [txhash, {"tracer": tracer_type}]
    else:
        # Opcode-level trace - enable structLogs explicitly for Anvil compatibility
        params = [txhash, {
            "enableMemory": True,
            "disableStack": False,
            "disableStorage": True,
            "enableReturnData": False
        }]

    result = web3.provider.make_request("debug_traceTransaction", params)
    if "error" in result:
        raise RuntimeError(result["error"])
    return result["result"]

MAX_TRACE_DEPTH = 8
MAX_TRACE_CALLS = 200


def extract_calls_from_call_tracer(call_frame, token_address, depth=0, calls=None, is_root=True, max_depth=MAX_TRACE_DEPTH, max_calls=MAX_TRACE_CALLS, exclude_addresses=None):
    """
    Extract suspicious external calls from callTracer output recursively.

    Args:
        call_frame: Call trace from callTracer
        token_address: Token contract address (to filter out self-calls)
        depth: Current call depth
        calls: Accumulated calls list
        is_root: Whether this is the root call (transfer itself)
        max_depth: Maximum recursion depth to avoid runaway traces
        max_calls: Maximum number of calls to collect
        exclude_addresses: Additional addresses that should be ignored
    """
    if calls is None:
        calls = []

    if depth > max_depth or len(calls) >= max_calls:
        return calls

    call_type = call_frame.get("type", "CALL").upper()
    to_addr = call_frame.get("to", "")
    input_data = call_frame.get("input", "0x")

    # Extract function selector (first 4 bytes of input)
    selector = input_data[:10] if len(input_data) >= 10 else "0x"

    # Skip root call (the transfer itself), precompiles, and non-suspicious calls
    # Only add nested calls from within the transfer execution
    if not is_root and to_addr and to_addr.lower() not in PRECOMPILES:
        if is_suspicious_external_call(
            to_addr,
            token_address,
            impl_address=_IMPLEMENTATION_HINTS.get(token_address.lower()),
            exclude_addresses=exclude_addresses,
        ):
            # Ensure we capture all call types
            if call_type in ["CALL", "DELEGATECALL", "STATICCALL", "CALLCODE"]:
                calls.append({
                    "type": call_type,
                    "to": to_addr,
                    "input": selector,
                    "depth": depth,
                    "pc": 0,  # callTracer doesn't provide PC
                })

    # Recursively process nested calls
    for subcall in call_frame.get("calls", []):
        if len(calls) >= max_calls:
            break
        extract_calls_from_call_tracer(
            subcall,
            token_address,
            depth + 1,
            calls,
            is_root=False,
            max_depth=max_depth,
            max_calls=max_calls,
            exclude_addresses=exclude_addresses,
        )

    return calls

def ensure_eth_balance(account, min_balance_wei, funder):
    """
    Ensure the given account has at least `min_balance_wei` ETH.
    Attempt to top up from `funder` if needed.
    """
    try:
        current_balance = web3.eth.get_balance(account.address)
    except Exception as exc:
        print(f"[warn] Failed to read ETH balance for {account.address}: {exc}")
        return False

    if current_balance >= min_balance_wei:
        return True

    top_up = min_balance_wei - current_balance
    try:
        funder.transfer(account.address, top_up)
    except Exception as exc:
        top_up_eth = Decimal(top_up) / Decimal(10 ** 18)
        print(f"[warn] Failed to fund {account.address} with {top_up_eth} ETH: {exc}")
        return False

    try:
        return web3.eth.get_balance(account.address) >= min_balance_wei
    except Exception as exc:
        print(f"[warn] Failed to confirm ETH top-up for {account.address}: {exc}")
        return False


def analyze_transfer(token, sender, recipient, amount,gas_limit, excluded_addresses=None):
    """
    Execute token.transfer and return the transaction plus external call frames.
    """
    tx = None
    reverted = False
    try:
        tx = token.transfer(recipient, amount, {"from": sender,"gas_limit":gas_limit})
        network.web3.provider.make_request("anvil_mine", [1])
        tx.wait(1)
        reverted = tx.status != 1
    except (VirtualMachineError, ValueError) as exc:
        tx = getattr(exc, "tx", None)
        reverted = True
        print(f"[warn] Transfer raised exception: {exc}")

        if tx is None and len(history) > 0:
            tx = history[-1]

    if tx is None:
        print("❌ Transaction object not available - cannot trace")
        return {"tx": None, "reverted": True, "external_calls": []}

    # Extract suspicious external calls using callTracer
    call_trace = trace_tx(tx.txid, tracer_type="callTracer")
    external_calls = extract_calls_from_call_tracer(
        call_trace,
        token.address,
        exclude_addresses=excluded_addresses,
    )

    # 외부 컨트랙트 호출 요약
    print(f"\n{'='*120}")
    print("[EXTERNAL CONTRACT CALLS SUMMARY]")
    if not external_calls:
        print("→ No external contract calls detected ✅")
    else:
        print(f"→ External contract calls detected ⚠️ (total: {len(external_calls)})\n")
        for i, call in enumerate(external_calls, 1):
            selector = call.get("input", "0x")[:10] if call.get("input") else "0x"
            print(f"  [{i}] {call['type']}")
            print(f"      to:       {call.get('to', 'N/A')}")
            print(f"      selector: {selector}")

    print(f"\n{'='*120}\n")

    return {"tx": tx, "reverted": reverted, "external_calls": external_calls}

def parse_amount(amount_str, decimals):
    """
    Convert a token amount expressed in human units to the smallest unit.
    """
    try:
        value = Decimal(str(amount_str))
    except (InvalidOperation, TypeError):
        raise ValueError(f"Invalid token amount: {amount_str}")

    scaled = value * (Decimal(10) ** decimals)
    return int(scaled.to_integral_value())

def run_scenario(analyzer):
    """Run the exterior function call scenario."""
    print(f"\n{'='*60}")
    print("시나리오 4: Exterior Function Call Tracer")
    print(f"{'='*60}\n")
    gas_limit = analyzer.gaslimit
    network.gas_limit(analyzer.gaslimit)
    
    try:
        decimals = analyzer.token.decimals()
    except Exception:
        decimals = 18
    # Provide implementation hint from analyzer (if already resolved)
    try:
        impl_hint = getattr(analyzer, "implementation_address", None)
        if impl_hint:
            _IMPLEMENTATION_HINTS[analyzer.token_address.lower()] = impl_hint
    except Exception:
        pass

    target_ratio = Decimal("0.005")

    token_reserve_raw = None
    pair_address = getattr(analyzer, "pair_address", None)
    excluded_addresses = set()
    if pair_address:
        excluded_addresses.add(pair_address)

    if pair_address:
        try:
            if analyzer.dex_version == "v2":
                pair_contract = Contract.from_abi("IUniswapV2Pair", pair_address, analyzer.pair_abi)
                reserves = pair_contract.getReserves()
                token0 = pair_contract.token0().lower()
                token_address = analyzer.token.address.lower()
                token_reserve_raw = reserves[0] if token0 == token_address else reserves[1]
            elif analyzer.dex_version == "v3":
                pool = Contract.from_abi("IUniswapV3Pool", analyzer.pair_address, analyzer.v3_pool_abi)
                liquidity = pool.liquidity()
                slot0 = pool.slot0()
                sqrt_price_x96 = slot0[0]
                token0 = pool.token0().lower()
                is_token0 = token0 == analyzer.token_address.lower()

                if sqrt_price_x96 > 0:
                    if is_token0:
                        token_reserve_raw = int(liquidity / (sqrt_price_x96 / (2 ** 96)))
                    else:
                        token_reserve_raw = int(liquidity * (sqrt_price_x96 / (2 ** 96)))
                else:
                    token_reserve_raw = 0
        except Exception as exc:
            print(f"[warn] Unable to read reserves from pool {pair_address}: {exc}")

    if token_reserve_raw:
        amount = max(1, int(Decimal(token_reserve_raw) * target_ratio))
        buy_amount = amount * 2
        amount_tokens = Decimal(amount) / (Decimal(10) ** decimals)
        buy_tokens = Decimal(buy_amount) / (Decimal(10) ** decimals)
        print(f"[info] Transfer amount set to 0.5% of reserve: {amount_tokens:.6f} tokens")
        print(f"[info] accounts[4] purchase target (2x): {buy_tokens:.6f} tokens")
    else:
        amount = parse_amount("10", decimals)
        buy_amount = amount * 2
        amount_tokens = Decimal(amount) / (Decimal(10) ** decimals)
        buy_tokens = Decimal(buy_amount) / (Decimal(10) ** decimals)
        print("[warn] Falling back to default transfer amount (pool reserves unavailable).")
        print(f"[info] Transfer amount: {amount_tokens:.6f} tokens")
        print(f"[info] accounts[4] purchase target (2x): {buy_tokens:.6f} tokens")
    gas_stipend_wei = 10 * 10 ** 18  # 10 ETH for gas
    gas_funder = accounts[0]

    sender = None

    print(f"\n[step] accounts[4] attempts to buy {buy_amount / 10**decimals} tokens")
    buyer = accounts[4]
    if utils.buy_tokens_from_pool(analyzer, buyer, buy_amount):
        if ensure_eth_balance(buyer, gas_stipend_wei, gas_funder):
            print("[info] accounts[4] funded with 10 ETH for gas")
            sender = buyer
        else:
            print("[warn] Failed to fund accounts[4] with gas; trying other candidates.")
    else:
        print("[warn] Token purchase by accounts[4] failed")

    if sender is None:
        print("\n[step] Checking owner and pair creator balances")
        candidate_info = [
            ("Owner", analyzer.token_owner),
            ("Pair Creator", analyzer.pair_creator),
        ]

        for label, address in candidate_info:
            if sender is not None or not address:
                continue

            try:
                balance = analyzer.token.balanceOf(address)
            except Exception as exc:
                print(f"[warn] Unable to read {label} token balance: {exc}")
                continue

            if balance < amount:
                print(f"   {label} balance too low ({balance / 10**decimals} < {amount / 10**decimals})")
                continue

            try:
                candidate = accounts.at(to_checksum_address(address), force=True)
            except Exception as exc:
                print(f"[warn] Could not impersonate {label} account: {exc}")
                continue

            if not ensure_eth_balance(candidate, gas_stipend_wei, gas_funder):
                print(f"[warn] Unable to fund {label} account with 10 ETH for gas.")
                continue

            final_eth = candidate.balance()
            print(f"[info] Using {label} as sender (balance: {balance / 10**decimals} tokens and {final_eth / 10**18} ETH)")
            sender = candidate

    if sender is None:
        print("[error] Failed to prepare any account with tokens and gas for the transfer simulation.")
        return {
            "scenario": "exterior_function_call",
            "result": "UNKNOWN",
            "reverted": True,
            "external_calls_count": 0,
            "external_calls": [],
            "reason": "No account with sufficient tokens and gas for transfer trace",
        }

    print(f"\n[info] Sender: {sender.address}")

    final_balance = analyzer.token.balanceOf(sender.address)
    print(f"   Sender token balance: {final_balance / 10**decimals}\n")

    recipient = accounts[5].address
    print(f"[info] Recipient: {recipient}")

    result = analyze_transfer(analyzer.token, sender, recipient, amount,gas_limit, excluded_addresses=excluded_addresses)

    test_result = {
        "scenario": "exterior_function_call",
        "result": "YES" if len(result["external_calls"]) else "NO",
        "reverted": result["reverted"],
        "external_calls_count": len(result["external_calls"]),
        "external_calls": result["external_calls"],
    }

    return test_result
