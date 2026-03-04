import asyncio
from mcp.client.sse import sse_client
from contextlib import asynccontextmanager

async def test_sse():
    print("Testing SSE...")
    try:
        async with sse_client("http://127.0.0.1:7300/event_hub/sse") as (read, write):
            print("Successfully connected to SSE!")
            msg = await read.receive()
            print("Received:", msg)
    except Exception as e:
        print(f"Exception connecting to SSE: {e}")

if __name__ == "__main__":
    asyncio.run(test_sse())
