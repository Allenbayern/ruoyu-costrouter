# Controller policy example

Use this as a starting policy for a Hermes controller session after installing `cost_router`.

## Routing map

- Route rough filtering, warning clustering, low-value preprocessing, table creation, checklist creation, and field extraction to `worker-dsflash`.
- Route bounded technical analysis, complex compression, and narrow source judgment to `worker-dspro`.
- Route high-quality Chinese drafts, rewrites, and user-facing prose drafts to `worker-gpt54`.
- Route hard execution slices that need top-tier reasoning to `worker-gpt55`.
- Keep final risk decisions, blocker calls, publication decisions, config-change decisions, and final user synthesis in the controller.

## Example call

```json
{
  "profile": "worker-dspro",
  "goal": "Review this failing integration point and return the smallest safe patch plan.",
  "context": "Include exact file paths, observed error output, constraints, and acceptance criteria.",
  "role": "leaf"
}
```

## Controller discipline

Workers return evidence, structure, drafts, or local judgment. The controller reviews worker output, keeps final authority, and decides whether another route is needed.
