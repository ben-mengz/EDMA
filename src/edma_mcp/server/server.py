from __future__ import annotations
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route, Mount
from contextlib import asynccontextmanager
from typing import List
import uvicorn
import logging

from edma_mcp.server.base import BaseMCP
from edma_mcp.server.event import EventMCP

logging.getLogger("mcp").setLevel(logging.WARNING)
logging.getLogger("fastmcp").setLevel(logging.WARNING)


def build_multiagent_asgi(agents_list: List[BaseMCP], event_hub: EventMCP):
    """(Internal) Build the final ASGI app connecting all agents and event hub."""
    event_app = getattr(event_hub.mcp, "http_app")(path="/")
    routes = [Mount("/event_hub", app=event_app)]
    lifespans = [getattr(event_app, "lifespan", None)]
    
    agent_names = [agent.name for agent in agents_list]

    async def list_agents(request):
        return JSONResponse(agent_names)

    routes.append(Route("/agents", list_agents, methods=["GET"]))
    
    for agent in agents_list:
        app = getattr(agent.mcp, "http_app")(path="/")
        routes.append(Mount(f"/mcp/{agent.name}", app=app))
        lifespans.append(getattr(app, "lifespan", None))
        
    return Starlette(routes=routes, lifespan=lambda app: merged_lifespan(app, lifespans))


@asynccontextmanager
async def merged_lifespan(app, lifespans):
    """Manage lifespans for multiple ASGI apps together."""
    exits = []
    try:
        for cm in lifespans:
            if cm is None:
                continue
            ctx = cm(app)
            await ctx.__aenter__()
            exits.append(ctx)
        yield
    finally:
        for ctx in reversed(exits):
            await ctx.__aexit__(None, None, None)


class EDMAServer:
    """
    Standard Server orchestrator.
    Users instantiate this, add their base agents, and call run().
    """
    def __init__(self, host: str = "127.0.0.1", port: int = 7300):
        self.host = host
        self.port = port
        self.event_hub = EventMCP()
        self.agents: List[BaseMCP] = []

    def add_agent(self, agent: BaseMCP) -> None:
        """Register an initialized agent into the server. If agent does not have an EventHub attached, warn/attach."""
        if agent._event_mcp != self.event_hub:
            print(f"Note: Setting Server's Global EventHub to Agent '{agent.name}'")
            agent._event_mcp = self.event_hub
        self.agents.append(agent)

    def run(self):
        print(f"[EDMAServer] Starting Multi-agent Event-Driven MCP server on {self.host}:{self.port} ...")
        asgi_app = build_multiagent_asgi(self.agents, self.event_hub)
        config = uvicorn.Config(
            app=asgi_app,
            host=self.host,
            port=self.port,
            log_level="info",
            # Enable auto-reload if needed using reload=True passed through config
        )
        server = uvicorn.Server(config)
        try:
            server.run()
        except KeyboardInterrupt:
            print("\n[EDMAServer] Server stopped.")
