"""Generate corrected event, holder, and dynamic features in a streaming pass."""

from __future__ import annotations

import argparse
import ast
import json
from io import StringIO
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd


FEATURES = ['imbalance_rate', 'liquidity_event_mask', 'max_sell_share', 'inactive_token_flag', 'rel_swap_event_share', 'rel_swap_block_share', 'rel_buy_tx_share', 'rel_sell_tx_share', 'rel_owner_sell_tx_share', 'rel_non_owner_sell_tx_share', 'rel_burn_event_share', 'rel_mint_event_share', 'rel_unique_sellers_per_sell', 'rel_unique_buyers_per_buy', 'rel_sell_run_share', 'rel_owner_sell_volume_share', 'rel_buy_volume_share', 'dynamic_json_available', 'abi_verified', 'existing_holders_check', 'exterior_call_check', 'tax_manipulation', 'trading_suspend_check', 'unlimited_mint', 'dynamic_buy_success_count', 'dynamic_paired_honeypot_count']

def add_aggregates(frame: pd.DataFrame) -> pd.DataFrame:
    """Collapse three buy, three sell, and three failure-type outputs.

    Failure type categories are deliberately ignored: sell success already
    represents success/failure, while the category semantics are not yet
    documented. Aggregate results stay missing only when the Basic analysis
    did not execute or its six result entries are incomplete; ABI verification
    is not required for these pre-gate scenarios.
    """
    result = frame.copy()
    buy_cols = [f"buy_success_{idx}" for idx in range(1, 4)]
    sell_cols = [f"sell_success_{idx}" for idx in range(1, 4)]
    if "basic_analysis_executed" in result:
        executed = result["basic_analysis_executed"].eq(1)
    else:
        executed = result["dynamic_json_available"].eq(1)
    complete = executed & result[buy_cols + sell_cols].notna().all(axis=1)

    buy_count = result[buy_cols].sum(axis=1, min_count=3)
    sell_count = result[sell_cols].sum(axis=1, min_count=3)
    paired_count = sum(
        (result[buy].eq(1) & result[sell].eq(0)).astype(int)
        for buy, sell in zip(buy_cols, sell_cols)
    )

    values: dict[str, pd.Series] = {
        "dynamic_buy_success_count": buy_count,
        "dynamic_sell_success_count": sell_count,
        "dynamic_sell_failure_count": 3 - sell_count,
        "dynamic_paired_honeypot_count": paired_count.astype(float),
        "dynamic_buy_any_success": buy_count.gt(0).astype(float),
        "dynamic_sell_any_success": sell_count.gt(0).astype(float),
        "dynamic_buy_overall_success": buy_count.eq(3).astype(float),
        "dynamic_sell_overall_success": sell_count.eq(3).astype(float),
        "dynamic_any_sell_failure": sell_count.lt(3).astype(float),
        "dynamic_buy_success_sell_failure": (
            buy_count.eq(3) & sell_count.lt(3)
        ).astype(float),
    }
    for name, value in values.items():
        result[name] = value.where(complete, np.nan)
    return result

def safe_ratio(numerator: pd.Series, denominator: pd.Series) -> pd.Series:
    denominator = denominator.astype(float)
    return numerator.astype(float).div(denominator.where(denominator.ne(0)))

def add_relative_event_features(frame: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """Create scale-reduced ratios only from already available Event fields."""
    frame = frame.copy()
    definitions = {
        "rel_swap_event_share": ("swap_event_count", "event_count"),
        "rel_swap_block_share": ("swap_block_count", "event_block_count"),
        "rel_buy_tx_share": ("total_buy_cnt", "swap_event_count"),
        "rel_sell_tx_share": ("total_sell_cnt", "swap_event_count"),
        "rel_owner_sell_tx_share": ("owner_sell_tx_count", "total_sell_cnt"),
        "rel_non_owner_sell_tx_share": ("non_owner_sell_tx_count", "total_sell_cnt"),
        "rel_burn_event_share": ("total_burn_events", "event_count"),
        "rel_mint_event_share": ("total_mint_events", "event_count"),
        "rel_unique_sellers_per_sell": ("unique_sellers", "total_sell_cnt"),
        "rel_unique_buyers_per_buy": ("unique_buyers", "total_buy_cnt"),
        "rel_sell_run_share": ("max_consecutive_sell_run", "total_sell_cnt"),
        "rel_owner_sell_volume_share": ("total_owner_sell_vol", "total_sell_vol"),
    }
    for output, (numerator, denominator) in definitions.items():
        frame[output] = safe_ratio(frame[numerator], frame[denominator])
    volume_total = frame["total_buy_vol"].astype(float) + frame["total_sell_vol"].astype(float)
    frame["rel_buy_volume_share"] = safe_ratio(frame["total_buy_vol"], volume_total)
    names = list(definitions) + ["rel_buy_volume_share"]
    return frame, names

def fit_preprocessor(frame: pd.DataFrame, mode: str) -> dict[str, object]:
    numeric = frame.replace([np.inf, -np.inf], np.nan)
    artifact: dict[str, object] = {"mode": mode, "features": list(frame.columns)}
    if mode == "native_missing":
        return artifact
    artifact["medians"] = numeric.median(numeric_only=True).fillna(0.0).to_dict()
    if mode == "winsor_0001":
        artifact["lower"] = numeric.quantile(0.001).fillna(-1e30).to_dict()
        artifact["upper"] = numeric.quantile(0.999).fillna(1e30).to_dict()
    elif mode == "winsor_01":
        artifact["lower"] = numeric.quantile(0.01).fillna(-1e30).to_dict()
        artifact["upper"] = numeric.quantile(0.99).fillna(1e30).to_dict()
    elif mode != "median_no_clip":
        raise ValueError(mode)
    return artifact

def transform(frame: pd.DataFrame, artifact: dict[str, object]) -> pd.DataFrame:
    features = list(artifact["features"])
    result = frame[features].replace([np.inf, -np.inf], np.nan).copy()
    for name in features:
        result[name] = pd.to_numeric(result[name], errors="coerce")
    if artifact["mode"] == "native_missing":
        return result
    for name in features:
        result[name] = result[name].fillna(float(artifact["medians"][name]))
        if "lower" in artifact:
            result[name] = result[name].clip(
                float(artifact["lower"][name]), float(artifact["upper"][name])
            )
    return result


def save_features(measurements: pd.DataFrame, output: Path) -> pd.DataFrame:
    # Preserve the CSV parsing used between the original research stages,
    # without writing an intermediate data file.
    frame = pd.read_csv(StringIO(measurements.to_csv(index=False)))
    frame, _ = add_relative_event_features(add_aggregates(frame))
    result = frame[['token_addr_idx', 'token_addr', 'label', *FEATURES]]
    output.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(output, index=False)
    return result


def norm(value: object) -> str:
    return "" if pd.isna(value) else str(value).strip().lower()


def parse_log(value: object) -> dict:
    if not isinstance(value, str) or not value:
        return {}
    try:
        result = ast.literal_eval(value)
        return result if isinstance(result, dict) else {}
    except (SyntaxError, ValueError, TypeError):
        return {}


def pair_created_signature(row: object, pair_idx: int, payload: dict | None = None) -> tuple:
    """Read projected token addresses, retaining compatibility with evt_log.

    Projection is storage metadata, not an additional model feature. If both
    representations exist they must agree; silently preferring either could
    reverse the canonical-pair decision and therefore Swap direction counts.
    """
    if pair_idx not in (0, 1):
        raise ValueError(f"Invalid pair_idx: {pair_idx}")
    payload = parse_log(getattr(row, "evt_log", "")) if payload is None else payload
    addresses = []
    for name in ("token0", "token1"):
        projected = norm(getattr(row, name, ""))
        embedded = norm(payload.get(name, ""))
        if projected and embedded and projected != embedded:
            raise ValueError(
                f"Conflicting {name} in PairCreated projection/evt_log "
                f"for token {getattr(row, 'token_addr_idx', '?')}"
            )
        addresses.append(projected or embedded)
    return (addresses[pair_idx], norm(payload.get("pairaddr", "")),
            norm(row.tx_hash), int(row.evt_idx))


def gini(values: np.ndarray) -> float:
    values = values[np.isfinite(values) & (values >= 0)]
    total = values.sum()
    if len(values) == 0 or total <= 0:
        return 0.0
    ordered = np.sort(values)
    n = len(ordered)
    return float((2 * np.sum(np.arange(1, n + 1) * ordered) / (n * total)) - ((n + 1) / n))


def new_event_state() -> dict[str, object]:
    return {
        "canonical_seen": False,
        "active": False,
        "canonical_signature": None,
        "last_order": None,
        "order_violations": 0,
        "parse_failures": 0,
        "excluded_noncanonical_rows": 0,
        "event_count": 0,
        "event_block_count": 0,
        "last_event_block": None,
        "swap_count": 0,
        "swap_block_count": 0,
        "last_swap_block": None,
        "mint_count": 0,
        "burn_count": 0,
        "buy_count": 0,
        "sell_count": 0,
        "owner_buy_count": 0,
        "owner_sell_count": 0,
        "buy_volume": 0.0,
        "sell_volume": 0.0,
        "owner_sell_volume": 0.0,
        "max_sell_amount": 0.0,
        "buyers": set(),
        "sellers": set(),
        "current_sell_run": 0,
        "max_consecutive_sell_run": 0,
        "ambiguous_swap_count": 0,
    }


def record_buy_address(state: dict, row: object, address_field: str = "tx_from") -> str:
    """Count distinct transaction initiators, not router destinations.

    tx_from is an initiator proxy, not a guarantee of the token recipient or
    beneficial owner. tx_to is available only for historical reproduction.
    """
    if address_field not in {"tx_from", "tx_to"}:
        raise ValueError(f"Unsupported buyer address field: {address_field}")
    address = norm(getattr(row, address_field))
    if address:
        state["buyers"].add(address)
    return address


def load_dynamic(raw: Path, directories: list[Path] | None = None) -> dict[str, dict[str, object]]:
    records: dict[str, dict[str, object]] = {}
    for directory in directories if directories is not None else (raw / "dynamic_results", raw / "dynamic", raw / "dynamic_honeypot", raw / "dynamic_normal"):
        if directories is not None and not directory.is_dir():
            raise ValueError(f'Dynamic directory does not exist: {directory}')
        for path in directory.glob("*.json"):
            # Analyzer run directories also hold manifests and runtime metadata.
            if directories is not None and not (len(path.stem) == 42 and path.stem.lower().startswith('0x')):
                continue
            payload = json.loads(path.read_text(encoding="utf-8"))
            address = path.stem.lower()
            if address in records:
                raise ValueError(f"Duplicate dynamic JSON token address: {address}")
            payload["_address"] = address
            records[address] = payload
    return records


def dynamic_features(
    payload: dict[str, object] | None,
    expected_address: str,
    holder_input_available: bool = False,
) -> dict[str, object]:
    result: dict[str, object] = {
        "dynamic_json_available": int(payload is not None),
        "abi_verified": np.nan,
        "dynamic_analysis_executed": 0,
        "basic_analysis_executed": 0,
        "abi_analysis_executed": 0,
    }
    scalar_names = [
        "balance_manipulation", "existing_holders_check", "exterior_call_check",
        "tax_manipulation", "trading_suspend_check", "unlimited_mint",
        "blacklist_check_raw",
    ]
    for name in scalar_names:
        result[name] = np.nan
    for scenario in range(1, 4):
        result[f"buy_success_{scenario}"] = np.nan
        result[f"sell_success_{scenario}"] = np.nan
        for category in range(5):
            result[f"sell_fail_type_{scenario}_{category}"] = np.nan

    if payload is None:
        return result
    if payload["_address"] != expected_address:
        raise ValueError(f"Dynamic JSON address mismatch for token {payload['token_addr_idx']}")
    verified = bool(payload.get("verified", False))
    result["abi_verified"] = int(verified) if payload.get('verified') is not None else np.nan
    buys = payload.get("buy", {}).get("result", [])
    sells = payload.get("sell", {}).get("result", [])
    fail_types = payload.get("sell", {}).get("fail_type", [])
    basic_executed = isinstance(buys, list) and isinstance(sells, list) and bool(buys or sells)
    if 'basic_swap_test' in payload.get('scenarios_executed', {}):
        basic_executed = payload['scenarios_executed']['basic_swap_test'] is True
    result["basic_analysis_executed"] = int(basic_executed)
    # Backward-compatible name: Basic execution, not ABI-dependent execution.
    result["dynamic_analysis_executed"] = int(basic_executed)
    result["abi_analysis_executed"] = int(verified)
    for scenario in range(1, 4):
        index = scenario - 1
        def measured(side, values):
            section = payload.get(side, {})
            flags = section.get('measured', section.get('executed'))
            if isinstance(flags, list) and (index >= len(flags) or flags[index] is not True):
                return False
            return (index < len(values) and type(values[index]) in (bool, int)
                    and values[index] in (0, 1))
        if measured('buy', buys):
            result[f"buy_success_{scenario}"] = int(bool(buys[scenario - 1]))
        if measured('sell', sells):
            result[f"sell_success_{scenario}"] = int(bool(sells[scenario - 1]))
        if measured('sell', sells) and scenario <= len(fail_types) and fail_types[scenario - 1] is not None:
            category = int(fail_types[scenario - 1])
            if category not in range(5):
                raise ValueError(f"Unknown fail type {category} for token {payload['token_addr_idx']}")
            for candidate in range(5):
                result[f"sell_fail_type_{scenario}_{candidate}"] = int(candidate == category)

    # Existing Holders runs before the ABI gate. Its serializer maps UNKNOWN
    # and NO to false, but the implementation assigns HIGH only after at least
    # one holder was actually tested. LOW therefore remains missing.
    existing = payload.get("existing_holders_check")
    explicit_existing = (isinstance(existing, dict) and
                         'executed' in existing and 'measured' in existing)
    if (
        isinstance(existing, dict)
        and "result" in existing
        and ((explicit_existing and existing['executed'] is True and existing['measured'] is True)
             or (not explicit_existing and holder_input_available
                 and str(existing.get("confidence", "")).upper() == "HIGH"))
    ):
        result["existing_holders_check"] = int(bool(existing["result"]))

    # All remaining scenarios occur after the verified-ABI gate.
    if not verified:
        return result
    for name in scalar_names:
        if name == "existing_holders_check":
            continue
        source_name = "blacklist_check" if name == "blacklist_check_raw" else name
        scenario_payload = payload.get(source_name)
        if (isinstance(scenario_payload, dict) and "result" in scenario_payload
                and scenario_payload.get('executed', True) is True
                and scenario_payload.get('measured', True) is True):
            result[name] = int(bool(scenario_payload["result"]))
    return result


def holder_features(group: pd.DataFrame, reported_count: object = None) -> dict[str, object]:
    """Legacy diagnostic summaries, never part of the final 26 features.

    The optional second argument is accepted for older callers only, and is
    ignored: holder_cnt was removed from the collection schema. Counts below
    describe observed holder candidates, not a reported total population.
    """
    shares = pd.to_numeric(group["rel_to_total"], errors="coerce").to_numpy(dtype=float)
    shares = shares[np.isfinite(shares) & (shares >= 0)]
    observed = len(shares)
    total_share = float(shares.sum())
    whale = shares >= 0.05
    whale_share = float(shares[whale].sum())
    small_share = float(shares[~whale].sum())
    mean = float(shares.mean()) if observed else 0.0
    std = float(shares.std()) if observed else 0.0
    reported = observed
    return {
        "holder_data_available": int(observed > 0),
        "holder_count_observed": observed,
        "total_holders_reported": reported,
        "holder_share_observed_total": total_share,
        "holder_share_coverage_gap": max(0.0, 1.0 - total_share),
        "gini_coefficient": gini(shares),
        "hhi_index": float(np.square(shares).sum() * 10000.0),
        "whale_count": int(whale.sum()),
        "whale_total_fraction": whale_share,
        "small_holder_total_fraction": small_share,
        "holder_balance_std": std,
        "holder_balance_cv": std / mean if mean > 0 else 0.0,
        "whale_presence_flag": int(whale.any()),
        "few_holders_flag": int(reported <= 3),
    }


def fake_lp_mask(features: pd.DataFrame, records: list[dict]) -> pd.Series:
    """An already cleaned cohort legitimately has an empty exclusion list."""
    fake = pd.DataFrame(records, columns=["token_addr_idx", "token_addr"])
    fake_ids = set(fake["token_addr_idx"].astype(int))
    fake_addresses = set(fake["token_addr"].map(norm))
    return features["token_addr_idx"].isin(fake_ids) | features["token_addr"].map(norm).isin(fake_addresses)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", dest="raw", metavar="DIRECTORY", type=Path, default=Path(__file__).resolve().parents[2] / "dataset")
    ap.add_argument('--dynamic-dir', type=Path, action='append',
                    help='Directory containing analyzer result JSONs; repeatable')
    ap.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[2] / "dataset/features/features.csv")
    ap.add_argument("--chunksize", type=int, default=200_000)
    ap.add_argument("--buyer-address-field", choices=["tx_from", "tx_to"], default="tx_from",
                    help="tx_from matches the released model; tx_to is available for legacy reproduction.")
    args = ap.parse_args()

    token = pd.read_csv(args.raw / "token_information.csv")
    token["token_addr_idx"] = token["token_addr_idx"].astype(int)
    token["token_addr_norm"] = token["token_addr"].map(norm)
    token["token_creator_norm"] = token["token_creator"].map(norm)
    token["pair_addr_norm"] = token["pair_addr"].map(norm)
    token = token.set_index("token_addr_idx", drop=False)
    labels = pd.read_csv(args.raw / "token_labels.csv").set_index("token_addr_idx")
    target_ids = set(labels.index.astype(int))
    states = {idx: new_event_state() for idx in target_ids}

    for chunk_number, chunk in enumerate(pd.read_csv(args.raw / "pair_evt.csv", chunksize=args.chunksize, low_memory=False), 1):
        ids = pd.to_numeric(chunk["token_addr_idx"], errors="coerce")
        chunk = chunk.loc[ids.isin(target_ids)].copy()
        if chunk.empty:
            continue
        chunk["token_addr_idx"] = ids.loc[chunk.index].astype(int)
        for row in chunk.itertuples(index=False):
            idx = row.token_addr_idx
            info = token.loc[idx]
            state = states[idx]
            event_type = row.evt_type
            payload = parse_log(row.evt_log) if event_type in {"PairCreated", "Swap"} else {}

            if event_type == "PairCreated":
                pair_idx = int(info["pair_idx"])
                signature = pair_created_signature(row, pair_idx, payload)
                canonical = signature[0] == info["token_addr_norm"] and signature[1] == info["pair_addr_norm"]
                if canonical:
                    if state["canonical_signature"] == signature:
                        continue
                    state["canonical_seen"] = True
                    state["active"] = True
                    state["canonical_signature"] = signature
                else:
                    if state["canonical_seen"]:
                        state["active"] = False
                    state["excluded_noncanonical_rows"] += 1
                    continue
            elif not state["active"]:
                state["excluded_noncanonical_rows"] += 1
                continue

            order = (int(row.block_number), int(row.evt_idx))
            if state["last_order"] is not None and order < state["last_order"]:
                state["order_violations"] += 1
            state["last_order"] = order
            state["event_count"] += 1
            if row.block_number != state["last_event_block"]:
                state["event_block_count"] += 1
                state["last_event_block"] = row.block_number
            if event_type == "Mint":
                state["mint_count"] += 1
            elif event_type == "Burn":
                state["burn_count"] += 1
            elif event_type == "Swap":
                if not payload:
                    state["parse_failures"] += 1
                    continue
                pair_idx = int(info["pair_idx"])
                amount_in = float(payload.get(f"amount{pair_idx}In", 0) or 0)
                amount_out = float(payload.get(f"amount{pair_idx}Out", 0) or 0)
                state["swap_count"] += 1
                if row.block_number != state["last_swap_block"]:
                    state["swap_block_count"] += 1
                    state["last_swap_block"] = row.block_number
                if amount_in > 0 and amount_out > 0:
                    state["ambiguous_swap_count"] += 1
                    state["current_sell_run"] = 0
                    continue
                if amount_in > 0:
                    state["sell_count"] += 1
                    state["sell_volume"] += amount_in
                    state["max_sell_amount"] = max(state["max_sell_amount"], amount_in)
                    seller = norm(row.tx_from)
                    if seller:
                        state["sellers"].add(seller)
                    if seller == info["token_creator_norm"]:
                        state["owner_sell_count"] += 1
                        state["owner_sell_volume"] += amount_in
                    state["current_sell_run"] += 1
                    state["max_consecutive_sell_run"] = max(state["max_consecutive_sell_run"], state["current_sell_run"])
                elif amount_out > 0:
                    state["buy_count"] += 1
                    state["buy_volume"] += amount_out
                    record_buy_address(state, row, args.buyer_address_field)
                    # Keep this OTHER feature unchanged to isolate the unique
                    # buyer-count experiment. Its tx_to definition is legacy.
                    if norm(row.tx_to) == info["token_creator_norm"]:
                        state["owner_buy_count"] += 1
                    state["current_sell_run"] = 0
        print(f"processed pair-event chunk {chunk_number}")

    holders = pd.read_csv(args.raw / "holders.csv")
    holders["token_addr_norm"] = holders["token_address"].map(norm)
    holder_groups = {key: group for key, group in holders.groupby("token_addr_norm", sort=False)}
    dynamic = load_dynamic(args.raw, args.dynamic_dir)
    output: list[dict[str, object]] = []
    for idx in sorted(target_ids):
        info = token.loc[idx]
        state = states[idx]
        if not state["canonical_seen"]:
            raise ValueError(f"No canonical PairCreated event for token {idx}")
        buys = int(state["buy_count"])
        sells = int(state["sell_count"])
        sell_volume = float(state["sell_volume"])
        row: dict[str, object] = {
            "token_addr_idx": idx,
            "token_addr": info["token_addr"],
            "label": int(labels.loc[idx, "label"]),
            "pair_idx": int(info["pair_idx"]),
            "event_count": state["event_count"],
            "event_block_count": state["event_block_count"],
            "swap_event_count": state["swap_count"],
            "swap_block_count": state["swap_block_count"],
            "total_buy_cnt": buys,
            "total_sell_cnt": sells,
            "owner_buy_tx_count": state["owner_buy_count"],
            "owner_sell_tx_count": state["owner_sell_count"],
            "non_owner_sell_tx_count": sells - int(state["owner_sell_count"]),
            "imbalance_rate": (buys - sells) / (buys + sells) if buys + sells else 0.0,
            "total_burn_events": state["burn_count"],
            "total_mint_events": state["mint_count"],
            "total_sell_vol": sell_volume,
            "total_buy_vol": state["buy_volume"],
            "total_owner_sell_vol": state["owner_sell_volume"],
            "total_sell_vol_log": np.log1p(sell_volume),
            "total_buy_vol_log": np.log1p(float(state["buy_volume"])),
            "total_owner_sell_vol_log": np.log1p(float(state["owner_sell_volume"])),
            "liquidity_event_mask": int(int(state["mint_count"]) + int(state["burn_count"]) > 0),
            "max_sell_share": float(state["max_sell_amount"]) / sell_volume if sell_volume > 0 else 0.0,
            "unique_sellers": len(state["sellers"]),
            "unique_buyers": len(state["buyers"]),
            "max_consecutive_sell_run": state["max_consecutive_sell_run"],
            "inactive_token_flag": int(buys + sells == 0),
            "ambiguous_swap_count": state["ambiguous_swap_count"],
            "event_order_violation_count": state["order_violations"],
            "event_parse_failure_count": state["parse_failures"],
            "excluded_noncanonical_event_rows": state["excluded_noncanonical_rows"],
        }
        group = holder_groups.get(info["token_addr_norm"], pd.DataFrame(columns=holders.columns))
        row.update(holder_features(group))
        row.update(dynamic_features(
            dynamic.get(info["token_addr_norm"]),
            info["token_addr_norm"],
            holder_input_available=not group.empty,
        ))
        output.append(row)

    all_features = pd.DataFrame(output)
    exclusions = args.raw / "fake_lp_list_merge.json"
    fake_records = json.loads(exclusions.read_text(encoding="utf-8")) if exclusions.exists() else []
    remove = fake_lp_mask(all_features, fake_records)
    removal = all_features.loc[remove, ["token_addr_idx", "token_addr", "label"]].copy()
    removal["removal_reason"] = "fake_lp_list"
    clean = all_features.loc[~remove].copy()

    save_features(clean, args.output)
    print(f"features_all={len(all_features)}, removed={len(removal)}, features_clean={len(clean)}")
    print(f"clean labels={clean['label'].value_counts().sort_index().to_dict()}")
    print(f"unique_buyers address field={args.buyer_address_field}; owner_buy_tx_count unchanged (legacy tx_to)")


if __name__ == "__main__":
    main()
