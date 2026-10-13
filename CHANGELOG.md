# Changelog

All notable changes to Liain are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
While on `0.x`, minor versions may include breaking changes.

## [Unreleased]

### Added
- **Query-time memory retrieval** (`liain/brain/retrieval.py`) — a BM25/FTS5 layer
  (`SqliteFts5Retriever` + `get_default_retriever` + `ingest_all` + Korean natural-language
  time-anchor parsing) that mirrors records/episodes/short-term into a SQLite FTS index and
  retrieves by relevance. Mirrors the reference persona's redesigned retrieval stack, adapted
  to the framework's record schema and `LIAIN_DATA_DIR` layout.
- **Upgraded `WorkingMemoryAssembler.retrieve_for_query`** — hybrid fusion (BM25 ⊕ dense SBERT
  via RRF, graceful BM25-only fallback when no embeddings), base-level activation for
  "person + emotion" queries gated by relevance (no spillover on unrelated turns), and
  subject labels on recalled lines to prevent misattribution. Persona/contact specifics are
  config-driven (`config.alias_to_entity` / `people` / `persona`), not hardcoded.
- **Memory-retrieval eval harness** (`liain/brain/eval.py`) — recall@K + negative-spillover
  measurement against a `golden_set.json`, plus a runnable abstract example under
  `examples/eval/` (fixtures + golden set; no real personal data).

Planned — porting remaining hardening/features proven in the reference persona (Lian):

### Added (planned)
- **Self-update (opt-in)** — a daemon that keeps itself current: checks for a newer release,
  applies it, health-checks, and rolls back on failure. Off by default. (Reference: Lian
  `code_apply.py` heartbeat-verify + rollback pattern.)
- **On-demand grounded search (optional)** — a persona that doesn't know something recent emits a
  search marker and answers from real results with dates/sources, instead of fabricating.
- **Daily briefings (optional)** — register a recurring topic brief by conversation.
- **Nightly letter (optional)** — a persona writes a calm daily letter to one recipient, mood-adaptive,
  with a first-letter intro and opt-out. (Reference: Lian `night_letter.py`.)

### Changed (planned)
- Per-person emotion gains a staleness guard — moods older than N days are no longer injected as
  "current".

## [0.2.0] — 2026-08-11

Porting hardening proven in the reference persona (Lian) into the framework. The headline is the
**two-stage generation pipeline**, which supersedes the single-prompt think/speak CoT as the root
fix for reasoning/strategy leaking into messages and mid-sentence truncation.

### Added
- **Two-stage generation (THINK → SPEAK)** — `generate.think_then_speak()`. A single prompt that
  carries both reasoning and message (inline `<생각>` CoT) mixes two purposes in one output, so
  leak/truncation remain *structurally*. Splitting generation in two — THINK derives strategy
  (private), SPEAK emits only the message while *referencing* the already-derived judgement — makes
  each stage single-purpose: leak ≈ 0, truncation sharply down. Response directives go into the
  SPEAK **system** prompt (not the user prompt) to avoid echo. Wires into `cli._on_message`.
- **Thought preservation via self-send** — the THINK result is sent by the agent *to its own
  account* (`config.self_handle(channel)`, e.g. a Note-to-Self thread) so the creator can inspect
  the private reasoning later. A code-added header names the room and the message it responds to.
  Self-send is `raw=True` (bypasses outgoing sanitize to preserve the original) and, being
  `is_from_me`, is never answered by the poller (no loop). Unset self-handle → graceful no-op.
- **Self-mood continuity** — a global persona mood (valence + label + note), separate from
  per-person emotion. Nightly self-reflection sets it (yesterday → today carryover), decaying
  toward baseline through the day; injected into every prompt.
- **Adaptive message coalescing** — burst messages get one reply, not one-per: completion-aware
  idle window + 30s cap + supersession (cancel an in-flight reply if a newer message arrives).
- **Confidential isolation** — `get_unified_context(speaking_with=…)` surfaces confidential-tagged
  records only to the entities they concern; person-tagged short-term items filtered likewise.
- **`Channel.send_to_self` + `send_target(raw=…)`** — the raw path bypasses `sanitize_outgoing`
  for internal self-logs only; external sends are never raw.

### Changed
- `cli._on_message` no longer appends the `<생각>` CoT `OUTPUT_FORMAT_NOTE` — the two-stage pipeline
  replaces it. `sanitize_outgoing` remains the transport chokepoint (final `<생각>`/monologue strip
  on every channel send), and `parse_think_speak` / `record_self_monologue` remain for thought
  capture. This is a generation-contract change, not an output filter.

## [0.1.0] — 2026

Initial extraction of the Lian persona daemon into a config-driven, installable framework.

### Added
- Config-driven persona: `persona.yaml` / `contacts.yaml` / `llm.yaml`, hot-reloadable.
- LLM role router (`chat` / `reasoning` / `vision` / `classify` / `embed`) with three profiles
  (`lite-subscription`, `full-subscription`, `full-local`); subscription Claude CLI + Ollama +
  API-Zero default. Keyword classify (LLM-free), local SBERT embeddings.
- Messaging channel abstraction — Telegram (default) and iMessage (macOS, optional), with routing
  + fallback.
- 5-layer brain: core / semantic / procedural / emotional / episodic memory; Mem0-style fact
  pipeline with temporal validity (ADD/UPDATE/DELETE/CONFLICT), decay-weighted recall, nightly
  consolidation, reflection engine (importance-sum trigger), and persona-voiced diary.
- CLI: `liain run | info | diary | consolidate | memory`.

[Unreleased]: https://github.com/Elcarsen5/LIAIN/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/Elcarsen5/LIAIN/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/Elcarsen5/LIAIN/releases/tag/v0.1.0
