# Round 4: the stranger's first hour

Scope: what someone gets from `pip install` and the README alone. Branch
`expert-2`. Nothing was published, tagged or pushed.

## How the review was done (honest)

No independent sub-agents were available in this session. One author played
every role below and ran real commands for each; the commands are the proof,
not the role names. Treat it as one reviewer wearing several hats.

## Specialists and findings

| Role | Finding | Command that proved it |
|---|---|---|
| Release engineer | `python -m build` FAILED on a clean tree: setuptools saw `tests/`, `bench/` and `sessionsteward/` as three top-level packages. The CI "package" job could not have passed. Fixed with `packages = ["sessionsteward"]`. | `python -m build --outdir dist .` (error: "Multiple top-level packages discovered") |
| Release engineer | Metadata had no author and only a Homepage link. Added author, Source, Issues, Changelog. | `unzip -p dist/*.whl '*/METADATA'` |
| Release engineer | Wheel now holds 10 files: the package, LICENSE, METADATA. No tests, bench, pyc. `twine check --strict` passes on wheel and sdist. | `unzip -l dist/*.whl`; `twine check --strict dist/*` |
| First-time user | Fresh venv, non-editable wheel, directory with spaces in its name, README quick start (init, install-hooks --write, snap, undo, status, lost, diet --lint) all work and print what the README says. `--version` and `--help` fine. | run from `/tmp/.../qs dir` |
| First-time user | `undo --steps 0` / `-3` silently undid a step; `--root /typo` was accepted. Both now a plain error, exit 2. | `sessionsteward undo --steps 0` |
| DX writer | `diff nosuch` showed raw git text. Now "no such snapshot". | `sessionsteward diff nosuch` |
| CI integrator | `log` and `status` had no `--json` (only `scan`, `lost`, `diet` did). Added both. | `sessionsteward status --json` |
| Supply chain | Zero runtime dependencies (clean venv `pip list` shows only pip + the package). Wheel carries no secrets or machine paths. | `pip list` in the clean venv |
| Hostile-input fuzzer | Invalid UTF-8 CLAUDE.md, 400k-line file, escape and bidi characters in a snapshot message, a missing git, a missing HOME, `--root` on a missing folder: no tracebacks. Diet --lint on a huge file 0.4 s; snap 0.7 s. | see ROUND4 commands in the session log |
| Concurrency | Six parallel `snap` runs all exit 0 and agree on one snapshot. | background loop of `sessionsteward snap` |
| Windows (static) | Reviewed lock (msvcrt), path mapping, `os.replace`, encoding (`_harden_streams`), CRLF, no `shell=True`, no `signal`. Earlier rounds already cover these with emulated tests. One real gap: the watch config was written non-atomically; fixed. NOT RUN on Windows. | `grep` of `shell=True`, `signal`, `os.replace` |
| Performance | Benchmark unchanged in behaviour; speeds within noise (see below). | `python bench/run.py` |
| Competitor's maintainer | Comparison table in README is sourced; not re-fetched this round (no web fetch was approved), so marked unverified for today. | none |

## Scored ideas (value / differentiation / effort / risk, 1-5; 5 = best value, 5 = most effort or risk)

| # | Idea | Value | Diff. | Effort | Risk | Status |
|---|---|---|---|---|---|---|
| 1 | Fix the failing build (package list) | 5 | 1 | 1 | 1 | DONE |
| 2 | Author, URLs, MANIFEST for sdist tests | 4 | 1 | 1 | 1 | DONE |
| 3 | Reject `--steps 0/-n`, `--limit 0` | 3 | 1 | 1 | 1 | DONE |
| 4 | Reject missing `--root` folder | 3 | 1 | 1 | 1 | DONE |
| 5 | Plain error for unknown sha in `diff` | 3 | 1 | 1 | 1 | DONE |
| 6 | `log --json` | 4 | 3 | 2 | 1 | DONE |
| 7 | `status --json` | 4 | 3 | 2 | 1 | DONE |
| 8 | Atomic config write | 3 | 1 | 1 | 1 | DONE |
| 9 | Lint step (pyflakes rules) in CI | 2 | 1 | 1 | 1 | DONE |
| 10 | Packaging tests (metadata, version, changelog) | 3 | 1 | 1 | 1 | DONE |
| 11 | README says PyPI is older than the repo | 4 | 1 | 1 | 1 | DONE |
| 12 | PUBLISH-CHECKLIST.md for the owner | 4 | 1 | 1 | 1 | DONE |
| 13 | `diff --json` | 2 | 2 | 2 | 1 | NOT-DONE |
| 14 | `py.typed` + type checking | 2 | 1 | 4 | 2 | NOT-DONE (no mypy here; claiming "typed" would be unverified) |
| 15 | Run tests on real Windows | 5 | 1 | 3 | 1 | NOT-DONE (no Windows machine; CI matrix does it on the next push) |
| 16 | Test on Python 3.10 | 3 | 1 | 2 | 1 | NOT-DONE (3.11, 3.12, 3.13 present only; 3.10 grammar parsed statically) |
| 17 | `pipx` install line in README | 2 | 1 | 1 | 2 | NOT-DONE (not run here) |
| 18 | Trusted-publishing release workflow | 4 | 1 | 3 | 3 | NOT-DONE (touches publishing; owner decision) |
| 19 | Shell completion | 2 | 2 | 3 | 1 | NOT-DONE |
| 20 | `doctor` command (git version, hooks, repo health) | 3 | 3 | 3 | 1 | NOT-DONE; `status` covers most of it |
| 21 | Read-only home test as non-root | 3 | 1 | 2 | 1 | NOT-DONE (sandbox runs as root; permission bits do not bind) |
| 22 | Re-verify competitor table today | 3 | 2 | 1 | 1 | NOT-DONE (no web fetch) |

## Numbers

- Before round 4: 399 passed, 21 skipped (about 5.5 minutes).
- After: 413 passed, 21 skipped (326 s). New file `tests/test_round4.py`
  (14 tests). Python 3.11 also ran the core and new tests green.
- Benchmark: detector recall and precision unchanged; speeds in
  `bench/RESULTS.md` (diet --lint 3.59 s before, 4.60 s now while the test
  suite ran alongside: load noise, no code change in that path).

## What could not be tested

- Windows and macOS (static review only).
- Python 3.10 (only parsed). 3.11 ran the new and core tests; 3.12 and 3.13
  were available.
- Read-only home folder (running as root).
- The registry upload itself, `pip install sessionsteward` from PyPI after
  release, competitors' current state.
