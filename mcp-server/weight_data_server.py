#!/usr/bin/env python3
"""
Minimal MCP (Model Context Protocol) server exposing the Weight Tracker's
live Gist data as a tool, for Claude Desktop. Stdlib-only (no pip installs)
so it runs with the system python3 as-is.

Talks JSON-RPC 2.0 over stdio, one message per line, per the MCP spec.
Register in Claude Desktop's config (~/Library/Application Support/Claude/
claude_desktop_config.json) under "mcpServers".
"""
import json
import sys
import time
import urllib.request

GIST_USER = 'halfassedapps'
GIST_ID = 'aa6e7ae2259f94c7a91637b447a0cd99'
GIST_FILE = 'weight-data.json'
RAW_URL = f'https://gist.githubusercontent.com/{GIST_USER}/{GIST_ID}/raw/{GIST_FILE}'

TOOLS = [{
    "name": "get_weight_data",
    "description": (
        "Fetches the current Weight Tracker data: daily body weight entries (wKg, "
        "kilograms), tirzepatide injections (date + doseMg per shot), the injection "
        "schedule, and daily calorie/activity entries. Always pulls the live Gist "
        "fresh (cache-busted), so this reflects today's data if it's been synced."
    ),
    "inputSchema": {"type": "object", "properties": {}},
}]


def fetch_weight_data():
    url = f'{RAW_URL}?t={int(time.time() * 1000)}'
    req = urllib.request.Request(url, headers={'Cache-Control': 'no-cache'})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode('utf-8'))
    # intervalsCfg.apiKey is a live credential for an unrelated service (intervals.icu)
    # — never surface it through this tool.
    data.pop('intervalsCfg', None)
    return data


def send(msg):
    sys.stdout.write(json.dumps(msg) + '\n')
    sys.stdout.flush()


def handle(req):
    method = req.get('method')
    req_id = req.get('id')

    if method == 'initialize':
        send({
            "jsonrpc": "2.0", "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "weight-tracker", "version": "1.0.0"},
            },
        })
    elif method == 'notifications/initialized':
        pass  # no response for notifications
    elif method == 'tools/list':
        send({"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}})
    elif method == 'tools/call':
        name = req.get('params', {}).get('name')
        if name != 'get_weight_data':
            send({"jsonrpc": "2.0", "id": req_id,
                  "error": {"code": -32602, "message": f"Unknown tool: {name}"}})
            return
        try:
            data = fetch_weight_data()
            send({
                "jsonrpc": "2.0", "id": req_id,
                "result": {"content": [{"type": "text", "text": json.dumps(data, indent=2)}]},
            })
        except Exception as e:
            send({
                "jsonrpc": "2.0", "id": req_id,
                "result": {"content": [{"type": "text", "text": f"Fetch failed: {e}"}], "isError": True},
            })
    elif req_id is not None:
        send({"jsonrpc": "2.0", "id": req_id,
              "error": {"code": -32601, "message": f"Method not found: {method}"}})


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            handle(req)
        except Exception as e:
            if req.get('id') is not None:
                send({"jsonrpc": "2.0", "id": req["id"],
                      "error": {"code": -32603, "message": str(e)}})


if __name__ == '__main__':
    main()
