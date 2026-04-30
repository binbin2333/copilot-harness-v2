# copilot-harness-v2

Copilot-native memory and evidence gate layer for repository-local workflows.

This MVP provides:

- a Python CLI (`harness-v2`);
- repo-local workflow state under `.github/harness-v2/state/`;
- evidence templates and artifact registration;
- implementation and verification gates;
- JSONL failure/correction memory;
- installer-generated Copilot hook adapters and skills.

## Quick start

```bash
uv run harness-v2 install /path/to/repo
uv run harness-v2 --repo /path/to/repo start feature my-feature
uv run harness-v2 --repo /path/to/repo evidence create context-map
uv run harness-v2 --repo /path/to/repo evidence add context-map /path/to/repo/.github/harness-v2/state/workflows/feature-my-feature/artifacts/context-map.md
uv run harness-v2 --repo /path/to/repo gate implementation
```

