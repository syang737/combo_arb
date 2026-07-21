# Monitoring combo-arb from Claude Code (MCP)

A small **read-only** MCP server lets you ask Claude Code about the running engine
— *"what's my paper PnL?"*, *"show the top near-misses"*, *"any flagged signals
today?"* — instead of writing SQL by hand. It only reads `combo_arb.db` (opened in
read-only mode); it can never write, change config, or trade.

## Install

```bash
pip install -e ".[mcp]"      # adds the MCP SDK + the combo-arb-mcp entry point
```

## Point it at your database

Resolution order: `--db` flag → `$COMBO_ARB_DB` → config `persistence.db_path` →
`data/combo_arb.db`. For a **local** setup just set the env var to your DB file.

> **Running the engine on Lightsail?** The DB lives on that box. For local-stdio use,
> copy it down periodically, e.g. `scp ubuntu@<ip>:~/combo_arb/data/combo_arb.db ./`
> and point `COMBO_ARB_DB` at the local copy. (Live remote querying would need an
> HTTP transport on the instance — a future option.)

## Add it to Claude Code

Either drop a `.mcp.json` in your project:

```json
{
  "mcpServers": {
    "combo-arb": {
      "command": "/ABS/PATH/.venv/bin/combo-arb-mcp",
      "env": { "COMBO_ARB_DB": "/ABS/PATH/combo_arb.db" }
    }
  }
}
```

…or register it from the CLI:

```bash
claude mcp add combo-arb \
  -e COMBO_ARB_DB=/ABS/PATH/combo_arb.db \
  -- /ABS/PATH/.venv/bin/combo-arb-mcp
```

Restart Claude Code; the `combo-arb` tools should appear.

## Tools (all read-only)

| Tool | Returns |
| --- | --- |
| `db_status` | DB path, size, last-update time, row counts per table |
| `pnl_summary` | Cumulative realized/unrealized PnL, latest equity, trade count |
| `recent_signals` | Most recent flagged (tradeable) combos |
| `top_near_misses` | Combos closest to flagging (largest `gap_to_flag`) |
| `recent_fills` | Most recent simulated (paper) fills |
| `open_positions` | Positions with non-zero net quantity |
| `evaluation_history` | Quote/fair/gap over time for one combo collection ticker |

## Example prompts

- "Use combo-arb: give me the pnl summary and db status."
- "Show the top 10 near-misses — how close is the market to an edge?"
- "Any flagged signals in the last runs? List recent fills."
- "Show the evaluation history for KXMVESPORTSMULTIGAMEEXTENDED-R."
