#!/usr/bin/env python3
"""
コードちゃん自律ワーカー v3
- Telegram経由でれじぇんどりゃーから指示を受ける
- claude CLI --continue で会話の文脈を維持
- 承認が必要な時はTelegramで聞いて返事を待つ
- GitHub issueを定期巡回して自動で作業
- systemdで常駐、VM再起動しても復活
"""

import asyncio
import json
import os
import subprocess
import time
import uuid
from pathlib import Path

import httpx

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8756603336:AAFNDjiXt3BCwjk_BWuN_ezIPdDKCws6ZGg")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "5458366490")
STATE_FILE = Path.home() / ".claude" / "telegram-bot-state.json"
APPROVAL_DIR = Path.home() / ".claude" / "approvals"
CLAUDE_PATH = "/home/aimiral/.npm-global/bin/claude"

MAX_MSG_LEN = 4000

PROJECTS = {
    "dexs-restaurant": {
        "dir": "/home/aimiral/AiMiraiLabs",
        "repo": "doryaaaa-ai/AiMiraiLabs",
        "desc": "DEXS飲食店向けDXシステム",
    },
    "dexs-business": {
        "dir": "/home/aimiral/AiMiraiLabs",
        "repo": "doryaaaa-ai/AiMiraiLabs",
        "desc": "DEXS企業向けDXシステム",
    },
}

# メインリポジトリ（issue巡回用）
MAIN_REPO = "doryaaaa-ai/AiMiraiLabs"
MAIN_DIR = "/home/aimiral/AiMiraiLabs"

# Issue巡回間隔（秒）
ISSUE_CHECK_INTERVAL = 300  # 5分


# ========== State Management ==========

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {
        "last_update_id": 0,
        "conversation": [],
        "active_task": None,
        "session_ids": {},       # project -> session_id (会話継続用)
        "processed_issues": [],  # 処理済みissue番号
    }


def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    if len(state.get("conversation", [])) > 40:
        state["conversation"] = state["conversation"][-40:]
    if len(state.get("processed_issues", [])) > 200:
        state["processed_issues"] = state["processed_issues"][-200:]
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


# ========== Approval System ==========

def check_pending_approvals() -> list[dict]:
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
    resp_file = APPROVAL_DIR / f"response_{approval_id}.json"
    resp_file.write_text(json.dumps({
        "approval_id": approval_id,
        "approved": approved,
        "message": message,
        "responded_at": time.time(),
    }, ensure_ascii=False))

    req_file = APPROVAL_DIR / f"request_{approval_id}.json"
    if req_file.exists():
        data = json.loads(req_file.read_text())
        data["status"] = "approved" if approved else "rejected"
        req_file.write_text(json.dumps(data, ensure_ascii=False))


def cleanup_old_approvals():
    """24時間以上前の承認ファイルを削除"""
    APPROVAL_DIR.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - 86400
    for f in APPROVAL_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text())
            if data.get("created_at", data.get("responded_at", 0)) < cutoff:
                f.unlink()
        except Exception:
            pass


# ========== Telegram Helpers ==========

async def send_message(client: httpx.AsyncClient, text: str, reply_to: int | None = None) -> int | None:
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
        try:
            resp = await client.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json=payload,
                timeout=30,
            )
            data = resp.json()
            if data.get("ok"):
                last_msg_id = data["result"]["message_id"]
        except Exception as e:
            print(f"Send error: {e}")
    return last_msg_id


async def send_typing(client: httpx.AsyncClient):
    try:
        await client.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendChatAction",
            json={"chat_id": CHAT_ID, "action": "typing"},
            timeout=10,
        )
    except Exception:
        pass


# ========== Claude Execution ==========

def detect_project(text: str) -> str | None:
    """テキストからプロジェクトを判定"""
    text_lower = text.lower()
    for name in PROJECTS:
        if name in text_lower or name.replace("-", " ") in text_lower:
            return name
    return None


def run_claude(message: str, state: dict, project: str | None = None, continue_session: bool = True) -> str:
    """Run claude CLI with conversation continuity."""
    work_dir = PROJECTS[project]["dir"] if project and project in PROJECTS else MAIN_DIR

    # 承認システムの説明をプロンプトに追加
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
    prompt = f"{message}\n\n{approval_instructions}"

    # セッションID管理（プロジェクトごとに会話継続）
    session_key = project or "_default"
    cmd = [CLAUDE_PATH, "-p", "--dangerously-skip-permissions"]

    if continue_session and session_key in state.get("session_ids", {}):
        # 前回のセッションを継続
        cmd.extend(["--resume", state["session_ids"][session_key]])
    else:
        # 新しいセッション
        session_id = str(uuid.uuid4())
        state.setdefault("session_ids", {})[session_key] = session_id
        cmd.extend(["--session-id", session_id])

    # プロンプトは stdin で渡す
    env = {
        **os.environ,
        "PATH": f"/home/aimiral/.npm-global/bin:/home/aimiral/.local/bin:/usr/local/bin:/usr/bin:/bin",
        "HOME": "/home/aimiral",
    }
    # ネスト防止の環境変数を除去
    env.pop("CLAUDECODE", None)
    env.pop("CLAUDE_CODE", None)

    try:
        result = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=1800,
            cwd=work_dir,
            env=env,
        )
        output = result.stdout.strip()
        if not output and result.stderr.strip():
            # セッション復帰失敗時は新しいセッションで再試行
            if "session" in result.stderr.lower() or "resume" in result.stderr.lower():
                print(f"Session resume failed, starting new session")
                if session_key in state.get("session_ids", {}):
                    del state["session_ids"][session_key]
                return run_claude(message, state, project, continue_session=False)
            output = f"[stderr] {result.stderr.strip()}"
        return output or "[空の応答]"
    except subprocess.TimeoutExpired:
        return "[タイムアウト: 10分以上かかったで]"
    except FileNotFoundError:
        return "[エラー: claude CLIが見つからん]"
    except Exception as e:
        return f"[エラー: {e}]"


# ========== GitHub Issue巡回 ==========

def fetch_issues(repo: str) -> list[dict]:
    """GitHub issueを取得"""
    try:
        result = subprocess.run(
            ["gh", "issue", "list", "--repo", repo, "--json", "number,title,labels,assignees,body", "--limit", "10"],
            capture_output=True,
            text=True,
            timeout=30,
            env={
                **os.environ,
                "PATH": f"/home/aimiral/.npm-global/bin:/home/aimiral/.local/bin:{os.environ.get('PATH', '')}",
            },
        )
        if result.returncode == 0 and result.stdout.strip():
            return json.loads(result.stdout)
    except Exception as e:
        print(f"Issue fetch error: {e}")
    return []


def find_auto_issues(state: dict) -> list[dict]:
    """自動処理すべきissueを探す（'codechan' or 'auto' ラベル付き）"""
    auto_issues = []
    processed = set(state.get("processed_issues", []))

    # メインリポジトリからissueを取得
    issues = fetch_issues(MAIN_REPO)
    for issue in issues:
        issue_key = f"main#{issue['number']}"
        if issue_key in processed:
            continue

        labels = [l.get("name", "").lower() for l in issue.get("labels", [])]
        # 'codechan' or 'auto' ラベルが付いてるissueを自動処理
        if any(l in ("codechan", "auto", "コードちゃん") for l in labels):
            auto_issues.append({
                "project": None,  # メインリポジトリ直接
                "issue": issue,
                "key": issue_key,
            })

    return auto_issues


# ========== Main Loop ==========

async def main():
    print("=== コードちゃん自律ワーカー v3 起動 ===")
    print(f"Chat ID: {CHAT_ID}")
    print(f"Approval Dir: {APPROVAL_DIR}")
    print(f"Issue Check Interval: {ISSUE_CHECK_INTERVAL}s")
    print("メッセージ待機中...")

    APPROVAL_DIR.mkdir(parents=True, exist_ok=True)
    state = load_state()
    offset = state["last_update_id"]
    pending_approval_msg_ids: dict[str, int] = {}

    async with httpx.AsyncClient() as client:
        await send_message(
            client,
            "コードちゃん v3 起動したで！🙌\n\n"
            "✅ 会話の文脈を覚えるようになった\n"
            "✅ GitHub issueを自動巡回するようになった\n"
            "  （'codechan'ラベルのissueを自動処理）\n"
            "✅ 承認が必要な時はここで聞くで\n\n"
            "コマンド:\n"
            "/ping - 生存確認\n"
            "/status - VM状態\n"
            "/projects - プロジェクト一覧\n"
            "/tasks - タスク確認\n"
            "/issues - GitHub issue確認\n"
            "/reset - 会話リセット"
        )

        # ----- Background: 承認チェッカー -----
        async def check_approvals_loop():
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
                    # 古い承認ファイル掃除
                    cleanup_old_approvals()
                except Exception as e:
                    print(f"Approval check error: {e}")
                await asyncio.sleep(3)

        # ----- Background: Issue巡回 -----
        async def issue_patrol_loop():
            while True:
                try:
                    auto_issues = await asyncio.get_event_loop().run_in_executor(None, find_auto_issues, state)
                    for item in auto_issues:
                        project = item["project"]
                        issue = item["issue"]
                        issue_key = item["key"]

                        # Telegramに通知
                        await send_message(
                            client,
                            f"🔍 Issue発見！自動作業開始するで\n\n"
                            f"プロジェクト: {project}\n"
                            f"#{issue['number']}: {issue['title']}\n"
                            f"{issue.get('body', '')[:300]}"
                        )

                        # Claude実行
                        await send_typing(client)
                        task_msg = (
                            f"GitHub Issue #{issue['number']}: {issue['title']}\n\n"
                            f"{issue.get('body', '')}\n\n"
                            f"このissueを調査・実装してください。\n"
                            f"調査系issueの場合は結果をissueにコメントしてから閉じてください：\n"
                            f"gh issue comment {issue['number']} --repo {MAIN_REPO} --body '調査結果'\n"
                            f"gh issue close {issue['number']} --repo {MAIN_REPO}\n"
                            f"実装系issueの場合はgit commit & pushしてからissueを閉じてください。"
                        )

                        project_name = project or "AiMiraiLabs"
                        state["active_task"] = f"[{project_name}] #{issue['number']}: {issue['title']}"
                        save_state(state)

                        loop = asyncio.get_event_loop()
                        response = await loop.run_in_executor(None, run_claude, task_msg, state, project)

                        await send_message(
                            client,
                            f"📋 Issue #{issue['number']} 作業完了\n\n{response}"
                        )

                        # 処理済みに追加
                        state["processed_issues"].append(issue_key)
                        state["active_task"] = None
                        save_state(state)

                except Exception as e:
                    print(f"Issue patrol error: {e}")

                await asyncio.sleep(ISSUE_CHECK_INTERVAL)

        approval_task = asyncio.create_task(check_approvals_loop())
        issue_task = asyncio.create_task(issue_patrol_loop())

        # Claude実行中のタスクを管理（メインループをブロックしない）
        active_claude_tasks: list[asyncio.Task] = []

        async def run_claude_task(text: str, project: str | None, msg_id: int):
            """Claude実行をバックグラウンドで行い、完了後にTelegramへ返信"""
            try:
                loop = asyncio.get_event_loop()
                response = await loop.run_in_executor(None, run_claude, text, state, project)
                print(f"📤 完了: {response[:100]}...")
                await send_message(client, response, msg_id)
                state["active_task"] = None
                state.setdefault("conversation", []).append({"role": "user", "text": text})
                state["conversation"].append({"role": "assistant", "text": response[:500]})
                save_state(state)
            except Exception as e:
                await send_message(client, f"[エラー: {e}]", msg_id)
                state["active_task"] = None
                save_state(state)

        while True:
            try:
                # 完了したタスクを掃除
                active_claude_tasks = [t for t in active_claude_tasks if not t.done()]

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

                    # ----- 承認への返事 -----
                    # reply_toでの承認マッチ
                    reply_to = msg.get("reply_to_message", {}).get("message_id")
                    if reply_to:
                        for aid, tmsg_id in list(pending_approval_msg_ids.items()):
                            if reply_to == tmsg_id:
                                text_lower = text.lower().strip()
                                approved = text_lower in (
                                    "ok", "おk", "承認", "yes", "ええよ", "おけ",
                                    "いいよ", "👍", "はい", "ええで", "おっけー", "go",
                                )
                                respond_to_approval(aid, approved, text)
                                status_emoji = "✅ 承認" if approved else "❌ 却下"
                                await send_message(client, f"{status_emoji}したで！", msg_id)
                                del pending_approval_msg_ids[aid]
                                break
                        continue

                    # 短い承認ワードで、pending承認がある場合は直接処理
                    text_lower = text.lower().strip()
                    approval_words = (
                        "ok", "おk", "承認", "yes", "ええよ", "おけ",
                        "いいよ", "はい", "ええで", "おっけー", "go",
                    )
                    reject_words = ("ng", "却下", "no", "あかん", "だめ", "ダメ")
                    pending = check_pending_approvals()
                    if pending and text_lower in approval_words + reject_words:
                        approved = text_lower in approval_words
                        for req in pending:
                            aid = req["approval_id"]
                            respond_to_approval(aid, approved, text)
                        status_emoji = "✅ 承認" if approved else "❌ 却下"
                        count = len(pending)
                        await send_message(client, f"{status_emoji} {count}件の承認を処理したで！", msg_id)
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
                        await send_message(
                            client,
                            f"Uptime: {proc.stdout.strip()}\n\n"
                            f"Disk:\n{disk.stdout.strip()}\n\n"
                            f"Memory:\n{mem.stdout.strip()}",
                            msg_id,
                        )
                        continue
                    elif text == "/projects":
                        proj_list = "\n".join(
                            f"{'🍽️' if 'restaurant' in n else '🏢'} {n} - {p['desc']}"
                            for n, p in PROJECTS.items()
                        )
                        await send_message(client, proj_list, msg_id)
                        continue
                    elif text == "/tasks":
                        pending = check_pending_approvals()
                        lines = []
                        if pending:
                            lines.append("⚠️ 承認待ち:")
                            for p in pending:
                                lines.append(f"  - {p.get('question', '?')}")
                        active = state.get("active_task")
                        if active:
                            lines.append(f"\n🔨 実行中: {active}")
                        if not lines:
                            lines.append("タスクなし。指示待ちやで！")
                        await send_message(client, "\n".join(lines), msg_id)
                        continue
                    elif text == "/issues":
                        all_issues = []
                        issues = fetch_issues(MAIN_REPO)
                        for iss in issues:
                            labels = ", ".join(l.get("name", "") for l in iss.get("labels", []))
                            label_str = f" [{labels}]" if labels else ""
                            all_issues.append(f"#{iss['number']}: {iss['title']}{label_str}")
                        if all_issues:
                            await send_message(client, f"GitHub Issues ({MAIN_REPO}):\n" + "\n".join(all_issues), msg_id)
                        else:
                            await send_message(client, "Issueなし！", msg_id)
                        continue
                    elif text == "/reset":
                        state["session_ids"] = {}
                        save_state(state)
                        await send_message(client, "会話リセットしたで！新しい文脈で始めるわ。", msg_id)
                        continue

                    # ----- 通常メッセージ → Claude実行（ノンブロッキング） -----
                    print(f"\n📩 受信: {text[:100]}...")
                    project = detect_project(text)
                    state["active_task"] = text[:100]
                    save_state(state)

                    proj_label = f" [{project}]" if project else ""
                    await send_message(client, f"🔨 了解{proj_label}、作業開始するで...", msg_id)
                    await send_typing(client)

                    # バックグラウンドで実行（メインループは止まらん）
                    task = asyncio.create_task(run_claude_task(text, project, msg_id))
                    active_claude_tasks.append(task)

            except httpx.TimeoutException:
                continue
            except KeyboardInterrupt:
                print("\n終了！")
                approval_task.cancel()
                issue_task.cancel()
                for t in active_claude_tasks:
                    t.cancel()
                await send_message(client, "コードちゃん落ちるで！またな！")
                break
            except Exception as e:
                print(f"エラー: {e}")
                await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
