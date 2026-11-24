"""Excel workbooks of the evaluation results (notebooks 08a and 08b).

The notebooks save their tables as CSV files and their charts as PNG files in
``reports/``. :func:`write_08a_workbook` and :func:`write_08b_workbook`
collect one run of each notebook into a formatted workbook: a Contents tab,
one tab per table and one tab per group of charts. The cells hold values
(not formulas), exactly as in the CSV files.

>>> from src.paths import REPORTS
>>> write_08a_workbook(REPORTS, "valid")      # doctest: +SKIP
>>> write_08b_workbook(REPORTS, "valid")      # doctest: +SKIP
"""
import json
import re
from pathlib import Path

import pandas as pd
from openpyxl import Workbook
from openpyxl.drawing.image import Image
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

# Display names of the models and benchmarks
MODEL_NAMES = {"claim_baseline_logreg": "logistic regression", "claim_rf": "random forest", "claim_xgb": "XGBoost",
               "claim_lgbm": "LightGBM", "lapse_baseline_logreg": "logistic regression", "lapse_rf": "random forest",
               "lapse_xgb": "XGBoost", "segment rate": "segment rule"}

# Column headers shown in the workbooks
LABELS = {
    "scope": "Scope", "model": "Model", "rows": "Policy-years", "events": "Events", "actual_rate": "Actual rate",
    "roc_auc": "ROC-AUC", "flagged_share": "Flagged share", "accuracy": "Accuracy", "precision": "Precision",
    "recall": "Recall", "f1": "F1", "lift": "Lift", "rule": "Cut-off rule", "threshold": "Cut-off (probability)",
    "cut_off": "Cut-off (probability)", "TP": "TP", "FP": "FP", "FN": "FN", "TN": "TN", "fpr": "False positive rate",
    "split": "Split", "accuracy_if_all_no": "Accuracy if all 'no'", "target": "Target",
    "predicted_rate": "Predicted rate", "a_to_e": "A/E", "log_loss": "Log loss", "brier": "Brier",
    "log_loss_skill": "Log loss skill", "brier_skill": "Brier skill", "ece": "ECE", "ece_pct_of_rate": "ECE % of rate",
    "intercept_a": "Calibration intercept", "slope_b": "Calibration slope", "decile": "Decile (10 = riskiest)",
    "predicted": "Predicted rate", "actual": "Actual", "actual_to_predicted": "Actual ÷ predicted",
    "period_year": "Year", "segment_type": "Segment type", "segment": "Segment", "year": "Year", "period": "Period",
    "quarter": "Quarter", "score_psi": "Score PSI", "max_feature_psi": "Largest feature PSI",
    "features_over_0.25": "Features with PSI > 0.25", "expected": "Expected", "a_to_e_used": "A/E used for the alert",
    "status": "Status", "action": "Action", "feature": "Feature", "cohort": "Cohort (year sold)",
    "year_k": "Policy year k", "at_risk": "At risk", "lapsed": "Lapsed", "cancelled": "Cancelled",
    "not_renewed": "Not renewed", "censored": "Censored", "hazard_actual": "Lapse hazard, actual",
    "hazard_model": "Lapse hazard, model", "hazard_segment rule": "Lapse hazard, segment rule",
    "survival_actual": "Retention, actual", "survival_model": "Retention, model",
    "survival_segment rule": "Retention, segment rule", "gap_model_pp": "Gap model − actual (pp)",
    "gap_segment_pp": "Gap segment rule − actual (pp)", "compared_with": "Primary model compared with",
    "policies": "Policies", "difference": "Difference (other − primary log loss)", "ci_low": "95% CI low",
    "ci_high": "95% CI high", "primary_better": "Primary better?",
}
_COUNTS = {"rows", "events", "TP", "FP", "FN", "TN", "at_risk", "lapsed", "cancelled", "not_renewed", "censored",
           "policies", "features_over_0.25", "decile", "year_k", "period_year", "year", "cohort"}

SPLIT_TEXT = {"test": ("test (OOT1, 2021–2022)", "test run", "test"),
              "valid": ("validation (OOT2, 2023 – Aug 2024)", "validation run", "validation")}
SPLITS = "Train 2014–2020; test (OOT1) 2021–2022; validation (OOT2) 2023 – Aug 2024"

_ARIAL = "Arial"
_HEAD = PatternFill("solid", fgColor="1F3864")
_LINE = Side(style="thin", color="BFBFBF")


# ----------------------------------------------------------------------------- formatting
def _label(col):
    c = str(col)
    if c.startswith("expected: "):
        return "Expected, " + c[len("expected: "):]
    return LABELS.get(c, c.replace("_", " "))


def _number_format(col, values):
    """Excel number format of a numeric column, from its name."""
    c = str(col)
    if c in _COUNTS or c.startswith("expected") or (c == "actual" and values.abs().max() > 50):
        return "#,##0"
    if "skill" in c:
        return "+0.0%;-0.0%;0.0%"
    if c == "roc_auc":
        return "0.000"
    if c in ("threshold", "cut_off", "log_loss", "brier", "ece", "difference", "ci_low", "ci_high"):
        return "0.00000"
    if c in ("a_to_e", "a_to_e_used", "lift", "actual_to_predicted", "intercept_a", "slope_b",
             "gap_model_pp", "gap_segment_pp") or "psi" in c or re.fullmatch(r"\d{4}Q\d", c):
        return "0.00"
    if c == "actual" or any(k in c for k in ("rate", "share", "accuracy", "precision", "recall", "f1", "fpr",
                                             "survival", "hazard", "predicted", "pct")):
        return "0.00%" if values.abs().max() < 0.1 else "0.0%"
    return None


def _read(path):
    """A saved table, with readable model names and without pandas' unnamed index columns."""
    df = pd.read_csv(path)
    first = df.columns[0]
    if str(first).startswith("Unnamed"):                       # an index saved without a name (feature PSI)
        df = df.rename(columns={first: "feature"})
    df = df[[c for c in df.columns if not str(c).startswith("Unnamed")]]   # inner index levels (by segment)
    for c in ("model", "compared_with"):
        if c in df:
            df[c] = df[c].map(lambda m: MODEL_NAMES.get(m, m))
    df.columns = [re.sub(r"^expected: (.*)$", lambda m: "expected: " + MODEL_NAMES.get(m.group(1), m.group(1)), str(c))
                  for c in df.columns]
    return df


# ----------------------------------------------------------------------------- sheets
def _table_sheet(wb, tab, df, step, title, note):
    ws = wb.create_sheet(tab)
    ws["A1"] = title
    ws["A1"].font = Font(name=_ARIAL, size=13, bold=True)
    ws["A2"] = f"Step {step}. {note}"
    ws["A2"].font = Font(name=_ARIAL, size=9, italic=True, color="595959")
    head = 4
    for j, c in enumerate(df.columns, start=1):
        cell = ws.cell(head, j, _label(c))
        cell.font = Font(name=_ARIAL, bold=True, color="FFFFFF", size=10)
        cell.fill = _HEAD
        cell.alignment = Alignment(wrap_text=True, vertical="center", horizontal="center")
    ws.row_dimensions[head].height = 42
    formats = {c: _number_format(c, df[c]) if pd.api.types.is_numeric_dtype(df[c]) else None for c in df.columns}
    for i, row in enumerate(df.itertuples(index=False), start=head + 1):
        for j, (c, v) in enumerate(zip(df.columns, row), start=1):
            cell = ws.cell(i, j, None if pd.isna(v) else v)
            cell.font = Font(name=_ARIAL, size=10)
            cell.border = Border(bottom=_LINE)
            if formats[c]:
                cell.number_format = formats[c]
    for j, c in enumerate(df.columns, start=1):
        widest = max([len(f"{x:.4f}") if isinstance(x, float) else len(str(x)) for x in df[c].tolist()]
                     + [min(len(_label(c)), 16)])
        ws.column_dimensions[get_column_letter(j)].width = max(9, min(widest + 2, 42))
    ws.freeze_panes = ws.cell(head + 1, 2 if len(df.columns) > 8 else 1)
    ws.auto_filter.ref = f"A{head}:{get_column_letter(len(df.columns))}{head + len(df)}"


def _chart_sheet(wb, tab, figures):
    ws = wb.create_sheet(tab)
    ws["A1"] = tab
    ws["A1"].font = Font(name=_ARIAL, size=13, bold=True)
    row = 3
    for path, caption in figures:
        if not path.exists():
            continue
        ws.cell(row, 1, caption).font = Font(name=_ARIAL, bold=True)
        img = Image(str(path))
        scale = 950 / img.width
        img.width, img.height = img.width * scale, img.height * scale
        ws.add_image(img, f"A{row + 1}")
        row += int(img.height / 20) + 4


def _contents_sheet(wb, title, info, tabs):
    ws = wb.active
    ws.title = "Contents"
    ws["A1"] = title
    ws["A1"].font = Font(name=_ARIAL, size=14, bold=True)
    r = 3
    for key, value in info:
        ws.cell(r, 1, key).font = Font(name=_ARIAL, bold=True, size=10)
        ws.cell(r, 2, value).font = Font(name=_ARIAL, size=10)
        r += 1
    r += 1
    for j, h in enumerate(["Tab", "What it shows", "How to read it", "Notebook step"], start=1):
        cell = ws.cell(r, j, h)
        cell.font = Font(name=_ARIAL, bold=True, color="FFFFFF")
        cell.fill = _HEAD
    for tab, step, title_, note in tabs:
        r += 1
        cell = ws.cell(r, 1, tab)
        cell.hyperlink = f"#'{tab}'!A1"
        cell.font = Font(name=_ARIAL, size=10, color="0563C1", underline="single")
        cell.alignment = Alignment(vertical="top")
        for j, v in enumerate([title_, note, step], start=2):
            c = ws.cell(r, j, v)
            c.font = Font(name=_ARIAL, size=10)
            c.alignment = Alignment(wrap_text=True, vertical="top")
    for col, width in zip("ABCD", (26, 60, 85, 14)):
        ws.column_dimensions[col].width = width


def _build(reports_dir, prefix, out, title, info, tabs, figures):
    """Write the workbook. ``tabs``: (tab, csv stem, step, title, note); missing CSV files are skipped."""
    tabs = [t for t in tabs if (reports_dir / f"{prefix}_{t[1]}.csv").exists()]
    wb = Workbook()
    _contents_sheet(wb, title, info,
                    [(t[0], t[2], t[3], t[4]) for t in tabs]
                    + [(name, "-", f"Charts saved by the notebook ({name.lower()})", "See each chart's caption.")
                       for name in figures])
    for tab, stem, step, ttl, note in tabs:
        _table_sheet(wb, tab, _read(reports_dir / f"{prefix}_{stem}.csv"), step, ttl, note)
    for name, figs in figures.items():
        _chart_sheet(wb, name, [(reports_dir / f"fig_{prefix}_{stem}.png", cap) for stem, cap in figs])
    wb.save(out)
    return out


def _run_info(reports_dir, split, notebook):
    prefix = f"eval_{split}"
    run = json.loads((reports_dir / f"{prefix}_08a_run.json").read_text())   # 08b reads the same predictions
    name = lambda m: MODEL_NAMES.get(m, m)
    return run, [
        ("Source", f'{notebook} run with EVAL_SPLIT = "{split}": every table is its CSV in reports/{prefix}_* (values, not formulas)'),
        ("Predictions scored at", run.get("scored_at", "?") + " (08a)"),
        ("Evaluated", SPLIT_TEXT[split][0]),
        ("Splits", SPLITS),
        ("Primary models", f"claim: {name(run['primary']['claim'])}; lapse: {name(run['primary']['lapse'])}"),
        ("Training base rates", f"claim {run['base_rate_train']['claim']:.2%}; lapse {run['base_rate_train']['lapse']:.2%}"),
        ("Model names", "segment rule = the training claim/lapse rate of each segment (benchmark); "
                        "base rate = the training rate for everyone"),
    ]


# ----------------------------------------------------------------------------- 08a
def write_08a_workbook(reports_dir, split, out_name=None):
    """Collect the tables and charts of one 08a run into an Excel workbook.

    Parameters
    ----------
    reports_dir : pathlib.Path
        The ``reports/`` folder, where 08a saved ``eval_<split>_*.csv`` and
        ``fig_eval_<split>_*.png``.
    split : {"test", "valid"}
        The ``EVAL_SPLIT`` of the run.
    out_name : str, optional
        File name of the workbook. Default:
        ``"08a Evaluation Classification Results (test run).xlsx"`` or
        ``"... (validation run).xlsx"``.

    Returns
    -------
    pathlib.Path
        The workbook written.
    """
    reports_dir = Path(reports_dir)
    prefix = f"eval_{split}"
    period, run_label, split_word = SPLIT_TEXT[split]
    out = reports_dir / (out_name or f"08a Evaluation Classification Results ({run_label}).xlsx")
    _, info = _run_info(reports_dir, split, "08a")

    def flagged(kind, rule):
        cm = pd.read_csv(reports_dir / f"{prefix}_{kind}_confusion.csv")
        row = cm[(cm["model"] == "segment rate") & (cm["rule"] == rule)]
        return float(row["flagged_share"].iloc[0]) if len(row) else float("nan")

    info.append(("Segment rule note",
                 "Every policy in a segment has the same score, so the segment rule's 'riskiest X% in train' cut-off can "
                 f"flag far more than X% (claims: {flagged('claim', 'riskiest 10% in train'):.1%} of the {split_word} "
                 f"policy-years at the 10% rule; lapse: {flagged('lapse', 'riskiest 20% in train'):.1%} at the 20% rule). "
                 "Compare it with the models at the same list size (Steps 3.2 / 4.2)."))
    tabs = [
        ("Cut-offs", "cutoffs", "2.2", "Cut-offs, set on the training split and frozen",
         "Each rule's probability cut-off, chosen on the training predictions (riskiest X% in train, F1-best, or 0.5) "
         "and applied unchanged to test and validation."),
        ("Claim Benchmark", "claim_benchmark_classification", "3.1", f"Claim model: benchmark table ({period}, and by year)",
         "ROC-AUC (ranking; 0.5 = random) and, at each model's main cut-off, flagged share, accuracy, precision, recall, "
         "F1 and lift (precision ÷ base rate)."),
        ("Claim Confusion", "claim_confusion", "3.3", f"Claim model: confusion matrices on {period}, every model and cut-off rule",
         "TP = flagged and claimed; FP = flagged, no claim; FN = missed claims; TN = not flagged, no claim. "
         "The flagged share can drift from the train share: that shows the score shift."),
        ("Claim by Split", "claim_by_split", "3.5", "Claim model: train vs test vs validation",
         "The same cut-off on each split. A large drop from train to test/validation means over-fitting or drift."),
        ("Claim Saved 07", "claim_saved_07_confusion", "3.6", "Claim model: saved test results of 07a",
         "Confusion matrices saved by the 07a notebooks on the test years."),
        ("Lapse Benchmark", "lapse_benchmark_classification", "4.1", f"Lapse model: benchmark table ({period}, and by year)",
         "As for claims; lift = precision ÷ lapse base rate."),
        ("Lapse Confusion", "lapse_confusion", "4.3", f"Lapse model: confusion matrices on {period}, every model and cut-off rule",
         "TP = flagged and lapsed; FN = missed lapses."),
        ("Lapse by Split", "lapse_by_split", "4.5", "Lapse model: train vs test vs validation", "The same cut-off on each split."),
        ("Lapse Saved 07", "lapse_saved_07_confusion", "4.6", "Lapse model: saved test results of 07b",
         "Confusion matrices saved by the 07b notebooks on the test years."),
    ]
    figures = {
        "Claim Charts": [("claim_confusion", "Confusion matrices (Step 3.3)"), ("claim_roc", "ROC curves (Step 3.4)"),
                         ("claim_by_split", "Train vs test vs validation (Step 3.5)"),
                         ("claim_saved_07_results", "Saved test results of 07a (Step 3.6)")],
        "Lapse Charts": [("lapse_confusion", "Confusion matrices (Step 4.3)"), ("lapse_roc", "ROC curves (Step 4.4)"),
                         ("lapse_by_split", "Train vs test vs validation (Step 4.5)"),
                         ("lapse_saved_07_results", "Saved test results of 07b (Step 4.6)")],
    }
    return _build(reports_dir, prefix, out, f"08a Evaluation: Classification View ({run_label})", info, tabs, figures)


# ----------------------------------------------------------------------------- 08b
def write_08b_workbook(reports_dir, split, out_name=None):
    """Collect the tables and charts of one 08b run into an Excel workbook.

    Parameters
    ----------
    reports_dir : pathlib.Path
        The ``reports/`` folder, where 08b saved ``eval_<split>_*.csv`` and
        ``fig_eval_<split>_*.png`` (and 08a its run record).
    split : {"test", "valid"}
        The ``EVAL_SPLIT`` of the run.
    out_name : str, optional
        File name of the workbook. Default:
        ``"08b Evaluation Additional Analysis Results (test run).xlsx"`` or
        ``"... (validation run).xlsx"``.

    Returns
    -------
    pathlib.Path
        The workbook written.
    """
    reports_dir = Path(reports_dir)
    prefix = f"eval_{split}"
    period, run_label, _ = SPLIT_TEXT[split]
    out = reports_dir / (out_name or f"08b Evaluation Additional Analysis Results ({run_label}).xlsx")
    _, info = _run_info(reports_dir, split, "08b")

    prob = ("A/E = actual ÷ predicted (1.00 = right on average). Skill = improvement over the training base rate "
            "(> 0 = better). Lower log loss / Brier = better.")
    tabs = []
    for kind, sec, name in [("claim", 1, "Claim"), ("lapse", 2, "Lapse")]:
        monitoring_step = f"{sec}.5" if kind == "claim" else "2.7"
        tabs += [
            (f"{name} Probability", f"{kind}_probability", f"{sec}.1", f"{name} model: probability check ({period}, and by year)", prob),
            (f"{name} Calib Summary", f"{kind}_calibration_summary", f"{sec}.2", f"{name} model: calibration summary",
             "ECE = average gap between predicted and actual rate over the deciles. Calibration slope 1 and intercept 0 = "
             "perfect; slope < 1 = probabilities too spread out."),
            (f"{name} Calib Deciles", f"{kind}_calibration_deciles", f"{sec}.2", f"{name} model: predicted vs actual rate by decile",
             f"Each model's own deciles on {period} (10 = riskiest). Actual ÷ predicted near 1 in every decile = well calibrated."),
            (f"{name} Expected vs Actual", f"{kind}_expected_vs_actual", f"{sec}.2", f"{name} model: expected vs actual {kind}s by year",
             "Expected = sum of the probabilities."),
            (f"{name} by Segment", f"{kind}_by_segment", f"{sec}.3", f"{name} model: calibration by segment",
             "A/E and skill within segments (state, zone, new business, mandatory purchase"
             + (", storm during the term" if kind == "claim" else "") + ")."),
            (f"{name} Stability", f"{kind}_stability_by_year", f"{sec}.4", f"{name} model: stability by year",
             "In-sample (training years) vs out-of-time years. Falling skill or A/E drifting from 1 = drift."),
        ]
        if kind == "lapse":
            tabs += [
                ("Cohort Life Tables", "lapse_cohort_life_tables", "2.5", "Lapse: cohort life tables of new business",
                 "Each sale cohort followed year by year: at risk, lapsed, hazard (lapse rate in year k) and retention "
                 "(share still insured), actual vs model vs segment rule."),
                ("Cohort Check", "lapse_cohort_check", "2.5", "Lapse: cohort retention, model vs actual",
                 "Gap in percentage points: negative = the model predicts lower retention than actually happened."),
            ]
        tabs += [
            (f"{name} Monitoring", f"{kind}_monitoring", monitoring_step, f"{name} model: simulated quarterly monitoring",
             "Score PSI and feature PSI vs the same calendar quarter of training (PSI > 0.25 = large shift), "
             "A/E and the alert status each quarter."),
            (f"{name} Feature PSI", f"{kind}_feature_psi", monitoring_step, f"{name} model: feature PSI by quarter",
             "PSI of each feature vs training: < 0.10 stable, 0.10–0.25 watch, > 0.25 large shift."),
        ]
    tabs += [
        ("Bootstrap", "bootstrap", "3.1", "Bootstrap by policy: is the primary model's gain over each benchmark real?",
         "Difference in log loss = other model − primary model (positive = the primary model is better), with a 95% "
         "interval from resampling policies (Poisson bootstrap)."),
        ("Claim by History", "claim_by_history", "3.2", "Claim model: sensitivity to policy history and linkage", prob),
        ("Lapse by History", "lapse_by_history", "3.2", "Lapse model: sensitivity to policy history and linkage", prob),
    ]
    figures = {
        "Claim Charts": [("claim_calibration", "Calibration (Step 1.2)"), ("claim_segments", "Calibration by segment (Step 1.3)"),
                         ("claim_stability", "Stability over time (Step 1.4)"), ("claim_monitoring", "Simulated monitoring (Step 1.5)")],
        "Lapse Charts": [("lapse_calibration", "Calibration (Step 2.2)"), ("lapse_segments", "Calibration by segment (Step 2.3)"),
                         ("lapse_stability", "Stability over time (Step 2.4)"),
                         ("lapse_cohort_survival", "Cohort survival (Step 2.5)"),
                         ("lapse_cohort_segments", "Retention by segment at sale (Step 2.6)"),
                         ("lapse_monitoring", "Simulated monitoring (Step 2.7)")],
    }
    return _build(reports_dir, prefix, out, f"08b Evaluation: Additional Analysis ({run_label})", info, tabs, figures)
