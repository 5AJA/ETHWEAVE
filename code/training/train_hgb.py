"""Fit the paper's selected HGB model once on the saved partitions.

Training, preprocessing and threshold calculations retain the research code.
Only input/output handling and experiment-driver dependencies are adapted.
"""
from pathlib import Path
import argparse
import hashlib
import json
import sys
import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import accuracy_score, average_precision_score, confusion_matrix, f1_score, fbeta_score, precision_score, recall_score, roc_auc_score
from threadpoolctl import threadpool_limits

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'code/preprocessing'))
from generate_features import FEATURES, fit_preprocessor, transform
PARAMETERS = {'max_iter': 200, 'learning_rate': 0.07197122290862965, 'max_leaf_nodes': 15, 'max_depth': 3, 'min_samples_leaf': 35, 'l2_regularization': 0.040370457142546716, 'max_bins': 255, 'class_weight': 'balanced'}
SEED = 261002

def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")

def metrics(y: pd.Series, probability: np.ndarray, threshold: float) -> tuple[dict[str, float], np.ndarray]:
    prediction = (probability >= threshold).astype(int)
    tn, fp, fn, tp = confusion_matrix(y, prediction, labels=[0, 1]).ravel()
    result = {
        "accuracy": accuracy_score(y, prediction),
        "precision": precision_score(y, prediction, zero_division=0),
        "recall": recall_score(y, prediction, zero_division=0),
        "f1": f1_score(y, prediction, zero_division=0),
        "f2": fbeta_score(y, prediction, beta=2, zero_division=0),
        "roc_auc": roc_auc_score(y, probability),
        "pr_auc": average_precision_score(y, probability),
        "tn": int(tn), "fp": int(fp), "fn": int(fn), "tp": int(tp),
        "threshold": threshold,
    }
    return result, prediction

def best_f1_threshold(y: pd.Series, probability: np.ndarray) -> tuple[float, dict[str, float]]:
    thresholds = np.linspace(0.01, 0.99, 99)
    actual = y.astype(int).to_numpy()[:, None]
    predicted = probability[:, None] >= thresholds[None, :]
    tp = ((actual == 1) & predicted).sum(axis=0).astype(float)
    fp = ((actual == 0) & predicted).sum(axis=0).astype(float)
    fn = ((actual == 1) & ~predicted).sum(axis=0).astype(float)
    precision = np.divide(tp, tp + fp, out=np.zeros_like(tp), where=(tp + fp) != 0)
    recall = np.divide(tp, tp + fn, out=np.zeros_like(tp), where=(tp + fn) != 0)
    f1 = np.divide(2 * precision * recall, precision + recall,
                   out=np.zeros_like(tp), where=(precision + recall) != 0)
    table = pd.DataFrame({
        "threshold": thresholds, "f1": f1, "precision": precision, "recall": recall,
    })
    selected = table.sort_values(
        ["f1", "precision", "recall", "threshold"], ascending=[False, False, False, True]
    ).iloc[0]
    threshold = float(selected.threshold)
    result, _ = metrics(y, probability, threshold)
    return threshold, result

def choose_threshold(y, probability, policy):
    best, _ = best_f1_threshold(pd.Series(np.asarray(y)), np.asarray(probability))
    if policy == "max_f1":
        return best
    thresholds = np.linspace(.01, .99, 99)
    actual = np.asarray(y).astype(int)[:, None]
    prediction = np.asarray(probability)[:, None] >= thresholds[None, :]
    tp = ((actual == 1) & prediction).sum(axis=0)
    fp = ((actual == 0) & prediction).sum(axis=0)
    fn = ((actual == 1) & ~prediction).sum(axis=0)
    f1 = np.divide(2 * tp, 2 * tp + fp + fn, out=np.zeros(99, dtype=float), where=(2 * tp + fp + fn) != 0)
    tolerance = .001 if policy == "near001_low" else .003
    eligible = thresholds[f1 >= f1.max() - tolerance - 1e-12]
    return float(eligible[0] if policy.endswith("low") else eligible[len(eligible)//2])

def factory(candidate, seed):
    return HistGradientBoostingClassifier(**candidate['params'], random_state=seed, early_stopping=False)

def fit_evaluate(frame, candidate, policy, train, calibration, test, seed, dest, label="validation"):
    assert not set(train) & set(calibration) and not set(train) & set(test) and not set(calibration) & set(test)
    pre = fit_preprocessor(frame.iloc[train][candidate["features"]], candidate["mode"])
    model = factory(candidate, seed).fit(transform(frame.iloc[train][candidate["features"]], pre), frame.iloc[train].label)
    cal_p = model.predict_proba(transform(frame.iloc[calibration][candidate["features"]], pre))[:, 1]
    threshold = choose_threshold(frame.iloc[calibration].label, cal_p, policy)
    cal_m, _ = metrics(frame.iloc[calibration].label, cal_p, threshold)
    p = model.predict_proba(transform(frame.iloc[test][candidate["features"]], pre))[:, 1]
    test_m, _ = metrics(frame.iloc[test].label, p, threshold)
    dest.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, dest / "model.joblib")
    joblib.dump(pre, dest / "preprocessing.joblib")
    result = dict(threshold=threshold, n_train=len(train), n_validation=len(calibration), n_test=len(test), n_features=len(candidate["features"]),
        **{"validation_"+k: v for k,v in cal_m.items()}, **{"test_"+k:v for k,v in test_m.items()})
    split = frame[["token_addr_idx", "token_addr", "label"]].copy()
    split["split"] = "unused"
    for name, idx in [("train", train), (label, calibration), ("test", test)]:
        split.loc[idx, "split"] = name
    split.to_csv(dest / "split.csv", index=False)
    for role, idx, probability in [("validation", calibration, cal_p), ("test", test, p)]:
        pred = frame.iloc[idx][["token_addr_idx", "token_addr", "label", "abi_verified"]].copy()
        pred["split"] = role
        pred["predicted_probability"], pred["predicted_label"] = probability, (probability>=threshold).astype(int)
        pred.to_csv(dest / ("predictions_"+role+".csv"), index=False)
    write(dest / "metadata.json", dict(classifier=type(model).__name__, family=candidate["family"], params=candidate["params"],
        features=candidate["features"], preprocessing=candidate["mode"], threshold_policy=policy, seed=seed,
        
        feature_semantics="unique_buyers=distinct buy tx_from transaction initiators; any relative features row-local",
        split_sha256=sha(dest / "split.csv"), **result))
    pd.DataFrame([result]).to_csv(dest / "metrics.csv", index=False)
    return result

def main():
    parser = argparse.ArgumentParser(description="Train the selected HGB model.")
    parser.add_argument('--features', type=Path, default=ROOT / 'dataset/features/features.csv')
    parser.add_argument('--split', type=Path, default=ROOT / 'dataset/split.csv')
    parser.add_argument('--output', type=Path, default=Path('trained_model'))
    args = parser.parse_args()
    frame = pd.read_csv(args.features, float_precision='round_trip')
    split = pd.read_csv(args.split)
    frame = frame.merge(split[['token_addr_idx', 'token_addr', 'label', 'split']], on=['token_addr_idx', 'token_addr', 'label'], validate='one_to_one')
    candidate = dict(family='HGB', params=PARAMETERS, features=FEATURES, mode='winsor_0001')
    train, validation, test = [np.flatnonzero(frame.split.eq(role)) for role in ['train', 'validation', 'test']]
    result = fit_evaluate(frame, candidate, 'near003_low', train, validation, test, SEED, args.output)
    print(json.dumps({'test_f1': result['test_f1'], 'threshold': result['threshold']}))

if __name__ == '__main__':
    with threadpool_limits(limits=1):
        main()
