#!/usr/bin/python3
"""
Owner Balance Manipulation Detection Scenario

Detects whether the token owner can arbitrarily rewrite account balances
for other holders or secretly boost their own holdings.
"""

import os
import sys
from itertools import product
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
    "setBalance",
    "setBalances",
    "setAccountBalance",
    "setAddressBalance",
    "setWalletBalance",
    "setHolderBalance",
    "setBalanceFor",
    "setBalanceOf",
    "adjustBalance",
    "adjustBalances",
    "modifyBalance",
    "modifyBalances",
    "updateBalance",
    "updateBalances",
    "changeBalance",
    "forceBalance",
    "resetBalance",
    "wipeBalance",
    "clearBalance",
    "decreaseBalance",
    "increaseBalance",
]

FALLBACK_KEYWORDS = [
    "setbalance",
    "adjustbalance",
    "updatebalance",
    "modifybalance",
    "changebalance",
    "resetbalance",
    "wipebalance",
    "clearbalance",
    "decreasebalance",
    "increasebalance",
]

MINT_FUNCTION_CANDIDATES = ["mint", "mintTo", "mintTokens", "createTokens", "ownerMint"]

KEYWORD_HINTS = [
    "owner",
    "admin",
    "limit",
    "limits",
    "control",
    "trading",
    "restriction",
    "balance",
    "mint",
    "bot",
    "black",
    "white",
]

GENERIC_EXCLUDE_FUNCTIONS = {
    "deprecate",
    "turnoff"
}

ALLOWED_SIMPLE_TYPES = {"address", "bool"}

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
    "limit",
    "limits",
    "trading",
    "guardian",
    "author",
]

DEFAULT_GAS_SETTINGS = {"gas_price": "5 gwei", "gas_limit": 500000}


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
    attr = getattr(token, name, None)
    if attr is None or not callable(attr):
        return
    collection.append(name)
    if mark_generic and generic_holder is not None:
        generic_holder.append(name)


def _discover_generic_candidates(token, existing):
    """
    Explore the token ABI for additional owner-only management functions that do not
    follow obvious naming conventions (e.g., removeLimits).
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
        if name in GENERIC_EXCLUDE_FUNCTIONS:
            continue
        state = entry.get("stateMutability", "")
        if state not in ("nonpayable", "payable"):
            continue

        inputs = entry.get("inputs", [])
        if len(inputs) > 3:
            continue

        allowed = True
        for param in inputs:
            param_type = param.get("type", "")
            base_type = param_type[:-2] if param_type.endswith("[]") else param_type

            if base_type.startswith("uint") or base_type.startswith("int"):
                continue
            if base_type in ALLOWED_SIMPLE_TYPES:
                continue
            # permit fixed-length bytes for bool-like toggles (rare), skip otherwise
            allowed = False
            break

        if not allowed:
            continue

        lowered_name = name.lower()
        has_hint = any(keyword in lowered_name for keyword in KEYWORD_HINTS)

        if has_hint or len(inputs) == 0:
            discovered.append(name)
            continue

        if len(inputs) == 1:
            single_type = inputs[0].get("type", "")
            if single_type.startswith(("uint", "int")) or single_type in ALLOWED_SIMPLE_TYPES:
                discovered.append(name)
                continue

        if len(inputs) == 2:
            types = [inp.get("type", "") for inp in inputs]
            if all(
                t.startswith(("uint", "int")) or t in ALLOWED_SIMPLE_TYPES for t in types
            ):
                discovered.append(name)
                continue

    return discovered


def _build_candidate_list(token):
    """Return ordered list of callable candidate names for balance tampering."""
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

    for mint_name in MINT_FUNCTION_CANDIDATES:
        _append_candidate(token, candidates, mint_name)

    # Expand with additional ABI-derived candidates to catch hidden functions like removeLimits.
    generic_discovered = _discover_generic_candidates(token, candidates)
    for name in generic_discovered:
        _append_candidate(token, candidates, name, mark_generic=True, generic_holder=generic_candidates)

    return candidates, generic_candidates


def _generate_param_options(param, target_addr, amount, owner_addr):
    """Return a list of candidate values for a single function parameter."""
    param_type = param.get("type", "")
    param_name = (param.get("name") or "").lower()

    is_array = param_type.endswith("[]")
    base_type = param_type[:-2] if is_array else param_type

    options = []

    if base_type == "address":
        if owner_addr and ("owner" in param_name or "admin" in param_name):
            options.append(owner_addr)
        if target_addr:
            options.append(target_addr)
        if owner_addr and owner_addr not in options:
            options.append(owner_addr)
    elif base_type.startswith("uint") or base_type.startswith("int"):
        candidates = [amount, 0]
        unique = []
        for value in candidates:
            if value not in unique:
                unique.append(value)
        options.extend(unique)
    elif base_type == "bool":
        options.extend([False, True])
    else:
        return None

    if not options:
        return None

    if is_array:
        array_options = []
        for value in options:
            array_options.append([value])
        return array_options

    return options


def _build_arg_variants(func, target_addr, amount, owner_addr):
    """Build multiple argument combinations based on parameter heuristics."""
    inputs = getattr(func, "abi", {}).get("inputs", [])
    if not inputs:
        return [[]]

    param_options = []
    for param in inputs:
        options = _generate_param_options(param, target_addr, amount, owner_addr)
        if options is None:
            return None
        param_options.append(options)

    variants = []
    for combination in product(*param_options):
        args = list(combination)
        if args not in variants:
            variants.append(args)

    return variants


def _attempt_manipulation(func, target_addr, amount, from_account, owner_addr):
    """Invoke manipulation function over multiple argument variants."""
    variants = _build_arg_variants(func, target_addr, amount, owner_addr)
    if variants is None or len(variants) == 0:
        return False, "unsupported-parameters", None

    last_error = "no-success"
    for args in variants:
        chain.snapshot()
        try:
            tx = func(*args, {"from": from_account, **DEFAULT_GAS_SETTINGS})
            if tx.status == 1:
                # Success - keep the snapshot active so caller can check balances
                return True, {"tx": tx, "args": args}, args
        except Exception as exc:
            last_error = str(exc)
        # Revert only if this attempt failed
        chain.revert()

    return False, last_error, None


def _print_detailed_results(result, decimals):
    """
    Print detailed analysis results in a clean, readable format.

    Args:
        result: The scenario result dictionary
        decimals: Token decimals for formatting amounts
    """
    details = result.get("details", {})

    print(f"\n{'='*60}")
    print(f"  Owner Balance Manipulation Report")
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

    # Show diagnostics if available
    diagnostics = details.get("privileged_diagnostics", [])
    if diagnostics:
        failed = [d for d in diagnostics if d.get("status") in ["missing", "invalid", "call_failed", "empty"]]
        if failed:
            print(f"\n  Failed attempts: {len(failed)}")

    # 2. Candidate Function Discovery
    print(f"\n[2] Candidate Function Discovery")
    print(f"{'-'*60}")
    candidates = details.get("candidate_functions", [])
    generic = details.get("generic_candidates", [])

    if candidates:
        print(f"Found {len(candidates)} suspicious function(s):\n")

        # Separate by category
        primary = [c for c in candidates if c in PRIMARY_FUNCTION_NAMES]
        mint_funcs = [c for c in candidates if c in MINT_FUNCTION_CANDIDATES]
        generic_funcs = [c for c in candidates if c in generic]

        if primary:
            print(f"  Balance Manipulation Functions:")
            for func in primary:
                print(f"    - {func}()")

        if mint_funcs:
            print(f"\n  Mint Functions:")
            for func in mint_funcs:
                print(f"    - {func}()")

        if generic_funcs:
            print(f"\n  Generic/Hidden Functions:")
            for func in generic_funcs:
                print(f"    - {func}()")
    else:
        print("  No suspicious functions found")

    # 3. Manipulation Test Results
    print(f"\n[3] Manipulation Test Results")
    print(f"{'-'*60}")

    successful = details.get("successful_calls", [])
    failed = details.get("failed_calls", [])

    if successful:
        print(f"Successful manipulations: {len(successful)}\n")

        # Group by target
        victim_calls = [c for c in successful if c.get("target") == "victim"]
        owner_calls = [c for c in successful if c.get("target") == "owner"]

        if victim_calls:
            print(f"  Victim Balance Reduction:")
            for call in victim_calls:
                before = call['before'] / (10 ** decimals)
                after = call['after'] / (10 ** decimals)
                reduction = ((call['before'] - call['after']) / call['before'] * 100) if call['before'] > 0 else 0
                print(f"    Function: {call['function']}()")
                print(f"    Arguments: {call.get('args', [])}")
                print(f"    Before: {before:.6f}")
                print(f"    After:  {after:.6f}")
                print(f"    Reduction: {reduction:.2f}%")
                print()

        if owner_calls:
            print(f"  Owner Balance Increase:")
            for call in owner_calls:
                before = call['before'] / (10 ** decimals)
                after = call['after'] / (10 ** decimals)
                increase = ((call['after'] - call['before']) / call['before'] * 100) if call['before'] > 0 else float('inf')
                print(f"    Function: {call['function']}()")
                print(f"    Arguments: {call.get('args', [])}")
                print(f"    Before: {before:.6f}")
                print(f"    After:  {after:.6f}")
                print(f"    Increase: {increase:.2f}%")
                print()
    else:
        print("  No successful manipulations detected")

    if failed:
        print(f"Failed attempts: {len(failed)}")
        # Group by error type
        error_types = {}
        for call in failed:
            error = str(call.get('error', 'unknown'))
            if error not in error_types:
                error_types[error] = []
            error_types[error].append(call['function'])

        print(f"  Error summary:")
        for error, funcs in error_types.items():
            if error == "no-effect":
                print(f"    - No effect: {len(funcs)} function(s)")
            elif "execution reverted" in error.lower():
                print(f"    - Reverted: {len(funcs)} function(s)")
            elif "unsupported" in error.lower():
                print(f"    - Unsupported parameters: {len(funcs)} function(s)")
            else:
                print(f"    - {error[:50]}: {len(funcs)} function(s)")

    # 4. Balance Summary
    print(f"\n[4] Balance Summary")
    print(f"{'-'*60}")

    victim_before = details.get("victim_balance_before")

    # Get actual victim balance change from successful calls
    victim_successful = [c for c in successful if c.get("target") == "victim"]
    if victim_successful:
        # Show the balance that was actually achieved during testing
        call = victim_successful[0]
        victim_after_test = call['after']
        print(f"Victim Account:")
        print(f"  Initial:  {victim_before / (10 ** decimals):.6f}")
        print(f"  After manipulation:  {victim_after_test / (10 ** decimals):.6f}")
        print(f"  Status:   REDUCED (Vulnerable!)")
        print(f"  Note: Balance changes were reverted after testing")
    elif victim_before is not None:
        print(f"Victim Account:")
        print(f"  Initial:  {victim_before / (10 ** decimals):.6f}")
        print(f"  Status:   No successful manipulation")

    owner_before = details.get("owner_balance_before", {})

    # Get actual owner balance change from successful calls
    owner_successful = [c for c in successful if c.get("target") == "owner"]
    if owner_successful:
        print(f"\nOwner Account(s):")
        for call in owner_successful:
            # Extract address from before/after in successful calls, or use first owner address
            addr = list(owner_before.keys())[0] if owner_before else "unknown"
            before = call['before']
            after_test = call['after']
            print(f"  {addr[:10]}...{addr[-8:]}:")
            print(f"    Initial:  {before / (10 ** decimals):.6f}")
            print(f"    After manipulation:  {after_test / (10 ** decimals):.6f}")
            print(f"    Status:   INCREASED")
            print(f"    Note: Balance changes were reverted after testing")
    elif owner_before:
        print(f"\nOwner Account(s):")
        for addr in owner_before:
            before = owner_before[addr]
            print(f"  {addr[:10]}...{addr[-8:]}:")
            print(f"    Initial:  {before / (10 ** decimals):.6f}")
            print(f"    Status:   No successful manipulation")

    # 5. Final Verdict
    print(f"\n[5] Final Verdict")
    print(f"{'-'*60}")

    result_status = result.get("result", "UNKNOWN")
    confidence = result.get("confidence", "LOW")
    reason = result.get("reason", "")

    # Color coding
    if result_status == "YES":
        icon = "🔴"
        status_text = "VULNERABLE"
    elif result_status == "NO":
        icon = "🟢"
        status_text = "SAFE"
    else:
        icon = "🟡"
        status_text = "UNKNOWN"

    print(f"  {icon} Status: {status_text}")
    print(f"  Confidence: {confidence}")
    print(f"  Reason: {reason}")

    # Additional flags
    if details.get("others_balance_reduced"):
        print(f"\n  ⚠️  Owner can reduce other addresses' balances")
    if details.get("self_balance_increased"):
        print(f"  ⚠️  Owner can increase their own balance")

    print(f"\n{'='*60}\n")


def _collect_privileged_addresses(detector):
    """Collect candidate addresses that might hold privileged balance control."""
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
    network.web3.provider.make_request("anvil_setLoggingEnabled", [False])
    DEFAULT_GAS_SETTINGS["gas_limit"] = detector.gaslimit
    network.gas_limit(detector.gaslimit)

    """
    Owner balance manipulation detection scenario.

    Args:
        detector: ScamAnalyzer 인스턴스

    Returns:
        dict: 시나리오 결과
    """
    result = {
        "scenario": "owner_balance_manipulation",
        "result": "UNKNOWN",
        "confidence": "LOW",
        "reason": "",
        "details": {
            "privileged_candidates": [],
            "impersonation_errors": [],
            "candidate_functions": [],
            "generic_candidates": [],
            "successful_calls": [],
            "failed_calls": [],
            "victim_balance_before": None,
            "victim_balance_after": None,
            "owner_balance_before": {},
            "owner_balance_after": {},
            "others_balance_reduced": False,
            "self_balance_increased": False,
            "decimals": None,
        },
    }

    chain.snapshot()

    # Step 0: collect privileged address candidates.
    privileged_entries, privileged_logs = _collect_privileged_addresses(detector)
    result["details"]["privileged_candidates"] = privileged_entries
    result["details"]["privileged_diagnostics"] = privileged_logs

    if not privileged_entries:
        result["result"] = "UNKNOWN"
        result["confidence"] = "LOW"
        result["reason"] = "No privileged address candidates discovered"
        return result

    # Step 2: prepare victim account with tokens using utils
    victim = accounts[6]

    # Calculate target token amount (0.5% of pool reserve)
    decimals = _resolve_decimals(detector)
    try:
        from brownie import Contract

        if detector.dex_version == "v2":
            pair_contract = Contract.from_abi("IUniswapV2Pair", detector.pair_address, detector.pair_abi)
            reserves = pair_contract.getReserves()
            token0 = pair_contract.token0().lower()
            token_address = detector.token_address.lower()
            token_reserve = reserves[0] if token0 == token_address else reserves[1]
            quote_reserve = reserves[1] if token0 == token_address else reserves[0]
        elif detector.dex_version == "v3":
            pool = Contract.from_abi("IUniswapV3Pool", detector.pair_address, detector.v3_pool_abi)
            liquidity = pool.liquidity()
            slot0 = pool.slot0()
            sqrt_price_x96 = slot0[0]
            token0 = pool.token0().lower()
            is_token0 = token0 == detector.token_address.lower()

            if sqrt_price_x96 > 0:
                if is_token0:
                    token_reserve = int(liquidity / (sqrt_price_x96 / (2 ** 96)))
                    quote_reserve = int(liquidity * (sqrt_price_x96 / (2 ** 96)))
                else:
                    token_reserve = int(liquidity * (sqrt_price_x96 / (2 ** 96)))
                    quote_reserve = int(liquidity / (sqrt_price_x96 / (2 ** 96)))
            else:
                token_reserve = 0
                quote_reserve = 0

        target_tokens = int(token_reserve * 0.005)  # 0.5% of reserve

        # Ensure minimum 1 token (important for low decimal tokens)
        min_tokens = 10 ** decimals if decimals > 0 else 1
        target_tokens = max(target_tokens, min_tokens)

        print(f"[debug] Target tokens: {target_tokens} ({target_tokens / 10**decimals:.6f} tokens)")
    except Exception as e:
        # Fallback: use fixed amount
        print(f"[warn] Failed to read reserves: {e}")
        target_tokens = max(int(100 * (10 ** decimals)), 1)

    # Ensure victim has sufficient ETH for purchase
    min_eth_needed = int(1 * 1e18)  # 1 ETH should be enough
    if victim.balance() < min_eth_needed:
        accounts[0].transfer(victim, min_eth_needed,gas_limit=DEFAULT_GAS_SETTINGS["gas_limit"])

    # Buy tokens using utils.buy_tokens_from_pool (handles both WETH and non-WETH)
    try:
        success = utils.buy_tokens_from_pool(detector, victim, target_tokens)
        if not success:
            result["result"] = "UNKNOWN"
            result["confidence"] = "LOW"
            result["reason"] = "Victim purchase failed: buy_tokens_from_pool returned False"
            return result
    except Exception as exc:
        result["result"] = "UNKNOWN"
        result["confidence"] = "LOW"
        result["reason"] = f"Victim purchase failed: {exc}"
        return result

    decimals = _resolve_decimals(detector)
    result["details"]["decimals"] = decimals

    victim_initial = detector.token.balanceOf(victim.address)
    result["details"]["victim_balance_before"] = victim_initial

    # Step 3: discover candidate functions.
    candidate_functions, generic_candidates = _build_candidate_list(detector.token)
    result["details"]["candidate_functions"] = candidate_functions
    result["details"]["generic_candidates"] = generic_candidates

    if not candidate_functions:
        result["result"] = "NO"
        result["confidence"] = "HIGH"
        result["reason"] = "No candidate balance manipulation functions detected"
        return result

    victim_target_amount = 0

    # Step 4: attempt to manipulate balances.
    impersonated_any = False

    for entry in privileged_entries:
        owner_address = entry["address"]
        source = entry["source"]
        attempt_note = {"address": owner_address, "source": source, "impersonated": False}

        try:
            owner_account = accounts.at(owner_address, force=True)
            attempt_note["impersonated"] = True
            impersonated_any = True
        except Exception as exc:
            attempt_note["error"] = str(exc)
            result["details"]["impersonation_errors"].append(attempt_note)
            continue

        # Ensure owner has ETH for gas.
        min_balance = int(1 * 1e18)
        if owner_account.balance() < min_balance:
            try:
                accounts[0].transfer(owner_account, min_balance,gas_limit=DEFAULT_GAS_SETTINGS['gas_limit'])
            except Exception as exc:
                attempt_note["funding_error"] = str(exc)
                result["details"]["impersonation_errors"].append(attempt_note)
                continue

        owner_initial = detector.token.balanceOf(owner_address)
        result["details"]["owner_balance_before"][owner_address] = owner_initial
        owner_target_amount = owner_initial + max(1, 10 ** decimals)

        for func_name in candidate_functions:
            func = getattr(detector.token, func_name)

            # First try against victim.
            success, response, used_args = _attempt_manipulation(
                func,
                victim.address,
                victim_target_amount,
                owner_account,
                owner_address,
            )

            if success:
                victim_after = detector.token.balanceOf(victim.address)
                result["details"]["victim_balance_after"] = victim_after
                if victim_after < victim_initial:
                    result["details"]["others_balance_reduced"] = True
                    result["details"]["successful_calls"].append(
                        {
                            "function": func_name,
                            "target": "victim",
                            "args": used_args,
                            "before": victim_initial,
                            "after": victim_after,
                        }
                    )
                else:
                    result["details"]["failed_calls"].append(
                        {
                            "function": func_name,
                            "target": "victim",
                            "error": "no-effect",
                            "args": used_args,
                        }
                    )
            else:
                result["details"]["failed_calls"].append(
                    {"function": func_name, "target": "victim", "error": str(response)}
                )
            if success:
                chain.revert()

            # Then try to inflate owner balance.
            success_owner, response_owner, used_owner_args = _attempt_manipulation(
                func,
                owner_address,
                owner_target_amount,
                owner_account,
                owner_address,
            )

            if success_owner:
                owner_after = detector.token.balanceOf(owner_address)
                result["details"]["owner_balance_after"][owner_address] = owner_after
                if owner_after > owner_initial:
                    result["details"]["self_balance_increased"] = True
                    result["details"]["successful_calls"].append(
                        {
                            "function": func_name,
                            "target": "owner",
                            "args": used_owner_args,
                            "before": owner_initial,
                            "after": owner_after,
                        }
                    )
                else:
                    result["details"]["failed_calls"].append(
                        {
                            "function": func_name,
                            "target": "owner",
                            "error": "no-effect",
                            "args": used_owner_args,
                        }
                    )
            else:
                result["details"]["failed_calls"].append(
                    {"function": func_name, "target": "owner", "error": str(response_owner)}
                )
            if success_owner and isinstance(response_owner, dict):
                chain.revert()

    if not impersonated_any:
        result["result"] = "UNKNOWN"
        result["confidence"] = "LOW"
        result["reason"] = "Unable to impersonate any privileged address candidates"
        return result

    # Re-fetch balances if they were never updated successfully.
    if result["details"]["victim_balance_after"] is None:
        result["details"]["victim_balance_after"] = detector.token.balanceOf(victim.address)
    if not result["details"]["owner_balance_after"]:
        for entry in privileged_entries:
            addr = entry["address"]
            try:
                result["details"]["owner_balance_after"][addr] = detector.token.balanceOf(addr)
            except Exception:
                continue

    reduced = result["details"]["others_balance_reduced"]
    inflated = result["details"]["self_balance_increased"]

    if reduced and inflated:
        result["result"] = "YES"
        result["confidence"] = "HIGH"
        result["reason"] = "Owner can both reduce others' balances and increase their own holdings"
    elif reduced:
        result["result"] = "YES"
        result["confidence"] = "HIGH"
        result["reason"] = "Owner can arbitrarily reduce other addresses' balances"
    elif inflated:
        result["result"] = "YES"
        result["confidence"] = "MEDIUM"
        result["reason"] = "Owner can inflate their own balance via privileged function"
    else:
        result["result"] = "NO"
        result["confidence"] = "MEDIUM"
        result["reason"] = "Candidate functions found but no balance impact observed"

    # Print detailed results
    _print_detailed_results(result, decimals)

    chain.revert()

    return result
