"""ML package — feature engineering and model training (multi-user-aware)."""

from ml.features import extract_features, stack_user_features
from ml.train import train_personal_model

__all__ = ["extract_features", "stack_user_features", "train_personal_model"]
