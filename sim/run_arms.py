"""
Three arms, one batch, one seed.

    Arm A  NAIVE        Razorpay Subscriptions' documented default behaviour.
    Arm B  RULES_ONLY   decision table + L2a. No economics.
    Arm C  BACKSTOP     decision table + L2b + L2a.

------------------------------------------------------------------------------
Why Arm C uses the same classifier as Arm B
------------------------------------------------------------------------------

The obvious design makes Arm C "LLM + everything" and Arm B "table + rules", and
then reports the difference as though it measured one thing. It measures two —
adding economics and adding a language model — confounded. If Arm C wins,
nothing in the result says which change did it.

Holding the classifier fixed makes A/B/C an ablation of the expected-value layer
specifically, which is the layer this week's work added, and it runs today with
no API key and no vendor dependency. The language model's marginal contribution
over the table is a separate measurement on the ambiguous and context-dependent
buckets, and it has NOT been made yet — see the status note in
app/classifier.py. Nothing here claims otherwise.

------------------------------------------------------------------------------
Why the naive arm is not a strawman
------------------------------------------------------------------------------

It is Razorpay's own published default: on a failed auto-charge the subscription
moves to `pending`, Razorpay retries once a day on T+1, T+2 and T+3, and if all
three fail it moves to `halted` and the customer is emailed a card-update link.
That is a genuinely reasonable policy, which is the point — beating a strawman
we invented would prove nothing, and this is a behaviour a Razorpay judge
already knows the shape of.

------------------------------------------------------------------------------
Beliefs against truth
------------------------------------------------------------------------------

The agent decides using `Beliefs` (world_model_constants.py, optionally
perturbed). The world resolves outcomes using `WorldParams` (world_model.py).
Different functional forms, different numbers, and neither reads the other.

`--belief-error` hands the agent deliberately wrong parameters so the robustness
arm can show the advantage degrading rather than inverting. `--adversarial`
switches contact fatigue and issuer penalties off in the WORLD, so blind
retrying costs nothing beyond the gateway fee.

Usage:
    python -m sim.run_arms --n 120 --seed 42
    python -m sim.run_arms --adversarial
    python -m sim.run_arms --belief-error 0.5 --belief-error 1.5
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

from app.classifier import Classifier, LookupClassifier
from app.models import FailureClass, InterventionAction
from app.policy import L1Proposal, evaluate
from app.rule_basis import basis_of
from app.scorer import Beliefs, ScoreContext, score
from app.stopping_rules import PolicyContext
from sim import world_model as W
from sim.generate_batch import generate_batch

A = InterventionAction
FC = FailureClass

HORIZON_DAYS = 30
MAX_STEPS = 40          # loop guard; a decision path longer than this is a bug


# ---------------------------------------------------------------------------
# World
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WorldParams:
    """
    Ground truth. The agent never sees this object.

    Separate from Beliefs by construction rather than by discipline: there is no
    code path from a scorer to these numbers.
    """

    churn_ceiling: float = W.ASSUMED_WORLD_CHURN_CEILING
    churn_saturation: float = W.ASSUMED_WORLD_CHURN_SATURATION
    patience_recovery_days: float = W.ASSUMED_WORLD_PATIENCE_RECOVERY_DAYS
    unrecovered_churn: float = W.ASSUMED_WORLD_UNRECOVERED_CHURN
    issuer_penalty_inr: float = W.ASSUMED_WORLD_ISSUER_PENALTY_INR
    retry_fee_inr: float = W.ASSUMED_WORLD_RETRY_FEE_INR
    nudge_lift: float = W.ASSUMED_NUDGE_LIFT

    def churn_probability(self, contacts: int, days_since_last: float | None) -> float:
        if contacts <= 0 or self.churn_ceiling <= 0:
            return 0.0
        effective = float(contacts)
        if days_since_last is not None and self.patience_recovery_days > 0:
            recovered = min(1.0, days_since_last / self.patience_recovery_days)
            effective = max(0.0, effective - recovered * effective)
        return self.churn_ceiling * (1.0 - math.exp(-effective / self.churn_saturation))


ADVERSARIAL = WorldParams(churn_ceiling=0.0, issuer_penalty_inr=0.0)


# ---------------------------------------------------------------------------
# Per-invoice state
# ---------------------------------------------------------------------------


@dataclass
class InvoiceState:
    case: dict
    now: datetime
    attempts: int = 0
    fast_retries: int = 0
    contacts: int = 0
    last_attempt_at: datetime | None = None
    last_contact_at: datetime | None = None
    predebit_notice_sent_at: datetime | None = None
    recovered: bool = False
    churned: bool = False
    closed: bool = False
    close_reason: str = ""
    events: list[dict] = field(default_factory=list)

    @property
    def amount_inr(self) -> float:
        return self.case["amount_paise"] / 100.0

    @property
    def true_class(self) -> FailureClass:
        return FailureClass(self.case["ground_truth"]["true_class"])

    @property
    def days_since_last_contact(self) -> float | None:
        if self.last_contact_at is None:
            prior = self.case["customer"].get("days_since_last_contact")
            return float(prior) if prior is not None else None
        return (self.now - self.last_contact_at).total_seconds() / 86400.0

    @property
    def total_contacts(self) -> int:
        """Contacts this run plus the history the customer arrived with."""
        return self.contacts + int(self.case["customer"].get("prior_contacts_30d", 0))


# ---------------------------------------------------------------------------
# Arms
#
# Each arm's step() returns (proposed_action, why, declared_class). The declared
# class is what the arm BELIEVES the failure is — L2a is gated on that, not on
# ground truth, exactly as it would be in production. Passing the true class
# here would quietly hand the policy layer information the pipeline never had.
# ---------------------------------------------------------------------------


class NoActionArm:
    """
    The zero. Never touches an invoice, so every one of them ages out and takes
    the customer with it at the world's lapse rate.

    This arm exists because the absolute net figure is otherwise unreadable. A
    batch is mostly invoices that were never going to be recovered, so every
    arm's absolute net is a large negative number dominated by losses no policy
    could have prevented, and comparing two large negatives tells a reader
    nothing. Measured against this floor, each arm's number is the value its
    decisions actually ADDED, which is the quantity anyone cares about.
    """

    name = "0_do_nothing"
    uses_gate = False

    def step(self, st: InvoiceState):
        return A.STOP_PERMANENT, "reference arm: no action ever taken", st.true_class


class NaiveArm:
    """
    Razorpay Subscriptions' documented default: retry T+1, T+2, T+3; on the
    third failure, halt and email a card-update link.

    Runs no policy gate and no scorer. It is what the platform does out of the
    box, and it is the number Backstop has to beat.
    """

    name = "A_naive"
    uses_gate = False

    def step(self, st: InvoiceState):
        if st.attempts < 3:
            return A.RETRY_NOW, "Razorpay default schedule T+1/T+2/T+3", st.true_class
        if st.contacts == 0:
            return A.REQUEST_INSTRUMENT_UPDATE, "halted: card-update email", st.true_class
        return A.STOP_PERMANENT, "schedule exhausted", st.true_class


class RulesOnlyArm:
    """The decision table's proposal, gated by L2a. No economics anywhere."""

    name = "B_rules_only"
    uses_gate = True

    def __init__(self, classifier: Classifier):
        self.classifier = classifier

    def step(self, st: InvoiceState):
        c = self.classifier.classify(st.case, {"attempts": st.attempts, "contacts": st.contacts})
        return c.proposed_action, c.rationale, c.classification


class BackstopArm:
    """
    The full pipeline: the table proposes, L2b prices, L2a gates.

    L2b may only narrow what the table proposed, so the proposal is a ceiling on
    aggression and the economics decide everything below it.
    """

    name = "C_backstop"
    uses_gate = True

    def __init__(self, classifier: Classifier, beliefs: Beliefs):
        self.classifier = classifier
        self.beliefs = beliefs

    def step(self, st: InvoiceState):
        c = self.classifier.classify(st.case, {"attempts": st.attempts, "contacts": st.contacts})
        sctx = ScoreContext(
            invoice_value_inr=st.amount_inr,
            recovery_bucket=c.recovery_bucket,
            failure_class=c.classification,
            attempt_no=st.attempts + 1,
            contacts_so_far=st.total_contacts,
            days_since_last_contact=st.days_since_last_contact,
            now=st.now,
            is_recurring=st.case["kind"] == "RECURRING",
            mastercard_advice_code=st.case.get("mastercard_advice_code"),
        )
        result = score(c.proposed_action, sctx, self.beliefs)
        best = result.scores[0]
        why = (
            f"EV({result.chosen.value})={best.ev:.2f} "
            f"[recovery {best.recovery_value:.2f}, issuer -{best.issuer_trust_term:.2f}, "
            f"churn -{best.churn_term:.2f}]"
        )
        return result.chosen, why, c.classification


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass
class ArmResult:
    arm: str
    gross_recovered_inr: float = 0.0
    churn_loss_inr: float = 0.0
    retry_fees_inr: float = 0.0
    issuer_penalties_inr: float = 0.0
    invoices: int = 0
    recovered: int = 0
    churned: int = 0
    attempts: int = 0
    contacts: int = 0
    vetoes: int = 0
    veto_by_rule: dict[str, int] = field(default_factory=dict)
    veto_by_basis: dict[str, int] = field(default_factory=dict)
    under_proposals: int = 0
    under_proposal_value_inr: float = 0.0
    by_bucket: dict[str, dict] = field(default_factory=dict)
    traces: list[dict] = field(default_factory=list)

    @property
    def net_recovered_inr(self) -> float:
        """The headline. Gross recovered, minus every cost the actions incurred."""
        return (
            self.gross_recovered_inr
            - self.churn_loss_inr
            - self.retry_fees_inr
            - self.issuer_penalties_inr
        )

    def value_added_over(self, floor: "ArmResult") -> float:
        """
        The headline. Net value this arm's decisions added over never acting.

        Absolute net is always a large negative — most of a realistic batch is
        unrecoverable, and those losses belong to the world, not to the policy.
        The difference from the do-nothing floor is the part a policy is
        responsible for.
        """
        return self.net_recovered_inr - floor.net_recovered_inr

    @property
    def contacts_per_recovery(self) -> float:
        return self.contacts / self.recovered if self.recovered else float("inf")

    @property
    def attempts_per_recovery(self) -> float:
        return self.attempts / self.recovered if self.recovered else float("inf")

    def summary(self, floor: "ArmResult | None" = None) -> dict:
        return {
            "arm": self.arm,
            "value_added_inr": round(self.value_added_over(floor), 2) if floor else None,
            "net_recovered_inr": round(self.net_recovered_inr, 2),
            "gross_recovered_inr": round(self.gross_recovered_inr, 2),
            "churn_loss_inr": round(self.churn_loss_inr, 2),
            "issuer_penalties_inr": round(self.issuer_penalties_inr, 2),
            "retry_fees_inr": round(self.retry_fees_inr, 2),
            "invoices": self.invoices,
            "recovered": self.recovered,
            "churned": self.churned,
            "attempts": self.attempts,
            "contacts": self.contacts,
            "contacts_per_recovery": round(self.contacts_per_recovery, 3),
            "attempts_per_recovery": round(self.attempts_per_recovery, 3),
            "veto_rate": round(self.vetoes / max(1, self.invoices), 3),
            "veto_by_rule": self.veto_by_rule,
            "veto_by_basis": self.veto_by_basis,
            "under_proposals": self.under_proposals,
            "under_proposal_value_inr": round(self.under_proposal_value_inr, 2),
            "by_bucket": self.by_bucket,
        }


def _policy_ctx(st: InvoiceState, declared: FailureClass) -> PolicyContext:
    """
    Gather everything L2a is allowed to see.

    Two details that are easy to get wrong and change the result if you do.

    The class passed in is the one the arm DECLARED, never ground truth. L2a in
    production only ever sees what the pipeline told it, and handing it the true
    class here would quietly give the policy layer information the system never
    had — which would make the veto-rate metric measure a system that does not
    exist.

    Times are timezone-aware UTC because L2a's quiet-hours rule converts to IST
    itself. Passing a naive datetime would either raise or, worse, be silently
    treated as UTC in one place and local in another.
    """
    return PolicyContext(
        now_utc=_utc(st.now),
        failure_class=declared,
        attempts_so_far=st.attempts,
        last_attempt_at_utc=_utc(st.last_attempt_at) if st.last_attempt_at else None,
        fast_retries_used=st.fast_retries,
        customer_contacts_in_window=st.total_contacts,
        issuer_breaker_tripped=False,
        issuer_breaker_reset_eta_utc=None,
        is_recurring=st.case["kind"] == "RECURRING",
        mastercard_advice_code=st.case.get("mastercard_advice_code"),
        predebit_notice_sent_at_utc=(
            _utc(st.predebit_notice_sent_at) if st.predebit_notice_sent_at else None
        ),
        card_reattempts_in_30d=st.attempts,
        auth_attempts_in_24h=0,
    )


def _utc(dt: datetime) -> datetime:
    """
    The batch carries naive timestamps and L2a requires aware UTC.

    The simulation clock is UTC throughout — `InvoiceState.now` is naive UTC, not
    naive IST. That distinction is not cosmetic: L2a's quiet-hours rule converts
    to IST itself, so feeding it an IST-valued datetime tagged as UTC would shift
    the protected window by five and a half hours and silently send messages at
    3am. Anything rendering these times for a human should convert explicitly
    rather than labelling them.
    """
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _execute(st: InvoiceState, action: InterventionAction, rng: random.Random,
             world: WorldParams, res: ArmResult) -> None:
    """
    Roll the world for one action and update state.

    The agent's beliefs are absent from this function entirely. Every outcome
    here comes from ground truth or WorldParams.
    """
    truth = st.case["ground_truth"]

    if action in (A.RETRY_NOW, A.RETRY_SCHEDULED):
        st.attempts += 1
        res.attempts += 1
        res.retry_fees_inr += world.retry_fee_inr
        if st.true_class is FC.SOFT_TRANSIENT and action is A.RETRY_NOW:
            st.fast_retries += 1

        needed = truth["retry_attempts_needed"]
        succeeded = needed is not None and st.attempts >= needed
        if succeeded:
            st.recovered = True
            st.closed = True
            st.close_reason = f"recovered on attempt {st.attempts}"
            res.gross_recovered_inr += st.amount_inr
            res.recovered += 1
        else:
            res.issuer_penalties_inr += world.issuer_penalty_inr
        st.last_attempt_at = st.now
        st.events.append({"t": st.now.isoformat(), "action": action.value,
                          "outcome": "SUCCEEDED" if succeeded else "FAILED"})
        return

    if action in (A.NUDGE, A.REQUEST_INSTRUMENT_UPDATE):
        # Churn is rolled against the INCREMENT this contact adds, so a customer
        # cannot be churned twice by the same accumulated hazard.
        before = world.churn_probability(st.total_contacts, st.days_since_last_contact)
        st.contacts += 1
        res.contacts += 1
        after = world.churn_probability(st.total_contacts, 0.0)
        increment = max(0.0, after - before)
        churned = rng.random() < increment

        recovers = (
            truth["link_recovers"]
            if action is A.REQUEST_INSTRUMENT_UPDATE
            else rng.random() < world.nudge_lift
        )
        st.last_contact_at = st.now

        if churned:
            st.churned = True
            st.closed = True
            st.close_reason = "customer churned after contact"
            res.churned += 1
            res.churn_loss_inr += st.amount_inr * truth["ltv_multiple"]
        elif recovers:
            st.recovered = True
            st.closed = True
            st.close_reason = "recovered via customer action"
            res.gross_recovered_inr += st.amount_inr
            res.recovered += 1
        st.events.append({
            "t": st.now.isoformat(), "action": action.value,
            "outcome": "CHURNED" if churned else ("SUCCEEDED" if recovers else "IGNORED"),
        })
        return

    # ESCALATE_HUMAN / STOP_PERMANENT close the invoice without touching anyone.
    st.closed = True
    st.close_reason = action.value.lower()
    st.events.append({"t": st.now.isoformat(), "action": action.value, "outcome": "CLOSED"})


def simulate(cases: list[dict], arm, world: WorldParams, seed: int,
             collect_traces: int = 0) -> ArmResult:
    rng = random.Random(seed)
    res = ArmResult(arm=arm.name)
    buckets: dict[str, dict] = {}

    for case in cases:
        st = InvoiceState(case=case, now=datetime.fromisoformat(case["failed_at"]))
        deadline = st.now + timedelta(days=HORIZON_DAYS)
        # A recurring invoice arrives with the RBI pre-debit notice already sent
        # for the original charge. What the rule actually constrains is a retry
        # inside the window that notice opened.
        if case["kind"] == "RECURRING":
            st.predebit_notice_sent_at = st.now - timedelta(hours=30)

        trace: list[dict] = []
        res.invoices += 1
        b = buckets.setdefault(
            case["bucket"],
            {"invoices": 0, "recovered": 0, "contacts": 0, "attempts": 0, "net_inr": 0.0},
        )
        b["invoices"] += 1
        before_net = (res.gross_recovered_inr - res.churn_loss_inr
                      - res.retry_fees_inr - res.issuer_penalties_inr)
        before_contacts, before_attempts = res.contacts, res.attempts

        for _ in range(MAX_STEPS):
            if st.closed or st.now >= deadline:
                break

            proposed, why, declared = arm.step(st)
            step_log = {"t": st.now.isoformat(), "proposed": proposed.value, "why": why,
                        "declared_class": declared.value}

            if arm.uses_gate:
                proposal = L1Proposal(
                    failure_class=declared,
                    proposed_action=proposed,
                    proposed_scheduled_for=None,
                    rationale=why,
                )
                d = evaluate(proposal, _policy_ctx(st, declared))
                bases = [basis_of(r) for r in d.rules_fired]
                step_log |= {
                    "permitted": d.permitted_action.value,
                    "vetoed": d.vetoed,
                    "downgraded": d.downgraded,
                    "rules_fired": list(d.rules_fired),
                    "bases": bases,
                    "notes": list(d.notes),
                }

                if d.downgraded:
                    res.vetoes += 1
                    for rule, basis in zip(d.rules_fired, bases):
                        res.veto_by_rule[rule] = res.veto_by_rule.get(rule, 0) + 1
                        res.veto_by_basis[basis] = res.veto_by_basis.get(basis, 0) + 1

                # A deferral is a wait, not an abandonment. L2a expresses one by
                # returning RETRY_SCHEDULED with a future time; honouring it is
                # what stops the quiet-hours and mandate-notice rules from
                # costing us invoices they were only meant to delay.
                sched = d.permitted_scheduled_for
                if sched is not None and _utc(sched) > _utc(st.now):
                    if _utc(sched) >= _utc(deadline):
                        trace.append(step_log | {"resolution": "deferred past horizon"})
                        break
                    st.now = sched.replace(tzinfo=None) if sched.tzinfo else sched
                    trace.append(step_log | {"resolution": "deferred"})
                    continue

                action = d.permitted_action
            else:
                action = proposed

            trace.append(step_log)
            _execute(st, action, rng, world, res)

            if st.closed:
                break
            st.now += timedelta(hours=24)

        if not st.closed:
            st.close_reason = "horizon reached"

        # Terminal accounting. An invoice nobody recovered loses the customer
        # some of the time — this is what makes stopping cost something, and
        # without it the optimal policy is to stop on everything and the
        # frontier chart collapses to a point at the origin.
        if not st.recovered and not st.churned:
            if rng.random() < world.unrecovered_churn:
                res.churn_loss_inr += st.amount_inr * case["ground_truth"]["ltv_multiple"]
                res.churned += 1

        # Under-proposal: the agent gave up on an invoice that was still live.
        # L2a cannot catch this and nothing else reports it, so a system that
        # measured only vetoes would be telling half the story.
        gt = case["ground_truth"]
        if not st.recovered:
            was_live = (
                gt["retry_attempts_needed"] is not None
                and gt["retry_attempts_needed"] > st.attempts
            ) or (gt["link_recovers"] and st.contacts == 0)
            if was_live:
                res.under_proposals += 1
                res.under_proposal_value_inr += st.amount_inr

        if st.recovered:
            b["recovered"] += 1
        b["contacts"] += res.contacts - before_contacts
        b["attempts"] += res.attempts - before_attempts
        after_net = (res.gross_recovered_inr - res.churn_loss_inr
                     - res.retry_fees_inr - res.issuer_penalties_inr)
        b["net_inr"] = round(b["net_inr"] + (after_net - before_net), 2)

        if len(res.traces) < collect_traces:
            res.traces.append({
                "case_id": case["case_id"],
                "bucket": case["bucket"],
                "amount_inr": st.amount_inr,
                "kind": case["kind"],
                "issuer": case["issuer"],
                "instrument_type": case["instrument_type"],
                "mastercard_advice_code": case.get("mastercard_advice_code"),
                "ambiguity": case.get("ambiguity", []),
                "ground_truth": gt,
                "error": case["error"],
                "customer": case["customer"],
                "steps": trace,
                "events": st.events,
                "outcome": st.close_reason,
                "recovered": st.recovered,
                "churned": st.churned,
            })

    res.by_bucket = buckets
    return res


def build_arms(beliefs: Beliefs) -> list:
    table = LookupClassifier()
    return [NoActionArm(), NaiveArm(), RulesOnlyArm(table), BackstopArm(table, beliefs)]


def run(n: int, seed: int, world: WorldParams, beliefs: Beliefs,
        collect_traces: int = 0) -> dict[str, ArmResult]:
    cases = [asdict(c) for c in generate_batch(n, seed)]
    return {
        arm.name: simulate(cases, arm, world, seed, collect_traces)
        for arm in build_arms(beliefs)
    }


def _print(results: dict[str, ArmResult], title: str) -> None:
    print(f"\n=== {title} ===")
    floor = results["0_do_nothing"]
    hdr = (f"{'arm':<15}{'value added':>13}{'gross':>10}{'rec':>5}{'att':>6}"
           f"{'cnt':>5}{'att/rec':>9}{'cnt/rec':>9}{'veto':>6}{'under':>7}")
    print(hdr)
    print("-" * len(hdr))
    for name, r in results.items():
        va = r.value_added_over(floor)
        print(f"{name:<15}{va:>13,.0f}{r.gross_recovered_inr:>10,.0f}{r.recovered:>5}"
              f"{r.attempts:>6}{r.contacts:>5}{r.attempts_per_recovery:>9.2f}"
              f"{r.contacts_per_recovery:>9.2f}{r.vetoes:>6}{r.under_proposals:>7}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--n", type=int, default=120)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--adversarial", action="store_true",
                    help="world with zero contact fatigue and zero issuer penalty")
    ap.add_argument("--belief-error", type=float, action="append", default=[],
                    help="multiply the agent's churn belief by this factor; repeatable")
    ap.add_argument("--out", type=str, default="sim/data/arms.json")
    args = ap.parse_args()

    beliefs = Beliefs.from_constants()
    world = ADVERSARIAL if args.adversarial else WorldParams()

    results = run(args.n, args.seed, world, beliefs, collect_traces=3)
    _print(results, "ADVERSARIAL WORLD (no contact fatigue, no issuer penalty)"
           if args.adversarial else "BASELINE")

    floor = results["0_do_nothing"]
    payload = {"baseline": {k: v.summary(floor) for k, v in results.items()}}

    for factor in args.belief_error:
        wrong = beliefs.perturbed({"contact_fatigue_base": factor})
        r = run(args.n, args.seed, world, wrong)
        _print(r, f"AGENT BELIEVES CHURN x{factor} (world unchanged)")
        payload[f"belief_x{factor}"] = {k: v.summary(r["0_do_nothing"]) for k, v in r.items()}

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"\nWrote {args.out}")


if __name__ == "__main__":
    main()
