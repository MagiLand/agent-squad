# Developer resolution fixture

For REV-001, detect blank input with `name.strip()` but preserve the original
name when it is nonblank. This clarifies the captured compatibility requirement.
Add tests for empty, whitespace-only, and padded nonblank names.

This is a scripted test decision, not a human decision on a real project.
