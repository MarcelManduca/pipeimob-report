import asyncio
from contextlib import asynccontextmanager
from threading import Thread

from fastapi import FastAPI
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from services.director_mcp_mount import mount_director_mcp


class FakeServer:
    def __init__(self):
        self.started = False
        self.stopped = False

    def streamable_http_app(self, **kwargs):
        assert kwargs == {
            "streamable_http_path": "/mcp",
            "stateless_http": True,
            "json_response": True,
            "host": "0.0.0.0",
        }

        async def endpoint(request):
            return PlainTextResponse("mcp")

        server = self

        @asynccontextmanager
        async def lifespan(app):
            server.started = True
            yield
            server.stopped = True

        return Starlette(routes=[Route("/mcp", endpoint)], lifespan=lifespan)


def test_mcp_mount_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv("MCP_ENABLED", raising=False)
    app = FastAPI()

    assert mount_director_mcp(app, server_factory=FakeServer) is False
    assert not hasattr(app.state, "director_mcp_enabled")


def test_mcp_mount_is_opt_in_and_manages_subapp_lifespan(monkeypatch):
    monkeypatch.setenv("MCP_ENABLED", "true")
    app = FastAPI()
    fake = FakeServer()

    assert mount_director_mcp(app, server_factory=lambda: fake) is True
    assert app.state.director_mcp_enabled is True
    assert any(getattr(route, "name", None) == "director-mcp" for route in app.routes)

    async def exercise_lifespan():
        async with app.router.lifespan_context(app):
            assert fake.started is True
            assert fake.stopped is False
        assert fake.stopped is True

    errors = []

    def run_in_isolated_thread():
        try:
            asyncio.run(exercise_lifespan())
        except BaseException as exc:  # pragma: no cover - re-raised in test thread
            errors.append(exc)

    thread = Thread(target=run_in_isolated_thread)
    thread.start()
    thread.join()
    if errors:
        raise errors[0]
