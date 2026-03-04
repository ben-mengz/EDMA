from typing import Optional
from edma_mcp.server.base import BaseMCP, mcp_tool
from edma_mcp.server.event import EventMCP
from edma_mcp.server.server import EDMAServer

# -------------------------------------------------------------
# Demonstration / Default Execution
# -------------------------------------------------------------

class DefaultTestAgent(BaseMCP):
    """
    An elegant, modern Python way to define a new agent!
    No need to write decorators inside __init__.
    """
    def __init__(self, event_mcp: Optional[EventMCP] = None):
        super().__init__(
            name="test_agent", 
            introduction="I am a test agent.",
            prompt="You are a helpful assistant.",
            model="gpt-4o",
            event_mcp=event_mcp
        )

    @mcp_tool()
    def greet(self, name: str) -> str:
        """Greet a user by name."""
        return f"Hello, {name}!"


def main():
    # 1. Instantiate the orchestrator
    server = EDMAServer(host="127.0.0.1", port=7300)
    
    # 2. Add as many classes as you want
    agent = DefaultTestAgent()
    server.add_agent(agent)
    
    # 3. Start the server!
    server.run()

if __name__ == "__main__":
    main()
