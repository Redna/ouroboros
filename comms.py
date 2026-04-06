import json
import time
import re
from typing import List, Dict, Any, Tuple

import requests
import constants
import agent_state

def _escape_markdown(text: str) -> str:
    """Escapes characters for Telegram MarkdownV2."""
    # List of characters that must be escaped in MarkdownV2
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)

def send_telegram_direct(chat_id: int, text: str):
    """Sends a Telegram message directly from the runtime (HAL)."""
    if not constants.TELEGRAM_BOT_TOKEN or not chat_id:
        return
    try:
        # Use simple escaping if the agent sends complex formatting
        # If it fails, we fall back to plain text
        requests.post(
            f"https://api.telegram.org/bot{constants.TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text},
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
    Stores a creator message in the pending queue for piggybacking.
    """
    agent_state.queue_system_notice(f"[CREATOR MESSAGE]\n- {new_message}")

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
                    
                    # V5: Creator messages are now piggybacked like HUD telemetry
                    queue_creator_message(text, update_id)
                    
                    if msg_id:
                        send_telegram_reaction(cid, msg_id, "📋")
                        
                    interrupt_triggered = True
                    
            if interrupt_triggered:
                # Still reload queue in case the message was a priority shift or task
                q = agent_state.load_task_queue()
    except Exception:
        pass
        
    return s, q
