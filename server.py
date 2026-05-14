"""
TickTick MCP Server

Exposes TickTick task management as MCP tools for Claude.
Run with: python server.py (or configure in Claude Desktop settings)
"""

import asyncio
import json
import os
from pathlib import Path
from typing import Any

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
        data={
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
        },
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
    """Make an authenticated request, refreshing the token once if needed."""
    tokens = _load_tokens()

    async with httpx.AsyncClient() as client:
        async def _do_request(access_token: str):
            headers = {"Authorization": f"Bearer {access_token}"}
            url = f"{BASE_URL}{path}"
            return await client.request(
                method,
                url,
                headers=headers,
                json=json_body,
                params=params,
            )

        resp = await _do_request(tokens["access_token"])

        if resp.status_code == 401:
            tokens = await _refresh_tokens(client, tokens)
            resp = await _do_request(tokens["access_token"])

        if resp.status_code == 204:
            return None

        resp.raise_for_status()
        return resp.json() if resp.content else None


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="list_projects",
            description="List all TickTick projects (task lists/folders).",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        types.Tool(
            name="get_project_tasks",
            description="Get all tasks inside a specific TickTick project.",
            inputSchema={
                "type": "object",
                "properties": {
                    "project_id": {
                        "type": "string",
                        "description": "The ID of the project to fetch tasks from.",
                    }
                },
                "required": ["project_id"],
            },
        ),
        types.Tool(
            name="get_task",
            description="Get details of a single task by its ID.",
            inputSchema={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "The task ID."},
                    "project_id": {
                        "type": "string",
                        "description": "The project ID the task belongs to.",
                    },
                },
                "required": ["task_id", "project_id"],
            },
        ),
        types.Tool(
            name="create_task",
            description="Create a new task in TickTick.",
            inputSchema={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Task title (required)."},
                    "project_id": {
                        "type": "string",
                        "description": "Project ID to add the task to. Omit for inbox.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Task notes/description.",
                    },
                    "due_date": {
                        "type": "string",
                        "description": "Due date in ISO 8601 format, e.g. '2025-12-31T23:59:00+0000'.",
                    },
                    "priority": {
                        "type": "integer",
                        "description": "Priority: 0=none, 1=low, 3=medium, 5=high.",
                        "enum": [0, 1, 3, 5],
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of tag names to apply.",
                    },
                },
                "required": ["title"],
            },
        ),
        types.Tool(
            name="update_task",
            description=(
                "Update an existing task. Pass only the fields you want to change."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "The task ID."},
                    "project_id": {
                        "type": "string",
                        "description": "The project ID the task belongs to.",
                    },
                    "title": {"type": "string", "description": "New task title."},
                    "content": {"type": "string", "description": "New task notes."},
                    "due_date": {
                        "type": "string",
                        "description": "New due date in ISO 8601 format.",
                    },
                    "priority": {
                        "type": "integer",
                        "description": "Priority: 0=none, 1=low, 3=medium, 5=high.",
                        "enum": [0, 1, 3, 5],
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "New list of tags (replaces existing tags).",
                    },
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
                    "task_id": {"type": "string", "description": "The task ID."},
                    "project_id": {
                        "type": "string",
                        "description": "The project ID the task belongs to.",
                    },
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
                    "task_id": {"type": "string", "description": "The task ID."},
                    "project_id": {
                        "type": "string",
                        "description": "The project ID the task belongs to.",
                    },
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
                    "name": {
                        "type": "string",
                        "description": "Name of the new project.",
                    },
                    "color": {
                        "type": "string",
                        "description": "Hex color code, e.g. '#FF5733'.",
                    },
                    "view_mode": {
                        "type": "string",
                        "description": "Display mode: 'list', 'kanban', or 'timeline'.",
                        "enum": ["list", "kanban", "timeline"],
                    },
                },
                "required": ["name"],
            },
        ),
    ]


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------

@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    try:
        result = await _dispatch(name, arguments)
        return [types.TextContent(type="text", text=json.dumps(result, indent=2))]
    except httpx.HTTPStatusError as e:
        error = {"error": f"TickTick API error {e.response.status_code}: {e.response.text}"}
        return [types.TextContent(type="text", text=json.dumps(error))]
    except Exception as e:
        return [types.TextContent(type="text", text=json.dumps({"error": str(e)}))]


async def _dispatch(name: str, args: dict) -> Any:
    if name == "list_projects":
        return await _list_projects()
    elif name == "get_project_tasks":
        return await _get_project_tasks(args["project_id"])
    elif name == "get_task":
        return await _get_task(args["task_id"], args["project_id"])
    elif name == "create_task":
        return await _create_task(args)
    elif name == "update_task":
        return await _update_task(args)
    elif name == "complete_task":
        return await _complete_task(args["task_id"], args["project_id"])
    elif name == "delete_task":
        return await _delete_task(args["task_id"], args["project_id"])
    elif name == "create_project":
        return await _create_project(args)
    else:
        raise ValueError(f"Unknown tool: {name}")


# ---------------------------------------------------------------------------
# API calls
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
        body["dueDate"] = args["due_date"]
    if "priority" in args:
        body["priority"] = args["priority"]
    if "tags" in args:
        body["tags"] = args["tags"]
    return await _api("POST", "/task", json_body=body)


async def _update_task(args: dict) -> dict:
    task_id = args["task_id"]
    project_id = args["project_id"]

    # Fetch current task first so we can merge fields
    current = await _get_task(task_id, project_id)

    body: dict = {
        "id": task_id,
        "projectId": project_id,
        "title": args.get("title", current.get("title")),
    }
    if "content" in args:
        body["content"] = args["content"]
    elif "content" in current:
        body["content"] = current["content"]

    if "due_date" in args:
        body["dueDate"] = args["due_date"]
    elif "dueDate" in current:
        body["dueDate"] = current["dueDate"]

    if "priority" in args:
        body["priority"] = args["priority"]
    elif "priority" in current:
        body["priority"] = current["priority"]

    if "tags" in args:
        body["tags"] = args["tags"]
    elif "tags" in current:
        body["tags"] = current["tags"]

    return await _api("POST", f"/task/{task_id}", json_body=body)


async def _complete_task(task_id: str, project_id: str) -> dict:
    return await _api("POST", f"/project/{project_id}/task/{task_id}/complete")


async def _delete_task(task_id: str, project_id: str) -> None:
    await _api("DELETE", f"/project/{project_id}/task/{task_id}")
    return {"status": "deleted", "task_id": task_id}


async def _create_project(args: dict) -> dict:
    body: dict = {"name": args["name"]}
    if "color" in args:
        body["color"] = args["color"]
    if "view_mode" in args:
        body["viewMode"] = args["view_mode"]
    return await _api("POST", "/project", json_body=body)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
