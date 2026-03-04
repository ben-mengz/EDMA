import json
import time
import threading
import asyncio
from collections import deque
from dataclasses import dataclass, asdict
from typing import Any, Deque, Dict, List, Optional
from pydantic import BaseModel
from fastmcp import Context
from edma_mcp.server.base import BaseMCP


@dataclass(frozen=True)
class _EventItem:
    cursor: int
    ts: float
    agent: str
    event_type: str
    payload: Dict[str, Any]
    targets: Optional[List[str]]


class _EventBus:
    def __init__(self, maxlen: int = 5000) -> None:
        self._lock = threading.RLock()
        self._events: Deque[_EventItem] = deque(maxlen=maxlen)
        self._cursor = 0

    def append(
        self,
        agent: str,
        event_type: str,
        payload: Dict[str, Any],
        targets: Optional[List[str]] = None,
    ) -> _EventItem:
        with self._lock:
            self._cursor += 1
            item = _EventItem(
                cursor=self._cursor,
                ts=time.time(),
                agent=agent,
                event_type=event_type,
                payload=payload,
                targets=targets,
            )
            self._events.append(item)
            return item

    def read_after(self, after: int, scope: Optional[str] = None) -> Dict[str, Any]:
        with self._lock:
            newest = self._cursor
            if scope:
                events = [asdict(e) for e in self._events if e.cursor > after and e.agent == scope]
            else:
                events = [asdict(e) for e in self._events if e.cursor > after]
            return {"next_cursor": newest, "events": events}


class EventPushArgs(BaseModel):
    agent: str
    event_type: str
    payload: Dict[str, Any] = {}
    targets: Optional[List[str]] = None


class EventReadArgs(BaseModel):
    after: int = 0


class EventMCP(BaseMCP):
    """
    A single internal MCP instance that stores and broadcasts events.
    Intended to be used by the client-plugin/dispatcher.
    """

    def __init__(self, max_events: int = 5000) -> None:
        self._resource_uri = "events://hub/all"
        super().__init__(
            name="event-hub",
            introduction="Internal event hub MCP",
            prompt="",
            model="none",
        )
        
        self._bus = _EventBus(maxlen=max_events)
        self._session_lock = threading.RLock()
        self._sessions: set[Any] = set()
        self._event_mcp = self
        self._set_up_resources()
        self._set_up_tools()

    def _set_up_resources(self) -> None:
        @self.mcp.resource(f"{self._resource_uri}/{{scope}}/{{after}}")
        def read_all_events(scope: str, after: str) -> str:
            """
            Resource returns JSON:
              - next_cursor: int
              - events: list[{cursor, ts, agent, event_type, payload, targets}]
            """
            try:
                after_i = int(after)
            except Exception:
                after_i = 0

            data = self._bus.read_after(after=after_i, scope=None)
            return json.dumps(data, ensure_ascii=False)

    def _set_up_tools(self) -> None:
        @self.mcp.tool()
        async def events_push(
            ctx: Context,
            agent: str,
            event_type: str,
            payload: Dict[str, Any] = {},
            targets: Optional[List[str]] = None
        ) -> str:
            """
            Push an event to the hub and notify subscribers that the resource updated.
            """
            item = self._bus.append(
                agent=agent,
                event_type=event_type,
                payload=payload,
                targets=targets,
            )

            await self._notify_resource_updated(ctx, self._resource_uri)
            return json.dumps({"ok": True, "cursor": item.cursor}, ensure_ascii=False)

        @self.mcp.tool()
        async def events_subscribe(ctx: Context) -> str:
            session = getattr(ctx, "session", None)
            if session is not None:
                with self._session_lock:
                    self._sessions.add(session)
                return json.dumps({"ok": True}, ensure_ascii=False)
            return json.dumps({"ok": False, "reason": "no session on ctx"}, ensure_ascii=False)

    async def _notify_resource_updated(self, ctx: Context, uri: str) -> None:
        """
        Best-effort notification across fastmcp variants.
        """
        session = getattr(ctx, "session", None)
        if session is not None:
            for method_name in ("send_resource_updated", "notify_resource_updated", "resource_updated"):
                method = getattr(session, method_name, None)
                if callable(method):
                    try:
                        result = method(uri)
                        if hasattr(result, "__await__"):
                            await result
                        return
                    except Exception:
                        pass

        send_notification = getattr(ctx, "send_notification", None)
        if callable(send_notification):
            try:
                ResourceUpdatedNotification = None
                try:
                    from mcp.types import ResourceUpdatedNotification as _R  # type: ignore
                    ResourceUpdatedNotification = _R
                except Exception:
                    pass

                if ResourceUpdatedNotification is None:
                    try:
                        from fastmcp.types import ResourceUpdatedNotification as _R  # type: ignore
                        ResourceUpdatedNotification = _R
                    except Exception:
                        pass

                if ResourceUpdatedNotification is not None:
                    notif = ResourceUpdatedNotification(uri=uri)
                    result = send_notification(notif)
                    if hasattr(result, "__await__"):
                        await result
                    return
            except Exception:
                pass

    async def notify_subscribers(self, uri: str) -> None:
        """
        Notify all subscribed sessions that `uri` has updated.
        """
        with self._session_lock:
            sessions = list(self._sessions)

        dead = []
        for session in sessions:
            ok = False
            for method_name in ("send_resource_updated", "notify_resource_updated", "resource_updated"):
                method = getattr(session, method_name, None)
                if callable(method):
                    try:
                        r = method(uri)
                        if hasattr(r, "__await__"):
                            await r
                        ok = True
                        break
                    except Exception:
                        continue
            if not ok:
                dead.append(session)

        if dead:
            with self._session_lock:
                for s in dead:
                    self._sessions.discard(s)

    def append_local(
        self,
        agent: str,
        event_type: str,
        payload: Dict[str, Any],
        targets: Optional[List[str]] = None
    ) -> int:
        item = self._bus.append(agent=agent, event_type=event_type, payload=payload, targets=targets)
        return item.cursor

    async def push_event(
        self,
        *,
        event_type: str,
        payload: Dict[str, Any],
        agent: Optional[str] = None,
        targets: Optional[List[str]] = None,
    ) -> None:
        self.append_local(
            agent=self.name,
            event_type=event_type,
            payload=payload,
            targets=targets,
        )
        notify_uri = f"{self._resource_uri}/all/0"
        
        await asyncio.wait_for(
            self.notify_subscribers(notify_uri),
            timeout=3.0,
        )
