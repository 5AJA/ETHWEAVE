"""TreeSHAP explanation using the original HGB numerical calculation."""
from pathlib import Path
import argparse
import json
import sys
import joblib
import numpy as np
import pandas as pd
import shap
from scipy.special import expit
from threadpoolctl import threadpool_limits

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'code/preprocessing'))
from generate_features import transform

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', type=Path, default=ROOT / 'dataset/features/features.csv')
    parser.add_argument('--model-dir', type=Path, default=ROOT / 'code/inference/model')
    parser.add_argument('--output', type=Path, default=Path('shap_values.csv'))
    args = parser.parse_args()
    frame = pd.read_csv(args.input, float_precision='round_trip')
    metadata = json.loads((args.model_dir / 'metadata.json').read_text())
    features = metadata['features']
    model = joblib.load(args.model_dir / 'model.joblib')
    processor = joblib.load(args.model_dir / 'preprocessing.joblib')
    matrix = transform(frame[features], processor)
    explainer = shap.TreeExplainer(model, model_output="raw", feature_perturbation="tree_path_dependent")
    values = np.asarray(explainer.shap_values(matrix))
    base = float(np.asarray(explainer.expected_value).ravel()[0])
    probability = model.predict_proba(matrix)[:, 1]
    error = float(np.max(np.abs(expit(values.sum(axis=1) + base) - probability)))
    assert error < 1e-7
    output = frame[[column for column in ['token_addr_idx', 'token_addr', 'label'] if column in frame]].reset_index(drop=True)
    for i, name in enumerate(features):
        output[name] = values[:, i]
    output['base_logit'] = base
    args.output.parent.mkdir(parents=True, exist_ok=True)
    output.to_csv(args.output, index=False)

if __name__ == '__main__':
    with threadpool_limits(limits=1):
        main()
