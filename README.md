# EDMA (Event-Driven Multi-Agent MCP)

**EDMA** stands for **Event-Driven Multi-Agent**. It is a workflow framework built on top of [FastMCP](https://github.com/jlowin/fastmcp) for structured and robust tool orchestration. By combining a distributed Server-Client architecture with a dedicated `Event Hub`, it allows for seamless two-way communication, server-initiated UI events, and seamless orchestration using powerful LLM architectures like the [OpenAI Agents SDK](https://github.com/openai/openai-agents).

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

## 🚦 Usage

Just import from the package framework to orchestrate your tools.

**Building a Server Agent:**
```python
from edma_mcp.server.base import BaseMCP, mcp_tool

class DataAgent(BaseMCP):
    def __init__(self, event_mcp=None):
        super().__init__(name="data_agent", introduction="I analyze JSON.", prompt="...", model="gpt-4o", event_mcp=event_mcp)

    @mcp_tool()
    def process_data(self, key: str) -> str:
        """My Custom API Tool"""
        return f"Processed {key}"
```

**Starting the Orchestrator Server:**
```python
from edma_mcp.server.server import EDMAServer

server = EDMAServer(host="127.0.0.1", port=7300)
server.add_agent(DataAgent(event_mcp=server.event_hub))
server.run()
```
