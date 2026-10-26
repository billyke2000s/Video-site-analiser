"""Site plugins. A plugin decides how one kind of site is analysed, searched and mined for media.

Built in:
  - RecipePlugin: any site described by a recipe JSON file (built-in or user-supplied).
  - GenericPlugin: unknown sites, using only page analysis (search forms, OpenSearch, schema.org
    SearchAction, media tags, structured data, optional browser rendering).
Third-party plugins register under the `vsa.plugins` entry-point group.
"""
from .base import Context, SitePlugin, load_plugins  # noqa: F401
