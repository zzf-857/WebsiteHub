"""Fetch and read public metadata from a user-supplied URL.

This is the first place in WebHub that lets the server reach an address the
*user* chose rather than a vendor endpoint we control, so it is also the
largest attack surface in the codebase.  Everything here is shaped by that.
"""

from .fetcher import (
    FetchOutcome,
    SiteMetadata,
    fetch_site_metadata,
)

__all__ = ["FetchOutcome", "SiteMetadata", "fetch_site_metadata"]
