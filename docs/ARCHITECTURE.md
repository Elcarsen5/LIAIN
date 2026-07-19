# Liain Architecture

Liain separates the **body** (this package) from the **soul** (your config).

## Layers

| Layer | Module | Role |
|---|---|---|
| Config | `liain.config` | loads `persona.yaml` / `contacts.yaml` / `llm.yaml` from `LIAIN_CONFIG_DIR` |
| Persona | `liain.persona` | renders a system prompt from config — the soul |
| LLM | `liain.llm` | role router → backend (`claude_cli` / `ollama` / `api`) by profile |
| Channels | `liain.channels` | messaging abstraction; `Channel` ABC + Telegram (default), iMessage (optional macOS) |

## LLM roles

Calls are split by **role** so one profile can mix backends:

- `chat` — conversational replies
- `reasoning` — diary / reflection / analysis
- `vision` — image description (graceful degradation: `none` → `""`)
- `classify` — always keyword (LLM-free)
- `embed` — always local SBERT

A `profile:` in `llm.yaml` picks a preset; `backends:` overrides per role.

## Channels

A channel implements `Channel` (send_role / send_target / poll). `route(role, text)`
tries available channels in priority order (iMessage first on macOS, then Telegram).
Add a channel by implementing the ABC — no core changes.

## Bring your own soul

The same body runs any persona. Lian is one instance (its `persona.yaml`
defines Lian/Lain). Your `persona.yaml` defines yours. No personal data lives
in this repo — it all comes from gitignored config + `.env`.
