# Task-graph file format (M39 slice 1, #300)

A declarative task graph is one YAML (`.yaml`/`.yml`) or JSON (`.json`)
file describing a whole batch: cards, dependency edges, backends, and
acceptance commands. `openswarm graph validate <file>` checks the file
before anything is dispatched; slice 2 wires it into dispatch
(`run --graph`), where the semantics below match the existing M4 batch
cards: `depends_on` lists, topo-order dispatch, a failed card blocks its
dependents.

## Example

```yaml
version: 1
defaults:
  backend: codex
  verify: pytest -q
cards:
  - id: setup
    task: Set up the fixture repo
  - id: impl
    backend: ocgo
    depends_on: [setup]
    task: |
      Implement the feature.
      Keep it small.
  - id: check
    depends_on:
      - impl
    verify: pytest tests/test_x.py -q
    task: Verify the feature
```

The same structure as JSON: `{"version": 1, "defaults": {...},
"cards": [...]}`. A runnable copy lives in
[`examples/task-graph.example.yaml`](../examples/task-graph.example.yaml)
(and the same graph as JSON in
[`examples/task-graph.example.json`](../examples/task-graph.example.json)).

## Fields

| Field | Required | Meaning |
| --- | --- | --- |
| `version` | yes | Must be the integer `1`. |
| `defaults` | no | Mapping with optional `backend` and `verify`. |
| `defaults.backend` | no | Backend for cards that do not name one. |
| `defaults.verify` | no | Acceptance command for cards that do not name one. |
| `cards` | yes | Non-empty list of cards. |
| `cards[].id` | yes | Unique card id: starts with a letter or digit, then letters, digits, `.`, `_`, `-` (max 80 chars). |
| `cards[].backend` | no | Backend for this card; overrides `defaults.backend`. Must be a known backend id (`codex`, `ocgo`, `deepseek`, `cursor`, `agy`, `pi`, `hermes`, `copilot`; `gemini` is accepted as the alias for `agy` and is canonicalized). Custom backends from `--backends-file` also count as known. |
| `cards[].verify` | no | Acceptance command for this card; overrides `defaults.verify`. |
| `cards[].depends_on` | no | List of card ids this card waits for (inline `[a, b]` or block `- a` form). Defaults to `[]`. Duplicates are removed, order preserved. |
| `cards[].task` | no | Free-text task description. A literal `\|` block keeps line breaks; a trailing newline is kept for `\|`, stripped for `\|-`. |

Unknown fields at any level are hard errors, never silently ignored.

## Defaults rules

- Effective backend = card `backend`, else `defaults.backend`, else
  *none* — a card without a backend anywhere is valid; the backend is
  resolved at dispatch time exactly like a batch without `--backend`.
- Effective acceptance command = card `verify`, else `defaults.verify`.
  **Every card must end up with an acceptance command** — a card with
  neither is rejected. There is no "no verification" fallback: a missing
  `verify` must never silently mean "plain verify" or "skip verify".

## Dispatch order

Cards run in topo order: every dependency precedes its dependents.
Among cards that are all ready, declaration order in the file wins (the
M4 batch equivalent is filename order). A failed card blocks its
dependents; a blocked card blocks transitively — same as batch
(`batch_dependency_failure`).

## Validation errors

`openswarm graph validate <file>` exits 0 for a valid graph and prints
`valid: <file>: N cards, dispatch order: a -> b -> ...` (or the
normalized graph with `--json`). Every problem below exits 2 with an
actionable message, before any dispatch and without writing any state:

| Problem | Message names |
| --- | --- |
| Dependency cycle | the cycle, e.g. `a -> b -> a` |
| Unknown card reference | the card and the missing id |
| Missing acceptance command | the card |
| Duplicate card id | the id |
| Unknown backend name | the name and the known ids |
| Self-dependency | the card |
| Bad structure (bad version, unknown field, empty `cards`, `depends_on` not a list, empty `verify`/`task`, tabs, duplicate keys, malformed YAML/JSON, unsupported suffix) | the field and line where applicable |

All structural problems found are reported at once; the cycle check runs
only once the references are clean.

## YAML subset

The project is stdlib-only, so YAML is the repository's small subset
(same family as the lesson parser): mappings, block and inline lists,
quoted and plain scalars, `#` comments, literal `|`/`|-` blocks.
Plain scalars may contain `:` (e.g. `task: Fix bug: handle None`).
Unsupported: tabs, flow mappings, anchors/aliases, multi-document files,
`|+` (treated like `|` — one trailing newline). Blank lines inside a
`|...` block are preserved; a `|` block with no indented lines is empty.
JSON files accept the full JSON syntax for the same structure.
