"""No ML training in the observation/stability phase.

Five or ten trading sessions are pipeline soak-test durations ONLY, not an ML
sample-size or market-regime qualification. Training approval must be a later,
independent documented research decision with multi-regime broker ticks and
chronological out-of-sample validation. No bypass flag is provided.
"""

class TrainingNotApproved(RuntimeError):
    pass


def require_training_approval(*, observation_sessions: int, **_unused):
    raise TrainingNotApproved(
        f"TRAINING_BLOCKED: {observation_sessions} observation sessions only establish "
        "possible collection stability, NOT dataset suitability. Obtain separately "
        "audited multi-regime broker history including volatile news regimes, "
        "document temporal leakage controls and out-of-sample testing before "
        "a later code release adds a training path."
    )
