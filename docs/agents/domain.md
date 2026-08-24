# Domain Documentation

This repository uses a single-context domain-document layout.

## Before exploring

Read these sources when they exist:

- `CONTEXT.md` for the project glossary and domain model
- Relevant ADRs under `docs/adr/`
- The canonical specification under `docs/`

If `CONTEXT.md` or `docs/adr/` does not exist, proceed without treating its absence as a problem. Create these records only when terminology or architectural decisions need durable documentation.

## Layout

```text
/
├── CONTEXT.md
├── docs/
│   ├── adr/
│   └── agents/
└── src/
```

## Vocabulary

Use terminology defined in `CONTEXT.md` consistently in issue titles, tests, implementation, and documentation. If a required concept is missing, determine whether the proposed term is unnecessary or whether the glossary needs an explicit addition.

## Architectural decisions

Read ADRs affecting the area before making changes. If proposed work contradicts an ADR, identify the conflict explicitly instead of silently overriding the recorded decision.
