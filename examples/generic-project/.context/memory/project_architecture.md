---
name: project_architecture
description: Overview of the modular layered architecture and data flows
type: reference
tier: project
tags:
  - architecture
  - backend
---

# Project Architecture Overview

The system follows a modular 3-tier architecture:
- **API / Protocol Layer**: Serves stdio MCP tools and REST endpoints.
- **Engine Core**: Manages in-memory BM25F indexing and store reloaders.
- **Storage Layer**: Plain Markdown and YAML files stored directly in Git.
