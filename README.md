# EDMA (Event-Driven Multi-Agent MCP)

## Authors

- Meng Zhao
- Anton Gladyshev
- Sherjeel Shabih
- Christoph T. Koch
- 
**EDMA** stands for **Event-Driven Multi-Agent**, a workflow framework built on top of **FastMCP** for structured and robust orchestration of tool-based tasks. Unlike traditional **prompt-driven systems**, where the interaction between the user, the LLM, and external tools is organized as a sequence of prompts and responses, EDMA adopts an **event-driven architecture** that enables dynamic coordination between agents, tools, and the user interface.

In conventional prompt-driven workflows, each step is typically triggered by a new prompt from the user or by manually chaining prompts within an application. This approach makes complex workflows difficult to manage, as intermediate states, user interactions, and tool outputs must all be encoded within prompts. As the number of steps increases, the workflow becomes fragile, difficult to control, and prone to hallucinations or logical inconsistencies.

EDMA addresses these limitations by introducing an **event-driven coordination layer**. Instead of relying solely on prompt sequences, agents and tools communicate through structured events routed via a central **Event Hub**. When a task progresses, components emit events such as tool execution results, requests for missing parameters, or notifications of state updates. These events can trigger actions in other agents, update the user interface, or request additional user input without requiring a new prompt.

This architecture enables **asynchronous and bidirectional communication** between backend agents and the user interface. For example, an agent can request that the user select a region of interest in an image, display intermediate results, or prompt for missing experimental parameters. Because workflow state is managed through events rather than prompt history, complex multi-step processes become more transparent, controllable, and robust.

By combining this event-driven design with LLM-based reasoning frameworks such as the **OpenAI Agents SDK**, EDMA enables workflows where **manual interaction, assisted operation, and automated execution can seamlessly coexist within a unified system**.

---

## 🚀 Features & Architecture

The codebase is split into two primary packages under `src/edma_mcp/`:

### 1. The Server (`edma_mcp.server`)
Built around `BaseMCP` and Starlette/Uvicorn, the Server effortlessly exposes multiple Agents containing custom APIs (`@mcp_tool`) as network endpoints.
- **`EventMCP` / Event Hub**: A unique internal MCP Server dedicated to ingesting, persisting, and rebroadcasting workflow events (JSON) through streamable HTTP (Server-Sent Events), so the Client application can react instantly to backend processing queues or Server-side UI triggers.
- **BaseMCP Auto-registration**: Simplifies defining your Agent. Any method marked with `@mcp_tool` is automatically ingested directly into the host FastMCP definitions.

### 2. The Client (`edma_mcp.client`)
Designed for sophisticated application integration (especially Desktop GUIs like Tkinter or PyQt), bridging remote MCP Agents natively into robust Python instances.
- **`EventHubListener` & `EventDispatcher`**: Background asyncio worker that automatically long-polls / streams events from the Server's Event Hub, delegating specific `.on("event_type")` logic down to dedicated callback handlers on the Client UI.
- **`OpenAIMCPBridge`**: Dynamically fetches the profiles, tools, and introductions of ALL active Agents on the remote `EDMAServer` and reconstructs them into executable `OpenAI Agent` primitives in memory. Supports auto-generating Triage/Router agents for transparent, distributed multi-agent executions.
- **`ThreadHelper`**: A beautifully engineered utility resolving `asyncio` and Main Thread UI blocking (Deadlocks). Safely pumps arbitrary async callbacks directly onto a strict GUI thread without clunky `sleep()` or `loop.run_until_complete()` polling.

---

## 🛠 Installation

You can install the package directly in editable mode:

```bash
cd EDMA
pip install -e .
```

To run the full `demo_client.py` capabilities (which showcase Multi-Agent network auto-generation and routing), you will also need the new OpenAI Agents module:

```bash
pip install openai-agents
```

*(Ensure you have an `OPENAI_API_KEY` present in your environment variables before testing LLM integrations).*

---

## 🎮 Understanding the Demos

Located in `examples/`, these demos exhibit a full-stack, UI-to-UI Event-Driven toolchain.

### Demo 1: The Server GUI (`python demo_server.py`)
This script starts up an asynchronous Uvicorn cluster routing **Three MCP Servers**:
1. `SearchAgent`: An LLM-driven helper that exposes a `search_weather` tool.
2. `FFTAgent`: A mockup processor exposing a `perform_fft` image processing tool.
3. `Event Hub`: The central notification hub.

**What it shows:**
The Server exposes a minimalist Tkinter GUI representing an "Application Controller". Clicking buttons on this Server GUI pushes specific events (`show` and `trigger` behaviors) into the Event Hub. Essentially, it demonstrates **Server-To-Client** communications, pushing tasks downstream instantly.

### Demo 2: The Client Swarm (`python demo_client.py`)
This script instantiates the EDMA Client App, featuring a chat interface and background listener.

**What it shows:**
1. **Event Listening**: Directly binds `EventDispatcher` to listen for remote Server notifications. If the Server pushes a `"show"` event, the client silently appends a UI log. If the Server pushes a `"trigger"` event, the client intercepts an automatic script instruction (e.g., *"Please use search_weather..."*).
2. **OpenAIMCPBridge integration**: Upon receiving a prompt, it automatically wraps all active remote Micro-Servers into `openai.Agent` objects, spins up a `MainRouter` triage agent, and starts an LLM Execution cascade (`Runner.run()`). 
3. **Thread Safety**: Notice that everything executes safely within Tkinter using `ThreadHelper`—async calls are executed in a daemon thread, and UI logs cleanly trigger via cross-thread events (`self.thread_helper.call_on_main_thread(_log)`) avoiding Tkinter crashes on Mac/Windows.

**How to test the workflow:**
1. Start `demo_server.py` in Terminal 1.
2. Start `demo_client.py` in Terminal 2.
3. In the Server UI, click the **Send 'Trigger' (Ask LLM to Search)** button.
4. Watch the Client App instantly receive the event over streamable HTTP, boot up the Multi-Agent framework, triage the instruction to the correct remote `SearchAgent`, execute the HTTP Tool cleanly, and print out the weather in Tokyo—all without you typing a word into the Client chat!

---

## 🚦 Step-by-Step Guide: Building Your Own Client and Server

Based on the mechanics demonstrated in the `examples/` directory, here is how you can step-by-step build out your own Server application and a connected Client app.

### Step 1: Create a Server Agent

Define your custom Agent by inheriting from `BaseMCP`. Use the `@mcp_tool` decorator to expose Python functions as remote tools for LLMs.

```python
from edma_mcp.server.base import BaseMCP, mcp_tool

class MyDataAgent(BaseMCP):
    def __init__(self, event_mcp=None):
        super().__init__(
            name="data_agent", 
            introduction="I analyze and process data.", 
            prompt="You are a data processing assistant.", 
            model="gpt-4o", 
            event_mcp=event_mcp
        )

    @mcp_tool()
    def process_data(self, key: str) -> str:
        """Process local data based on a key."""
        return f"Processed data for {key} successfully."
```

### Step 2: Start the EDMA Server

Instantiate the `EDMAServer`, add your agents, and start the server thread. The server will seamlessly expose your agents as well as the central Event Hub.

```python
import threading
from edma_mcp.server.server import EDMAServer

# 1. Initialize the central Server
server = EDMAServer(host="127.0.0.1", port=7300)

# 2. Add your Agent and connect it to the Event Hub
data_agent = MyDataAgent(event_mcp=server.event_hub)
server.add_agent(data_agent)

# 3. Run the fast Uvicorn server in the background
threading.Thread(target=server.run, daemon=True).start()
print("Server is running on http://127.0.0.1:7300")
```

*(You can explore `examples/demo_server.py` to see how to manually trigger events and push them into the Event Hub natively via `data_agent.push_event()`).*

### Step 3: Create the EDMA Client Listener

Over on the client application, start an `EventHubListener` to subscribe to the remote Event Hub, and use `EventDispatcher` to map incoming events to local functions.

```python
from edma_mcp.client.eventHubListener import EventHubListener, EventHubConfig, EventDispatcher
from typing import Dict, Any

dispatcher = EventDispatcher()

@dispatcher.on("trigger")
def handle_remote_trigger(event: Dict[str, Any]):
    agent = event.get('agent', 'unknown')
    msg = event.get('payload', {}).get('tool_call_message', {}).get('trigger_message', '')
    print(f"Received remote trigger from {agent}: {msg}")
    # Here, you would typically pass the `msg` to your LLM framework to continue the workflow

@dispatcher.set_default
def handle_default(event: Dict[str, Any]):
    print(f"Unhandled event: {event}")

# Configure and start the background listener
config = EventHubConfig(
    base_url="http://127.0.0.1:7300/event_hub/", 
    resource_base="events://hub/all", scope="all", poll_interval_sec=0.5
)
listener = EventHubListener(config=config, dispatcher=dispatcher)
listener.start()
```

### Step 4: Dynamically Bridge the LLM Network

With the Client running, use the `OpenAIMCPBridge` to automatically fetch all Server agents over HTTP and translate them into actionable `OpenAI Agent` routines.

```python
import asyncio
from edma_mcp.client.openai_bridge import OpenAIMCPBridge
from agents import Runner

async def run_client_llm(user_message: str):
    bridge = OpenAIMCPBridge(base_http_url="http://127.0.0.1:7300")
    
    # 1. Dynamically build OpenAI Agents from the remote EDMAServer
    triage_agent, sub_agents = await bridge.build_openai_system_via_fastmcp(
        model="gpt-4o",
        triage_name="MainRouter",
        triage_instructions="Forward the request to the specialist."
    )
    
    # 2. Let OpenAI Agents SDK handle tool routing and execution natively!
    result = await Runner.run(starting_agent=triage_agent, input=user_message)
    print(f"LLM Response: {result.final_output}")

# Execute the chain via asyncio
# asyncio.run(run_client_llm("Please use MyDataAgent to process key 'test_123'"))
```

*(If you are developing GUI applications like Tkinter or PyQt, utilize `ThreadHelper`—as showcased in `examples/demo_client.py`—to elegantly handle asyncio loops without freezing the main UI thread!)*
