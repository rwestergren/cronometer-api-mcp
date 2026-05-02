# cronometer-api-mcp

<!-- mcp-name: io.github.rwestergren/cronometer-api-mcp -->

An [MCP (Model Context Protocol)](https://modelcontextprotocol.io/) server for [Cronometer](https://cronometer.com/) nutrition tracking, built on the reverse-engineered mobile REST API.

Unlike [cronometer-mcp](https://github.com/cphoskins/cronometer-mcp), which takes a comprehensive GWT-RPC approach against Cronometer's web backend, this server talks to the same JSON REST API used by the Cronometer Android app -- with clean payloads and stable, versioned endpoints.

## Features

- **Food log** -- diary entries with food names, amounts, meal groups
- **Nutrition data** -- daily macro/micro totals and nutrition scores with per-nutrient confidence
- **Food search** -- search the Cronometer food database, get detailed nutrition info
- **Diary management** -- add/remove entries, copy days, mark days complete
- **Custom foods** -- create foods with custom nutrition data
- **Macro targets** -- read weekly schedule and saved templates
- **Fasting** -- view history and aggregate statistics

## Quick Start

### 1. Install [uv](https://docs.astral.sh/uv/)

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### 2. Set credentials

```bash
export CRONOMETER_USERNAME="your@email.com"
export CRONOMETER_PASSWORD="your-password"
```

### 3. Configure your MCP client

`uvx` downloads and runs the server on demand -- no separate install step.

#### OpenCode (`opencode.json`)

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "cronometer": {
      "type": "local",
      "command": ["uvx", "cronometer-api-mcp"],
      "environment": {
        "CRONOMETER_USERNAME": "{env:CRONOMETER_USERNAME}",
        "CRONOMETER_PASSWORD": "{env:CRONOMETER_PASSWORD}"
      },
      "enabled": true
    }
  }
}
```

#### Claude Desktop (`claude_desktop_config.json`)

```json
{
  "mcpServers": {
    "cronometer": {
      "command": "uvx",
      "args": ["cronometer-api-mcp"],
      "env": {
        "CRONOMETER_USERNAME": "your@email.com",
        "CRONOMETER_PASSWORD": "your-password"
      }
    }
  }
}
```

## Available Tools

### Food Log & Nutrition

| Tool | Description |
|------|-------------|
| `get_food_log` | Diary entries for a date with food names, amounts, and meal groups, plus an energy_summary (target/consumed/remaining kcal) |
| `get_daily_nutrition` | Daily macro and micronutrient totals |
| `get_nutrition_scores` | Category scores (Vitamins, Minerals, etc.) with per-nutrient consumed amounts and confidence levels |

### Food Search & Details

| Tool | Description |
|------|-------------|
| `search_foods` | Search the Cronometer food database by name |
| `get_food_details` | Full nutrition profile and serving sizes for a food |

### Diary Management

| Tool | Description |
|------|-------------|
| `add_food_entry` | Log a food serving to the diary |
| `remove_food_entry` | Remove one or more diary entries |
| `add_custom_food` | Create a custom food with specified nutrition |
| `copy_day` | Copy all entries from the previous day |
| `mark_day_complete` | Mark a diary day as complete or incomplete |

### Targets & Tracking

| Tool | Description |
|------|-------------|
| `get_macro_targets` | Weekly macro schedule and saved target templates |
| `get_fasting_history` | Fasting history within a date range |
| `get_fasting_stats` | Aggregate fasting statistics |

All date parameters use `YYYY-MM-DD` format and default to today when omitted.

## Remote Deployment

The server supports remote deployment with OAuth 2.1 authorization (PKCE) for use with Claude.ai and other remote MCP clients.

### Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `CRONOMETER_USERNAME` | Yes | Cronometer account email |
| `CRONOMETER_PASSWORD` | Yes | Cronometer account password |
| `MCP_TRANSPORT` | No | Transport mode: `stdio` (default), `sse`, or `streamable-http` |
| `MCP_AUTH_TOKEN` | No | Bearer token for remote auth (enables OAuth flow) |
| `MCP_OAUTH_CLIENT_ID` | No | OAuth client ID for remote clients |
| `MCP_OAUTH_CLIENT_SECRET` | No | OAuth client secret for remote clients |
| `MCP_BASE_URL` | No | Public base URL for OAuth metadata endpoints |
| `PORT` | No | Listen port for remote transports (default 8000) |

### Dokku / Heroku Deployment

The project includes a `Procfile` and `.python-version` for direct deployment with the Heroku Python buildpack:

```bash
# Create app
dokku apps:create cronometer-api-mcp

# Set environment
dokku config:set cronometer-api-mcp \
  MCP_TRANSPORT=streamable-http \
  MCP_AUTH_TOKEN=$(openssl rand -hex 32) \
  MCP_OAUTH_CLIENT_ID=my-client \
  MCP_OAUTH_CLIENT_SECRET=$(openssl rand -hex 32) \
  MCP_BASE_URL=https://your-domain.com \
  CRONOMETER_USERNAME=your@email.com \
  CRONOMETER_PASSWORD=your-password

# Deploy
git push dokku main
```

### Claude.ai Remote Connection

When deployed remotely with OAuth configured, connect from Claude.ai using:

- **Server URL**: `https://your-domain.com/mcp`
- **OAuth Client ID**: Value of `MCP_OAUTH_CLIENT_ID`
- **OAuth Client Secret**: Value of `MCP_OAUTH_CLIENT_SECRET`

Claude.ai will open a browser tab for authorization. Click **Authorize** to complete the connection.

## Development

For local development, copy `.env.example` to `.env` and fill in your credentials:

```bash
cp .env.example .env
# edit .env
uv run cronometer-api-mcp
```

The CLI auto-loads `.env` on startup (dev convenience only). Real environment variables always win over `.env`, so production deployments and MCP client `env` blocks are unaffected.

## How It Works

This server communicates with `mobile.cronometer.com` -- the same REST API used by the Cronometer Android/Flutter app. The API was reverse-engineered through:

1. Static analysis of `libapp.so` (Dart AOT snapshot) from the APK to discover endpoint names
2. Traffic interception via Frida + mitmproxy to capture exact request/response formats
3. Trial-and-error against the live API to confirm payload shapes

The API uses two protocols:

- **v2 (`POST /api/v2/*`)** -- JSON-body auth, used for most operations (food search, diary read/write, nutrition, fasting, macros)
- **v3 (`DELETE /api/v3/user/{id}/*`)** -- Header-based auth (`x-crono-session`), used for diary entry deletion

## Python API

You can use the client directly:

```python
from cronometer_api_mcp.client import CronometerClient
from datetime import date

client = CronometerClient()

# Search for foods
results = client.search_food("chicken breast")

# Get food details
food = client.get_food(results[0]["id"])

# Log a serving
client.add_serving(
    food_id=food["id"],
    measure_id=food["defaultMeasureId"],
    grams=200,
)

# Get today's diary
diary = client.get_diary()

# Get nutrition scores
scores = client.get_nutrition_scores()
```

## License

MIT
