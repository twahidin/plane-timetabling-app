"""The smart workbook reader (spec docs/superpowers/specs/2026-10-08-smart-workbook-reader-design.md):
any workbook read through reading recipes — JSON the app runs deterministically — into a plan patch.

- `render`: each sheet as compact text for the recipe writer (§3.1).
- `recipes`: the recipe language — validation and `run` (in a child process, under a time limit);
  `recipes.to_patch` turns records into a plan patch (§2).
- `language`, `patterns`, `grid`, `execute`, `patch`: its vocabulary, safe patterns, the sheet as a
  recipe sees it, the in-process execution, and records -> patch.
- `signature`: a workbook's layout signature, so a confirmed reader is remembered (§5).
- `writer`: the AI step (§3) — the model writes recipes, the app validates, runs and repairs them
  once — and `read`, a workbook as a reading (saved reader first).
- `readers`: saved readers, global kv `readers` keyed by signature (§5).
"""
