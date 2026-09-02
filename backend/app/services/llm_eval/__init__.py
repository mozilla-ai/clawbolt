"""Model-swap evaluator.

Replays a user's own recent turns through their current model and a candidate
model and reports whether the candidate can be trusted with their account.

Entry points:

- :func:`~backend.app.services.llm_eval.runner.launch_run` starts a run on a
  background task; the admin route creates the row first and polls it.
- :func:`~backend.app.services.llm_eval.runner.mark_interrupted_runs` is
  called at startup to close out runs orphaned by a restart.

Only reachable in ``AUTH_MODE=multi_user``: the router that drives it is
mounted there, and the thing it exists to decide (moving one tenant to a
different model) has no meaning in a single-user deployment.
"""

from backend.app.services.llm_eval.runner import launch_run, mark_interrupted_runs
from backend.app.services.llm_eval.types import (
    AgreementClass,
    JudgeVerdict,
    Recommendation,
    RunStatus,
    SafetyFinding,
)

__all__ = [
    "AgreementClass",
    "JudgeVerdict",
    "Recommendation",
    "RunStatus",
    "SafetyFinding",
    "launch_run",
    "mark_interrupted_runs",
]
