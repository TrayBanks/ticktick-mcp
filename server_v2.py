"""
TickTick MCP Server - V2

New in V2:
- Sub-task support (add, complete, list sub-tasks)
- Smart lists (Today, Tomorrow, This Week)
- Bulk operations (complete/delete/move multiple tasks at once)
- Natural language date parsing ("next Friday", "tomorrow at 3pm")
- Improved error messages
"""

import asyncio
import json
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import dateparser
import httpx
from dotenv import load_dotenv
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types

load_dotenv()

BASE_URL = "https://ticktick.com/open/v1"
TOKEN_URL = "https://ticktick.com/oauth/token"
TOKENS_FILE = Path(__file__).parent / ".tokens.json"

server = Server("ticktick")


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

TICKTICK_ERROR_CODES = {
    "invalid_client": "Invalid client credentials. Check your TICKTICK_CLIENT_ID and TICKTICK_CLIENT_SECRET.",
    "invalid_token": "Access token is invalid or expired. Run auth.py again.",
    "not_found": "The requested task or project was not found.",
    "forbidden": "You don't have permission to perform this action.",
    "rate_limit": "Too many requests. Please slow down.",
}

TICKTICK_HTTP_ERRORS = {
    400: "Bad request — one or more fields are invalid.",
    401: "Unauthorized — token expired or invalid. Run auth.py again.",
    403: "Forbidden — you don't have access to this resource.",
    404: "Not found — task or project ID doesn't exist.",
    429: "Rate limited — too many requests. Try again in a moment.",
    500: "TickTick server error — try again later.",
}


def _parse_api_error(status_code: int, body: str) -> str:
    """Turn a raw API error into a human-readable message."""
    try:
        data = json.loads(body)
        for key in ("errorMessage", "message", "error_description", "error"):
            if key in data:
                msg = data[key]
                code = data.get("errorCode", data.get("error", ""))
                friendly = TICKTICK_ERROR_CODES.get(str(code), "")
                return f"{friendly} (API: {msg})" if friendly else str(msg)
    except (json.JSONDecodeError, TypeError):
        pass
    return TICKTICK_HTTP_ERRORS.get(status_code, f"HTTP {status_code}: {body[:200]}")


# ---------------------------------------------------------------------------
# Natural language date parsing
# ---------------------------------------------------------------------------

def _parse_date(date_str: str | None) -> str | None:
    """
    Accept natural language or ISO dates and return TickTick's expected format.
    Examples: "tomorrow", "next Friday", "in 3 days", "Dec 31", "2025-12-31T23:59:00+0000"
    """
    if not date_str:
        return None

    # Already looks like a full ISO datetime — pass through
    if "T" in date_str and len(date_str) > 16:
        return date_str

    parsed = dateparser.parse(
        date_str,
        settings={
            "PREFER_DATES_FROM": "future",
            "RETURN_AS_TIMEZONE_AWARE": True,
        },
    )
    if not parsed:
        raise ValueError(
            f"Could not understand date '{date_str}'. "
            "Try 'tomorrow', 'next Friday', 'Dec 31', or '2025-12-31T23:59:00+0000'."
        )
    return parsed.strftime("%Y-%m-%dT%H:%M:%S+0000")


# ---------------------------------------------------------------------------
# Token management
# ---------------------------------------------------------------------------

def _load_tokens() -> dict:
    if not TOKENS_FILE.exists():
        raise RuntimeError(
            "No tokens found. Run `python auth.py` first to authorize the MCP server."
        )
    return json.loads(TOKENS_FILE.read_text())


def _save_tokens(tokens: dict) -> None:
    TOKENS_FILE.write_text(json.dumps(tokens, indent=2))


async def _refresh_tokens(client: httpx.AsyncClient, tokens: dict) -> dict:
    client_id = os.getenv("TICKTICK_CLIENT_ID")
    client_secret = os.getenv("TICKTICK_CLIENT_SECRET")
    response = await client.post(
        TOKEN_URL,
        auth=(client_id, client_secret),
        data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]},
    )
    response.raise_for_status()
    new_tokens = response.json()
    _save_tokens(new_tokens)
    return new_tokens


async def _api(
    method: str,
    path: str,
    *,
    json_body: dict | None = None,
    params: dict | None = None,
) -> Any:
    """Authenticated request with automatic token refresh and friendly errors."""
    tokens = _load_tokens()

    async with httpx.AsyncClient() as client:
        async def _do_request(access_token: str):
            return await client.request(
                method,
                f"{BASE_URL}{path}",
                headers={"Authorization": f"Bearer {access_token}"},
                json=json_body,
                params=params,
            )

        resp = await _do_request(tokens["access_token"])

        if resp.status_code == 401:
            tokens = await _refresh_tokens(client, tokens)
            resp = await _do_request(tokens["access_token"])

        if resp.status_code == 204:
            return None

        if not resp.is_success:
            raise httpx.HTTPStatusError(
                _parse_api_error(resp.status_code, resp.text),
                request=resp.request,
                response=resp,
            )

        return resp.json() if resp.content else None


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        # ── Core ──────────────────────────────────────────────────────────
        types.Tool(
            name="list_projects",
            description="List all TickTick projects (task lists/folders).",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        types.Tool(
            name="get_project_tasks",
            description="Get all tasks (including sub-tasks) inside a specific project.",
            inputSchema={
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "The project ID."}
                },
                "required": ["project_id"],
            },
        ),
        types.Tool(
            name="get_task",
            description="Get full details of a task including any sub-tasks.",
            inputSchema={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "project_id": {"type": "string"},
                },
                "required": ["task_id", "project_id"],
            },
        ),
        types.Tool(
            name="create_task",
            description=(
                "Create a new task. Due dates accept natural language like "
                "'tomorrow', 'next Friday', 'in 3 days', or ISO format."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "project_id": {"type": "string", "description": "Omit to add to inbox."},
                    "content": {"type": "string", "description": "Notes/description."},
                    "due_date": {"type": "string", "description": "e.g. 'tomorrow', 'next Friday'."},
                    "priority": {"type": "integer", "enum": [0, 1, 3, 5], "description": "0=none 1=low 3=medium 5=high."},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "subtasks": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Sub-task titles to create alongside the task.",
                    },
                },
                "required": ["title"],
            },
        ),
        types.Tool(
            name="update_task",
            description="Update an existing task. Pass only the fields to change. Due dates accept natural language.",
            inputSchema={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "project_id": {"type": "string"},
                    "title": {"type": "string"},
                    "content": {"type": "string"},
                    "due_date": {"type": "string"},
                    "priority": {"type": "integer", "enum": [0, 1, 3, 5]},
                    "tags": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["task_id", "project_id"],
            },
        ),
        types.Tool(
            name="complete_task",
            description="Mark a task as completed.",
            inputSchema={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "project_id": {"type": "string"},
                },
                "required": ["task_id", "project_id"],
            },
        ),
        types.Tool(
            name="delete_task",
            description="Permanently delete a task.",
            inputSchema={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "project_id": {"type": "string"},
                },
                "required": ["task_id", "project_id"],
            },
        ),
        types.Tool(
            name="create_project",
            description="Create a new TickTick project (list/folder).",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "color": {"type": "string", "description": "Hex color e.g. '#FF5733'."},
                    "view_mode": {"type": "string", "enum": ["list", "kanban", "timeline"]},
                },
                "required": ["name"],
            },
        ),
        # ── Sub-tasks ─────────────────────────────────────────────────────
        types.Tool(
            name="add_subtask",
            description="Add a sub-task to an existing task.",
            inputSchema={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "project_id": {"type": "string"},
                    "title": {"type": "string", "description": "Sub-task title."},
                },
                "required": ["task_id", "project_id", "title"],
            },
        ),
        types.Tool(
            name="complete_subtask",
            description="Mark a specific sub-task as complete.",
            inputSchema={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "Parent task ID."},
                    "project_id": {"type": "string"},
                    "subtask_id": {"type": "string", "description": "Sub-task ID."},
                },
                "required": ["task_id", "project_id", "subtask_id"],
            },
        ),
        # ── Smart lists ───────────────────────────────────────────────────
        types.Tool(
            name="get_smart_list",
            description="Get incomplete tasks from a smart view: today, tomorrow, or this_week.",
            inputSchema={
                "type": "object",
                "properties": {
                    "list_name": {
                        "type": "string",
                        "enum": ["today", "tomorrow", "this_week"],
                    }
                },
                "required": ["list_name"],
            },
        ),
        # ── Bulk operations ───────────────────────────────────────────────
        types.Tool(
            name="bulk_complete_tasks",
            description="Complete multiple tasks at once.",
            inputSchema={
                "type": "object",
                "properties": {
                    "tasks": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "task_id": {"type": "string"},
                                "project_id": {"type": "string"},
                            },
                            "required": ["task_id", "project_id"],
                        },
                    }
                },
                "required": ["tasks"],
            },
        ),
        types.Tool(
            name="bulk_delete_tasks",
            description="Delete multiple tasks at once.",
            inputSchema={
                "type": "object",
                "properties": {
                    "tasks": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "task_id": {"type": "string"},
                                "project_id": {"type": "string"},
                            },
                            "required": ["task_id", "project_id"],
                        },
                    }
                },
                "required": ["tasks"],
            },
        ),
        types.Tool(
            name="bulk_move_tasks",
            description="Move multiple tasks to a different project.",
            inputSchema={
                "type": "object",
                "properties": {
                    "task_ids": {"type": "array", "items": {"type": "string"}},
                    "from_project_id": {"type": "string"},
                    "to_project_id": {"type": "string"},
                },
                "required": ["task_ids", "from_project_id", "to_project_id"],
            },
        ),
    ]


# ---------------------------------------------------------------------------
# Tool handler
# ---------------------------------------------------------------------------

@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    try:
        result = await _dispatch(name, arguments)
        return [types.TextContent(type="text", text=json.dumps(result, indent=2))]
    except ValueError as e:
        return [types.TextContent(type="text", text=json.dumps({"error": str(e)}))]
    except httpx.HTTPStatusError as e:
        return [types.TextContent(type="text", text=json.dumps({"error": str(e)}))]
    except Exception as e:
        return [types.TextContent(type="text", text=json.dumps({"error": f"Unexpected error: {str(e)}"}))]


async def _dispatch(name: str, args: dict) -> Any:
    match name:
        case "list_projects":         return await _list_projects()
        case "get_project_tasks":     return await _get_project_tasks(args["project_id"])
        case "get_task":              return await _get_task(args["task_id"], args["project_id"])
        case "create_task":           return await _create_task(args)
        case "update_task":           return await _update_task(args)
        case "complete_task":         return await _complete_task(args["task_id"], args["project_id"])
        case "delete_task":           return await _delete_task(args["task_id"], args["project_id"])
        case "create_project":        return await _create_project(args)
        case "add_subtask":           return await _add_subtask(args["task_id"], args["project_id"], args["title"])
        case "complete_subtask":      return await _complete_subtask(args["task_id"], args["project_id"], args["subtask_id"])
        case "get_smart_list":        return await _get_smart_list(args["list_name"])
        case "bulk_complete_tasks":   return await _bulk_complete_tasks(args["tasks"])
        case "bulk_delete_tasks":     return await _bulk_delete_tasks(args["tasks"])
        case "bulk_move_tasks":       return await _bulk_move_tasks(args["task_ids"], args["from_project_id"], args["to_project_id"])
        case _:                       raise ValueError(f"Unknown tool: {name}")


# ---------------------------------------------------------------------------
# API implementations
# ---------------------------------------------------------------------------

async def _list_projects() -> list[dict]:
    return await _api("GET", "/project")


async def _get_project_tasks(project_id: str) -> dict:
    return await _api("GET", f"/project/{project_id}/data")


async def _get_task(task_id: str, project_id: str) -> dict:
    return await _api("GET", f"/task/{task_id}", params={"projectId": project_id})


async def _create_task(args: dict) -> dict:
    body: dict = {"title": args["title"]}
    if "project_id" in args:
        body["projectId"] = args["project_id"]
    if "content" in args:
        body["content"] = args["content"]
    if "due_date" in args:
        body["dueDate"] = _parse_date(args["due_date"])
    if "priority" in args:
        body["priority"] = args["priority"]
    if "tags" in args:
        body["tags"] = args["tags"]
    if "subtasks" in args:
        body["items"] = [
            {"title": t, "status": 0, "sortOrder": i * 100000}
            for i, t in enumerate(args["subtasks"])
        ]
    return await _api("POST", "/task", json_body=body)


async def _update_task(args: dict) -> dict:
    task_id = args["task_id"]
    project_id = args["project_id"]
    current = await _get_task(task_id, project_id)

    body: dict = {
        "id": task_id,
        "projectId": project_id,
        "title": args.get("title", current.get("title")),
    }
    for field, api_key in [("content", "content"), ("priority", "priority"), ("tags", "tags")]:
        body[api_key] = args[field] if field in args else current.get(api_key)

    body["dueDate"] = _parse_date(args["due_date"]) if "due_date" in args else current.get("dueDate")

    # Always preserve existing sub-tasks
    if "items" in current:
        body["items"] = current["items"]

    return await _api("POST", f"/task/{task_id}", json_body=body)


async def _complete_task(task_id: str, project_id: str) -> dict:
    result = await _api("POST", f"/project/{project_id}/task/{task_id}/complete")
    return result or {"status": "completed", "task_id": task_id}


async def _delete_task(task_id: str, project_id: str) -> dict:
    await _api("DELETE", f"/project/{project_id}/task/{task_id}")
    return {"status": "deleted", "task_id": task_id}


async def _create_project(args: dict) -> dict:
    body: dict = {"name": args["name"]}
    if "color" in args:
        body["color"] = args["color"]
    if "view_mode" in args:
        body["viewMode"] = args["view_mode"]
    return await _api("POST", "/project", json_body=body)


# ── Sub-tasks ────────────────────────────────────────────────────────────────

async def _add_subtask(task_id: str, project_id: str, title: str) -> dict:
    current = await _get_task(task_id, project_id)
    items = current.get("items", [])
    items.append({"title": title, "status": 0, "sortOrder": (len(items) + 1) * 100000})
    body = {**current, "id": task_id, "projectId": project_id, "items": items}
    result = await _api("POST", f"/task/{task_id}", json_body=body)
    return result or {"status": "subtask_added", "title": title}


async def _complete_subtask(task_id: str, project_id: str, subtask_id: str) -> dict:
    current = await _get_task(task_id, project_id)
    items = current.get("items", [])
    found = False
    for item in items:
        if item.get("id") == subtask_id:
            item["status"] = 2
            item["completedTime"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+0000")
            found = True
            break
    if not found:
        raise ValueError(f"Sub-task '{subtask_id}' not found on task '{task_id}'.")
    body = {**current, "id": task_id, "projectId": project_id, "items": items}
    await _api("POST", f"/task/{task_id}", json_body=body)
    return {"status": "subtask_completed", "subtask_id": subtask_id}


# ── Smart lists ──────────────────────────────────────────────────────────────

def _in_date_range(task: dict, start: date, end: date) -> bool:
    due = task.get("dueDate")
    if not due:
        return False
    try:
        dt = datetime.fromisoformat(due.replace("+0000", "+00:00"))
        return start <= dt.date() <= end
    except (ValueError, TypeError):
        return False


async def _get_smart_list(list_name: str) -> dict:
    today = date.today()
    if list_name == "today":
        start, end = today, today
    elif list_name == "tomorrow":
        d = today + timedelta(days=1)
        start, end = d, d
    elif list_name == "this_week":
        start = today - timedelta(days=today.weekday())  # Monday
        end = start + timedelta(days=6)                  # Sunday
    else:
        raise ValueError(f"Unknown list '{list_name}'. Use 'today', 'tomorrow', or 'this_week'.")

    projects = await _list_projects()
    matched: list[dict] = []

    async def fetch(project: dict):
        try:
            data = await _get_project_tasks(project["id"])
            tasks = data.get("tasks", []) if isinstance(data, dict) else []
            for task in tasks:
                if task.get("status", 0) == 0 and _in_date_range(task, start, end):
                    matched.append({**task, "projectName": project.get("name", "")})
        except Exception:
            pass

    await asyncio.gather(*[fetch(p) for p in projects])
    matched.sort(key=lambda t: t.get("dueDate", ""))
    return {
        "list": list_name,
        "range": f"{start.isoformat()} to {end.isoformat()}",
        "count": len(matched),
        "tasks": matched,
    }


# ── Bulk operations ──────────────────────────────────────────────────────────

async def _bulk_complete_tasks(tasks: list[dict]) -> dict:
    results = await asyncio.gather(
        *[_complete_task(t["task_id"], t["project_id"]) for t in tasks],
        return_exceptions=True,
    )
    return _bulk_summary("completed", [t["task_id"] for t in tasks], results)


async def _bulk_delete_tasks(tasks: list[dict]) -> dict:
    results = await asyncio.gather(
        *[_delete_task(t["task_id"], t["project_id"]) for t in tasks],
        return_exceptions=True,
    )
    return _bulk_summary("deleted", [t["task_id"] for t in tasks], results)


async def _bulk_move_tasks(task_ids: list[str], from_project_id: str, to_project_id: str) -> dict:
    async def move(task_id: str):
        current = await _get_task(task_id, from_project_id)
        body = {**current, "id": task_id, "projectId": to_project_id}
        return await _api("POST", f"/task/{task_id}", json_body=body)

    results = await asyncio.gather(*[move(tid) for tid in task_ids], return_exceptions=True)
    return _bulk_summary("moved", task_ids, results)


def _bulk_summary(action: str, task_ids: list[str], results: list) -> dict:
    succeeded, failed = [], []
    for tid, result in zip(task_ids, results):
        if isinstance(result, Exception):
            failed.append({"task_id": tid, "error": str(result)})
        else:
            succeeded.append(tid)
    return {
        "action": action,
        "succeeded": len(succeeded),
        "failed": len(failed),
        "succeeded_ids": succeeded,
        "failures": failed,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
