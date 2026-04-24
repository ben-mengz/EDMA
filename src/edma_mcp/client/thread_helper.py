import asyncio
import threading
import collections
from typing import Any, Callable, Coroutine, List, Optional, Tuple

class ThreadHelper:
    """
    ThreadHelper provides:
    1. Safe synchronous calls from background threads (e.g. asyncio loops) to a main thread (e.g. GUI UI loop).
    2. A dedicated background asyncio event loop for running async tasks without blocking the main thread.
    
    This is extremely useful when integrating EDMA MCP tools with UI frameworks like PyQt or Tkinter.
    """

    def __init__(self, main_event_loop: asyncio.AbstractEventLoop) -> None:
        """
        :param main_event_loop: The asyncio event loop running on the main UI thread. 
                                Many GUI frameworks support wrapping their event loops with asyncio.
        """
        # Main thread (UI) event loop
        self.__event_loop = main_event_loop

        # Pending calls to be executed on the main thread
        self.__pending_calls: collections.deque[
            Tuple[Callable, List, threading.Event, List[Any]]
        ] = collections.deque()

        self.__lock = threading.RLock()

        # Background asyncio loop infrastructure
        self.__bg_loop: Optional[asyncio.AbstractEventLoop] = None
        self.__bg_thread: Optional[threading.Thread] = None

        self.__start_background_loop()

    # ------------------------------------------------------------------
    # Background asyncio loop management
    # ------------------------------------------------------------------

    def __start_background_loop(self) -> None:
        """Start a dedicated background asyncio event loop in a separate thread."""
        if self.__bg_thread is not None:
            return

        def _run_loop() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self.__bg_loop = loop
            loop.run_forever()

        self.__bg_thread = threading.Thread(
            target=_run_loop,
            name="ThreadHelper-BackgroundAsyncLoop",
            daemon=True,
        )
        self.__bg_thread.start()

    @property
    def background_loop(self) -> asyncio.AbstractEventLoop:
        """Return the background asyncio event loop."""
        if self.__bg_loop is None:
            raise RuntimeError("Background asyncio loop is not initialized.")
        return self.__bg_loop

    def submit_async(
        self,
        coro: Coroutine,
        *,
        wait: bool = False,
        timeout: Optional[float] = None,
    ) -> Any:
        """
        Submit a coroutine to the background asyncio loop.

        Parameters
        ----------
        coro: The Coroutine to execute.
        wait: If True, block until completion and return the result.
              WARNING: Do NOT use wait=True on the UI main thread.
        timeout: Optional timeout when waiting for result.
        """
        future = asyncio.run_coroutine_threadsafe(coro, self.background_loop)

        if not wait:
            return None

        # Blocking wait (must NOT be called from UI main thread)
        return future.result(timeout=timeout)

    # ------------------------------------------------------------------
    # Main thread call handling
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Flush pending main-thread calls before shutdown."""
        with self.__lock:
            while self.__pending_calls:
                handle_func, params, event, result_container = self.__pending_calls.popleft()
                if not event.is_set():
                    try:
                        result_container[0] = handle_func(*params)
                    except Exception as e:
                        result_container[1] = e
                    finally:
                        event.set()
        
        # Optionally, shut down the background loop
        if self.__bg_loop is not None:
            self.__bg_loop.call_soon_threadsafe(self.__bg_loop.stop)
        if self.__bg_thread is not None:
            self.__bg_thread.join(timeout=2.0)

    def call_on_main_thread(
        self,
        func: Callable[..., Any],
        params: Optional[List] = None,
    ) -> Any:
        """
        Execute a callable on the main UI thread.

        If called from a background thread, this method blocks until
        the main thread has executed the function.
        """
        if params is None:
            params = []

        if threading.current_thread() != threading.main_thread():
            event = threading.Event()
            result_container = [None, None]  # [result, exception]

            with self.__lock:
                self.__pending_calls.append((func, params, event, result_container))
                self.__event_loop.call_soon_threadsafe(self.__process_pending_calls)

            # Block until main thread finishes execution
            event.wait()

            if result_container[1] is not None:
                raise result_container[1]

            return result_container[0]

        # Already on main thread
        return func(*params)

    def __process_pending_calls(self) -> None:
        """Process queued calls on the main UI thread."""
        with self.__lock:
            while self.__pending_calls:
                handle_func, params, event, result_container = self.__pending_calls.popleft()
                if event.is_set():
                    continue
                try:
                    result_container[0] = handle_func(*params)
                except Exception as e:
                    result_container[1] = e
                finally:
                    event.set()
