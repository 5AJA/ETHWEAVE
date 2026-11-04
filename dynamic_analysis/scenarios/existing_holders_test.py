#!/usr/bin/python3
"""
Existing Holders Sell Test Scenario

Validate that previously funded holders can still exit their positions.
"""

from brownie import accounts, chain, network
from brownie.network import gas_price
from eth_utils import to_checksum_address
from decimal import Decimal

from scripts import utils

MIN_GAS_BALANCE = int(2 * 10 ** 18)   # Minimum acceptable balance (2 ETH)
TARGET_GAS_BALANCE = int(12 * 10 ** 18)  # Ensure holders end up with ~12 ETH


def _get_eoa_holders(detector, limit=3):
    """Holder CSV에서 EOA 홀더 목록을 추려 반환"""
    csv_path = getattr(detector, "holder_csv_path", None)
    if not csv_path:
        return []

    try:
        holders = utils.load_holders(csv_path, detector.token_address)
    except Exception as exc:
        print(f"? Holder CSV load failed: {exc}")
        return []

    if not holders:
        print("? Holder 정보 없음.")
        return []

    eoa_holders = []
    for holder in holders:
        address = holder.get("holder_address")
        if not address:
            continue

        try:
            checksum_addr = to_checksum_address(address)
        except ValueError:
            continue

        try:
            code = network.web3.eth.get_code(checksum_addr)
        except Exception as exc:
            continue

        if code:
            continue

        holder["holder_address"] = checksum_addr
        eoa_holders.append(holder)

        if limit is not None and len(eoa_holders) >= limit:
            break

    if not eoa_holders:
        print("? 사용 가능한 EOA 홀더 없음.")
    elif limit is not None and len(eoa_holders) < limit:
        print(f"[warn] 요청한 {limit}명의 EOA 중 {len(eoa_holders)}명만 확보되었습니다.")

    return eoa_holders


def _ensure_gas(account, decimals=18):
    """Ensure impersonated account has enough ETH for gas usage."""
    current_balance = account.balance()
    if current_balance >= MIN_GAS_BALANCE:
        return

    accounts[0].transfer(account, TARGET_GAS_BALANCE)

    try:
        network.web3.provider.make_request("anvil_mine", [1])
    except Exception:
        pass


def run_scenario(detector):
    network.gas_limit(detector.gaslimit)
    result = {
        "scenario": "Existing Holders Sell Test",
        "result": "UNKNOWN",
        "confidence": "LOW",
        "reason": "",
        "details": {
            "holders_tested": [],
            "skipped_holders": [],
            "failures": 0
        },
    }

    holder_entries = _get_eoa_holders(detector, limit=3)
    if not holder_entries:
        result["reason"] = "Holder CSV 데이터가 없거나 일치하는 주소를 찾지 못했습니다."
        return result
    
    try:
        decimals = detector.token.decimals()
    except Exception:
        decimals = 18

    previous_gas_price = gas_price()
    gas_price("5 gwei")

    try:
        for holder in holder_entries:
            address = holder.get("holder_address")
            if not address:
                continue

            checksum_addr = to_checksum_address(address)
            chain.snapshot()
            holder_record = {
                "address": checksum_addr,
                "csv_balance": str(holder.get("balance_decimal")),
                "relative_share": str(holder.get("relative_share")),
            }

            try:
                onchain_balance = detector.token.balanceOf(checksum_addr)
            except Exception as exc:
                holder_record["error"] = f"balanceOf 실패: {exc}"
                result["details"]["skipped_holders"].append(holder_record)
                chain.revert()
                continue

            holder_record["onchain_balance"] = onchain_balance
            if onchain_balance == 0:
                holder_record["error"] = "잔액이 0입니다."
                result["details"]["skipped_holders"].append(holder_record)
                chain.revert()
                continue

            try:
                holder_account = accounts.at(checksum_addr, force=True)
            except Exception as exc:
                holder_record["error"] = f"impersonate 실패: {exc}"
                result["details"]["skipped_holders"].append(holder_record)
                chain.revert()
                continue

            _ensure_gas(holder_account, decimals)
            success, quote_received = utils.sell_tokens_to_pool(detector, holder_account, onchain_balance)
            if not success:
                result["details"]["failures"] += 1
                result["details"]["holders_tested"].append(holder_record)
                chain.revert()
                continue

            quote_decimals = getattr(detector, "quote_token_decimals", 18)
            quote_received_dec = Decimal(quote_received) / (Decimal(10) ** quote_decimals)
            # if (quote_received_dec) <= 0.000009:
            #     print(f"❌ sell failed | received: {quote_received_dec}")
            #     success = False

            holder_record["success"] = success
            holder_record["quote_received"] = quote_received
            result["details"]["holders_tested"].append(holder_record)

            if not success:
                result["details"]["failures"] += 1

            chain.revert()
    finally:
        gas_price(previous_gas_price)
        try:
            chain.revert()
        except Exception:
            pass

    tested = len(result["details"]["holders_tested"])
    failures = result["details"]["failures"]
    if tested == 0:
        result["reason"] = "홀더 테스트를 수행하지 못했습니다."
    else:
        if failures >= 1:
            result["result"] = "YES"
            result["confidence"] = "HIGH"
            result["reason"] = f"{tested}명 중 {failures}명이 매도에 실패했습니다."
        else:
            result["result"] = "NO"
            result["confidence"] = "MEDIUM" if failures == 1 else "HIGH"
            if failures == 0:
                result["reason"] = "모든 기존 홀더가 매도에 성공했습니다."
            else:
                result["reason"] = "일부 홀더만 매도 실패했지만 임계치 미만입니다."

    return result
