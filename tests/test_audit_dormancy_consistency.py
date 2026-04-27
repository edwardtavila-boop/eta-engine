"""
CI gate for the dormancy-consistency audit.

Runs ``scripts/_audit_dormancy_consistency.py`` against the live
codebase and refuses to pass if any operator-facing doc / script
references Tradovate without dormancy context.

Why this exists
---------------
The broker dormancy mandate (2026-04-24, see
``memory/broker_dormancy_mandate.md``) puts Tradovate in
DORMANT_BROKERS until funding clears. Several operator-facing docs
were written when Tradovate was the assumed Tier-A venue and
referenced it in operationally load-bearing places. The runbook
rewrite + audit script in this branch fixed the existing drift; this
test prevents regression: any new doc / script that adds a
Tradovate reference must include dormancy context (DORMANT /
Appendix A / dormancy_mandate / ...) within ±N lines, OR the file
must be on the explicit allowlist.

The test invokes the auditor's ``audit()`` function directly (not
the CLI) so a regression points at this test, not at a subprocess.
"""

from __future__ import annotations

from eta_engine.scripts._audit_dormancy_consistency import audit


def test_no_stale_tradovate_refs_in_operator_facing_docs_or_scripts() -> None:
    """No Tradovate reference may exist without dormancy context."""
    findings = audit()
    if findings:
        rendered = "\n".join(f"  {rel}:{line_no}: {text}" for rel, line_no, text in findings)
        msg = (
            f"\n{len(findings)} stale Tradovate reference(s) found. Each "
            f"must mention DORMANT / Appendix A / dormancy_mandate within "
            f"the per-file-type context window, or the file must be on "
            f"the explicit allowlist in "
            f"scripts/_audit_dormancy_consistency.py:\n{rendered}"
        )
        raise AssertionError(msg)
