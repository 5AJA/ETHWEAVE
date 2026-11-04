# Dynamic Analysis

Brownie-based token trading simulations on a local Ethereum mainnet fork.

## Setup

Install Anvil (Foundry) and make the `anvil` executable available on `PATH`. From this directory, install the Python dependencies:

```sh
pip install -r requirements.txt requests psutil
```

Create a `.env` file in this directory:

```dotenv
ALCHEMY_URL=https://eth-mainnet.g.alchemy.com/v2/YOUR_API_KEY
ETHERSCAN_TOKEN=YOUR_ETHERSCAN_API_KEY
```

`ALCHEMY_URL` is the Ethereum mainnet RPC endpoint used for the fork. `ETHERSCAN_TOKEN` is used to retrieve contract ABIs.

## Run

Run from this directory so the analyzer can locate `interfaces/`:

```sh
python scripts/scam_analyzer.py ../dataset/token_information.csv ../dataset/pair_evt.csv ../dataset/holders.csv
```

The arguments are the token-information CSV, pair-event CSV, and optional holder CSV, in that order. Include the holder CSV to run existing-holder sell checks. Token addresses and router types are read from the token-information CSV; each token is analyzed using the latest mainnet block available when its analysis starts.

The token-information CSV requires `token_addr_idx`, `token_addr`, `pair_addr`, and `pair_type`. The pair-event CSV requires `token_addr_idx`, `tx_from`, `evt_type`, `evt_log`, and `block_number`.

The supported `pair_type` values are `UniswapV2`, `UniswapV2_Old`, `SusiswapV2`, `ShibaswapV1`, `PancakeV2`, `Fraxwap`, and `Whiteswap`. Use these exact spellings, including `SusiswapV2` and `Fraxwap`.

## Output

The analyzer prints scenario results and saves a JSON record for each analyzed token in `results/`. Simulated transactions run on the local fork.
