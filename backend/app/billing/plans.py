"""Plan definitions for the multi-tier free rollout.

Two tiers exist while paid billing is paused:

- ``free`` is the default for self-signups and is sized for casual
  exploration. Limits are intentionally tight so a runaway user does
  not burn through the global daily cap.
- ``pro`` is a comped tier for invited stress-testers. Admins flip
  users into it manually via ``PUT /api/admin/users/{id}/plan``;
  when paid billing returns, the same plan name will be the paid
  tier and the distinction between comped and paid will live in
  the billing layer, not here.

Limits were picked from real May 2026 usage by the heaviest invited
tester: free covers a generous round-up of one month of stress-test
volume, and pro doubles that to give comped users headroom.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class PlanLimits:
    messages_per_month: int
    tokens_per_month: int


PLANS: dict[str, PlanLimits] = {
    "free": PlanLimits(
        messages_per_month=300,
        tokens_per_month=10_000_000,
    ),
    "pro": PlanLimits(
        messages_per_month=600,
        tokens_per_month=20_000_000,
    ),
}


def get_plan_limits(plan_name: str) -> PlanLimits:
    """Return limits for a plan name. Unknown plans fall back to free."""
    return PLANS.get(plan_name, PLANS["free"])
