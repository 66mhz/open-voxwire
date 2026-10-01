# Voxwire — agent guide

**Voxwire** is a throat-mic-native, local-first voice agent: speak (stealth throat
mic *or* regular mic) → on-device STT → a tool-capable agent runs work across the
systems you connect. This repo is **public-bound** (currently **private**).
Architecture: read `docs/DESIGN.md` first.

The differentiator is the **throat mic**: it's band-limited (loses consonants), so
Voxwire is built to handle that (dual-mic fusion, enhancement, faithful fixup) —
not just another Whisper wrapper.

---

## 🔴 Inviolable rules — a violation here is expensive and silent

1. **No private / employer content, EVER.** This repo is public. Never commit
   employer or project names, internal host names or IP ranges, or
   secrets/keys/tokens. **Before every commit** run and require a clean result:
   ```
   ./scripts/leakcheck.sh
   ```
   It scans for secret-shaped patterns (private IP ranges, API-key formats) and,
   if `scripts/private-denylist.txt` is present (git-ignored, maintainer-local),
   the specific private terms listed there. Keep that denylist current — it lists
   the names/hosts to catch and is never committed, so the guard itself stays
   leak-free.
2. **Private-first.** Do NOT `git push`, change repo visibility, or publish anything
   without explicit human approval. Local commits are fine. (Tooling denies `push`.)
   Before any visibility change, `./scripts/leakcheck.sh --history` (with the
   denylist present) and the manual *History audit* workflow must both be clean —
   CI scans only the working tree, and removed terms still live in history.
3. **Security is structural, not prompt-based.** Confirmation state is owned by the
   **executor** — the agent must never be able to self-approve. Effective permission
   = `persona.tool_scope ∩ executor.host_tier`, computed in ONE function,
   deny-by-default. Never weaken a gate to make a feature work.
4. **LLM fixup stays FAITHFUL.** Fix mishearings only; never invent or expand; skip
   ≤2-word inputs; keep it OFF by default. Do NOT reintroduce "reconstruct the
   intended command" prompting — it hallucinated ("continue" → "okay, start
   execution").
5. **Model IDs live in ONE tier config table** (`fast|heavy|cloud`, `voxwire/tiers.py`)
   and nowhere else; STT model IDs live in `voxwire/stt/models.py`.
   `tests/test_model_registry.py` fails on a model ID anywhere else.
   **Integrations are plugins** (`voxwire/integrations/`), never hardcoded into the
   gateway.
6. **Never commit** `.venv/`, `.env`, `recordings/`, `*.wav` (they're gitignored —
   keep it that way).

## Layout

| Path | What |
|---|---|
| `voxwire/server.py` | FastAPI (:8123): STT, dictation, mic, executor-seed + `/api/command` gateway endpoints |
| `voxwire/tiers.py` | **the ONE LLM model tier table** (`fast|heavy|cloud`); code asks for a tier, never a model |
| `voxwire/gateway.py` | generic transcript → integration router (`dispatch` / `load_integrations`); no service hardcoded; never self-approves a gated action |
| `voxwire/menubar.py` | native macOS menubar app (rumps + pyobjc); runs the server in-process; holds the native confirm primitive |
| `voxwire/index.html` | web config UI |
| `voxwire/integrations/base.py` | **the plugin interface** — `Integration` / `register` / `route`. Flexibility lives here |
| `voxwire/integrations/echo.py` | the reference plugin (self-registers); copy it to build your own |
| `voxwire/run.sh` | `./run.sh app` (menubar) · `./run.sh` (headless + web config) |
| `scripts/install.sh` | single-click setup (uv env + deps) |
| `docs/DESIGN.md` | architecture, security model, roadmap |

## Run / dev

- Setup: `./scripts/install.sh`
- Menubar app (recommended): `cd voxwire && ./run.sh app`
- Headless + web config: `cd voxwire && ./run.sh` → http://127.0.0.1:8123
- Python **3.12** venv at `voxwire/.venv` (MLX needs Python ≤3.13).
- Dev tooling: `uv pip install --python voxwire/.venv/bin/python -e ".[dev]"` (pytest + ruff).
- Tests: `voxwire/.venv/bin/python -m pytest` (from repo root). Lint: `voxwire/.venv/bin/ruff check .`
- Deps + extras live in `pyproject.toml` (`[mlx]`, `[faster-whisper]`, `[tray]`, `[macos]`, `[dev]`);
  CI (ruff + pytest + gitleaks) runs on PRs — see `.github/workflows/ci.yml`. Keep both green.

## Where the work is

The roadmap is in `docs/DESIGN.md` (*Status / roadmap*); bugs and proposals go
in GitHub issues.

## Definition of done (every change)

- Real tests for the behavior; run them; report actual results (never claim green if red).
- Leak check clean (rule 1).
- Matches existing style; no model IDs outside the tier table; integrations stay plugins.
- Update `docs/DESIGN.md` if you changed the architecture.

## Commits

End commit messages with:
`Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`
