"""Campaign automations: recurring sequences, behavioural nudges, cohort bulk.

All three share one runtime (:mod:`app.automations.runtime`) and differ only in
how they answer "who, and when". Each feature module contributes candidates;
the runtime owns consent, quiet hours, dedup, dispatch and the delivery ledger,
so a safety property fixed in one place holds for all three.
"""
