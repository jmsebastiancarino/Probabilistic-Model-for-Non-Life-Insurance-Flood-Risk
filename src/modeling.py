"""Shared pieces for the model notebooks (06a, 06b, 07a, 07b, 08).

A fitted model saved with ``joblib`` stores every class it uses by its
module name. A class defined inside a notebook can't be found again from
another notebook, so the transformers our pipelines use live here.
"""

import numpy as np
from sklearn.base import BaseEstimator, TransformerMixin


class QuantileClipper(BaseEstimator, TransformerMixin):
    """Cap every column at percentiles learned on the data it is fitted on.

    Parameters
    ----------
    low, high : float, default 0.01 and 0.99
        Percentiles (as fractions) of the lower and upper caps.

    Examples
    --------
    >>> import numpy as np
    >>> c = QuantileClipper(0.1, 0.9).fit(np.arange(11.0).reshape(-1, 1))
    >>> c.transform(np.array([[-5.0], [5.0], [50.0]])).ravel().tolist()
    [1.0, 5.0, 9.0]
    """

    def __init__(self, low=0.01, high=0.99):
        self.low, self.high = low, high

    def fit(self, X, y=None):
        X = np.asarray(X, dtype="float64")
        self.low_ = np.nanquantile(X, self.low, axis=0)
        self.high_ = np.nanquantile(X, self.high, axis=0)
        return self

    def transform(self, X):
        return np.clip(np.asarray(X, dtype="float64"), self.low_, self.high_)

    def get_feature_names_out(self, input_features=None):
        return np.asarray(input_features, dtype=object)


def make_portable(transformer):
    """Swap any notebook-defined QuantileClipper inside a
    ColumnTransformer for this module's class, keeping the learned caps.

    Use it once before saving a model that was fitted with a clipper
    defined in the notebook. Both the fitted steps (transformers_) and
    the settings it was built from (transformers) are updated; a model
    built with this module's class is returned unchanged.

    Parameters
    ----------
    transformer : sklearn.compose.ColumnTransformer

    Returns
    -------
    int
        Number of clippers swapped.
    """
    swapped = 0
    for attr in ("transformers", "transformers_"):
        for _, trans, _ in getattr(transformer, attr, []):
            for i, (name, step) in enumerate(getattr(trans, "steps", [])):
                if type(step).__name__ == "QuantileClipper" and type(step) is not QuantileClipper:
                    new = QuantileClipper(step.low, step.high)
                    for fitted in ("low_", "high_"):
                        if hasattr(step, fitted):
                            setattr(new, fitted, getattr(step, fitted))
                    trans.steps[i] = (name, new)
                    swapped += 1
    return swapped


class RandomSampleImputer(BaseEstimator, TransformerMixin):
    """Fill missing values with random values drawn from the training data.

    For features whose *missingness* is tied to the outcome (notebook 05,
    5.2d: the ACS features, ``tenure_years``, ``years_since_purchase``) we
    must not let the model know which rows were missing. A median fill
    hides that from a logistic regression, but not from tree models: every
    missing row gets the same exact value, so a tree can split on it and
    find the missing rows anyway. Random draws from the training values
    make a missing row look like an ordinary row, so the trees can't
    single it out.

    Used by the tree models (07b random forest, LightGBM, XGBoost) so all
    of them treat these features the same way.

    Parameters
    ----------
    random_state : int, default 42
        Seed. The same rows in the same order always get the same draws,
        so a saved model gives the same predictions after reloading.
    max_pool : int, default 100_000
        Most training values kept per column to draw from (keeps the saved
        model small).

    Examples
    --------
    >>> import numpy as np
    >>> imp = RandomSampleImputer(random_state=0).fit(np.array([[1.0], [2.0], [3.0], [np.nan]]))
    >>> out = imp.transform(np.array([[np.nan], [5.0]]))
    >>> float(out[0, 0]) in {1.0, 2.0, 3.0}, float(out[1, 0])
    (True, 5.0)
    """

    def __init__(self, random_state=42, max_pool=100_000):
        self.random_state, self.max_pool = random_state, max_pool

    def fit(self, X, y=None):
        X = np.asarray(X, dtype="float64")
        rng = np.random.default_rng(self.random_state)
        self.pools_ = []
        for j in range(X.shape[1]):
            values = X[~np.isnan(X[:, j]), j]                      # the observed training values of column j
            if len(values) > self.max_pool:
                values = rng.choice(values, self.max_pool, replace=False)
            if len(values) == 0:
                values = np.array([0.0])                           # an all-missing column: fill 0
            self.pools_.append(values)
        return self

    def transform(self, X):
        X = np.array(X, dtype="float64")                           # a copy, so the input is not changed
        rng = np.random.default_rng(self.random_state)             # same seed every call: same rows, same draws
        for j, pool in enumerate(self.pools_):
            missing = np.isnan(X[:, j])
            X[missing, j] = rng.choice(pool, missing.sum())
        return X

    def get_feature_names_out(self, input_features=None):
        return np.asarray(input_features, dtype=object)


class StatePreprocessor:
    """The 07b XGBoost preprocessing, as one object that can be saved and reloaded.

    Notebook 07b XGB prepares its predictors with two notebook functions,
    ``fit_preprocessor`` (which learns a *state*: medians, category levels
    and donor pools) and ``transform_features``. A function defined in a
    notebook can't be loaded from another notebook, so this class keeps the
    learned state and repeats ``transform_features`` exactly. Notebooks
    08a/08b load it through the model registry (``src.evaluation``).

    Parameters
    ----------
    state : dict
        The output of ``fit_preprocessor``: ``features``, ``medians``,
        ``levels`` and ``pools``.
    cats, flags : list of str
        Categorical and 0/1 features (``docs/feature_plan.csv``).
    missing_plan : dict of {str: str}
        Feature -> missing-value rule (``docs/feature_plan.csv``).
    seed : int
        Seed of the stable donor draws.

    Notes
    -----
    ``transform`` needs the **raw** rows of ``features.parquet`` with a
    ``period_id`` column: it applies the fixed clean-up rules itself, and the
    donor draw of each row is keyed on its ``period_id``, so a row always
    gets the same value whatever batch it is scored in.
    """

    def __init__(self, state, cats, flags, missing_plan, seed=42):
        self.state, self.cats, self.flags = state, list(cats), list(flags)
        self.missing_plan, self.seed = dict(missing_plan), seed

    def _stable_draw(self, ids, key, pool_size):
        import hashlib
        return np.fromiter((int.from_bytes(hashlib.sha256(f"{self.seed}|{key}|{value}".encode()).digest()[:8], "little")
                            % pool_size for value in ids.astype(str)), dtype=np.int64, count=len(ids))

    def transform(self, frame):
        import pandas as pd
        from src.features import apply_fixed_rules
        clean = apply_fixed_rules(frame).reset_index(drop=True)
        result = pd.DataFrame(index=clean.index)
        random_values = {}
        for key, (group, pool) in self.state["pools"].items():
            selected = self._stable_draw(clean["period_id"], key, len(pool))
            for j, f in enumerate(group):
                random_values[f] = pool[selected, j]
        for f in self.state["features"]:
            if f in self.cats:
                values = clean[f].astype("string").fillna("__MISSING__")
                result[f] = pd.Categorical(values, categories=self.state["levels"][f])
                continue
            values = pd.to_numeric(clean[f], errors="coerce").astype("float64").replace([np.inf, -np.inf], np.nan)
            rule = self.missing_plan.get(f, "")
            if f in random_values:
                values = values.fillna(pd.Series(random_values[f], index=clean.index))
            elif rule.startswith("fill 99"):
                values = values.fillna(99)
            elif f in self.flags or rule.startswith("fill 0"):
                result[f + "__missing"] = values.isna().astype("float32")
                values = values.fillna(0)
            else:
                if "+ missing flag" in rule:
                    result[f + "__missing"] = values.isna().astype("float32")
                values = values.fillna(self.state["medians"][f])
            result[f] = values.astype("float32")
        return result
