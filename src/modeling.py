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
