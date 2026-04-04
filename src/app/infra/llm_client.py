"""Foundation Model API client for research planning, evaluation, and synthesis."""
import asyncio
import json
import logging
from typing import Optional

from databricks.sdk import WorkspaceClient

from config import Config

logger = logging.getLogger(__name__)

# Common JSON output instruction appended to all JSON-returning prompts
_JSON_RULE = "Return one JSON object only. No leading/trailing text, no markdown fences. Use null (not \"null\" string)."

PLAN_SYSTEM_PROMPT_TEMPLATE = """You are a senior data analyst planning a multi-step research investigation. Given a user's question, decompose it into exactly {n} sub-questions that each produce a single SQL query.

## Schema constraint (HIGHEST PRIORITY)
If a data source reference is provided, it is the ONLY source of truth for available data.
- You MUST use ONLY the exact column names listed in the schema. Do NOT invent, paraphrase, or assume columns that are not explicitly listed.
- Do NOT use generic business terms (e.g., "商品カテゴリ", "product category", "customer segment") unless they appear verbatim as column names in the schema.
- Questions referencing non-existent columns WILL FAIL with no recovery.
- Before finalizing each question, verify: "Does every column I reference exist in the schema?" If not, rewrite.

## Approach: Overview first, then hypothesis-driven drill-down

**Question 1 — Overview**: A broad aggregation across the primary dimension that reveals the overall shape (rankings, totals, averages across all groups).

**Questions 2 to {n_minus_1} — Hypothesis-driven drill-downs**: Anticipate the most likely pattern from the overview. Form hypotheses about WHY, and design each question to test a different one.
If a date/time column exists in the schema, one drill-down MUST analyze trends over time.

**Question {n} — Control for confounding factors**: The last question MUST cross-tabulate TWO or more dimensions to test whether the patterns from earlier questions hold after controlling for a confounding variable.

Example pattern:
- Q1: "Total Y by primary dimension" (overview)
- Q2: "Y broken down by dimension A" (hypothesis: A drives the pattern)
- Q3: "Y broken down by dimension B" (competing hypothesis: B drives it)
- Q4: "Y by dimension A AND dimension B" (control: does A still matter after accounting for B?)

Each drill-down question must:
- Target a DIFFERENT explanatory dimension (do not reuse the same grouping columns)
- Test a specific "why" or "what drives this" — not just slice the same data differently

Rules:
- Return exactly {n} sub-questions as a JSON array of non-empty strings, no duplicates
- Each sub-question must be self-contained and answerable independently
- Use the SAME LANGUAGE as the user's question
- Each question must use a DIFFERENT set of grouping/dimension columns from the others
- Do NOT ask for statistical functions that produce a single number (correlation coefficients, R-squared, regression, p-values)
- Questions should produce multi-row results (at least 3+ rows of category × numeric value)

{json_rule}
Example: {{"sub_questions": ["question 1", "question 2", "question 3", "question 4"]}}"""

SUMMARIZE_SYSTEM_PROMPT = """Summarize the following SQL query result in 1-2 sentences.
Focus on the key finding. Use the SAME LANGUAGE as the question. Be specific with numbers.

IMPORTANT:
- The data shown is a SAMPLE (first rows). Do not claim it represents the full dataset unless row_count matches sample size.
- Say "the sample shows..." or "the top N rows indicate..." when the full dataset is larger.
- Do NOT infer trends or patterns beyond what the provided rows show.
- Do NOT fabricate numbers not present in the data."""

EVALUATE_SYSTEM_PROMPT_TEMPLATE = """You are evaluating research results to decide whether deeper follow-up is needed.

{phase_context}

Review the results and decide:
1. If the existing evidence is already sufficient to answer the original question well → synthesize
2. If the results reveal an important unresolved pattern worth investigating → continue with follow-up questions

CRITICAL: Your response must be a single JSON object starting with {{"action":...}}.

Look for these QUALITY signals in the completed results that warrant follow-up:
- **Outlier**: A single category dominates disproportionately or is far above/below the rest — worth investigating WHY
- **Contradiction**: Two steps show conflicting patterns for similar metrics — need a controlling query to resolve
- **Concentration**: Results are dominated by a few categories while others are negligible — worth checking if this holds after a different cut
- **Missing driver**: A large difference exists between groups but no step explains WHAT causes it

Do NOT continue for these reasons:
- A generic analysis type (temporal, share, ranking) is absent — Plan already designed the analytical coverage
- The data "could be" interesting to slice another way — without a specific anomaly, synthesize
- Wanting to "confirm" a finding — if two steps agree, that IS confirmation

Follow-up questions must be clearly motivated by observed results AND directly help answer the original question — not just explore an interesting tangent.

If failed questions are listed, consider whether their analytical angle is worth retrying from a different direction. A failed question means the data source could not generate SQL for it — suggest a simpler alternative that uses different columns to get at the same insight.

Rules:
- Maximum 2 new follow-up questions per evaluation
- new_questions must be an array of non-empty strings in the same language as the original question
- Do not repeat questions already asked. Do not suggest follow-up questions that reuse the same dimension AND measure column combinations listed in the user message — these would produce structurally redundant analysis. Reusing dimensions with a DIFFERENT measure is acceptable for contradiction/driver investigations
- If fewer than {max_steps} steps are completed and a high-value finding needs explanation, continue
- Include a brief reason for your decision

{json_rule}
Examples:
{{"action": "continue", "reason": "Region A dominates disproportionately — need to check if this holds after controlling for category", "new_questions": ["question motivated by data", "question to explain finding"]}}
{{"action": "synthesize", "reason": "All major angles covered, no standout anomalies requiring deeper analysis"}}"""

NARRATIVE_SYSTEM_PROMPT = """You are a senior data analyst writing a polished research report for business stakeholders.

Write a cohesive, professional Markdown report. This must read like a consulting report — NOT like a Q&A or a list of query results.

Language: Use the SAME LANGUAGE as the original question for ALL headings and text.
If Japanese, use Japanese headings (## 要約, ## 分析結果, ## 結論).

Step summaries may be in English or terse (they come from Genie's raw SQL descriptions).
Interpret them and write the report entirely in the question's language regardless.
If a step summary is unclear, focus on the data tables that will be injected at [Step N] markers.

You will receive column_profile metadata with statistics (min/max/mean/median/stddev for numeric columns, top values for categorical columns). These are computed from sampled query results — use them as directional evidence to support your analysis. Do not claim exact population-level precision from sample statistics.

Structure:
1. **# Title** — a compelling analytical title (not just restating the question)
2. **## 要約 / Summary** — 3-5 sentences with the key conclusion and most important numbers. Write as if this is the only section the reader will see.
3. **## 分析結果 / Findings** — the core analysis, organized by THEME or INSIGHT (NOT by step/query):
   - Use ### subheadings that describe the INSIGHT, not the query (e.g., "### Primary driver of variance" / "### 格差の主因" not "### Raw ranking output" / "### ランキング結果")
   - Write flowing prose that builds an argument. Each paragraph should have a clear point.
   - Naturally weave in data references — place [Step N] on its own line where a supporting chart/table should appear
   - **Cross-reference findings across steps**:
     - If step A and step B show the SAME metric with DIFFERENT values (e.g., different averages for the same group), explain WHY (different filters, groupings, or conditions)
     - If step A suggests X is the key driver but step B shows Y matters more, discuss the CONTRADICTION and resolve it
     - If step A's ranking changes after controlling for conditions in step B, highlight this as a key finding
   - **Include at least one cross-cutting insight** that combines evidence from 2+ steps to reach a conclusion that NO single step could support alone
4. **## 結論 / Conclusion** — REQUIRED:
   - The definitive answer to the original question
   - Key factors ranked by importance
   - Actionable recommendations or implications

[Step N] marker rules:
- Use [Step N] format EXACTLY (N = integer 1, 2, 3..., NOT step ID like s1)
- Place each marker on its own line where supporting evidence should appear
- Use each [Step N] marker AT MOST ONCE — do not repeat the same marker
- Only reference steps that exist in the provided data

CRITICAL RULES:
- Write like a NARRATIVE, not a report card. Never write "Step 1 showed..." or "The query results indicate..."
- Do NOT repeat the sub-questions. The reader doesn't know or care what SQL was run.
- Do NOT list findings in the same order as the steps. Organize by importance and logical flow.
- Data tables and charts will be automatically injected at [Step N] markers
- Every claim must be backed by data from the steps. Do NOT fabricate numbers.
- Aim for the quality of a McKinsey or BCG research deliverable."""


def _format_column_meta(meta: list[dict]) -> str:
    """Format column metadata for evaluate prompt (compact)."""
    if not meta:
        return "unknown"
    parts = []
    for m in meta:
        dtype_label = "num" if m.get("dtype") == "numeric" else "cat"
        role = m.get("semantic_role", "")
        role_str = f",role={role}" if role else ""
        parts.append(f"{m['name']}({dtype_label},uniq={m.get('unique_count', '?')}{role_str})")
    return ", ".join(parts)


class LLMClient:
    """Foundation Model API client for research orchestration."""

    def __init__(self, ws: WorkspaceClient):
        self._ws = ws
        self._endpoint = Config.LLM_RESEARCH_ENDPOINT
        self._narrative_endpoint = Config.LLM_NARRATIVE_ENDPOINT

    @staticmethod
    def _extract_response_text(resp: dict, endpoint: str) -> str:
        """Extract text from LLM response with validation."""
        try:
            choices = resp.get("choices")
            if not choices:
                raise ValueError(f"No 'choices' in response from {endpoint}")
            text = choices[0]["message"]["content"].strip()
        except (KeyError, IndexError, TypeError) as e:
            raise RuntimeError(
                f"Unexpected response format from {endpoint}: {e}. "
                f"Response: {str(resp)[:200]}"
            )

        # Extract from code fences
        if "```" in text:
            parts = text.split("```")
            for part in parts[1:]:
                content = part.split("\n", 1)[-1] if "\n" in part else part
                content = content.strip()
                if content:
                    return content
        return text

    async def _call(self, system: str, user: str, max_tokens: int = 1000) -> str:
        """Call Foundation Model API and return content string."""
        resp = await asyncio.to_thread(
            self._ws.api_client.do,
            "POST",
            f"/serving-endpoints/{self._endpoint}/invocations",
            body={
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "max_tokens": max_tokens,
                "temperature": 0,
            },
        )
        return self._extract_response_text(resp, self._endpoint)

    async def _call_narrative(self, system: str, user: str, max_tokens: int = 4000) -> str:
        """Call narrative LLM endpoint (Opus) for report generation.

        Uses requests directly with a 10-minute timeout because Opus
        generates long Japanese narratives that exceed the SDK's default
        60s per-request / 300s retry timeout.
        """
        import requests as _requests

        def _do():
            cfg = self._ws.config
            url = f"{cfg.host}/serving-endpoints/{self._narrative_endpoint}/invocations"
            headers = {"Content-Type": "application/json"}
            headers.update(cfg.authenticate())
            r = _requests.post(
                url,
                json={
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "max_tokens": max_tokens,
                    "temperature": 0,
                },
                headers=headers,
                timeout=600,
            )
            r.raise_for_status()
            return r.json()

        resp = await asyncio.to_thread(_do)
        return self._extract_response_text(resp, self._narrative_endpoint)

    @staticmethod
    def _extract_json(text: str) -> Optional[str]:
        """Try to extract a JSON object from text that may contain prose."""
        import re
        match = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', text)
        if match:
            return match.group(0)
        return None

    async def generate_chart_spec(
        self, columns: list[dict], sample_rows: list[list],
        column_profile: list[dict] | None = None,
        question: str | None = None,
    ) -> Optional[dict]:
        """Generate a chart spec from column metadata and sample data.

        Delegates to ``infra.chart_spec_client.get_chart_spec`` (synchronous)
        via ``asyncio.to_thread``.  Returns a canonical spec dict with keys
        ``type, x, y, y2, hue, sort, title``, or None.
        """
        from infra.chart_spec_client import get_chart_spec

        return await asyncio.to_thread(
            get_chart_spec,
            self._ws,
            columns,
            sample_rows,
            column_profile=column_profile,
            question=question,
        )

    async def generate_plan(self, question: str, schema_info: str = "") -> list[str]:
        """Generate research sub-questions from user question."""
        user_msg = question
        if schema_info:
            if len(schema_info) > 5000:
                logger.warning(f"Large schema info ({len(schema_info)} chars) — may affect plan latency")
            user_msg = (
                f"{question}\n\n"
                f"[Data source reference — tables and columns that are likely available]:\n"
                f"{schema_info}\n\n"
                f"Prefer questions that use the columns listed above. "
                f"If a question requires data not listed, it may fail — consider alternatives."
            )
        plan_prompt = PLAN_SYSTEM_PROMPT_TEMPLATE.format(
            n=Config.INITIAL_PLAN_STEPS,
            n_minus_1=Config.INITIAL_PLAN_STEPS - 1,
            json_rule=_JSON_RULE,
        )
        text = await self._call(plan_prompt, user_msg, max_tokens=3000)
        try:
            data = json.loads(text)
            questions = data.get("sub_questions", [])
        except json.JSONDecodeError:
            extracted = self._extract_json(text)
            if extracted:
                try:
                    questions = json.loads(extracted).get("sub_questions", [])
                except json.JSONDecodeError:
                    logger.error(f"Failed to parse plan JSON: {text[:200]}")
                    return []
            else:
                logger.error(f"Failed to parse plan JSON: {text[:200]}")
                return []

        # Validate: must be list of non-empty strings
        if not isinstance(questions, list):
            logger.error(f"Plan returned non-list: {type(questions)}")
            return []
        return [q for q in questions if isinstance(q, str) and q.strip()]

    async def summarize_result(self, question: str, columns: list[str], sample_rows: list, row_count: int) -> str:
        """Summarize a Genie query result in 1-2 sentences."""
        sample_note = f" (showing first {len(sample_rows)} of {row_count})" if row_count > len(sample_rows) else ""
        user_msg = (
            f"Question: {question}\n"
            f"Columns: {', '.join(columns)}\n"
            f"Total rows: {row_count}{sample_note}\n"
            f"Sample data (first 5 rows): {json.dumps(sample_rows[:5], ensure_ascii=False)}"
        )
        return await self._call(SUMMARIZE_SYSTEM_PROMPT, user_msg, max_tokens=200)

    @staticmethod
    def _column_fingerprints(step_summaries: list[dict]) -> set[tuple[frozenset[str], frozenset[str]]]:
        """Extract (dimensions, measures) fingerprints from completed steps.

        Uses semantic_role to identify dimensions (not dtype), so numeric
        dimensions like year/month codes are correctly included.

        Returns set of (dimension_cols, measure_cols) tuples. Two steps are
        structurally redundant only when BOTH dimensions AND measures match.
        This allows follow-ups that reuse the same dimensions with a different
        measure (e.g., contradiction/missing-driver investigations).
        """
        _DIMENSION_ROLES = {"dimension", "high_cardinality_dimension", "time", "ordinal_bin"}
        fingerprints = set()
        for s in step_summaries:
            meta = s.get("column_meta", [])
            dims = frozenset(
                m["name"] for m in meta
                if m.get("semantic_role", "") in _DIMENSION_ROLES
            )
            measures = frozenset(
                m["name"] for m in meta
                if m.get("semantic_role", "") == "measure"
            )
            if dims:
                fingerprints.add((dims, measures))
        return fingerprints

    async def evaluate_progress(
        self, original_question: str, step_summaries: list[dict],
        failed_questions: list[str] | None = None,
        force_continue: bool = False,
    ) -> dict:
        """Evaluate whether to continue or synthesize."""
        steps_text = "\n".join(
            f"Step {s['step_id']}: {s['question']} → {s['summary']} "
            f"(rows={s.get('row_count', '?')}, columns={_format_column_meta(s.get('column_meta', []))})"
            for s in step_summaries
        )
        phase = "initial plan" if len(step_summaries) <= Config.INITIAL_PLAN_STEPS else "follow-up"
        phase_context = (
            f"Completed steps: {len(step_summaries)} of max {Config.MAX_STEPS} (phase: {phase}).\n"
            f"These steps were the {phase}."
        )
        eval_prompt = EVALUATE_SYSTEM_PROMPT_TEMPLATE.format(
            phase_context=phase_context,
            max_steps=Config.MAX_STEPS,
            json_rule=_JSON_RULE,
        )
        existing_fps = self._column_fingerprints(step_summaries)
        fps_text = "; ".join(
            f"dims={{{', '.join(sorted(dims))}}} measures={{{', '.join(sorted(measures))}}}"
            for dims, measures in existing_fps
        )
        failed_text = ""
        if failed_questions:
            failed_list = "\n".join(f"- {q}" for q in failed_questions)
            if force_continue:
                failed_text = (
                    f"\n\nFailed questions (data source could not answer these):\n{failed_list}\n"
                    f"IMPORTANT: You MUST return action=continue with alternative questions. "
                    f"For each failed question, suggest a SIMPLER alternative that approaches "
                    f"the same insight using different columns available in the data source. "
                    f"Do NOT repeat the failed questions verbatim."
                )
            else:
                failed_text = (
                    f"\n\nFailed questions (data source could not answer these):\n{failed_list}\n"
                    f"If the analytical angle of a failed question is important, suggest an ALTERNATIVE "
                    f"question that approaches the same insight from a different direction using available columns."
                )

        user_msg = (
            f"Original question: {original_question}\n\n"
            f"Completed steps:\n{steps_text}\n\n"
            f"Column combinations already analyzed: {fps_text}\n"
            f"Do NOT suggest follow-up questions that reuse the SAME dimension AND measure columns. "
            f"Follow-ups that reuse dimensions with a DIFFERENT measure are acceptable (e.g., to investigate contradictions or missing drivers)."
            f"{failed_text}"
        )
        text = await self._call(eval_prompt, user_msg, max_tokens=800)
        try:
            result = json.loads(text)
        except json.JSONDecodeError:
            extracted = self._extract_json(text)
            if extracted:
                try:
                    result = json.loads(extracted)
                except json.JSONDecodeError:
                    logger.error(f"Failed to parse evaluation JSON: {text[:200]}")
                    return {"action": "synthesize"}
            else:
                logger.error(f"Failed to parse evaluation JSON: {text[:200]}")
                return {"action": "synthesize"}

        # force_continue: override synthesize when retrying failed questions
        if force_continue and result.get("action") != "continue":
            if result.get("new_questions"):
                # LLM said synthesize but provided alternatives — override to continue
                result["action"] = "continue"
                logger.info(f"Evaluate overridden to continue (force_continue, {len(result['new_questions'])} alternatives)")
            else:
                logger.info("Evaluate produced no alternatives despite force_continue, synthesizing")
                return {"action": "synthesize"}

        # Log reason for debugging
        if result.get("reason"):
            logger.info(f"Evaluate decision: {result['action']} — {result['reason']}")

        # Validate new_questions if continuing
        if result.get("action") == "continue":
            new_qs = result.get("new_questions", [])
            if not isinstance(new_qs, list) or not all(isinstance(q, str) and q.strip() for q in new_qs):
                logger.warning(f"Invalid new_questions from evaluate, falling back to synthesize: {new_qs}")
                return {"action": "synthesize"}
            # Deduplicate against existing questions
            existing = {s["question"].strip().lower() for s in step_summaries}
            filtered = [q for q in new_qs if q.strip().lower() not in existing]
            if not filtered:
                logger.info("All follow-up questions duplicate existing steps, synthesizing")
                return {"action": "synthesize"}
            result["new_questions"] = filtered

        return result

    async def generate_narrative(
        self,
        original_question: str,
        step_summaries: list[dict],
        evidence_structure: str,
    ) -> str:
        """Generate Markdown narrative for the final report."""
        steps_text = "\n".join(
            f"[Step {s['step_id']}] {s['question']} → {s['summary']} ({s.get('row_count', '?')} rows)"
            for s in step_summaries
        )
        user_msg = f"Original question: {original_question}\n\nResearch steps:\n{steps_text}\n\nEvidence structure:\n{evidence_structure}"
        return await self._call_narrative(NARRATIVE_SYSTEM_PROMPT, user_msg, max_tokens=4000)
