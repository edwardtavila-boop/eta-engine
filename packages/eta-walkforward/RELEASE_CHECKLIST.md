# eta-walkforward — release checklist

Run through this when the operator says "open-source it". Until then
the package stays inside the private monorepo.

## Pre-release verification

- [ ] `python -m pytest tests/` — all 16 tests pass.
- [ ] `python -m ruff check eta_walkforward/ tests/` — clean.
- [ ] `python -m build` — wheel + sdist build clean.
- [ ] `python -m twine check dist/*` — README renders on PyPI.
- [ ] Bump `pyproject.toml` version if changed since last tag.
- [ ] Tag the release: `git tag v0.1.0 && git push --tags`.

## Public-repo bootstrap

- [ ] Create `github.com/edwardtavila-boop/eta-walkforward` (public).
- [ ] Copy `packages/eta-walkforward/` to the new repo (no monorepo
      noise, no eta_engine internals).
- [ ] Confirm the README's Homepage / Source / Issues URLs in
      `pyproject.toml` are correct.
- [ ] Verify LICENSE is MIT and the copyright line names
      Evolutionary Trading Algo LLC.
- [ ] Run `pip install <repo-url>` from a clean venv to confirm the
      package installs without the rest of the monorepo present.

## PyPI publish

- [ ] Register PyPI account / 2FA.
- [ ] `python -m build` produces clean wheel + sdist.
- [ ] `python -m twine upload --repository testpypi dist/*` first.
- [ ] Test install from TestPyPI: `pip install -i https://test.pypi.org/simple/ eta-walkforward`.
- [ ] If clean, `python -m twine upload dist/*` for production.
- [ ] Add the PyPI badge to the README on the next push.

## Marketing surface (optional, but cheap)

- [ ] Tweet / post the release with the headline:
      "Open-sourced our walk-forward gate. The same gate that
      promotes our 5 production strategies, with the same FP-noise
      guards we caught and fixed today."
- [ ] Add a brief mention to the public site's `/research` log
      with a link to the GitHub repo.
- [ ] Add to https://github.com/topics/walk-forward and
      `topics/quant`.

## What stays inside the monorepo (DO NOT publish)

- The strategy modules in `eta_engine/strategies/` — those are the edge.
- The strategy registry in `eta_engine/strategies/per_bot_registry.py`
  — promotion-gated configs are proprietary.
- The frozen `docs/strategy_baselines.json` — production baselines.
- Any data fetchers under `eta_engine/scripts/fetch_*.py` —
  exchange / IBKR specific.

## What's safe to publish (the contents of this package)

- The walk-forward gate (`evaluate_gate`, `WalkForwardConfig`,
  `WindowStats`, `WalkForwardResult`).
- The Sharpe / Sortino / DSR / drawdown math.
- The drift monitor (`assess_drift`, `BaselineSnapshot`,
  `DriftAssessment`).
- The `Trade` / `BacktestConfig` pydantic models — minimal interface
  contracts, no proprietary content.

That's it. The package was carefully scoped to be the
"methodology / measurement" half, not the "what-do-I-trade" half.

## Known gotchas

- The package depends on `pydantic>=2.5`. Don't loosen this — the
  forward-ref resolution in `models.py` requires v2 semantics.
- `compute_sharpe`'s 1e-3 dispersion threshold is a load-bearing
  constant. Don't relax it without re-running the regression test
  against the FP-noise reproducer.
- The strict gate's IS-positive requirement is also load-bearing.
  Same advice — relax it and you re-open the lucky-OOS-split trap.

## After publish

- Watch issues / PRs for the first 30 days. Most likely category:
  "my strategy doesn't pass and I think the gate is wrong." That's
  the gate working — engage with the methodology, not the verdict.
- Cut a 0.2.0 release whenever the gate semantics change. SemVer
  matters here because the gate is the contract.
