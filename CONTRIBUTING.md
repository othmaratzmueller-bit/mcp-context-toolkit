# Contributing to mcp-context-toolkit

Thanks for your interest! This project is maintained as part of a larger private
codebase and published as a curated mirror — external contributions are welcome
via issues and pull requests on GitHub.

## Ground rules

- **Keep it generic.** The toolkit must stay free of any project-specific
  knowledge. Rules/memories in tests use neutral fixture paths (`web/`, `src/`);
  never reference a concrete product, company, or internal system.
- **Plain markdown + YAML frontmatter is the contract.** Any change to the store
  format needs a documented migration story — existing stores must keep loading.
- **No new runtime dependencies without discussion.** The engine intentionally
  runs on a small footprint (see `pyproject.toml`); every new dependency must be
  permissively licensed (MIT/Apache-2.0/BSD/ISC/PSF).
- **A new feature must work without TALOS.** This is the acceptance test for
  any PR, not just a style preference (see [Project boundary](README.md#project-boundary)):
  a fresh clone, a fresh venv, and `examples/generic-project/` must be enough
  to exercise it end to end. If it genuinely needs host-specific behavior
  (a symbol resolver, a project convention), that behavior belongs behind an
  optional host adapter — not inline in the core, and not gated on a TALOS
  checkout being present.

## Development setup

```bash
cd <repo>
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest            # full suite must be green before any PR
```

## Pull requests

1. One topic per PR — small, reviewable diffs.
2. Add or extend a test for every behaviour change (`tests/` mirrors the
   module layout; `test_engine.py` covers loading/matching, `test_memory.py`
   recall/frecency, `test_mcp_server.py` the MCP surface).
3. Update `CHANGELOG.md` (newest on top) and, where user-visible, `README.md`.
4. CI (GitHub Actions) must pass — it runs the same `pytest` suite.

## Reporting bugs

Open a GitHub issue with: toolkit version, Python version, a minimal store
layout that reproduces the problem (a few markdown files inline in the issue
are perfect), what you expected, and what happened instead.

## Security issues

Please do **not** open public issues for vulnerabilities — see
[SECURITY.md](SECURITY.md).

## Release checklist

Before tagging a release, in order:

1. **Fresh clone, fresh venv.** Not the working tree you developed in —
   a `git clone` into an empty directory, a new `.venv`, then the SAME
   install line CI uses (`.github/workflows/ci.yml`), not just the
   `dev` extra: `pip install -e ".[dev]" "ruff==0.16.0" build`. The
   `dev` extra alone omits `ruff` and `build` — a fresh clone with only
   `pip install -e ".[dev]"` cannot run step 2 or 3 below. `ruff` is
   version-pinned on purpose (a linter defines by version what counts
   as an error — see the comment in `ci.yml` next to the pin), so use
   the exact version CI pins, not whatever is newest. This is the
   actual acceptance test: a real user starts from a clone, not from a
   repo that already has stray state in it.
2. `ruff check src tests examples` and `pytest -q` — both clean, zero
   TALOS-conditioned skips (a skip that only fires inside a TALOS
   checkout is fine; one that fires in a plain clone means something
   host-specific leaked into the default path).
3. **Build both distribution formats** and confirm they install cleanly
   from that fresh venv: `python -m build` (produces a wheel and an
   sdist), then `pip install dist/*.whl` in a second, throwaway venv.
4. **Walk the README's own examples**, in the fresh clone, without
   TALOS context: register the server with an MCP client, run one
   `recall` call, run `context-toolkit-query --doctor`. If a step needs
   knowledge that isn't in the README, the README is the bug, not the
   step.
5. **Cross-check `pyproject.toml`'s `version`, `CHANGELOG.md`'s newest
   entry, and the git tag you're about to create** — all three name the
   same version, and the CHANGELOG entry is dated with the actual
   release date, not the date it was drafted.
6. Only after 1–5 pass: create the tag and push.
