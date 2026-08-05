"""MIRAGE pipeline utilities.

This package provides common utilities for the MIRAGE image processing pipeline:

- **logger**: Centralized logging configuration with timing utilities
- **constants**: Pipeline-wide constants and exit codes
- **image_utils**: Image I/O and processing utilities
- **metadata**: OME-TIFF metadata handling
- **registration_utils**: Registration helper functions

Examples
--------
>>> from utils import get_logger, ExitCode, DEFAULT_FOV_SIZE
>>> logger = get_logger(__name__)
>>> logger.info(f"Using FOV size: {DEFAULT_FOV_SIZE}")
"""

from __future__ import annotations

# Constants
from .constants import (
    DEFAULT_FOV_SIZE,
    DEFAULT_PIXEL_SIZE,
    DEFAULT_TILE_SIZE,
    LOG_SEPARATOR,
    OME_TIFF_EXTENSIONS,
    TIFF_EXTENSIONS,
    ExitCode,
)

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
    # Constants
    "ExitCode",
    "DEFAULT_FOV_SIZE",
    "DEFAULT_PIXEL_SIZE",
    "DEFAULT_TILE_SIZE",
    "LOG_SEPARATOR",
    "TIFF_EXTENSIONS",
    "OME_TIFF_EXTENSIONS",
]
