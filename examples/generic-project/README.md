# Generic Project Example (`examples/generic-project`)

This directory contains a complete, standalone example project showcasing all 4 knowledge domains served by `mcp-context-toolkit`:

1. **Rules** (`.context/rules/`): File-scoped standards and policies.
2. **Memory** (`.context/memory/`): Frecency-ranked lessons and reference notes.
3. **Work** (`.context/work/`): Goals, steps, and plans with reuse tracking.
4. **Docs** (`docs/`): Prose documentation indexed by section.

## Quick Start

Test this sample project using `context-toolkit-query`:

```bash
cd examples/generic-project
context-toolkit-query doctor
context-toolkit-query validate --all
```
