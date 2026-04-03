# Genie Slack Bot

## Architecture

Clean Architecture: `domain/` (pure logic) → `infra/` (external services) → `presentation/` (Slack UI, charts, PDF).

DI wiring is in `app.py`. All research components are lazy-loaded only when `ENABLE_RESEARCH=true`.

## Key Design Decisions

### Research Pipeline

`domain/orchestrator.py` runs: Plan → Parallel Execute → Evaluate → (optional Follow-up) → Synthesize.

- Plan generates `INITIAL_PLAN_STEPS` (default 4) sub-questions using "overview → hypothesis-driven drill-down → confounding control" framework, all executed in parallel via Genie API
- After parallel batch, LLM evaluates results using quality signals (outlier, contradiction, concentration, missing driver) and may add up to 2 follow-up questions
- Follow-up questions run sequentially with context injection from prior results
- Simple questions typically complete in 4 steps (~3 min). Complex questions (hypothesis conflicts) extend to `MAX_STEPS` (default 6, ~6 min)
- `MAX_DURATION` (default 300s) governs follow-up and chart skip only
- No hard timeout on: initial parallel batch, Genie API polling, narrative generation, PDF upload

### Heartbeat & Orphan Recovery

- Orchestrator updates `heartbeat_at` in Delta every ~30s during execution
- On startup, `slack_handler.recover_orphaned_jobs()` marks stale jobs as failed
- Stale = `heartbeat_at` older than `ORPHAN_THRESHOLD` (default 600s / 10min)
- Orphaned jobs are **failed, not resumed** (idempotency is not guaranteed)

### Font Handling (Japanese)

- `presentation/font_init.py` is the single source of truth for Japanese font registration
- Both `chart_generator.py` and `chart_generator_quick.py` call `register_japanese_font()`
- `addfont()` runs once; `rcParams["font.family"]` is re-applied every call (seaborn's `set_style` resets it)
- **Do not use `sns.set_theme()`** — it resets ALL rcParams including font.family

### Deployment

- `databricks bundle deploy` uploads files but does NOT restart the app
- `databricks apps deploy <app-name> --source-code-path <path>` is needed to restart
- Always restart after deploy to pick up code changes
- `scripts/setup_permissions.sh` automates SP permission setup (Genie, Warehouse, UC)
- `Config.validate_runtime(ws)` checks LLM endpoints and catalog at startup (warns, does not fail)

### Slack Formatting

- `_markdown_to_slack()` converts Markdown bold (`**text**`) to Slack mrkdwn (`*text*`)
- Japanese text requires spaces around `*` delimiters for Slack to recognize bold
- `_fix_bold_boundaries()` handles this automatically

## LLM Endpoints

| Endpoint env var | Purpose | Used by | Recommended model |
|---|---|---|---|
| `LLM_CHART_ENDPOINT` | Chart spec generation | Both modes | GPT-5.4-mini |
| `LLM_RESEARCH_ENDPOINT` | Plan, evaluate, summarize | Research only | GPT-5.4 |
| `LLM_NARRATIVE_ENDPOINT` | Report narrative (long Japanese) | Research only | Claude Opus 4.6 |

## Delta Tables

Auto-created by `infra/init_tables.py` in `{RESEARCH_CATALOG}.genie_research`:

- `research_jobs` — job state machine (queued → planning → running → evaluating → synthesizing → completed)
- `research_steps` — per-step results, chart paths, column profiles
- `research_reports` — final merged Markdown + raw narrative

## Known Limitations & Future Work

### Evaluate follow-up frequency
- Switched from gap-checklist to quality-based triggers (outlier, contradiction, concentration, missing driver)
- Plan prompt and Evaluate prompt no longer share the same analytical framework
- **Remaining risk**: With narrow Genie Spaces, even quality signals may be weak (few columns = few anomalies). Actual follow-up behavior depends on LLM judgment — prompt changes are not deterministic.

### Question/chart diversity
- Evaluate now rejects follow-ups with duplicate dimension+measure column combinations (same dims with different measures are still allowed for contradiction investigations)
- bar→hbar threshold relaxed (>12 categories / >16 avg label length)
- column_profile upgrades categorical columns that match date patterns (e.g., `2024-01`, `1月`) to `semantic_role: "time"`, and chart_generator uses this to override bar→line when the x-axis has role=time
- Chart title now reflects sub-question context and color/grouping axis (not just y-axis measure)
- **Remaining risk**: Temporal override only fires when chart LLM initially chose bar/hbar; if LLM chose scatter/pie, the override does not apply

## Testing

```bash
cd src/app
ENABLE_RESEARCH=true RESEARCH_CATALOG=<catalog> \
DATABRICKS_GENIE_SPACE_ID=<space_id> \
LLM_RESEARCH_ENDPOINT=databricks-gpt-5-4 \
LLM_CHART_ENDPOINT=databricks-gpt-5-4-mini \
LLM_NARRATIVE_ENDPOINT=databricks-claude-opus-4-6 \
uv run python ../../scripts/local_e2e_test.py --profile=<profile>
```
