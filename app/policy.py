"""
L2 — the policy gate. Pure functions, zero I/O (build spec §5, §10).

Takes L1's proposal (a failure class + a proposed action, optionally with a scheduled time, plus
a rationale L2 never reads) and the deterministic PolicyContext, runs it through every rule in
app/stopping_rules.RULE_PIPELINE, and returns a PolicyDecision.

The one property this module exists to guarantee: **L2 can only reduce the blast radius of L1's
proposal, never expand it.** BLAST_RADIUS_RANK below gives every InterventionAction a score;
`evaluate()` asserts the final permitted action's rank never exceeds the proposed action's rank.
That assertion is not a test-only nicety — it runs in production too, because a violated invariant
here is a correctness bug in the one place correctness bugs are least acceptable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.models import FailureClass, InterventionAction
from app.stopping_rules import RULE_PIPELINE, PolicyContext, RuleOutcome

# Lower rank = less autonomous blast radius. L2 may only move a proposal to an equal or lower rank.
# RETRY_NOW is the single most aggressive thing the system can do (an immediate, unattended charge
# attempt); STOP_PERMANENT is the least (nothing happens). NUDGE and REQUEST_INSTRUMENT_UPDATE sit
# at the same tier — both are "contact the customer, don't move money" — so a rule may swap between
# them but that swap is lateral, not a downgrade in the ranking sense (see hard_decline_no_retry).
BLAST_RADIUS_RANK: dict[InterventionAction, int] = {
    InterventionAction.RETRY_NOW: 5,
    InterventionAction.RETRY_SCHEDULED: 4,
    InterventionAction.NUDGE: 3,
    InterventionAction.REQUEST_INSTRUMENT_UPDATE: 3,
    InterventionAction.ESCALATE_HUMAN: 1,
    InterventionAction.STOP_PERMANENT: 0,
}

# Families used to distinguish a VETO (the action category changed — a compliance-flavored refusal)
# from a DOWNGRADE (same category, softened timing or a lateral swap within the category — e.g.
# RETRY_NOW held back to RETRY_SCHEDULED is still "a retry," just not right now). Build spec §5
# talks about L2 "vetoing or downgrading" as two distinct things; this is what makes that concrete.
_RETRY_FAMILY = frozenset({InterventionAction.RETRY_NOW, InterventionAction.RETRY_SCHEDULED})
_CONTACT_FAMILY = frozenset({InterventionAction.NUDGE, InterventionAction.REQUEST_INSTRUMENT_UPDATE})
_TERMINAL_FAMILY = frozenset({InterventionAction.ESCALATE_HUMAN, InterventionAction.STOP_PERMANENT})
_FAMILIES = (_RETRY_FAMILY, _CONTACT_FAMILY, _TERMINAL_FAMILY)


def _same_family(a: InterventionAction, b: InterventionAction) -> bool:
    if a == b:
        return True
    return any(a in fam and b in fam for fam in _FAMILIES)


class PolicyViolation(RuntimeError):
    """Raised if a rule pipeline result would increase blast radius. Should be unreachable — every
    rule in stopping_rules.py is written to only ever hold or reduce rank — but this is money
    movement, so the invariant is checked rather than trusted."""


@dataclass(frozen=True)
class L1Proposal:
    failure_class: FailureClass
    proposed_action: InterventionAction
    proposed_scheduled_for: datetime | None
    rationale: str


@dataclass(frozen=True)
class PolicyDecision:
    permitted_action: InterventionAction
    permitted_scheduled_for: datetime | None
    vetoed: bool                 # True iff the permitted action's FAMILY differs from proposed
                                  # (retry/contact/terminal — see _FAMILIES). A same-family action
                                  # swap or timing push does not count as a veto, only a downgrade.
    downgraded: bool             # True iff anything changed at all (action and/or timing)
    rules_fired: tuple[str, ...]  # rule names, in firing order; empty if nothing fired
    notes: tuple[str, ...]


def evaluate(proposal: L1Proposal, ctx: PolicyContext) -> PolicyDecision:
    if ctx.failure_class != proposal.failure_class:
        raise ValueError("PolicyContext.failure_class must match the proposal being evaluated")

    action = proposal.proposed_action
    scheduled_for = proposal.proposed_scheduled_for
    rules_fired: list[str] = []
    notes: list[str] = []
    proposed_rank = BLAST_RADIUS_RANK[proposal.proposed_action]

    for rule in RULE_PIPELINE:
        outcome: RuleOutcome = rule(ctx, action, scheduled_for)
        if not outcome.fired:
            continue

        rules_fired.append(outcome.rule.value)
        notes.append(outcome.note)

        if outcome.forced_action is not None:
            action = outcome.forced_action
        if outcome.forced_scheduled_for is not None:
            scheduled_for = outcome.forced_scheduled_for
        else:
            # Every firing rule sets at least one of forced_action/forced_scheduled_for (see
            # RULE_PIPELINE's contract note) — so reaching here means forced_action was set
            # instead, to something that isn't a scheduled retry at all (ESCALATE_HUMAN,
            # REQUEST_INSTRUMENT_UPDATE, ...) — the old scheduled_for no longer means anything.
            scheduled_for = None

        new_rank = BLAST_RADIUS_RANK[action]
        if new_rank > proposed_rank:
            raise PolicyViolation(
                f"Rule {outcome.rule.value} increased blast radius: "
                f"{proposal.proposed_action.value} (rank {proposed_rank}) -> {action.value} (rank {new_rank})"
            )

    vetoed = not _same_family(action, proposal.proposed_action)
    downgraded = (
        action != proposal.proposed_action or scheduled_for != proposal.proposed_scheduled_for
    )

    return PolicyDecision(
        permitted_action=action,
        permitted_scheduled_for=scheduled_for,
        vetoed=vetoed,
        downgraded=downgraded,
        rules_fired=tuple(rules_fired),
        notes=tuple(notes),
    )
