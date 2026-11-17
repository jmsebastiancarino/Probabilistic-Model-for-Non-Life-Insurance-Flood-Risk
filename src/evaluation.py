"""Shared pieces for the evaluation notebooks (08a, 08b).

08a scores the evaluation split once and judges the models as classifiers
(confusion matrices, ROC). 08b reads those predictions and adds the
probability checks and the business analyses (calibration, segments,
stability, cohort survival, monitoring). Both notebooks call the
functions here, once for the claim model and once for the lapse model, so
the two sections of each notebook run the same code.

Every query reads the DuckDB view ``feats`` (``data/processed/features.parquet``).
"""
import json

import numpy as np
import pandas as pd
from sklearn.metrics import (brier_score_loss, confusion_matrix, log_loss, precision_recall_curve, roc_auc_score,
                             roc_curve)

from src.features import apply_fixed_rules

#: The two targets: population flag, target column, the segment rule of
#: the naive benchmark (as in 06a/06b), the cut-off rules (share of
#: policy-years flagged) and the main rule used in the 08a benchmark table.
TARGETS = {
    "claim": {"target": "CLAIM_IN_TERM", "pop": "in_pop_claim", "event": "claim", "events": "claims",
              "segment": {"seg_1": "CASE WHEN zone_group = 'UNKNOWN' THEN 'D' ELSE zone_group END",
                          "seg_2": "CAST(COALESCE(had_prior_claim, FALSE) AS INTEGER)"},
              "segment_label": "zone group x earlier claim",
              "flag_shares": [0.05, 0.10, 0.20], "main_share": 0.10},
    "lapse": {"target": "LAPSE", "pop": "in_pop_lapse", "event": "lapse", "events": "lapses",
              "segment": {"seg_1": "CAST(COALESCE(tenure_years >= 0 AND tenure_years < 1, FALSE) AS INTEGER)",
                          "seg_2": "CAST(COALESCE(mandatory_purchase, FALSE) AS INTEGER)"},
              "segment_label": "new business x mandatory purchase",
              "flag_shares": [0.10, 0.20, 0.30], "main_share": 0.20},
}

#: Colours: one per model (the segment rule is always grey), and the three periods.
MODEL_COLORS = ["#2a6fb0", "#d9822b", "#7b5ea7", "#3c9a5f"]
GREY = "#9a9a9a"
PERIOD_COLORS = {"in-sample": "#f4f4f4", "OOT1": "#fbeee0", "OOT2": "#efeaf6"}
EVAL_NAMES = {"test": "OOT1 (test, 2021-2022)", "valid": "OOT2 (validation, 2023 - Aug 2024)"}


def split_of_year(year):
    """The split a policy year belongs to (train 2014-2020, test 2021-2022, valid 2023-2024)."""
    return "train" if year <= 2020 else ("test" if year <= 2022 else "valid")


def period_of_year(year):
    """The validation period of a policy year: in-sample, OOT1 or OOT2."""
    return {"train": "in-sample", "test": "OOT1", "valid": "OOT2"}[split_of_year(year)]


# ----------------------------------------------------------------------------- models and benchmarks
def load_registry(kind, model_dir, reports_dir):
    """Every saved model for one target: ``<model_dir>/<kind>_*.joblib``.

    Parameters
    ----------
    kind : {"claim", "lapse"}
    model_dir, reports_dir : pathlib.Path
        Where notebooks 06/07 saved the models and their run records.

    Returns
    -------
    dict of {str: dict}
        Model name -> ``{"preprocess", "model", "features", "run"}``, plus
        ``"calibrator"`` when the notebook saved one (07b random forest).

    Notes
    -----
    Every model must be saved as one ``joblib`` dict with ``preprocess``
    (a fitted transformer), ``model`` (with ``predict_proba``) and
    ``features``; an optional ``calibrator`` (with ``predict``, such as
    ``IsotonicRegression``) is applied to the model's probability.
    """
    import joblib
    models = {}
    for path in sorted(model_dir.glob(f"{kind}_*.joblib")):
        saved = joblib.load(path)
        if "preprocess" not in saved and "prep" in saved and ({"model", "booster"} & set(saved)):
            from src.modeling import adapt_tree_bundle                       # a 07a claim tree bundle (RF, XGBoost, LightGBM)
            saved = adapt_tree_bundle(saved)
        missing = {"preprocess", "model", "features"} - set(saved)
        if missing:
            raise ValueError(f"{path.name} lacks {sorted(missing)}: save it as a dict with preprocess, model and features")
        run_file = reports_dir / f"{path.stem}_run.json"
        saved["run"] = json.loads(run_file.read_text()) if run_file.exists() else {}
        share_file = reports_dir / f"{path.stem}_test_recall_by_share.csv"          # 07a: recall at equal flagged shares
        if share_file.exists():
            by_share = pd.read_csv(share_file, index_col=0)
            main = by_share.index[np.isclose(by_share.index, TARGETS[kind]["main_share"])]
            if len(main):
                saved["test_recall_main_share"] = float(by_share.loc[main[0]].iloc[-1])   # last column = this model
        # a calibrator is applied only if the notebook used it: an explicit "use_calibrator" key wins, otherwise
        # the run record's "calibration" note (07b random forest: "... not used (made test results slightly worse)")
        if saved.get("calibrator") is not None:
            note = str(saved["run"].get("calibration", ""))
            saved["use_calibrator"] = saved.get("use_calibrator", "not used" not in note)
        models[path.stem] = saved
    if not models:
        raise FileNotFoundError(f"no saved {kind} model in {model_dir}: run notebook 06{'a' if kind == 'claim' else 'b'} first")
    return models


def primary_model(models, choice=None):
    """The model whose results the charts show.

    ``choice`` (a model name) wins; otherwise the advanced model (07a/07b)
    with the lowest test log loss in its run record (07a claim tree models,
    which save no log loss: the highest test recall when exactly 10% are flagged),
    otherwise the baseline.
    """
    if choice:
        if choice not in models:
            raise KeyError(f"{choice} is not a saved model: {list(models)}")
        return choice
    advanced = [name for name in models if "baseline" not in name]
    if not advanced:
        return next(iter(models))

    def test_log_loss(name):
        run = models[name].get("run", {})
        if "test_overall" in run:
            return run["test_overall"].get("log_loss", np.inf)
        for r in run.get("development_metrics", []):                          # 07b XGBoost
            if r.get("model") == run.get("selected_model"):
                return r.get("log_loss", np.inf)
        return np.inf

    def test_recall(name):
        # 07a claim tree models save confusion results only: test recall when exactly the main share
        # (10%) of policy-years is flagged, so every model is compared at the same list size
        return models[name].get("test_recall_main_share", 0.0)
    return min(advanced, key=lambda name: (test_log_loss(name), -test_recall(name)))


def registry_table(kind, models, primary):
    """One row per model: type, number of features, training split and settings."""
    rows = []
    for name, m in models.items():
        run, params = m["run"], m["run"].get("params", {})
        rows.append({"target": kind, "model": name, "primary": name == primary, "type": getattr(m["model"], "type_name", type(m["model"]).__name__),
                     "features": len(m["features"]), "trained_on": run.get("train_split", run.get("training_data", "?")),
                     "settings": ", ".join(f"{k}={params[k]}" for k in ("alpha", "eta0") if k in params),
                     "epochs": run.get("epochs"), "saved_at": run.get("saved_at")})
    return pd.DataFrame(rows)


def build_benchmarks(con, kind):
    """Rebuild the two naive benchmarks from the training split (same SQL as 06a/06b).

    Creates the temporary table ``seg_<kind>`` (segment -> training rate) and
    returns the training base rate.
    """
    spec = TARGETS[kind]
    base_rate = con.execute(f"SELECT AVG(CAST({spec['target']} AS DOUBLE)) FROM feats "
                            f"WHERE {spec['pop']} AND split = 'train'").fetchall()[0][0]
    seg_sql = ", ".join(f"{expr} AS {col}" for col, expr in spec["segment"].items())
    con.execute(f"""CREATE OR REPLACE TEMP TABLE seg_{kind} AS
                    SELECT {seg_sql}, COUNT(*) AS train_rows, AVG(CAST({spec['target']} AS DOUBLE)) AS segment_rate
                    FROM feats WHERE {spec['pop']} AND split = 'train' GROUP BY ALL""")
    return base_rate


def build_storm_term(con, split, storm_path):
    """Temporary table ``storm_term``: the strongest IBTrACS wind at the property *during* the term.

    Information from the outcome period, used only to cut the results
    (an evaluation segment), never as a feature.
    """
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE storm_term AS
        SELECT f.period_id, MAX(w.wind_kt) AS term_wind_kt
        FROM feats f
        JOIN read_parquet('{storm_path}') w
          ON w.latitude = CAST(f.latitude AS DOUBLE) AND w.longitude = CAST(f.longitude AS DOUBLE)
         AND w.wind_date BETWEEN f.period_start AND f.period_end
        WHERE f.split = '{split}'
        GROUP BY f.period_id""")


# ----------------------------------------------------------------------------- scoring
def prepare(df, features, kinds):
    """Fixed clean-up rules and the column types the pipelines were fitted on (as in 06a/06b)."""
    df = apply_fixed_rules(df)
    for f in features:
        if kinds.get(f) == "cat":
            s = df[f].astype("string")
            df[f] = s.astype(object).where(s.notna(), np.nan)
        else:
            df[f] = pd.to_numeric(df[f], errors="coerce").astype("float32")
    return df


def predict(m, X, raw=None):
    """Probability of the event from one registry entry: preprocessing, model, then the calibrator if used.

    ``X`` holds the prepared feature columns (:func:`prepare`). A model saved
    with ``"raw_input": True`` (07b XGBoost) gets the raw rows ``raw``
    instead, because its preprocessing applies the fixed rules itself and
    needs ``period_id``.
    """
    p = m["model"].predict_proba(m["preprocess"].transform(raw if m.get("raw_input") else X))[:, 1]
    if m.get("calibrator") is not None and m.get("use_calibrator", True):
        p = np.clip(m["calibrator"].predict(p), 0.0, 1.0)
    return p


def model_columns(models):
    """Probability columns in table order: the two benchmarks, then every model."""
    return {"base rate": "p_base", "segment rate": "p_segment", **{name: f"p_{name}" for name in models}}


def score_rows(con, kind, models, base_rate, kinds, where, sample_rows=None, seed=42):
    """Score the population rows of one target that match ``where`` with every model and both benchmarks.

    Parameters
    ----------
    con : duckdb.DuckDBPyConnection
        Connection with the ``feats`` view and the ``seg_<kind>`` table.
    kind : {"claim", "lapse"}
    models : dict
        From :func:`load_registry`.
    base_rate : float
        From :func:`build_benchmarks`.
    kinds : dict of {str: str}
        Feature -> ``num`` / ``cat`` / ``flag`` (``docs/feature_plan.csv``).
    where : str
        SQL filter on ``feats f``, for example ``"f.split = 'test'"``.
    sample_rows : int, optional
        A random sample of that size (the same rows for the same seed).

    Returns
    -------
    (pandas.DataFrame, pandas.DataFrame)
        The scored rows (ids, segments, outcome ``y`` and one ``p_*``
        column per model and benchmark) and the prepared feature rows.

    Notes
    -----
    The table ``storm_term`` (:func:`build_storm_term`) is joined when it
    exists; rows outside it get no storm wind.
    """
    spec = TARGETS[kind]
    needed = sorted(set().union(*(m["features"] for m in models.values())))
    seg_sql = ", ".join(f"{expr} AS {col}" for col, expr in spec["segment"].items())
    query = f"""
        SELECT f.period_id, f.policy_key, f.period_year, f.period_start, f.propertyState AS state,
               f.{spec['target']} AS y, {seg_sql},
               CASE WHEN f.zone_group = 'UNKNOWN' THEN 'D' ELSE f.zone_group END AS ev_zone_group,
               CAST(COALESCE(f.tenure_years >= 0 AND f.tenure_years < 1, FALSE) AS INTEGER) AS ev_new_business,
               CAST(COALESCE(f.mandatory_purchase, FALSE) AS INTEGER) AS ev_mandatory_purchase,
               CASE WHEN f.prev_gap_days IS NULL THEN 'first term on the key'
                    WHEN f.returned_after_lapse THEN 'returned after a gap'
                    ELSE 'continuous renewal' END AS history,
               {', '.join(f'f.{c}' for c in needed)}
        FROM feats f WHERE f.{spec['pop']} AND {where}"""
    if sample_rows:
        query = f"SELECT * FROM ({query}) USING SAMPLE reservoir({sample_rows} ROWS) REPEATABLE ({seed})"
    has_storm = con.execute("SELECT COUNT(*) FROM duckdb_tables() WHERE table_name = 'storm_term'").fetchall()[0][0] > 0
    storm_sql = "LEFT JOIN storm_term st USING (period_id)" if has_storm else ""
    query = f"""
        SELECT q.*, COALESCE(s.segment_rate, {base_rate}) AS p_segment,
               {'st.term_wind_kt' if has_storm else 'CAST(NULL AS DOUBLE) AS term_wind_kt'}
        FROM ({query}) q
        LEFT JOIN seg_{kind} s USING (seg_1, seg_2)
        {storm_sql}"""
    raw = con.sql(query).df()
    df = prepare(raw, needed, kinds)                      # a cleaned copy; raw stays as read
    out = df[["period_id", "policy_key", "period_year", "period_start", "state", "history", "y", "p_segment"]].copy()
    out["y"] = out["y"].astype("int8")
    out["zone_group"] = df["ev_zone_group"]
    out["new_business"] = df["ev_new_business"]
    out["mandatory_purchase"] = df["ev_mandatory_purchase"]
    out["storm_in_term"] = pd.cut(df["term_wind_kt"].astype("float64").fillna(0), [-1, 33.9, 63.9, 999],
                                  labels=["no storm wind", "tropical-storm wind (34-63 kt)", "hurricane wind (64+ kt)"])
    out["p_base"] = base_rate
    for name, m in models.items():
        out[f"p_{name}"] = predict(m, df[m["features"]], raw)
    return out, df


def score_split(con, kind, models, base_rate, kinds, split, n_chunks=8, seed=42, chunks=None):
    """Score every row of one split, ``n_chunks`` chunks at a time (``chunks``: only these chunk numbers)."""
    parts = [score_rows(con, kind, models, base_rate, kinds,
                        f"f.split = '{split}' AND hash(f.period_id || '{seed}') % {n_chunks} = {k}")[0]
             for k in (range(n_chunks) if chunks is None else chunks)]
    return pd.concat(parts, ignore_index=True)


def score_split_light(con, kind, models, base_rate, kinds, split, n_chunks=28, seed=42, chunks=None):
    """Score every row of a large split, keeping only the outcome and the probabilities.

    Like :func:`score_split`, but each chunk keeps just ``y`` (int8) and the
    ``p_*`` columns (float32), so all 27.5M training rows fit in about 0.5 GB.
    """
    cols = list(model_columns(models).values())
    parts = []
    for k in (range(n_chunks) if chunks is None else chunks):
        s, _ = score_rows(con, kind, models, base_rate, kinds,
                          f"f.split = '{split}' AND hash(f.period_id || '{seed}') % {n_chunks} = {k}")
        parts.append(pd.concat([s[["y"]], s[cols].astype("float32")], axis=1))
    return pd.concat(parts, ignore_index=True)


# ----------------------------------------------------------------------------- classification view (08a)
def f1_optimal_cutoff(y, p):
    """The cut-off that maximizes F1 on (y, p).

    >>> f1_optimal_cutoff(np.array([0, 0, 1, 1]), np.array([0.1, 0.2, 0.7, 0.8]))
    0.7
    """
    precision, recall, thresholds = precision_recall_curve(y, p)
    f1 = 2 * precision[:-1] * recall[:-1] / np.clip(precision[:-1] + recall[:-1], 1e-12, None)
    return float(thresholds[int(np.argmax(f1))])


def cutoff_rules(kind, train_scored, col):
    """The cut-off rules for one model, all set on training predictions.

    Returns
    -------
    dict of {str: float}
        ``"riskiest 10% in train"`` -> cut-off, ..., ``"F1-optimal in train"``
        and ``"p >= 0.5 (default)"``.
    """
    p = train_scored[col].to_numpy()
    rules = {f"riskiest {s:.0%} in train": float(np.quantile(p, 1 - s)) for s in TARGETS[kind]["flag_shares"]}
    rules["F1-optimal in train"] = f1_optimal_cutoff(train_scored["y"].to_numpy(), p)
    rules["p >= 0.5 (default)"] = 0.5
    return rules


def confusion_row(y, p, cut):
    """Confusion matrix and the classification measures at one cut-off (flag if p >= cut)."""
    y = np.asarray(y)
    # compare in float32: the cut-offs come from training scores kept as float32 (score_split_light), and a
    # float64 score equal to the cut-off (every row of a segment-rule segment) could otherwise fall just below it
    flagged = (np.asarray(p, dtype=np.float32) >= np.float32(cut)).astype(int)
    tn, fp, fn, tp = confusion_matrix(y, flagged, labels=[0, 1]).ravel()
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    return {"threshold": cut, "flagged_share": flagged.mean(), "TP": int(tp), "FP": int(fp), "FN": int(fn), "TN": int(tn),
            "accuracy": (tp + tn) / len(y), "precision": precision, "recall": recall,
            "f1": 2 * precision * recall / (precision + recall) if precision + recall else 0.0,
            "fpr": fp / (fp + tn) if fp + tn else 0.0, "lift": precision / y.mean() if y.mean() else np.nan}


def classification_table(pred, kind, cols, rules_by_model):
    """Every model (except the base rate) at every cut-off rule."""
    rows = []
    for name, col in cols.items():
        if name == "base rate":
            continue
        for rule, cut in rules_by_model[name].items():
            rows.append({"model": name, "rule": rule, **confusion_row(pred["y"], pred[col], cut)})
    return pd.DataFrame(rows)


def benchmark_classification(kind, scopes, cols, rules_by_model):
    """ROC-AUC and the measures at the main rule, for every model and scope (overall, each year)."""
    main = f"riskiest {TARGETS[kind]['main_share']:.0%} in train"
    rows = []
    for scope, part in scopes:
        for name, col in cols.items():
            row = {"scope": scope, "model": name, "rows": len(part), "events": int(part["y"].sum()),
                   "actual_rate": part["y"].mean(), "roc_auc": roc_auc_score(part["y"], part[col])}
            if name != "base rate":
                r = confusion_row(part["y"], part[col], rules_by_model[name][main])
                row.update({f"{k}": r[k] for k in ("flagged_share", "accuracy", "precision", "recall", "f1", "lift")})
            rows.append(row)
    return pd.DataFrame(rows), main


def split_comparison(kind, splits, cols, rules_by_model, rules=None):
    """Accuracy and the other classification measures of every model on each split (train, test, validation).

    Parameters
    ----------
    kind : {"claim", "lapse"}
    splits : dict of {str: pandas.DataFrame or None}
        Split label -> scored rows (``y`` and the ``p_*`` columns); ``None``
        for a split that stays closed (the validation split in the dry run).
    cols : dict
        Model name -> probability column (:func:`model_columns`).
    rules_by_model : dict
        Model name -> cut-off rules (:func:`cutoff_rules`).
    rules : list of str, optional
        Rules to show; default: the main rule and the default 0.5.

    Returns
    -------
    pandas.DataFrame
        One row per split, model and rule, with ``accuracy_if_all_no``: the
        accuracy of always predicting "no event" (1 - the actual rate).
    """
    rules = rules or [f"riskiest {TARGETS[kind]['main_share']:.0%} in train", "p >= 0.5 (default)"]
    out = []
    for label, frame in splits.items():
        for name, col in cols.items():
            if name == "base rate":
                continue
            for rule in rules:
                row = {"split": label, "model": name, "rule": rule}
                if frame is None or col not in frame:
                    row["note"] = "closed until the final run" if frame is None else "no saved predictions"
                else:
                    r = confusion_row(frame["y"], frame[col], rules_by_model[name][rule])
                    row.update({"rows": len(frame), "actual_rate": frame["y"].mean(),
                                "accuracy_if_all_no": 1 - frame["y"].mean(), "roc_auc": roc_auc_score(frame["y"], frame[col]),
                                **{k: r[k] for k in ("flagged_share", "accuracy", "precision", "recall", "f1")}})
                out.append(row)
    return pd.DataFrame(out)


def saved_test_results(kind, reports_dir, skip=()):
    """The test (2021-2022) results that notebooks 07a/07b saved for models that are not scored here.

    Reads ``<reports_dir>/<kind>_<name>_test_confusion.csv`` and, when
    present, ``<kind>_<name>_test_auc.csv`` (model x scope) and
    ``<kind>_<name>_run.json``. Baselines and the names in ``skip`` (the
    models 08a scores itself) are left out.

    Returns
    -------
    dict of {str: dict}
        Model file name -> ``{"confusion": DataFrame, "auc": DataFrame or None, "run": dict}``.
    """
    out = {}
    for f in sorted(reports_dir.glob(f"{kind}_*_test_confusion.csv")):
        name = f.name[: -len("_test_confusion.csv")]
        if "baseline" in name or name in skip:
            continue
        auc_file, run_file = reports_dir / f"{name}_test_auc.csv", reports_dir / f"{name}_run.json"
        out[name] = {"confusion": pd.read_csv(f),
                     "auc": pd.read_csv(auc_file, index_col=0) if auc_file.exists() else None,
                     "run": json.loads(run_file.read_text()) if run_file.exists() else {}}
    return out


# ----------------------------------------------------------------------------- probability view (08b)
def prob_scores(y, p):
    """Rows, events, actual and predicted rate, A/E, log loss and Brier score."""
    y, p = np.asarray(y), np.clip(np.asarray(p, dtype="float64"), 1e-7, 1 - 1e-7)
    return {"rows": len(y), "events": int(y.sum()), "actual_rate": y.mean(), "predicted_rate": p.mean(),
            "a_to_e": y.sum() / p.sum(), "log_loss": log_loss(y, p, labels=[0, 1]), "brier": brier_score_loss(y, p)}


def add_skill(table, by):
    """Log-loss and Brier skill over the base-rate row of the same group ``by``."""
    base = table[table["model"] == "base rate"].set_index(by)
    for m in ("log_loss", "brier"):
        table[f"{m}_skill"] = 1 - table[m] / table[by].map(base[m])
    return table


def probability_table(scopes, cols):
    """:func:`prob_scores` and skill for every model and scope."""
    rows = [{"scope": scope, "model": name, **prob_scores(part["y"], part[col])}
            for scope, part in scopes for name, col in cols.items()]
    return add_skill(pd.DataFrame(rows), "scope")


def calibration_deciles(frame, col, n_bins=10):
    """Mean predicted vs actual rate in each decile of the predicted probability (10 = highest)."""
    d = pd.qcut(frame[col].rank(method="first"), n_bins, labels=range(1, n_bins + 1)).astype(int)
    t = frame.groupby(d).agg(rows=("y", "size"), events=("y", "sum"), predicted=(col, "mean"), actual=("y", "mean"))
    t["actual_to_predicted"] = t["actual"] / t["predicted"]
    return t


def logit(p):
    p = np.clip(np.asarray(p, dtype="float64"), 1e-7, 1 - 1e-7)
    return np.log(p / (1 - p))


def calibration_summary(frame, cols, seed=42, max_rows=1_000_000):
    """A/E, ECE and the calibration intercept and slope of every model (except the base rate)."""
    from sklearn.linear_model import LogisticRegression
    sample = frame.sample(min(len(frame), max_rows), random_state=seed)
    rows, deciles = [], {}
    for name, col in cols.items():
        if name == "base rate":
            continue
        t = calibration_deciles(frame, col)
        deciles[name] = t
        ece = float((t["rows"] / t["rows"].sum() * (t["actual"] - t["predicted"]).abs()).sum())
        fit = LogisticRegression(penalty=None).fit(logit(sample[col]).reshape(-1, 1), sample["y"])
        rows.append({"model": name, "a_to_e": frame["y"].sum() / frame[col].sum(), "ece": ece,
                     "ece_pct_of_rate": ece / frame["y"].mean(), "intercept_a": fit.intercept_[0], "slope_b": fit.coef_[0, 0]})
    return pd.DataFrame(rows), deciles


def segment_table(frame, cols, key):
    """:func:`prob_scores` and skill for every model in every group of ``key`` (a Series aligned with frame)."""
    g = key.astype("string").fillna("(missing)")
    rows = [{"segment": s, "model": name, **prob_scores(part["y"], part[col])}
            for s, part in frame.groupby(g, observed=True) for name, col in cols.items()]
    return add_skill(pd.DataFrame(rows), "segment")


def psi(ref, cur, numeric, n_bins=10, top=15, eps=1e-4):
    """Population stability index of ``cur`` against ``ref``.

    Numbers: 10 quantile bins of the reference; categories: the 15 largest
    plus "other"; missing values are a bin of their own.

    >>> round(psi(pd.Series(range(1000)), pd.Series(range(1000)), numeric=True), 6)
    0.0
    """
    ref, cur = pd.Series(ref), pd.Series(cur)
    if numeric:
        r = pd.to_numeric(ref, errors="coerce").astype("float64")
        c = pd.to_numeric(cur, errors="coerce").astype("float64")
        edges = np.unique(np.nanquantile(r, np.linspace(0, 1, n_bins + 1))[1:-1]) if r.notna().any() else np.array([])
        rb = pd.Series(np.where(r.isna(), -1, np.digitize(r.fillna(0), edges)))
        cb = pd.Series(np.where(c.isna(), -1, np.digitize(c.fillna(0), edges)))
    else:
        r, c = ref.astype("string").fillna("(missing)"), cur.astype("string").fillna("(missing)")
        keep = r.value_counts().index[:top]
        rb, cb = r.where(r.isin(keep), "other"), c.where(c.isin(keep), "other")
    p, q = rb.value_counts(normalize=True), cb.value_counts(normalize=True)
    idx = p.index.union(q.index)
    p, q = p.reindex(idx, fill_value=0) + eps, q.reindex(idx, fill_value=0) + eps
    return float(((q - p) * np.log(q / p)).sum())


def row_log_loss(y, p):
    p = np.clip(np.asarray(p, dtype="float64"), 1e-7, 1 - 1e-7)
    y = np.asarray(y)
    return -(y * np.log(p) + (1 - y) * np.log(1 - p))


def bootstrap_log_loss(pred, cols, primary_col, n_boot=200, seed=42):
    """Paired Poisson bootstrap of the log-loss difference (other - primary), resampling policies."""
    y = pred["y"].to_numpy()
    others = {n: c for n, c in cols.items() if c != primary_col}
    ll_primary = row_log_loss(y, pred[primary_col])
    diffs = pd.DataFrame({n: row_log_loss(y, pred[c]) - ll_primary for n, c in others.items()})
    diffs["n"] = 1
    by_policy = diffs.groupby(pred["policy_key"].to_numpy()).sum()
    D, n = by_policy[list(others)].to_numpy(), by_policy["n"].to_numpy()
    rng = np.random.default_rng(seed)
    stats = np.empty((n_boot, len(others)))
    for b in range(n_boot):
        w = rng.poisson(1.0, len(n))
        stats[b] = (w @ D) / (w @ n)
    rows = []
    for j, name in enumerate(others):
        est = D[:, j].sum() / n.sum()
        lo, hi = np.percentile(stats[:, j], [2.5, 97.5])
        rows.append({"compared_with": name, "policies": len(n), "difference": est, "ci_low": lo, "ci_high": hi,
                     "primary_better": "yes" if lo > 0 else ("no" if hi < 0 else "not sure")})
    return pd.DataFrame(rows)


# ----------------------------------------------------------------------------- cohort survival (08b, lapse)
def build_cohort_keys(con, last_year, n_keys, seed=42):
    """Temporary table ``cohort_keys``: a sample of new-business policies per cohort year (2014 to ``last_year``).

    New business = tenure under one year and no earlier policy-year on the
    same key (so a return after a lapse is not counted as new).
    """
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE cohort_keys AS
        WITH entry AS (
            SELECT policy_key, MIN(period_start) AS entry_start
            FROM feats
            WHERE in_pop_lapse AND tenure_years >= 0 AND tenure_years < 1 AND period_year BETWEEN 2014 AND {last_year}
            GROUP BY policy_key
        ), clean AS (
            SELECT e.policy_key, e.entry_start, year(e.entry_start) AS cohort
            FROM entry e
            WHERE NOT EXISTS (SELECT 1 FROM feats f WHERE f.policy_key = e.policy_key AND f.period_start < e.entry_start)
        )
        SELECT * FROM clean
        QUALIFY row_number() OVER (PARTITION BY cohort ORDER BY hash(CAST(policy_key AS VARCHAR) || '{seed}')) <= {n_keys}""")
    return sorted(r[0] for r in con.execute("SELECT DISTINCT cohort FROM cohort_keys").fetchall())


def life_table(frame, curves, min_at_risk=500):
    """Flow table by year since new business: at risk, lapsed (cancelled / not renewed), censored, hazard, survival.

    ``curves``: name -> probability column; each gives a predicted hazard
    (the mean prediction over the policies at risk) and survival.
    """
    t = frame.groupby("year_k").agg(at_risk=("y", "size"), lapsed=("y", "sum"), cancelled=("cancelled", "sum"),
                                    not_renewed=("not_renewed", "sum"),
                                    **{f"hazard_{n}": (c, "mean") for n, c in curves.items()})
    t = t[t["at_risk"] >= min_at_risk]
    t["censored"] = t["at_risk"] - t["lapsed"] - t["at_risk"].shift(-1, fill_value=0)
    t["hazard_actual"] = t["lapsed"] / t["at_risk"]
    for n in ["actual", *curves]:
        t[f"survival_{n}"] = (1 - t[f"hazard_{n}"]).cumprod()
    return t


# ----------------------------------------------------------------------------- charts
def style_axes(ax, grid="both"):
    ax.spines[["top", "right"]].set_visible(False)
    ax.grid(axis=grid, color="#e6e6e6")
    ax.set_axisbelow(True)


def model_style(name, i):
    """Line style of a model: grey for the segment rule, a colour per model otherwise."""
    if name == "segment rate":
        return dict(color=GREY, linewidth=1.5)
    return dict(color=MODEL_COLORS[(i - 1) % len(MODEL_COLORS)], linewidth=2)


def plot_confusion(ax, row, event):
    """A 2 x 2 confusion matrix, shaded by the share of each actual row."""
    m = np.array([[row["TN"], row["FP"]], [row["FN"], row["TP"]]])
    ax.imshow(m / m.sum(axis=1, keepdims=True), cmap="Blues", vmin=0, vmax=1)
    for a in range(2):
        for b in range(2):
            share = m[a, b] / m[a].sum()
            ax.text(b, a, f"{m[a, b]:,}\n({share:.1%} of row)", ha="center", va="center", fontsize=10,
                    color="white" if share > 0.6 else "#333333")
    ax.set_xticks([0, 1], ["flagged: no", "flagged: yes"])
    ax.set_yticks([0, 1], [f"actual: no {event}", f"actual: {event}"])
    ax.set_title(f"{row['rule']}\nprecision {row['precision']:.1%}, recall {row['recall']:.1%}, "
                 f"F1 {row['f1']:.3f}, accuracy {row['accuracy']:.1%}", fontsize=9)


def plot_roc(ax, pred, cols, rules_table, title):
    """ROC curves of every model (except the base rate), with the primary model's cut-off rules marked."""
    ax.plot([0, 1], [0, 1], color=GREY, linewidth=1, linestyle="--", label="random order (AUC 0.500)")
    for i, (name, col) in enumerate(cols.items()):
        if name == "base rate":
            continue
        fpr, tpr, _ = roc_curve(pred["y"], pred[col])
        ax.plot(fpr, tpr, label=f"{name} (AUC {roc_auc_score(pred['y'], pred[col]):.3f})", **model_style(name, i))
    t = rules_table[rules_table["rule"] != "p >= 0.5 (default)"]
    ax.scatter(t["fpr"], t["recall"], color="#d9822b", s=50, zorder=3, edgecolor="white", label="cut-off rules")
    for _, r in t.iterrows():
        ax.annotate(r["rule"].replace(" in train", ""), (r["fpr"], r["recall"]), textcoords="offset points",
                    xytext=(8, -12), fontsize=8)
    ax.set(title=title, xlabel="false positive rate", ylabel="true positive rate (recall)", xlim=(0, 1), ylim=(0, 1))
    ax.legend(frameon=False, fontsize=8, loc="lower right")
    style_axes(ax)


def plot_calibration(ax, deciles, title, event):
    """Mean predicted vs actual rate by decile, for every model in ``deciles``."""
    from matplotlib.ticker import PercentFormatter
    lim = max(max(t["predicted"].max(), t["actual"].max()) for t in deciles.values()) * 1.08
    ax.plot([0, lim], [0, lim], color=GREY, linewidth=1, linestyle="--", label="perfect calibration")
    for i, (name, t) in enumerate(deciles.items()):
        style = model_style(name, i)
        ax.plot(t["predicted"], t["actual"], marker="s" if name == "segment rate" else "o", markersize=6,
                markeredgecolor="white", label=name, **style)
    ax.set(title=title, xlabel=f"mean predicted {event} probability", ylabel=f"actual {event} rate", xlim=(0, lim), ylim=(0, lim))
    ax.xaxis.set_major_formatter(PercentFormatter(1, decimals=1))
    ax.yaxis.set_major_formatter(PercentFormatter(1, decimals=1))
    ax.legend(frameon=False, fontsize=8)
    style_axes(ax)


def plot_ae_by_segment(ax, seg_tables, model, title):
    """A/E of one model by segment with a 95% Poisson interval; ``seg_tables``: label -> segment table."""
    labels, ae, lo, hi, ypos, y0, heads = [], [], [], [], [], 0, []
    for label, t in seg_tables.items():
        heads.append((y0, label))
        for _, r in t[t["model"] == model].iterrows():
            half = 1.96 / np.sqrt(max(r["events"], 1))
            labels.append(str(r["segment"])); ae.append(r["a_to_e"])
            lo.append(r["a_to_e"] * (1 - half)); hi.append(r["a_to_e"] * (1 + half))
            ypos.append(y0); y0 += 1
        y0 += 1
    ax.axvspan(0.8, 1.2, color="#eef3f9", zorder=0)
    ax.axvline(1, color="#555555", linewidth=1)
    ax.hlines(ypos, lo, hi, color="#2a6fb0", linewidth=2)
    ax.plot(ae, ypos, "o", color="#2a6fb0", markersize=7, markeredgecolor="white")
    ax.set_yticks(ypos, labels, fontsize=8)
    for y, label in heads:
        ax.annotate(label, xy=(0, y - 0.9), xycoords=("axes fraction", "data"), fontsize=8, fontweight="bold", color="#555555")
    ax.invert_yaxis()
    ax.set(title=title, xlabel="A/E (1 = right on average; band = 0.8-1.2)")
    style_axes(ax, grid="x")


def plot_by_year(axes, stability, cols, title):
    """A/E (top axis) and log-loss skill (bottom axis) per year, with the periods shaded."""
    from matplotlib.patches import Patch
    from matplotlib.ticker import PercentFormatter
    for ax, (metric, ylabel) in zip(axes, [("a_to_e", "A/E (actual / expected)"),
                                           ("log_loss_skill", "log-loss skill over base rate")]):
        for period, color in PERIOD_COLORS.items():
            yrs = stability.loc[stability["period"] == period, "year"]
            if len(yrs):
                ax.axvspan(yrs.min() - 0.5, yrs.max() + 0.5, color=color, zorder=0)
        for i, name in enumerate(cols):
            if name == "base rate":
                continue
            s = stability[stability["model"] == name]
            ax.plot(s["year"], s[metric], marker="s" if name == "segment rate" else "o", markersize=5,
                    markeredgecolor="white", label=name, **model_style(name, i))
        ax.axhline(1 if metric == "a_to_e" else 0, color="#555555", linewidth=1)
        ax.set(ylabel=ylabel)
        if metric == "log_loss_skill":
            ax.yaxis.set_major_formatter(PercentFormatter(1, decimals=0))
        style_axes(ax, grid="y")
    axes[0].set_title(title)
    axes[-1].set(xlabel="policy year (2024: January to August)")
    axes[-1].set_xticks(sorted(stability["year"].unique()))
    handles = axes[0].get_legend_handles_labels()[0]
    handles += [Patch(color=c, label=p) for p, c in PERIOD_COLORS.items() if p in set(stability["period"])]
    axes[0].legend(handles=handles, frameon=False, fontsize=8, loc="upper left")


def plot_psi_heatmap(ax, table, title, top=15):
    """The ``top`` features with the largest PSI in any quarter, quarter by quarter."""
    t = table.loc[table.max(axis=1).sort_values(ascending=False).index[:top]]
    im = ax.imshow(t.to_numpy(), cmap="Blues", vmin=0, vmax=0.5, aspect="auto")
    ax.set_xticks(range(t.shape[1]), t.columns, rotation=90, fontsize=8)
    ax.set_yticks(range(t.shape[0]), t.index, fontsize=8)
    for i in range(t.shape[0]):
        for j in range(t.shape[1]):
            v = t.iat[i, j]
            ax.text(j, i, f"{v:.2f}", ha="center", va="center", fontsize=6.5, color="white" if v > 0.3 else "#333333")
    ax.set_title(title)
    return im


# ----------------------------------------------------------------------------- stability and monitoring (08b)
def stability_by_year(con, kind, models, base_rate, kinds, years, rows_per_year, seed=42):
    """A/E and skill of every model per policy year, on a random sample of each year."""
    cols = model_columns(models)
    out = []
    for y in years:
        s, _ = score_rows(con, kind, models, base_rate, kinds,
                          f"f.split = '{split_of_year(y)}' AND f.period_year = {y}", sample_rows=rows_per_year, seed=seed)
        for name, col in cols.items():
            out.append({"year": y, "period": period_of_year(y), "model": name, **prob_scores(s["y"], s[col])})
    t = pd.DataFrame(out)
    base = t[t["model"] == "base rate"].set_index("year")
    for m in ("log_loss", "brier"):
        t[f"{m}_skill"] = 1 - t[m] / t["year"].map(base[m])
    return t


def monitor_quarters(con, kind, models, base_rate, kinds, primary, quarters, reference_rows, quarter_rows, seed=42):
    """Feature PSI, score PSI, A/E and skill of the primary model per quarter.

    The reference for each quarter is a sample of the same calendar quarter
    in 2014-2020, because several features are seasonal.

    Returns
    -------
    (pandas.DataFrame, pandas.DataFrame)
        One row per quarter, and the feature x quarter PSI table.
    """
    col, feats = f"p_{primary}", models[primary]["features"]
    refs = {q: score_rows(con, kind, models, base_rate, kinds, f"f.split = 'train' AND quarter(f.period_start) = {q}",
                          sample_rows=reference_rows, seed=seed) for q in range(1, 5)}
    rows, psi_rows = [], {}
    for y, q in quarters:
        ref_scored, ref_raw = refs[q]
        where = f"f.split = '{split_of_year(y)}' AND year(f.period_start) = {y} AND quarter(f.period_start) = {q}"
        cur_scored, cur_raw = score_rows(con, kind, models, base_rate, kinds, where, sample_rows=quarter_rows, seed=seed)
        label = f"{y}Q{q}"
        psi_rows[label] = {f: psi(ref_raw[f], cur_raw[f], numeric=kinds.get(f) == "num") for f in feats}
        s = prob_scores(cur_scored["y"], cur_scored[col])
        rows.append({"quarter": label, "rows": len(cur_scored),
                     "score_psi": psi(ref_scored[col], cur_scored[col], numeric=True),
                     "max_feature_psi": max(psi_rows[label].values()),
                     "features_over_0.25": sum(v > 0.25 for v in psi_rows[label].values()),
                     "actual": s["events"], "expected": s["predicted_rate"] * s["rows"], "a_to_e": s["a_to_e"],
                     "log_loss_skill": 1 - s["log_loss"] / prob_scores(cur_scored["y"], cur_scored["p_base"])["log_loss"]})
    return pd.DataFrame(rows), pd.DataFrame(psi_rows)


def apply_alert_rules(monitor, rolling_quarters=None):
    """Status and action of every quarter under the rules of 08b (PSI, A/E bands, skill).

    ``rolling_quarters``: read the A/E over this many rolling quarters (claims: 4); ``None`` = quarterly.
    """
    m = monitor.copy()
    if rolling_quarters:
        m["a_to_e_used"] = (m["actual"].rolling(rolling_quarters, min_periods=1).sum()
                            / m["expected"].rolling(rolling_quarters, min_periods=1).sum())
    else:
        m["a_to_e_used"] = m["a_to_e"]

    def status(r):
        flags = []
        if r["max_feature_psi"] > 0.25: flags.append("feature PSI > 0.25")
        if r["score_psi"] > 0.25: flags.append("score PSI > 0.25")
        elif r["score_psi"] > 0.10: flags.append("score PSI watch")
        if not 0.8 <= r["a_to_e_used"] <= 1.2: flags.append("A/E outside 0.8-1.2")
        elif not 0.9 <= r["a_to_e_used"] <= 1.1: flags.append("A/E watch")
        if r["log_loss_skill"] < 0: flags.append("skill < 0")
        return "; ".join(flags) or "ok"

    m["status"] = m.apply(status, axis=1)
    out_of_band = ~m["a_to_e_used"].between(0.8, 1.2)
    m["action"] = np.where(out_of_band & out_of_band.shift(fill_value=False), "recalibrate", "")
    m.loc[(m["log_loss_skill"] < 0) & (m["log_loss_skill"].shift() < 0), "action"] = "retrain"
    return m


def load_saved_test(pred_dir, models):
    """The test predictions saved by notebooks 06/07 (``<model>_test.parquet``), merged on ``period_id``.

    Returns
    -------
    pandas.DataFrame or None
        ``period_id``, ``y``, ``p_base``, ``p_segment`` and one ``p_<model>``
        column per model that has a saved file; ``None`` if there is none.
    """
    frames = {}
    for name in models:
        f = pred_dir / f"{name}_test.parquet"
        if f.exists():
            t = pd.read_parquet(f)
            cands = [c for c in t.columns if c.startswith("p_") and c not in ("p_base", "p_segment")]
            # the column of the saved model: the calibrated one if its calibrator is used, otherwise the last other one
            # (07a claim files also carry the logistic regression's p_logreg next to their own column: skip it)
            if "logreg" not in name:
                cands = [c for c in cands if c != "p_logreg"] or cands
            cal = [c for c in cands if c.endswith("_cal")]
            uses_cal = models[name].get("calibrator") is not None and models[name].get("use_calibrator", True)
            plain = [c for c in cands if not c.endswith("_cal")] or cands
            col = cal[-1] if cal and uses_cal else plain[-1]
            chosen = models[name].get("run", {}).get("selected_prediction_column")    # 07b XGBoost records it
            if chosen in cands:
                col = chosen
            frames[name] = t.rename(columns={col: f"p_{name}"})
    if not frames:
        return None
    # the benchmarks come from a file that has both (06a/06b do; the 07a claim files have no p_base)
    base = next((f for f in frames.values() if {"p_base", "p_segment"} <= set(f.columns)), next(iter(frames.values())))
    out = base[["period_id", "y"] + [c for c in ("p_base", "p_segment") if c in base.columns]]
    for name, f in frames.items():
        out = out.merge(f[["period_id", f"p_{name}"]], on="period_id", how="inner")
    return out
