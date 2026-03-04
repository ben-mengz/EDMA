from __future__ import annotations
from typing import Any, Callable, Dict, List, Optional
from fastmcp import FastMCP
import asyncio
import inspect


def mcp_tool(name: Optional[str] = None, description: Optional[str] = None):
    """Decorator to mark a class method as an MCP tool."""
    def decorator(func: Callable):
        func._is_mcp_tool = True
        func._mcp_name = name or func.__name__
        func._mcp_desc = description or func.__doc__
        return func
    return decorator


def mcp_resource(uri: str):
    """Decorator to mark a class method as an MCP resource."""
    def decorator(func: Callable):
        func._is_mcp_resource = True
        func._mcp_uri = uri
        return func
    return decorator


class BaseMCP:
    def __init__(self, name: str, introduction: str, prompt: str, model: str, event_mcp: Optional[Any] = None) -> None:
        self.name = name
        self.introduction = introduction
        self.prompt = prompt
        self.model = model
        self.mcp = FastMCP(f"mcp-agent-{self.name}")
        self._event_mcp = event_mcp
        
        if self._event_mcp is None:
            print(f"Warning: no event mcp configured for {self.name}; event-driven triage will be disabled")
            
        @self.mcp.resource(f"introduction://{self.name}")
        def agent_introduction() -> str:
            return self.introduction
            
        @self.mcp.resource(f"prompt://{self.name}")
        def agent_prompt() -> str:
            return self.prompt
            
        @self.mcp.resource(f"model://{self.name}")
        def agent_model() -> str:
            return self.model

        self._auto_register()

    def _auto_register(self):
        """Automatically scan and register methods decorated with @mcp_tool or @mcp_resource."""
        for name in dir(self):
            # Skip built-ins and dunder methods heavily to avoid unneeded getattr
            if name.startswith("__"):
                continue
            
            attr = getattr(self, name)
            if not callable(attr):
                continue
            
            if getattr(attr, "_is_mcp_tool", False):
                t_name = getattr(attr, "_mcp_name")
                t_desc = getattr(attr, "_mcp_desc")
                # fastmcp uses decorator natively. So we just pass the bound method
                if t_desc:
                    self.mcp.tool(name=t_name, description=t_desc)(attr)
                else:
                    self.mcp.tool(name=t_name)(attr)

            if getattr(attr, "_is_mcp_resource", False):
                t_uri = getattr(attr, "_mcp_uri")
                self.mcp.resource(t_uri)(attr)

    @property
    def mcp_app(self) -> Any:
        return self.mcp.http_app(path="/")
    
    async def push_event(
        self,
        *,
        event_type: str,
        payload: Dict[str, Any],
        targets: Optional[List[str]] = None,
    ) -> None:
        if self._event_mcp is None:
            return
            
        getattr(self._event_mcp, "append_local")(
            agent=self.name,
            event_type=event_type,
            payload=payload,
            targets=targets,
        )
        
        notify_uri = f"{getattr(self._event_mcp, '_resource_uri')}/all/0"
        try:
            await asyncio.wait_for(
                getattr(self._event_mcp, "notify_subscribers")(notify_uri),
                timeout=3.0,
            )
        except Exception as e:
            print(f"Error notifying subscribers: {e}")

