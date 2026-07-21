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
