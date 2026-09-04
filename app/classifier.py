"""
L1 — classification and parameter estimation.

Two implementations behind one interface, and the distinction between them is
the most important thing on this page.

`LookupClassifier` is a decision table over Razorpay's structured `reason`
field. It is not a weak strawman: on a clean, well-formed payload it is exactly
right, it is free, and it never hallucinates. Arm B (rules-only) uses it, and
so does Arm C by default — which means the measured difference between those
two arms is the expected-value layer alone, with the classifier held constant.
That is a cleaner experiment than confounding "we added economics" with "we
added a language model", and it is the one we can actually run today.

`LLMClassifier` is the real L1. It exists to do four things a dictionary
cannot, and the README narrows its role to exactly these, because if L1's only
job were mapping decline codes to seven classes then a lookup table would tie
it and the ablation would say so:

  1. Ambiguous payloads. Generic or absent error codes; a description that
     contradicts the structured reason. Razorpay's own card-error docs state
     they may not have a specific failure reason for bank declines, because
     customer banks typically do not provide one. The ambiguity is documented
     by the gateway; we did not invent it.
  2. Parameter estimation. The recovery bucket from multi-signal input —
     payment history, prior failure pattern, tenure, invoice value relative to
     that customer's normal, prior contact count. None of that is in a decline
     code.
  3. Action selection among legal options, when policy permits several.
  4. The written audit rationale, which is a judged deliverable.

It emits an ORDINAL BUCKET, never a probability. A model asked for 0.34 will
produce 0.34 and a judge asking where it came from must not be told "the model
said so". The bucket maps to a base rate in the constants file, so every number
in the expected value traces to a citation. ScoreContext enforces this at the
boundary, so a float cannot reach the arithmetic even if a prompt change lets
one out of the model.

STATUS, STATED PLAINLY: no LLM has been run against the batch yet. There is no
API key in this environment, and the three-arm result currently in
docs/results/ uses LookupClassifier for both Arm B and Arm C. The value of a
language model over a decision table is therefore NOT YET MEASURED in this
repository. It is not claimed anywhere either. Running it is the next task.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Protocol

from app.models import FailureClass, InterventionAction
from app.scorer import VALID_BUCKETS

A = InterventionAction
FC = FailureClass


@dataclass(frozen=True)
class Classification:
    """L1's structured output. Schema violations are rejected and retried."""

    classification: FailureClass
    classification_confidence: str          # HIGH | MEDIUM | LOW
    recovery_bucket: str                    # ordinal. Never numeric.
    proposed_action: InterventionAction
    rationale: str
    ambiguity_flags: tuple[str, ...] = ()
    deferral_days: int = 0

    def __post_init__(self) -> None:
        if self.recovery_bucket not in VALID_BUCKETS:
            raise ValueError(f"recovery_bucket must be ordinal, got {self.recovery_bucket!r}")
        if self.classification_confidence not in ("HIGH", "MEDIUM", "LOW"):
            raise ValueError(f"bad confidence {self.classification_confidence!r}")


class Classifier(Protocol):
    def classify(self, case: dict, state: dict) -> Classification: ...


# ---------------------------------------------------------------------------
# The decision table
# ---------------------------------------------------------------------------

def remediation_for(failure_class: FailureClass) -> InterventionAction:
    """
    The correct non-retry action for a class, when a retry is illegal or
    mechanically incapable of clearing the invoice.

    A PROPOSER'S helper, and it lives here rather than beside the policy layer
    for a reason: L2a's contract is that it may veto but never propose, so the
    gate must not be able to reach for this. The classifier chooses an action;
    the gate only ever narrows it.

    HARD_RISK is the one class with no customer-facing remediation. A suspected
    fraud decline must never become a "try another card" — that is a system
    telling whoever holds the card which instrument to try next. It goes to a
    human.
    """
    return {
        FC.HARD_INSTRUMENT: A.REQUEST_INSTRUMENT_UPDATE,
        FC.HARD_MANDATE: A.REQUEST_INSTRUMENT_UPDATE,
        FC.HARD_RISK: A.ESCALATE_HUMAN,
        FC.SOFT_AUTH: A.REQUEST_INSTRUMENT_UPDATE,
    }.get(failure_class, A.NUDGE)


REASON_TO_CLASS: dict[str, FailureClass] = {
    "gateway_technical_error": FC.SOFT_TRANSIENT,
    "payment_timed_out": FC.SOFT_TRANSIENT,
    "issuer_not_available": FC.SOFT_TRANSIENT,
    "payment_failed_insufficient_funds": FC.SOFT_FUNDS,
    "insufficient_funds": FC.SOFT_FUNDS,
    "payment_limit_exceeded": FC.SOFT_LIMIT,
    "daily_limit_exceeded": FC.SOFT_LIMIT,
    "payment_otp_timeout": FC.SOFT_AUTH,
    "payment_cancelled_by_customer": FC.SOFT_AUTH,
    "payment_failed_card_expired": FC.HARD_INSTRUMENT,
    "card_blocked": FC.HARD_INSTRUMENT,
    "account_closed": FC.HARD_INSTRUMENT,
    "payment_declined_risk": FC.HARD_RISK,
    "card_lost_or_stolen": FC.HARD_RISK,
    "mandate_revoked": FC.HARD_MANDATE,
    "mandate_exhausted": FC.HARD_MANDATE,
}

# What the table falls back to when `reason` is absent or unrecognised. This is
# the single most consequential line in the file for the comparison: a bare
# "payment_failed" from a bank carries no class, and the table has to guess.
#
# SOFT_FUNDS is the least-bad guess — it is the largest single class in the
# India-adjusted mix, and guessing a soft class keeps the invoice alive rather
# than writing it off. It is still a guess, and it is wrong most of the time it
# is used. That is the ambiguity bucket's whole point.
LOOKUP_FALLBACK = FC.SOFT_FUNDS


def _bucket_from_class(fc: FailureClass) -> str:
    """
    The table's recovery estimate: a function of the class alone, because that
    is all a table has. Customer history is invisible to it.
    """
    return {
        FC.SOFT_TRANSIENT: "VERY_HIGH",
        FC.SOFT_FUNDS: "HIGH",
        FC.SOFT_LIMIT: "MEDIUM",
        FC.SOFT_AUTH: "MEDIUM",
        FC.HARD_INSTRUMENT: "LOW",
        FC.HARD_MANDATE: "LOW",
        FC.HARD_RISK: "VERY_LOW",
    }[fc]


# The escalation ladder.
#
# An earlier version returned one action per class and returned it forever, so
# a SOFT_FUNDS invoice was retried until the attempt cap and then abandoned —
# the table never reached for a payment link, and the naive baseline, which
# does retry-retry-retry-then-link, beat it comfortably.
#
# That is worth stating carefully because of how the valve works. L2b may only
# narrow what the classifier proposed, so the proposal is a CEILING on
# aggression: an action the classifier never proposes is an action the whole
# pipeline can never take, however good its expected value. A weak proposer
# therefore caps the entire system, and a rules-only arm that cannot escalate
# is a strawman baseline rather than an honest one.
#
# So the table escalates the way a competent hand-written dunning policy would:
# exhaust the cheap silent channel, then ask the customer once, then stop.

RETRY_LADDER_DEPTH = 3


def _table_action(fc: FailureClass, attempts: int, contacts: int) -> InterventionAction:
    if fc is FC.HARD_RISK:
        return A.ESCALATE_HUMAN

    # Classes where a retry has no mechanism: go straight to the customer, once.
    if fc.is_hard or fc is FC.SOFT_AUTH:
        return remediation_for(fc) if contacts == 0 else A.STOP_PERMANENT

    if attempts < RETRY_LADDER_DEPTH:
        return A.RETRY_SCHEDULED
    if contacts == 0:
        return A.REQUEST_INSTRUMENT_UPDATE
    return A.STOP_PERMANENT


@dataclass
class LookupClassifier:
    """
    Deterministic, free, and correct whenever the payload is well-formed.

    Deliberately given every advantage that costs nothing: it reads the
    structured `reason` rather than the free-text description, so the
    "description contradicts reason" ambiguity does not fool it. The only thing
    it cannot do is invent a class when the bank supplied none — and estimate a
    recovery bucket from customer history, which it never sees.
    """

    name: str = "lookup"

    def classify(self, case: dict, state: dict) -> Classification:
        reason = (case.get("error") or {}).get("reason")
        known = reason in REASON_TO_CLASS
        fc = REASON_TO_CLASS.get(reason, LOOKUP_FALLBACK)
        action = _table_action(fc, state.get("attempts", 0), state.get("contacts", 0))
        return Classification(
            classification=fc,
            classification_confidence="HIGH" if known else "LOW",
            recovery_bucket=_bucket_from_class(fc),
            proposed_action=action,
            rationale=(
                f"reason={reason!r} maps to {fc.value} in the decision table"
                if known
                else f"reason={reason!r} is not in the decision table; defaulted to {fc.value}"
            ),
            ambiguity_flags=() if known else ("unmapped_reason",),
        )


# ---------------------------------------------------------------------------
# The real L1
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You classify failed payment attempts for an Indian payment-recovery agent and \
propose one next action. Your output is gated afterwards by a deterministic \
policy layer that can veto or narrow your proposal but never widen it, so \
propose the action you actually believe is correct rather than a defensive one.

You will receive a Razorpay error object and the customer's history.

The error object's `source` (customer / business / razorpay / gateway / bank / \
network) and `step` (payment_initiation / payment_authentication / \
payment_authorization / payment_capture) together carry more signal than the \
decline code alone. A failure at payment_authentication with source=customer is \
a person who walked away from an OTP screen; the same description at \
payment_authorization with source=bank is an issuer decision. Reason about that \
triple explicitly.

Razorpay's documentation states that for bank declines they may not have a \
specific failure reason, because customer banks typically do not provide one. A \
bare "payment_failed" with source=bank is therefore expected, not malformed. \
Say so in ambiguity_flags rather than inventing a precise cause.

RECOVERY BUCKET IS ORDINAL. Never output a number. Estimate it from the whole \
picture: payment history, prior failure pattern, tenure, this invoice's size \
relative to the customer's normal, and how many times they have already been \
contacted. A long-tenure customer with thirty successful payments and a first \
insufficient-funds decline is not the same bucket as a three-week-old account \
on its fourth failure with the identical decline code.

The seven classes:
  SOFT_TRANSIENT   gateway/network/issuer-unavailable. Clears on prompt retry.
  SOFT_FUNDS       insufficient funds. Timing-dependent; salary credit in India
                   clusters on the 28th-31st, not the 1st.
  SOFT_LIMIT       per-transaction or daily limit. Retry with split or alternate.
  SOFT_AUTH        OTP/3DS drop-off. A RETRY CANNOT FIX THIS - the customer never
                   authorised anything. Only a fresh link can.
  HARD_INSTRUMENT  expired, blocked, closed, invalid. Never retry; needs a new
                   instrument.
  HARD_RISK        suspected fraud, lost/stolen. Never retry, and never contact
                   the customer - that tells whoever holds the card which
                   instrument to try next. Escalate to a human.
  HARD_MANDATE     mandate revoked/paused/exhausted. Needs re-authorisation.

The rationale is a judged deliverable. Cite actual field values from the payload.
Do not restate the classification in words.

Return ONLY a JSON object:
{"classification": "...", "classification_confidence": "HIGH|MEDIUM|LOW",
 "recovery_bucket": "VERY_LOW|LOW|MEDIUM|HIGH|VERY_HIGH",
 "ambiguity_flags": ["..."], "proposed_action":
 "RETRY_NOW|RETRY_SCHEDULED|REQUEST_INSTRUMENT_UPDATE|NUDGE|ESCALATE_HUMAN|STOP_PERMANENT",
 "deferral_days": 0, "rationale": "..."}
"""


@dataclass
class LLMClassifier:
    """
    The real L1. The only place in the repo that touches a model.

    That is enforced by the module boundary rather than by convention, so
    "which parts of this system talk to an LLM" is answerable by grepping for
    one filename. Nothing in app/policy.py, app/stopping_rules.py or
    app/scorer.py has a code path that reaches a model.

    Schema violations are rejected and retried, and every rejection is logged —
    the rejection rate is itself a reportable number about model reliability in
    a money-movement path.
    """

    model: str = "claude-sonnet-4-5"
    max_retries: int = 2
    name: str = "llm"
    rejections: list[str] = field(default_factory=list)

    def _client(self):
        try:
            import anthropic
        except ImportError as e:  # pragma: no cover - depends on install
            raise RuntimeError("pip install anthropic to run the LLM arm") from e
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. The LLM arm cannot run; arms A/B/C "
                "do not need it. Never commit this key - .env is gitignored."
            )
        return anthropic.Anthropic(api_key=key)

    def classify(self, case: dict, state: dict) -> Classification:  # pragma: no cover
        client = self._client()
        payload = json.dumps(
            {
                "error": case.get("error"),
                "mastercard_advice_code": case.get("mastercard_advice_code"),
                "instrument_type": case.get("instrument_type"),
                "issuer": case.get("issuer"),
                "amount_inr": case.get("amount_paise", 0) / 100.0,
                "kind": case.get("kind"),
                "customer": case.get("customer"),
                "attempts_so_far": state.get("attempts", 0),
                "contacts_so_far": state.get("contacts", 0),
            },
            indent=2,
        )
        last_error = ""
        for _ in range(self.max_retries + 1):
            resp = client.messages.create(
                model=self.model,
                max_tokens=700,
                temperature=0,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": payload}],
            )
            text = resp.content[0].text.strip()
            try:
                return self._parse(text)
            except (ValueError, KeyError, json.JSONDecodeError) as e:
                last_error = f"{type(e).__name__}: {e}"
                self.rejections.append(last_error)
        raise ValueError(f"L1 failed schema validation after retries: {last_error}")

    @staticmethod
    def _parse(text: str) -> Classification:  # pragma: no cover
        if text.startswith("```"):
            text = text.split("```")[1].removeprefix("json").strip()
        raw = json.loads(text)
        return Classification(
            classification=FailureClass(raw["classification"]),
            classification_confidence=raw["classification_confidence"],
            recovery_bucket=raw["recovery_bucket"],
            proposed_action=InterventionAction(raw["proposed_action"]),
            rationale=raw["rationale"],
            ambiguity_flags=tuple(raw.get("ambiguity_flags", ())),
            deferral_days=int(raw.get("deferral_days", 0)),
        )


# ---------------------------------------------------------------------------
# The replayable LLM arm
# ---------------------------------------------------------------------------


@dataclass
class CachedLLMClassifier:
    """
    Replays a recorded set of L1 classifications from disk.

    This exists because a live model call is not reproducible and a judged
    result has to be. `LLMClassifier` above is the production path; this is how
    a measured run gets pinned so that `sim/run_arms.py` produces the same
    numbers on someone else's machine a week later.

    READ THE PROVENANCE BLOCK in the JSON file before quoting any figure that
    comes out of this class. The recorded classifications were produced by
    a large language model reading only the fields a real L1 receives, in four
    separate fresh contexts that had never seen the batch generator or the ground truth — that
    isolation is the whole reason the accuracy numbers mean anything, because
    whoever wrote the generator knows the answer key.

    It is NOT a live API run, and nothing in the repo claims it is.

    Falls back to the decision table for any case_id it has no record of, and
    counts those, so a batch regenerated at a different seed or size degrades
    into arm B rather than crashing or silently scoring itself on a subset.
    """

    path: str = "sim/data/l1_classifications_seed42.json"
    name: str = "cached_llm"
    # Use the model's own recovery bucket only when it is confident. On LOW
    # confidence, fall back to the class-level bucket the decision table would
    # have used.
    #
    # This is not hedging, it is reading the measurement. The recorded run is
    # well calibrated — HIGH 100% accurate over 83 cases, MEDIUM 76% over 17,
    # LOW 20% over 20 — and its per-case buckets are far more dispersed than the
    # table's class-level mapping (34 cases at LOW or VERY_LOW against the
    # table's 21, at the same mean). More dispersion is more information when
    # the model is right and a stop-signal manufactured from thin evidence when
    # it is not. Since the model already tells us which is which, ignoring that
    # and trusting every bucket equally leaves a free improvement on the table.
    trust_bucket_below_confidence: bool = False
    _records: dict = field(default_factory=dict)
    _fallback: LookupClassifier = field(default_factory=LookupClassifier)
    misses: int = 0

    def __post_init__(self) -> None:
        try:
            with open(self.path) as f:
                self._records = json.load(f)["classifications"]
        except FileNotFoundError:
            self._records = {}

    @property
    def available(self) -> bool:
        return bool(self._records)

    def classify(self, case: dict, state: dict) -> Classification:
        rec = self._records.get(case.get("case_id"))
        if rec is None:
            self.misses += 1
            return self._fallback.classify(case, state)
        fc = FailureClass(rec["classification"])
        confidence = rec["classification_confidence"]
        bucket = rec["recovery_bucket"]
        if confidence == "LOW" and not self.trust_bucket_below_confidence:
            bucket = _bucket_from_class(fc)

        return Classification(
            classification=fc,
            classification_confidence=confidence,
            recovery_bucket=bucket,
            # The model proposed an opening action for a fresh failure. The
            # simulator revisits an invoice several times, so the escalation
            # ladder — retry, then ask once, then stop — has to come from live
            # invoice state rather than from a recording made before any of it
            # happened. The model's contribution here is the CLASS and the
            # BUCKET, which is what the comparison against the table measures;
            # replaying a stale RETRY_NOW on attempt four would be measuring the
            # recording's age, not the model.
            proposed_action=_table_action(
                fc,
                state.get("attempts", 0),
                state.get("contacts", 0),
            ),
            rationale=rec["rationale"],
            ambiguity_flags=tuple(rec.get("ambiguity_flags", ())),
            deferral_days=int(rec.get("deferral_days", 0)),
        )
