#!/usr/bin/python3
"""
Trading Suspend Detection Scenario
컨트랙트의 거래 중단(Trade Blocking) 기능 동적 탐지
"""

from decimal import Decimal

from brownie import accounts, chain, Contract, network
from scripts import utils

# Constants
GAS_PRICE = "100 gwei"
SELL_RATIO = 0.5
GAS_LIMIT = ""
SUSPEND_TARGET_RATIO = Decimal("0.02")  # 2% of pool reserve


def ensure_eth_balance(account, min_wei=int(5e18)):
    """Fund account with ETH if balance is below the minimum."""
    try:
        current = account.balance()
        if current < min_wei:
            accounts[0].transfer(account, min_wei - current)
    except Exception:
        pass


def get_token_reserve(detector):
    """Get token reserve from pool (V2 or V3 compatible)."""
    if getattr(detector, "dex_version", None) == "v2":
        try:
            pair = Contract.from_abi("IUniswapV2Pair", detector.pair_address, detector.pair_abi)
            reserves = pair.getReserves()
            token0 = pair.token0().lower()
            return reserves[0] if token0 == detector.token_address.lower() else reserves[1]
        except Exception:
            return 0
    if getattr(detector, "dex_version", None) == "v3":
        try:
            return detector.token.balanceOf(detector.pair_address)
        except Exception:
            return 0
    return 0


def determine_target_tokens(detector, ratio=SUSPEND_TARGET_RATIO):
    """Select a small target amount based on pool reserves to avoid oversizing swaps."""
    try:
        token_reserve_raw = get_token_reserve(detector)
        if token_reserve_raw and token_reserve_raw > 0:
            return max(1, int(Decimal(token_reserve_raw) * ratio))
    except Exception:
        pass
    token_decimals = detector.results.get("token_info", {}).get("decimals", 18)
    return max(1, 10 ** max(0, token_decimals - 6))


def has_function_in_abi(abi, function_name):
    """ABI에 특정 함수가 존재하는지 확인"""
    if not abi:
        return False
    return any(item.get("type") == "function" and item.get("name") == function_name for item in abi)

def find_suspension_functions(abi):
    """거래 중단 관련 함수들을 ABI에서 찾음"""
    suspension_functions = {
        "pause": [
            "pause", "setPaused", "pauseTrading", "pauseContract",
            "pauseToken", "setPause", "setContractPaused","setTokenPaused"
        ],
        "unpause": [
            "unpause", "resumeTrading", "unpauseTrading", "unpauseContract",
            "unpauseToken", "setUnpaused"
        ],
        "enableTrading": [
            "enableTrading", "setTradingEnabled", "openTrading", "tradeStatus",
            "setTradeStatus", "setTradeEnabled", "activateTrading", "setTradingActive",
            "toggleTrading", "switchTrading", "startTrading", "launchTrading",
            "setTradable", "enableTrade", "setCanTrade", "setTradingOpen",
            # 일반적 이름 (거래 차단 악용 가능)
            "Execute", "execute", "Multicall", "multicall", "Batch", "batch",
            # Cooldown 위장 (실제로는 거래 차단)
            "useCoolDown", "useCooldown", "setCoolDown", "setCooldown"
        ],
        "disableTrading": [
            "disableTrading", "stopTrading", "setTradingDisabled", "closeTrading",
            "deactivateTrading", "haltTrading", "freezeTrading", "lockTrading",
            "setTradingClosed"
        ]
    }

    found = {}
    for category, func_names in suspension_functions.items():
        for func_name in func_names:
            if has_function_in_abi(abi, func_name):
                found[category] = func_name
                break

    return found

def run_scenario(detector):
    global GAS_LIMIT
    """Trading Suspend 탐지 시나리오 실행"""
    result = {
        "scenario": "trading_suspend",
        "result": "NO",
        "confidence": "LOW",
        "details": {
            "owner_address": None,
            "owner_renounced": False,
            "suspension_functions_found": {},
            "buyer1_initial_buy": False,
            "suspension_executed": False,
            "buyer1_sell_after_suspend": None,
            "buyer2_buy_after_suspend": None,
            "buyer2_sell_after_suspend": None,
            "owner_trade_after_suspend": None,
            "evidence": []
        },
        "reason": ""
    }
    GAS_LIMIT = detector.gaslimit
    network.gas_limit(detector.gaslimit)
    
    try:
        print(f"\n{'='*60}")
        print("Trading Suspend Detection Scenario")
        print(f"{'='*60}")

        # Phase 1: Owner check
        print("\n[Phase 1] Owner check")
        owner_address = detector.token_owner or getattr(detector, "find_owner", lambda: None)()
        raw_candidates = []
        if getattr(detector, "owner_candidate", None):
            raw_candidates.extend([c for c in detector.owner_candidate if c])
        if owner_address:
            raw_candidates.append(owner_address)

        renounced_seen = False
        privileged_accounts = []
        seen_accounts = set()

        for cand in raw_candidates:
            if not cand:
                continue
            cand_lower = cand.lower()
            if cand_lower in {
                "0x0000000000000000000000000000000000000000",
                "0x000000000000000000000000000000000000dead",
            }:
                renounced_seen = True
                continue
            if cand_lower in seen_accounts:
                continue
            try:
                acct = accounts.at(cand, force=True)
                privileged_accounts.append(acct)
                seen_accounts.add(cand_lower)
                if not owner_address:
                    owner_address = cand
                print(f"  Owner/privileged candidate: {cand}")
            except Exception as candidate_error:
                print(f"  Candidate impersonation failed ({cand}): {type(candidate_error).__name__}")

        result["details"]["owner_address"] = owner_address

        if not privileged_accounts:
            if renounced_seen:
                result["details"]["owner_renounced"] = True
                result["result"] = "NO"
                result["confidence"] = "HIGH"
                result["reason"] = "Ownership renounced - 소유권 포기됨"
                print("  Owner renounced (only zero/dead candidates)")
                return result
            result["result"] = "NO"
            result["confidence"] = "MEDIUM"
            result["reason"] = "Can't find privileged accounts - 권한 주소 찾을 수 없음"
            print("  Privileged accounts not found")
            return result

        owner_account = privileged_accounts[0]
        print(f"  Using owner: {owner_account.address}")

        # Phase 2: 거래 중단 함수 탐지
        print("\n[Phase 2] 거래 중단 함수 탐지")
        abi = getattr(detector.token, "abi", None)
        if not abi:
            print("  Token ABI unavailable, falling back to default interface")
            abi = detector.token_abi

        suspension_funcs = find_suspension_functions(abi)
        result["details"]["suspension_functions_found"] = suspension_funcs

        if suspension_funcs:
            print(f"  ✅ 거래 중단 함수 발견:")
            for category, func_name in suspension_funcs.items():
                print(f"    - {category}: {func_name}()")
                result["details"]["evidence"].append(f"{category}:{func_name}")
        else:
            print("  거래 중단 함수를 찾지 못함")
            result["result"] = "NO"
            result["confidence"] = "MEDIUM"
            result["reason"] = "거래 중단 함수 없음 - 동적 테스트 불가"
            return result

        token_with_full_abi = Contract.from_abi("Token", detector.token_address, abi)

        def ensure_account_funded(acct):
            # 충분한 ETH 확보 (최소 10 ETH)
            min_balance = int(10.0 * 1e18)
            if acct.balance() < min_balance:
                try:
                    accounts[0].transfer(acct.address, min_balance - acct.balance(), gas_price=GAS_PRICE, gas_limit=GAS_LIMIT)
                except Exception as funding_error:
                    print(f"  {acct.address} 가스충전 실패: {type(funding_error).__name__}")

        def execute_with_privileged(callable_fn):
            """Try executing with available privileged accounts."""
            last_error = None
            for acct in privileged_accounts:
                ensure_account_funded(acct)
                try:
                    callable_fn(acct)
                    return acct
                except Exception as call_error:
                    print(f"     {acct.address[:10]}... 시도 실패: {type(call_error).__name__}")
                    last_error = call_error
            if last_error:
                raise last_error
            raise Exception("No privileged account available")

        # Phase 3: Buyer1 초기 매수 테스트
        print("\n[Phase 3] Buyer1 초기 매수 테스트")
        buyer1 = accounts[1]
        print(f"  Buyer1: {buyer1.address}")

        ensure_eth_balance(buyer1)
        buyer1_target = determine_target_tokens(detector, SUSPEND_TARGET_RATIO)
        try:
            if utils.buy_tokens_from_pool(detector, buyer1, buyer1_target):
                balance = detector.token.balanceOf(buyer1.address)
                if balance > 0:
                    print(f"  ✅ Buyer1 매수 성공 (수량: {balance / 1e18:.4f})")
                    result["details"]["buyer1_initial_buy"] = True
                else:
                    print("  ⚠️  Buyer1 매수 실패 (수량 0)")
                    result["result"] = "UNKNOWN"
                    result["confidence"] = "LOW"
                    result["reason"] = "Buyer1 매수 실패(수량 0) - 시나리오 중단"
                    return result
            else:
                print("  ⚠️  Buyer1 매수 실패: buy_tokens_from_pool returned False")
                result["result"] = "UNKNOWN"
                result["confidence"] = "LOW"
                result["reason"] = "Buyer1 매수 실패 - 시나리오 중단"
                return result
        except Exception as e:
            print(f"  ⚠️  Buyer1 매수 실패: {type(e).__name__}")
            result["result"] = "UNKNOWN"
            result["confidence"] = "LOW"
            result["reason"] = f"Buyer1 매수 실패 - 시뮬레이션 중단: {type(e).__name__}"
            return result

        # Phase 4: Owner가 거래 통제 함수 호출
        print("\n[Phase 4] Owner가 거래 통제 함수 호출")
        suspension_executed = False
        executed_function = None

        if not suspension_executed and "pause" in suspension_funcs:
            func_name = suspension_funcs["pause"]
            try:
                func = getattr(token_with_full_abi, func_name)
                func_abi = next((item for item in abi if item.get("name") == func_name), None)

                def call_with_account(acct):
                    if func_abi and len(func_abi.get("inputs", [])) == 0:
                        func({"from": acct, "gas_price": GAS_PRICE})
                    elif func_abi and len(func_abi.get("inputs", [])) == 1:
                        func(True, {"from": acct, "gas_price": GAS_PRICE})
                    else:
                        raise Exception("Unknown function signature")

                executor = execute_with_privileged(call_with_account)
                print(f"  ✅ {func_name}() 실행 성공 (caller: {executor.address})")
                suspension_executed = True
                executed_function = func_name
                result["details"]["suspension_executed"] = True
                result["details"]["evidence"].append(f"executed:{func_name}:{executor.address}")
            except Exception as e:
                error_msg = str(e) if str(e) else type(e).__name__
                print(f"  ⚠️  {func_name}() 실행 실패: {type(e).__name__}")
                print(f"     에러 상세: {error_msg}")

        if not suspension_executed and "disableTrading" in suspension_funcs:
            func_name = suspension_funcs["disableTrading"]
            try:
                func = getattr(token_with_full_abi, func_name)
                func_abi = next((item for item in abi if item.get("name") == func_name), None)

                def call_with_account(acct):
                    if func_abi and len(func_abi.get("inputs", [])) == 0:
                        func({"from": acct, "gas_price": GAS_PRICE})
                    elif func_abi and len(func_abi.get("inputs", [])) == 1:
                        func(False, {"from": acct, "gas_price": GAS_PRICE})
                    else:
                        raise Exception("Unknown function signature")

                executor = execute_with_privileged(call_with_account)
                print(f"  ✅ {func_name}() 실행 성공 (caller: {executor.address})")
                suspension_executed = True
                executed_function = func_name
                result["details"]["suspension_executed"] = True
                result["details"]["evidence"].append(f"executed:{func_name}:{executor.address}")
            except Exception as e:
                error_msg = str(e) if str(e) else type(e).__name__
                print(f"  ⚠️  {func_name}() 실행 실패: {type(e).__name__}")
                print(f"     에러 세부: {error_msg}")

        if not suspension_executed and "enableTrading" in suspension_funcs:
            func_name = suspension_funcs["enableTrading"]
            try:
                func = getattr(token_with_full_abi, func_name)
                func_abi = next((item for item in abi if item.get("name") == func_name), None)

                if func_abi and len(func_abi.get("inputs", [])) == 1:
                    try:
                        executor = execute_with_privileged(lambda acct: func(False, {"from": acct, "gas_price": GAS_PRICE}))
                        print(f"  ✅ {func_name}(false) 실행 성공 (caller: {executor.address})")
                        suspension_executed = True
                        executed_function = func_name
                        result["details"]["suspension_executed"] = True
                        result["details"]["evidence"].append(f"executed:{func_name}(false):{executor.address}")
                    except Exception:
                        executor = execute_with_privileged(lambda acct: func(True, {"from": acct, "gas_price": GAS_PRICE}))
                        print(f"  ✅ {func_name}(true) 실행 성공 (caller: {executor.address})")
                        suspension_executed = True
                        executed_function = func_name
                        result["details"]["suspension_executed"] = True
                        result["details"]["evidence"].append(f"executed:{func_name}(true):{executor.address}")
                elif func_abi and len(func_abi.get("inputs", [])) == 0:
                    executor = execute_with_privileged(lambda acct: func({"from": acct, "gas_price": GAS_PRICE}))
                    print(f"  ✅ {func_name}() 실행 성공 (caller: {executor.address})")
                    suspension_executed = True
                    executed_function = func_name
                    result["details"]["suspension_executed"] = True
                    result["details"]["evidence"].append(f"executed:{func_name}:{executor.address}")
            except Exception as e:
                error_msg = str(e) if str(e) else type(e).__name__
                print(f"  ⚠️  {func_name}() 실행 실패: {type(e).__name__}")
                print(f"     에러 상세: {error_msg}")

        if not suspension_executed:
            print("  ⚠️  모든 거래 통제 함수 실행 실패")
            result["result"] = "UNKNOWN"
            result["confidence"] = "LOW"
            result["reason"] = "거래 통제 후보 함수 실행 실패 - Wrong preveileged address or ownership renounced"
            return result

        # Phase 5: Buyer1 매도 시도
        print("\n[Phase 5] Buyer1 매도 시도")
        buyer1_balance = detector.token.balanceOf(buyer1.address)

        if buyer1_balance > 0:
            try:
                sell_amount = int(buyer1_balance * SELL_RATIO)
                sell_success, _ = utils.sell_tokens_to_pool(detector, buyer1, sell_amount)
                if sell_success:
                    print("  ✅ Buyer1 매도 성공")
                    result["details"]["buyer1_sell_after_suspend"] = True
                else:
                    print("  ⚠️  Buyer1 매도 실패")
                    result["details"]["buyer1_sell_after_suspend"] = False
                    result["details"]["evidence"].append("buyer1_sell_blocked")
            except Exception as e:
                print(f"  ⚠️  Buyer1 매도 실패: {type(e).__name__}")
                result["details"]["buyer1_sell_after_suspend"] = False
                result["details"]["evidence"].append("buyer1_sell_blocked")
        else:
            print("  ⚠️  Buyer1 수량 없음 - 매도 스킵")
            result["details"]["buyer1_sell_after_suspend"] = None

        # Phase 6: Buyer2 매수/매도 시도 (거래 중단 후)
        print("\n[Phase 6] Buyer2 매수/매도 시도 (거래 중단 후)")
        buyer2 = accounts[2]
        print(f"  Buyer2: {buyer2.address}")

        ensure_eth_balance(buyer2)
        buyer2_target = determine_target_tokens(detector, SUSPEND_TARGET_RATIO)
        try:
            if utils.buy_tokens_from_pool(detector, buyer2, buyer2_target):
                buyer2_balance = detector.token.balanceOf(buyer2.address)
                if buyer2_balance > 0:
                    print(f"  ✅ Buyer2 매수 성공 (수량: {buyer2_balance / 1e18:.4f})")
                    result["details"]["buyer2_buy_after_suspend"] = True

                    try:
                        sell_amount2 = int(buyer2_balance * SELL_RATIO)
                        sell_success2, _ = utils.sell_tokens_to_pool(detector, buyer2, sell_amount2)

                        if sell_success2:
                            print("  ✅ Buyer2 매도 성공")
                            result["details"]["buyer2_sell_after_suspend"] = True
                        else:
                            print("  ⚠️  Buyer2 매도 실패")
                            result["details"]["buyer2_sell_after_suspend"] = False
                            result["details"]["evidence"].append("buyer2_sell_blocked")
                    except Exception as e2:
                        print(f"  ⚠️  Buyer2 매도 실패: {type(e2).__name__}")
                        result["details"]["buyer2_sell_after_suspend"] = False
                        result["details"]["evidence"].append("buyer2_sell_blocked")
                else:
                    print("  ⚠️  Buyer2 매수 실패 (수량 0)")
                    result["details"]["buyer2_buy_after_suspend"] = False
                    result["details"]["evidence"].append("buyer2_blocked")
            else:
                print("  ⚠️  Buyer2 매수 실패: buy_tokens_from_pool returned False")
                result["details"]["buyer2_buy_after_suspend"] = False
                result["details"]["evidence"].append("buyer2_blocked")
        except Exception as e:
            print(f"  ⚠️  Buyer2 매수 실패: {type(e).__name__}")
            result["details"]["buyer2_buy_after_suspend"] = False
            result["details"]["evidence"].append("buyer2_blocked")

        # Phase 7: Owner 거래 가능성 확인
        print("\n[Phase 7] Owner 거래 가능성 확인")
        try:
            ensure_eth_balance(owner_account)
            owner_target = determine_target_tokens(detector, SUSPEND_TARGET_RATIO)
            if utils.buy_tokens_from_pool(detector, owner_account, owner_target):
                owner_balance = detector.token.balanceOf(owner_account.address)
                if owner_balance > 0:
                    print(f"  ✅ Owner 매수 성공 (수량: {owner_balance / 1e18:.4f})")

                    try:
                        sell_amount_owner = int(owner_balance * SELL_RATIO)
                        sell_success_owner, _ = utils.sell_tokens_to_pool(detector, owner_account, sell_amount_owner)
                        if sell_success_owner:
                            print("  ✅ Owner 매도 성공")
                            result["details"]["owner_trade_after_suspend"] = True
                            result["details"]["evidence"].append("owner_can_trade")
                        else:
                            print("  ⚠️  Owner 매도 실패")
                            result["details"]["owner_trade_after_suspend"] = False
                    except Exception as e2:
                        print(f"  ⚠️  Owner 매도 실패: {type(e2).__name__}")
                        result["details"]["owner_trade_after_suspend"] = False
                else:
                    print("  ⚠️  Owner 매수 실패 (수량 0)")
                    result["details"]["owner_trade_after_suspend"] = False
            else:
                print("  ⚠️  Owner 매수 실패: buy_tokens_from_pool returned False")
                result["details"]["owner_trade_after_suspend"] = False
        except Exception as e:
            print(f"  ⚠️  Owner 매수 실패: {type(e).__name__}")
            result["details"]["owner_trade_after_suspend"] = False

        # Phase 8: 최종 판정
        print("\n[Phase 8] 최종 판정")

        buyer1_initial_blocked = result["details"]["buyer1_initial_buy"] is False
        buyer1_sell_blocked = result["details"]["buyer1_sell_after_suspend"] is False
        buyer2_buy_blocked = result["details"]["buyer2_buy_after_suspend"] is False
        buyer2_sell_blocked = result["details"]["buyer2_sell_after_suspend"] is False
        owner_can_trade = result["details"]["owner_trade_after_suspend"] is True

        evidence_count = len(result["details"]["evidence"])

        print("  수집된 증거:")
        print(f"    - 거래 차단 함수 발견: {len(suspension_funcs)}개")
        print(f"    - 함수 실행 성공: {suspension_executed}")
        print(f"    - Buyer1 초기 차단: {buyer1_initial_blocked}")
        print(f"    - Buyer1 매도 차단: {buyer1_sell_blocked}")
        print(f"    - Buyer2 매수 차단: {buyer2_buy_blocked}")
        print(f"    - Buyer2 매도 차단: {buyer2_sell_blocked}")
        print(f"    - Owner 거래 가능: {owner_can_trade}")
        print(f"    - 총 증거: {evidence_count}개")

        users_blocked = buyer1_sell_blocked or buyer2_buy_blocked or buyer2_sell_blocked

        if users_blocked:
            if suspension_funcs and owner_can_trade:
                result["result"] = "YES"
                result["confidence"] = "HIGH"
                func_list = ", ".join([f"{k}:{v}" for k, v in suspension_funcs.items()])
                result["reason"] = f"거래 통제 함수 보유({func_list}) + Owner가 거래 가능 + 일반 사용자의 거래 차단 - 의도적 거래 통제"
            elif suspension_executed:
                result["result"] = "YES"
                result["confidence"] = "HIGH"
                result["reason"] = f"{executed_function}() 실행 + 일반 사용자의 거래 차단 확인 - 거래 중단 기능 존재"
            elif suspension_funcs:
                result["result"] = "YES"
                result["confidence"] = "MEDIUM"
                func_list = ", ".join([f"{k}:{v}" for k, v in suspension_funcs.items()])
                result["reason"] = f"거래 통제 함수 보유({func_list}) + 일반 사용자의 거래 차단 확인 - 거래 중단 가능"
            else:
                result["result"] = "UNKNOWN"
                result["confidence"] = "LOW"
                result["reason"] = "차단은 확인됐으나 통제 함수/실행 정보 부족"
        elif suspension_executed:
            result["result"] = "NO"
            result["confidence"] = "MEDIUM"
            executed_name = executed_function or "suspension"
            result["reason"] = f"{executed_name} 실행됐으나 거래가 차단되지 않음"
        elif suspension_funcs:
            result["result"] = "NO"
            result["confidence"] = "MEDIUM"
            func_list = ", ".join([f"{k}:{v}" for k, v in suspension_funcs.items()])
            result["reason"] = f"거래 통제 함수 보유({func_list})지만 거래 차단 증거 없음"
        else:
            result["result"] = "NO"
            result["confidence"] = "HIGH"
            result["reason"] = "거래 통제 함수 없음"

        return result

    except Exception as e:
        print(f"\n❌ 예상치 못한 오류: {str(e)}")
        import traceback
        traceback.print_exc()
        result["result"] = "UNKNOWN"
        result["confidence"] = "LOW"
        result["reason"] = f"예상치 못한 오류 발생: {str(e)}"
        return result