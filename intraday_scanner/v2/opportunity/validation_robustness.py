"""Explicit downstream facade for WP005-C robustness controls."""

from intraday_scanner.v2.opportunity.validation_robustness_contracts import (
    CalibrationRegionKind,
    CalibrationSourceArtifact,
    ConfirmatoryUnit,
    ParameterPerturbationSpec,
    RobustnessArmKind,
    RobustnessCalibrationPolicy,
    RobustnessCheckKind,
    RobustnessCheckStatus,
    RobustnessEvidenceStatus,
    RobustnessVerdict,
    build_calibration_source_artifact,
    build_confirmatory_unit,
    build_robustness_calibration_policy,
)
from intraday_scanner.v2.opportunity.validation_robustness_controls import (
    CausalControlArm,
    CausalSessionObservation,
    ComplexityEvidence,
    FutureDataSentinelEvidence,
    RegimeStabilityEvidence,
    build_causal_control_arm,
    build_complexity_evidence,
    build_control_observations,
    build_future_data_sentinel_evidence,
    build_regime_stability_evidence,
    build_unavailable_control_arm,
)
from intraday_scanner.v2.opportunity.validation_robustness_math import (
    SessionClusterConfidenceInterval,
    build_session_clustered_confidence_interval,
)
from intraday_scanner.v2.opportunity.validation_robustness_population import (
    ConfirmatoryPopulation,
    RobustnessSessionObservation,
    build_confirmatory_population,
)
from intraday_scanner.v2.opportunity.validation_robustness_report import (
    RobustnessCheck,
    ValidationRobustnessReport,
    build_validation_robustness_report,
)

__all__ = [
    "CalibrationRegionKind",
    "CalibrationSourceArtifact",
    "CausalControlArm",
    "CausalSessionObservation",
    "ComplexityEvidence",
    "ConfirmatoryPopulation",
    "ConfirmatoryUnit",
    "FutureDataSentinelEvidence",
    "ParameterPerturbationSpec",
    "RegimeStabilityEvidence",
    "RobustnessArmKind",
    "RobustnessCalibrationPolicy",
    "RobustnessCheck",
    "RobustnessCheckKind",
    "RobustnessCheckStatus",
    "RobustnessEvidenceStatus",
    "RobustnessSessionObservation",
    "RobustnessVerdict",
    "SessionClusterConfidenceInterval",
    "ValidationRobustnessReport",
    "build_calibration_source_artifact",
    "build_causal_control_arm",
    "build_complexity_evidence",
    "build_confirmatory_population",
    "build_confirmatory_unit",
    "build_control_observations",
    "build_future_data_sentinel_evidence",
    "build_regime_stability_evidence",
    "build_robustness_calibration_policy",
    "build_session_clustered_confidence_interval",
    "build_unavailable_control_arm",
    "build_validation_robustness_report",
]
