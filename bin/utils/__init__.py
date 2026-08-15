"""MIRAGE pipeline utilities.

This package provides common utilities for the MIRAGE image processing pipeline:

- **logger**: Centralized logging configuration
- **image_utils**: Image I/O and processing utilities
- **metadata**: OME-TIFF metadata handling

Examples
--------
>>> from utils.logger import get_logger
>>> logger = get_logger(__name__)
>>> logger.info("Starting pipeline step")
"""

from __future__ import annotations
