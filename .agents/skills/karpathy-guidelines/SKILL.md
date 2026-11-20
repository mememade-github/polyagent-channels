---
name: karpathy-guidelines
description: Behavioral guidelines to reduce common LLM coding mistakes. Use when writing, reviewing, or refactoring code to avoid overcomplication, make surgical changes, surface assumptions, and define verifiable success criteria.
license: MIT
---

# Karpathy Guidelines

> Skill mirror of `.claude/rules/behavioral-core.md`. Derived from [Andrej Karpathy's observations](https://x.com/karpathy/status/2015883857489522876); source package [forrestchang/andrej-karpathy-skills](https://github.com/forrestchang/andrej-karpathy-skills) (MIT). `scripts/meta/karpathy-consistency-check.sh` automates the comparison. Rules 1–4 and the closing self-test stay synchronized; only frontmatter, title, attribution, and source-link text may differ.

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

## 1. Think Before Coding

State assumptions that affect the approach. If different readings would materially change the result, present them or ask. Say when a simpler approach is sufficient.

## 2. Simplicity First

Implement only requested behavior. Do not add speculative features, abstractions, configuration, or handling for impossible cases.

## 3. Surgical Changes

Read before editing, match existing style, and change only lines traceable to the request. Leave unrelated cleanup alone; remove only orphans your change creates.

## 4. Goal-Driven Execution

Turn the request into verifiable outcomes and run the relevant checks before claiming completion. Never claim tests, builds, or behavior passed without tool evidence.

---

**These guidelines are working if:** fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, and clarifying questions come before implementation rather than after mistakes.
