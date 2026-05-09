# copilot-harness-v2

`copilot-harness-v2` adds a lightweight workflow guardrail for Copilot CLI tasks inside a repository.

It provides:

- a CLI: `harness-v2`
- repo-local workflow state in `.github/harness-v2/state/`
- evidence registration for planning, verification, and review artifacts
- implementation and completion gates
- workflow memory stored as JSONL files

## Install

```bash
uv sync
```

## Basic usage

```bash
uv run harness-v2 install .
uv run harness-v2 --repo . start feature auth-flow
uv run harness-v2 --repo . status
uv run harness-v2 --repo . gate implementation --path harness_v2/cli.py
uv run harness-v2 --repo . gate completion
```
