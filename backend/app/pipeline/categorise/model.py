"""ML fallback categoriser: char n-gram TF-IDF + amount/channel features -> LightGBM (SPEC §5.4)."""
from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from pathlib import Path

import joblib
import numpy as np
from lightgbm import LGBMClassifier
from scipy import sparse
from sklearn.feature_extraction.text import TfidfVectorizer

from app.core.config import ARTIFACTS_DIR
from app.pipeline.merchants import clean, parse_narration

CHANNELS = ["upi", "pos", "card", "other"]
MODEL_PATH = ARTIFACTS_DIR / "categoriser.joblib"
MODEL_VERSION = "1"


def feature_text(narration: str, merchant_hint: str | None = None, channel_hint: str | None = None,
                 account_kind: str | None = None) -> tuple[str, str]:
    """The same payee extraction the pipeline uses, so train and inference see identical text."""
    p = parse_narration(narration, merchant_hint, channel_hint, account_kind)
    ch = p.channel if p.channel in CHANNELS else "other"
    return clean(p.payee), ch


@dataclass
class Categoriser:
    seed: int
    vectorizer: TfidfVectorizer = field(default=None)
    model: LGBMClassifier = field(default=None)
    classes: list[str] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    version: str = MODEL_VERSION

    def _features(self, texts: list[str], amounts_paise: list[int], channels: list[str], fit: bool = False):
        x_text = self.vectorizer.fit_transform(texts) if fit else self.vectorizer.transform(texts)
        amt = np.log1p(np.asarray(amounts_paise, dtype=float) / 100.0).reshape(-1, 1)
        ch = np.array([[c == k for k in CHANNELS] for c in channels], dtype=float)
        return sparse.hstack([x_text, sparse.csr_matrix(amt), sparse.csr_matrix(ch)], format="csr")

    def fit(self, texts, amounts_paise, channels, labels) -> Categoriser:
        self.vectorizer = TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 5), min_df=2, sublinear_tf=True,
                                          max_features=15000, dtype=np.float32)
        x = self._features(texts, amounts_paise, channels, fit=True)
        self.model = LGBMClassifier(
            objective="multiclass", n_estimators=120, learning_rate=0.15, num_leaves=15, min_child_samples=5,
            colsample_bytree=0.5, subsample=1.0, reg_lambda=1.0, random_state=self.seed, deterministic=True,
            force_col_wise=True, num_threads=1, verbose=-1)
        self.model.fit(x, np.asarray(labels))
        self.classes = list(self.model.classes_)
        return self

    def predict(self, texts, amounts_paise, channels) -> tuple[list[str], list[float]]:
        if not texts:
            return [], []
        x = self._features(texts, amounts_paise, channels)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            proba = self.model.predict_proba(x)
        idx = proba.argmax(axis=1)
        return [self.classes[i] for i in idx], [round(float(proba[r, i]), 4) for r, i in enumerate(idx)]

    def save(self, path: Path = MODEL_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(self, path)

    @staticmethod
    def load(path: Path = MODEL_PATH) -> Categoriser:
        return joblib.load(path)
