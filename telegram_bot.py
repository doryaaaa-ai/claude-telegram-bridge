#!/usr/bin/env python3
"""
コードちゃん自律ワーカー v2
- Telegram経由でれじぇんどりゃーから指示を受ける
- claude CLIで自律的に作業する
- 承認が必要な時はTelegramで聞いて返事を待つ
- systemdで常駐、VM再起動しても復活
"""

import asyncio
import json
import os
import subprocess
import time
from pathlib import Path

import httpx

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8756603336:AAFNDjiXt3BCwjk_BWuN_ezIPdDKCws6ZGg")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "5458366490")
STATE_FILE = Path.home() / ".claude" / "telegram-bot-state.json"
APPROVAL_DIR = Path.home() / ".claude" / "approvals"

MAX_MSG_LEN = 4000


# ========== State Management ==========

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"last_update_id": 0, "conversation": [], "active_task": None}


def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    if len(state.get("conversation", [])) > 40:
        state["conversation"] = state["conversation"][-40:]
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


# ========== Approval System ==========

def check_pending_approvals() -> list[dict]:
    """Check for approval requests from Claude tasks."""
    APPROVAL_DIR.mkdir(parents=True, exist_ok=True)
    pending = []
    for f in APPROVAL_DIR.glob("request_*.json"):
        try:
            data = json.loads(f.read_text())
            if data.get("status") == "pending":
                pending.append(data)
        except Exception:
            pass
    return pending


def respond_to_approval(approval_id: str, approved: bool, message: str = ""):
    """Write approval response for Claude task to pick up."""
    resp_file = APPROVAL_DIR / f"response_{approval_id}.json"
    resp_file.write_text(json.dumps({
        "approval_id": approval_id,
        "approved": approved,
        "message": message,
        "responded_at": time.time(),
    }, ensure_ascii=False))

    # Mark request as responded
    req_file = APPROVAL_DIR / f"request_{approval_id}.json"
    if req_file.exists():
        data = json.loads(req_file.read_text())
        data["status"] = "approved" if approved else "rejected"
        req_file.write_text(json.dumps(data, ensure_ascii=False))


# ========== Telegram Helpers ==========

async def send_message(client: httpx.AsyncClient, text: str, reply_to: int | None = None) -> int | None:
    """Send message to Telegram. Returns message_id."""
    chunks = []
    while len(text) > MAX_MSG_LEN:
        split_at = text.rfind("\n", 0, MAX_MSG_LEN)
        if split_at == -1:
            split_at = MAX_MSG_LEN
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    chunks.append(text)

    last_msg_id = None
    for chunk in chunks:
        if not chunk.strip():
            continue
        payload = {"chat_id": CHAT_ID, "text": chunk}
        if reply_to:
            payload["reply_to_message_id"] = reply_to
        resp = await client.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json=payload,
            timeout=30,
        )
        data = resp.json()
        if data.get("ok"):
            last_msg_id = data["result"]["message_id"]
    return last_msg_id


async def send_typing(client: httpx.AsyncClient):
    await client.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendChatAction",
        json={"chat_id": CHAT_ID, "action": "typing"},
        timeout=10,
    )


# ========== Claude Execution ==========

def build_claude_prompt(message: str) -> str:
    """Build prompt with approval system instructions."""
    approval_instructions = f"""
承認が必要な操作（デプロイ、課金、破壊的変更、重要な設計判断）がある場合は、
以下のJSONファイルを作成して承認を待ってください：

ファイル: {APPROVAL_DIR}/request_<timestamp>.json
内容:
{{
  "approval_id": "<timestamp>",
  "question": "承認したい内容の説明",
  "options": ["承認", "却下"],
  "status": "pending",
  "created_at": <unix_timestamp>
}}

ファイル作成後、{APPROVAL_DIR}/response_<timestamp>.json が作られるまで
数秒おきにチェックしてください。response の approved フィールドで判断を確認できます。
"""
    return f"{message}\n\n{approval_instructions}"


def run_claude(message: str, work_dir: str = "/home/aimiral") -> str:
    """Run claude CLI with the message."""
    prompt = build_claude_prompt(message)
    try:
        result = subprocess.run(
            ["claude", "-p", "--allowedTools", "*", prompt],
            capture_output=True,
            text=True,
            timeout=600,
            cwd=work_dir,
            env={
                **os.environ,
                "PATH": f"/home/aimiral/.npm-global/bin:/home/aimiral/.local/bin:{os.environ.get('PATH', '')}",
            },
        )
        output = result.stdout.strip()
        if not output and result.stderr.strip():
            output = f"[stderr] {result.stderr.strip()}"
        return output or "[空の応答]"
    except subprocess.TimeoutExpired:
        return "[タイムアウト: 10分以上かかったで]"
    except FileNotFoundError:
        return "[エラー: claude CLIが見つからん]"
    except Exception as e:
        return f"[エラー: {e}]"


# ========== Main Loop ==========

async def main():
    print("=== コードちゃん自律ワーカー v2 起動 ===")
    print(f"Chat ID: {CHAT_ID}")
    print(f"Approval Dir: {APPROVAL_DIR}")
    print("メッセージ待機中...")

    APPROVAL_DIR.mkdir(parents=True, exist_ok=True)
    state = load_state()
    offset = state["last_update_id"]
    pending_approval_msg_ids: dict[str, int] = {}  # approval_id -> telegram msg_id

    async with httpx.AsyncClient() as client:
        await send_message(
            client,
            "コードちゃん起動したで！🙌\n"
            "Telegramから指示してな。承認が必要な時はここで聞くで。\n\n"
            "コマンド:\n"
            "/ping - 生存確認\n"
            "/status - VM状態\n"
            "/projects - プロジェクト一覧\n"
            "/tasks - 実行中タスク確認"
        )

        # Background: approval checker
        async def check_approvals_loop():
            """定期的に承認リクエストをチェックしてTelegramに送る"""
            notified: set[str] = set()
            while True:
                try:
                    pending = check_pending_approvals()
                    for req in pending:
                        aid = req["approval_id"]
                        if aid not in notified:
                            question = req.get("question", "承認が必要です")
                            msg_id = await send_message(
                                client,
                                f"⚠️ 承認リクエスト ⚠️\n\n"
                                f"{question}\n\n"
                                f"👉 「OK」か「NG」で返信してな\n"
                                f"(ID: {aid})"
                            )
                            if msg_id:
                                pending_approval_msg_ids[aid] = msg_id
                            notified.add(aid)
                except Exception as e:
                    print(f"Approval check error: {e}")
                await asyncio.sleep(3)

        approval_task = asyncio.create_task(check_approvals_loop())

        while True:
            try:
                resp = await client.get(
                    f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
                    params={
                        "offset": offset + 1,
                        "timeout": 30,
                        "allowed_updates": '["message"]',
                    },
                    timeout=45,
                )
                data = resp.json()
                if not data.get("ok"):
                    await asyncio.sleep(5)
                    continue

                for update in data.get("result", []):
                    update_id = update["update_id"]
                    offset = update_id
                    state["last_update_id"] = offset
                    save_state(state)

                    msg = update.get("message", {})
                    chat_id = str(msg.get("chat", {}).get("id", ""))
                    if chat_id != str(CHAT_ID):
                        continue

                    text = msg.get("text", "").strip()
                    msg_id = msg.get("message_id")
                    if not text:
                        continue

                    # ----- 承認への返事チェック -----
                    reply_to = msg.get("reply_to_message", {}).get("message_id")
                    if reply_to:
                        # 承認リクエストへの返信か確認
                        for aid, tmsg_id in list(pending_approval_msg_ids.items()):
                            if reply_to == tmsg_id:
                                text_lower = text.lower().strip()
                                approved = text_lower in ("ok", "おk", "ок", "承認", "yes", "ええよ", "おけ", "いいよ", "👍", "はい")
                                respond_to_approval(aid, approved, text)
                                status = "✅ 承認" if approved else "❌ 却下"
                                await send_message(client, f"{status}したで！(ID: {aid})", msg_id)
                                del pending_approval_msg_ids[aid]
                                break
                        continue

                    # ----- コマンド処理 -----
                    if text == "/start":
                        await send_message(client, "コードちゃんやで！なんでも指示してな！", msg_id)
                        continue
                    elif text == "/ping":
                        await send_message(client, "pong! 生きてるで！💪", msg_id)
                        continue
                    elif text == "/status":
                        proc = subprocess.run(["uptime"], capture_output=True, text=True)
                        disk = subprocess.run(["df", "-h", "/"], capture_output=True, text=True)
                        mem = subprocess.run(["free", "-h"], capture_output=True, text=True)
                        status_text = (
                            f"Uptime: {proc.stdout.strip()}\n\n"
                            f"Disk:\n{disk.stdout.strip()}\n\n"
                            f"Memory:\n{mem.stdout.strip()}"
                        )
                        await send_message(client, status_text, msg_id)
                        continue
                    elif text == "/projects":
                        await send_message(
                            client,
                            "📱 ai-phone - AI電話サービス（未着手）\n"
                            "🍽️ menucraft - LINE連携レストラン管理（未着手）",
                            msg_id,
                        )
                        continue
                    elif text == "/tasks":
                        pending = check_pending_approvals()
                        if pending:
                            tasks = "\n".join(
                                f"- {p.get('question', '?')} (ID: {p['approval_id']})"
                                for p in pending
                            )
                            await send_message(client, f"承認待ちタスク:\n{tasks}", msg_id)
                        else:
                            active = state.get("active_task")
                            if active:
                                await send_message(client, f"実行中: {active}", msg_id)
                            else:
                                await send_message(client, "タスクなし。指示待ちやで！", msg_id)
                        continue

                    # ----- 通常メッセージ → Claude実行 -----
                    print(f"\n📩 受信: {text[:100]}...")
                    state["active_task"] = text[:100]
                    save_state(state)

                    await send_message(client, "🔨 了解、作業開始するで...", msg_id)
                    await send_typing(client)

                    # 作業ディレクトリ判定
                    work_dir = "/home/aimiral"
                    text_lower = text.lower()
                    if "ai-phone" in text_lower or "ai phone" in text_lower:
                        work_dir = "/home/aimiral/ai-phone"
                    elif "menucraft" in text_lower:
                        work_dir = "/home/aimiral/menucraft"

                    # Claude実行（別スレッドで）
                    loop = asyncio.get_event_loop()
                    response = await loop.run_in_executor(None, run_claude, text, work_dir)

                    print(f"📤 完了: {response[:100]}...")
                    await send_message(client, response, msg_id)

                    # タスク完了
                    state["active_task"] = None
                    state["conversation"].append({"role": "user", "text": text})
                    state["conversation"].append({"role": "assistant", "text": response[:500]})
                    save_state(state)

            except httpx.TimeoutException:
                continue
            except KeyboardInterrupt:
                print("\n終了！")
                approval_task.cancel()
                await send_message(client, "コードちゃん落ちるで！またな！")
                break
            except Exception as e:
                print(f"エラー: {e}")
                await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
