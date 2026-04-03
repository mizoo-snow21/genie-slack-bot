"""Slack Block Kit builders for research progress display."""


def build_progress_blocks(
    status: str,
    steps: list[dict] | None = None,
    job_id: str = "",
    elapsed_seconds: float = 0,
    error: str | None = None,
    summary: str | None = None,
) -> list[dict]:
    """Build Block Kit blocks for research progress display."""
    if status == "planning":
        return _build_planning_blocks()
    elif status == "running_subquestion":
        return _build_running_blocks(steps or [], job_id, elapsed_seconds)
    elif status == "evaluating":
        return _build_evaluating_blocks(steps or [])
    elif status == "synthesizing":
        return _build_synthesizing_blocks(steps or [], elapsed_seconds)
    elif status == "completed":
        return _build_completed_blocks(elapsed_seconds, summary)
    elif status == "failed":
        return _build_failed_blocks(error)
    elif status == "cancelled":
        return _build_cancelled_blocks()
    else:
        return [_section(f"ステータス: {status}")]


def build_cancel_requested_blocks() -> list[dict]:
    """Build blocks for cancel-in-progress state."""
    return [_section("⏳ キャンセル処理中...")]


# ── Internal builders ────────────────────────────────────────


def _build_planning_blocks() -> list[dict]:
    return [_section("🔬 *リサーチ開始*\n分析計画を作成しています...")]


def _build_running_blocks(steps: list[dict], job_id: str, elapsed: float = 0) -> list[dict]:
    done_count = 0
    total = len(steps)
    current_question = None
    lines = []

    for s in steps:
        st = s.get("status", "pending")
        q = s.get("question", "")
        icon = _step_icon(st)
        if st in ("completed", "failed"):
            done_count += 1
            lines.append(f"{icon} {q}")
        elif st == "running":
            if current_question is None:
                current_question = q
            lines.append(f"{icon} *{q}*")
        else:
            lines.append(f"{icon} {q}")

    # Header: what's happening now
    if current_question:
        if len(current_question) > 60:
            current_question = current_question[:57] + "..."
        header = f"🔬 *調査中:* {current_question}"
    else:
        header = "🔬 *分析中...*"

    time_str = _format_time(elapsed) if elapsed > 0 else ""
    progress_line = f"_{done_count}/{total} 完了"
    if time_str:
        progress_line += f" · {time_str}経過"
    progress_line += "_"

    blocks: list[dict] = [
        _section(f"{header}\n{progress_line}"),
        _section("\n".join(lines)),
    ]

    # Cancel button
    if job_id:
        blocks.append({
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "キャンセル"},
                    "action_id": "cancel_research",
                    "value": job_id,
                    "style": "danger",
                },
            ],
        })

    return blocks


def _build_evaluating_blocks(steps: list[dict]) -> list[dict]:
    completed = _completed_questions(steps)
    text = f"🔬 *{len(completed)}件の分析結果を評価中*\n追加の深堀りが必要か判断しています...\n\n"
    text += "\n".join(_completed_summary_lines(completed))
    return [_section(text)]


def _build_synthesizing_blocks(steps: list[dict], elapsed: float = 0) -> list[dict]:
    completed = _completed_questions(steps)
    time_str = _format_time(elapsed) if elapsed > 0 else ""
    text = f"📝 *レポート作成中* ({len(completed)}件の分析結果を統合しています)\n"
    if time_str:
        text += f"_{time_str}経過_\n"
    text += "\n" + "\n".join(_completed_summary_lines(completed))
    return [_section(text)]


def _build_completed_blocks(elapsed_seconds: float, summary: str | None) -> list[dict]:
    time_str = _format_time(elapsed_seconds)
    blocks: list[dict] = [_section(f"✅ *リサーチ完了* ({time_str})")]
    if summary:
        # Truncate for Slack block limit (3000 chars)
        excerpt = summary[:2900]
        if len(summary) > 2900:
            excerpt += "..."
        blocks.append(_section(excerpt))
    return blocks


def _build_failed_blocks(error: str | None) -> list[dict]:
    blocks: list[dict] = [_section("❌ *分析に失敗しました*")]
    if error:
        blocks.append(_section(f"```{error[:2900]}```"))
    return blocks


def _build_cancelled_blocks() -> list[dict]:
    return [_section("❌ *リサーチをキャンセルしました*")]


# ── Helpers ──────────────────────────────────────────────────


def _completed_questions(steps: list[dict]) -> list[str]:
    """Extract questions from completed steps."""
    return [s.get("question", "") for s in steps if s.get("status") == "completed"]


def _completed_summary_lines(completed: list[str], max_show: int = 4) -> list[str]:
    """Format completed questions as summary lines."""
    lines = [f"✅ {q}" for q in completed[:max_show]]
    if len(completed) > max_show:
        lines.append(f"_...他{len(completed) - max_show}件_")
    return lines


def _section(text: str) -> dict:
    """Build a simple mrkdwn section block."""
    return {
        "type": "section",
        "text": {"type": "mrkdwn", "text": text},
    }


def _step_icon(status: str) -> str:
    """Return an emoji icon for a step status."""
    return {
        "completed": "✅",
        "running": "⏳",
        "failed": "❌",
    }.get(status, "⬚")


def _format_time(seconds: float) -> str:
    """Format seconds as human-readable time."""
    mins = int(seconds) // 60
    secs = int(seconds) % 60
    if mins > 0:
        return f"{mins}分{secs}秒"
    return f"{secs}秒"
