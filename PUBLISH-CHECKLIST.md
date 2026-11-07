# Publish checklist (for Dan; nothing here has been run)

PyPI currently has 0.1.0 only (checked read-only on 2026-11-07). The repo is
0.3.0. Release **0.3.0**. It is a minor release; one small break: `undo
--steps 0`, a negative `--steps`/`--limit` and a missing `--root` folder are
now errors instead of being accepted (listed in CHANGELOG).

Check first
1. Push the branch and wait for the CI run (Linux, Windows, macOS; Python
   3.10-3.14). Windows has never been run by hand. Fix anything red.
2. Change the CHANGELOG heading `0.3.0 (unreleased)` to `0.3.0 - <date>`.
3. Make sure you own the name `sessionsteward` on PyPI (it has 0.1.0).
4. After release, change the README install line to `pip install
   sessionsteward` and remove the "PyPI still holds the old 0.1.0" note.

Commands (in the repo, in a clean virtual environment)
```
python -m pip install build twine
python -m build
python -m twine check --strict dist/*
git tag v0.3.0
python -m twine upload dist/*      # needs your PyPI token; test with --repository testpypi first
git push origin v0.3.0
```
After upload: in a new empty environment run `pip install sessionsteward`
and then `sessionsteward --version`; it must say 0.3.0.
