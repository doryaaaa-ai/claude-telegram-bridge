"""Claude Telegram Bridge - MCP server for async Claude <-> Telegram communication."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("telegram-bridge")

# Config
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
STATE_FILE = Path.home() / ".claude" / "telegram-bridge-state.json"

# Defaults
DEFAULT_STATE = {
    "away": False,
    "project": None,
    "last_update_id": 0,
    "buffered_messages": [],
    "pending_replies": {},
}


def _load_state() -> dict:
    """Load state from JSON file."""
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return DEFAULT_STATE.copy()


def _save_state(state: dict) -> None:
    """Save state to JSON file."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


async def _send_message(text: str, reply_to: int | None = None) -> int:
    """Send a Telegram message. Returns message_id."""
    async with httpx.AsyncClient() as client:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        payload: dict = {
            "chat_id": CHAT_ID,
            "text": text,
        }
        if reply_to:
            payload["reply_to_message_id"] = reply_to
        resp = await client.post(url, json=payload, timeout=30)
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram API error: {data}")
        return data["result"]["message_id"]


async def _get_updates(state: dict, timeout: int = 10) -> list[dict]:
    """Get new messages from Telegram via long polling."""
    async with httpx.AsyncClient() as client:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
        params = {
            "offset": state["last_update_id"] + 1,
            "timeout": timeout,
            "allowed_updates": '["message"]',
        }
        resp = await client.get(url, params=params, timeout=timeout + 15)
        data = resp.json()
        if not data.get("ok"):
            return []
        return data.get("result", [])


def _process_updates(state: dict, updates: list[dict]) -> list[dict]:
    """Process updates: handle commands, route replies, buffer unthreaded messages.
    Returns list of processed message dicts."""
    messages = []
    for update in updates:
        update_id = update["update_id"]
        if update_id > state["last_update_id"]:
            state["last_update_id"] = update_id

        msg = update.get("message", {})
        if not msg or str(msg.get("chat", {}).get("id")) != str(CHAT_ID):
            continue

        text = msg.get("text", "")
        reply_to_msg_id = msg.get("reply_to_message", {}).get("message_id")

        # Handle commands (always, regardless of away mode)
        if text.strip().lower() == "/away":
            state["away"] = True
            messages.append({"type": "command", "command": "away"})
            continue
        elif text.strip().lower() == "/back":
            state["away"] = False
            messages.append({"type": "command", "command": "back"})
            continue
        elif text.strip().lower() == "/status":
            status = "Away" if state["away"] else "At terminal"
            project = state.get("project") or "none"
            messages.append({"type": "command", "command": "status", "status": status, "project": project})
            continue

        msg_data = {
            "type": "reply" if reply_to_msg_id else "message",
            "text": text,
            "reply_to_message_id": reply_to_msg_id,
            "message_id": msg["message_id"],
        }

        # Route replies to pending_replies
        if reply_to_msg_id:
            key = str(reply_to_msg_id)
            if key not in state["pending_replies"]:
                state["pending_replies"][key] = []
            state["pending_replies"][key].append(text)
        else:
            # Buffer unthreaded messages
            state["buffered_messages"].append(text)

        messages.append(msg_data)

    return messages


async def _poll_for_reply(state: dict, sent_msg_id: int, timeout: int = 120, follow_up: int = 3) -> str | None:
    """Poll for a reply to a specific sent message."""
    key = str(sent_msg_id)
    deadline = time.time() + timeout

    while time.time() < deadline:
        # Check pending_replies first (another session may have stored it)
        state = _load_state()
        if key in state["pending_replies"] and state["pending_replies"][key]:
            replies = state["pending_replies"].pop(key)
            _save_state(state)

            # Follow-up poll for multi-message replies
            time.sleep(follow_up)
            state = _load_state()
            if key in state["pending_replies"] and state["pending_replies"][key]:
                replies.extend(state["pending_replies"].pop(key))
                _save_state(state)

            return "\n".join(replies)

        # Poll Telegram
        updates = await _get_updates(state, timeout=5)
        if updates:
            _process_updates(state, updates)
            _save_state(state)

            # Check again after processing
            if key in state["pending_replies"] and state["pending_replies"][key]:
                replies = state["pending_replies"].pop(key)
                _save_state(state)

                time.sleep(follow_up)
                state = _load_state()
                if key in state["pending_replies"] and state["pending_replies"][key]:
                    replies.extend(state["pending_replies"].pop(key))
                    _save_state(state)

                return "\n".join(replies)

    return None


@mcp.tool()
async def setup_check() -> str:
    """Verify bot configuration and discover chat IDs."""
    if not BOT_TOKEN:
        return "ERROR: TELEGRAM_BOT_TOKEN not set"
    if not CHAT_ID:
        return "WARNING: TELEGRAM_CHAT_ID not set. Send a message to the bot, then call this again."

    async with httpx.AsyncClient() as client:
        # Verify bot
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/getMe"
        resp = await client.get(url, timeout=10)
        data = resp.json()
        if not data.get("ok"):
            return f"ERROR: Invalid bot token: {data}"

        bot_info = data["result"]
        bot_name = bot_info.get("first_name", "Unknown")
        bot_username = bot_info.get("username", "Unknown")

        # Get recent updates to find chat IDs
        url2 = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
        resp2 = await client.get(url2, params={"limit": 10}, timeout=10)
        data2 = resp2.json()

        chats = set()
        if data2.get("ok"):
            for upd in data2.get("result", []):
                chat = upd.get("message", {}).get("chat", {})
                if chat:
                    chats.add(f"  - {chat.get('first_name', '')} (ID: {chat['id']})")

        result = f"Bot: {bot_name} (@{bot_username})\nConfigured chat_id: {CHAT_ID}\n"
        if chats:
            result += "Recent chats:\n" + "\n".join(chats)
        else:
            result += "No recent chats found. Send a message to the bot first."

        return result


@mcp.tool()
async def set_away_mode(away: bool, project: str | None = None) -> str:
    """Toggle away mode on/off. When on, Claude can send questions via Telegram."""
    state = _load_state()
    state["away"] = away
    if project:
        state["project"] = project
    _save_state(state)

    if away:
        tag = f"[{project}] " if project else ""
        await _send_message(f"{tag}Away mode ON. I'll send questions here.")
        return f"Away mode activated. Project: {project or 'none'}"
    else:
        return "Away mode deactivated."


@mcp.tool()
async def send_question(question: str) -> str:
    """Send a question to the user via Telegram and wait for reply. Requires away mode."""
    state = _load_state()
    if not state["away"]:
        return "ERROR: Away mode is not active. Use set_away_mode first."

    tag = f"[{state.get('project', '')}] " if state.get("project") else ""
    msg_id = await _send_message(f"{tag}Question:\n{question}")
    _save_state(state)

    reply = await _poll_for_reply(state, msg_id, timeout=300)
    if reply:
        return f"User replied: {reply}"
    else:
        return "No reply received (timed out after 5 minutes)."


@mcp.tool()
async def send_summary(summary: str) -> str:
    """Send a summary/notification via Telegram. Polls briefly for response."""
    state = _load_state()
    if not state["away"]:
        return "ERROR: Away mode is not active. Use set_away_mode first."

    tag = f"[{state.get('project', '')}] " if state.get("project") else ""
    msg_id = await _send_message(f"{tag}{summary}")
    _save_state(state)

    # Brief poll (30s) to catch immediate replies
    reply = await _poll_for_reply(state, msg_id, timeout=30)
    if reply:
        return f"Summary sent. User replied: {reply}"
    else:
        return "Summary sent. No immediate reply."


@mcp.tool()
async def check_messages() -> str:
    """Check for new Telegram messages. Always processes commands (/away, /back, /status)."""
    state = _load_state()

    # Poll for new updates
    updates = await _get_updates(state, timeout=3)
    processed = _process_updates(state, updates)

    # Handle command responses
    for msg in processed:
        if msg.get("type") == "command":
            cmd = msg["command"]
            if cmd == "away":
                await _send_message("Away mode activated remotely.")
            elif cmd == "back":
                await _send_message("Welcome back! Away mode deactivated.")
            elif cmd == "status":
                await _send_message(f"Status: {msg['status']}, Project: {msg['project']}")

    # Drain buffered messages
    buffered = state["buffered_messages"]
    state["buffered_messages"] = []
    _save_state(state)

    if buffered:
        return "Messages from Telegram:\n" + "\n---\n".join(buffered)
    elif processed:
        cmds = [m["command"] for m in processed if m.get("type") == "command"]
        return f"Processed commands: {', '.join(cmds)}" if cmds else "No new messages."
    else:
        return "No new messages."


def main():
    """Entry point for the MCP server."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
