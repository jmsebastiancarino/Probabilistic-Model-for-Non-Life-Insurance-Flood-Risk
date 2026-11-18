"""Shared pieces for notebook 09 (explainability and report).

Every model of the registry (``src.evaluation.load_registry``) is explained
with the same two model-agnostic tools, so the models can be compared:

* **permutation importance**: shuffle one feature and measure the drop in
  ROC-AUC;
* **partial dependence**: set one feature to a value for every policy and
  average the predicted probability.

Each model is also broken down policy by policy into one contribution per
feature, with the exact method for its type (no extra package needed):

* XGBoost and LightGBM: exact **TreeSHAP**, computed by the libraries
  themselves (log-odds scale);
* random forests: the **path decomposition** of each tree (Saabas /
  ``treeinterpreter``): the change in the tree's probability at every split
  is credited to the split's feature (probability scale);
* logistic regression: **coefficient x (value - training mean)**, which is
  the exact SHAP value of a linear model (log-odds scale).

In every case the contributions add up to the model's prediction:
``baseline + sum(contributions) = prediction`` (on the scale stated).
"""
import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score

from src import evaluation as ev


# ----------------------------------------------------------------------------- data
def load_sample(con, kind, models, split, n_rows, seed=42, extra=("premium", "premium_per_1k_coverage",
                                                                    "building_coverage", "contents_coverage")):
    """A random sample of one split for one target: the raw rows and the prepared rows.

    Parameters
    ----------
    con : duckdb.DuckDBPyConnection
        Connection with the ``feats`` view.
    kind : {"claim", "lapse"}
    models : dict
        The registry of that target.
    split : {"train", "test", "valid"}
    n_rows : int
        Sample size (the same rows for the same seed).

    Returns
    -------
    (pandas.DataFrame, pandas.DataFrame)
        ``raw`` (as read, with ``period_id``, ``period_year``, ``y`` and the
        ``extra`` columns) and ``df`` (the same rows after
        :func:`src.evaluation.prepare`).
    """
    spec = ev.TARGETS[kind]
    needed = features_of(models)
    cols = list(dict.fromkeys(needed + [c for c in extra if c not in needed]))
    query = f"""SELECT period_id, period_year, {spec['target']} AS y, {', '.join(cols)}
                FROM feats WHERE {spec['pop']} AND split = '{split}'"""
    raw = con.sql(f"SELECT * FROM ({query}) USING SAMPLE reservoir({n_rows} ROWS) REPEATABLE ({seed})").df()
    raw = raw.sort_values("period_id").reset_index(drop=True)
    raw["y"] = raw["y"].astype("int8")
    return raw, prepare(raw, models)


def features_of(models):
    """Every feature used by at least one model, in a fixed order."""
    return sorted(set().union(*(m["features"] for m in models.values())))


def prepare(raw, models):
    """The prepared rows (fixed rules and types) for every model of the registry."""
    kinds = _kinds()
    return ev.prepare(raw.copy(), features_of(models), kinds)


_KINDS = None
def _kinds():
    global _KINDS
    if _KINDS is None:
        from src.paths import DOCS
        _KINDS = pd.read_csv(DOCS / "feature_plan.csv").set_index("feature")["kind"].to_dict()
    return _KINDS


def predict_all(models, raw, df):
    """Probability of the event from every model: model name -> numpy array."""
    return {name: ev.predict(m, df[m["features"]], raw) for name, m in models.items()}


# ----------------------------------------------------------------------------- permutation importance
def permutation_importance(models, raw, seed=42, features=None):
    """Drop in ROC-AUC when one feature is shuffled, for every model and feature.

    The feature is shuffled in the **raw** rows and the rows are prepared
    again, so every model sees the same shuffled data (and derived features
    follow: shuffling ``tenure_years`` also changes ``new_business``, which
    the fixed rules recompute from it).

    Returns
    -------
    pandas.DataFrame
        One row per feature, one column per model (``NaN`` where the model
        does not use the feature).
    """
    y = raw["y"].to_numpy()
    df = prepare(raw, models)
    base = {name: roc_auc_score(y, p) for name, p in predict_all(models, raw, df).items()}
    rng = np.random.default_rng(seed)
    rows = {}
    for f in (features or features_of(models)):
        shuffled = raw.copy()
        shuffled[f] = shuffled[f].to_numpy()[rng.permutation(len(shuffled))]
        sdf = prepare(shuffled, models)
        rows[f] = {name: (base[name] - roc_auc_score(y, ev.predict(m, sdf[m["features"]], shuffled))
                          if f in m["features"] else np.nan)
                   for name, m in models.items()}
    return pd.DataFrame(rows).T.rename_axis("feature")


# ----------------------------------------------------------------------------- partial dependence
def grid_for(raw, feature, n_points=12, top_levels=8):
    """Values to try for one feature: quantiles of a number (5th to 95th), or the most common levels."""
    s = raw[feature]
    if _kinds().get(feature) == "cat":
        return s.dropna().astype(str).value_counts().head(top_levels).index.tolist()
    if _kinds().get(feature) == "flag":
        return [0, 1]
    vals = pd.to_numeric(s, errors="coerce").dropna()
    lo, hi = (np.quantile(vals, [0.0, 0.995]) if len(vals) else (0, 0))
    if len(vals) and np.allclose(vals, np.round(vals)) and hi - lo <= 20:   # a small count (prior claims): 0, 1, 2, ...
        return list(np.arange(lo, max(hi, lo + 3) + 1)[:n_points].astype(float))
    return sorted(set(np.round(np.quantile(vals, np.linspace(0.05, 0.95, n_points)), 6)))


def partial_dependence(models, raw, feature, grid=None):
    """Mean predicted probability of every model when ``feature`` is set to each grid value for every row.

    Returns
    -------
    pandas.DataFrame
        Index: the grid values; one column per model.
    """
    grid = grid_for(raw, feature) if grid is None else grid
    out = {}
    for v in grid:
        r = raw.copy()
        r[feature] = v
        d = prepare(r, models)
        out[v] = {name: float(np.mean(ev.predict(m, d[m["features"]], r)))
                  for name, m in models.items() if feature in m["features"]}
    return pd.DataFrame(out).T.rename_axis(feature)


# ----------------------------------------------------------------------------- contributions per policy
def contributions(m, raw, df):
    """Per-policy contributions of each feature for one registry entry.

    Returns
    -------
    (pandas.DataFrame, numpy.ndarray, str)
        ``contrib`` (rows x features, summed over the model columns of each
        feature, e.g. a category's levels or a missing-value flag), the
        ``baseline`` of every row, and the ``scale`` ("log-odds" or
        "probability"). ``baseline + contrib.sum(axis=1)`` equals the model's
        raw output on that scale (before any calibrator).
    """
    model = m["model"]
    library = getattr(model, "library", None)
    if library in ("xgboost", "lightgbm") or type(model).__module__.startswith("xgboost"):
        return _tree_shap(m, raw, df)
    if hasattr(model, "estimators_") or library == "sklearn":
        Z, names, est = _forest_matrix(m, raw, df)
        base, contrib = forest_paths(est, Z)
        return _to_features(contrib, names, m["features"]), base, "probability"
    if hasattr(model, "coef_"):
        return _linear(m, df)
    raise TypeError(f"no contribution method for {type(model).__name__}")


def _tree_shap(m, raw, df):
    import xgboost as xgb
    model = m["model"]
    if getattr(model, "library", None) == "lightgbm":                   # 07a LightGBM bundle
        X = m["preprocess"].transform(df[m["features"]])
        out = model.estimator.predict(X, num_iteration=model.trees, pred_contrib=True)
        names = list(X.columns)
    elif getattr(model, "library", None) == "xgboost":                  # 07a XGBoost bundle (Booster)
        X = m["preprocess"].transform(df[m["features"]])
        out = model.estimator.predict(xgb.DMatrix(X, enable_categorical=True), pred_contribs=True,
                                      iteration_range=(0, model.trees) if model.trees else (0, 0))
        names = list(X.columns)
    else:                                                               # 07b XGBoost (XGBClassifier, raw input)
        X = m["preprocess"].transform(raw if m.get("raw_input") else df[m["features"]])
        out = model.get_booster().predict(xgb.DMatrix(X, enable_categorical=True), pred_contribs=True)
        names = list(X.columns)
    out = np.asarray(out)
    return _to_features(out[:, :-1], names, m["features"]), out[:, -1], "log-odds"


def _forest_matrix(m, raw, df):
    model = m["model"]
    if getattr(model, "library", None) == "sklearn":                    # 07a random forest bundle
        X = m["preprocess"].transform(df[m["features"]])
        Z = X.copy()
        for c in model.categories:
            codes = Z[c].cat.codes.astype("float32")
            Z[c] = codes.where(codes >= 0, np.nan)
        return Z[model.features].to_numpy("float32"), list(model.features), model.estimator
    Z = m["preprocess"].transform(df[m["features"]])                   # 07b random forest (ColumnTransformer)
    names = list(m["preprocess"].get_feature_names_out())
    return np.asarray(Z, dtype=np.float32), names, model


def forest_paths(forest, Z):
    """Path decomposition of a random forest: baseline and one contribution per model column.

    For each tree, the change in the probability of the event from a node to
    its child is credited to the feature the node splits on; averaged over the
    trees, ``baseline + contributions.sum(axis=1)`` equals the forest's
    probability exactly.

    >>> from sklearn.ensemble import RandomForestClassifier
    >>> rng = np.random.default_rng(0); X = rng.normal(size=(300, 3)); y = (X[:, 0] > 0).astype(int)
    >>> rf = RandomForestClassifier(n_estimators=5, random_state=0).fit(X, y)
    >>> b, c = forest_paths(rf, X[:4])
    >>> bool(np.allclose(b + c.sum(axis=1), rf.predict_proba(X[:4])[:, 1]))
    True
    """
    n, k = Z.shape
    contrib = np.zeros((n, k))
    base = np.zeros(n)
    for tree in forest.estimators_:
        t = tree.tree_
        v = t.value[:, 0, :]
        p = v[:, 1] / v.sum(axis=1)                                     # probability of class 1 at each node
        path = tree.decision_path(Z)
        for i in range(n):
            nodes = path.indices[path.indptr[i]:path.indptr[i + 1]]     # root -> leaf (node ids grow down a path)
            np.add.at(contrib[i], t.feature[nodes[:-1]], p[nodes[1:]] - p[nodes[:-1]])
        base += p[0]
    T = len(forest.estimators_)
    return base / T, contrib / T


def _linear(m, df):
    X = m["preprocess"].transform(df[m["features"]])
    X = X.toarray() if hasattr(X, "toarray") else np.asarray(X, dtype=float)
    names = list(m["preprocess"].get_feature_names_out())
    coef = m["model"].coef_.ravel()
    mean = np.asarray(X.mean(axis=0)).ravel() if m.get("x_mean") is None else m["x_mean"]
    contrib = (X - mean) * coef
    base = np.full(len(X), float(m["model"].intercept_[0] + mean @ coef))
    return _to_features(contrib, names, m["features"]), base, "log-odds"


def _to_features(contrib, names, features):
    """Sum model-column contributions into one column per feature (levels, missing flags)."""
    features = sorted(features, key=len, reverse=True)                  # longest first: 'premium_change_pct' before 'premium'
    def owner(col):
        c = col.replace("missingindicator_", "")
        if c.endswith("__missing"):
            c = c[: -len("__missing")]
        return next((f for f in features if c == f or c.startswith(f + "_")), c)
    frame = pd.DataFrame(contrib, columns=names)
    return frame.T.groupby([owner(c) for c in names]).sum().T


def mean_abs(contrib):
    """Global importance from contributions: mean absolute contribution of each feature, largest first."""
    return contrib.abs().mean().sort_values(ascending=False)


# ----------------------------------------------------------------------------- charts
def plot_beeswarm(ax, contrib, values, title, top=15, n_dots=3000, seed=42):
    """SHAP-style summary: one dot per policy and feature, coloured by the feature's value (low blue, high red)."""
    order = mean_abs(contrib).head(top).index[::-1]
    rng = np.random.default_rng(seed)
    idx = rng.choice(len(contrib), size=min(n_dots, len(contrib)), replace=False)
    for j, f in enumerate(order):
        x = contrib[f].to_numpy()[idx]
        v = values[f].iloc[idx] if f in values else pd.Series(np.nan, index=idx)
        if v.dtype == object or str(v.dtype) == "category":
            colour = np.full(len(idx), 0.5)
        else:
            r = pd.to_numeric(v, errors="coerce").rank(pct=True).to_numpy()
            colour = np.where(np.isnan(r), 0.5, r)
        ax.scatter(x, j + rng.uniform(-0.3, 0.3, len(idx)), c=colour, cmap="coolwarm", s=4, alpha=0.5,
                   vmin=0, vmax=1, rasterized=True)
    ax.axvline(0, color="#9a9a9a", linewidth=1)
    ax.set_yticks(range(len(order)), order)
    ax.set_title(title, fontsize=10)
    ev.style_axes(ax, grid="x")


def plot_waterfall(ax, contrib_row, base, pred, scale, title, top=8):
    """One policy: baseline, the largest contributions, the rest, and the prediction."""
    c = contrib_row.reindex(contrib_row.abs().sort_values(ascending=False).index)
    shown, rest = c.head(top), c.iloc[top:].sum()
    labels = ["baseline"] + list(shown.index) + [f"{len(c) - top} other features"] + ["prediction"]
    steps = [base] + list(shown.values) + [rest]
    start = np.concatenate([[0], np.cumsum(steps)[:-1]])
    for i, (s, d) in enumerate(zip(start, steps)):
        colour = "#9a9a9a" if i == 0 else ("#d9822b" if d > 0 else "#2a6fb0")
        ax.barh(i, d if i else s + d, left=s if i else 0, color=colour, height=0.6)
    ax.barh(len(steps), pred, color="#333333", height=0.6)
    ax.set_yticks(range(len(labels)), labels)
    ax.invert_yaxis()
    ax.set_xlabel(f"contribution ({scale})")
    ax.set_title(title, fontsize=10)
    ev.style_axes(ax, grid="x")


def to_probability(x, scale):
    """Turn a model output on its scale into a probability."""
    return 1 / (1 + np.exp(-np.asarray(x))) if scale == "log-odds" else np.asarray(x)
