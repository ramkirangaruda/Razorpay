"""
What each stopping rule actually rests on.

The reframe this project turns on is that restraint is an economic result, not a
rule set — so the rules that remain need to declare honestly which of them are
somebody else's law and which are ours. Without that split, "the policy gate
fired 47 times" is an unreadable number: a regulatory veto is the system working
exactly as designed, and a backstop veto is a finding that the expected-value
scorer was wrong and something had to catch it.

This lives in its own module rather than as a field on `RuleOutcome` because
`app/stopping_rules.py` is built and tested at 149 tests and the build spec is
explicit about not modifying its logic. A lookup keyed on `RuleName` gets the
same reporting for free and touches none of it.

    REGULATORY  Indian law or a card-network rule. Not a parameter, not
                sweepable, not ours. If one of these fires, the system prevented
                a compliance incident.

    BACKSTOP    Ours, and deliberately loose. These bound the damage a broken
                scorer can do, and they are set well outside the range the EV
                model should ever reach on its own. A backstop firing often is a
                statement about the scorer, and the veto-rate metric reports it
                as one rather than as a success.
"""

from __future__ import annotations

from app.stopping_rules import RuleName

REGULATORY = "REGULATORY"
BACKSTOP = "BACKSTOP"

# rule -> (basis, citation)
RULE_BASIS: dict[RuleName, tuple[str, str]] = {
    RuleName.HARD_DECLINE_NO_RETRY: (
        REGULATORY,
        "Visa Excessive Reattempts (Category 1); Mastercard TPE MAC 03/21. "
        "Reframed from 'our policy' — a Category 1 reattempt is excessive from the "
        "first attempt, which makes this the network's rule and not ours.",
    ),
    RuleName.MC_NEVER_RETRY_ADVICE_CODE: (
        REGULATORY,
        "Mastercard Transaction Processing Excellence — MAC 03 (fraudulent), 21 (lost/stolen).",
    ),
    RuleName.NETWORK_REATTEMPT_CAP: (
        REGULATORY,
        "Visa Excessive Reattempts Rule (15 per card / 30 days); "
        "Mastercard TPE (10 authorisations per PAN / 24 hours).",
    ),
    RuleName.EMANDATE_PREDEBIT_NOTICE: (
        REGULATORY,
        "RBI/DPSS/2026-27/396, 21 April 2026 — pre-transaction notification at least "
        "24h before every e-mandate debit. Buffered to 26h, matching Stripe's India "
        "recurring flow.",
    ),
    RuleName.HARD_RISK_NO_CONTACT: (
        BACKSTOP,
        "ours — no card network forbids emailing a cardholder whose payment was declined "
        "for suspected fraud. This is our judgment that a risk decline is a risk "
        "operation rather than a dunning one, and it is labelled as ours accordingly.",
    ),
    RuleName.QUIET_HOURS: (
        REGULATORY,
        "TRAI TCCCPR 2018 as amended 12 Feb 2025 — promotional commercial communication "
        "restricted to 09:00–21:00. The 2025 amendment moved 'service explicit' templates "
        "into the promotional category, so a dunning contact is treated as promotional "
        "unless it is a bare RBI-mandated debit notice.",
    ),
    RuleName.MAX_LIFETIME_ATTEMPTS: (
        BACKSTOP,
        "ours — bounds scorer error. The EV model should decline a marginal retry long "
        "before this binds; when it does bind, that is a finding about the scorer.",
    ),
    RuleName.MIN_ATTEMPT_INTERVAL: (
        BACKSTOP,
        "ours — prevents attempt bunching against a single issuer.",
    ),
    RuleName.CONTACT_FREQUENCY_CAP: (
        BACKSTOP,
        "ours — bounds contact fatigue, which is the least-sourced term in the EV model.",
    ),
    RuleName.ISSUER_CIRCUIT_BREAKER: (
        BACKSTOP,
        "ours — the transplanted admission-control finding: a crowd of failing retries "
        "degrades the success rate of retries that would otherwise have worked.",
    ),
}

# Every rule must be classified. A rule that fires without a declared basis makes
# the veto-rate metric silently wrong, which is worse than it being absent.
assert set(RULE_BASIS) == set(RuleName), (
    f"unclassified rules: {set(RuleName) - set(RULE_BASIS)}"
)


def basis_of(rule_name: str) -> str:
    """Basis for a rule name as it appears in `PolicyDecision.rules_fired`."""
    return RULE_BASIS[RuleName(rule_name)][0]


def citation_of(rule_name: str) -> str:
    return RULE_BASIS[RuleName(rule_name)][1]


def is_regulatory(rule_name: str) -> bool:
    return basis_of(rule_name) == REGULATORY
