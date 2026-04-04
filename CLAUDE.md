# Genie Slack Bot

## Architecture

Clean Architecture: `domain/` (pure logic) → `infra/` (external services) → `presentation/` (Slack UI, charts, PDF).

DI wiring is in `app.py`. `ChartGenerator` is always instantiated (used by both modes). Other research components are lazy-loaded only when `ENABLE_RESEARCH=true`.

## Key Design Decisions

### Research Pipeline

`domain/orchestrator.py` runs: Plan → Parallel Execute → (Auto-Retry) → Evaluate → (optional Follow-up) → Synthesize.

- Plan generates `INITIAL_PLAN_STEPS` (default 4) sub-questions using "overview → hypothesis-driven drill-down → confounding control" framework, all executed in parallel via Genie API
- Plan LLM receives Genie Space schema with column types (e.g., `contract_date (date)`, `rent (double)`) to prevent hallucination of non-existent columns
- Schema constraint is HIGHEST PRIORITY in the plan prompt (exact column names only, no paraphrasing). Diversity and cross-tab rules are hard constraints — when sparse schemas cause failures, auto-retry handles recovery
- If Plan questions fail (Genie can't generate SQL), auto-retry generates alternative questions via Evaluate (force_continue). Retries do NOT consume the follow-up budget (MAX_STEPS)
- After parallel batch + retries, LLM evaluates results using quality signals (outlier, contradiction, concentration, missing driver) and may add up to 2 follow-up questions
- Follow-up questions run sequentially with context injection from prior results
- `_transition_to(job_id, target)` handles all state transitions — reads actual status before CAS, no hardcoded 'from' states
- Simple questions typically complete in 4 steps (~3 min). Complex questions (hypothesis conflicts) extend to `MAX_STEPS` (default 6, ~6 min)
- `MAX_DURATION` (default 300s) governs follow-up and chart skip only
- No hard timeout on: initial parallel batch, Genie API polling, narrative generation, PDF upload

### Chart Rendering

- `infra/chart_spec_client.py` is the single source of truth for LLM chart spec generation (prompt + field normalization)
- `presentation/chart_generator.py` (`ChartGenerator` class) is the single entry point: `generate_bytes()` for quick mode, `generate()` for research mode
- `presentation/chart_renderer.py` is the shared renderer (15 chart types: bar, hbar, line, multiline, area, stacked_bar, grouped_bar, donut, pie, scatter, bubble, histogram, dual_axis, heatmap, boxplot)
- LLM generates chart spec with `sort` field (desc/asc/none). Renderer applies LLM sort; defaults to desc for bar, asc for hbar when sort is "none"
- `generate()` returns `(volume_path, canonical_spec)` — orchestrator uses the spec's sort to reorder the stored `result_sample` so report tables match chart sort order
- `_prepare()` handles x/y auto-swap when LLM assigns columns backwards, temporal detection (bar→line override), and per-type validation (histogram only needs x, etc.)
- Chart colors use seaborn's `"muted"` palette (global) and `"YlGnBu_r"` for bar/hbar gradients — no custom color constants
- PDF report colors are hardcoded in `pdf_renderer.py` (dark blue `(41,65,122)` accent theme)

### Heartbeat & Orphan Recovery

- Orchestrator updates `heartbeat_at` in Delta every ~30s during execution
- On startup, `slack_handler.recover_orphaned_jobs()` marks stale jobs as failed
- Stale = `heartbeat_at` older than `ORPHAN_THRESHOLD` (default 600s / 10min)
- Orphaned jobs are **failed, not resumed** (idempotency is not guaranteed)

### Font Handling (Japanese)

- `presentation/font_init.py` is the single source of truth for Japanese font registration
- `chart_renderer.py` calls `register_japanese_font()` at module load (idempotent)
- `addfont()` runs once; `rcParams["font.family"]` is re-applied every call (seaborn's `set_style` resets it)
- **Do not use `sns.set_theme()`** — it resets ALL rcParams including font.family

### Deployment

- `databricks bundle deploy` uploads files but does NOT restart the app
- `databricks apps deploy <app-name> --source-code-path <path>` is needed to restart
- Always restart after deploy to pick up code changes
- `scripts/setup_permissions.sh` automates SP permission setup (Genie, Warehouse, UC)
- `Config.validate_runtime(ws)` checks LLM endpoints and catalog at startup (warns, does not fail)

### Storage & Cleanup

- Charts: `/Volumes/{catalog}/{schema}/charts/{job_id}/{step_id}.png` — per-job directory
- PDF: `/Volumes/{catalog}/{schema}/charts/{job_id}/report.pdf` — saved alongside charts
- Volume paths use `Config.volume_charts_dir(job_id)` — single source of truth
- `CLEANUP_RETENTION_DAYS` (default 30) — orchestrator deletes chart/PDF dirs for jobs older than this after each completion
- Cleanup is best-effort (async, 1-hour cooldown, parallel deletes via asyncio.gather, max 50 jobs per run)
- Job IDs are human-readable: `res_{YYYYMMDD_HHMMSS}_{8-char-hex}`

### Slack Formatting

- `_markdown_to_slack()` converts Markdown bold (`**text**`) to Slack mrkdwn (`*text*`)
- Japanese text requires spaces around `*` delimiters for Slack to recognize bold
- `_fix_bold_boundaries()` handles this automatically

## LLM Endpoints

| Endpoint env var | Purpose | Used by | Recommended model |
|---|---|---|---|
| `LLM_CHART_ENDPOINT` | Chart spec generation (15 types + sort) | Both modes (via `chart_spec_client`) | GPT-5.4-mini |
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
- column_profile upgrades categorical columns that match date patterns (e.g., `2024-01`, `1月`) to `semantic_role: "time"`, and chart_renderer uses this to override bar→line when the x-axis has role=time
- Chart title now reflects sub-question context and color/grouping axis (not just y-axis measure)
- **Remaining risk**: Temporal override only fires when chart LLM initially chose bar/hbar; if LLM chose scatter/pie, the override does not apply

### Chart generator unification (completed)
- `infra/chart_spec_client.py` がLLMチャートスペック生成の単一実装（統一プロンプト + フィールド正規化）
- `LLMClient.generate_chart_spec()` は `chart_spec_client.get_chart_spec()` への薄いasyncラッパー
- `presentation/chart_generator.py` の `ChartGenerator` クラスが唯一の入口: `generate_bytes()`（quick mode、同期）と `generate()`（research mode、async + Volume保存 + spec返却）
- `chart_generator_quick.py` は削除済み

### Genie schema fetch
- `genie_client.get_schema()` がGenie Spaceにスキーマ情報をリクエストし、プロセス単位でキャッシュ
- カラム名に加えてデータ型（string, date, int, double等）を含むフォーマットで取得
- Plan LLMに渡され、日時カラムの正確な識別や存在しないカラムの抑止に利用される

### Plan schema validation (next step)
- 現在はプロンプトでスキーマ遵守を指示しているだけで、Plan LLM の出力をプログラムで検証していない
- プロンプトによる制約は確率的であり、モデルやコンテキストの変化で破られる可能性がある
- **Next step**: Plan 出力後にスキーマとの突合チェックを実装。質問から使用カラムを抽出し、スキーマに存在しなければリジェクト＆再生成する

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
