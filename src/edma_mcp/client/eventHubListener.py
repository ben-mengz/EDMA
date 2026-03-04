import asyncio
import json
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Set

from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client


class EventDispatcher:
    """
    A registry for routing events based on their 'targets' or 'event_type'.
    Helps separate UI logic from the EventHubListener.
    """
    def __init__(self):
        self._handlers: Dict[str, Callable[[Dict[str, Any]], None]] = {}
        self._default_handler: Optional[Callable[[Dict[str, Any]], None]] = None

    def on(self, target: str):
        """Decorator to register a function for a specific target string."""
        def decorator(func: Callable[[Dict[str, Any]], None]):
            self._handlers[target] = func
            return func
        return decorator

    def set_default(self, func: Callable[[Dict[str, Any]], None]):
        """Set a fallback handler for events with no matching target."""
        self._default_handler = func
        return func

    def dispatch(self, event: Dict[str, Any]) -> None:
        """The main callback to pass to EventHubListener."""
        targets = event.get("targets", [])
        
        # If the event specifies targets, route to them
        handled = False
        if isinstance(targets, list):
            for target in targets:
                handler = self._handlers.get(target)
                if handler:
                    try:
                        handler(event)
                        handled = True
                    except Exception as e:
                        print(f"[Dispatcher] Error in handler for '{target}': {e}")
        
        # Fallback to default handler if no target matched or targets is empty
        if not handled and self._default_handler:
            try:
                self._default_handler(event)
            except Exception as e:
                print(f"[Dispatcher] Error in default handler: {e}")


def _extract_read_write(transport: Any):
    """Extract (read, write) from the transport returned by streamable_http_client."""
    if isinstance(transport, tuple):
        if len(transport) < 2:
            raise ValueError(f"Unexpected transport tuple length: {len(transport)}")
        return transport[0], transport[1]
    read = getattr(transport, "read", None)
    write = getattr(transport, "write", None)
    if read is None or write is None:
        raise TypeError(f"Unsupported transport type: {type(transport)}")
    return read, write


@dataclass(frozen=True)
class EventHubConfig:
    base_url: str                 
    resource_base: str            
    scope: str                    
    poll_interval_sec: float = 0.3
    max_events_per_pull: int = 200
    backoff_max_sec: float = 2.0


class EventHubListener:
    """
    Background event listener for MCP EventHub (Streamable HTTP).
    Runs in a dedicated thread with its own asyncio loop.
    """

    def __init__(
        self,
        config: EventHubConfig,
        dispatcher: EventDispatcher,
        start_cursor: int = 0,
    ) -> None:
        self._cfg = config
        self._dispatcher = dispatcher

        self._after_cursor: int = start_cursor
        self._seen: Set[str] = set()

        self._thread: Optional[threading.Thread] = None
        self._stop_evt = threading.Event()

        self._loop: Optional[asyncio.AbstractEventLoop] = None

    @property
    def after_cursor(self) -> int:
        return self._after_cursor

    def start(self) -> None:
        """Start the listener thread."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_evt.clear()
        self._thread = threading.Thread(target=self._thread_entry, daemon=True)
        self._thread.start()

    def stop(self, timeout_sec: float = 2.0) -> None:
        """Request stop and join the listener thread."""
        self._stop_evt.set()
        if self._loop is not None:
            try:
                self._loop.call_soon_threadsafe(self._loop.stop)
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=timeout_sec)

    def _thread_entry(self) -> None:
        """Thread entry: create and run a dedicated asyncio loop."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._run())
        except Exception as e:
            # Emit error as an event to caller
            self._emit_event({
                "event_type": "listener_error",
                "agent": "event_hub_listener",
                "payload": {"error": repr(e)},
            })
        finally:
            try:
                self._loop.close()
            except Exception:
                pass

    async def _run(self) -> None:
        """Main async runner."""
        backoff = self._cfg.poll_interval_sec

        async with streamable_http_client(self._cfg.base_url) as transport:
            read, write = _extract_read_write(transport)

            async with ClientSession(read, write) as session:
                await session.initialize()

                try:
                    await session.call_tool("events_subscribe", {})
                except Exception:
                    pass

                await self._pull_once(session)
                backoff = self._cfg.poll_interval_sec

                while not self._stop_evt.is_set():
                    changed = await self._pull_once(session)
                    if changed:
                        backoff = self._cfg.poll_interval_sec
                    else:
                        backoff = min(self._cfg.backoff_max_sec, backoff + self._cfg.poll_interval_sec)
                    await asyncio.sleep(backoff)

    async def _pull_once(self, session: ClientSession) -> bool:
        """
        Pull events once. Returns True if any new events were delivered.
        """
        data = await self._read_events(session, self._cfg.scope, self._after_cursor)
        next_cursor = int(data.get("next_cursor", self._after_cursor))
        events = data.get("events", []) or []

        # Always advance cursor to avoid re-reading the same window.
        self._after_cursor = next_cursor

        if not events:
            return False

        new_events: List[Dict[str, Any]] = []
        for e in events[: self._cfg.max_events_per_pull]:
            cursor = e.get("cursor")
            eid = f"{self._cfg.scope}:{cursor}" if cursor is not None else None
            if eid is not None and eid in self._seen:
                continue
            if eid is not None:
                self._seen.add(eid)
            new_events.append(e)

        if not new_events:
            return False

        for ev in new_events:
            self._emit_event(ev)
            
        return True

    async def _read_events(self, session: ClientSession, scope: str, after: int) -> Dict[str, Any]:
        """
        Read events from: {resource_base}/{scope}/{after}
        Expected JSON: {"next_cursor": int, "events": list}
        """
        uri = f"{self._cfg.resource_base}/{scope}/{after}"
        try:
            res = await session.read_resource(uri)
        except Exception:
            return {"next_cursor": after, "events": []}

        contents = getattr(res, "contents", None)
        if contents is None:
            contents = res

        if not contents:
            return {"next_cursor": after, "events": []}

        # Check content type if returning TextContent
        text = getattr(contents[0], "text", None)
        if not isinstance(text, str) or not text:
            return {"next_cursor": after, "events": []}

        try:
            data = json.loads(text)
        except Exception:
            return {"next_cursor": after, "events": []}

        if "next_cursor" not in data:
            data["next_cursor"] = after
        if "events" not in data:
            data["events"] = []
        return data

    def _emit_event(self, event: Dict[str, Any]) -> None:
        """
        Dispatch the event up to the provided handler safely.
        """
        try:
            self._dispatcher.dispatch(event)
        except Exception as e:
            # We fail silently here so the listener loop never crashes because of bad UI handling
            print(f"[EventHubListener] Unhandled exception in user event callback: {e}")