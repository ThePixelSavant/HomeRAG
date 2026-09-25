# Documentation

Reference material for people and models working on this repo. Written to be
read in any order, but if you are new, read [architecture.md](architecture.md)
first and [status.md](status.md) second.

## What lives where

| Document | Answers |
|---|---|
| [cli.md](cli.md) | Every command — `rag` and `make` — with syntax, flags and why each behaves as it does |
| [architecture.md](architecture.md) | How the system is put together and how a document flows through it |
| [security-model.md](security-model.md) | What the vault protects against, what it does not, and why each gate exists |
| [status.md](status.md) | What is built, what is verified, what is known-broken, what is waiting on a human |
| [roadmap.md](roadmap.md) | The plan: what was just landed, what Phase 2 is, what comes after |
| [decisions.md](decisions.md) | Why each significant choice was made, and what was rejected |
| [development.md](development.md) | Running tests, adding an extractor or a domain, writing a migration |

Four other things matter and are **not** in this folder:

- [`../scripts/rag`](../scripts/rag) — the command wrapper. Symlink it into
  `~/.local/bin` and the whole control plane works from any directory.

- [`../README.md`](../README.md) — user-facing setup and day-to-day commands.
- [`../CLAUDE.md`](../CLAUDE.md) — the invariants and gotchas a model must not
  break while editing. Terse on purpose; the reasoning behind it is here.
- [`../sources.yaml`](../sources.yaml) — the source manifest, heavily commented.

## The one thing to know before changing anything

**Extract → scan → route → embed, in that order.**

Embedding an open-tier document writes a plaintext vector to Qdrant, and
[embedding inversion](decisions.md#adr-002-sensitive-data-never-reaches-qdrant)
reconstructs most of the source text from a vector. A sensitive document that
reaches `embed_passages` is effectively published, and there is no cleaning it
up afterwards — the vector is already on disk and any snapshot of it is too.

Everything else in this system is a convenience. That ordering is not.

## Conventions in these docs

- Anything stated as measured was measured against the real corpus on this
  machine, and the numbers are reproducible with the commands shown.
- "Phase 2" means designed but not built. Code for it does not exist; where a
  stub exists, it is a deliberate rejection (`sources.yaml` refuses `git` and
  `web` types rather than failing obscurely later).
- File references are clickable relative links.
