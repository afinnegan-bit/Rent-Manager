# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=2.2,<3", "httpx>=0.27"]
# ///
"""Rent Manager MCP server.

Exposes the Rent Manager 12 REST API to Claude (Claude Desktop, Claude Code,
or any MCP client) as a set of read-only tools.

Configuration (environment variables):
    RM_CORP_ID      Your Rent Manager corporate ID, i.e. the "xxxx" in
                    https://xxxx.api.rentmanager.com
    RM_USERNAME     Rent Manager API / web-service user name
    RM_PASSWORD     Password for that user
    RM_LOCATION_ID  Location (database) ID to sign in to. Defaults to 1.
    RM_BASE_URL     Optional full base URL override (takes precedence over RM_CORP_ID)
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

TOKEN_HEADER = "X-RM12Api-ApiToken"
MAX_PAGE_SIZE = 1000
READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=True)

mcp = MCPServer(
    name="rent-manager",
    instructions=(
        "Read-only access to the company's Rent Manager 12 database: properties, "
        "units, tenants, charges/balances, prospects, owners and service issues "
        "(work orders). Use `rm_get` for any endpoint without a dedicated tool. "
        "Filters use Rent Manager syntax: 'Field,operator,value' joined with ';', "
        "e.g. 'Status,eq,Current;PropertyID,eq,12'."
    ),
)


class RentManagerClient:
    def __init__(self) -> None:
        base_url = os.environ.get("RM_BASE_URL")
        if not base_url:
            corp_id = os.environ.get("RM_CORP_ID")
            if not corp_id:
                raise RuntimeError("Set RM_CORP_ID (or RM_BASE_URL) in the server environment.")
            base_url = f"https://{corp_id}.api.rentmanager.com"
        self.username = os.environ.get("RM_USERNAME", "")
        self.password = os.environ.get("RM_PASSWORD", "")
        if not self.username or not self.password:
            raise RuntimeError("Set RM_USERNAME and RM_PASSWORD in the server environment.")
        self.location_id = int(os.environ.get("RM_LOCATION_ID", "1"))
        self.http = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=60)
        self.token: str | None = None

    async def _authenticate(self) -> None:
        resp = await self.http.post(
            "/Authentication/AuthorizeUser",
            json={
                "Username": self.username,
                "Password": self.password,
                "LocationID": self.location_id,
            },
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"Rent Manager login failed ({resp.status_code}): {resp.text[:500]}")
        # The API returns the token as a JSON-encoded string.
        self.token = resp.json() if resp.text.startswith('"') else resp.text.strip()

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        if not path.startswith("/"):
            path = "/" + path
        if path.lower().startswith("/authentication"):
            raise ValueError("Authentication endpoints are not exposed through this tool.")
        params = {k: v for k, v in (params or {}).items() if v not in (None, "")}
        for attempt in range(2):
            if self.token is None:
                await self._authenticate()
            resp = await self.http.get(path, params=params, headers={TOKEN_HEADER: self.token})
            if resp.status_code == 401 and attempt == 0:
                self.token = None  # token expired; sign in again once
                continue
            break
        if resp.status_code >= 400:
            raise RuntimeError(f"GET {path} failed ({resp.status_code}): {resp.text[:1000]}")
        return resp.json() if resp.content else None


_client: RentManagerClient | None = None


def client() -> RentManagerClient:
    global _client
    if _client is None:
        _client = RentManagerClient()
    return _client


async def _list(
    path: str,
    filters: str | None,
    fields: str | None,
    embeds: str | None,
    page_size: int,
    page_number: int,
) -> str:
    data = await client().get(
        path,
        {
            "filters": filters,
            "fields": fields,
            "embeds": embeds,
            "pagesize": max(1, min(page_size, MAX_PAGE_SIZE)),
            "pagenumber": max(1, page_number),
        },
    )
    return json.dumps(data, default=str)


@mcp.tool(annotations=READ_ONLY)
async def rm_get(
    path: str,
    filters: str | None = None,
    fields: str | None = None,
    embeds: str | None = None,
    page_size: int | None = None,
    page_number: int | None = None,
) -> str:
    """GET any Rent Manager API endpoint, e.g. '/Tenants/123', '/Units', '/Leases'.

    filters: 'Field,op,value' clauses joined by ';' (ops include eq, ne, gt, lt, ge, le, ct, in).
    fields: comma-separated fields to return. embeds: comma-separated related objects to include.
    """
    data = await client().get(
        path,
        {
            "filters": filters,
            "fields": fields,
            "embeds": embeds,
            "pagesize": page_size,
            "pagenumber": page_number,
        },
    )
    return json.dumps(data, default=str)


@mcp.tool(annotations=READ_ONLY)
async def list_properties(
    filters: str | None = None,
    fields: str | None = None,
    embeds: str | None = None,
    page_size: int = 100,
    page_number: int = 1,
) -> str:
    """List properties. Example filter: 'IsActive,eq,true'."""
    return await _list("/Properties", filters, fields, embeds, page_size, page_number)


@mcp.tool(annotations=READ_ONLY)
async def list_units(
    filters: str | None = None,
    fields: str | None = None,
    embeds: str | None = None,
    page_size: int = 100,
    page_number: int = 1,
) -> str:
    """List units. Example filter: 'PropertyID,eq,12'. Useful embeds: 'UnitType,Property'."""
    return await _list("/Units", filters, fields, embeds, page_size, page_number)


@mcp.tool(annotations=READ_ONLY)
async def list_tenants(
    filters: str | None = None,
    fields: str | None = None,
    embeds: str | None = None,
    page_size: int = 100,
    page_number: int = 1,
) -> str:
    """List tenants. Example filter: 'Status,eq,Current'. Useful embeds: 'Leases,Contacts,Balance'."""
    return await _list("/Tenants", filters, fields, embeds, page_size, page_number)


@mcp.tool(annotations=READ_ONLY)
async def get_tenant(tenant_id: int, embeds: str | None = None) -> str:
    """Get one tenant by TenantID. Useful embeds: 'Leases,Contacts,Balance,Charges,Payments'."""
    data = await client().get(f"/Tenants/{tenant_id}", {"embeds": embeds})
    return json.dumps(data, default=str)


@mcp.tool(annotations=READ_ONLY)
async def list_charges(
    filters: str | None = None,
    fields: str | None = None,
    embeds: str | None = None,
    page_size: int = 100,
    page_number: int = 1,
) -> str:
    """List charges. Example filter: 'TransactionDate,ge,2026-01-01'."""
    return await _list("/Charges", filters, fields, embeds, page_size, page_number)


@mcp.tool(annotations=READ_ONLY)
async def list_service_issues(
    filters: str | None = None,
    fields: str | None = None,
    embeds: str | None = None,
    page_size: int = 100,
    page_number: int = 1,
) -> str:
    """List Service Manager issues (work orders). Example filter: 'IsClosed,eq,false'."""
    return await _list("/ServiceManagerIssues", filters, fields, embeds, page_size, page_number)


@mcp.tool(annotations=READ_ONLY)
async def list_prospects(
    filters: str | None = None,
    fields: str | None = None,
    embeds: str | None = None,
    page_size: int = 100,
    page_number: int = 1,
) -> str:
    """List prospects (leads/applicants)."""
    return await _list("/Prospects", filters, fields, embeds, page_size, page_number)


@mcp.tool(annotations=READ_ONLY)
async def list_owners(
    filters: str | None = None,
    fields: str | None = None,
    embeds: str | None = None,
    page_size: int = 100,
    page_number: int = 1,
) -> str:
    """List property owners."""
    return await _list("/Owners", filters, fields, embeds, page_size, page_number)


if __name__ == "__main__":
    mcp.run()
