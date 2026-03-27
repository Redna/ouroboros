import json
import time
from typing import List, Dict, Any, Tuple

import requests
import constants
import agent_state

def send_telegram_direct(chat_id: int, text: str):
    """Sends a Telegram message directly from the runtime (HAL)."""
    if not constants.TELEGRAM_BOT_TOKEN or not chat_id:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{constants.TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=10
        )
        agent_state.append_chat_history("Ouroboros", text)
    except Exception as e:
        print(f"[HAL Error] Failed to send read receipt: {e}")

def send_telegram_reaction(chat_id: int, message_id: int, emoji: str):
    """Sends a reaction to a specific message."""
    if not constants.TELEGRAM_BOT_TOKEN or not chat_id or not message_id:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{constants.TELEGRAM_BOT_TOKEN}/setMessageReaction",
            json={
                "chat_id": chat_id,
                "message_id": message_id,
                "reaction": [{"type": "emoji", "emoji": emoji}],
                "is_big": False
            },
            timeout=10
        )
    except Exception as e:
        print(f"[HAL Error] Failed to send reaction: {e}")

def send_telegram_action(chat_id: int, action: str = "typing"):
    """Sends a chat action (e.g. typing)."""
    if not constants.TELEGRAM_BOT_TOKEN or not chat_id:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{constants.TELEGRAM_BOT_TOKEN}/sendChatAction",
            json={"chat_id": chat_id, "action": action},
            timeout=10
        )
    except Exception as e:
        print(f"[HAL Error] Failed to send chat action: {e}")

def queue_creator_message(new_message: str, update_id: int):
    """
    Safely adds a creator message to the queue. 
    If a P999 task is already pending, it appends the message to prevent fragmentation.
    """
    queue = agent_state.load_task_queue()
    
    # Look for an existing, unstarted Priority 999 task
    existing_p999 = None
    for task in queue:
        if task.get("priority") == 999:
            existing_p999 = task
            break
            
    tid = f"task_msg_{update_id}"
    
    # Check for existing task_id to prevent duplicates (IDEMPOTENCY)
    if any(t.get("task_id") == tid for t in queue):
        # We also check the description to see if it's already coalesced
        # but the tid check is usually enough for Telegram updates.
        return

    if existing_p999:
        # Coalesce the messages
        timestamp = time.strftime("%H:%M:%S")
        existing_p999["description"] += f"\n\n[Follow-up at {timestamp}]: {new_message}"
        constants.TASK_QUEUE_PATH.write_text(json.dumps(queue, indent=2), encoding="utf-8")
    else:
        # No pending P999 task, create a new one
        tid = f"task_msg_{update_id}"
        queue.append({
            "task_id": tid,
            "description": new_message,
            "priority": 999,
            "turn_count": 0
        })
        queue.sort(key=lambda x: x.get("priority", 1), reverse=True)
        constants.TASK_QUEUE_PATH.write_text(json.dumps(queue, indent=2), encoding="utf-8")
        # print("[HAL] Queued new P999 creator interrupt.")

def poll_telegram(s: Dict[str, Any], q: List[Dict[str, Any]]) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    if not constants.TELEGRAM_BOT_TOKEN:
        return s, q
        
    offset = s.get("offset", 0)
    try:
        r = requests.get(f"https://api.telegram.org/bot{constants.TELEGRAM_BOT_TOKEN}/getUpdates", params={"offset": offset, "timeout": 5}, timeout=10).json()
        if r.get("ok") and r.get("result"):
            new_offset = r["result"][-1]["update_id"] + 1
            s["offset"], s["wake_time"] = new_offset, 0
            agent_state.save_state(s)
            
            interrupt_triggered = False
            for u in r["result"]:
                msg = u.get("message", {})
                if msg.get("text"): 
                    text, cid = msg["text"], msg["chat"]["id"]
                    msg_id = msg.get("message_id")
                    if not s.get("creator_id"):
                        s["creator_id"] = cid
                        agent_state.save_state(s)
                    agent_state.append_chat_history("User", text)
                    update_id = u.get('update_id', int(time.time()))
                    queue_creator_message(text, update_id)
                    
                    if msg_id:
                        send_telegram_reaction(cid, msg_id, "📋")
                        
                    interrupt_triggered = True
                    
            if interrupt_triggered:
                q = agent_state.load_task_queue()
    except Exception:
        pass
        
    return s, q
