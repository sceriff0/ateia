"""MIRAGE pipeline utilities.

This package provides common utilities for the MIRAGE image processing pipeline:

- **logger**: Centralized logging configuration
- **image_io**: the one owner of TIFF writes (bigtiff on every write)
- **image_utils**: image loading and array-shape utilities
- **metadata**: OME-TIFF metadata handling
- **registration_utils**: Registration helper functions

Examples
--------
>>> from utils.logger import get_logger
>>> logger = get_logger(__name__)
>>> logger.info("Starting pipeline step")
"""

from __future__ import annotations
