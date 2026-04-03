"""Slack Block Kit builders for mode selection."""


def build_mode_selection_blocks(question: str) -> list[dict]:
    """Build mode selection message blocks with Quick Answer and Research buttons."""
    truncated_q = question[:1900]  # Slack button value limit is 2000 chars
    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "どのように調べますか？"},
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "⚡ 即答"},
                    "action_id": "mode_quick",
                    "value": truncated_q,
                    "style": "primary",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "🔬 詳しく分析"},
                    "action_id": "mode_research",
                    "value": truncated_q,
                },
            ],
        },
    ]


def build_research_upgrade_blocks(question: str) -> list[dict]:
    """Build single button to upgrade quick-answer to research."""
    truncated_q = question[:1900]
    return [
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "🔬 詳しく分析する"},
                    "action_id": "mode_research",
                    "value": truncated_q,
                },
            ],
        },
    ]
