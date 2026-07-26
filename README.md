# cronometer-api-mcp

<!-- mcp-name: io.github.rwestergren/cronometer-api-mcp -->

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![CI](https://github.com/rwestergren/cronometer-api-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/rwestergren/cronometer-api-mcp/actions/workflows/ci.yml)
[![Build Docker image](https://github.com/rwestergren/cronometer-api-mcp/actions/workflows/docker.yml/badge.svg)](https://github.com/rwestergren/cronometer-api-mcp/actions/workflows/docker.yml)
[![PyPI](https://img.shields.io/pypi/v/cronometer-api-mcp.svg)](https://pypi.org/project/cronometer-api-mcp/)

> **Hosted version for Claude.ai, ChatGPT, and Grok coming soon.** [**Join the waitlist →**](https://tally.so/r/A7WVge?ref=cronometer-api-mcp)

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
- **Biometrics** -- weight, body fat, heart rate, and other tracked metrics over a date range

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
| `get_food_log` | Diary entries for a date, each enriched with food name, source, serving measure/count, and that food's per-entry nutrient contribution, plus an energy_summary (target/consumed/remaining kcal) and a nutrition_summary of consumed totals for every tracked nutrient |
| `get_daily_nutrition` | Consumed macro and micronutrient totals for every nutrient tracked in Cronometer |
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
| `list_biometrics` | List trackable biometric metrics and their units |
| `get_biometrics` | Biometric time series (e.g. weight, body fat) within a date range |

All date parameters use `YYYY-MM-DD` format and default to today when omitted.

## Transport

stdio only. For remote/hosted use, the stdio server is wrapped by
[supergateway](https://github.com/supercorp-ai/supergateway) (see `Dockerfile`),
which owns the HTTP listener and exposes MCP streamable-HTTP at `/mcp`. The
server has **no built-in authentication** — any remote deployment must sit
behind an authenticating gateway or reverse proxy.

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

- **v2 (`POST /api/v2/*`)** -- JSON-body auth, used for most operations (food search, diary read/write, nutrition, fasting, macros, biometrics)
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
