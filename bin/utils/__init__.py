"""MIRAGE pipeline utilities.

This package provides common utilities for the MIRAGE image processing pipeline:

- **logger**: Centralized logging configuration with timing utilities
- **image_utils**: Image I/O and processing utilities
- **metadata**: OME-TIFF metadata handling
- **registration_utils**: Registration helper functions

Examples
--------
>>> from utils import get_logger
>>> logger = get_logger(__name__)
>>> logger.info("Starting pipeline step")
"""

from __future__ import annotations

# Logger utilities
from .logger import (
    configure_logging,
    get_logger,
    log_progress,
    log_timing,
    timed,
)

__all__ = [
    # Logger
    "get_logger",
    "configure_logging",
    "log_progress",
    "log_timing",
    "timed",
]
