"""Opt-in mounting of the directors' MCP server in the validation backend."""

import os
from typing import Any, Callable


def mount_director_mcp(
    app: Any,
    *,
    server_factory: Callable[..., Any] | None = None,
) -> bool:
    """Mount MCP only when explicitly enabled for the current environment."""
    if os.getenv("MCP_ENABLED", "false").strip().lower() != "true":
        return False

    if server_factory is None:
        from mcp_server import create_server

        server_factory = create_server

    mcp_server = server_factory()
    mcp_app = mcp_server.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        host=os.getenv("MCP_HOST", "0.0.0.0"),
    )

    async def start_mcp_lifespan() -> None:
        context = mcp_app.router.lifespan_context(mcp_app)
        await context.__aenter__()
        app.state.director_mcp_lifespan = context

    async def stop_mcp_lifespan() -> None:
        context = getattr(app.state, "director_mcp_lifespan", None)
        if context is not None:
            await context.__aexit__(None, None, None)
            app.state.director_mcp_lifespan = None

    app.router.add_event_handler("startup", start_mcp_lifespan)
    app.router.add_event_handler("shutdown", stop_mcp_lifespan)
    # Mounted last so the API's existing routes keep precedence.
    app.mount("", mcp_app, name="director-mcp")
    app.state.director_mcp_enabled = True
    return True
