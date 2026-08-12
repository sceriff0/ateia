"""Retry utilities for transient failure handling.

This module provides retry mechanisms for operations that may fail
due to transient issues like memory pressure or I/O contention.

Examples
--------
Using the context manager:
>>> retry_ctx = RetryContext(max_attempts=2, delay_seconds=2.0)
>>> for attempt in retry_ctx:
...     try:
...         result = do_something()
...         break
...     except SomeError as e:
...         retry_ctx.failed(e)
"""

from __future__ import annotations

import gc
import logging
import time
from typing import Callable, Optional, Tuple, Type

__all__ = [
    "RetryContext",
]


logger = logging.getLogger(__name__)


class RetryContext:
    """Context manager for retry operations with cleanup.

    Provides an iterator-based retry mechanism that allows for
    cleanup operations between retry attempts.

    Parameters
    ----------
    max_attempts : int, default=2
        Maximum number of retry attempts
    delay_seconds : float, default=2.0
        Delay between retries in seconds
    cleanup_func : callable, optional
        Function to call between retries for cleanup (e.g., gc.collect)
    exceptions : tuple of Exception types, default=(Exception,)
        Exception types that trigger retries

    Attributes
    ----------
    attempt : int
        Current attempt number (1-indexed)
    last_exception : Exception or None
        Last exception that occurred

    Examples
    --------
    >>> def cleanup():
    ...     gc.collect()
    ...
    >>> retry_ctx = RetryContext(
    ...     max_attempts=2,
    ...     delay_seconds=2.0,
    ...     cleanup_func=cleanup
    ... )
    >>> for attempt in retry_ctx:
    ...     try:
    ...         result = process_slide(slide)
    ...         break  # Success, exit retry loop
    ...     except MemoryError as e:
    ...         retry_ctx.failed(e)
    ...         # Will retry after cleanup and delay

    With error checking after loop:
    >>> if retry_ctx.last_exception:
    ...     print(f"All retries failed: {retry_ctx.last_exception}")
    """

    def __init__(
        self,
        max_attempts: int = 2,
        delay_seconds: float = 2.0,
        cleanup_func: Optional[Callable[[], None]] = None,
        exceptions: Tuple[Type[Exception], ...] = (Exception,),
    ):
        self.max_attempts = max_attempts
        self.delay_seconds = delay_seconds
        self.cleanup_func = cleanup_func
        self.exceptions = exceptions
        self.attempt = 0
        self.last_exception: Optional[Exception] = None
        self._should_retry = True

    def __iter__(self):
        """Return iterator."""
        return self

    def __next__(self) -> int:
        """Get next attempt number.

        Returns
        -------
        int
            Current attempt number (1-indexed)

        Raises
        ------
        StopIteration
            When max attempts reached
        """
        if not self._should_retry or self.attempt >= self.max_attempts:
            if self.last_exception:
                raise self.last_exception
            raise StopIteration

        self.attempt += 1
        return self.attempt

    def failed(self, exception: Exception) -> None:
        """Mark current attempt as failed.

        Performs cleanup if configured and waits before next retry.

        Parameters
        ----------
        exception : Exception
            The exception that caused the failure
        """
        self.last_exception = exception

        # Run cleanup function if provided
        if self.cleanup_func:
            try:
                self.cleanup_func()
            except Exception as cleanup_err:
                logger.warning(f"Cleanup failed: {cleanup_err}")

        # Wait before next retry (unless this was the last attempt)
        if self.attempt < self.max_attempts:
            logger.debug(
                f"Retry {self.attempt}/{self.max_attempts} failed: {exception}. "
                f"Waiting {self.delay_seconds}s before retry..."
            )
            time.sleep(self.delay_seconds)

    def succeeded(self) -> None:
        """Mark operation as succeeded, stopping further retries."""
        self._should_retry = False
        self.last_exception = None

    @property
    def all_attempts_failed(self) -> bool:
        """Check if all retry attempts have been exhausted.

        Returns
        -------
        bool
            True if all attempts failed
        """
        return self.attempt >= self.max_attempts and self.last_exception is not None


def default_cleanup() -> None:
    """Default cleanup function that runs garbage collection."""
    gc.collect()
