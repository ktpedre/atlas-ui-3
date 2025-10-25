"""
Notification utilities - pure functions for handling chat event notifications.

This module provides stateless utility functions for sending various types
of notifications during chat operations without maintaining any state.
Also includes minimal sanitization to avoid leaking sensitive tokens/paths
in filenames returned from tools.
"""

import logging
from typing import Any, Dict, List, Optional, Callable, Awaitable
import json
import re
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Type hint for update callback
UpdateCallback = Callable[[Dict[str, Any]], Awaitable[None]]


_S3_KEY_PREFIX_PATTERN = re.compile(r"^(?:\d{9,})_[0-9a-fA-F]{6,}_(.+)$")


def _sanitize_filename_value(value: Any) -> Any:
    """Return a user-safe filename string with no token or internal prefixes.

    - If not a string, return as-is
    - Strip query string (e.g., ?token=...)
    - If URL, keep basename of the path
    - Else if path-like, keep basename
    - If basename matches ts_hash_original.ext, return original.ext
    """
    if not isinstance(value, str) or not value:
        return value

    # Drop query
    without_query = value.split("?", 1)[0]

    # Extract path from URL if any
    path = without_query
    if without_query.startswith("http://") or without_query.startswith("https://"):
        try:
            parsed = urlparse(without_query)
            path = parsed.path or without_query
        except Exception:
            path = without_query

    # Basename only
    basename = path.rsplit("/", 1)[-1]

    # Strip known storage prefix pattern 1755396436_d71d38d7_original.csv
    m = _S3_KEY_PREFIX_PATTERN.match(basename)
    if m:
        return m.group(1)
    return basename


def _sanitize_result_for_ui(obj: Any) -> Any:
    """Recursively sanitize tool result content for UI display.

    Rules:
    - Any key literally named 'filename' is reduced to a clean basename.
    - For common structures like {'file': {'filename': ...}}, sanitize nested filename too.
    - Lists and nested dicts are traversed.
    """
    try:
        if isinstance(obj, dict):
            sanitized: Dict[str, Any] = {}
            for k, v in obj.items():
                if k == "filename":
                    sanitized[k] = _sanitize_filename_value(v)
                elif k == "file" and isinstance(v, dict):
                    # Typical shape in artifacts-like objects
                    inner = dict(v)
                    if "filename" in inner:
                        inner["filename"] = _sanitize_filename_value(inner.get("filename"))
                    sanitized[k] = _sanitize_result_for_ui(inner)
                else:
                    sanitized[k] = _sanitize_result_for_ui(v)
            return sanitized
        if isinstance(obj, list):
            return [_sanitize_result_for_ui(x) for x in obj]
        return obj
    except Exception:
        # Fail open on sanitization to avoid breaking UI updates
        return obj


async def safe_notify(callback: UpdateCallback, message: Dict[str, Any]) -> None:
    """
    Invoke callback safely, logging but suppressing exceptions.
    
    Pure function that handles notification errors gracefully.
    """
    try:
        await callback(message)
    except Exception as e:
        logger.warning(f"Update callback failed: {e}")


async def notify_tool_start(
    tool_call,
    parsed_args: Dict[str, Any],
    update_callback: Optional[UpdateCallback]
) -> None:
    """
    Send tool start notification.
    
    Pure function that creates and sends tool start notification.
    """
    if not update_callback:
        return
        
    # Derive server name for display context
    parts = tool_call.function.name.split("_")
    server_name = "_".join(parts[:-1]) if len(parts) > 1 else "unknown"
    
    payload = {
        "type": "tool_start",
        "tool_call_id": tool_call.id,
        "tool_name": tool_call.function.name,
        "server_name": server_name,
        "arguments": parsed_args
    }
    await safe_notify(update_callback, payload)


async def notify_tool_complete(
    tool_call,
    result,
    parsed_args: Dict[str, Any],
    update_callback: Optional[UpdateCallback]
) -> None:
    """
    Send tool completion notification with canvas handling.
    
    Pure function that handles tool completion notifications.
    """
    if not update_callback:
        return

    # Standard completion notification (with sanitized result for UI)
    result_content = getattr(result, "content", None)
    # If content is JSON string, parse first so we can sanitize nested filename fields
    if isinstance(result_content, str):
        try:
            parsed = json.loads(result_content)
            sanitized_content = _sanitize_result_for_ui(parsed)
        except Exception:
            sanitized_content = _sanitize_result_for_ui(result_content)
    else:
        sanitized_content = _sanitize_result_for_ui(result_content)
    complete_payload = {
        "type": "tool_complete",
        "tool_call_id": tool_call.id,
        "tool_name": tool_call.function.name,
        "success": result.success,
        "result": sanitized_content
    }
    
    # Canvas tool special handling
    if tool_call.function.name == "canvas_canvas":
        await notify_canvas_content(parsed_args, update_callback)
    
    # Trace artifacts/display presence when available
    try:
        arts = getattr(result, "artifacts", None)
        disp = getattr(result, "display_config", None)
        if arts or disp:
            logger.info(
                "Tool result has artifacts/display: artifacts=%d, has_display=%s",
                len(arts) if isinstance(arts, list) else 0,
                bool(disp),
            )
    except Exception:
        pass

    await safe_notify(update_callback, complete_payload)


async def notify_tool_progress(
    tool_call_id: str,
    tool_name: str,
    progress: float,
    total: Optional[float],
    message: Optional[str],
    update_callback: Optional[UpdateCallback]
) -> None:
    """
    Send tool progress notification.

    Emits an event shaped for the UI to render progress bars/messages.
    """
    if not update_callback:
        return

    try:
        pct: Optional[float] = None
        if total is not None and total != 0:
            try:
                pct = (float(progress) / float(total)) * 100.0
            except Exception:
                pct = None
        payload = {
            "type": "tool_progress",
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
            "progress": progress,
            "total": total,
            "percentage": pct,
            "message": message or "",
        }
        await safe_notify(update_callback, payload)
    except Exception as e:
        logger.warning(f"Failed to emit tool_progress: {e}")


async def notify_canvas_content(
    parsed_args: Dict[str, Any],
    update_callback: UpdateCallback
) -> None:
    """
    Send canvas content notification.
    
    Pure function that extracts and sends canvas content.
    """
    try:
        content_arg = parsed_args.get("content") if isinstance(parsed_args, dict) else None
        if content_arg:
            logger.info("Emitting canvas_content event (length=%s)", len(content_arg) if isinstance(content_arg, str) else "obj")
            await safe_notify(update_callback, {
                "type": "canvas_content",
                "content": content_arg
            })
        else:
            logger.info("Canvas tool called without 'content' arg; skipping canvas_content event")
    except Exception as e:
        logger.warning("Failed to emit canvas_content event: %s", e)


async def notify_tool_error(
    tool_call,
    error: str,
    update_callback: Optional[UpdateCallback]
) -> None:
    """
    Send tool error notification.
    
    Pure function that creates and sends error notification.
    """
    if not update_callback:
        return
        
    await safe_notify(update_callback, {
        "type": "tool_error",
        "tool_call_id": tool_call.id,
        "tool_name": tool_call.function.name,
        "error": error
    })


async def notify_chat_response(
    message: str,
    has_pending_tools: bool = False,
    update_callback: Optional[UpdateCallback] = None
) -> None:
    """
    Send chat response notification.
    
    Pure function that notifies about chat responses.
    """
    if not update_callback:
        return
        
    await safe_notify(update_callback, {
        "type": "chat_response",
        "message": message,
        "has_pending_tools": has_pending_tools
    })


async def notify_response_complete(update_callback: Optional[UpdateCallback]) -> None:
    """
    Send response completion notification.
    
    Pure function that signals completion.
    """
    if not update_callback:
        return
        
    await safe_notify(update_callback, {"type": "response_complete"})


async def notify_tool_synthesis(
    message: str,
    update_callback: Optional[UpdateCallback]
) -> None:
    """
    Send tool synthesis notification.
    
    Pure function that notifies about synthesis results.
    """
    if not update_callback:
        return
        
    if message and message.strip():
        await safe_notify(update_callback, {
            "type": "tool_synthesis",
            "message": message
        })


async def notify_agent_update(
    update_type: str,
    connection,
    **kwargs
) -> None:
    """
    Send agent mode update notification.
    
    Pure function that handles agent-specific notifications.
    """
    if not connection:
        return
        
    try:
        payload = {
            "type": "agent_update",
            "update_type": update_type,
            **kwargs
        }
        await connection.send_json(payload)
    except Exception as e:
        logger.warning(f"Agent update notification failed: {e}")


async def notify_files_update(
    organized_files: Dict[str, Any],
    update_callback: Optional[UpdateCallback]
) -> None:
    """
    Send files update notification.
    
    Pure function that notifies about file changes.
    """
    if not update_callback:
        return
        
    await safe_notify(update_callback, {
        "type": "intermediate_update",
        "update_type": "files_update",
        "data": organized_files
    })


async def notify_canvas_files(
    canvas_files: List[Dict[str, Any]],
    update_callback: Optional[UpdateCallback]
) -> None:
    """
    Send canvas files notification.
    
    Pure function that notifies about canvas-displayable files.
    """
    if not update_callback or not canvas_files:
        return
        
    await safe_notify(update_callback, {
        "type": "intermediate_update",
        "update_type": "canvas_files",
        "data": {"files": canvas_files}
    })


def create_error_response(error_message: str, message_type: str = "error") -> Dict[str, str]:
    """
    Create standardized error response.
    
    Pure function that creates consistent error responses.
    """
    return {
        "type": message_type,
        "message": error_message
    }


def create_chat_response(message: str, message_type: str = "chat_response") -> Dict[str, str]:
    """
    Create standardized chat response.
    
    Pure function that creates consistent chat responses.
    """
    return {
        "type": message_type,
        "message": message
    }


