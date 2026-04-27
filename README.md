# ETA Engine

Internal engineering repository for **Evolutionary Trading Algo LLC** (Georgia, USA).

This is the multi-strategy execution engine — regime-aware setup gating across
MNQ futures and a parallel BTC engine in late-stage development. The repo is
public for engineering hygiene, not for solicitation.

## Status

- **Operating mode:** founder-LLC capital only
- **Regulatory posture:** CFTC Rule 4.14(a)(10) pre-CTA de minimis (<15 persons)
- **Public surface:** [evolutionarytradingalgo.com](https://evolutionarytradingalgo.com) (placeholder)
- **Beta access:** by individual invitation only — see app.evolutionarytradingalgo.com
- **Performance disclosures:** all numbers shown anywhere in this org are
  hypothetical or paper-sim and carry CFTC Rule 4.41 disclosure language

## What's in here

A regime-classified, multi-setup execution framework. The live setups are
documented per-module under their `[REAL]` / `[CONTRACT]` / `[STUB]` status tags.
Frozen baselines live under `eta_v3_framework/v1_locked/` with SHA-256 manifest
gating to prevent silent edits to reproducibility-critical code.

Audit-trail mirror to a Supabase `decision_journal` (append-only). Local JSONL
fallback if the mirror is unreachable.

## Internal codename note

The codebase is mid-migration from a prior internal codename (`apex_predator`)
to the current `eta_engine` package layout. Some legacy identifiers persist
inside historical files; a staged shim/re-export migration is in progress
(see `eta_full_rebrand_override` decision, 2026-04-27).

## Not for use

This repository contains research code that trades real capital under the
authority of the LLC. Forking and running it against a brokerage account is
neither supported nor advisable. There are no public releases.

## Contact

[contact@evolutionarytradingalgo.com](mailto:contact@evolutionarytradingalgo.com)
