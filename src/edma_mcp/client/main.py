import asyncio
from typing import Any, Dict
from edma_mcp.client.bridge import MCPBridgeManager
from edma_mcp.client.eventHubListener import EventHubListener, EventHubConfig, EventDispatcher

# -------------------------------------------------------------
# 1. User/UI developers can create an EventDispatcher instance
# -------------------------------------------------------------
dispatcher = EventDispatcher()

@dispatcher.on("show")
def handle_show(event: Dict[str, Any]):
    """Called only when event 'targets' list includes 'show'."""
    payload: Any = event.get("payload", {})
    print(f"\n[UI Action : show] -> Update GUI Screen with:")
    print(payload)
    print("-" * 40)

@dispatcher.on("trigger")
def handle_trigger(event: Dict[str, Any]):
    """Called only when event 'targets' list includes 'trigger'."""
    payload: Any = event.get("payload", {})
    msg = payload.get("tool_call_message", {}).get("trigger_message", "No message")
    print(f"\n[UI Action : trigger] -> Trigger LLM process:")
    print(msg)
    print("-" * 40)

@dispatcher.set_default
def handle_unknown_event(event: Dict[str, Any]):
    """Called when an event has no matching explicit handlers."""
    print(f"\n[EventHub Default] Received Event:")
    for k, v in event.items():
        print(f"  {k}: {v}")
    print("-" * 40)
# -------------------------------------------------------------

async def run_client():
    print("Initializing Client...")
    
    server_url = "http://127.0.0.1:7300"
    bridge_manager = MCPBridgeManager(base_http_url=server_url)
    
    # Refresh to see what agents are available
    result = await bridge_manager.refresh()
    print("Discovery result:", result)
    
    if not result.ok or not result.agents:
        print("No agents found. Make sure the server is running.")
        return
        
    for agent in result.agents:
        print(f"\n--- Checking Agent: {agent} ---")
        
        # List Tools
        try:
            tools = await bridge_manager.list_tools(agent)
            print(f"Tools available for {agent}:")
            for t in tools:
                if isinstance(t, dict):
                    print(f"  - {t.get('name')}: {t.get('description')}")
                else:
                    print(f"  - {getattr(t, 'name', 'unknown')}: {getattr(t, 'description', 'unknown')}")
        except Exception as e:
            print(f"Failed to list tools for {agent}: {e}")

        # Try to call a specific tool 'greet' if it exists
        try:
            print(f"Calling tool 'greet' on {agent} if it exists...")
            if agent == "test_agent":
                response = await bridge_manager.call_tool(agent, "greet", {"name": "EMMA User"})
                
                if isinstance(response, list) and len(response) > 0:
                    print(f"Response from greet: {getattr(response[0], 'text', str(response))}")
                else:
                    print(f"Response from greet: {response}")
        except Exception as e:
            print(f"Error calling tool: {e}")

    # Set up and start the EventHubListener for event-driven behavior
    config = EventHubConfig(
        base_url=f"{server_url}/event_hub/sse", 
        resource_base="events://all",
        scope="all",
        poll_interval_sec=1.0, 
    )
    
    listener = EventHubListener(config=config, dispatcher=dispatcher)
    listener.start()
    
    print("\n[Client] Event listener started. Waiting for events... (Press Ctrl+C to exit)")
    
    try:
        # Keep the main loop running to receive events
        while True:
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        pass
    finally:
        listener.stop()

def main():
    try:
        asyncio.run(run_client())
    except KeyboardInterrupt:
        print("\n[Client] Shutdown requested.")

if __name__ == "__main__":
    main()
