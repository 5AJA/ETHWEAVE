import ast
import csv
from pathlib import Path
from typing import Dict, Iterable, List


ZERO_ADDRESS = "0x" + "0" * 40


def load_token_info(rows: Iterable[dict[str, str]]) -> Dict[int, dict[str, str]]:
    """Build a dictionary keyed by token index from the token CSV rows."""
    token_map: Dict[int, dict[str, str]] = {}
    for row in rows:
        idx = int(row["token_addr_idx"])
        token_map[idx] = {
            "token_addr_idx": idx,
            "token_addr": row["token_addr"],
            "pair_addr": row["pair_addr"],
            "pair_type": row["pair_type"],
        }
    return token_map


def find_liquidity_blocks(rows: Iterable[dict[str, str]]) -> tuple[Dict[int, int], Dict[int, str], Dict[int, int]]:
    """Find the first liquidity-supply block, liquidity provider, and latest event block per token."""
    liquidity_blocks: Dict[int, int] = {}
    liquidity_provider: Dict[int, str] = {}
    pair_created_flags: Dict[int, bool] = {}
    pair_created_blocks: Dict[int, int] = {}
    last_event_blocks: Dict[int, int] = {}

    for row in rows:
        idx = int(row["token_addr_idx"])

        if row.get("evt_removed", "").upper() == "TRUE":
            continue

        raw_block_number = row.get("block_number", "")
        try:
            block_number = int(raw_block_number)
        except (TypeError, ValueError):
            try:
                block_number = int(float(raw_block_number))
            except (TypeError, ValueError):
                block_number = None

        if block_number is not None:
            existing_block = last_event_blocks.get(idx)
            if existing_block is None or block_number > existing_block:
                last_event_blocks[idx] = block_number

        event_type = row.get("evt_type", "")

        if event_type == "PairCreated":
            liquidity_provider[idx] = row.get("tx_from", "")
            pair_created_flags[idx] = True
            if block_number is not None:
                pair_created_blocks[idx] = block_number
            continue

        if not pair_created_flags.get(idx) or idx in liquidity_blocks:
            continue

        if event_type != "Transfer":
            continue

        log_data = ast.literal_eval(row.get("evt_log", "{}"))
        from_addr = (log_data.get("from") or "").lower()

        if from_addr == ZERO_ADDRESS:
            if block_number is None:
                raise ValueError(f"Invalid block_number value: {raw_block_number!r}")
            liquidity_blocks[idx] = block_number

    # If no liquidity block found for a token, use PairCreated block
    for idx in pair_created_flags.keys():
        if idx not in liquidity_blocks and idx in pair_created_blocks:
            liquidity_blocks[idx] = pair_created_blocks[idx]

    return liquidity_blocks, last_event_blocks, liquidity_provider


def parse_csv(token_csv_path: str, pair_evt_csv_path: str) -> List[dict[str, object]]:
    """Parse both CSV files and return a list of token dictionaries."""
    token_path = Path(token_csv_path)
    pair_evt_path = Path(pair_evt_csv_path)

    with token_path.open("r", newline="", encoding="utf-8") as token_file:
        token_reader = csv.DictReader(token_file)
        required_token_fields = {"token_addr_idx", "token_addr", "pair_addr", "pair_type"}
        if not required_token_fields.issubset(token_reader.fieldnames or []):
            missing = required_token_fields - set(token_reader.fieldnames or [])
            raise ValueError(f"Missing fields in token_information.csv: {', '.join(sorted(missing))}")
        token_data = load_token_info(token_reader)

    with pair_evt_path.open("r", newline="", encoding="utf-8") as evt_file:
        evt_reader = csv.DictReader(evt_file)
        required_evt_fields = {"token_addr_idx", "tx_from", "evt_type", "evt_log", "block_number"}
        if not required_evt_fields.issubset(evt_reader.fieldnames or []):
            missing = required_evt_fields - set(evt_reader.fieldnames or [])
            raise ValueError(f"Missing fields in pair_evt.csv: {', '.join(sorted(missing))}")
        liquidity_blocks, last_event_blocks, liquidity_provider = find_liquidity_blocks(evt_reader)

    result: List[dict[str, object]] = []
    for idx in sorted(token_data.keys()):
        token_info = token_data[idx].copy()
        token_info["liquidity_block"] = liquidity_blocks.get(idx)
        token_info["last_event_block"] = last_event_blocks.get(idx)
        token_info["liquidity_provider"] = liquidity_provider.get(idx)
        result.append(token_info)

    return result
