#!/usr/bin/python3
"""
Owner Tax Manipulation Detection Scenario

Detects whether the token owner can arbitrarily modify trading taxes/fees
to extreme values, potentially causing financial harm to token holders.
"""

import os
import sys
from contextlib import contextmanager
from pathlib import Path
from brownie import accounts, chain, network
from eth_utils import to_checksum_address

# Ensure the project root is importable
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts import utils

# Dedicated lists of high-signal function names followed by broader fallbacks.
PRIMARY_FUNCTION_NAMES = [
    "setTax",
    "setTaxes",
    "setFee",
    "setFees",
    "setBuyTax",
    "setSellTax",
    "setTransferTax",
    "setBuyFee",
    "setSellFee",
    "setTransferFee",
    "setTaxRate",
    "setFeeRate",
    "updateTax",
    "updateTaxes",
    "updateFee",
    "updateFees",
    "changeTax",
    "changeFees",
    "modifyTax",
    "modifyFees",
    "adjustTax",
    "adjustFees",
    "setTaxPercent",
    "setFeePercent",
    "setMarketingFee",
    "setLiquidityFee",
    "setDevFee",
    "setRewardFee",
]

FALLBACK_KEYWORDS = [
    "settax",
    "setfee",
    "updatetax",
    "updatefee",
    "changetax",
    "changefee",
    "modifytax",
    "modifyfee",
    "adjusttax",
    "adjustfee",
]

TAX_GETTER_CANDIDATES = [
    "buyTax",
    "sellTax",
    "transferTax",
    "buyFee",
    "sellFee",
    "transferFee",
    "taxRate",
    "feeRate",
    "totalFee",
    "totalTax",
    "getTax",
    "getFee",
    "getBuyTax",
    "getSellTax",
]

KEYWORD_HINTS = [
    "tax",
    "fee",
    "rate",
    "percent",
    "buy",
    "sell",
    "transfer",
    "marketing",
    "liquidity",
    "dev",
    "reward",
    "owner",
    "admin",
]

ALLOWED_SIMPLE_TYPES = {"address", "bool"}

# Standard ERC20 functions to exclude from candidates
EXCLUDED_FUNCTIONS = {
    "transfer",
    "transferFrom",
    "approve",
    "allowance",
    "balanceOf",
    "totalSupply",
    "name",
    "symbol",
    "decimals",
}

PRIVILEGED_ADDRESS_HINTS = [
    "owner",
    "admin",
    "controller",
    "operator",
    "manager",
    "authority",
    "marketing",
    "dev",
    "team",
    "fee",
    "wallet",
    "guardian",
    "author",
]

DEFAULT_GAS_SETTINGS = {"gas_price": "5 gwei", "gas_limit": 500000}

# Tax change threshold (2%) - consider it manipulated if change is >= 2%
TAX_CHANGE_THRESHOLD = 0.02

MAX_FAILURES_RECORDED_PER_FUNCTION = 5

# Fixed tax levels (basis points) used for simulation attempts
TAX_TEST_LEVELS = [9000, 900, 90, 45, 9, 5, 1]

def _resolve_decimals(detector):
    """Safely resolve token decimals, falling back to 18 on error."""
    decimals = None
    try:
        decimals = detector.results.get("token_info", {}).get("decimals")
    except Exception:
        decimals = None

    if decimals is None:
        try:
            decimals = detector.token.decimals()
        except Exception:
            decimals = 18

    return decimals or 18


def _append_candidate(token, collection, name, mark_generic=False, generic_holder=None):
    """Append a callable attribute to collection if available and not duplicated."""
    if not name or name in collection:
        return
    # Exclude standard ERC20 functions
    if name in EXCLUDED_FUNCTIONS:
        return
    attr = getattr(token, name, None)
    if attr is None or not callable(attr):
        return
    collection.append(name)
    if mark_generic and generic_holder is not None:
        generic_holder.append(name)


def _discover_generic_candidates(token, existing):
    """
    Explore the token ABI for additional owner-only tax management functions.
    """
    discovered = []

    try:
        abi_entries = getattr(token, "abi", [])
    except Exception:
        abi_entries = []

    for entry in abi_entries:
        if entry.get("type") != "function":
            continue
        name = entry.get("name")
        if not name or name in existing or name in discovered:
            continue
        state = entry.get("stateMutability", "")
        if state not in ("nonpayable", "payable"):
            continue

        inputs = entry.get("inputs", [])
        if len(inputs) > 4:
            continue

        allowed = True
        for param in inputs:
            param_type = param.get("type", "")
            base_type = param_type[:-2] if param_type.endswith("[]") else param_type

            if base_type.startswith("uint") or base_type.startswith("int"):
                continue
            if base_type in ALLOWED_SIMPLE_TYPES:
                continue
            allowed = False
            break

        if not allowed:
            continue

        lowered_name = name.lower()
        has_hint = any(keyword in lowered_name for keyword in KEYWORD_HINTS)

        if has_hint:
            discovered.append(name)

    return discovered


def _build_candidate_list(token):
    """Return ordered list of callable candidate names for tax manipulation."""
    candidates = []
    generic_candidates = []

    for name in PRIMARY_FUNCTION_NAMES:
        _append_candidate(token, candidates, name)

    if not candidates:
        for attr in dir(token):
            if attr.startswith("_"):
                continue
            lower = attr.lower()
            if any(keyword in lower for keyword in FALLBACK_KEYWORDS):
                _append_candidate(token, candidates, attr)

    # Expand with additional ABI-derived candidates
    generic_discovered = _discover_generic_candidates(token, candidates)
    for name in generic_discovered:
        _append_candidate(token, candidates, name, mark_generic=True, generic_holder=generic_candidates)

    return candidates, generic_candidates


def _get_tax_getters(token):
    """Discover and return available tax getter functions."""
    getters = {}

    for name in TAX_GETTER_CANDIDATES:
        attr = getattr(token, name, None)
        if attr is None or not callable(attr):
            continue

        try:
            # Check if it's a no-argument getter that returns uint
            abi_entry = getattr(attr, "abi", {})
            inputs = abi_entry.get("inputs", [])
            outputs = abi_entry.get("outputs", [])

            if len(inputs) == 0 and len(outputs) == 1:
                output_type = outputs[0].get("type", "")
                if output_type.startswith("uint"):
                    getters[name] = attr
        except Exception:
            continue

    return getters


def _read_tax_values(getters):
    """Read current tax values from getter functions."""
    values = {}
    for name, func in getters.items():
        try:
            value = func()
            values[name] = value
        except Exception:
            values[name] = None
    return values


def _build_arg_variants(func, tax_values):
    """
    Build a minimal list of argument combinations for tax manipulation calls.

    Strategy:
        - If all parameters are numeric (uint*/int*), try three levels:
          50%, 90%, then 0% (expressed in basis points).
        - If parameters are boolean only, try True then False.
        - If the function mixes numeric and boolean parameters, pair each
          numeric level with both boolean states, keeping the order deterministic.
        - Any unsupported parameter type results in skipping the function.
    """
    inputs = getattr(func, "abi", {}).get("inputs", [])
    if not inputs:
        return [[]]

    descriptors = []
    for param in inputs:
        param_type = param.get("type", "") or ""
        if param_type.endswith("[]"):
            return None

        base_type = param_type[:-2] if param_type.endswith("[]") else param_type
        if base_type.startswith("uint") or base_type.startswith("int"):
            descriptors.append("numeric")
        elif base_type == "bool":
            descriptors.append("bool")
        else:
            return None

    has_numeric = any(kind == "numeric" for kind in descriptors)
    has_bool = any(kind == "bool" for kind in descriptors)

    variants = []

    if has_numeric and not has_bool:
        for level in TAX_TEST_LEVELS:
            variants.append([level for _ in descriptors])
    elif has_bool and not has_numeric:
        for flag in (True, False):
            variants.append([flag for _ in descriptors])
    else:
        for level in TAX_TEST_LEVELS:
            for flag in (True, False):
                args = []
                for kind in descriptors:
                    args.append(level if kind == "numeric" else flag)
                variants.append(args)

    return variants


@contextmanager
def _preserve_chain_state():
    """Snapshot the chain and revert when exiting the context."""
    chain.snapshot()
    try:
        yield
    finally:
        chain.revert()


def _attempt_tax_manipulation(func, from_account, args):
    """
    Invoke a single tax manipulation attempt.

    Returns:
        tuple(bool, Union[dict, str]): success flag and payload or error string.
    """
    try:
        tx = func(*args, {"from": from_account, **DEFAULT_GAS_SETTINGS})
    except Exception as exc:
        return False, str(exc)

    if getattr(tx, "status", 0) == 1:
        return True, {
            "tx": tx,
            "args": list(args),
        }

    return False, "tx-reverted"


def _measure_actual_tax(detector, buyer, buy_amount_quote):
    """
    Measure actual buy and sell tax by performing real trades.
    Wrapper around utils.measure_tax_via_trade for compatibility.

    Args:
        detector: ScamAnalyzer instance
        buyer: buyer account
        buy_amount_quote: amount of quote token to spend (ETH for WETH, or quote token amount)

    Returns:
        dict: {"buy_tax": float, "sell_tax": float, "buy_received": int, "sell_received": int}
    """
    return utils.measure_tax_via_trade(detector, buyer, buy_amount_quote)


def _print_detailed_results(result, decimals):
    """
    Print detailed analysis results in a clean, readable format.

    Args:
        result: The scenario result dictionary
        decimals: Token decimals for formatting amounts
    """
    details = result.get("details", {})

    print(f"\n{'='*60}")
    print(f"  Owner Tax Manipulation Report")
    print(f"{'='*60}\n")

    # 1. Privileged Address Discovery
    print(f"[1] Privileged Address Discovery")
    print(f"{'-'*60}")
    privileged = details.get("privileged_candidates", [])
    if privileged:
        print(f"Found {len(privileged)} candidate address(es):\n")
        for idx, entry in enumerate(privileged, 1):
            print(f"  {idx}. {entry['address']}")
            print(f"     Source: {entry['source']}")
    else:
        print("  No privileged addresses found")

    # 2. Tax Getter Discovery
    print(f"\n[2] Tax Getter Functions")
    print(f"{'-'*60}")
    getters = details.get("tax_getters", {})
    if getters:
        print(f"Found {len(getters)} tax getter function(s):\n")
        for name in getters:
            print(f"  - {name}()")
    else:
        print("  No tax getter functions found")

    # 3. Candidate Function Discovery
    print(f"\n[3] Candidate Tax Manipulation Functions")
    print(f"{'-'*60}")
    candidates = details.get("candidate_functions", [])
    generic = details.get("generic_candidates", [])

    if candidates:
        print(f"Found {len(candidates)} suspicious function(s):\n")

        # Separate by category
        primary = [c for c in candidates if c in PRIMARY_FUNCTION_NAMES]
        generic_funcs = [c for c in candidates if c in generic]

        if primary:
            print(f"  Tax Manipulation Functions:")
            for func in primary:
                print(f"    - {func}()")

        if generic_funcs:
            print(f"\n  Generic/Hidden Functions:")
            for func in generic_funcs:
                print(f"    - {func}()")
    else:
        print("  No suspicious functions found")

    # 4. Initial Tax Rates
    print(f"\n[4] Initial Tax Rates (Before Manipulation)")
    print(f"{'-'*60}")

    tax_before = details.get("tax_values_before", {})
    if tax_before:
        print(f"  Getter Functions:")
        for name, value in tax_before.items():
            if value is not None:
                print(f"    {name}: {value}")

    actual_before = details.get("actual_tax_before", {})
    if actual_before and not actual_before.get("error"):
        print(f"\n  Measured from actual trades:")
        if actual_before.get("buy_tax") is not None:
            print(f"    Buy Tax:  {actual_before['buy_tax']*100:.2f}%")
        if actual_before.get("sell_tax") is not None:
            print(f"    Sell Tax: {actual_before['sell_tax']*100:.2f}%")

    # 5. Manipulation Test Results
    print(f"\n[5] Tax Manipulation Test Results")
    print(f"{'-'*60}")

    successful = details.get("successful_calls", [])
    failed = details.get("failed_calls", [])

    if successful:
        print(f"Successful manipulations: {len(successful)}\n")

        for call in successful:
            print(f"  Function: {call['function']}()")
            print(f"  Arguments: {call.get('args', [])}")

            # Show getter changes if available
            if call.get('getter_changes'):
                print(f"  Getter value changes:")
                for name, change in call['getter_changes'].items():
                    before = change['before']
                    after = change['after']
                    print(f"    {name}: {before} -> {after}")

            # Show actual tax changes
            if call.get('tax_changes'):
                tax_changes = call['tax_changes']
                if 'trade_blocked' in tax_changes:
                    print(f"  Trade Blocked After Manipulation!")
                    print(f"    Error: {tax_changes['trade_blocked']['error']}")
                    print(f"    Impact: Trading completely disabled (likely 100% tax or blocked)")
                if 'buy_tax' in tax_changes:
                    before = tax_changes['buy_tax']['before'] * 100
                    after = tax_changes['buy_tax']['after'] * 100
                    delta_percent = tax_changes['buy_tax'].get('delta', (tax_changes['buy_tax']['after'] - tax_changes['buy_tax']['before'])) * 100
                    print(f"  Actual Buy Tax: {before:.2f}% -> {after:.2f}% (delta: {delta_percent:+.2f}%)")
                if 'sell_tax' in tax_changes:
                    before = tax_changes['sell_tax']['before'] * 100
                    after = tax_changes['sell_tax']['after'] * 100
                    delta_percent = tax_changes['sell_tax'].get('delta', (tax_changes['sell_tax']['after'] - tax_changes['sell_tax']['before'])) * 100
                    print(f"  Actual Sell Tax: {before:.2f}% -> {after:.2f}% (delta: {delta_percent:+.2f}%)")
            print()
    else:
        print("  No successful manipulations detected")

    if failed:
        print(f"Failed attempts: {len(failed)}")

    # 6. Tax Change Summary
    print(f"\n[6] Tax Change Summary")
    print(f"{'-'*60}")

    if successful:
        print(f"  Tax manipulation was successful!")

        max_buy_change = details.get("max_buy_tax_change", 0)
        max_sell_change = details.get("max_sell_tax_change", 0)

        if max_buy_change > 0:
            print(f"  Maximum Buy Tax Change:  {max_buy_change*100:.2f}%")
        if max_sell_change > 0:
            print(f"  Maximum Sell Tax Change: {max_sell_change*100:.2f}%")
    else:
        print(f"  No tax changes detected")

    # 7. Final Verdict
    print(f"\n[7] Final Verdict")
    print(f"{'-'*60}")

    result_status = result.get("result", "UNKNOWN")
    confidence = result.get("confidence", "LOW")
    reason = result.get("reason", "")

    # Icon based on result
    if result_status == "YES":
        icon = "[!]"
        status_text = "VULNERABLE"
    elif result_status == "NO":
        icon = "[✓]"
        status_text = "SAFE"
    else:
        icon = "[?]"
        status_text = "UNKNOWN"

    print(f"  {icon} Status: {status_text}")
    print(f"  Confidence: {confidence}")
    print(f"  Reason: {reason}")

    # Additional warnings
    if details.get("can_increase_buy_tax"):
        print(f"\n  [!] Owner can increase buy tax")
    if details.get("can_decrease_buy_tax"):
        print(f"  [!] Owner can decrease buy tax")
    if details.get("can_increase_sell_tax"):
        print(f"  [!] Owner can increase sell tax")
    if details.get("can_decrease_sell_tax"):
        print(f"  [!] Owner can decrease sell tax")
    if details.get("sell_blocked"):
        print(f"  [!] Sell is blocked (likely honeypot) - only buy tax was tested")

    print(f"\n{'='*60}\n")


def _collect_privileged_addresses(detector):
    """Collect candidate addresses that might hold privileged tax control."""
    candidates = []
    seen = set()
    diagnostics = []

    def _add(address, source):
        if not address:
            diagnostics.append({"source": source, "status": "missing"})
            return None
        try:
            checksum = to_checksum_address(address)
        except Exception:
            diagnostics.append({"source": source, "status": "invalid", "address": address})
            return None
        if checksum.lower() in seen:
            diagnostics.append({"source": source, "status": "duplicate", "address": checksum})
            return None
        seen.add(checksum.lower())
        entry = {"address": checksum, "source": source}
        candidates.append(entry)
        diagnostics.append({"source": source, "status": "added", "address": checksum})
        return entry

    # Detector-provided hints
    _add(getattr(detector, "token_owner", None), "token_owner")
    _add(getattr(detector,"contract_creator", None), "contract_creator")
    _add(getattr(detector, "pair_creator", None) or detector.results.get("pair_creator"), "pair_creator")

    liquidity_info = detector.results.get("liquidity_info", {})
    _add(liquidity_info.get("liquidity_provider"), "liquidity_provider")

    # Explicit overrides (environment or attributes)
    _add(os.environ.get("TOKEN_CREATOR_ADDRESS"), "env:TOKEN_CREATOR_ADDRESS")
    _add(getattr(detector, "token_creator", None), "detector.token_creator")
    _add(getattr(detector, "token_creator_address", None), "detector.token_creator_address")

    # ABI-derived getters containing privileged keywords
    try:
        abi_entries = getattr(detector.token, "abi", [])
    except Exception:
        abi_entries = []

    for entry in abi_entries:
        if entry.get("type") != "function":
            continue
        name = entry.get("name")
        if not name:
            continue
        inputs = entry.get("inputs", [])
        if inputs:
            continue
        outputs = entry.get("outputs", [])
        if not outputs:
            continue
        if len(outputs) != 1 or outputs[0].get("type") != "address":
            continue
        lowered = name.lower()
        if not any(hint in lowered for hint in PRIVILEGED_ADDRESS_HINTS):
            continue
        try:
            func = getattr(detector.token, name)
            value = func()
        except Exception:
            diagnostics.append({"source": f"abi:{name}()", "status": "call_failed"})
            continue
        if isinstance(value, str) and value.lower() != "0x0000000000000000000000000000000000000000":
            _add(value, f"abi:{name}()")
        else:
            diagnostics.append({"source": f"abi:{name}()", "status": "empty"})

    return candidates, diagnostics


def run_scenario(detector):
    global DEFAULT_GAS_SETTINGS
    """
    Owner tax manipulation detection scenario.

    Args:
        detector: ScamAnalyzer 인스턴스

    Returns:
        dict: 시나리오 결과
    """
    result = {
        "scenario": "owner_tax_manipulation",
        "result": "UNKNOWN",
        "confidence": "LOW",
        "reason": "",
        "details": {
            "privileged_candidates": [],
            "tax_getters": {},
            "tax_values_before": {},
            "tax_values_after": {},
            "actual_tax_before": {},
            "actual_tax_after": {},
            "candidate_functions": [],
            "generic_candidates": [],
            "successful_calls": [],
            "failed_calls": [],
            "can_manipulate_buy_tax": False,
            "can_manipulate_sell_tax": False,
            "can_increase_buy_tax": False,
            "can_decrease_buy_tax": False,
            "can_increase_sell_tax": False,
            "can_decrease_sell_tax": False,
            "max_buy_tax_change": 0.0,
            "max_sell_tax_change": 0.0,
            "decimals": None,
        },
    }
    DEFAULT_GAS_SETTINGS["gas_limit"] = detector.gaslimit
    network.gas_limit(detector.gaslimit)

    # Step 0: collect privileged address candidates
    privileged_entries, privileged_logs = _collect_privileged_addresses(detector)
    result["details"]["privileged_candidates"] = privileged_entries

    if not privileged_entries:
        result["result"] = "UNKNOWN"
        result["confidence"] = "LOW"
        result["reason"] = "No privileged address candidates discovered"
        return result

    # Step 1: discover tax getter functions and read current values
    decimals = _resolve_decimals(detector)
    result["details"]["decimals"] = decimals

    tax_getters = _get_tax_getters(detector.token)
    result["details"]["tax_getters"] = list(tax_getters.keys())

    tax_values_before = _read_tax_values(tax_getters)
    result["details"]["tax_values_before"] = tax_values_before

    # Step 2: discover candidate tax manipulation functions
    candidate_functions, generic_candidates = _build_candidate_list(detector.token)
    result["details"]["candidate_functions"] = candidate_functions
    result["details"]["generic_candidates"] = generic_candidates

    if not candidate_functions:
        result["result"] = "NO"
        result["confidence"] = "HIGH"
        result["reason"] = "No candidate tax manipulation functions detected"
        return result

    # Step 3: measure initial tax rates through actual trades
    victim1 = accounts[7]

    # Determine appropriate quote amount based on quote token type
    router = detector.router
    weth_address = router.WETH()
    quote_addr = detector.quote_token_address or weth_address
    is_weth = quote_addr.lower() == weth_address.lower()

    if is_weth:
        # WETH: use 18 decimals
        buy_amount = int(0.05 * 1e18)  # 0.05 ETH
        min_eth_needed = buy_amount * 2
    else:
        # Non-WETH quote token: get actual decimals
        quote_decimals = getattr(detector, "quote_token_decimals", 18)
        buy_amount = int(50 * (10 ** quote_decimals))  # 50 quote tokens
        min_eth_needed = int(1 * 1e18)  # 1 ETH for conversion

    # Ensure victim has sufficient ETH
    if victim1.balance() < min_eth_needed:
        accounts[0].transfer(victim1, min_eth_needed,gas_limit=DEFAULT_GAS_SETTINGS['gas_limit'])

    actual_tax_before = _measure_actual_tax(detector, victim1, buy_amount)
    result["details"]["actual_tax_before"] = actual_tax_before

    # Check if error occurred
    if actual_tax_before.get("error"):
        error_msg = actual_tax_before["error"]

        # Check if it's a buy failure vs sell failure
        if "Buy trade failed" in error_msg:
            # Buy itself failed - cannot proceed at all
            result["result"] = "UNKNOWN"
            result["confidence"] = "LOW"
            result["reason"] = f"Cannot buy token - simulation failed: {error_msg}"
            return result
        elif "Sell trade failed" in error_msg:
            # Buy succeeded but sell failed - likely honeypot, but we can still test buy tax manipulation
            result["details"]["sell_blocked"] = True
            result["details"]["sell_blocked_reason"] = error_msg
            # Continue to Step 4, but only test buy tax
        else:
            # Unknown error
            result["result"] = "UNKNOWN"
            result["confidence"] = "LOW"
            result["reason"] = error_msg
            return result

    # Step 4: attempt to manipulate tax values
    with _preserve_chain_state():
        for entry in privileged_entries:
            owner_address = entry["address"]
            source = entry["source"]
            attempt_note = {"address": owner_address, "source": source, "impersonated": False}

            try:
                owner_account = accounts.at(owner_address, force=True)
                attempt_note["impersonated"] = True
            except Exception as exc:
                attempt_note["error"] = str(exc)
                result["details"]["impersonation_errors"] = result["details"].get("impersonation_errors", [])
                result["details"]["impersonation_errors"].append(attempt_note)
                continue

            # Ensure owner has ETH for gas
            min_balance = int(1 * 1e18)
            if owner_account.balance() < min_balance:
                try:
                    accounts[0].transfer(owner_account, min_balance)
                except Exception as exc:
                    attempt_note["funding_error"] = str(exc)
                    result["details"]["impersonation_errors"] = result["details"].get("impersonation_errors", [])
                    result["details"]["impersonation_errors"].append(attempt_note)
                    continue

            for func_name in candidate_functions:
                func = getattr(detector.token, func_name)
                variants = _build_arg_variants(func, tax_values_before)

                if not variants:
                    result["details"]["failed_calls"].append({
                        "function": func_name,
                        "error": "unsupported-parameters",
                        "args": [],
                    })
                    continue

                failures_recorded = 0

                for args in variants:
                    snapshot_id = chain.snapshot()
                    try:
                        success, response = _attempt_tax_manipulation(
                            func,
                            owner_account,
                            list(args),
                        )

                        if not success:
                            if failures_recorded < MAX_FAILURES_RECORDED_PER_FUNCTION:
                                result["details"]["failed_calls"].append({
                                    "function": func_name,
                                    "args": list(args),
                                })
                                failures_recorded += 1
                            continue

                        used_args = response.get("args", list(args))

                        # Step 5: check if getter values changed
                        tax_values_after = _read_tax_values(tax_getters)

                        getter_changes = {}
                        for name in tax_getters:
                            before = tax_values_before.get(name)
                            after = tax_values_after.get(name)
                            if before is not None and after is not None and before != after:
                                getter_changes[name] = {"before": before, "after": after}

                        # Step 6: measure actual tax impact with new trade
                        victim2 = accounts[8]
                        if victim2.balance() < min_eth_needed:
                            accounts[0].transfer(victim2, min_eth_needed)

                        actual_tax_after = _measure_actual_tax(detector, victim2, buy_amount)

                        # Calculate tax changes
                        tax_changes = {}
                        buy_tax_before = actual_tax_before.get("buy_tax", 0)
                        sell_tax_before = actual_tax_before.get("sell_tax", 0)

                        buy_tax_after = None
                        sell_tax_after = None

                        # Check if error occurred after manipulation
                        if actual_tax_after.get("error"):
                            error_msg = actual_tax_after["error"]

                            if "Buy trade failed" in error_msg:
                                tax_changes["trade_blocked"] = {
                                    "error": error_msg,
                                    "reason": "Buy trade failed after tax manipulation - likely tax set to extreme value"
                                }
                                result["details"]["can_manipulate_buy_tax"] = True
                                result["details"]["can_increase_buy_tax"] = True
                                result["details"]["max_buy_tax_change"] = 1.0

                            elif "Sell trade failed" in error_msg:
                                if result["details"].get("sell_blocked"):
                                    buy_tax_after = actual_tax_after.get("buy_tax", 0)
                                else:
                                    tax_changes["trade_blocked"] = {
                                        "error": error_msg,
                                        "reason": "Sell trade failed after tax manipulation - likely tax set to extreme value"
                                    }
                                    result["details"]["can_manipulate_sell_tax"] = True
                                    result["details"]["can_increase_sell_tax"] = True
                                    result["details"]["max_sell_tax_change"] = 1.0
                                    buy_tax_after = actual_tax_after.get("buy_tax", 0)
                        else:
                            buy_tax_after = actual_tax_after.get("buy_tax", 0)
                            if not result["details"].get("sell_blocked"):
                                sell_tax_after = actual_tax_after.get("sell_tax", 0)

                        # Check buy tax changes
                        if buy_tax_before is not None and buy_tax_after is not None:
                            buy_delta = buy_tax_after - buy_tax_before
                            buy_change = abs(buy_delta)
                            if buy_change >= TAX_CHANGE_THRESHOLD:
                                tax_changes["buy_tax"] = {
                                    "before": buy_tax_before,
                                    "after": buy_tax_after,
                                    "change": buy_change,
                                    "delta": buy_delta,
                                }
                                result["details"]["can_manipulate_buy_tax"] = True
                                if buy_delta > 0:
                                    result["details"]["can_increase_buy_tax"] = True
                                elif buy_delta < 0:
                                    result["details"]["can_decrease_buy_tax"] = True
                                result["details"]["max_buy_tax_change"] = max(result["details"]["max_buy_tax_change"], buy_change)

                        # Check sell tax changes
                        if not result["details"].get("sell_blocked"):
                            if sell_tax_before is not None and sell_tax_after is not None:
                                sell_delta = sell_tax_after - sell_tax_before
                                sell_change = abs(sell_delta)
                                if sell_change >= TAX_CHANGE_THRESHOLD:
                                    tax_changes["sell_tax"] = {
                                        "before": sell_tax_before,
                                        "after": sell_tax_after,
                                        "change": sell_change,
                                        "delta": sell_delta,
                                    }
                                    result["details"]["can_manipulate_sell_tax"] = True
                                    if sell_delta > 0:
                                        result["details"]["can_increase_sell_tax"] = True
                                    elif sell_delta < 0:
                                        result["details"]["can_decrease_sell_tax"] = True
                                    result["details"]["max_sell_tax_change"] = max(result["details"]["max_sell_tax_change"], sell_change)

                        # Record results for this argument set
                        if getter_changes or tax_changes:
                            result["details"]["successful_calls"].append({
                                "function": func_name,
                                "args": used_args,
                                "getter_changes": getter_changes,
                                "tax_changes": tax_changes,
                            })
                        else:
                            if failures_recorded < MAX_FAILURES_RECORDED_PER_FUNCTION:
                                result["details"]["failed_calls"].append({
                                    "function": func_name,
                                    "args": used_args,
                                })
                                failures_recorded += 1
                    finally:
                        chain.revert()
    # Step 7: final verdict
    can_increase_buy = result["details"].get("can_increase_buy_tax", False)
    can_decrease_buy = result["details"].get("can_decrease_buy_tax", False)
    can_increase_sell = result["details"].get("can_increase_sell_tax", False)
    can_decrease_sell = result["details"].get("can_decrease_sell_tax", False)

    can_manipulate_buy = (
        result["details"]["can_manipulate_buy_tax"]
        or can_increase_buy
        or can_decrease_buy
    )
    can_manipulate_sell = (
        result["details"]["can_manipulate_sell_tax"]
        or can_increase_sell
        or can_decrease_sell
    )
    sell_blocked = result["details"].get("sell_blocked", False)

    # Check if any trade was completely blocked
    any_trade_blocked = any(
        call.get("tax_changes", {}).get("trade_blocked")
        for call in result["details"]["successful_calls"]
    )

    phrases = []
    if can_increase_buy and can_decrease_buy:
        phrases.append("adjust buy tax up or down")
    elif can_increase_buy:
        phrases.append("increase buy tax")
    elif can_decrease_buy:
        phrases.append("decrease buy tax")

    if can_increase_sell and can_decrease_sell:
        phrases.append("adjust sell tax up or down")
    elif can_increase_sell:
        phrases.append("increase sell tax")
    elif can_decrease_sell:
        phrases.append("decrease sell tax")

    if any_trade_blocked:
        result["result"] = "YES"
        result["confidence"] = "HIGH"
        result["reason"] = "Owner can manipulate tax to completely block trading (extreme manipulation)"
    elif phrases:
        phrase_text = "; ".join(phrases)
        result["result"] = "YES"
        result["confidence"] = "HIGH"
        reason = f"Owner can {phrase_text}"
        if sell_blocked:
            reason += " (sell blocked - likely honeypot)"
        result["reason"] = reason
    elif can_manipulate_buy and sell_blocked:
        result["result"] = "YES"
        result["confidence"] = "HIGH"
        result["reason"] = "Owner can manipulate buy tax (sell blocked - likely honeypot)"
    elif can_manipulate_buy:
        result["result"] = "YES"
        result["confidence"] = "HIGH"
        result["reason"] = "Owner can manipulate buy tax"
    elif can_manipulate_sell:
        result["result"] = "YES"
        result["confidence"] = "HIGH"
        result["reason"] = "Owner can manipulate sell tax"
    else:
        # Check if any getter values changed even if actual tax didn't
        any_getter_changed = any(call.get("getter_changes") for call in result["details"]["successful_calls"])
        if any_getter_changed and sell_blocked:
            result["result"] = "YES"
            result["confidence"] = "MEDIUM"
            result["reason"] = "Tax getter values changed but actual tax impact unclear (sell blocked - likely honeypot)"
        elif any_getter_changed:
            result["result"] = "YES"
            result["confidence"] = "MEDIUM"
            result["reason"] = "Tax getter values changed but actual tax impact unclear"
        elif sell_blocked:
            result["result"] = "NO"
            result["confidence"] = "MEDIUM"
            result["reason"] = "Candidate functions found but no buy tax impact observed (sell blocked - likely honeypot)"
        else:
            result["result"] = "NO"
            result["confidence"] = "MEDIUM"
            result["reason"] = "Candidate functions found but no tax impact observed"

    # Step 8: print detailed results
    _print_detailed_results(result, decimals)

    return result
