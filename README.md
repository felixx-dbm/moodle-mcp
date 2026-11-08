# moodle-mcp

An [MCP](https://modelcontextprotocol.io) server that lets AI assistants (Claude Code, Claude Desktop, Cursor, ...) browse your Moodle courses and download course material.

Built for DHBW Stuttgart, works with any Moodle instance.

## The problem

University Moodle instances usually sit behind SSO, have no API tokens for students, and bury lecture slides in nested folders. Getting material into an AI study workflow means clicking through every course and downloading files by hand.

## How it works

No password and no API token. The server reuses the Moodle session you already have in Chrome:

1. Reads the `MoodleSession` cookie from Chrome's cookie database (macOS Keychain decrypts it; you approve once with "Always Allow").
2. Validates it and caches it in `~/.config/moodle-mcp/session.json` (mode `600`).
3. Keeps the session alive with `core_session_touch` every 15 minutes, so it does not expire while your assistant works.
4. If the session dies, it falls back to Chrome again. If that is expired too, it tells you to log in in the browser.

## Tools

| Tool | What it does |
|---|---|
| `list_courses` | Courses you are enrolled in (id, name, url) |
| `course_contents(course_id)` | Sections and activities: files, folders, links, assignments |
| `folder_contents(folder_url)` | Files inside a folder activity |
| `download(url, dest_dir)` | Downloads a file (resource page or `pluginfile.php` URL) |

## Setup

Requirements: macOS, Google Chrome, [uv](https://docs.astral.sh/uv/).

Log in to Moodle in Chrome once, then check that the server can see the session:

```bash
uv run moodle_mcp.py login
```

Add it to Claude Code:

```bash
claude mcp add moodle -- uv run /path/to/moodle_mcp.py
```

Or to any MCP client config:

```json
{
  "mcpServers": {
    "moodle": {
      "command": "uv",
      "args": ["run", "/path/to/moodle_mcp.py"],
      "env": { "MOODLE_BASE_URL": "https://elearning.dhbw-stuttgart.de/moodle" }
    }
  }
}
```

`MOODLE_BASE_URL` defaults to DHBW Stuttgart. Set it to your university's Moodle root.

Example prompt: *"Download all PDFs from my Datenbanken course into ~/Uni/DB and summarize lecture 3."*

## Security

- The server only reads the `MoodleSession` cookie for your Moodle host, nothing else from Chrome.
- The session cookie is stored locally with owner-only permissions and never leaves your machine except as a request to your Moodle.
- Anyone who can run this on your account can act as you on Moodle. Treat it like a logged-in browser.

## Stack

Python 3.12, `mcp` (FastMCP), `httpx`, `selectolax`, `cryptography`. Single file, dependencies declared inline (PEP 723).

## License

MIT
