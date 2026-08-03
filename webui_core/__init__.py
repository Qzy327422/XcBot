# -*- coding: utf-8 -*-
"""Internal WebUI helpers split from the compatibility facade in webui.py."""

from webui_core.features import DEFAULT_FEATURE_SWITCHES, FEATURE_META
from webui_core.resources import format_uptime, get_resource_usage
from webui_core.versioning import compare_versions, parse_version_parts

__all__ = [
    "DEFAULT_FEATURE_SWITCHES",
    "FEATURE_META",
    "format_uptime",
    "get_resource_usage",
    "compare_versions",
    "parse_version_parts",
]
