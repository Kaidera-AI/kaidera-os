# Cortex v2.1 Upgrade Notice for Kaidera AI Team

**From:** Rex (CPO, ASW Connect)
**Date:** 2026-04-11
**Updated:** 2026-04-11 — Cortex API layer now live
**Priority:** High — read before starting work

---

## What Happened

The shared Cortex database was corrupted on 2026-04-11 when an ASW Connect agent ran application Alembic migrations against cortex-pg. All agent memory was lost. The database has been rebuilt with a new schema (v2.1) that includes three protection layers.

## What Changed

### Schema v2.1
- Embedding dimensions: 2048 → 768 (all-mpnet-base-v2, best quality at $0.005/1M)
- New tables: cortex_meta (schema lock), cortex_audit_log, agent_diaries, cortex_entities, cortex_relationships
- New columns: importance (lessons), invalidated_at (decisions/lessons), project_hex (cortex_projects)
- Hybrid search: trigram + vector + rerank (Cohere) + graph

### Protection Layers
1. **Alembic guard:** env.py refuses to migrate against cortex databases
2. **Read-only user:** cortex_reader has SELECT-only — prevents accidental schema changes
3. **Schema lock:** cortex_meta table with db_type='cortex' — migration tools check this

### Embedding Model
- Benchmarked 18 models on OpenRouter
- Winner: `sentence-transformers/all-mpnet-base-v2` (768d, $0.005/1M, best discrimination)
- Runner-up dimensions don't matter: 4096d qwen3 ranked 9th, 37% worse than 768d mpnet
- cortex-embed and cortex-search both updated to use mpnet

### Boot Injection
- cortex-boot now outputs INFRASTRUCTURE + CRITICAL LESSONS on every session
- Lessons with importance >= 8 appear automatically
- No agent can miss the production topology

## Cortex API — New Way of Working

All Cortex operations now go through the API at `http://localhost:8501`. The cortex-* CLI scripts call the API. Direct database access is restricted to CPO agents for emergency recovery only.

### How to use Cortex (API-first)

```bash
# Boot a session
curl http://localhost:8501/boot/rex -H "X-Project: kaidera"

# Log a decision (auto-embeds)
curl -X POST http://localhost:8501/log \
  -H "X-Agent-Name: rex" -H "X-Project: kaidera" \
  -H "Content-Type: application/json" \
  -d '{"event_type": "decision", "summary": "Your decision text here"}'

# Search
curl "http://localhost:8501/search?q=your+query" -H "X-Project: kaidera"

# Create handoff
curl -X POST http://localhost:8501/handoffs \
  -H "X-Agent-Name: rex" -H "X-Project: kaidera" \
  -H "Content-Type: application/json" \
  -d '{"to_role": "backend-specialist", "priority": "high", "summary": "Task description"}'
```

The cortex-* CLI scripts also work — they call the API internally.

### What Kaidera AI Team Should Do

1. Verify Cortex API is running: `curl http://localhost:8501/health`
2. Boot test: `curl http://localhost:8501/boot/rex -H "X-Project: kaidera"`
3. Check project: `curl http://localhost:8501/projects` — kaidera should be listed
4. Seed Kaidera AI-specific lessons via the API:
   ```bash
   curl -X POST http://localhost:8501/log \
     -H "X-Agent-Name: rex" -H "X-Project: kaidera" \
     -H "Content-Type: application/json" \
     -d '{"event_type": "lesson", "summary": "Kaidera AI uses a monorepo with 6 components", "importance": 8}'
   ```
5. Update AGENTS.md with topology section (where the app runs, deployment path)
6. Update role profiles with positive infrastructure directives

### Kaidera AI Production Cortex (Future — from Amad)

For the Kaidera AI production platform, the Cortex needs a more robust auth model:
- **Identity:** `project + agent + company` (multiple companies share the same Cortex)
- **Auth:** API tokens required — each company gets a token for their agents
- **Security:** Company isolation — agents from Company A cannot see Company B's data
- **Scope:** This is Rex-Kaidera's responsibility to design and implement

The local dev Cortex (shared on Mac) uses simple `agent:project` headers. The production version needs the full auth model above.

### Ongoing
- Log lessons generously — they auto-embed and surface in future boots
- Use natural language in search — semantic search finds related content
- End every session with diary entry
- Log architecture learnings at importance 8+ (appear in all future boots)

## Research Documents (in Obsidian vault)

| Doc | What |
|-----|------|
| 22 - Hive Mind Memory & Session Continuity | Why agents forget, how to fix it |
| 23 - GitNexus Patterns for Cortex | Hybrid search, process traces, precomputed context |
| 24 - Cortex API Layer | API design to replace direct DB access |
| 25 - Cortex v2.1 Architecture & Recovery | Full architecture, 18-model benchmark, recovery plan |

All in: `~/Library/CloudStorage/Dropbox/Amads-Vault/03 Research/Multi-agent Development/`
