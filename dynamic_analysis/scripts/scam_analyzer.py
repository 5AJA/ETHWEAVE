#!/usr/bin/python3
"""
Brownie 동적 허니팟 탐지기
토큰 주소를 입력받아 메인넷 포크 환경에서 매수/매도 시뮬레이션을 수행하고
허니팟 여부를 판단합니다.
"""

import json
import os
import requests
from datetime import datetime
from pathlib import Path
from input_parser import parse_csv 
from brownie import accounts, chain,network,Contract,project, web3
from brownie.network import gas_price
from eth_utils import to_checksum_address
from dotenv import load_dotenv
import sys
import time
import psutil
from web3 import Web3

# Import scenario modules
sys.path.insert(0, str(Path(__file__).parent.parent))
from scenarios import (
    basic_swap_test,
    black_whitelist,
    owner_suspend,
    exterior_function_call,
    owner_bal_manipulation,
    unlimited_mint,
    owner_tax_manipulation,
    existing_holders_test,
)
from scripts import utils, v3_utils

# Constants
ROUTER_LIST = {
    "UniswapV2": "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D",
    "UniswapV2_Old": "0xf164fC0Ec4E93095b804a4795bBe1e041497b92a",
    "SusiswapV2":"0xd9e1cE17f2641f24aE83637ab66a2cca9C378B9F",
    "ShibaswapV1": "0x03f7724180AA6b939894B5Ca4314783B0b36b329",
    "PancakeV2": "0xEfF92A263d31888d860bD50809A8D171709b7b1c",
    "Fraxwap": "0xC14d550632db8592D1243Edc8B95b0Ad06703867",
    "Whiteswap":"0x463672ffdED540f7613d3e8248e3a8a51bAF7217"
}
FACTORY_LIST = {
    'UniswapV2':'0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f',
    'UniswapV2_Old':'0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f',
    'SusiswapV2':'0xC0AEe478e3658e2610c5F7A4A2E1777cE9e4f2Ac',
    'PancakeV2':'0x1097053Fd2ea711dad45caCcc45EfF7548fCB362',
    'ShibaswapV1':'0x115934131916C8b277DD010Ee02de363c09d037c',
    'Fraxwap':'0x43eC799eAdd63848443E2347C49f5f52e8Fe0F6f',
    'Whiteswap':'0x69bd16aE6F507bd3Fc9eCC984d50b04F029EF677'
}

# UNISWAP_V2_FACTORY = "0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f"
WETH_ADDRESS = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"

# Major Quote Tokens (우선순위 순서)
QUOTE_TOKENS = {
    "WETH": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
    "USDT": "0xdAC17F958D2ee523a2206206994597C13D831ec7",
    "USDC": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
    "DAI": "0x6B175474E89094C44Da98b954EedeAC495271d0F",
}

QUOTE_TOKEN_DECIMALS = {
    "WETH": 18,
    "USDT": 6,
    "USDC": 6,
    "DAI": 18,
}

# Liquidity pool settings
INITIAL_ETH_LIQUIDITY = 10.0
INITIAL_TOKEN_LIQUIDITY = 1_000_000

# ABI Json File Path
TOKEN_ABI_JSON = "interfaces/IERC20.json"
PAIR_ABI_JSON = "interfaces/IUniswapV2Pair.json"
ROUTER_ABI_JSON = "interfaces/IUniswapV2Router02.json"
FACTORY_ABI_JSON = "interfaces/IUniswapV2Factory.json"
WETH_ABI_JSON = "interfaces/IWETH.json"
EXTENDED_ABI_JSON = "interfaces/IExtendedERC20.json"

V3_POOL_ABI_JSON = "interfaces/IUniswapV3Pool.json"
V3_FACTORY_ABI_JSON = "interfaces/IUniswapV3Factory.json"
V3_SWAP_ROUTER_ABI_JSON = "interfaces/IUniswapV3SwapRouter.json"
V3_QUOTER_ABI_JSON = "interfaces/IQuoterV2.json"

# Honeypot detection thresholds
THRESHOLDS = {
    "high_risk": 0.5,
    "medium_risk": 0.9,
    "safe": 0.9
}

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
EIP1967_IMPLEMENTATION_SLOT = int(
    "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc", 16
)
EIP1967_BEACON_SLOT = int(
    "0xa3f0ad74e5423aebfd80d3ef4346578335a9a72aeaee59ff6cb3582b35133d50", 16
)
EIP1822_IMPLEMENTATION_SLOT = int(Web3.keccak(text="PROXIABLE").hex(), 16) - 1
BEACON_IMPLEMENTATION_ABI = [
    {
        "inputs": [],
        "name": "implementation",
        "outputs": [{"internalType": "address", "name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    }
]


def format_units(amount, decimals):
    """Convert raw token amount to human-readable units for logs."""
    if decimals is None:
        return amount
    return amount / (10 ** decimals)


def wait_for_rpc_ready(timeout=120):
    """Wait until the RPC endpoint responds after launch or reset."""
    start_time = time.time()
    while time.time() - start_time < timeout:
        if network.is_connected():
            try:
                network.web3.eth.block_number
                return
            except Exception:
                pass
        time.sleep(0.5)
    raise RuntimeError("RPC endpoint did not become ready within the timeout window")


def ensure_rpc_fork(fork_url, block_number):
    """Launch or reset Anvil to the desired fork block."""
    if not network.is_connected():
        network.connect("anvil-fork")
        wait_for_rpc_ready()

    reset_payload = {"forking": {"jsonRpcUrl": fork_url, "blockNumber": block_number}}
    response = network.web3.provider.make_request("anvil_reset", [reset_payload])
    if response and response.get("error"):
        raise RuntimeError(f"anvil_reset failed: {response['error']}")
    time.sleep(0.5)
    wait_for_rpc_ready()


def set_rpc_timeout(seconds=120):
    """Increase HTTP RPC timeout for both Brownie network.web3 and global web3."""
    for w3 in (getattr(network, "web3", None), globals().get("web3")):
        try:
            if w3 and getattr(w3, "provider", None) and hasattr(w3.provider, "_request_kwargs"):
                w3.provider._request_kwargs["timeout"] = seconds
        except Exception as exc:
            print(f"[warn] Failed to set RPC timeout: {exc}")

class ScamAnalyzer:
    """허니팟 탐지 클래스"""

    def __init__(self, token_address, token_idx, service,blocknum,pair_addr,pair_creator=None,results=None, holder_csv_path=None):
        """
        초기화

        Args:
            token_address (str): 검사할 토큰 주소
        """
        with open(TOKEN_ABI_JSON,'r') as f:
            self.token_abi = json.load(f)
        with open(PAIR_ABI_JSON,'r') as f:
            self.pair_abi = json.load(f)
        with open(ROUTER_ABI_JSON,'r') as f:
            self.router_abi = json.load(f)
        with open(FACTORY_ABI_JSON,'r') as f:
            self.factory_abi = json.load(f)
        with open(WETH_ABI_JSON, 'r') as f:
            self.weth_abi = json.load(f)

        with open(V3_POOL_ABI_JSON, 'r') as f:
            self.v3_pool_abi = json.load(f)
        with open(V3_FACTORY_ABI_JSON, 'r') as f:
            self.v3_factory_abi = json.load(f)
        with open(V3_SWAP_ROUTER_ABI_JSON, 'r') as f:
            self.v3_router_abi = json.load(f)
        with open(V3_QUOTER_ABI_JSON, 'r') as f:
            self.v3_quoter_abi = json.load(f)

        self.router_addr = ROUTER_LIST[service]
        self.factory_addr = FACTORY_LIST[service]

        self.v3_factory_addr = v3_utils.V3_FACTORY_ADDRESS
        self.v3_router_addr = v3_utils.V3_SWAP_ROUTER_ADDRESS
        self.v3_quoter_addr = v3_utils.V3_QUOTER_V2_ADDRESS

        self.token_address = to_checksum_address(token_address)
        self.token_idx = token_idx
        self.blocknum = blocknum
        self.router = Contract.from_abi("IUniswapV2Router02",self.router_addr, self.router_abi)
        self.factory = Contract.from_abi("IUniswapV2Factory",self.factory_addr, self.factory_abi)
        self.weth = Contract.from_abi("IWETH",WETH_ADDRESS,self.weth_abi)

        self.v3_factory = Contract.from_abi("UniswapV3Factory", self.v3_factory_addr, self.v3_factory_abi)
        self.v3_router = Contract.from_abi("SwapRouter", self.v3_router_addr, self.v3_router_abi)
        self.v3_quoter = Contract.from_abi("QuoterV2", self.v3_quoter_addr, self.v3_quoter_abi)

        self.pair_address = pair_addr
        self.liquidity_provider = None
        self.token_owner = None
        self.pair_creator = to_checksum_address(pair_creator) if pair_creator else None
        self.is_verified = False
        self.implementation_address = None
        self.quote_token_address = None
        self.quote_token = None
        self.quote_token_decimals = 18
        self.eth_usd_price = None  # cached 1 WETH -> USD estimate
        self.gaslimit = web3.eth.get_block(blocknum)["gasLimit"]
        self.holder_csv_path = holder_csv_path

        self.dex_version = None
        self.fee_tier = None

        if results is not None:
            self.results = results
        else:
            self.results = {
                "analysis_info":{
                    "token_address": self.token_address,
                    "timestamp": datetime.now().isoformat(),
                    "network": network.show_active(),
                    "liquidity_created": False,
                },
                "token_info": [],
                "tests": []
            }

    def find_owner(self):
        """Locate the token owner using common getter patterns."""

        def is_valid(address):
            return address and address != ZERO_ADDRESS

        owner_getters = [
            ("owner", lambda: self.token.owner()),
            ("getOwner", lambda: self.token.getOwner()),
        ]

        for label, getter in owner_getters:
            try:
                owner = getter()
                if is_valid(owner):
                    return owner
            except AttributeError:
                pass
            except Exception:
                pass

        try:
            owner_attr = getattr(self.token, "_owner")
        except AttributeError:
            owner_attr = None

        if owner_attr is not None:
            candidates = []
            if callable(owner_attr):
                candidates.append(owner_attr)
            if hasattr(owner_attr, "call"):
                candidates.append(owner_attr.call)

            for candidate in candidates:
                try:
                    owner = candidate()
                    if is_valid(owner):
                        return owner
                except Exception:
                    pass

        # Brownie stores tx owner on `_owner`, so fall back to raw web3 contract call.
        try:
            raw_contract = web3.eth.contract(address=self.token_address, abi=self.token.abi)
            owner = raw_contract.functions._owner().call()
            if is_valid(owner):
                return owner
        except Exception:
            pass

        return None

    def _safe_erc20_call(self, func_name, default):
        """
        기본 ABI에 해당 함수가 없어도 ERC20 표준 ABI를 fallback으로 호출한다.
        """
        try:
            fn = getattr(self.token, func_name)
            return fn()
        except Exception:
            pass

        try:
            raw_contract = web3.eth.contract(address=self.token_address, abi=self.token_abi)
            raw_fn = getattr(raw_contract.functions, func_name)
            return raw_fn().call()
        except Exception:
            return default

    def _load_basic_token_info(self):
        """토큰 기본 정보 로드 (이미 self.token이 설정된 상태에서 호출)"""
        name = self._safe_erc20_call("name", "Unknown")
        symbol = self._safe_erc20_call("symbol", "Unknown")
        decimals = self._safe_erc20_call("decimals", 18)

        self.results["token_info"] = {
            "name": name,
            "symbol": symbol,
            "decimals": decimals
        }

        print(f"\n{'='*60}")
        print(f"토큰 정보")
        print(f"{'='*60}")
        print(f"주소: {self.token_address}")
        print(f"이름: {name}")
        print(f"심볼: {symbol}")
        print(f"소수점: {decimals}")

        return True

    def _read_address_from_slot(self, slot):
        """지정된 슬롯에서 주소를 읽어온다."""
        try:
            raw = web3.eth.get_storage_at(self.token_address, slot)
        except Exception as exc:
            print(f"⚠️ storage slot {hex(slot)} 읽기 실패: {exc}")
            return None

        if not raw or not any(raw):
            return None

        candidate = "0x" + raw[-20:].hex()
        if candidate.lower() == ZERO_ADDRESS.lower():
            return None

        try:
            return to_checksum_address(candidate)
        except Exception:
            return None

    def _detect_proxy_implementation(self):
        """EIP-1822 / EIP-1967 / Beacon 패턴만 감지."""
        impl = self._read_address_from_slot(EIP1967_IMPLEMENTATION_SLOT)
        if impl:
            print(f"⚠️  EIP-1967 Proxy 감지 - 구현 주소: {impl}")
            return impl

        beacon = self._read_address_from_slot(EIP1967_BEACON_SLOT)
        if beacon:
            print(f"⚠️  Beacon Proxy 감지 - Beacon 주소: {beacon}")
            impl_addr = None
            try:
                beacon_contract = web3.eth.contract(
                    address=beacon,
                    abi=BEACON_IMPLEMENTATION_ABI,
                )
                impl_addr = beacon_contract.functions.implementation().call()
            except Exception as exc:
                print(f"⚠️  Beacon implementation() 호출 실패: {exc}")

            if impl_addr:
                if isinstance(impl_addr, (bytes, bytearray)):
                    impl_addr = "0x" + impl_addr[-20:].hex()
                if impl_addr == ZERO_ADDRESS:
                    return None
                impl = to_checksum_address(impl_addr)
                print(f"   ↳ 구현 주소: {impl}")
                return impl

        impl = self._read_address_from_slot(EIP1822_IMPLEMENTATION_SLOT)
        if impl:
            print(f"⚠️  EIP-1822 Proxy 감지 - 구현 주소: {impl}")
            return impl

        return None

    def load_token(self):
        """토큰 컨트랙트 로드 및 기본 정보 조회 (Etherscan API V2 대응)"""
        try:
            # 1. Etherscan API 키 가져오기
            etherscan_api_key = os.environ.get("ETHERSCAN_TOKEN")
            if not etherscan_api_key:
                print("❌ .env 파일에 ETHERSCAN_TOKEN이 설정되지 않았습니다.")
                print(f"⚠️  기본 ERC20 ABI 사용")
                self.is_verified = False
                # 기본 ERC20 ABI로 계속 진행
                self.token = Contract.from_abi("Token", self.token_address, self.token_abi)
                return self._load_basic_token_info()

            print(f"\n[INFO] Etherscan API(V2)에서 토큰 ABI 가져오는 중... ({self.token_address})")

            # 2. 체인 ID 자동 결정
            current_network = network.show_active().lower()
            chain_map = {
                "mainnet": 1,
                "ethereum": 1,
                "sepolia": 11155111,
                "holesky": 17000,
                "bsc": 56,
                "polygon": 137,
                "arbitrum": 42161,
                "optimism": 10
            }
            chain_id = chain_map.get(current_network, 1)

            abi_address = self.token_address
            proxy_target = self._detect_proxy_implementation()
            if proxy_target:
                abi_address = proxy_target
                self.implementation_address = proxy_target
                print(f"⚠️  Proxy 컨트랙트 감지 - 구현 주소 ABI 사용: {proxy_target}")

            # 3. V2 엔드포인트 구성
            api_url = (
                f"https://api.etherscan.io/v2/api?"
                f"chainid={chain_id}&"
                f"module=contract&"
                f"action=getabi&"
                f"address={abi_address}&"
                f"apikey={etherscan_api_key}"
            )

            # 4. API 요청
            response = requests.get(api_url, timeout=15)

            # 5. JSON 응답 검증
            try:
                response_data = response.json()
            except Exception:
                print("❌ API 응답이 JSON 형식이 아닙니다.")
                print(f"   응답 코드: {response.status_code}")
                print(f"   응답 내용 (앞부분): {response.text[:200]}...")
                print(f"⚠️  Verified 되지 않은 컨트랙트 - 기본 ERC20 ABI 사용")
                self.is_verified = False
                # 기본 ERC20 ABI로 계속 진행
                abi = self.token_abi
                self.token = Contract.from_abi("Token", self.token_address, abi)
                # 기본 정보만 가져오고 계속 진행
                return self._load_basic_token_info()

            # 6. 결과 처리
            if response_data.get("status") == "1" and response_data.get("result"):
                abi_string = response_data["result"]
                abi = json.loads(abi_string)
                print(f"✅ ABI 로드 성공 (체인 ID: {chain_id})")
                self.is_verified = True  # Verified 컨트랙트
            else:
                error_message = response_data.get("result", response_data.get("message", "Unknown API error"))
                print(f"❌ 토큰 ABI 로드 실패: {error_message}")
                print(f"⚠️  Verified 되지 않은 컨트랙트 - 기본 ERC20 ABI 사용")
                self.is_verified = False  # Unverified 컨트랙트
                # 기본 ERC20 ABI 사용
                abi = self.token_abi

            # 7. Contract 객체 생성
            self.token = Contract.from_abi("Token", self.token_address, abi)
            
            # 8. 토큰 기본 정보 로드
            return self._load_basic_token_info()

        except Exception as e:
            print(f"❌ 토큰 로드 실패 (전역 예외): {str(e)}")
            import traceback
            traceback.print_exc()
            self.results["error"] = f"Token load failed (Global Exc): {str(e)}"
            return False


    def check_liquidity_pool(self):
        """
        Search both V2 and V3 pools and select the one with highest liquidity
        """
        try:
            v2_candidates = self._find_v2_pools()
            v3_candidates = self._find_v3_pools()

            all_candidates = v2_candidates + v3_candidates

            if not all_candidates:
                self.results["error"] = "No liquidity pool with reserves"
                return False

            best = max(all_candidates, key=lambda x: x['comparable_value'])

            self.pair_address = best['pool_address']
            self.quote_token_address = best['quote_address']
            self.quote_token_decimals = best['quote_decimals']
            self.dex_version = best['dex_version']
            self.fee_tier = best.get('fee_tier')
            selected_dex = best.get("dex_name")

            if self.dex_version == "v2":
                if best.get("router_address"):
                    self.router_addr = best["router_address"]
                    self.router = Contract.from_abi("IUniswapV2Router02", self.router_addr, self.router_abi)
                if best.get("factory_address"):
                    self.factory_addr = best["factory_address"]
                    self.factory = Contract.from_abi("IUniswapV2Factory", self.factory_addr, self.factory_abi)

            try:
                if self.quote_token_address.lower() == WETH_ADDRESS.lower():
                    self.quote_token = self.weth
                    quote_symbol = best['quote_symbol'] or "WETH"
                else:
                    self.quote_token = Contract.from_abi("QuoteToken", self.quote_token_address, self.token_abi)
                    try:
                        quote_symbol = self.quote_token.symbol()
                    except Exception:
                        quote_symbol = best['quote_symbol'] or "QUOTE"
            except Exception as e:
                print(f"   ⚠️  Quote token load failed, fallback to WETH: {e}")
                self.quote_token_address = WETH_ADDRESS
                self.quote_token = self.weth
                self.quote_token_decimals = QUOTE_TOKEN_DECIMALS.get("WETH", 18)
                quote_symbol = "WETH"

            token_decimals = self.results.get("token_info", {}).get("decimals", 18)

            if self.dex_version == "v2":
                pair = Contract.from_abi("IUniswapV2Pair", self.pair_address, self.pair_abi)
                reserves = pair.getReserves()
                token0 = pair.token0().lower()

                if token0 == self.quote_token_address.lower():
                    quote_reserve = reserves[0]
                    token_reserve = reserves[1]
                else:
                    quote_reserve = reserves[1]
                    token_reserve = reserves[0]

                self.results["liquidity_info"] = {
                    "pair_address": self.pair_address,
                    "pool_existed": True,
                    "dex_version": "v2",
                    "dex_name": selected_dex,
                    "router_address": self.router_addr,
                    "quote_symbol": quote_symbol,
                    "quote_reserve": quote_reserve,
                    "token_reserve": token_reserve,
                    "quote_decimals": self.quote_token_decimals,
                    "token_decimals": token_decimals,
                }
            else:
                pool = Contract.from_abi("IUniswapV3Pool", self.pair_address, self.v3_pool_abi)
                liquidity = pool.liquidity()
                slot0 = pool.slot0()
                quote_reserve = best.get("quote_reserve")

                self.results["liquidity_info"] = {
                    "pool_address": self.pair_address,
                    "pool_existed": True,
                    "dex_version": "v3",
                    "dex_name": selected_dex,
                    "quote_symbol": quote_symbol,
                    "liquidity": liquidity,
                    "sqrt_price_x96": slot0[0],
                    "current_tick": slot0[1],
                    "fee_tier": self.fee_tier,
                    "quote_decimals": self.quote_token_decimals,
                    "token_decimals": token_decimals,
                    "quote_reserve": quote_reserve,
                    "token_reserve": best.get("token_reserve"),
                }

            self.results["analysis_info"]["liquidity_created"] = False

            # Final concise log for selected pool
            version_display = f"{self.dex_version.upper()}" + (f" (fee: {self.fee_tier/10000}%)" if self.fee_tier else "")
            dex_display = f" [{selected_dex}]" if selected_dex else ""
            print(f"{'='*60}")
            print(f"Liquidity Pool Selected: {best['quote_symbol']} - {version_display}{dex_display}")
            print(f"Address: {self.pair_address}")
            if self.dex_version == "v2":
                print(f"Quote balance: {format_units(self.results['liquidity_info']['quote_reserve'], self.quote_token_decimals):.6f} {quote_symbol}")
                print(f"Token balance: {self.results['liquidity_info']['token_reserve'] / (10 ** token_decimals):.6f}")
            else:
                print(f"Liquidity: {liquidity}")
                print(f"Current tick: {slot0[1]}")
                if quote_reserve is not None:
                    print(f"Quote reserve: {format_units(quote_reserve, self.quote_token_decimals):.6f} {quote_symbol}")
                if best.get("token_reserve") is not None:
                    token_reserve_v3 = best["token_reserve"]
                    print(f"Token reserve: {token_reserve_v3 / (10 ** token_decimals):.6f}")
            print(f"{'='*60}")
            return True

        except Exception as e:
            print(f"❌ Liquidity check failed: {str(e)}")
            import traceback
            traceback.print_exc()
            self.results["liquidity_error"] = str(e)
            return False

    def _find_v2_pools(self):
        """Find all V2 pools for the token"""
        candidates = []

        for dex_name, factory_addr in FACTORY_LIST.items():
            try:
                factory = Contract.from_abi("IUniswapV2Factory", factory_addr, self.factory_abi)
            except Exception as e:
                print(f"   V2 factory load failed for {dex_name}: {str(e)}")
                continue

            router_addr = ROUTER_LIST.get(dex_name)

            for quote_symbol, quote_addr in QUOTE_TOKENS.items():
                try:
                    pair_addr = factory.getPair(quote_addr, self.token_address)

                    if pair_addr is None or pair_addr == ZERO_ADDRESS:
                        continue

                    pair = Contract.from_abi("IUniswapV2Pair", pair_addr, self.pair_abi)
                    reserves = pair.getReserves()
                    token0 = pair.token0().lower()

                    if token0 == quote_addr.lower():
                        quote_reserve = reserves[0]
                        token_reserve = reserves[1]
                    else:
                        quote_reserve = reserves[1]
                        token_reserve = reserves[0]

                    current_decimals = QUOTE_TOKEN_DECIMALS.get(quote_symbol, 18)
                    comparable_value = utils.quote_value_in_usd(
                        quote_symbol,
                        quote_reserve,
                        current_decimals,
                        lambda: utils.ensure_eth_usd_price(
                            self.router, WETH_ADDRESS, QUOTE_TOKENS, QUOTE_TOKEN_DECIMALS, cache_obj=self
                        ),
                    )

                    candidates.append({
                        'pool_address': pair_addr,
                        'quote_symbol': quote_symbol,
                        'quote_address': quote_addr,
                        'quote_decimals': current_decimals,
                        'quote_reserve': quote_reserve,
                        'token_reserve': token_reserve,
                        'dex_version': 'v2',
                        'dex_name': dex_name,
                        'router_address': router_addr,
                        'factory_address': factory_addr,
                        'comparable_value': comparable_value
                    })

                except Exception as e:
                    print(f"   V2 {dex_name} {quote_symbol} pool error: {str(e)}")
                    continue

        return candidates

    def _find_v3_pools(self):
        """Find all V3 pools for the token"""
        candidates = []

        for quote_symbol, quote_addr in QUOTE_TOKENS.items():
            for fee in v3_utils.V3_FEE_TIERS:
                try:
                    # Try both token orderings (V3 is order-sensitive)
                    pool_addr = self.v3_factory.getPool(quote_addr, self.token_address, fee)

                    # If not found, try reverse order
                    if pool_addr == ZERO_ADDRESS or pool_addr is None:
                        pool_addr = self.v3_factory.getPool(self.token_address, quote_addr, fee)

                    if pool_addr == ZERO_ADDRESS or pool_addr is None:
                        continue

                    pool = Contract.from_abi("IUniswapV3Pool", pool_addr, self.v3_pool_abi)

                    try:
                        slot0 = pool.slot0()
                        sqrt_price_x96 = slot0[0]
                        current_tick = slot0[1]
                    except Exception:
                        # Some pools revert on slot0 if not initialized yet
                        sqrt_price_x96 = 0
                        current_tick = None

                    try:
                        liquidity = pool.liquidity()
                    except Exception:
                        liquidity = 0

                    # Use actual token balances as comparable metric
                    current_decimals = QUOTE_TOKEN_DECIMALS.get(quote_symbol, 18)
                    try:
                        quote_token = Contract.from_abi("QuoteToken", quote_addr, self.token_abi)
                        quote_reserve = quote_token.balanceOf(pool_addr)
                    except Exception:
                        quote_reserve = 0

                    try:
                        token_reserve = self.token.balanceOf(pool_addr)
                    except Exception:
                        token_reserve = 0

                    comparable_value = utils.quote_value_in_usd(
                        quote_symbol,
                        quote_reserve,
                        current_decimals,
                        lambda: utils.ensure_eth_usd_price(
                            self.router, WETH_ADDRESS, QUOTE_TOKENS, QUOTE_TOKEN_DECIMALS, cache_obj=self
                        ),
                    )

                    if liquidity == 0 and quote_reserve == 0 and token_reserve == 0:
                        continue

                    candidates.append({
                        'pool_address': pool_addr,
                        'quote_symbol': quote_symbol,
                        'quote_address': quote_addr,
                        'quote_decimals': current_decimals,
                        'fee_tier': fee,
                        'liquidity': liquidity,
                        'quote_reserve': quote_reserve,
                        'token_reserve': token_reserve,
                        'sqrt_price_x96': sqrt_price_x96,
                        'current_tick': current_tick,
                        'dex_version': 'v3',
                        'comparable_value': comparable_value
                    })

                except Exception as e:
                    print(f"   V3 {quote_symbol} pool (fee: {fee/10000}%) error: {str(e)}")
                    continue

        return candidates

    def run_scenario(self, scenario_module):
        """
        시나리오 실행

        Args:
            scenario_module: 시나리오 모듈 (run_scenario 함수 포함)

        Returns:
            list: 테스트 결과 리스트
        """
        return scenario_module.run_scenario(self)

    def get_creator(self):
        etherkey = os.environ.get("ETHERSCAN_TOKEN")
        param = {
            "chainid":1,
            "module": "contract",
            "action": "getcontractcreation",
            "contractaddresses": self.token_address,
            "apikey": etherkey
        }
        res = requests.get("https://api.etherscan.io/v2/api",params=param)
        response = res.json()['result'][0]
        return response['contractCreator']

    def run_tests(self,step = None):
        """
        환경 구성 및 모든 시나리오 실행
        """
        # 토큰 로드
        if not self.load_token():
            return self.results

        # 토큰 소유자 정보 확보
        self.token_owner = self.find_owner()
        self.contract_creator = self.get_creator()
        try:
            controller_attr = getattr(self.token, "controller")
        except AttributeError:
            controller_attr = None

        if controller_attr is not None:
           self.controller = self.token.controller()
        else:
            self.controller = None
        print(self.controller)

        self.owner_candidate = [self.token_owner,self.contract_creator,self.pair_creator,self.controller]

        print(f"토큰 소유자: {self.token_owner}")
        print(f"{'='*60}\n")

        # 유동성 풀 생성
        if not self.check_liquidity_pool():
            print("❌ 유동성 풀 확인 실패로 테스트를 중단합니다.")
            return self.results

        # 시나리오 실행 결과 저장
        if step == 1:
            print(f"\n{'#'*60}")
            print("# Scenario 1: Basic Swap Test")
            print(f"{'#'*60}")
            basic_results = self.run_scenario(basic_swap_test)
            self.results["tests"] = basic_results
            self.analyze_results()
            self.print_final_summary()

            return self.results
        
        self.results["scenarios"] = {}

        # 1. Basic Swap Test
        print(f"\n{'#'*60}")
        print("# Scenario 1: Basic Swap Test")
        print(f"{'#'*60}")
        basic_results = self.run_scenario(basic_swap_test)
        self.results["tests"] = basic_results
        self.analyze_results()

        # 2. Existing Holders Sell Test
        if self.holder_csv_path:
            print(f"\n{'#'*60}")
            print("# Scenario 2: Existing Holders Sell Test")
            print(f"{'#'*60}")
            existing_result = self.run_scenario(existing_holders_test)
            self.print_scenario_result(existing_result)
            self.results["scenarios"]["existing_holders_test"] = existing_result

        # Verified 컨트랙트만 고급 시나리오 실행
        if not self.is_verified:
            print(f"\n{'⚠'*60}")
            print("⚠️  Unverified 컨트랙트 - 고급 시나리오 분석 스킵")
            print(f"{'⚠'*60}\n")
            return self.results

        # 3. Blacklist/Whitelist Test
        print(f"\n{'#'*60}")
        print("# Scenario 3: Blacklist/Whitelist Detection")
        print(f"{'#'*60}")
        blacklist_result = self.run_scenario(black_whitelist)
        self.results["scenarios"]["blacklist_whitelist"] = blacklist_result
        self.print_scenario_result(blacklist_result)

        # 4. Exterior Function Call Test
        print(f"\n{'#'*60}")
        print("# Scenario 4: Exterior Function Call Detection")
        print(f"{'#'*60}")
        exterior_result = self.run_scenario(exterior_function_call)
        self.results["scenarios"]["exterior_function_call"] = exterior_result

        # 5. Owner BalanceManipulation Test
        print(f"\n{'#'*60}")
        print("# Scenario 5: Owner Balance Manipulation Detection")
        print(f"{'#'*60}")
        bal_manipulation_result = self.run_scenario(owner_bal_manipulation)
        self.results["scenarios"]["owner_bal_manipulation"] = bal_manipulation_result

        # 6. Owner Tax Manipulation Test
        print(f"\n{'#'*60}")
        print("# Scenario 6: Owner Tax Manipulation Detection")
        print(f"{'#'*60}")
        tax_manipulation_result = self.run_scenario(owner_tax_manipulation)
        self.results["scenarios"]["owner_tax_manipulation"] = tax_manipulation_result
        
        # 7. Trading Suspend Test
        print(f"\n{'#'*60}")
        print("# Scenario 7: Trading Suspend Detection")
        print(f"{'#'*60}")
        suspend_result = self.run_scenario(owner_suspend)
        self.results["scenarios"]["trading_suspend"] = suspend_result
        self.print_scenario_result(suspend_result)

        # 8. Unlimited Mint Test
        print(f"\n{'#'*60}")
        print("# Scenario 8: Unlimited Mint Detection")
        print(f"{'#'*60}")
        unlimited_mint_result = self.run_scenario(unlimited_mint)
        self.results["scenarios"]["unlimited_mint"] = unlimited_mint_result

        # 최종 요약
        self.print_final_summary()

        return self.results

    def print_scenario_result(self, result):
        """시나리오 결과 출력"""
        print(f"\n{'='*60}")
        print(f"시나리오: {result['scenario']}")
        print(f"{'='*60}")
        print(f"판정: {result['result']}")
        print(f"신뢰도: {result['confidence']}")
        print(f"이유: {result['reason']}")
        print(f"\n상세 정보:")
        for key, value in result['details'].items():
            # 리스트 처리
            if isinstance(value, list):
                if len(value) > 0:
                    print(f"  {key}: {', '.join(str(v) for v in value)}")
            # 딕셔너리 처리
            elif isinstance(value, dict):
                if len(value) > 0:
                    print(f"  {key}:")
                    for k, v in value.items():
                        print(f"    {k}: {v}")
            # None이 아닌 값 처리
            elif value is not None:
                if isinstance(value, bool):
                    print(f"  {key}: {value}")
                elif isinstance(value, (int, float)) and key.endswith('_received'):
                    print(f"  {key}: {value / 1e18:.6f} ETH")
                else:
                    print(f"  {key}: {value}")
        print(f"{'='*60}\n")

    def print_final_summary(self):
        """최종 요약 출력"""
        print(f"\n{'#'*60}")
        print("# 최종 분석 요약")
        print(f"{'#'*60}\n")
        
        # Basic Swap Test 결과
        if "verdict" in self.results:
            print(f"[1] Basic Swap Test (허니팟 탐지)")
            print(f"    판정: {self.results['verdict']['conclusion']}")
            print(f"    위험도: {self.results['verdict']['risk_level']}")
            print(f"    매도 성공률: {self.results['verdict']['sell_success_rate']}")

        # Existing Holders Sell test 결과
        if "existing_holders_test" in self.results.get("scenarios", {}):
            eh_result = self.results["scenarios"]["existing_holders_test"]
            print(f"\n[2] Existing Holders Sell Test")
            print(f"    판정: {eh_result['result']} (신뢰도: {eh_result['confidence']})")
            print(f"    {eh_result['reason']}")

        # Blacklist/Whitelist 결과
        if "blacklist_whitelist" in self.results.get("scenarios", {}):
            bl_result = self.results["scenarios"]["blacklist_whitelist"]
            print(f"\n[3] Blacklist/Whitelist Detection")
            print(f"    판정: {bl_result['result']} (신뢰도: {bl_result['confidence']})")
            print(f"    {bl_result['reason']}")

        # Exterior Function Call 결과
        if "exterior_function_call" in self.results.get("scenarios", {}):
            ef_result = self.results["scenarios"]["exterior_function_call"]
            print(f"\n[4] Exterior Function Call Detection")
            print(f"    판정: {ef_result['result']}")
            external_calls_count = ef_result['external_calls_count']
            if external_calls_count > 0:
                print(f"    Transfer 결과: {'실패 (Reverted)' if ef_result['reverted'] else '성공'}")
                print(f"    외부 컨트랙트 호출: {external_calls_count}개")
            else:
                print(f"    외부 컨트랙트 호출 없음")
        
        # Owner Balance Manipulation 결과
        if "owner_bal_manipulation" in self.results.get("scenarios", {}):
            ob_result = self.results["scenarios"]["owner_bal_manipulation"]
            print(f"\n[5] Owner Balance Manipulation Detection")
            print(f"    판정: {ob_result['result']} (신뢰도: {ob_result['confidence']})")
            print(f"    {ob_result['reason']}")

        # Owner Tax Manipulation 결과
        if "owner_tax_manipulation" in self.results.get("scenarios", {}):
            ot_result = self.results["scenarios"]["owner_tax_manipulation"]
            print(f"\n[6] Owner Tax Manipulation Detection")
            print(f"    판정: {ot_result['result']} (신뢰도: {ot_result['confidence']})")
            print(f"    {ot_result['reason']}")

        # Trading Suspend 결과
        if "trading_suspend" in self.results.get("scenarios", {}):
            ts_result = self.results["scenarios"]["trading_suspend"]
            print(f"\n[7] Trading Suspend Detection")
            print(f"    판정: {ts_result['result']} (신뢰도: {ts_result['confidence']})")
            print(f"    {ts_result['reason']}")

        # Unlimited Mint 결과
        if "unlimited_mint" in self.results.get("scenarios", {}):
            ts_result = self.results["scenarios"]["unlimited_mint"]
            print(f"\n[8] Unlimited Mint Detection")
            print(f"    판정: {ts_result['result']} (신뢰도: {ts_result['confidence']})")
            print(f"    {ts_result['reason']}")

        print(f"\n{'#'*60}\n")

    def analyze_results(self):
        """테스트 결과 분석 및 종합 판정"""
        print(f"{'#'*60}")
        print("# 종합 분석 결과")
        print(f"{'#'*60}")

        sell_success_count = sum(1 for test in self.results["tests"] if test.get("sell_success"))
        total_tests = len(self.results["tests"])

        if sell_success_count == 0:
            verdict = "HONEYPOT (강한 의심)"
            risk_level = "HIGH"
            reason = "모든 매도 시도가 실패했습니다."
            color = "🔴"
        else:
            recovery_rates = [
                test.get("recovery_rate")
                for test in self.results["tests"]
                if test.get("sell_success") and test.get("recovery_rate") is not None
            ]
            recovery_rates = [rate for rate in recovery_rates if rate is not None]
            avg_recovery_rate = sum(recovery_rates) / len(recovery_rates) if recovery_rates else 0

            if avg_recovery_rate < THRESHOLDS["high_risk"]:
                verdict = "HONEYPOT (의심)"
                risk_level = "HIGH"
                reason = f"평균 실수령률이 {avg_recovery_rate * 100:.2f}%로 매우 낮습니다."
                color = "🟠"
            elif avg_recovery_rate < THRESHOLDS["medium_risk"]:
                verdict = "WARNING (주의)"
                risk_level = "MEDIUM"
                reason = f"평균 실수령률이 {avg_recovery_rate * 100:.2f}%로 낮은 편입니다."
                color = "🟡"
            else:
                verdict = "SAFE (정상)"
                risk_level = "LOW"
                reason = f"평균 실수령률이 {avg_recovery_rate * 100:.2f}%로 정상 범위입니다."
                color = "🟢"

        self.results["verdict"] = {
            "conclusion": verdict,
            "risk_level": risk_level,
            "reason": reason,
            "sell_success_rate": f"{sell_success_count}/{total_tests}"
        }

        print(f"{color} 판정: {verdict}")
        print(f"위험도: {risk_level}")
        print(f"사유: {reason}")
        print(f"매도 성공률: {sell_success_count}/{total_tests}")

        print(f"{'='*60}")
        print("개별 테스트 결과:")
        print(f"{'='*60}")
        for test in self.results["tests"]:
            name = test.get("test_name", "UNKNOWN")
            quote_symbol = test.get("quote_symbol", "QUOTE")
            print(f"[{name}]")
            if "tokens_target" in test:
                print(f"  목표 토큰: {test['tokens_target']:.6f}")
            if "tokens_received" in test:
                print(f"  수령 토큰: {test['tokens_received']:.6f}")
            if "quote_spent" in test:
                print(f"  사용 {quote_symbol}: {test['quote_spent']:.6f}")
            if "quote_received" in test:
                print(f"  회수 {quote_symbol}: {test['quote_received']:.6f}")
            print(f"  매수: {'✅성공' if test.get('buy_success') else '❌실패'}")
            print(f"  매도: {'✅성공' if test.get('sell_success') else '❌실패'}")
            if test.get("sell_success") and test.get("recovery_rate") is not None:
                print(f"  실수령률: {test['recovery_rate'] * 100:.2f}%")

        print(f"{'#'*60}")

    def save_results(self):
        """결과를 JSON 파일로 저장 (규격화된 형태)"""
        # buy_sell 판정 로직
        buy_result = []
        sell_result = []
        fail_type = []

        if self.results.get("tests"):
            for test in self.results["tests"]:
                tmp_buy = test["buy_success"]
                buy_result.append(tmp_buy)

                tmp_sell = test["sell_success"]
                sell_result.append(tmp_sell)

                tmp_fail = test["sell_fail"]
                fail_type.append(tmp_fail)

        # if self.results.get("tests"):
        #     # 모든 테스트에서 매수가 하나라도 성공했는지 확인
        #     any_buy_success = any(test["buy_success"] for test in self.results["tests"])

        #     if any_buy_success:
        #         buy_result = True
        #         # 매수 성공한 테스트들 중 매도 성공 여부 확인
        #         buy_succeeded_tests = [test for test in self.results["tests"] if test["buy_success"]]
        #         any_sell_success = any(test["sell_success"] for test in buy_succeeded_tests)
                
        #         if any_sell_success:
        #             sell_result = True  # 매수/매도 모두 성공

        #             # 매도 성공한 테스트들의 평균 실수령률 계산
        #             recovery_rates = [
        #                 test.get("recovery_rate")
        #                 for test in buy_succeeded_tests
        #                 if test.get("sell_success") and test.get("recovery_rate") is not None
        #             ]

        #             if recovery_rates:
        #                 avg_recovery_rate = sum(recovery_rates) / len(recovery_rates)
        #                 return_rate = round(avg_recovery_rate * 100, 2)  # 퍼센트로 변환
        #         else:
        #             fail_type = self.results["tests"][0]["sell_fail"]
        #             sell_result = False  # 매수는 가능하나 매도 불가
        #     else:
        #         buy_result = False  # 매수 실패
        #         sell_result = False

        # 시나리오 결과를 규격화된 형태로 변환
        def format_scenario_result(scenario_key):
            """시나리오 결과를 {result: bool, confidence: str} 형태로 변환"""
            scenario = self.results.get("scenarios", {}).get(scenario_key, {})
            return {
                "result": scenario.get("result") == "YES",
                "confidence": scenario.get("confidence", "LOW")
            }

        # 규격화된 결과 생성
        standardized_result = {
            "token_addr_idx": self.token_idx,
            "verified": self.is_verified,
            "buy": {
                "result": buy_result
            },
            "sell":{
                "result":sell_result,
                "fail_type": fail_type,
            },
            "blacklist_check": format_scenario_result("blacklist_whitelist"),
            "trading_suspend_check": format_scenario_result("trading_suspend"),
            "exterior_call_check": format_scenario_result("exterior_function_call"),
            "unlimited_mint": format_scenario_result("unlimited_mint"),
            "balance_manipulation": format_scenario_result("owner_bal_manipulation"),
            "tax_manipulation": format_scenario_result("owner_tax_manipulation"),
            "existing_holders_check": format_scenario_result("existing_holders_test"),
        }

        # 파일명 생성: {token_address}.json
        filename = f"{self.token_address}.json"

        # results 디렉토리 경로
        results_dir = Path(__file__).parent.parent / "results"
        results_dir.mkdir(exist_ok=True)

        filepath = results_dir / filename

        # JSON 저장
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(standardized_result, f, indent=2, ensure_ascii=False)

        return filepath


def get_validblock(pair_addr,alchemy,etherscan):
    param = {
        "chainid":1,
        "module": "logs",
        "action": "getLogs",
        "fromBlock": "0",
        "toBlock": "latest",
        "address": pair_addr,
        "topic0": "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
        "topic2": "0x0000000000000000000000000000000000000000000000000000000000000000",
        "apikey": etherscan
    }

    w3 = Web3(Web3.HTTPProvider(alchemy))
    time.sleep(0.1)
    res = requests.get('https://api.etherscan.io/v2/api',params=param)
    response = res.json()
    result = response['result']

    if len(result) < 2:
        valid_block = w3.eth.block_number

    else:
        # print(result[-1]['transactionHash'])
        valid_block = int(result[-1]['blockNumber'],16) - 1
    
    return valid_block


def main():
    load_dotenv()
    """메인 함수 - .env 기반 단일 토큰 검사"""
    network.rpc._revert_trace = False
    w3 = Web3(Web3.HTTPProvider("http://127.0.0.1:8545"))

    skip_flag = False
    if len(sys.argv) < 3:
        print("Please input 'token_information.csv', 'pair_evt.csv' file path")
        return
    else:
        token_info_csv = sys.argv[1]
        pair_evt_csv= sys.argv[2]
        holder_csv_path = sys.argv[3] if len(sys.argv) > 3 else None

    token_list_dict = parse_csv(token_info_csv,pair_evt_csv)
    print(f"\n{'='*60}")
    print(f"총 {len(token_list_dict)}개의 토큰 발견")
    print(f"{'='*60}\n")
    fork_url = os.environ.get("ALCHEMY_URL")
    etherscan = os.environ.get("ETHERSCAN_TOKEN")
    w3 = Web3(Web3.HTTPProvider(fork_url))
    try:
        for token_info in token_list_dict:
            # .env에서 토큰 주소, fork url 읽기
            service_input = token_info['pair_type']
            token_idx = token_info['token_addr_idx']
            pair_addr = token_info['pair_addr']
            block_number = w3.eth.block_number
            # block_number = get_validblock(pair_addr,fork_url,etherscan)              

            pair_creator = token_info['liquidity_provider']
            token_address = token_info['token_addr']
            # blocknum_list= []
            # blocknum_list.append(blocknum)

            # if blocknum != lastblocknum:
            #     blocknum_list.append(lastblocknum)
        
            # Validation: 필수 정보 체크
            if not token_address:
                print(f"\n{'='*60}")
                print(f"⚠️  토큰 #{token_idx}: 토큰 주소가 없어 스킵합니다.")
                print(f"{'='*60}\n")
                continue
    
            if block_number is None:
                print(f"\n{'='*60}")
                print(f"⚠️  토큰 #{token_idx} ({token_address})")
                print(f"⚠️  유동성 블록 번호를 찾을 수 없어 스킵합니다.")
                print(f"{'='*60}\n")
                continue
    
            if not fork_url:
                print(f"\n{'='*60}")
                print(f"⚠️  토큰 #{token_idx} ({token_address})")
                print(f"⚠️  WEB3_INFURA_PROJECT_ID가 설정되지 않아 스킵합니다.")
                print(f"{'='*60}\n")
                continue
            try:
                # for step in range(len(blocknum_list)):
                if network.is_connected():
                    print('network already')
                    network.disconnect()
                # fork to certain block number
                if network.rpc.is_active():
                    print("rpc already")
                    network.rpc.kill()
                
                for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
                    cmd = " ".join(proc.info['cmdline']).lower()
                    if "anvil" in cmd:
                        print(f"💀 Killing old RPC process: {proc.pid}")
                        proc.kill()

                network.rpc.launch(
                    cmd=f"anvil --fork-url={fork_url} --fork-block-number={block_number} --accounts=10 --hardfork=cancun --no-storage-caching"
                )

                time.sleep(2)
                if not network.is_connected():
                    network.connect("development")
                try:
                    wait_for_rpc_ready(timeout=60)
                except Exception as exc:
                    print(f"[warn] RPC readiness check failed: {exc}")
                # Expand HTTP RPC timeout to survive heavy traces
                set_rpc_timeout(seconds=120)
                print("Anvil accounts:", web3.eth.accounts)

                print(f"\n{'='*60}")
                print(f"현재 네트워크: {network.show_active()}")
                print(f"블록 번호: {chain.height}")
                print(f"인덱스 번호: {token_idx}")
                print(f"{'='*60}")

                # gas configuration
                gas_price("1000 gwei")

                # run analyzer
                print(f"\n{'#'*60}")
                print(f"# 검사 시작: {token_address}")
                print(f"{'#'*60}")
                # if step == 1:
                #     detector = ScamAnalyzer(token_address, token_idx, service_input, pair_creator,results)
                #     results = detector.run_tests(step)
                # else:
                detector = ScamAnalyzer(
                    token_address,
                    token_idx,
                    service_input,
                    block_number,
                    pair_addr,
                    pair_creator,
                    holder_csv_path=holder_csv_path,
                )
                results = detector.run_tests()

                skip_reason = results.get("error") or results.get("liquidity_error")
                if skip_reason:
                    skip_flag = True
                    print(f"[SKIP] 분석 중 오류 발생으로 결과 저장을 건너뜁니다.")
                        # break

            except Exception as e:
                skip_flag = False
                print(f"\n{'='*60}")
                print(f"❌ 토큰 #{token_idx} ({token_address}) 처리 중 오류 발생")
                print(f"❌ 오류: {str(e)}")
                print(f"{'='*60}\n")
            
            if skip_flag == True:
                skip_flag = False
                continue
            # save results
            filepath = detector.save_results()
            print(f"📄 상세 결과: {filepath}")
            print(f"{'='*60}\n")

    finally:
        # 정상/예외 상관없이 항상 RPC 정리
        try:
            if network.is_connected():
                network.disconnect()
        except Exception as e:
            print(f"⚠️  네트워크 연결 해제 중 오류: {e}")

        try:
            if network.rpc.is_active():
                network.rpc.kill()
        except Exception as e:
            print(f"⚠️  RPC 종료 중 오류: {e}")

        time.sleep(1)
if __name__ == "__main__":
    main()
