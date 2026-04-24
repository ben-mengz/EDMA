from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
import httpx
from fastmcp.client import Client as FastMCPClient


@dataclass(frozen=True)
class AgentsDiscoveryResult:
    ok: bool
    agents: List[str]
    message: str


@dataclass(frozen=True)
class AgentEndpoint:
    name: str
    mcp_base_url: str


class _FastMCPAgentBridge:
    def __init__(self, endpoint: AgentEndpoint) -> None:
        self._endpoint = endpoint
        self._client = FastMCPClient(endpoint.mcp_base_url)

    @property
    def name(self) -> str:
        return self._endpoint.name

    async def list_tools(self) -> Any:
        async with self._client:
            if hasattr(self._client, "list_tools"):
                return await self._client.list_tools()
            if hasattr(self._client, "list_tools_mcp"):
                return await self._client.list_tools_mcp()
            raise AttributeError("FastMCP client has no list_tools method")

    async def call_tool(self, tool_name: str, arguments: Optional[Dict[str, Any]] = None) -> Any:
        async with self._client:
            return await self._client.call_tool(tool_name, arguments or {})

    async def list_resources(self) -> Any:
        async with self._client:
            return await self._client.list_resources()

    async def read_resource(self, uri: str) -> Any:
        async with self._client:
            return await self._client.read_resource(uri)


class MCPBridgeManager:
    def __init__(
        self,
        base_http_url: str,
        mcp_prefix: str = "/mcp",
        agents_path: str = "/agents",
        timeout_sec: float = 2.0,
        exclude_agents: Optional[List[str]] = None,
    ) -> None:
        self._base_http_url = base_http_url.rstrip("/")
        self._mcp_prefix = mcp_prefix.rstrip("/")
        self._agents_path = agents_path if agents_path.startswith("/") else f"/{agents_path}"
        self._timeout_sec = timeout_sec
        self._exclude = set(exclude_agents or [])

        self._agent_names: List[str] = []
        self._bridges: Dict[str, _FastMCPAgentBridge] = {}

    def discover_agents(self) -> AgentsDiscoveryResult:
        url = f"{self._base_http_url}{self._agents_path}"
        try:
            r = httpx.get(url, timeout=self._timeout_sec)
        except Exception as e:
            return AgentsDiscoveryResult(ok=False, agents=[], message=f"No MCP response: {e}")

        if r.status_code != 200:
            return AgentsDiscoveryResult(ok=False, agents=[], message=f"MCP Error: Status {r.status_code}")

        try:
            data = r.json()
        except Exception:
            return AgentsDiscoveryResult(ok=False, agents=[], message="MCP response is not JSON")

        if not isinstance(data, list):
            return AgentsDiscoveryResult(ok=False, agents=[], message="MCP response format is expected to be a list")

        agents = [str(x) for x in data if str(x).strip()]
        agents = [a for a in agents if a not in self._exclude]

        if not agents:
            return AgentsDiscoveryResult(ok=False, agents=[], message="MCP has started but no agents available")

        return AgentsDiscoveryResult(ok=True, agents=agents, message="ok")

    def list_agents_sync(self) -> List[str]:
        if self._agent_names:
            return list(self._agent_names)
        result = self.discover_agents()
        if result.ok:
            self._agent_names = list(result.agents)
            self._ensure_bridges()
        return list(self._agent_names)

    async def refresh(self) -> AgentsDiscoveryResult:
        result = self.discover_agents()
        if result.ok:
            self._agent_names = list(result.agents)
            self._ensure_bridges()
        else:
            self._agent_names = []
            self._bridges = {}
        return result

    async def list_agents(self) -> List[str]:
        if not self._agent_names:
            await self.refresh()
        return list(self._agent_names)

    async def list_tools(self, agent_name: str) -> List[Dict[str, Any]]:
        bridge = await self._get_bridge(agent_name)
        return await bridge.list_tools()

    async def call_tool(self, agent_name: str, tool_name: str, arguments: Optional[Dict[str, Any]] = None) -> Any:
        bridge = await self._get_bridge(agent_name)
        return await bridge.call_tool(tool_name, arguments or {})

    async def list_resources(self, agent_name: str) -> List[Dict[str, Any]]:
        bridge = await self._get_bridge(agent_name)
        return await bridge.list_resources()

    async def read_resource(self, agent_name: str, uri: str) -> Any:
        bridge = await self._get_bridge(agent_name)
        return await bridge.read_resource(uri)

    def _ensure_bridges(self) -> None:
        for name in self._agent_names:
            if name in self._bridges:
                continue
            endpoint = self._build_endpoint(name)
            self._bridges[name] = _FastMCPAgentBridge(endpoint=endpoint)

        for existing in list(self._bridges.keys()):
            if existing not in self._agent_names:
                del self._bridges[existing]

    def _build_endpoint(self, agent_name: str) -> AgentEndpoint:
        url = f"{self._base_http_url}{self._mcp_prefix}/{agent_name}/"
        return AgentEndpoint(name=agent_name, mcp_base_url=url)

    async def _get_bridge(self, agent_name: str) -> _FastMCPAgentBridge:
        if agent_name in self._exclude:
            raise RuntimeError(f"Agent '{agent_name}' is excluded.")

        if agent_name not in self._bridges:
            await self.refresh()

        bridge = self._bridges.get(agent_name)
        if bridge is None:
            if not self._agent_names:
                raise RuntimeError("No agents are available")
            raise RuntimeError(f"Agent not found: {agent_name}")
        return bridge
