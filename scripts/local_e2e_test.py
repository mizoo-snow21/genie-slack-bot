"""
Local E2E test for genie-slack-bot.
Tests all flows without Slack dependency.

Usage:
    cd src/app
    ENABLE_RESEARCH=true RESEARCH_CATALOG=ymizoguchi_demo \
    DATABRICKS_GENIE_SPACE_ID=01f12bf647141af587a4748293041a26 \
    LLM_ENDPOINT=databricks-gpt-5-4 \
    LLM_CHART_ENDPOINT=databricks-gpt-5-4-mini \
    LLM_NARRATIVE_ENDPOINT=databricks-claude-opus-4-6 \
    uv run python ../../scripts/local_e2e_test.py --profile=e2-demo-tokyo
"""
import asyncio
import argparse
import sys
import os
import time
import traceback

# Ensure src/app is on path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "app"))

# Set dummy Slack tokens for local testing (not used, just to pass validation)
for key in ("SLACK_BOT_TOKEN", "SLACK_SIGNING_SECRET", "SLACK_APP_TOKEN"):
    if not os.environ.get(key):
        os.environ[key] = "local-test-dummy"


class TestResult:
    def __init__(self):
        self.passed = []
        self.failed = []

    def ok(self, name, detail=""):
        self.passed.append(name)
        print(f"  ✅ {name}" + (f" — {detail}" if detail else ""))

    def fail(self, name, error):
        self.failed.append((name, error))
        print(f"  ❌ {name} — {error}")

    def summary(self):
        total = len(self.passed) + len(self.failed)
        print(f"\n{'='*60}")
        print(f"Results: {len(self.passed)}/{total} passed")
        if self.failed:
            print("\nFailed tests:")
            for name, error in self.failed:
                print(f"  ❌ {name}: {error}")
        print(f"{'='*60}")
        return len(self.failed) == 0


async def run_tests(profile: str):
    result = TestResult()

    # ============================================================
    # 1. Config
    # ============================================================
    print("\n[1] Config")
    try:
        from config import Config
        Config.validate()
        result.ok("Config.validate()", f"ENABLE_RESEARCH={Config.ENABLE_RESEARCH}")
    except Exception as e:
        result.fail("Config.validate()", str(e))
        return result

    # ============================================================
    # 2. Import chain (ENABLE_RESEARCH=true)
    # ============================================================
    print("\n[2] Import chain")
    try:
        from infra.genie_client import GenieClient
        result.ok("import GenieClient")
    except Exception as e:
        result.fail("import GenieClient", str(e))

    try:
        from presentation.chart_generator import ChartGenerator
        result.ok("import ChartGenerator")
    except Exception as e:
        result.fail("import ChartGenerator", str(e))

    try:
        from infra.llm_client import LLMClient
        from infra.job_store import JobStore
        from infra.step_store import StepStore
        from infra.init_tables import init_tables
        from presentation.chart_generator import ChartGenerator
        from presentation.pdf_renderer import PdfRenderer
        from domain.orchestrator import ResearchOrchestrator
        result.ok("import all research modules")
    except Exception as e:
        result.fail("import research modules", str(e))

    try:
        from presentation.slack_handler import SlackHandler
        from presentation.mode_selector import build_mode_selection_blocks
        from presentation.progress_view import build_progress_blocks
        result.ok("import slack_handler + UI components")
    except Exception as e:
        result.fail("import slack_handler", str(e))

    # ============================================================
    # 3. WorkspaceClient + GenieClient
    # ============================================================
    print("\n[3] Databricks connection")
    try:
        from databricks.sdk import WorkspaceClient
        ws = WorkspaceClient(profile=profile)
        genie = GenieClient(ws, Config.DATABRICKS_GENIE_SPACE_ID)
        result.ok("WorkspaceClient + GenieClient created")
    except Exception as e:
        result.fail("WorkspaceClient creation", str(e))
        return result

    # ============================================================
    # 4. Genie API — quick answer
    # ============================================================
    print("\n[4] Genie API — quick answer")
    try:
        qa_result = genie.ask_question("売上TOP3を教えて")
        if qa_result.get("success"):
            resp = qa_result.get("response", "")[:100]
            result.ok("ask_question()", f"response={resp}...")
        else:
            result.fail("ask_question()", qa_result.get("error", "unknown"))
    except Exception as e:
        result.fail("ask_question()", traceback.format_exc())

    # ============================================================
    # 5. Quick-answer chart generation
    # ============================================================
    print("\n[5] Quick-answer chart")
    try:
        chart_gen = ChartGenerator(llm_client=LLMClient(ws), ws=ws)
        if qa_result.get("success") and qa_result.get("result_data"):
            rd = qa_result["result_data"]
            data_array = rd.get("data", {}).get("data_array", [])
            columns = rd.get("schema", {}).get("columns", [])
            if data_array and columns:
                col_names = [c["name"] for c in columns]
                png = chart_gen.generate_bytes(
                    column_names=col_names,
                    data_array=data_array,
                    column_types=columns,
                    user_question="売上TOP3",
                )
                if png:
                    result.ok("generate_bytes()", f"{len(png)} bytes PNG")
                else:
                    result.ok("generate_bytes()", "returned None (LLM decided no chart)")
            else:
                result.ok("generate_bytes()", "skipped — no data in response")
        else:
            result.ok("generate_bytes()", "skipped — quick answer failed")
    except Exception as e:
        result.fail("generate_bytes()", traceback.format_exc())

    # ============================================================
    # 6. Genie API — schema fetch (async, used by research)
    # ============================================================
    print("\n[6] Genie schema fetch")
    try:
        schema = await genie.get_schema()
        if schema:
            result.ok("get_schema()", f"{len(schema)} chars")
        else:
            result.fail("get_schema()", "returned empty string")
    except Exception as e:
        result.fail("get_schema()", traceback.format_exc())

    # ============================================================
    # 7. Init tables
    # ============================================================
    print("\n[7] Delta tables init")
    try:
        init_tables(ws)
        result.ok("init_tables()", "tables created/verified")
    except Exception as e:
        result.fail("init_tables()", traceback.format_exc())

    # ============================================================
    # 8. LLM client — plan generation
    # ============================================================
    print("\n[8] LLM — plan generation")
    try:
        llm = LLMClient(ws)
        plan = await llm.generate_plan("売上の地域別傾向を分析して", schema_info=schema or "")
        if plan and len(plan) > 0:
            result.ok("generate_plan()", f"{len(plan)} sub-questions: {plan}")
        else:
            result.fail("generate_plan()", "returned empty plan")
    except Exception as e:
        result.fail("generate_plan()", traceback.format_exc())

    # ============================================================
    # 9. Full research pipeline (orchestrator.run)
    # ============================================================
    print("\n[9] Full research pipeline")
    try:
        job_store = JobStore(ws)
        step_store = StepStore(ws)
        chart_gen = ChartGenerator(llm_client=llm, ws=ws)
        orchestrator = ResearchOrchestrator(
            job_store=job_store,
            step_store=step_store,
            genie=genie,
            llm=llm,
            chart_generator=chart_gen,
        )

        job_id = job_store.create_job(
            Config.DATABRICKS_GENIE_SPACE_ID,
            "売上の地域別傾向を分析して",
            {
                "max_steps": 4,
                "max_duration_seconds": 300,
                "max_result_rows_per_query": 100,
                "enable_charts": True,
            },
        )
        result.ok("create_job()", f"job_id={job_id}")

        # queued → planning
        job_store.transition_status(job_id, "queued", "planning")
        result.ok("transition queued→planning")

        start = time.time()
        await orchestrator.run(job_id)
        elapsed = int(time.time() - start)

        job = job_store.get_job(job_id)
        status = job.get("status", "unknown")
        if status == "completed":
            result.ok("orchestrator.run()", f"status=completed, elapsed={elapsed}s")
        else:
            result.fail("orchestrator.run()", f"status={status}, error={job.get('error', '')}")
    except Exception as e:
        result.fail("orchestrator.run()", traceback.format_exc())

    # ============================================================
    # 10. Report retrieval
    # ============================================================
    print("\n[10] Report retrieval")
    try:
        report = step_store.get_report(job_id)
        if report:
            result.ok("get_report()", f"{len(report)} chars")
        else:
            result.fail("get_report()", "no report found")

        report_record = step_store.get_report_record(job_id)
        if report_record and report_record.get("report_narrative"):
            result.ok("get_report_record()", "narrative present")
        else:
            result.fail("get_report_record()", "no narrative")
    except Exception as e:
        result.fail("get_report_record()", traceback.format_exc())

    # ============================================================
    # 11. PDF generation
    # ============================================================
    print("\n[11] PDF generation")
    pdf_renderer = None
    try:
        from domain.report_renderer import ReportRenderer

        steps = step_store.get_steps(job_id)
        completed_steps = [s for s in steps if s.get("status") == "completed"]

        # Collect chart images
        chart_images = {}
        for s in completed_steps:
            vol_path = s.get("chart_volume_path")
            if vol_path:
                try:
                    resp = ws.files.download(vol_path)
                    chart_images[s["step_id"]] = resp.contents.read()
                except Exception as e:
                    print(f"    (chart download skipped: {e})")

        evidence = ReportRenderer.build_evidence(completed_steps)
        result.ok("build_evidence()", f"{len(evidence)} evidence items")

        narrative = report_record.get("report_narrative", "")
        pdf_report = ReportRenderer.merge(narrative, evidence, job_id=job_id, for_pdf=True)
        result.ok("merge(for_pdf=True)", f"{len(pdf_report)} chars")

        pdf_renderer = PdfRenderer()
        pdf_bytes = pdf_renderer.render(pdf_report, job_id, chart_images)
        if pdf_bytes and len(pdf_bytes) > 1000:
            result.ok("PdfRenderer.render()", f"{len(pdf_bytes)} bytes PDF")
        else:
            result.fail("PdfRenderer.render()", f"PDF too small: {len(pdf_bytes) if pdf_bytes else 0} bytes")
    except Exception as e:
        result.fail("PDF generation", traceback.format_exc())

    # ============================================================
    # 12. UI components (Block Kit)
    # ============================================================
    print("\n[12] UI components")
    try:
        blocks = build_mode_selection_blocks("テスト質問")
        assert len(blocks) == 2
        assert blocks[1]["type"] == "actions"
        assert len(blocks[1]["elements"]) == 2
        assert blocks[1]["elements"][0]["action_id"] == "mode_quick"
        assert blocks[1]["elements"][1]["action_id"] == "mode_research"
        result.ok("build_mode_selection_blocks()")

        prog_blocks = build_progress_blocks(
            status="running_subquestion",
            steps=[
                {"question": "地域別売上", "status": "completed"},
                {"question": "カテゴリ別", "status": "running"},
                {"question": "前年比較", "status": "pending"},
            ],
            job_id="test-123",
            elapsed_seconds=60,
        )
        assert any("✅" in str(b) for b in prog_blocks)
        assert any("⏳" in str(b) for b in prog_blocks)
        result.ok("build_progress_blocks(running)")

        done_blocks = build_progress_blocks(
            status="completed", steps=[], job_id="test-123",
            elapsed_seconds=260, summary="売上は前年比+12%で成長。")
        assert any("リサーチ完了" in str(b) for b in done_blocks)
        result.ok("build_progress_blocks(completed)")

        cancel_blocks = build_progress_blocks(
            status="cancelled", steps=[], job_id="test-123")
        assert any("キャンセル" in str(b) for b in cancel_blocks)
        result.ok("build_progress_blocks(cancelled)")
    except Exception as e:
        result.fail("UI components", traceback.format_exc())

    # ============================================================
    # 13. SlackHandler instantiation (no Slack connection)
    # ============================================================
    print("\n[13] SlackHandler instantiation")
    try:
        handler = SlackHandler(
            slack_bot_token="xoxb-test",
            slack_signing_secret="test",
            slack_app_token="xapp-test",
            genie_client=genie,
            chart_generator=chart_gen,
            orchestrator=orchestrator,
            job_store=job_store,
            step_store=step_store,
            pdf_renderer=pdf_renderer,
        )
        assert handler._research_enabled is True
        result.ok("SlackHandler(research_enabled=True)")

        handler_no_research = SlackHandler(
            slack_bot_token="xoxb-test",
            slack_signing_secret="test",
            slack_app_token="xapp-test",
            genie_client=genie,
            chart_generator=chart_gen,
        )
        assert handler_no_research._research_enabled is False
        result.ok("SlackHandler(research_enabled=False)")
    except Exception as e:
        result.fail("SlackHandler instantiation", traceback.format_exc())

    return result


def main():
    parser = argparse.ArgumentParser(description="Local E2E test")
    parser.add_argument("--profile", default="e2-demo-tokyo", help="Databricks CLI profile")
    args = parser.parse_args()

    print("=" * 60)
    print("Genie Slack Bot — Local E2E Test")
    print("=" * 60)

    os.chdir(os.path.join(os.path.dirname(__file__), "..", "src", "app"))

    result = asyncio.run(run_tests(args.profile))
    success = result.summary()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
