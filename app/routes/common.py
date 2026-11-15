"""Shared helpers for the HTML routes."""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse

from app.config import get_settings
from app.security import token_required
from app.templating import templates

NAV_GROUPS: list[dict[str, Any]] = [
    {
        "label": "Books (local)",
        "collapsed": False,
        "items": [
            {"href": "/book-video", "label": "Book Video"},
            {"href": "/audiobooks", "label": "Audiobook"},
            {"href": "/voices", "label": "Voices"},
            {"href": "/files", "label": "Files"},
        ],
    },
    {
        "label": "Video Projects",
        "collapsed": False,
        "items": [
            {"href": "/projects", "label": "Projects"},
            {"href": "/images", "label": "Image Library"},
            {"href": "/prompts", "label": "Prompts"},
            {"href": "/assembly", "label": "Assembly"},
            {"href": "/outputs", "label": "Video Library"},
        ],
    },
    {
        "label": "Cloud & Remote GPU",
        "collapsed": True,
        "items": [
            {"href": "/", "label": "Overview"},
            {"href": "/providers", "label": "Providers"},
            {"href": "/accounts", "label": "Credits"},
            {"href": "/queue", "label": "Queue"},
            {"href": "/manual", "label": "Manual Assist"},
            {"href": "/remote", "label": "Remote GPU"},
            {"href": "/capacity", "label": "Daily Capacity"},
        ],
    },
    {
        "label": "System",
        "collapsed": False,
        "items": [
            {"href": "/settings", "label": "Settings"},
            {"href": "/diagnostics", "label": "Diagnostics"},
        ],
    },
]

NAV_ITEMS: list[dict[str, Any]] = [item for group in NAV_GROUPS for item in group["items"]]


def page(
    request: Request,
    template: str,
    context: dict[str, Any] | None = None,
    *,
    status_code: int = 200,
) -> HTMLResponse:
    """Render a full page with the chrome context the base template needs."""
    from app.main import STARTUP_PROBLEMS

    settings = get_settings()
    merged: dict[str, Any] = {
        "nav_items": NAV_ITEMS,
        "nav_groups": NAV_GROUPS,
        "data_root": str(settings.data_root),
        "startup_problems": STARTUP_PROBLEMS,
        # The base template echoes this into an hx-headers attribute. When no
        # token is configured it is empty and the header is ignored.
        "ui_token_hint": settings.ui_token if token_required() else "",
    }
    merged.update(context or {})
    return templates.TemplateResponse(request, template, merged, status_code=status_code)


def fragment(
    request: Request,
    template: str,
    context: dict[str, Any] | None = None,
    *,
    flash: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    """Render an HTMX partial, optionally with a flash message."""
    response = templates.TemplateResponse(
        request, template, context or {}, status_code=status_code
    )
    if flash:
        response.headers["X-AVM-Flash"] = flash
    return response


def flash_only(message: str, *, trigger: str | None = None) -> HTMLResponse:
    """Empty response that just shows a message and optionally refreshes a view."""
    response = HTMLResponse("")
    response.headers["X-AVM-Flash"] = message
    if trigger:
        response.headers["HX-Trigger"] = trigger
    return response
