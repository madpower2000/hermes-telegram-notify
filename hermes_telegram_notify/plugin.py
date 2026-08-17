"""Hermes plugin registration surface."""

from __future__ import annotations

from .cli import handle_cli, setup_cli
from .hooks import (
    on_post_approval_response,
    on_pre_approval_request,
    on_pre_llm_call,
    on_session_end,
)


def register(ctx) -> None:
    """Register lifecycle observers and the management CLI command."""
    ctx.register_hook("pre_llm_call", on_pre_llm_call)
    ctx.register_hook("on_session_end", on_session_end)
    ctx.register_hook("pre_approval_request", on_pre_approval_request)
    ctx.register_hook("post_approval_response", on_post_approval_response)
    ctx.register_cli_command(
        name="telegram-notify",
        help="Configure Hermes Telegram notifications",
        setup_fn=setup_cli,
        handler_fn=handle_cli,
        description="Configure and test the telegram-notify Hermes plugin.",
    )
