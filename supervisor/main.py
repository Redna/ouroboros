"""
Ouroboros Supervisor — main entry point.

Run with:
    uv run ouroboros
    # or equivalently:
    uv run python -m supervisor.main

Reads configuration from environment variables (or a .env file in the repo root).
See .env.example for all available settings.
"""

import logging
import os
import pathlib
import sys

# ── Logging setup (module-level so it's set before anything else) ─────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Add repo root to sys.path ──────────────────────────────────────────────────
_REPO_DIR = pathlib.Path(__file__).parent.parent.resolve()
if str(_REPO_DIR) not in sys.path:
    sys.path.insert(0, str(_REPO_DIR))


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _require(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        log.error("Required env var %s is not set. Check your .env file.", name)
        sys.exit(1)
    return v

def _opt(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip() or default

def _opt_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, str(default)) or str(default)))
    except Exception:
        return default

def _opt_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)) or str(default))
    except Exception:
        return default


# ──────────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    import datetime
    import queue as _queue_mod
    import threading
    import time
    import types
    import uuid
    from typing import Any, Optional

    # ── Config ────────────────────────────────────────────────────────────────
    TELEGRAM_BOT_TOKEN = _require("TELEGRAM_BOT_TOKEN")
    GITHUB_TOKEN       = _require("GITHUB_TOKEN")
    GITHUB_USER        = _require("GITHUB_USER")
    GITHUB_REPO        = _require("GITHUB_REPO")
    TOTAL_BUDGET_LIMIT = _opt_float("TOTAL_BUDGET", 0.0)

    DRIVE_ROOT = pathlib.Path(
        _opt("OUROBOROS_DRIVE_ROOT", str(pathlib.Path.home() / ".ouroboros"))
    ).resolve()
    REPO_DIR = pathlib.Path(
        _opt("OUROBOROS_REPO_DIR", str(_REPO_DIR))
    ).resolve()

    BRANCH_DEV    = _opt("OUROBOROS_BRANCH_DEV",    "ouroboros")
    BRANCH_STABLE = _opt("OUROBOROS_BRANCH_STABLE", "ouroboros-stable")
    REMOTE_URL    = (
        f"https://{GITHUB_TOKEN}:x-oauth-basic@github.com/{GITHUB_USER}/{GITHUB_REPO}.git"
    )

    MAX_WORKERS          = _opt_int("OUROBOROS_MAX_WORKERS", 3)
    SOFT_TIMEOUT_SEC     = _opt_int("OUROBOROS_SOFT_TIMEOUT_SEC", 600)
    HARD_TIMEOUT_SEC     = _opt_int("OUROBOROS_HARD_TIMEOUT_SEC", 1800)
    DIAG_HEARTBEAT_SEC   = _opt_int("OUROBOROS_DIAG_HEARTBEAT_SEC", 30)
    DIAG_SLOW_CYCLE_SEC  = _opt_int("OUROBOROS_DIAG_SLOW_CYCLE_SEC", 20)
    BUDGET_REPORT_EVERY  = 10
    _ACTIVE_MODE_SEC     = 300

    log.info("Drive root : %s", DRIVE_ROOT)
    log.info("Repo dir   : %s", REPO_DIR)
    log.info("Model      : %s", _opt("OUROBOROS_MODEL", "(not set)"))

    # ── Create Drive dirs ─────────────────────────────────────────────────────
    for sub in ("state", "logs", "memory", "memory/knowledge", "memory/owner_mailbox",
                "index", "locks", "archive", "task_results"):
        (DRIVE_ROOT / sub).mkdir(parents=True, exist_ok=True)

    chat_log_path = DRIVE_ROOT / "logs" / "chat.jsonl"
    if not chat_log_path.exists():
        chat_log_path.write_text("", encoding="utf-8")

    # Propagate env vars so sub-modules read consistent values
    os.environ.setdefault("OUROBOROS_DRIVE_ROOT", str(DRIVE_ROOT))
    os.environ.setdefault("GITHUB_USER",  GITHUB_USER)
    os.environ.setdefault("GITHUB_REPO",  GITHUB_REPO)

    # ── Supervisor modules ────────────────────────────────────────────────────
    from supervisor.state import (
        init as state_init, load_state, save_state, append_jsonl,
        update_budget_from_usage, status_text, rotate_chat_log_if_needed,
        init_state,
    )
    state_init(DRIVE_ROOT, TOTAL_BUDGET_LIMIT)
    init_state()

    from supervisor.telegram import (
        init as telegram_init, TelegramClient, send_with_budget, log_chat,
    )
    TG = TelegramClient(str(TELEGRAM_BOT_TOKEN))
    telegram_init(
        drive_root=DRIVE_ROOT,
        total_budget_limit=TOTAL_BUDGET_LIMIT,
        budget_report_every=BUDGET_REPORT_EVERY,
        tg_client=TG,
    )

    from supervisor.git_ops import (
        init as git_ops_init, ensure_repo_present, safe_restart,
    )
    git_ops_init(
        repo_dir=REPO_DIR, drive_root=DRIVE_ROOT, remote_url=REMOTE_URL,
        branch_dev=BRANCH_DEV, branch_stable=BRANCH_STABLE,
    )

    from supervisor.queue import (
        enqueue_task, enforce_task_timeouts, enqueue_evolution_task_if_needed,
        persist_queue_snapshot, restore_pending_from_snapshot,
        cancel_task_by_id, queue_review_task, sort_pending,
    )
    from supervisor.workers import (
        init as workers_init, get_event_q, WORKERS, PENDING, RUNNING,
        spawn_workers, kill_workers, assign_tasks, ensure_workers_healthy,
        handle_chat_direct, _get_chat_agent, auto_resume_after_restart,
    )
    workers_init(
        repo_dir=REPO_DIR, drive_root=DRIVE_ROOT, max_workers=MAX_WORKERS,
        soft_timeout=SOFT_TIMEOUT_SEC, hard_timeout=HARD_TIMEOUT_SEC,
        total_budget_limit=TOTAL_BUDGET_LIMIT,
        branch_dev=BRANCH_DEV, branch_stable=BRANCH_STABLE,
    )

    from supervisor.events import dispatch_event

    # ── Bootstrap git repo ────────────────────────────────────────────────────
    # Skip bootstrap if REPO_DIR already has a .git (local dev mode).
    # Only do a full clone+checkout when starting from scratch (Colab / CI).
    _skip_bootstrap = os.environ.get("OUROBOROS_SKIP_BOOTSTRAP", "").strip() in ("1", "true", "yes")
    _repo_exists = (REPO_DIR / ".git").exists()

    if _skip_bootstrap or _repo_exists:
        if _repo_exists:
            # Just configure the remote and record current branch/SHA in state
            import subprocess as _sp
            _sp.run(["git", "remote", "set-url", "origin", REMOTE_URL],
                    cwd=str(REPO_DIR), check=False)
            _sp.run(["git", "config", "user.name", "Ouroboros"],
                    cwd=str(REPO_DIR), check=False)
            _sp.run(["git", "config", "user.email", "ouroboros@users.noreply.github.com"],
                    cwd=str(REPO_DIR), check=False)
            _branch = _sp.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=str(REPO_DIR), capture_output=True, text=True,
            ).stdout.strip() or "unknown"
            _sha = _sp.run(
                ["git", "rev-parse", "HEAD"],
                cwd=str(REPO_DIR), capture_output=True, text=True,
            ).stdout.strip() or "unknown"
            _st = load_state()
            _st["current_branch"] = _branch
            _st["current_sha"] = _sha
            save_state(_st)
            log.info("Local repo detected — skipping clone/checkout (branch=%s sha=%s)", _branch, _sha[:8])
    else:
        # First-time setup: clone and checkout the dev branch
        ensure_repo_present()
        ok, msg = safe_restart(reason="bootstrap", unsynced_policy="rescue_and_reset")
        if not ok:
            log.error("Bootstrap failed: %s", msg)
            sys.exit(1)

    # ── Start workers ─────────────────────────────────────────────────────────
    kill_workers()
    spawn_workers(MAX_WORKERS)
    restored = restore_pending_from_snapshot()
    persist_queue_snapshot(reason="startup")
    if restored > 0:
        st_boot = load_state()
        if st_boot.get("owner_chat_id"):
            send_with_budget(
                int(st_boot["owner_chat_id"]),
                f"♻️ Restored {restored} pending tasks from queue snapshot.",
            )

    append_jsonl(DRIVE_ROOT / "logs" / "supervisor.jsonl", {
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "type": "launcher_start",
        "branch": load_state().get("current_branch"),
        "sha": load_state().get("current_sha"),
        "max_workers": MAX_WORKERS,
        "model": _opt("OUROBOROS_MODEL"),
        "soft_timeout_sec": SOFT_TIMEOUT_SEC,
        "hard_timeout_sec": HARD_TIMEOUT_SEC,
        "drive_root": str(DRIVE_ROOT),
        "repo_dir": str(REPO_DIR),
    })
    auto_resume_after_restart()

    # ── Background consciousness ───────────────────────────────────────────────
    from ouroboros.consciousness import BackgroundConsciousness

    def _get_owner_chat_id() -> Optional[int]:
        try:
            cid = load_state().get("owner_chat_id")
            return int(cid) if cid else None
        except Exception:
            return None

    _consciousness = BackgroundConsciousness(
        drive_root=DRIVE_ROOT,
        repo_dir=REPO_DIR,
        event_queue=get_event_q(),
        owner_chat_id_fn=_get_owner_chat_id,
    )
    try:
        _consciousness.start()
        log.info("🧠 Background consciousness started")
    except Exception as e:
        log.warning("Consciousness auto-start failed: %s", e)

    # ── Event context for dispatch_event ──────────────────────────────────────
    _event_ctx = types.SimpleNamespace(
        DRIVE_ROOT=DRIVE_ROOT, REPO_DIR=REPO_DIR,
        BRANCH_DEV=BRANCH_DEV, BRANCH_STABLE=BRANCH_STABLE,
        TG=TG, WORKERS=WORKERS, PENDING=PENDING, RUNNING=RUNNING,
        MAX_WORKERS=MAX_WORKERS,
        send_with_budget=send_with_budget,
        load_state=load_state, save_state=save_state,
        update_budget_from_usage=update_budget_from_usage,
        append_jsonl=append_jsonl,
        enqueue_task=enqueue_task, cancel_task_by_id=cancel_task_by_id,
        queue_review_task=queue_review_task,
        persist_queue_snapshot=persist_queue_snapshot,
        safe_restart=safe_restart,
        kill_workers=kill_workers, spawn_workers=spawn_workers,
        sort_pending=sort_pending,
        consciousness=_consciousness,
    )

    # ── Supervisor slash-commands ─────────────────────────────────────────────
    def _handle_supervisor_command(text: str, chat_id: int, tg_offset: int = 0):
        lowered = text.strip().lower()

        if lowered.startswith("/panic"):
            send_with_budget(chat_id, "🛑 PANIC: stopping everything now.")
            kill_workers()
            st2 = load_state(); st2["tg_offset"] = tg_offset; save_state(st2)
            raise SystemExit("PANIC")

        if lowered.startswith("/restart"):
            st2 = load_state(); st2["session_id"] = uuid.uuid4().hex
            st2["tg_offset"] = tg_offset; save_state(st2)
            send_with_budget(chat_id, "♻️ Restarting.")
            ok2, msg2 = safe_restart(reason="owner_restart", unsynced_policy="rescue_and_reset")
            if not ok2:
                send_with_budget(chat_id, f"⚠️ Restart cancelled: {msg2}")
                return True
            kill_workers()
            os.execv(sys.executable, [sys.executable, "-m", "supervisor.main"])

        if lowered.startswith("/status"):
            send_with_budget(
                chat_id,
                status_text(WORKERS, PENDING, RUNNING, SOFT_TIMEOUT_SEC, HARD_TIMEOUT_SEC),
                force_budget=True,
            )
            return "[Supervisor handled /status]\n"

        if lowered.startswith("/review"):
            queue_review_task(reason="owner:/review", force=True)
            return "[Supervisor handled /review — review task queued]\n"

        if lowered.startswith("/evolve"):
            parts = lowered.split()
            turn_on = (parts[1] if len(parts) > 1 else "on") not in ("off", "stop", "0")
            st2 = load_state(); st2["evolution_mode_enabled"] = bool(turn_on); save_state(st2)
            if not turn_on:
                PENDING[:] = [t for t in PENDING if str(t.get("type")) != "evolution"]
                sort_pending(); persist_queue_snapshot(reason="evolve_off")
            state_str = "ON" if turn_on else "OFF"
            send_with_budget(chat_id, f"🧬 Evolution: {state_str}")
            return f"[Supervisor handled /evolve — evolution toggled {state_str}]\n"

        if lowered.startswith("/bg"):
            parts = lowered.split()
            action = parts[1] if len(parts) > 1 else "status"
            if action in ("start", "on", "1"):
                send_with_budget(chat_id, f"🧠 {_consciousness.start()}")
            elif action in ("stop", "off", "0"):
                send_with_budget(chat_id, f"🧠 {_consciousness.stop()}")
            else:
                bg_st = "running" if _consciousness.is_running else "stopped"
                send_with_budget(chat_id, f"🧠 Background consciousness: {bg_st}")
            return f"[Supervisor handled /bg {action}]\n"

        return ""

    # ── Main Telegram polling loop ─────────────────────────────────────────────
    def _safe_qsize(q: Any) -> int:
        try:
            return int(q.qsize())
        except Exception:
            return -1

    log.info("🚀 Ouroboros supervisor started. Listening on Telegram...")

    offset = int(load_state().get("tg_offset") or 0)
    _last_diag_heartbeat_ts = 0.0
    _last_message_ts: float = time.time()

    while True:
        loop_started_ts = time.time()
        rotate_chat_log_if_needed(DRIVE_ROOT)
        ensure_workers_healthy()

        event_q = get_event_q()
        while True:
            try:
                evt = event_q.get_nowait()
            except _queue_mod.Empty:
                break
            dispatch_event(evt, _event_ctx)

        enforce_task_timeouts()
        enqueue_evolution_task_if_needed()
        assign_tasks()
        persist_queue_snapshot(reason="main_loop")

        _now = time.time()
        _active = (_now - _last_message_ts) < _ACTIVE_MODE_SEC
        _poll_timeout = 0 if _active else 10
        try:
            updates = TG.get_updates(offset=offset, timeout=_poll_timeout)
        except Exception as e:
            append_jsonl(DRIVE_ROOT / "logs" / "supervisor.jsonl", {
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "type": "telegram_poll_error", "offset": offset, "error": repr(e),
            })
            time.sleep(1.5)
            continue

        for upd in updates:
            offset = int(upd["update_id"]) + 1
            msg = upd.get("message") or upd.get("edited_message") or {}
            if not msg:
                continue

            chat_id = int(msg["chat"]["id"])
            from_user = msg.get("from") or {}
            user_id = int(from_user.get("id") or 0)
            text = str(msg.get("text") or "")
            caption = str(msg.get("caption") or "")

            image_data = None
            if msg.get("photo"):
                best_photo = msg["photo"][-1]
                b64, mime = TG.download_file_base64(best_photo.get("file_id"))
                if b64:
                    image_data = (b64, mime, caption)
            elif msg.get("document"):
                doc = msg["document"]
                if str(doc.get("mime_type") or "").startswith("image/"):
                    b64, mime = TG.download_file_base64(doc.get("file_id"))
                    if b64:
                        image_data = (b64, mime, caption)

            st = load_state()
            if st.get("owner_id") is None:
                st["owner_id"] = user_id
                st["owner_chat_id"] = chat_id
                st["last_owner_message_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
                save_state(st)
                log_chat("in", chat_id, user_id, text)
                send_with_budget(chat_id, "✅ Owner registered. Ouroboros online.")
                continue

            if user_id != int(st.get("owner_id")):
                continue

            log_chat("in", chat_id, user_id, text)
            st["last_owner_message_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            _last_message_ts = time.time()
            save_state(st)

            if text.strip().lower().startswith("/"):
                try:
                    result = _handle_supervisor_command(text, chat_id, tg_offset=offset)
                    if result is True:
                        continue
                    elif result:
                        text = result + text
                except SystemExit:
                    raise
                except Exception:
                    log.warning("Supervisor command handler error", exc_info=True)

            if not text and not image_data:
                continue

            _consciousness.inject_observation(f"Owner message: {text[:100]}")
            agent = _get_chat_agent()

            if agent._busy:
                if image_data:
                    send_with_budget(chat_id, "📎 Photo received but a task is in progress.")
                elif text:
                    agent.inject_message(text)
            else:
                _consciousness.pause()
                def _run_and_resume(cid, txt, img):
                    try:
                        handle_chat_direct(cid, txt, img)
                    finally:
                        _consciousness.resume()
                t = threading.Thread(target=_run_and_resume, args=(chat_id, text, image_data), daemon=True)
                try:
                    t.start()
                except Exception as te:
                    log.error("Failed to start chat thread: %s", te)
                    _consciousness.resume()

        st = load_state()
        st["tg_offset"] = offset
        save_state(st)

        loop_duration_sec = time.time() - loop_started_ts
        if DIAG_SLOW_CYCLE_SEC > 0 and loop_duration_sec >= float(DIAG_SLOW_CYCLE_SEC):
            append_jsonl(DRIVE_ROOT / "logs" / "supervisor.jsonl", {
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "type": "main_loop_slow_cycle",
                "duration_sec": round(loop_duration_sec, 3),
            })

        now_epoch = time.time()
        if DIAG_HEARTBEAT_SEC > 0 and (now_epoch - _last_diag_heartbeat_ts) >= float(DIAG_HEARTBEAT_SEC):
            workers_alive = sum(1 for w in WORKERS.values() if w.proc.is_alive())
            append_jsonl(DRIVE_ROOT / "logs" / "supervisor.jsonl", {
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "type": "main_loop_heartbeat", "offset": offset,
                "workers_total": len(WORKERS), "workers_alive": workers_alive,
                "pending_count": len(PENDING), "running_count": len(RUNNING),
                "event_q_size": _safe_qsize(event_q),
            })
            _last_diag_heartbeat_ts = now_epoch

        time.sleep(0.1 if _active else 0.5)


if __name__ == "__main__":
    main()
