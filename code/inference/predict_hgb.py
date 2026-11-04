"""HGB inference using the exact frozen final feature order and preprocessor."""
import argparse
import hashlib
import json
import sys
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from threadpoolctl import threadpool_limits

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'code/preprocessing'))
from generate_features import add_aggregates, transform, add_relative_event_features


def main():
    default_model_dir = ROOT / 'code/inference/model'
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--model-dir", type=Path, default=default_model_dir)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.input.resolve() == args.output.resolve():
        raise ValueError("Output must not overwrite input")
    frame = pd.read_csv(args.input)
    metadata = json.loads((args.model_dir / "metadata.json").read_text(encoding="utf-8"))
    if "tx_from" in metadata.get("feature_semantics", ""):
        legacy_input_hash = "3a8c4ac8999985798514bb51b0a934e77ceb1cabd685a751d2b9df08d283e335"
        if hashlib.sha256(args.input.read_bytes()).hexdigest() == legacy_input_hash:
            raise ValueError("Legacy tx_to feature CSV is incompatible with the selected tx_from model; use corrected features or regenerate with --buyer-address-field tx_from")
    required = metadata["features"]
    compact = {"dynamic_buy_success_count", "dynamic_paired_honeypot_count"}
    if not compact <= set(frame.columns):
        raw = {f"{action}_success_{i}" for action in ["buy", "sell"] for i in range(1, 4)}
        if not raw <= set(frame.columns):
            raise ValueError("Supply both compact Basic features or all six measured buy/sell fields")
        frame = add_aggregates(frame)
    relative = {name for name in required if name.startswith("rel_")}
    if relative and relative <= set(frame.columns):
        # Compact CSVs contain float64 ratios already calculated by the
        # training pipeline. The fast default CSV float parser may move a
        # repeated rational value across an HGB bin boundary by one ULP.
        # Preserve exported float bits; raw historical feature CSVs continue
        # to use their original parsing before row-local ratio generation.
        frame = pd.read_csv(args.input, float_precision="round_trip")
    if not relative <= set(frame.columns):
        try:
            frame, _ = add_relative_event_features(frame)
        except KeyError as exc:
            raise ValueError("Relative-activity model requires its compact relative columns or the original Event numerator/denominator fields") from exc
    missing = set(required + ["token_addr_idx", "token_addr"]) - set(frame.columns)
    if missing:
        raise ValueError(f"Missing fields: {sorted(missing)}")
    if not frame.token_addr.str.lower().is_unique or not frame.token_addr_idx.is_unique:
        raise ValueError("Duplicate token identities")
    if frame.dynamic_json_available.notna().all() and frame.dynamic_json_available.isin([0, 1]).all():
        missing_json = frame.dynamic_json_available.eq(0)
        measurements = ["existing_holders_check", "exterior_call_check", "tax_manipulation", "trading_suspend_check", "unlimited_mint", "dynamic_buy_success_count", "dynamic_paired_honeypot_count"]
        if not frame.loc[missing_json, ["abi_verified", *measurements]].isna().all().all():
            raise ValueError("Absent JSON must remain missing, not placeholder zero")
        abi_missing = frame.abi_verified.eq(0)
        advanced = ["exterior_call_check", "tax_manipulation", "trading_suspend_check", "unlimited_mint"]
        if not frame.loc[abi_missing, advanced].isna().all().all():
            raise ValueError("ABI-dependent unexecuted checks must be missing")
    else:
        raise ValueError("dynamic_json_available must be 0 or 1")
    processor = joblib.load(args.model_dir / "preprocessing.joblib")
    model = joblib.load(args.model_dir / "model.joblib")
    probability = model.predict_proba(transform(frame[required], processor))[:, 1]
    threshold = float(metadata["threshold"])
    output = frame[["token_addr_idx", "token_addr"]].copy()
    output["predicted_probability"] = probability
    output["predicted_label"] = (probability >= threshold).astype(int)
    output["threshold"] = threshold
    args.output.parent.mkdir(parents=True, exist_ok=True)
    output.to_csv(args.output, index=False)
    print(f"HGB inference: {len(output)} tokens, threshold={threshold}, output={args.output}")


if __name__ == "__main__":
    with threadpool_limits(limits=1):
        main()
