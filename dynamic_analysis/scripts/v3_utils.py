"""
Uniswap V3 specific utilities for pool discovery and liquidity analysis
"""

import json
from brownie import Contract

V3_FACTORY_ADDRESS = "0x1F98431c8aD98523631AE4a59f267346ea31F984"
V3_SWAP_ROUTER_ADDRESS = "0xE592427A0AEce92De3Edee1F18E0157C05861564"
V3_QUOTER_V2_ADDRESS = "0x61fFE014bA17989E743c5F6cB21bF9697530B21e"

V3_FEE_TIERS = [100, 500, 3000, 10000]  # 0.01%, 0.05%, 0.3%, 1%

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

V3_FACTORY_ABI_JSON = "interfaces/IUniswapV3Factory.json"
V3_POOL_ABI_JSON = "interfaces/IUniswapV3Pool.json"
V3_SWAP_ROUTER_ABI_JSON = "interfaces/IUniswapV3SwapRouter.json"
V3_QUOTER_ABI_JSON = "interfaces/IQuoterV2.json"


def load_v3_contracts():
    """Load V3 contract ABIs"""
    with open(V3_FACTORY_ABI_JSON, 'r') as f:
        factory_abi = json.load(f)
    with open(V3_POOL_ABI_JSON, 'r') as f:
        pool_abi = json.load(f)
    with open(V3_SWAP_ROUTER_ABI_JSON, 'r') as f:
        router_abi = json.load(f)
    with open(V3_QUOTER_ABI_JSON, 'r') as f:
        quoter_abi = json.load(f)

    return {
        'factory_abi': factory_abi,
        'pool_abi': pool_abi,
        'router_abi': router_abi,
        'quoter_abi': quoter_abi
    }


def get_v3_factory():
    """Get V3 factory contract instance"""
    abis = load_v3_contracts()
    return Contract.from_abi("UniswapV3Factory", V3_FACTORY_ADDRESS, abis['factory_abi'])


def get_v3_router():
    """Get V3 swap router contract instance"""
    abis = load_v3_contracts()
    return Contract.from_abi("SwapRouter", V3_SWAP_ROUTER_ADDRESS, abis['router_abi'])


def get_v3_quoter():
    """Get V3 quoter contract instance"""
    abis = load_v3_contracts()
    return Contract.from_abi("QuoterV2", V3_QUOTER_V2_ADDRESS, abis['quoter_abi'])


def find_v3_pools(token_address, quote_tokens, factory_abi, pool_abi):
    """
    Find all V3 pools for given token across all quote tokens and fee tiers

    Args:
        token_address: Target token address
        quote_tokens: Dict of {symbol: address} for quote tokens
        factory_abi: V3 factory ABI
        pool_abi: V3 pool ABI

    Returns:
        List of dicts containing pool info sorted by liquidity (descending)
    """
    factory = Contract.from_abi("UniswapV3Factory", V3_FACTORY_ADDRESS, factory_abi)
    pools = []

    for quote_symbol, quote_addr in quote_tokens.items():
        for fee in V3_FEE_TIERS:
            try:
                pool_addr = factory.getPool(quote_addr, token_address, fee)

                if pool_addr == ZERO_ADDRESS or pool_addr is None:
                    continue

                pool = Contract.from_abi("IUniswapV3Pool", pool_addr, pool_abi)

                slot0 = pool.slot0()
                sqrt_price_x96 = slot0[0]
                current_tick = slot0[1]

                liquidity = pool.liquidity()

                if liquidity == 0:
                    continue

                price = (sqrt_price_x96 / (2 ** 96)) ** 2

                token0 = pool.token0().lower()
                is_token0 = token0 == token_address.lower()

                pools.append({
                    'pool_address': pool_addr,
                    'quote_symbol': quote_symbol,
                    'quote_address': quote_addr,
                    'fee_tier': fee,
                    'liquidity': liquidity,
                    'sqrt_price_x96': sqrt_price_x96,
                    'current_tick': current_tick,
                    'price': price,
                    'is_token0': is_token0,
                    'dex_version': 'v3'
                })

            except Exception:
                continue

    pools.sort(key=lambda x: x['liquidity'], reverse=True)
    return pools


def calculate_v3_quote_value(liquidity, sqrt_price_x96, is_token0, quote_decimals):
    """
    Estimate quote token value in V3 pool
    Note: This is a rough approximation as V3 liquidity is concentrated

    Args:
        liquidity: Current liquidity at active tick
        sqrt_price_x96: Square root price in Q64.96 format
        is_token0: Whether target token is token0
        quote_decimals: Quote token decimals

    Returns:
        Estimated quote value (for comparison purposes)
    """
    if liquidity == 0 or sqrt_price_x96 == 0:
        return 0

    price = (sqrt_price_x96 / (2 ** 96)) ** 2

    if is_token0:
        quote_value = liquidity * price / (2 ** 96)
    else:
        quote_value = liquidity / price / (2 ** 96)

    return int(quote_value * (10 ** quote_decimals))


def get_pool_info(pool_address, pool_abi):
    """
    Get detailed pool information

    Args:
        pool_address: Pool contract address
        pool_abi: Pool ABI

    Returns:
        Dict with pool details
    """
    pool = Contract.from_abi("IUniswapV3Pool", pool_address, pool_abi)

    slot0 = pool.slot0()
    liquidity = pool.liquidity()
    token0 = pool.token0()
    token1 = pool.token1()
    fee = pool.fee()

    return {
        'pool_address': pool_address,
        'token0': token0,
        'token1': token1,
        'fee': fee,
        'liquidity': liquidity,
        'sqrt_price_x96': slot0[0],
        'tick': slot0[1],
        'observation_index': slot0[2],
        'observation_cardinality': slot0[3],
    }
