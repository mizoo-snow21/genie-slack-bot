"""Research orchestrator: plan → execute → evaluate → synthesize."""
import asyncio
import json
import logging
import time
from typing import Optional

from infra.genie_client import GenieClient
from infra.llm_client import LLMClient
from domain.column_profiler import compute_column_profile
from domain.report_renderer import ReportRenderer
from infra.job_store import JobStore
from infra.step_store import StepStore
from config import Config

logger = logging.getLogger(__name__)


def _sort_sample(
    sample: list[list],
    col_names: list[str],
    chart_spec: dict,
) -> list[list]:
    """Sort sample rows to match the chart's sort order.

    Returns the original list unchanged when no sort is needed.
    """
    sort_order = str(chart_spec.get("sort", "none")).lower()
    if sort_order == "none":
        return sample

    y_col = chart_spec.get("y")
    if not y_col or y_col not in col_names:
        return sample

    y_idx = col_names.index(y_col)

    def _sort_key(row):
        try:
            return float(row[y_idx])
        except (ValueError, TypeError, IndexError):
            return 0.0

    try:
        return sorted(sample, key=_sort_key, reverse=(sort_order == "desc"))
    except Exception:
        return sample


class ResearchOrchestrator:
    """Runs the full research loop for a single job."""

    def __init__(
        self,
        job_store: JobStore,
        step_store: StepStore,
        genie: GenieClient,
        llm: LLMClient,
        chart_generator=None,
    ):
        self._jobs = job_store
        self._steps = step_store
        self._genie = genie
        self._llm = llm
        self._chart_gen = chart_generator

    async def run(self, job_id: str):
        """Execute the full research pipeline for a job."""
        job = self._jobs.get_job(job_id)
        if not job:
            logger.error(f"Job {job_id} not found")
            return

        question = job["question"]
        config = json.loads(job["config"]) if isinstance(job["config"], str) else job["config"]
        max_steps = config.get("max_steps", Config.MAX_STEPS)
        max_duration = config.get("max_duration_seconds", Config.MAX_DURATION)
        max_rows = config.get("max_result_rows_per_query", Config.MAX_RESULT_ROWS)
        start_time = time.time()

        try:
            # --- PLAN ---
            if self._check_cancel(job_id):
                return

            # Fetch Genie Space schema to guide plan (best-effort, non-blocking).
            # Short timeout: schema is a hint, not a hard dependency.
            try:
                schema_info = await asyncio.wait_for(
                    self._genie.get_schema(), timeout=10
                )
            except (asyncio.TimeoutError, Exception) as e:
                logger.warning(f"Job {job_id}: schema fetch skipped ({e})")
                schema_info = ""

            sub_questions = await self._llm.generate_plan(question, schema_info=schema_info)
            if not sub_questions:
                self._jobs.transition_status(job_id, "planning", "failed", error="LLM failed to generate plan")
                return

            # Cap initial plan to configured size
            sub_questions = sub_questions[:Config.INITIAL_PLAN_STEPS]

            # Create step records
            for i, sq in enumerate(sub_questions):
                self._steps.create_step(job_id, f"s{i+1}", i + 1, sq)

            logger.info(f"Job {job_id}: planned {len(sub_questions)} sub-questions")

            # --- PARALLEL EXECUTE initial plan sub-questions ---
            completed_summaries = []

            if not self._jobs.transition_status(job_id, "planning", "running_subquestion"):
                logger.error(f"Job {job_id}: failed to transition to running_subquestion")
                return

            # Fire all initial plan sub-questions in parallel (no context injection
            # for the initial batch since there are no prior results yet).

            async def _run_initial_step(idx: int, sq: str):
                step_id = f"s{idx + 1}"
                return (idx, sq, step_id, await self._execute_step(
                    job_id, step_id, sq, max_rows,
                    config=config, start_time=start_time, max_duration=max_duration,
                ))

            tasks = [
                asyncio.create_task(_run_initial_step(i, sq))
                for i, sq in enumerate(sub_questions)
            ]

            # Wait with periodic cancel check
            while True:
                if self._check_cancel(job_id):
                    for t in tasks:
                        t.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    return

                done_tasks = [t for t in tasks if t.done()]
                if len(done_tasks) == len(tasks):
                    break
                await asyncio.sleep(2)
                self._jobs.update_heartbeat(job_id)

            initial_results = []
            for t in tasks:
                try:
                    initial_results.append(t.result())
                except Exception as exc:
                    initial_results.append(exc)

            # Collect results from parallel execution.
            # For the parallel batch we count total failures (not "consecutive")
            # because execution order is non-deterministic.
            parallel_failures = 0
            failed_questions = []
            for item in initial_results:
                if isinstance(item, Exception):
                    logger.exception(f"Job {job_id}: parallel step exception: {item}")
                    parallel_failures += 1
                    continue
                idx, sq, step_id, result = item
                if result:
                    completed_summaries.append({
                        "step_id": step_id,
                        "question": sq,
                        "summary": result["summary"],
                        "row_count": result.get("row_count", 0),
                        "column_meta": result.get("column_meta", []),
                    })
                else:
                    parallel_failures += 1
                    failed_questions.append(sq)

            step_counter = len(sub_questions)
            # plan_steps tracks only Plan + follow-up (retries are free, don't count)
            plan_steps = len(sub_questions)

            if not completed_summaries:
                self._jobs.transition_status(
                    job_id, "running_subquestion", "failed",
                    error=f"All {parallel_failures} initial sub-questions failed"
                )
                return

            if parallel_failures:
                logger.warning(
                    f"Job {job_id}: {parallel_failures}/{len(sub_questions)} "
                    f"parallel steps failed, proceeding with {len(completed_summaries)} successful"
                )

            # Reset consecutive failure counter for the sequential follow-up loop.
            # Parallel batch failures should NOT carry over.
            consecutive_failures = 0

            # --- AUTO-RETRY failed questions (outside MAX_STEPS budget) ---
            # Retries restore the Plan's intended coverage. They don't count
            # toward max_steps so follow-up budget is preserved.
            if failed_questions:
                if not self._transition_to(job_id, "evaluating"):
                    return

                retry_eval = await self._llm.evaluate_progress(
                    question, completed_summaries, failed_questions=failed_questions,
                    force_continue=True,
                )
                retry_qs = retry_eval.get("new_questions", [])[:len(failed_questions)]

                for i, rq in enumerate(retry_qs):
                    if self._check_cancel(job_id):
                        return
                    if time.time() - start_time > max_duration:
                        break

                    step_counter += 1
                    sid = f"s{step_counter}"
                    self._steps.create_step(job_id, sid, step_counter, rq)
                    logger.info(f"Job {job_id}: retry for failed step: {rq[:60]}")

                    if not self._transition_to(job_id, "running_subquestion"):
                        return

                    result = await self._execute_step(
                        job_id, sid, self._inject_context(rq, completed_summaries), max_rows,
                        config=config, start_time=start_time, max_duration=max_duration,
                    )
                    if result:
                        completed_summaries.append({
                            "step_id": sid, "question": rq,
                            "summary": result["summary"],
                            "row_count": result.get("row_count", 0),
                            "column_meta": result.get("column_meta", []),
                        })

                    # Transition back to evaluating for next retry (if any)
                    if i < len(retry_qs) - 1:
                        self._transition_to(job_id, "evaluating")

                failed_questions = []

            # --- EVALUATE (follow-up budget uses plan_steps, not step_counter) ---
            if plan_steps < max_steps and time.time() - start_time < max_duration:
                if self._check_cancel(job_id):
                    return

                if not self._transition_to(job_id, "evaluating"):
                    return

                evaluation = await self._llm.evaluate_progress(question, completed_summaries)

                if evaluation.get("action") == "continue":
                    # Pre-build (question, step_id) tuples so registration and execution stay in sync
                    followup_tasks = []
                    for nq in evaluation.get("new_questions", []):
                        if plan_steps < max_steps:
                            step_counter += 1
                            plan_steps += 1
                            sid = f"s{step_counter}"
                            self._steps.create_step(job_id, sid, step_counter, nq)
                            followup_tasks.append((nq, sid))

                    # --- Sequential follow-up loop ---
                    for nq, sid in followup_tasks:

                        if self._check_cancel(job_id):
                            return
                        if time.time() - start_time > max_duration:
                            logger.warning(f"Job {job_id}: max duration reached")
                            break

                        if not self._transition_to(job_id, "running_subquestion"):
                            return

                        result = await self._execute_step(
                            job_id, sid, self._inject_context(nq, completed_summaries), max_rows,
                            config=config, start_time=start_time, max_duration=max_duration,
                        )

                        if result:
                            completed_summaries.append({
                                "step_id": sid, "question": nq,
                                "summary": result["summary"],
                                "row_count": result.get("row_count", 0),
                                "column_meta": result.get("column_meta", []),
                            })
                            consecutive_failures = 0
                        else:
                            consecutive_failures += 1
                            if consecutive_failures >= 3:
                                self._jobs.transition_status(
                                    job_id, "running_subquestion", "failed",
                                    error=f"{consecutive_failures} consecutive failures"
                                )
                                return

                        # Re-evaluate after each follow-up
                        if plan_steps < max_steps and time.time() - start_time < max_duration:
                            if not self._transition_to(job_id, "evaluating"):
                                return
                            evaluation = await self._llm.evaluate_progress(question, completed_summaries)
                            if evaluation.get("action") == "continue":
                                for eq in evaluation.get("new_questions", []):
                                    if plan_steps < max_steps:
                                        step_counter += 1
                                        plan_steps += 1
                                        esid = f"s{step_counter}"
                                        self._steps.create_step(job_id, esid, step_counter, eq)
                                        followup_tasks.append((eq, esid))
                            else:
                                break

            # --- SYNTHESIZE ---
            if self._check_cancel(job_id):
                return

            # Fail if no evidence was gathered (all steps failed/skipped)
            if not completed_summaries:
                job = self._jobs.get_job(job_id)
                if job and job["status"] not in ("completed", "failed", "cancelled"):
                    self._jobs.transition_status(
                        job_id, job["status"], "failed",
                        error="All sub-questions failed; no evidence to synthesize"
                    )
                return

            # Re-read current status to avoid CAS mismatch. The actual status
            # depends on whether the loop exited from running_subquestion or
            # evaluating, which varies by exit path (max_steps, max_duration,
            # evaluate-says-stop, etc.).
            job = self._jobs.get_job(job_id)
            if not job or job["status"] in ("completed", "failed", "cancelled"):
                return
            from_status = job["status"]
            if not self._jobs.transition_status(job_id, from_status, "synthesizing"):
                return

            report, narrative = await self._synthesize(job_id, question, completed_summaries)

            # Save report THEN transition to completed. If save_report
            # succeeds but transition fails, the report exists with a
            # non-completed job (orphan recovery will mark it failed).
            # If save_report fails, the exception handler transitions to
            # failed. GET /report checks completed status, so a partial
            # state never leaks to clients.
            self._steps.save_report(job_id, report, report_narrative=narrative)

            elapsed = int(time.time() - start_time)
            if not self._jobs.transition_status(job_id, "synthesizing", "completed"):
                # CAS failed — someone else moved the job (cancel or orphan).
                # Report is saved but job won't show as completed. This is safe:
                # the report is inert data until the job reaches 'completed'.
                logger.warning(f"Job {job_id}: completed transition failed after report save")
                return
            logger.info(f"Job {job_id}: completed in {elapsed}s with {step_counter} steps")

            # Best-effort cleanup of old chart/PDF files
            try:
                asyncio.create_task(self._cleanup_old_files())
            except Exception:
                pass  # Never fail the job for cleanup

        except Exception as e:
            logger.exception(f"Job {job_id}: fatal error")
            # Try to transition to failed from whatever current state
            job = self._jobs.get_job(job_id)
            if job and job["status"] not in ("completed", "failed", "cancelled"):
                self._jobs.transition_status(
                    job_id, job["status"], "failed", error=str(e)[:500]
                )

    async def _execute_step(
        self, job_id: str, step_id: str, question: str, max_rows: int,
        *, config: dict | None = None, start_time: float = 0.0, max_duration: int = 300,
    ) -> Optional[dict]:
        """Execute a single sub-question via Genie, optionally generating a chart."""
        try:
            # Mark step as running BEFORE Genie call so that GET /research/{id}
            # shows the correct current_step during execution. conversation_id
            # is updated after the call completes.
            self._steps.update_step_running(job_id, step_id, genie_conversation_id="")

            def heartbeat():
                self._jobs.update_heartbeat(job_id)

            result = await self._genie.ask_question_async(
                question=question,
                heartbeat_callback=heartbeat,
            )

            if not result["success"]:
                logger.warning(f"Step {step_id} failed: {result.get('error')}")
                self._steps.update_step_failed(job_id, step_id)
                return None

            # Update conversation_id now that we have it
            conv_id = result.get("conversation_id", "")
            if conv_id:
                self._steps.update_step_running(job_id, step_id, conv_id)

            # Extract data — result_data may be None if Genie answered
            # with text only (no SQL generated for the question)
            result_data = result.get("result_data") or {}
            result_schema = result.get("result_schema") or {}
            data_array = result_data.get("data_array", [])
            columns = result_schema.get("columns", [])
            col_names = [c.get("name", "") for c in columns]

            if not data_array or not columns:
                logger.warning(f"Step {step_id}: Genie returned no data (text-only response)")
                self._steps.update_step_failed(job_id, step_id)
                return None

            # Use row_count from SQL statement API metadata (not len(data_array)
            # which may be a page fragment). Fall back to manifest total_row_count.
            total_rows = (
                result_data.get("row_count")
                or result.get("result_schema", {}).get("total_row_count")
                or len(data_array)
            )
            if isinstance(total_rows, str):
                total_rows = int(total_rows)
            sample = data_array[:max_rows]
            is_truncated = total_rows > len(sample)

            # Use Genie's own description instead of a separate LLM call
            summary = result.get("description", "") or f"Query returned {total_rows} rows"

            # Compute column profile from full data_array (up to 100 rows),
            # independent of max_rows which controls result_sample size.
            profile_rows = data_array[:100]
            profile = compute_column_profile(columns, profile_rows)

            # Save step result
            self._steps.update_step_completed(
                job_id=job_id,
                step_id=step_id,
                sql_query=result.get("sql_query", ""),
                result_summary=summary,
                result_columns=columns,
                result_row_count=total_rows,
                result_sample=sample,
                result_is_truncated=is_truncated,
                column_profile=profile,
            )

            # Check cancel before potentially long chart generation
            if self._check_cancel(job_id):
                return {"summary": summary, "row_count": total_rows}

            # Chart generation (if enabled and chart_generator is available)
            if self._chart_gen and config:
                enable_charts = config.get("enable_charts", True)
                if enable_charts:
                    remaining_time = max_duration - (time.time() - start_time)
                    if remaining_time > Config.CHART_TIMEOUT:
                        try:
                            result = await asyncio.wait_for(
                                self._chart_gen.generate(
                                    job_id=job_id,
                                    step_id=step_id,
                                    columns=columns,
                                    sample_rows=sample,
                                    column_profile=profile,
                                    question=question,
                                ),
                                timeout=Config.CHART_TIMEOUT,
                            )
                            if result:
                                chart_path, chart_spec = result
                                self._steps.update_step_chart(job_id, step_id, chart_path)
                                # Sort table data to match chart sort order
                                sorted_sample = _sort_sample(
                                    sample, col_names, chart_spec,
                                )
                                if sorted_sample is not sample:
                                    self._steps.update_step_sample(
                                        job_id, step_id, sorted_sample,
                                    )
                        except asyncio.TimeoutError:
                            logger.warning(f"Chart generation timed out for {job_id}/{step_id}")
                        except Exception as e:
                            logger.warning(f"Chart generation failed for {job_id}/{step_id}: {e}")
                    else:
                        logger.info(f"Chart skipped for {job_id}/{step_id}: remaining time {remaining_time:.0f}s < {Config.CHART_TIMEOUT}s")

            self._jobs.update_heartbeat(job_id)

            return {
                "summary": summary,
                "row_count": total_rows,
                "column_meta": [
                    {"name": p["name"], "dtype": p["dtype"], "unique_count": p["unique_count"], "semantic_role": p.get("semantic_role", "")}
                    for p in profile
                ],
            }

        except Exception as e:
            logger.exception(f"Step {step_id} execution error")
            self._steps.update_step_failed(job_id, step_id)
            return None

    async def _synthesize(
        self, job_id: str, question: str, summaries: list[dict]
    ) -> tuple[str, str]:
        """Generate the final Markdown report.

        Returns:
            Tuple of (merged_report, raw_narrative).
        """
        steps = self._steps.get_steps(job_id)
        completed_steps = [s for s in steps if s.get("status") == "completed"]

        # Phase A: deterministic evidence
        evidence = ReportRenderer.build_evidence(completed_steps)
        # Include result_sample in evidence payload for LLM (spec requirement)
        # Limit sample data sent to LLM to avoid timeout on large evidence payloads
        evidence_text = json.dumps(
            [{"step": e["step_number"], "question": e["question"],
              "columns": e["columns"], "row_count": e["row_count"],
              "column_profile": e.get("column_profile", []),
              "sample_data": e["table_rows"][:5]}  # max 5 rows for narrative (full data in tables)
             for e in evidence],
            ensure_ascii=False,
        )

        # Phase B: LLM narrative
        narrative = await self._llm.generate_narrative(question, summaries, evidence_text)

        # Phase C: merge tables (default mode for Markdown/GDocs)
        report = ReportRenderer.merge(narrative, evidence, job_id=job_id)
        return report, narrative

    def _transition_to(self, job_id: str, target: str) -> bool:
        """Transition job to target status from whatever the current status is.

        Reads actual status first to avoid CAS failures from hardcoded
        'from' states. Returns False if job is terminal or transition fails.
        """
        job = self._jobs.get_job(job_id)
        if not job:
            return False
        current = job["status"]
        if current in ("completed", "failed", "cancelled"):
            return False
        if current == target:
            return True  # already there
        return self._jobs.transition_status(job_id, current, target)

    def _inject_context(self, question: str, prior_summaries: list[dict]) -> str:
        """If prior steps exist, prepend relevant context to the question."""
        if not prior_summaries:
            return question

        context_lines = [
            f"- {s['question']}: {s['summary']}"
            for s in prior_summaries[-3:]  # Last 3 steps max for token economy
        ]
        context = "\n".join(context_lines)
        return f"Context from prior analysis:\n{context}\n\nQuestion: {question}"

    def _check_cancel(self, job_id: str) -> bool:
        """Check if cancellation was requested and handle it.

        Tries to transition to cancelled. If transition fails (race), re-checks
        whether job is already terminal. Returns True if job should stop.
        """
        if not self._jobs.is_cancel_requested(job_id):
            return False

        job = self._jobs.get_job(job_id)
        if not job:
            return True  # Job gone, stop

        current = job["status"]
        if current in ("completed", "failed", "cancelled"):
            return True  # Already terminal

        success = self._jobs.transition_status(job_id, current, "cancelled")
        if success:
            logger.info(f"Job {job_id}: cancelled from {current}")
            return True

        # Transition failed — re-check if someone else moved it to terminal
        job = self._jobs.get_job(job_id)
        if job and job["status"] in ("completed", "failed", "cancelled"):
            return True

        # Non-terminal but transition from re-read status failed (race).
        # Retry cancelled from freshly-read status. Never fall back to failed —
        # DELETE must only result in cancelled. If all retries fail, just stop
        # processing and let orphan recovery handle it.
        for _ in range(3):
            job = self._jobs.get_job(job_id)
            if not job or job["status"] in ("completed", "failed", "cancelled"):
                return True
            if self._jobs.transition_status(job_id, job["status"], "cancelled"):
                logger.info(f"Job {job_id}: cancelled (retry) from {job['status']}")
                return True
        logger.warning(f"Job {job_id}: cancel CAS exhausted, stopping processing (orphan recovery will clean up)")
        return True

    _last_cleanup_at: float = 0  # class-level cooldown tracker

    async def _cleanup_old_files(self):
        """Delete chart/PDF files for jobs older than CLEANUP_RETENTION_DAYS.

        Runs best-effort after job completion with a 1-hour cooldown.
        Failures are logged but never propagated.
        """
        import time as _time
        from datetime import datetime, timedelta

        # Cooldown: skip if ran within the last hour
        now = _time.time()
        if now - ResearchOrchestrator._last_cleanup_at < 3600:
            return
        ResearchOrchestrator._last_cleanup_at = now

        retention_days = Config.CLEANUP_RETENTION_DAYS
        if retention_days <= 0:
            return

        cutoff = datetime.utcnow() - timedelta(days=retention_days)
        cutoff_str = cutoff.strftime("%Y-%m-%d %H:%M:%S")

        try:
            old_job_ids = await asyncio.to_thread(
                self._jobs.get_old_job_ids, cutoff_str
            )
            if not old_job_ids:
                return

            ws = self._genie._ws

            async def _delete(jid: str):
                try:
                    await asyncio.to_thread(
                        ws.files.delete_directory,
                        Config.volume_charts_dir(jid), recursive=True,
                    )
                    return True
                except Exception:
                    return False

            results = await asyncio.gather(*[_delete(jid) for jid in old_job_ids])
            deleted = sum(1 for r in results if r)
            if deleted:
                logger.info(f"Cleanup: deleted {deleted} job dirs older than {retention_days} days")

        except Exception as e:
            logger.debug(f"Cleanup skipped: {e}")
