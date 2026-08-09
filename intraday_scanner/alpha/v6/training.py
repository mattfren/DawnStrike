"""Leakage-safe, evidence-gated model fitting for the V6 shadow challenger."""

from __future__ import annotations

import math
from collections.abc import Iterable
from typing import Any

from intraday_scanner.alpha.v6.contracts import (
    ALPHAOPS_V6_MODEL_VERSION,
    canonical_hash,
    utc_now,
)
from intraday_scanner.alpha.v6.models import evidence_lineage, model_eligibility
from intraday_scanner.alpha.v6.scoring import conservative_utility
from intraday_scanner.alpha.v6.validation import expanding_purged_splits

_MIN_BINARY_LABELS = 100
_MIN_CONFORMAL_RESIDUALS = 20
_PROHIBITED_FEATURE_TOKENS = frozenset(
    {
        "action",
        "eventual",
        "future",
        "high_after_entry",
        "label",
        "low_after_entry",
        "mfe",
        "mae",
        "outcome",
        "prediction",
        "rank",
        "realized",
        "selected",
        "selection",
        "target_net_excess_return",
    }
)


def train_shadow_challengers(dataset: dict[str, Any], *, code_sha: str) -> dict[str, Any]:
    """Fit the frozen model suite only after the predeclared evidence threshold.

    The returned artifact is JSON-safe and hash-addressable. It contains the
    complete linear/logistic preprocessing and coefficients needed to audit
    those baselines. Gradient boosting is a separately identified challenger;
    it is never selected merely because its in-sample fit is better.
    """

    rows = list(dataset.get("rows") or [])
    activation_rows = list(dataset.get("activation_rows") or [])
    eligibility = model_eligibility(rows).to_dict()
    base = {
        "model_version": ALPHAOPS_V6_MODEL_VERSION,
        "trained_at": utc_now(),
        "training_cutoff": dataset.get("training_cutoff"),
        "dataset_id": dataset.get("dataset_id"),
        "dataset_hash_sha256": dataset.get("dataset_hash_sha256"),
        "training_input_hash_sha256": dataset.get("dataset_hash_sha256"),
        "feature_schema_version": dataset.get("feature_schema_version"),
        "code_sha": code_sha,
        "eligibility": eligibility,
        "evidence_lineage": _lineage_summary(rows),
        "eligibility_dimensions": {
            "research_training": {
                "eligible": eligibility["eligible_label_count"] >= 100,
                "count": eligibility["retrospective_research_eligible_count"],
            },
            "prospective_promotion": {
                "eligible": False,
                "count": eligibility["prospective_promotion_eligible_count"],
                "automatic_promotion": False,
                "status": "MANUAL_REVIEW_REQUIRED",
            },
        },
        "research_only": True,
        "broker_execution_enabled": False,
        "automatic_promotion": False,
    }
    if eligibility["status"] == "NOT_TRAINED_INSUFFICIENT_LABELS":
        return _receipt(base, status="NOT_TRAINED_INSUFFICIENT_LABELS", artifact=None)
    dependency = _research_dependency()
    if dependency is None:
        return _receipt(base, status="BLOCKED_RESEARCH_DEPENDENCY", artifact=None)
    suite = _fit_model_suite(
        rows,
        activation_rows=activation_rows,
        allow_gradient_boosting=(
            "controlled_gradient_boosting" in eligibility["allowed_families"]
        ),
    )
    artifact = {
        "artifact_schema_version": "dawnstrike.alphaops_v6.model_artifact.v2",
        "library": "scikit-learn",
        "library_version": dependency["sklearn_version"],
        "numpy_version": dependency["numpy_version"],
        "fitted": True,
        "feature_names": suite["feature_names"],
        "feature_set_hash_sha256": canonical_hash(suite["feature_names"]),
        "prohibited_feature_tokens": sorted(_PROHIBITED_FEATURE_TOKENS),
        "models": suite["artifact_models"],
        "conformal": suite["conformal"],
        "candidate_families": eligibility["allowed_families"],
        "selection_policy": (
            "regularized models remain the frozen baseline; controlled gradient "
            "boosting is evaluated as a challenger only"
        ),
        "limitations": [
            "This is a research-only shadow model, not a return claim.",
            "Promotion requires purged forward and untouched-holdout evidence.",
            "Missing labels and source failures are excluded, never imputed as zero.",
        ],
    }
    status = (
        "TRAINED_CONTROLLED_CHALLENGERS"
        if suite["gradient_boosting_fitted"]
        else "TRAINED_RESEARCH_BASELINES"
    )
    return _receipt(base, status=status, artifact=artifact)


def walk_forward_challenger_predictions(
    dataset: dict[str, Any], *, model_run_id: str
) -> list[dict[str, Any]]:
    """Generate only purged, date-forward predictions for persisted evaluation."""

    rows = list(dataset.get("rows") or [])
    activation_rows = list(dataset.get("activation_rows") or [])
    if _research_dependency() is None:
        return []
    predictions: list[dict[str, Any]] = []
    for fold in expanding_purged_splits(rows):
        training_dates = set(fold["training_dates"])
        test_dates = set(fold["test_dates"])
        training_rows = [row for row in rows if row.get("market_date") in training_dates]
        eligibility = model_eligibility(training_rows)
        if eligibility.status == "NOT_TRAINED_INSUFFICIENT_LABELS":
            continue
        training_activation = [
            row for row in activation_rows if row.get("market_date") in training_dates
        ]
        suite = _fit_model_suite(
            training_rows,
            activation_rows=training_activation,
            # This eligibility decision is intentionally made inside every
            # expanding fold.  A complex challenger may only see labels that
            # predate the held-out session; it must never inherit the final
            # dataset's complexity permission.
            allow_gradient_boosting=(
                "controlled_gradient_boosting" in eligibility.allowed_families
            ),
        )
        training_max_date = max(training_dates)
        generated_at = utc_now()
        for row in rows:
            if row.get("market_date") not in test_dates:
                continue
            prediction = _predict_suite(suite, row)
            payload = {
                "decision_id": row.get("decision_id"),
                "model_run_id": model_run_id,
                "fold_id": fold["fold_id"],
                "market_date": row.get("market_date"),
                "generated_at": generated_at,
                "status": prediction["status"],
                "training_min_market_date": min(training_dates),
                "training_max_market_date": training_max_date,
                "embargoed_dates": fold["embargoed_dates"],
                "no_lookahead": training_max_date < str(row.get("market_date") or ""),
                "prediction": prediction,
                "permitted_families": list(eligibility.allowed_families),
                "evidence_lineage": _lineage_summary([row]),
                "research_only": True,
                "broker_execution_enabled": False,
            }
            payload["prediction_id"] = "v6p-" + canonical_hash(
                {
                    "decision_id": payload["decision_id"],
                    "model_run_id": model_run_id,
                    "fold_id": payload["fold_id"],
                }
            )[:28]
            predictions.append(payload)
    return predictions


def predict_from_frozen_model_run(
    model_run: dict[str, Any] | None,
    decision: dict[str, Any],
) -> dict[str, Any] | None:
    """Score one later decision from a persisted JSON-safe frozen artifact."""

    run = model_run or {}
    artifact = run.get("artifact")
    data = artifact if isinstance(artifact, dict) else {}
    decision_date = str(decision.get("market_date") or "")[:10]
    cutoff = str(run.get("training_cutoff") or "")[:10]
    run_feature_schema = str(run.get("feature_schema_version") or "")
    decision_feature_schema = str(decision.get("feature_schema_version") or "")
    if (
        data.get("fitted") is not True
        or not cutoff
        or not decision_date
        or cutoff >= decision_date
        or not run_feature_schema
        or run_feature_schema != decision_feature_schema
    ):
        return None
    feature_names = list(data.get("feature_names") or [])
    models = data.get("models")
    model_data = models if isinstance(models, dict) else {}
    values = _frozen_standardized_values(
        decision,
        feature_names,
        dict(model_data.get("conditional_return_model") or {}),
    )
    conditional_return = _frozen_linear_prediction(
        dict(model_data.get("conditional_return_model") or {}), values
    )
    activation = _frozen_binary_prediction(
        dict(model_data.get("activation_classifier") or {}),
        decision,
        feature_names,
    )
    tail_probability = _frozen_binary_prediction(
        dict(model_data.get("tail_risk_classifier") or {}),
        decision,
        feature_names,
    )
    tail_severity = _finite(model_data.get("tail_severity_pct"))
    expected_tail = (
        tail_probability * tail_severity
        if tail_probability is not None and tail_severity is not None
        else None
    )
    conformal = data.get("conformal")
    conformal_data = conformal if isinstance(conformal, dict) else {}
    uncertainty = _finite(conformal_data.get("absolute_residual_quantile_pct"))
    capacity_penalty = _capacity_penalty(decision)
    score = conservative_utility(
        activation_probability=activation,
        conditional_net_excess_return_pct=conditional_return,
        tail_loss_pct=expected_tail,
        uncertainty_pct=uncertainty,
        capacity_penalty_pct=capacity_penalty,
        safety_vetoes=list(decision.get("safety_vetoes") or []),
    )
    return {
        "status": (
            "FROZEN_POINT_IN_TIME_SHADOW_SCORE"
            if score.get("utility_lcb_pct") is not None
            else "UNCALIBRATED_INCOMPLETE_EVIDENCE"
        ),
        "model_run_id": run.get("model_run_id"),
        "training_cutoff": cutoff,
        "dataset_hash_sha256": run.get("dataset_hash_sha256"),
        "model_artifact_hash_sha256": run.get("model_artifact_hash_sha256"),
        "sample_size": dict(run.get("eligibility") or {}).get("eligible_label_count"),
        "activation_probability": _round(activation),
        "conditional_net_excess_return_pct": _round(conditional_return),
        "tail_loss_probability": _round(tail_probability),
        "tail_loss_severity_pct": _round(tail_severity),
        "tail_loss_pct": _round(expected_tail),
        "uncertainty_pct": _round(uncertainty),
        "interval_lower_pct": (
            _round(conditional_return - uncertainty)
            if conditional_return is not None and uncertainty is not None
            else None
        ),
        "interval_upper_pct": (
            _round(conditional_return + uncertainty)
            if conditional_return is not None and uncertainty is not None
            else None
        ),
        "capacity_penalty_pct": _round(capacity_penalty),
        "utility_lcb_pct": score.get("utility_lcb_pct"),
        "research_only": True,
        "broker_execution_enabled": False,
    }


def _frozen_binary_prediction(
    artifact: dict[str, Any],
    decision: dict[str, Any],
    feature_names: list[str],
) -> float | None:
    if artifact.get("fitted") is not True:
        return None
    if artifact.get("family") == "empirical_bayes_constant":
        return _finite(artifact.get("probability"))
    values = _frozen_standardized_values(decision, feature_names, artifact)
    linear = _frozen_linear_prediction(artifact, values)
    if linear is None:
        return None
    if linear >= 0:
        return 1.0 / (1.0 + math.exp(-linear))
    exponent = math.exp(linear)
    return exponent / (1.0 + exponent)


def _frozen_standardized_values(
    row: dict[str, Any],
    feature_names: list[str],
    artifact: dict[str, Any],
) -> list[float]:
    mapping = _feature_mapping(row)
    imputer = list(artifact.get("imputer_statistics") or [])
    means = list(artifact.get("scaler_mean") or [])
    scales = list(artifact.get("scaler_scale") or [])
    output = []
    for index, name in enumerate(feature_names):
        value = _finite(mapping.get(name))
        if value is None:
            value = _finite(imputer[index]) if index < len(imputer) else 0.0
        mean_value = _finite(means[index]) if index < len(means) else 0.0
        scale = _finite(scales[index]) if index < len(scales) else 1.0
        output.append((float(value or 0.0) - float(mean_value or 0.0)) / float(scale or 1.0))
    return output


def _frozen_linear_prediction(
    artifact: dict[str, Any], values: list[float]
) -> float | None:
    coefficients = list(artifact.get("coefficients") or [])
    intercepts = list(artifact.get("intercept") or [])
    if artifact.get("fitted") is not True or len(coefficients) != len(values):
        return None
    intercept = _finite(intercepts[0]) if intercepts else 0.0
    return float(intercept or 0.0) + sum(
        float(coefficient) * value
        for coefficient, value in zip(coefficients, values, strict=True)
    )


def _fit_model_suite(
    rows: list[dict[str, Any]],
    *,
    activation_rows: list[dict[str, Any]],
    allow_gradient_boosting: bool,
) -> dict[str, Any]:
    import numpy as np  # type: ignore[import-not-found]

    feature_rows = [_feature_mapping(row) for row in [*rows, *activation_rows]]
    feature_names = sorted({name for item in feature_rows for name in item})
    return_x = _matrix(rows, feature_names)
    return_y = np.asarray(
        [float(row["target_net_excess_return_pct"]) for row in rows], dtype=float
    )
    return_weights = _weights(rows)
    return_model = _fit_linear(return_x, return_y, return_weights)

    activation_model = None
    activation_constant = None
    if len(activation_rows) >= _MIN_BINARY_LABELS:
        activation_y = np.asarray(
            [float(row["activation_label"]) for row in activation_rows], dtype=float
        )
        if len(set(activation_y.tolist())) >= 2:
            activation_model = _fit_logistic(
                _matrix(activation_rows, feature_names),
                activation_y,
                _weights(activation_rows),
            )
        else:
            activation_constant = _beta_rate(activation_y.tolist())

    tail_y = np.asarray(
        [float(row.get("tail_loss_label") or 0.0) for row in rows], dtype=float
    )
    tail_model = None
    tail_constant = None
    if len(rows) >= _MIN_BINARY_LABELS:
        if len(set(tail_y.tolist())) >= 2:
            tail_model = _fit_logistic(return_x, tail_y, return_weights)
        else:
            tail_constant = _beta_rate(tail_y.tolist())
    tail_returns = [
        float(row["target_net_excess_return_pct"])
        for row in rows
        if float(row.get("tail_loss_label") or 0.0) == 1.0
    ]
    tail_severity = sum(tail_returns) / len(tail_returns) if tail_returns else None

    conformal = _conformal_receipt(rows, feature_names)
    gradient_model = None
    gradient_conformal: dict[str, Any] | None = None
    if allow_gradient_boosting:
        gradient_model = _new_gradient_model()
        gradient_model.fit(return_x, return_y, sample_weight=return_weights)
        gradient_conformal = _gradient_conformal_receipt(rows, feature_names)

    artifact_models = {
        "activation_classifier": (
            _linear_artifact(activation_model, "regularized_logistic")
            if activation_model is not None
            else _constant_artifact(activation_constant, "activation")
        ),
        "conditional_return_model": _linear_artifact(return_model, "regularized_linear"),
        "tail_risk_classifier": (
            _linear_artifact(tail_model, "regularized_logistic")
            if tail_model is not None
            else _constant_artifact(tail_constant, "tail_loss")
        ),
        "tail_severity_pct": _round(tail_severity),
        "controlled_gradient_boosting": (
            {
                "family": "hist_gradient_boosting_regressor",
                "fitted": True,
                "parameters": {
                    "learning_rate": 0.05,
                    "max_iter": 150,
                    "max_leaf_nodes": 15,
                    "l2_regularization": 1.0,
                    "random_state": 0,
                },
                "training_prediction_hash_sha256": canonical_hash(
                    [round(float(value), 10) for value in gradient_model.predict(return_x)]
                ),
                "conformal": gradient_conformal,
                "promotion_selected": False,
            }
            if gradient_model is not None
            else {"fitted": False, "reason": "complexity_evidence_gate_not_met"}
        ),
    }
    return {
        "feature_names": feature_names,
        "return_model": return_model,
        "activation_model": activation_model,
        "activation_constant": activation_constant,
        "tail_model": tail_model,
        "tail_constant": tail_constant,
        "tail_severity_pct": tail_severity,
        "conformal": conformal,
        "gradient_model": gradient_model,
        "gradient_conformal": gradient_conformal,
        "artifact_models": artifact_models,
        "gradient_boosting_fitted": gradient_model is not None,
    }


def _predict_suite(suite: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    matrix = _matrix([row], suite["feature_names"])
    activation = _binary_prediction(
        suite["activation_model"], suite["activation_constant"], matrix
    )
    conditional_return = float(suite["return_model"].predict(matrix)[0])
    tail_probability = _binary_prediction(
        suite["tail_model"], suite["tail_constant"], matrix
    )
    tail_severity = suite["tail_severity_pct"]
    expected_tail = (
        float(tail_probability) * float(tail_severity)
        if tail_probability is not None and tail_severity is not None
        else None
    )
    residual_quantile = suite["conformal"].get("absolute_residual_quantile_pct")
    interval_lower = (
        conditional_return - float(residual_quantile)
        if residual_quantile is not None
        else None
    )
    interval_upper = (
        conditional_return + float(residual_quantile)
        if residual_quantile is not None
        else None
    )
    capacity_penalty = _capacity_penalty(row)
    score = conservative_utility(
        activation_probability=activation,
        conditional_net_excess_return_pct=conditional_return,
        tail_loss_pct=expected_tail,
        uncertainty_pct=float(residual_quantile) if residual_quantile is not None else None,
        capacity_penalty_pct=capacity_penalty,
    )
    baseline = {
        "status": (
            "FROZEN_OOF_RESEARCH_SCORE"
            if score.get("utility_lcb_pct") is not None
            else "UNCALIBRATED_INCOMPLETE_EVIDENCE"
        ),
        "activation_probability": _round(activation),
        "conditional_net_excess_return_pct": _round(conditional_return),
        "tail_loss_probability": _round(tail_probability),
        "tail_loss_severity_pct": _round(tail_severity),
        "tail_loss_pct": _round(expected_tail),
        "uncertainty_pct": _round(residual_quantile),
        "interval_lower_pct": _round(interval_lower),
        "interval_upper_pct": _round(interval_upper),
        "capacity_penalty_pct": _round(capacity_penalty),
        "utility_lcb_pct": score.get("utility_lcb_pct"),
        "research_only": True,
        "broker_execution_enabled": False,
    }
    family_predictions = {"regularized_baselines": baseline}
    gradient_model = suite.get("gradient_model")
    if gradient_model is not None:
        family_predictions["controlled_gradient_boosting"] = _gradient_prediction(
            model=gradient_model,
            matrix=matrix,
            activation=activation,
            tail_probability=tail_probability,
            tail_severity=tail_severity,
            capacity_penalty=capacity_penalty,
            conformal=suite.get("gradient_conformal"),
        )
    # The baseline remains the visible score.  Family scores are persisted
    # solely for exact-fold research comparison and cannot change policy.
    return {
        **baseline,
        "selected_family": "regularized_baselines",
        "family_predictions": family_predictions,
        "automatic_family_selection": False,
    }


def _gradient_prediction(
    *,
    model: Any,
    matrix: Any,
    activation: float | None,
    tail_probability: float | None,
    tail_severity: float | None,
    capacity_penalty: float | None,
    conformal: Any,
) -> dict[str, Any]:
    """Score the controlled challenger without electing it as policy."""

    conditional_return = float(model.predict(matrix)[0])
    conformal_data = conformal if isinstance(conformal, dict) else {}
    residual_quantile = _finite(conformal_data.get("absolute_residual_quantile_pct"))
    expected_tail = (
        float(tail_probability) * float(tail_severity)
        if tail_probability is not None and tail_severity is not None
        else None
    )
    score = conservative_utility(
        activation_probability=activation,
        conditional_net_excess_return_pct=conditional_return,
        tail_loss_pct=expected_tail,
        uncertainty_pct=residual_quantile,
        capacity_penalty_pct=capacity_penalty,
    )
    return {
        "status": (
            "FROZEN_OOF_RESEARCH_SCORE"
            if score.get("utility_lcb_pct") is not None
            else "UNCALIBRATED_INCOMPLETE_EVIDENCE"
        ),
        "activation_probability": _round(activation),
        "conditional_net_excess_return_pct": _round(conditional_return),
        "tail_loss_probability": _round(tail_probability),
        "tail_loss_severity_pct": _round(tail_severity),
        "tail_loss_pct": _round(expected_tail),
        "uncertainty_pct": _round(residual_quantile),
        "interval_lower_pct": _round(
            conditional_return - residual_quantile
            if residual_quantile is not None
            else None
        ),
        "interval_upper_pct": _round(
            conditional_return + residual_quantile
            if residual_quantile is not None
            else None
        ),
        "capacity_penalty_pct": _round(capacity_penalty),
        "utility_lcb_pct": score.get("utility_lcb_pct"),
        "research_only": True,
        "broker_execution_enabled": False,
    }


def _new_gradient_model() -> Any:
    from sklearn.ensemble import HistGradientBoostingRegressor  # type: ignore[import-not-found]

    return HistGradientBoostingRegressor(
        learning_rate=0.05,
        max_iter=150,
        max_leaf_nodes=15,
        l2_regularization=1.0,
        random_state=0,
    )


def _fit_linear(x: Any, y: Any, weights: Any) -> Any:
    from sklearn.impute import SimpleImputer  # type: ignore[import-not-found]
    from sklearn.linear_model import Ridge  # type: ignore[import-not-found]
    from sklearn.pipeline import Pipeline  # type: ignore[import-not-found]
    from sklearn.preprocessing import StandardScaler  # type: ignore[import-not-found]

    model = Pipeline(
        [
            ("imputer", SimpleImputer(strategy="median", keep_empty_features=True)),
            ("scaler", StandardScaler()),
            ("estimator", Ridge(alpha=10.0)),
        ]
    )
    model.fit(x, y, estimator__sample_weight=weights)
    return model


def _fit_logistic(x: Any, y: Any, weights: Any) -> Any:
    from sklearn.impute import SimpleImputer  # type: ignore[import-not-found]
    from sklearn.linear_model import LogisticRegression  # type: ignore[import-not-found]
    from sklearn.pipeline import Pipeline  # type: ignore[import-not-found]
    from sklearn.preprocessing import StandardScaler  # type: ignore[import-not-found]

    model = Pipeline(
        [
            ("imputer", SimpleImputer(strategy="median", keep_empty_features=True)),
            ("scaler", StandardScaler()),
            (
                "estimator",
                LogisticRegression(C=0.1, max_iter=2_000, random_state=0),
            ),
        ]
    )
    model.fit(x, y, estimator__sample_weight=weights)
    return model


def _conformal_receipt(rows: list[dict[str, Any]], feature_names: list[str]) -> dict[str, Any]:
    dates = sorted({str(row.get("market_date") or "") for row in rows})
    calibration_date_count = max(1, math.ceil(len(dates) * 0.2))
    fit_dates = set(dates[:-calibration_date_count])
    calibration_dates = set(dates[-calibration_date_count:])
    fit_rows = [row for row in rows if row.get("market_date") in fit_dates]
    calibration_rows = [
        row for row in rows if row.get("market_date") in calibration_dates
    ]
    if len(fit_rows) < 50 or len(calibration_rows) < _MIN_CONFORMAL_RESIDUALS:
        return {
            "status": "INSUFFICIENT_CHRONOLOGICAL_CALIBRATION_ROWS",
            "coverage_target_pct": 90.0,
            "calibration_row_count": len(calibration_rows),
            "absolute_residual_quantile_pct": None,
        }
    model = _fit_linear(
        _matrix(fit_rows, feature_names),
        _targets(fit_rows),
        _weights(fit_rows),
    )
    predicted = model.predict(_matrix(calibration_rows, feature_names))
    residuals = sorted(
        abs(float(actual) - float(prediction))
        for actual, prediction in zip(_targets(calibration_rows), predicted, strict=True)
    )
    rank = min(len(residuals) - 1, math.ceil(0.9 * (len(residuals) + 1)) - 1)
    return {
        "status": "FROZEN_CHRONOLOGICAL_CONFORMAL",
        "coverage_target_pct": 90.0,
        "fit_max_market_date": max(fit_dates),
        "calibration_min_market_date": min(calibration_dates),
        "calibration_row_count": len(calibration_rows),
        "absolute_residual_quantile_pct": _round(residuals[rank]),
    }


def _gradient_conformal_receipt(
    rows: list[dict[str, Any]], feature_names: list[str]
) -> dict[str, Any]:
    """Build a chronological interval receipt for the gradient challenger.

    It intentionally repeats the baseline's predeclared chronological split.
    The challenger never receives later calibration rows, and the receipt is
    separate so an optimistic linear-model interval cannot be borrowed.
    """

    dates = sorted({str(row.get("market_date") or "") for row in rows})
    calibration_date_count = max(1, math.ceil(len(dates) * 0.2))
    fit_dates = set(dates[:-calibration_date_count])
    calibration_dates = set(dates[-calibration_date_count:])
    fit_rows = [row for row in rows if row.get("market_date") in fit_dates]
    calibration_rows = [
        row for row in rows if row.get("market_date") in calibration_dates
    ]
    if len(fit_rows) < 50 or len(calibration_rows) < _MIN_CONFORMAL_RESIDUALS:
        return {
            "status": "INSUFFICIENT_CHRONOLOGICAL_CALIBRATION_ROWS",
            "coverage_target_pct": 90.0,
            "calibration_row_count": len(calibration_rows),
            "absolute_residual_quantile_pct": None,
        }
    model = _new_gradient_model()
    model.fit(
        _matrix(fit_rows, feature_names),
        _targets(fit_rows),
        sample_weight=_weights(fit_rows),
    )
    predicted = model.predict(_matrix(calibration_rows, feature_names))
    residuals = sorted(
        abs(float(actual) - float(prediction))
        for actual, prediction in zip(_targets(calibration_rows), predicted, strict=True)
    )
    rank = min(len(residuals) - 1, math.ceil(0.9 * (len(residuals) + 1)) - 1)
    return {
        "status": "FROZEN_CHRONOLOGICAL_CONFORMAL",
        "coverage_target_pct": 90.0,
        "fit_max_market_date": max(fit_dates),
        "calibration_min_market_date": min(calibration_dates),
        "calibration_row_count": len(calibration_rows),
        "absolute_residual_quantile_pct": _round(residuals[rank]),
    }


def _feature_mapping(row: dict[str, Any]) -> dict[str, float]:
    feature = row.get("feature_vector")
    feature_data = feature if isinstance(feature, dict) else {}
    raw = feature_data.get("feature_json")
    raw_data = raw if isinstance(raw, dict) else feature_data
    output: dict[str, float] = {}
    _flatten_numeric(raw_data, prefix="feature", output=output)
    for key in ("setup_key", "regime_key", "source_key", "liquidity_bucket", "catalyst_bucket"):
        value = str(row.get(key) or "unknown").strip().lower()
        output[f"category.{key}.{value}"] = 1.0
    return output


def _flatten_numeric(value: Any, *, prefix: str, output: dict[str, float]) -> None:
    if isinstance(value, dict):
        for key, item in sorted(value.items()):
            normalized = str(key).strip().lower()
            if any(token in normalized for token in _PROHIBITED_FEATURE_TOKENS):
                continue
            _flatten_numeric(item, prefix=f"{prefix}.{normalized}", output=output)
        return
    if isinstance(value, bool):
        output[prefix] = float(value)
        return
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        output[prefix] = float(value)


def _matrix(rows: list[dict[str, Any]], feature_names: list[str]) -> Any:
    import numpy as np  # type: ignore[import-not-found]

    matrix = np.full((len(rows), len(feature_names)), np.nan, dtype=float)
    positions = {name: index for index, name in enumerate(feature_names)}
    for row_index, row in enumerate(rows):
        for name, value in _feature_mapping(row).items():
            index = positions.get(name)
            if index is not None:
                matrix[row_index, index] = value
    return matrix


def _targets(rows: list[dict[str, Any]]) -> Any:
    import numpy as np  # type: ignore[import-not-found]

    return np.asarray(
        [float(row["target_net_excess_return_pct"]) for row in rows], dtype=float
    )


def _weights(rows: list[dict[str, Any]]) -> Any:
    import numpy as np  # type: ignore[import-not-found]

    return np.asarray(
        [
            min(10.0, max(1.0, _finite(row.get("inverse_probability_weight")) or 1.0))
            for row in rows
        ],
        dtype=float,
    )


def _linear_artifact(model: Any, family: str) -> dict[str, Any]:
    if model is None:
        return {"family": family, "fitted": False}
    estimator = model.named_steps["estimator"]
    coefficients = getattr(estimator, "coef_", [])
    return {
        "family": family,
        "fitted": True,
        "imputer_statistics": _list(model.named_steps["imputer"].statistics_),
        "scaler_mean": _list(model.named_steps["scaler"].mean_),
        "scaler_scale": _list(model.named_steps["scaler"].scale_),
        # Binary logistic coefficients are shaped (1, n); Ridge coefficients
        # are shaped (n,). Persist one flat vector so the JSON-only inference
        # path has one unambiguous contract for both families.
        "coefficients": _list(coefficients),
        "intercept": _list(getattr(estimator, "intercept_", [])),
        "parameters": {
            "alpha": getattr(estimator, "alpha", None),
            "C": getattr(estimator, "C", None),
            "max_iter": getattr(estimator, "max_iter", None),
            "random_state": getattr(estimator, "random_state", None),
        },
    }


def _constant_artifact(value: float | None, target: str) -> dict[str, Any]:
    return {
        "family": "empirical_bayes_constant",
        "target": target,
        "fitted": value is not None,
        "probability": _round(value),
        "reason": None if value is not None else "insufficient_binary_labels",
    }


def _binary_prediction(model: Any, constant: float | None, matrix: Any) -> float | None:
    if model is not None:
        return float(model.predict_proba(matrix)[0][1])
    return constant


def _beta_rate(values: Iterable[float]) -> float:
    rows = list(values)
    return (sum(rows) + 1.0) / (len(rows) + 2.0)


def _capacity_penalty(row: dict[str, Any]) -> float | None:
    cost_bps = _finite(row.get("estimated_round_trip_cost_bps"))
    if cost_bps is None:
        return None
    return max(0.0, cost_bps - 25.0) / 100.0


def _research_dependency() -> dict[str, str] | None:
    try:
        import numpy  # type: ignore[import-not-found]
        import sklearn  # type: ignore[import-not-found]
    except ImportError:
        return None
    return {
        "numpy_version": str(getattr(numpy, "__version__", "unknown")),
        "sklearn_version": str(getattr(sklearn, "__version__", "unknown")),
    }


def _list(values: Any) -> list[float]:
    raw = values.tolist() if hasattr(values, "tolist") else list(values or [])
    if isinstance(raw, (int, float)):
        return [round(float(raw), 12)]
    if raw and isinstance(raw[0], list):
        return [round(float(value), 12) for row in raw for value in row]
    return [round(float(value), 12) for value in raw]


def _finite(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _round(value: float | None) -> float | None:
    return round(float(value), 10) if value is not None and math.isfinite(float(value)) else None


def _receipt(
    base: dict[str, Any], *, status: str, artifact: dict[str, Any] | None
) -> dict[str, Any]:
    payload = {**base, "status": status, "artifact": artifact}
    payload["model_artifact_hash_sha256"] = canonical_hash(artifact) if artifact else None
    payload["model_run_id"] = "v6m-" + canonical_hash(
        {
            "model_version": payload.get("model_version"),
            "dataset_id": payload.get("dataset_id"),
            "dataset_hash_sha256": payload.get("dataset_hash_sha256"),
            "code_sha": payload.get("code_sha"),
            "status": status,
            "model_artifact_hash_sha256": payload["model_artifact_hash_sha256"],
        }
    )[:28]
    return payload


def _lineage_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    lineages = [evidence_lineage(row) for row in rows]
    return {
        "source_artifact_hashes": sorted(
            {
                item
                for lineage in lineages
                for item in lineage["source_artifact_hashes"]
            }
        ),
        "path_replay_ids": sorted(
            {
                str(lineage["path_replay_id"])
                for lineage in lineages
                if lineage["path_replay_id"]
            }
        ),
        "benchmark_hashes": sorted(
            {
                str(lineage["benchmark_hash_sha256"])
                for lineage in lineages
                if lineage["benchmark_hash_sha256"]
            }
        ),
        "observed_cost_model_identities": sorted(
            {
                str(lineage["observed_cost_model_identity"])
                for lineage in lineages
                if lineage["observed_cost_model_identity"]
            }
        ),
        "modeled_cost_model_identities": sorted(
            {
                str(lineage["modeled_cost_model_identity"])
                for lineage in lineages
                if lineage["modeled_cost_model_identity"]
            }
        ),
        "evidence_cohorts": sorted(
            {
                str(lineage["evidence_cohort"])
                for lineage in lineages
                if lineage["evidence_cohort"]
            }
        ),
        "row_lineage_hash_sha256": canonical_hash(
            [lineage["evidence_lineage_hash_sha256"] for lineage in lineages]
        ),
        "missing_truth_is_zero": False,
    }


__all__ = [
    "predict_from_frozen_model_run",
    "train_shadow_challengers",
    "walk_forward_challenger_predictions",
]
