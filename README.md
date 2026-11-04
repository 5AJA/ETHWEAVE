# ETHWEAVE

Code and data for **ETHWEAVE: Explainable Ethereum Honeypot Detection from On-Chain Events and Dynamic Analysis**.

## Contents

| Directory | Contents |
| --- | --- |
| [dataset/](dataset/) | Token data, labels, saved splits, and final features in `features/` |
| [code/](code/) | Preprocessing, HGB training, inference, and SHAP |
| [code/inference/model/](code/inference/model/) | Trained HGB model and fitted preprocessing statistics |
| [dynamic_analysis/](dynamic_analysis/) | Dynamic analyzer and its execution instructions |
| [case_study/](case_study/) | Dataset and features for the 18 cases |

Labels: `1` = honeypot, `0` = normal. Dynamic analysis outputs are in `dataset/dynamic_results/`.

## Usage

Python 3.13. Install the `requirements.txt` in the code folder you use. Run commands from this directory.

```sh
# Preprocessing
python code/preprocessing/generate_features.py --dataset dataset --output dataset/features/features.csv

# Training
python code/training/train_hgb.py --output trained_model

# Inference
python code/inference/predict_hgb.py --input dataset/features/features.csv --output predictions.csv

# Case-study SHAP
python code/inference/shap_hgb.py --input case_study/features/features_case.csv --output shap_values.csv
```

For dynamic analysis, see [dynamic_analysis/README.md](dynamic_analysis/README.md).

## License

The [LICENSE](LICENSE) contains both license texts: MIT for our original code and models, and CC BY 4.0 for our original data contributions. Third-party data, labels, and materials are excluded from these grants and retain their original terms.
