#!/usr/bin/python3
"""
Blacklist/Whitelist Detection Scenario
Owner 권한으로 blacklist/whitelist 함수를 호출하여 실제로 거래 차단이 발생하는지 동적 검증
"""

from brownie import accounts, chain, Contract, network

# 공통 상수
GAS_PRICE = "5 gwei"
GAS_LIMIT = 500000
ETH_TRANSFER_AMOUNT = int(0.1 * 1e18)
BUY_AMOUNT = int(0.01 * 1e18)
SELL_RATIO = 0.9


def swap_quote_for_tokens(detector, quote_token, quote_addr, amount, recipient, account):
    """Helper function to swap quote tokens for target tokens (V2/V3 compatible)"""
    deadline = chain.time() + 300
    path = [quote_addr, detector.token_address]

    if detector.dex_version == "v3":
        quote_token.approve(detector.v3_router_addr, amount, {"from": account})

        params = (
            quote_addr,
            detector.token_address,
            detector.fee_tier,
            recipient,
            deadline,
            amount,
            0,
            0
        )

        detector.v3_router.exactInputSingle(
            params,
            {"from": account, "gas_price": GAS_PRICE, "gas_limit": GAS_LIMIT}
        )
    else:
        quote_token.approve(detector.router_addr, amount, {"from": account})
        detector.router.swapExactTokensForTokensSupportingFeeOnTransferTokens(
            amount, 0, path, recipient, deadline,
            {"from": account, "gas_price": GAS_PRICE, "gas_limit": GAS_LIMIT}
        )


def swap_tokens_for_quote(detector, token_amount, quote_addr, recipient, account):
    """Helper function to swap target tokens for quote tokens (V2/V3 compatible)"""
    deadline = chain.time() + 300
    is_weth = quote_addr.lower() == detector.weth.address.lower()

    if detector.dex_version == "v3":
        detector.token.approve(detector.v3_router_addr, token_amount, {"from": account, "gas_price": GAS_PRICE, "gas_limit": GAS_LIMIT})

        if is_weth:
            params = (
                detector.token_address,
                quote_addr,
                detector.fee_tier,
                detector.v3_router_addr,
                deadline,
                token_amount,
                0,
                0
            )

            detector.v3_router.exactInputSingle(
                params,
                {"from": account, "gas_price": GAS_PRICE, "gas_limit": GAS_LIMIT}
            )

            if hasattr(detector.v3_router, 'unwrapWETH9'):
                weth_balance = detector.weth.balanceOf(detector.v3_router_addr)
                if weth_balance > 0:
                    detector.v3_router.unwrapWETH9(weth_balance, recipient, {"from": account})
        else:
            params = (
                detector.token_address,
                quote_addr,
                detector.fee_tier,
                recipient,
                deadline,
                token_amount,
                0,
                0
            )

            detector.v3_router.exactInputSingle(
                params,
                {"from": account, "gas_price": GAS_PRICE, "gas_limit": GAS_LIMIT}
            )
    else:
        detector.token.approve(detector.router_addr, token_amount, {"from": account, "gas_price": GAS_PRICE, "gas_limit": GAS_LIMIT})

        if is_weth:
            path_reverse = [detector.token_address, detector.weth.address]
            detector.router.swapExactTokensForETHSupportingFeeOnTransferTokens(
                token_amount, 0, path_reverse, recipient, deadline,
                {"from": account, "gas_price": GAS_PRICE, "gas_limit": GAS_LIMIT}
            )
        else:
            path_reverse = [detector.token_address, quote_addr]
            detector.router.swapExactTokensForTokensSupportingFeeOnTransferTokens(
                token_amount, 0, path_reverse, recipient, deadline,
                {"from": account, "gas_price": GAS_PRICE, "gas_limit": GAS_LIMIT}
            )

def prepare_account_with_quote(detector, account, amount_wei):
    """계정에 Quote Token 준비 (WETH 또는 다른 Quote Token)"""
    quote_addr = detector.quote_token_address or detector.weth.address

    # WETH deposit (2배로 넉넉하게)
    weth_deposit_amount = amount_wei * 2

    # WETH가 아닌 경우 스왑에 필요한 추가 WETH
    swap_amount = int(amount_wei * 1.5) if quote_addr.lower() != detector.weth.address.lower() else 0

    # 필요한 총 ETH: WETH deposit + 가스비 여유 (넉넉하게)
    gas_buffer = int(1.0 * 1e18)  # 1.0 ETH 가스비 버퍼 (증가)
    total_eth_needed = weth_deposit_amount + gas_buffer

    # 현재 잔고 확인 후 무조건 충분하게 전송
    current_balance = account.balance()
    if current_balance < total_eth_needed:
        transfer_amount = total_eth_needed - current_balance + int(0.2 * 1e18)  # 여유 0.2 ETH 추가
        accounts[0].transfer(account, transfer_amount)
        network.web3.provider.make_request("anvil_mine", [1])  # transfer 후 mine

    # WETH deposit 전 잔고 재확인
    final_balance = account.balance()
    if final_balance < weth_deposit_amount:
        # 여전히 부족하면 더 전송
        accounts[0].transfer(account, weth_deposit_amount - final_balance + int(0.5 * 1e18))
        network.web3.provider.make_request("anvil_mine", [1])

    # WETH deposit
    detector.weth.deposit({"from": account, "value": weth_deposit_amount})
    network.web3.provider.make_request("anvil_mine", [1])

    # WETH인 경우 완료
    if quote_addr.lower() == detector.weth.address.lower():
        return detector.weth, quote_addr

    # WETH -> Quote Token swap
    deadline = chain.time() + 300

    if detector.dex_version == "v3":
        detector.weth.approve(detector.v3_router_addr, swap_amount, {"from": account})
        network.web3.provider.make_request("anvil_mine", [1])

        params = (
            detector.weth.address,
            detector.quote_token_address,
            3000,  # 0.3% fee tier for WETH-stablecoin pairs
            account.address,
            deadline,
            swap_amount,
            0,
            0
        )

        detector.v3_router.exactInputSingle(params, {"from": account})
    else:
        detector.weth.approve(detector.router_addr, swap_amount, {"from": account})
        network.web3.provider.make_request("anvil_mine", [1])
        detector.router.swapExactTokensForTokensSupportingFeeOnTransferTokens(
            swap_amount,
            0,
            [detector.weth.address, detector.quote_token_address],
            account.address,
            deadline,
            {"from": account}
        )

    network.web3.provider.make_request("anvil_mine", [1])
    return detector.quote_token, detector.quote_token_address

# 통합 접근 제어 함수 목록 (Blacklist + Whitelist, 정방향 + 역방향 탐지)
ACCESS_CONTROL_SETTERS = [
    # Blacklist - 배열 파라미터
    {"name": "addApprove", "args_type": "array"},
    {"name": "AddApprove", "args_type": "array"},
    {"name": "addSender", "args_type": "array"},
    {"name": "AddSender", "args_type": "array"},
    {"name": "addBot", "args_type": "array"},
    {"name": "AddBot", "args_type": "array"},
    {"name": "addBots", "args_type": "array"},
    {"name": "AddBots", "args_type": "array"},
    {"name": "setBots", "args_type": "array"},
    {"name": "SetBots", "args_type": "array"},
    {"name": "setBlacklist", "args_type": "array"},
    {"name": "SetBlacklist", "args_type": "array"},
    {"name": "addToBlacklist", "args_type": "array"},
    {"name": "AddToBlacklist", "args_type": "array"},
    {"name": "addBlacklisted", "args_type": "array"},
    {"name": "AddBlacklisted", "args_type": "array"},
    {"name": "blacklistAddresses", "args_type": "array"},
    {"name": "BlacklistAddresses", "args_type": "array"},
    {"name": "setBlacklistAddresses", "args_type": "array"},
    {"name": "SetBlacklistAddresses", "args_type": "array"},
    {"name": "addToBlackList", "args_type": "array"},
    {"name": "AddToBlackList", "args_type": "array"},
    {"name": "blacklistWallets", "args_type": "array"},
    {"name": "BlacklistWallets", "args_type": "array"},
    {"name": "addBlacklistAddresses", "args_type": "array"},
    {"name": "AddBlacklistAddresses", "args_type": "array"},
    {"name": "block", "args_type": "array"},
    {"name": "Block", "args_type": "array"},
    # Whitelist - 배열 파라미터
    {"name": "addReceiver", "args_type": "array"},
    {"name": "AddReceiver", "args_type": "array"},
    {"name": "addToWhitelist", "args_type": "array"},
    {"name": "AddToWhitelist", "args_type": "array"},
    {"name": "setWhitelist", "args_type": "array"},
    {"name": "SetWhitelist", "args_type": "array"},
    {"name": "addWhitelisted", "args_type": "array"},
    {"name": "AddWhitelisted", "args_type": "array"},
    {"name": "whitelistAddresses", "args_type": "array"},
    {"name": "WhitelistAddresses", "args_type": "array"},
    {"name": "setWhitelistAddresses", "args_type": "array"},
    {"name": "SetWhitelistAddresses", "args_type": "array"},
    {"name": "addToWhiteList", "args_type": "array"},
    {"name": "AddToWhiteList", "args_type": "array"},
    {"name": "whitelistWallets", "args_type": "array"},
    {"name": "WhitelistWallets", "args_type": "array"},
    {"name": "addWhitelistAddresses", "args_type": "array"},
    {"name": "AddWhitelistAddresses", "args_type": "array"},
    {"name": "setWhitelisted", "args_type": "array"},
    {"name": "SetWhitelisted", "args_type": "array"},
    # Blacklist - 단일 파라미터
    {"name": "setBot", "args_type": "single"},
    {"name": "SetBot", "args_type": "single"},
    {"name": "blacklist", "args_type": "single"},
    {"name": "Blacklist", "args_type": "single_bool"},
    {"name": "addBlacklist", "args_type": "single"},
    {"name": "AddBlacklist", "args_type": "single"},
    {"name": "setBlacklistAddress", "args_type": "single"},
    {"name": "SetBlacklistAddress", "args_type": "single"},
    {"name": "blacklistAddress", "args_type": "single"},
    {"name": "BlacklistAddress", "args_type": "single"},
    {"name": "addToBlacklistSingle", "args_type": "single"},
    {"name": "AddToBlacklistSingle", "args_type": "single"},
    {"name": "setBlacklisted", "args_type": "single"},
    {"name": "SetBlacklisted", "args_type": "single"},
    {"name": "blacklistWallet", "args_type": "single"},
    {"name": "BlacklistWallet", "args_type": "single"},
    {"name": "addBlacklistAddress", "args_type": "single"},
    {"name": "AddBlacklistAddress", "args_type": "single"},
    # Whitelist - 단일 파라미터
    {"name": "whitelist", "args_type": "single"},
    {"name": "Whitelist", "args_type": "single_bool"},
    {"name": "addWhitelist", "args_type": "single"},
    {"name": "AddWhitelist", "args_type": "single"},
    {"name": "setWhitelistAddress", "args_type": "single"},
    {"name": "SetWhitelistAddress", "args_type": "single"},
    {"name": "whitelistAddress", "args_type": "single"},
    {"name": "WhitelistAddress", "args_type": "single"},
    {"name": "addToWhitelistSingle", "args_type": "single"},
    {"name": "AddToWhitelistSingle", "args_type": "single"},
    {"name": "setWhitelistedAddress", "args_type": "single"},
    {"name": "SetWhitelistedAddress", "args_type": "single"},
    {"name": "whitelistWallet", "args_type": "single"},
    {"name": "WhitelistWallet", "args_type": "single"},
    {"name": "addWhitelistAddress", "args_type": "single"},
    {"name": "AddWhitelistAddress", "args_type": "single"}
]

def get_abi_from_etherscan(token_address):
    """Deprecated stub kept for backward compatibility."""
    return None

def has_function_in_abi(abi, func_name):
    """ABI에 특정 함수가 존재하는지 확인"""
    if not abi:
        return False
    return any(item.get('type') == 'function' and item.get('name') == func_name for item in abi)

def ensure_account_funded(account):
    """테스트 계정 잔액 확보"""
    if account.balance() < ETH_TRANSFER_AMOUNT:
        try:
            accounts[0].transfer(account.address, ETH_TRANSFER_AMOUNT)
        except Exception as funding_error:
            print(f"  ⚠️ {account.address} 가스 충전 실패: {type(funding_error).__name__}")

def call_access_control_function(contract, func_name, args_type, target_address, candidate_accounts):
    """접근 제어 함수 호출 (Blacklist/Whitelist)"""
    last_error = None
    for account in candidate_accounts:
        ensure_account_funded(account)
        tx_params = {"from": account, "gas_price": GAS_PRICE, "gas_limit": GAS_LIMIT}

        try:
            if args_type == "single":
                getattr(contract, func_name)(target_address, tx_params)
            elif args_type == "single_bool":
                getattr(contract, func_name)(target_address, True, tx_params)
            else:  # array
                getattr(contract, func_name)([target_address], tx_params)
            return account
        except Exception as call_error:
            last_error = call_error

    if last_error:
        raise last_error
    raise Exception("No privileged account succeeded")

def test_blacklist(detector, owner_address, abi, privileged_accounts):
    """Blacklist 동적 테스트"""
    result = {
        "type": "blacklist",
        "result": "UNKNOWN",
        "confidence": "LOW",
        "details": {
            "function_used": None,
            "buyer1_buy_success": False,
            "buyer1_sell_after_blacklist": None,
            "buyer2_buy_success": False,
            "buyer2_sell_success": False,
            "blacklist_confirmed": False,
            "executed_by": None
        },
        "reason": ""
    }

    # 함수 탐지
    found_setter = None
    for func_info in ACCESS_CONTROL_SETTERS:
        if has_function_in_abi(abi, func_info["name"]):
            found_setter = func_info
            result["details"]["function_used"] = func_info["name"]
            print(f"  발견: {func_info['name']}()")
            break

    if not found_setter:
        result["result"] = "NO"
        result["confidence"] = "HIGH"
        result["reason"] = "Blacklist 설정 함수 발견되지 않음"
        return result

    token_contract = Contract.from_abi("Token", detector.token_address, abi)
    buyer1 = accounts[1]
    buyer2 = accounts[2]
    deadline = chain.time() + 300

    # Quote Token 준비
    quote_token, quote_addr = prepare_account_with_quote(detector, buyer1, ETH_TRANSFER_AMOUNT)
    quote_balance_buyer1 = quote_token.balanceOf(buyer1.address)

    path = [quote_addr, detector.token_address]
    path_reverse = [detector.token_address, quote_addr]

    # Buyer1 buy
    try:
        swap_quote_for_tokens(detector, quote_token, quote_addr, BUY_AMOUNT, buyer1.address, buyer1)
        network.web3.provider.make_request("anvil_mine", [1])
        buyer1_tokens = detector.token.balanceOf(buyer1.address)
        result["details"]["buyer1_buy_success"] = True
        print(f"  Buyer1 매수 성공: {buyer1_tokens / 1e18:.4f} 토큰")
    except:
        result["result"] = "UNKNOWN"
        result["reason"] = "Buyer1 매수 실패"
        return result

    # Blacklist 함수 호출
    try:
        caller_account = call_access_control_function(
            token_contract, found_setter["name"], found_setter["args_type"],
            buyer1.address, privileged_accounts
        )
        result["details"]["executed_by"] = caller_account.address
        print(f"  {found_setter['name']}() 호출 성공 (caller: {caller_account.address})")
    except:
        result["result"] = "NO"
        result["confidence"] = "MEDIUM"
        result["reason"] = f"{found_setter['name']}() 호출 실패"
        return result

    # Buyer1 sell attempt (check if blocked)
    try:
        swap_tokens_for_quote(detector, int(buyer1_tokens * SELL_RATIO), quote_addr, buyer1.address, buyer1)
        result["details"]["buyer1_sell_after_blacklist"] = "SUCCESS"
        result["result"] = "NO"
        result["confidence"] = "MEDIUM"
        result["reason"] = f"{found_setter['name']}() 함수 존재하나 실제 차단 안 함"
        return result
    except:
        result["details"]["buyer1_sell_after_blacklist"] = "FAILED"
        result["details"]["blacklist_confirmed"] = True
        print(f"  Buyer1 매도 차단 확인")

    # Buyer2 매수/매도 시도 (선택적 차단 확인)
    quote_token2, _ = prepare_account_with_quote(detector, buyer2, ETH_TRANSFER_AMOUNT)
    quote_balance_buyer2 = quote_token2.balanceOf(buyer2.address)

    try:
        swap_quote_for_tokens(detector, quote_token2, quote_addr, BUY_AMOUNT, buyer2.address, buyer2)
        network.web3.provider.make_request("anvil_mine", [1])
        buyer2_tokens = detector.token.balanceOf(buyer2.address)
        result["details"]["buyer2_buy_success"] = True

        swap_tokens_for_quote(detector, int(buyer2_tokens * SELL_RATIO), quote_addr, buyer2.address, buyer2)
        network.web3.provider.make_request("anvil_mine", [1])
        result["details"]["buyer2_sell_success"] = True
        print(f"  Buyer2 매도 성공")
    except:
        pass

    # 최종 판정
    if result["details"]["blacklist_confirmed"]:
        if result["details"]["buyer2_sell_success"]:
            result["result"] = "YES"
            result["confidence"] = "HIGH"
            result["reason"] = f"{found_setter['name']}()로 Buyer1 차단, Buyer2 정상 - 선택적 차단 가능"
        else:
            result["result"] = "YES"
            result["confidence"] = "MEDIUM"
            result["reason"] = f"{found_setter['name']}()로 Buyer1 차단 확인"
    else:
        result["result"] = "NO"
        result["confidence"] = "LOW"
        result["reason"] = "차단 효과 확인 안 됨"

    return result

def test_whitelist(detector, owner_address, owner_account, abi, privileged_accounts):
    """Whitelist 동적 테스트"""
    result = {
        "type": "whitelist",
        "result": "UNKNOWN",
        "confidence": "LOW",
        "details": {
            "function_used": None,
            "owner_buy_success": False,
            "owner_sell_success": False,
            "buyer1_buy_success": False,
            "buyer1_sell_success": False,
            "buyer2_buy_success": False,
            "buyer2_sell_success": False,
            "whitelist_confirmed": False,
            "executed_by": None
        },
        "reason": ""
    }

    # 함수 탐지
    found_setter = None
    for func_info in ACCESS_CONTROL_SETTERS:
        if has_function_in_abi(abi, func_info["name"]):
            found_setter = func_info
            result["details"]["function_used"] = func_info["name"]
            print(f"  발견: {func_info['name']}()")
            break

    if not found_setter:
        result["result"] = "NO"
        result["confidence"] = "HIGH"
        result["reason"] = "Whitelist 설정 함수 발견되지 않음"
        return result

    token_contract = Contract.from_abi("Token", detector.token_address, abi)
    buyer1 = accounts[3]
    buyer2 = accounts[4]
    deadline = chain.time() + 300

    # Quote Token 준비
    quote_token_owner, quote_addr = prepare_account_with_quote(detector, owner_account, ETH_TRANSFER_AMOUNT)
    quote_balance_owner = quote_token_owner.balanceOf(owner_account.address)

    path = [quote_addr, detector.token_address]
    path_reverse = [detector.token_address, quote_addr]

    # Owner 매수/매도
    try:
        quote_token_owner.approve(detector.router_addr, quote_balance_owner, {"from": owner_account})
        detector.router.swapExactTokensForTokensSupportingFeeOnTransferTokens(
            BUY_AMOUNT, 0, path, owner_account.address, deadline,
            {"from": owner_account, "gas_price": GAS_PRICE, "gas_limit": GAS_LIMIT}
        )
        network.web3.provider.make_request("anvil_mine", [1])
        owner_tokens = detector.token.balanceOf(owner_account.address)
        result["details"]["owner_buy_success"] = True
        print(f"  Owner 매수 성공: {owner_tokens / 1e18:.4f} 토큰")

        detector.token.approve(detector.router_addr, owner_tokens, {"from": owner_account, "gas_price": GAS_PRICE, "gas_limit": GAS_LIMIT})
        detector.router.swapExactTokensForTokensSupportingFeeOnTransferTokens(
            int(owner_tokens * SELL_RATIO), 0, path_reverse, owner_account.address, deadline,
            {"from": owner_account, "gas_price": GAS_PRICE, "gas_limit": GAS_LIMIT}
        )
        network.web3.provider.make_request("anvil_mine", [1])
        result["details"]["owner_sell_success"] = True
        print(f"  Owner 매도 성공")
    except:
        pass

    # Buyer1 매수
    quote_token_buyer1, _ = prepare_account_with_quote(detector, buyer1, ETH_TRANSFER_AMOUNT)
    quote_balance_buyer1 = quote_token_buyer1.balanceOf(buyer1.address)

    try:
        quote_token_buyer1.approve(detector.router_addr, quote_balance_buyer1, {"from": buyer1})
        detector.router.swapExactTokensForTokensSupportingFeeOnTransferTokens(
            BUY_AMOUNT, 0, path, buyer1.address, deadline,
            {"from": buyer1, "gas_price": GAS_PRICE, "gas_limit": GAS_LIMIT}
        )
        network.web3.provider.make_request("anvil_mine", [1])
        buyer1_tokens = detector.token.balanceOf(buyer1.address)
        result["details"]["buyer1_buy_success"] = True
        print(f"  Buyer1 매수 성공: {buyer1_tokens / 1e18:.4f} 토큰")
    except:
        result["result"] = "UNKNOWN"
        result["reason"] = "Buyer1 매수 실패"
        return result

    # Whitelist 함수 호출
    try:
        caller_account = call_access_control_function(
            token_contract, found_setter["name"], found_setter["args_type"],
            buyer1.address, privileged_accounts
        )
        result["details"]["executed_by"] = caller_account.address
        print(f"  {found_setter['name']}() 호출 성공 (caller: {caller_account.address})")
    except:
        result["result"] = "NO"
        result["confidence"] = "MEDIUM"
        result["reason"] = f"{found_setter['name']}() 호출 실패"
        return result

    # Buyer1 매도 시도
    try:
        detector.token.approve(detector.router_addr, buyer1_tokens, {"from": buyer1, "gas_price": GAS_PRICE, "gas_limit": GAS_LIMIT})
        detector.router.swapExactTokensForTokensSupportingFeeOnTransferTokens(
            int(buyer1_tokens * SELL_RATIO), 0, path_reverse, buyer1.address, deadline,
            {"from": buyer1, "gas_price": GAS_PRICE, "gas_limit": GAS_LIMIT}
        )
        network.web3.provider.make_request("anvil_mine", [1])
        result["details"]["buyer1_sell_success"] = True
        print(f"  Buyer1 매도 성공")
    except:
        pass

    # Buyer2 매수/매도 시도
    quote_token_buyer2, _ = prepare_account_with_quote(detector, buyer2, ETH_TRANSFER_AMOUNT)
    quote_balance_buyer2 = quote_token_buyer2.balanceOf(buyer2.address)

    try:
        quote_token_buyer2.approve(detector.router_addr, quote_balance_buyer2, {"from": buyer2})
        detector.router.swapExactTokensForTokensSupportingFeeOnTransferTokens(
            BUY_AMOUNT, 0, path, buyer2.address, deadline,
            {"from": buyer2, "gas_price": GAS_PRICE, "gas_limit": GAS_LIMIT}
        )
        network.web3.provider.make_request("anvil_mine", [1])
        buyer2_tokens = detector.token.balanceOf(buyer2.address)
        result["details"]["buyer2_buy_success"] = True
        print(f"  Buyer2 매수 성공: {buyer2_tokens / 1e18:.4f} 토큰")

        detector.token.approve(detector.router_addr, buyer2_tokens, {"from": buyer2, "gas_price": GAS_PRICE, "gas_limit": GAS_LIMIT})
        detector.router.swapExactTokensForTokensSupportingFeeOnTransferTokens(
            int(buyer2_tokens * SELL_RATIO), 0, path_reverse, buyer2.address, deadline,
            {"from": buyer2, "gas_price": GAS_PRICE, "gas_limit": GAS_LIMIT}
        )
        network.web3.provider.make_request("anvil_mine", [1])
        result["details"]["buyer2_sell_success"] = True
        print(f"  Buyer2 매도 성공")
    except:
        pass

    # 최종 판정
    owner_ok = result["details"]["owner_buy_success"] and result["details"]["owner_sell_success"]
    buyer1_ok = result["details"]["buyer1_buy_success"] and result["details"]["buyer1_sell_success"]
    buyer2_fail = result["details"]["buyer2_buy_success"] and not result["details"]["buyer2_sell_success"]

    if owner_ok and buyer1_ok and buyer2_fail:
        result["result"] = "YES"
        result["confidence"] = "HIGH"
        result["details"]["whitelist_confirmed"] = True
        result["reason"] = f"{found_setter['name']}()로 Owner/Buyer1 정상, Buyer2 차단 - Whitelist 작동"
    else:
        result["result"] = "NO"
        result["confidence"] = "MEDIUM"
        result["reason"] = "Whitelist 함수 존재하나 작동 확인 안 됨"

    return result

def run_scenario(detector):
    global GAS_LIMIT
    GAS_LIMIT = detector.gaslimit
    network.gas_limit(detector.gaslimit)

    """Blacklist/Whitelist 탐지 시나리오 실행"""
    result = {
        "scenario": "blacklist_whitelist",
        "result": "UNKNOWN",
        "confidence": "LOW",
        "details": {
            "blacklist_test": None,
            "whitelist_test": None,
            "owner_address": None,
            "owner_impersonated": False
        },
        "reason": ""
    }

    try:
        print(f"\n{'='*60}")
        print("Blacklist/Whitelist Detection Scenario")
        print(f"{'='*60}")

        print("\n[Phase 0] Owner 확인")
        owner_address = detector.token_owner
        result["details"]["owner_address"] = owner_address

        if not owner_address:
            result["result"] = "NO"
            result["confidence"] = "HIGH"
            result["reason"] = "Owner를 찾을 수 없음"
            return result

        print(f"  Owner 주소: {owner_address}")

        print("\n[Phase 1] Owner 계정 impersonate")
        try:
            if accounts[0].balance() >= int(10 * 1e18):
                accounts[0].transfer(owner_address, int(10 * 1e18),gas_limit=GAS_LIMIT)
            owner_account = accounts.at(owner_address, force=True)
            result["details"]["owner_impersonated"] = True
            print(f"  Owner impersonate 성공")
        except Exception as e:
            print(f"  Owner impersonate 실패: {str(e)}")
            result["result"] = "UNKNOWN"
            result["confidence"] = "LOW"
            result["reason"] = "Owner impersonate 실패"
            return result

        privileged_accounts = [owner_account]
        seen_accounts = {owner_address.lower()}
        if hasattr(detector, "owner_candidate"):
            for candidate in detector.owner_candidate:
                if not candidate:
                    continue
                candidate_lower = candidate.lower()
                if candidate_lower in seen_accounts:
                    continue
                try:
                    candidate_account = accounts.at(candidate, force=True)
                    privileged_accounts.append(candidate_account)
                    seen_accounts.add(candidate_lower)
                    print(f"  추가 권한 후보 확보: {candidate}")
                except Exception as candidate_error:
                    print(f"  추가 후보 impersonate 실패 ({candidate}): {type(candidate_error).__name__}")

        abi = getattr(detector.token, "abi", None)
        if not abi:
            print("  Token ABI unavailable, falling back to default interface")
            abi = detector.token_abi

        print("\n[Phase 2] Blacklist 테스트")
        blacklist_result = test_blacklist(detector, owner_address, abi, privileged_accounts)
        result["details"]["blacklist_test"] = blacklist_result

        print("\n[Phase 3] Whitelist 테스트")
        whitelist_result = test_whitelist(detector, owner_address, owner_account, abi, privileged_accounts)
        result["details"]["whitelist_test"] = whitelist_result

        print("\n[Phase 4] 최종 판정")
        if blacklist_result["result"] == "YES":
            result["result"] = "YES"
            result["confidence"] = blacklist_result["confidence"]
            result["reason"] = f"Blacklist: {blacklist_result['reason']}"
        elif whitelist_result["result"] == "YES":
            result["result"] = "YES"
            result["confidence"] = whitelist_result["confidence"]
            result["reason"] = f"Whitelist: {whitelist_result['reason']}"
        else:
            result["result"] = "NO"
            result["confidence"] = "HIGH"
            result["reason"] = "Blacklist/Whitelist 함수 없거나 작동 안 함"

        return result

    except Exception as e:
        print(f"\n예상치 못한 오류: {str(e)}")
        result["result"] = "UNKNOWN"
        result["confidence"] = "LOW"
        result["reason"] = f"예상치 못한 오류 발생: {str(e)}"
        return result
