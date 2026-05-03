# Vera Bot — magicpin AI Challenge

## Approach

Single-prompt LLM composer (Claude Sonnet) with:
- **Trigger-kind routing**: each trigger kind generates a context-specific prompt variant
- **Auto-reply detection**: regex patterns catch WhatsApp Business canned replies; 3-tier escalation (nudge → wait 4h → end)
- **Intent-transition handling**: explicit "let's do it / join / confirm" immediately skips qualifying and moves to action
- **Anti-repetition guard**: last vera body checked before each reply
- **Adaptive context versioning**: idempotent `/v1/context` with higher-version replace
- **Graceful exits**: hard opt-out, 3× auto-reply, and hostile messages all terminate cleanly

## Architecture

```
FastAPI server (bot.py)
├── /v1/context   — stores category/merchant/customer/trigger payloads in memory
├── /v1/tick      — iterates available_triggers, calls LLM for each, returns actions[]
├── /v1/reply     — detects auto-reply/intent, routes to LLM reply composer
├── /v1/healthz   — liveness + context counts
└── /v1/metadata  — team info
```

## What tradeoffs were made

- In-memory storage (no Redis/DB) — fits the 60-min test window; restarts would lose state
- Single LLM call per compose (no retrieval/RAG) — fast enough for 30s budget; digest items sent inline in prompt
- No async background composition — `/v1/tick` blocks for all actions synchronously; capped at 10 per tick
- Temperature=0 equivalent via model default (Sonnet is near-deterministic for structured JSON)

## What additional context would have helped most

1. **Merchant's WhatsApp Business number + session state** — knowing whether 24h session is open would change template vs free-form decision
2. **Historical open rates by trigger kind** — would let us suppress low-CTR trigger types
3. **Real competitor names + distances** for `competitor_opened` triggers
4. **Patient/customer names pre-fetched** for recall triggers (currently derived from customer context)

## Running locally

```bash
pip install -r requirements.txt
uvicorn bot:app --host 0.0.0.0 --port 8080
# In another terminal:
export BOT_URL=http://localhost:8080
python judge_simulator.py  # (from challenge zip)
```

## Generating submission.jsonl

```bash
python3 generate_submission.py
```
