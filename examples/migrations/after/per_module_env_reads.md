---
key: per_module_env_reads
title: 'Per-module environment reads'
kind: decision
status: dropped
theme: migrations
opened: 2026-01-01
closed: 2026-01-01
evidence:
  commits: []
  files:
---

_Migrated from legacy status: rejected._

Rejected alternative to layered_config: let each module read its own
os.environ keys directly. Superseded before it shipped — see
layered_config for the decision actually made.
