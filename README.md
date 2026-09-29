# Rent Manager connector for Claude

A small [MCP](https://modelcontextprotocol.io) server that lets Claude read your Rent Manager 12 data (properties, units, tenants, charges, work orders, prospects and owners) through the Rent Manager REST API.

It is **read-only**. Claude can look things up and analyze them, but it can't post charges, edit tenants or change anything else.

## 1. Get Rent Manager API access

You need three things from Rent Manager:

1. **API access turned on for your company.** It isn't on by default. Ask your Rent Manager account rep or support for REST API / web-service access.
2. **Your corporate ID.** It's the `xxxx` in `https://xxxx.api.rentmanager.com`, which is usually the same as your Rent Manager login URL.
3. **A dedicated API user** (username and password). Give it read-only permissions in Rent Manager, and note the **Location ID** it should sign in to (usually `1`).

## 2. Install `uv`

`uv` runs the server and installs its dependencies on its own.

- macOS: `brew install uv`, or `curl -LsSf https://astral.sh/uv/install.sh | sh`
- Windows (PowerShell): `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`

Then download this repo, or just `server.py`, to your computer.

## 3. Connect it to Claude

### Claude Desktop

Go to **Settings → Developer → Edit Config** and add:

```json
{
  "mcpServers": {
    "rent-manager": {
      "command": "uv",
      "args": ["run", "/FULL/PATH/TO/Rent-Manager/server.py"],
      "env": {
        "RM_CORP_ID": "yourcorpid",
        "RM_USERNAME": "api-user",
        "RM_PASSWORD": "api-password",
        "RM_LOCATION_ID": "1"
      }
    }
  }
}
```

Restart Claude Desktop. **rent-manager** should show up under the tools/connectors menu.

> If Claude Desktop reports `uv` not found, replace `"uv"` with its full path. Run `which uv` on macOS or `where uv` on Windows to find it.

### Claude Code

```bash
claude mcp add rent-manager \
  -e RM_CORP_ID=yourcorpid -e RM_USERNAME=api-user -e RM_PASSWORD=api-password -e RM_LOCATION_ID=1 \
  -- uv run /FULL/PATH/TO/Rent-Manager/server.py
```

## 4. Try it

- "List all my properties and how many units each has."
- "Which current tenants have a balance over $1,000?"
- "Show open work orders at property 12, oldest first."

## Tools

| Tool | What it does |
| --- | --- |
| `list_properties`, `list_units`, `list_tenants`, `list_charges`, `list_service_issues`, `list_prospects`, `list_owners` | List records, with `filters`, `fields`, `embeds` and paging |
| `get_tenant` | Get one tenant by ID, optionally with leases, contacts or balance |
| `rm_get` | GET any other Rent Manager endpoint, such as `/Leases` or `/Payments` |

Filters use Rent Manager syntax: `Field,operator,value`, with clauses joined by `;`. For example: `Status,eq,Current;PropertyID,eq,12`.

## Security notes

- Your credentials stay in your local Claude config and are only sent to `*.api.rentmanager.com`. Don't commit them to this repo.
- Use a dedicated, least-privilege Rent Manager user so you can revoke it on its own.
- Data Claude reads (for example, tenant PII) is sent to Claude as part of the conversation. Follow your company's data policies.

## Vendor contact report

`vendor_report.py` lists every vendor billed to a group of properties over the last 12 months, with their contacts from Rent Manager, and saves it as Excel and CSV. It uses the same `RM_*` environment variables.

```bash
uv run vendor_report.py                      # property group named "AG"
uv run vendor_report.py --name-prefix AG     # or: ShortName starts with "AG"
uv run vendor_report.py --property-ids 1,2,3 --months 12 --out AG_vendors.xlsx
```

## V-COH rehab status report

`vcoh_report.py` rebuilds the "CPCM V-COH Inventory" report for one community (the API can't run Report Writer reports) and adds rehab spend. For each home in Make Ready, Delivered or Ready status it shows purchase price, rehab and utility bills (tagged to the home, or naming its lot in the bill comment), the Make Ready service issue's checklist progress, and the latest dated note.

```bash
RM_TOKEN_FILE=~/.rm_token uv run vcoh_report.py --community CDMH --out CDMH_VCOH.xlsx
```

`RM_TOKEN_FILE` reuses one Rent Manager session between runs. Rent Manager limits how many sessions a user can have open at once.
