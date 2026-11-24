# Domain Documentation

This repository uses a single-context domain-document layout.

## Before exploring

Read these sources when they exist:

- `GLOSSARY.md` at the repository root for the project glossary and domain model
- Relevant ADRs under `docs/adr/`
- The canonical specification under `docs/`

If `GLOSSARY.md` or `docs/adr/` does not exist, proceed without mentioning its absence or suggesting that it be created. The `/domain-modeling` skill creates these records when a term or architectural decision is actually settled.

## Layout

```text
/
├── GLOSSARY.md
├── docs/
│   ├── adr/
│   └── agents/
└── src/
```

## Vocabulary

Use terminology defined in `GLOSSARY.md` consistently in issue titles, tests, implementation, and documentation. If a required concept is missing, determine whether the proposed term is unnecessary or whether the glossary needs an explicit addition.

## Architectural decisions

Read ADRs affecting the area before making changes. If proposed work contradicts an ADR, identify the conflict explicitly instead of silently overriding the recorded decision.
