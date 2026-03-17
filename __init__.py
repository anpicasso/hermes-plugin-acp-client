"""
ACP Client Plugin (stdio transport)
====================================

Connects Hermes to agents running ACP (Agent Client Protocol) servers
over stdio — primarily OpenCode. Spawns ``opencode acp`` as a subprocess,
communicates via NDJSON (newline-delimited JSON-RPC 2.0).

Tools provided:
  acp_agents   — discover available agents/modes
  acp_send     — send a prompt and get the response
  acp_sessions — list active sessions
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════
#  Config
# ═══════════════════════════════════════════════════════════════════════════

_CONFIG_FILE = "config.yaml"
_cached_config: Optional[Dict[str, Any]] = None


def _plugin_dir() -> Path:
    return Path(__file__).parent


def _load_config(reload: bool = False) -> Dict[str, Any]:
    global _cached_config
    if _cached_config is not None and not reload:
        return _cached_config
    path = _plugin_dir() / _CONFIG_FILE
    if not path.exists() or yaml is None:
        _cached_config = {}
        return _cached_config
    with open(path) as f:
        _cached_config = yaml.safe_load(f) or {}
    return _cached_config


# ═══════════════════════════════════════════════════════════════════════════
#  ACP Client — stdio NDJSON transport
# ═══════════════════════════════════════════════════════════════════════════


class ACPClient:
    """Manage an ACP server subprocess with bidirectional JSON-RPC 2.0."""

    def __init__(self, binary: str, default_cwd: str, auto_approve: bool = True):
        self.binary = os.path.expanduser(binary)
        self.default_cwd = os.path.expanduser(default_cwd)
        self.auto_approve = auto_approve
        self.process: Optional[subprocess.Popen] = None

        # JSON-RPC bookkeeping
        self._msg_id = 0
        self._lock = threading.Lock()
        self._resp_events: Dict[int, threading.Event] = {}
        self._resp_data: Dict[int, dict] = {}

        # Streaming update collectors (session_id → list of updates)
        self._update_queues: Dict[str, List[dict]] = {}

        # Terminal subprocess management
        self._terminals: Dict[str, subprocess.Popen] = {}
        self._term_bufs: Dict[str, bytearray] = {}

        self._reader: Optional[threading.Thread] = None
        self._alive = False
        self.agent_info: Optional[dict] = None

    # ── ID generation ──────────────────────────────────────────────────

    def _next_id(self) -> int:
        with self._lock:
            self._msg_id += 1
            return self._msg_id

    # ── Lifecycle ──────────────────────────────────────────────────────

    def start(self) -> dict:
        """Spawn opencode acp and perform the initialize handshake."""
        if self._alive:
            return {"already_started": True}

        if not os.path.isfile(self.binary):
            raise FileNotFoundError(f"OpenCode binary not found: {self.binary}")

        cmd = [self.binary, "acp", "--cwd", self.default_cwd]
        logger.info("Starting ACP subprocess: %s", " ".join(cmd))

        self.process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

        self._alive = True  # must be set BEFORE reader starts
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

        resp = self._request("initialize", {
            "protocolVersion": 1,
            "clientInfo": {"name": "hermes-acp-client", "version": "1.0.0"},
            "clientCapabilities": {
                "fs": {"readTextFile": True, "writeTextFile": True},
                "terminal": True,
                "auth": {"terminal": True},
            },
        })

        if "result" in resp:
            self.agent_info = resp["result"].get("agentInfo")
        return resp

    def stop(self):
        """Terminate the ACP subprocess and clean up."""
        self._alive = False
        if self.process:
            try:
                self.process.stdin.close()
            except Exception:
                pass
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except Exception:
                self.process.kill()
            self.process = None

        for proc in self._terminals.values():
            try:
                proc.kill()
            except Exception:
                pass
        self._terminals.clear()
        self._term_bufs.clear()

    @property
    def is_alive(self) -> bool:
        return (
            self._alive
            and self.process is not None
            and self.process.poll() is None
        )

    # ── Wire protocol ─────────────────────────────────────────────────

    def _send(self, msg: dict):
        if not self.process or not self.process.stdin:
            raise ConnectionError("ACP process not running")
        payload = json.dumps(msg, separators=(",", ":")).encode() + b"\n"
        self.process.stdin.write(payload)
        self.process.stdin.flush()

    def _request(self, method: str, params: dict = None, timeout: int = 300) -> dict:
        mid = self._next_id()
        evt = threading.Event()
        self._resp_events[mid] = evt

        msg: dict = {"jsonrpc": "2.0", "id": mid, "method": method}
        if params is not None:
            msg["params"] = params

        self._send(msg)

        if not evt.wait(timeout=timeout):
            self._resp_events.pop(mid, None)
            raise TimeoutError(f"ACP timeout ({timeout}s) on {method}")

        data = self._resp_data.pop(mid, {})
        self._resp_events.pop(mid, None)
        return data

    # ── Reader thread ─────────────────────────────────────────────────

    def _read_loop(self):
        while self._alive and self.process and self.process.stdout:
            try:
                line = self.process.stdout.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                msg = json.loads(line)
                self._dispatch(msg)
            except json.JSONDecodeError:
                continue
            except Exception as exc:
                logger.error("ACP reader error: %s", exc)
                break
        self._alive = False
        # Wake any pending waiters so they don't hang forever
        for evt in list(self._resp_events.values()):
            evt.set()

    def _dispatch(self, msg: dict):
        # 1) Response to our request
        if "id" in msg and ("result" in msg or "error" in msg) and "method" not in msg:
            mid = msg["id"]
            if mid in self._resp_events:
                self._resp_data[mid] = msg
                self._resp_events[mid].set()
            return

        # 2) Notification (no id)
        if "method" in msg and "id" not in msg:
            self._on_notification(msg)
            return

        # 3) Agent→client request  (has id AND method) — handle in own thread
        if "method" in msg and "id" in msg:
            threading.Thread(
                target=self._on_agent_request, args=(msg,), daemon=True,
            ).start()

    # ── Notifications ─────────────────────────────────────────────────

    def _on_notification(self, msg: dict):
        method = msg.get("method")
        params = msg.get("params", {})
        if method == "session/update":
            sid = params.get("sessionId", "")
            upd = params.get("update", {})
            if sid in self._update_queues:
                self._update_queues[sid].append(upd)

    # ── Agent → Client requests ───────────────────────────────────────

    def _on_agent_request(self, msg: dict):
        method = msg["method"]
        mid = msg["id"]
        params = msg.get("params", {})

        try:
            if method == "session/request_permission":
                self._do_permission(mid, params)
            elif method == "fs/read_text_file":
                self._do_fs_read(mid, params)
            elif method == "fs/write_text_file":
                self._do_fs_write(mid, params)
            elif method.startswith("terminal/"):
                self._do_terminal(method.split("/", 1)[-1], mid, params)
            else:
                self._send({
                    "jsonrpc": "2.0", "id": mid,
                    "error": {"code": -32601, "message": f"Unsupported: {method}"},
                })
        except Exception as exc:
            logger.error("Error handling %s: %s", method, exc)
            try:
                self._send({
                    "jsonrpc": "2.0", "id": mid,
                    "error": {"code": -32603, "message": str(exc)},
                })
            except Exception:
                pass

    def _do_permission(self, mid: int, params: dict):
        oid = "allow_once"
        if self.auto_approve:
            # Prefer allow_always, fall back to allow_once
            for opt in params.get("options", []):
                if opt.get("kind") == "allow_always":
                    oid = opt["optionId"]
                    break
            else:
                for opt in params.get("options", []):
                    if opt.get("kind") == "allow_once":
                        oid = opt["optionId"]
                        break
        self._send({"jsonrpc": "2.0", "id": mid, "result": {"optionId": oid}})

    def _do_fs_read(self, mid: int, params: dict):
        path = params.get("path", "")
        try:
            with open(path) as f:
                content = f.read()
            self._send({"jsonrpc": "2.0", "id": mid, "result": {"content": content}})
        except Exception as exc:
            self._send({
                "jsonrpc": "2.0", "id": mid,
                "error": {"code": -32603, "message": str(exc)},
            })

    def _do_fs_write(self, mid: int, params: dict):
        path = params.get("path", "")
        content = params.get("content", "")
        try:
            dirname = os.path.dirname(path)
            if dirname:
                os.makedirs(dirname, exist_ok=True)
            with open(path, "w") as f:
                f.write(content)
            self._send({"jsonrpc": "2.0", "id": mid, "result": {}})
        except Exception as exc:
            self._send({
                "jsonrpc": "2.0", "id": mid,
                "error": {"code": -32603, "message": str(exc)},
            })

    # ── Terminal handling ─────────────────────────────────────────────

    def _do_terminal(self, op: str, mid: int, params: dict):
        if op == "create":
            tid = f"t-{uuid.uuid4().hex[:8]}"
            command = params.get("command", "")
            args = params.get("args", [])
            cwd = params.get("cwd", self.default_cwd)
            env = {**os.environ, **(params.get("env") or {})}

            cmd = [command] + args if args else command
            proc = subprocess.Popen(
                cmd,
                shell=isinstance(cmd, str),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=cwd,
                env=env,
            )
            self._terminals[tid] = proc
            self._term_bufs[tid] = bytearray()
            # Background reader for terminal stdout
            threading.Thread(
                target=self._term_reader, args=(tid,), daemon=True,
            ).start()
            self._send({"jsonrpc": "2.0", "id": mid, "result": {"terminalId": tid}})

        elif op == "output":
            tid = params.get("terminalId", "")
            buf = self._term_bufs.get(tid, bytearray())
            out = buf.decode("utf-8", errors="replace")
            if tid in self._term_bufs:
                self._term_bufs[tid] = bytearray()
            self._send({"jsonrpc": "2.0", "id": mid, "result": {"output": out}})

        elif op == "wait_for_exit":
            tid = params.get("terminalId", "")
            proc = self._terminals.get(tid)
            if not proc:
                self._send({
                    "jsonrpc": "2.0", "id": mid,
                    "error": {"code": -32002, "message": f"Terminal not found: {tid}"},
                })
                return
            try:
                exit_code = proc.wait(timeout=600)
            except subprocess.TimeoutExpired:
                proc.kill()
                exit_code = -1
            time.sleep(0.1)  # let reader catch final output
            out = self._term_bufs.get(tid, bytearray()).decode("utf-8", errors="replace")
            self._send({
                "jsonrpc": "2.0", "id": mid,
                "result": {"exitCode": exit_code, "output": out},
            })

        elif op == "kill":
            tid = params.get("terminalId", "")
            proc = self._terminals.get(tid)
            if proc:
                proc.kill()
            self._send({"jsonrpc": "2.0", "id": mid, "result": {}})

        elif op == "release":
            tid = params.get("terminalId", "")
            self._terminals.pop(tid, None)
            self._term_bufs.pop(tid, None)
            self._send({"jsonrpc": "2.0", "id": mid, "result": {}})

        else:
            self._send({
                "jsonrpc": "2.0", "id": mid,
                "error": {"code": -32601, "message": f"Unknown terminal op: {op}"},
            })

    def _term_reader(self, tid: str):
        """Background reader for a terminal subprocess's stdout."""
        proc = self._terminals.get(tid)
        if not proc or not proc.stdout:
            return
        while True:
            chunk = proc.stdout.read(4096)
            if not chunk:
                break
            if tid in self._term_bufs:
                self._term_bufs[tid].extend(chunk)

    # ── High-level API ────────────────────────────────────────────────

    def new_session(self, cwd: str = None) -> dict:
        """Create a new ACP session for a workspace."""
        return self._request("session/new", {
            "cwd": os.path.expanduser(cwd or self.default_cwd),
            "mcpServers": [],
        })

    def prompt(self, session_id: str, text: str, timeout: int = 600) -> dict:
        """Send a prompt and collect all streaming updates.

        Returns ``{"wire": <rpc response>, "updates": [<update dicts>]}``.
        On timeout, sends session/cancel and returns partial updates.
        """
        self._update_queues[session_id] = []
        try:
            result = self._request("session/prompt", {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": text}],
            }, timeout=timeout)
        except TimeoutError:
            # Cancel in-progress prompt
            try:
                self._send({
                    "jsonrpc": "2.0",
                    "method": "session/cancel",
                    "params": {"sessionId": session_id},
                })
            except Exception:
                pass
            updates = self._update_queues.pop(session_id, [])
            return {
                "wire": {"result": {"stopReason": "timeout"}},
                "updates": updates,
            }

        updates = self._update_queues.pop(session_id, [])
        return {"wire": result, "updates": updates}

    def set_mode(self, session_id: str, mode_id: str) -> dict:
        """Switch the agent/mode for a session."""
        return self._request("session/set_mode", {
            "sessionId": session_id, "modeId": mode_id,
        })

    def list_sessions(self, cwd: str = None) -> dict:
        """List existing sessions, optionally filtered by workspace."""
        params: dict = {}
        if cwd:
            params["cwd"] = os.path.expanduser(cwd)
        return self._request("session/list", params)


# ═══════════════════════════════════════════════════════════════════════════
#  Singleton client
# ═══════════════════════════════════════════════════════════════════════════

_client: Optional[ACPClient] = None
_client_lock = threading.Lock()


def _get_client() -> ACPClient:
    """Get or create the singleton ACP client."""
    global _client
    with _client_lock:
        if _client and _client.is_alive:
            return _client
        if _client:
            _client.stop()

        cfg = _load_config(reload=True)
        binary = cfg.get("opencode_binary", "~/.opencode/bin/opencode")
        cwd = cfg.get("default_cwd", "~")
        auto = cfg.get("auto_approve", True)

        _client = ACPClient(binary, cwd, auto)
        _client.start()
        return _client


# ═══════════════════════════════════════════════════════════════════════════
#  Response assembly
# ═══════════════════════════════════════════════════════════════════════════

def _assemble(raw: dict) -> dict:
    """Turn raw prompt result + streaming updates into a clean response."""
    updates = raw.get("updates", [])
    wire = raw.get("wire", {})

    text_parts: List[str] = []
    thought_parts: List[str] = []
    tool_calls: Dict[str, dict] = {}
    plan_entries: List[dict] = []
    usage: Optional[dict] = None

    for u in updates:
        kind = u.get("sessionUpdate", "")

        if kind == "agent_message_chunk":
            c = u.get("content", {})
            if c.get("type") == "text":
                text_parts.append(c["text"])

        elif kind == "agent_thought_chunk":
            c = u.get("content", {})
            if c.get("type") == "text":
                thought_parts.append(c["text"])

        elif kind == "tool_call":
            tcid = u.get("toolCallId", "")
            tool_calls[tcid] = {
                "title": u.get("title", ""),
                "kind": u.get("kind", ""),
                "status": u.get("status", ""),
            }

        elif kind == "tool_call_update":
            tcid = u.get("toolCallId", "")
            if tcid in tool_calls:
                if u.get("status"):
                    tool_calls[tcid]["status"] = u["status"]

        elif kind == "plan":
            plan_entries = [
                {"title": e.get("title", ""), "status": e.get("status", "")}
                for e in u.get("entries", [])
            ]

        elif kind == "usage_update":
            usage = {k: v for k, v in u.items() if k != "sessionUpdate"}

    # Build result
    result: Dict[str, Any] = {
        "response": "".join(text_parts),
    }

    # Use wire-level usage if available, else streaming usage
    wire_result = wire.get("result", {})
    result["stop_reason"] = wire_result.get("stopReason", "unknown")
    if wire_result.get("usage"):
        result["usage"] = wire_result["usage"]
    elif usage:
        result["usage"] = usage

    # Include thinking if present (truncate if huge)
    thinking = "".join(thought_parts)
    if thinking:
        if len(thinking) > 3000:
            result["thinking"] = thinking[:3000] + "… [truncated]"
        else:
            result["thinking"] = thinking

    if tool_calls:
        result["tool_calls"] = list(tool_calls.values())

    if plan_entries:
        result["plan"] = plan_entries

    # Surface errors
    if "error" in wire:
        result["error"] = wire["error"]

    return result


# ═══════════════════════════════════════════════════════════════════════════
#  Tool handlers
# ═══════════════════════════════════════════════════════════════════════════

def handle_acp_agents(args: Dict[str, Any], **kwargs) -> str:
    """Discover available agents/modes from the ACP server."""
    try:
        client = _get_client()
        cwd = args.get("cwd") or client.default_cwd
        resp = client.new_session(cwd=cwd)

        if "error" in resp:
            return json.dumps({"error": resp["error"]})

        r = resp.get("result", {})
        modes = r.get("modes", {})
        models = r.get("models", {})

        agents = []
        for m in modes.get("availableModes", []):
            agents.append({
                "id": m.get("id", ""),
                "name": m.get("name", ""),
                "description": m.get("description", ""),
            })

        return json.dumps({
            "agents": agents,
            "current_agent": modes.get("currentModeId", ""),
            "current_model": models.get("currentModelId", ""),
            "available_models": [
                {"id": m.get("modelId", ""), "name": m.get("name", "")}
                for m in models.get("availableModels", [])
            ],
            "session_id": r.get("sessionId", ""),
            "cwd": cwd,
        })

    except Exception as exc:
        return json.dumps({"error": str(exc)})


def handle_acp_send(args: Dict[str, Any], **kwargs) -> str:
    """Send a prompt to an ACP agent and return the assembled response."""
    prompt_text = args.get("prompt", "").strip()
    if not prompt_text:
        return json.dumps({"error": "prompt is required"})

    try:
        client = _get_client()
        cwd = args.get("cwd") or client.default_cwd
        agent = args.get("agent", "")
        session_id = args.get("session_id", "")
        timeout = int(args.get("timeout", 600))

        # Create or reuse session
        if not session_id:
            resp = client.new_session(cwd=cwd)
            if "error" in resp:
                return json.dumps({"error": resp["error"]})

            r = resp["result"]
            session_id = r["sessionId"]
            current_mode = r.get("modes", {}).get("currentModeId", "")

            # Switch mode if requested and different
            if agent and agent != current_mode:
                mode_resp = client.set_mode(session_id, agent)
                if "error" in mode_resp:
                    return json.dumps({
                        "error": f"Failed to set agent '{agent}': {mode_resp['error']}",
                        "session_id": session_id,
                    })
        elif agent:
            # Existing session — try switching mode (ignore if same)
            client.set_mode(session_id, agent)

        # Send the prompt
        raw = client.prompt(session_id, prompt_text, timeout=timeout)
        assembled = _assemble(raw)
        assembled["session_id"] = session_id
        assembled["agent"] = agent or "default"

        return json.dumps(assembled)

    except Exception as exc:
        return json.dumps({"error": str(exc)})


def handle_acp_sessions(args: Dict[str, Any], **kwargs) -> str:
    """List active ACP sessions."""
    try:
        client = _get_client()
        cwd = args.get("cwd")
        resp = client.list_sessions(cwd=cwd)

        if "error" in resp:
            return json.dumps({"error": resp["error"]})

        sessions = resp.get("result", {}).get("sessions", [])
        return json.dumps({
            "sessions": [
                {
                    "session_id": s.get("sessionId", ""),
                    "cwd": s.get("cwd", ""),
                    "title": s.get("title", ""),
                    "updated_at": s.get("updatedAt", ""),
                }
                for s in sessions
            ],
        })

    except Exception as exc:
        return json.dumps({"error": str(exc)})


# ═══════════════════════════════════════════════════════════════════════════
#  Tool schemas
# ═══════════════════════════════════════════════════════════════════════════

ACP_AGENTS_SCHEMA = {
    "name": "acp_agents",
    "description": (
        "Discover available agents/modes from an ACP server (OpenCode). "
        "Returns agent names, descriptions, available models, and a pre-created "
        "session_id you can reuse with acp_send. Call this to see what agents "
        "are available (e.g. sisyphus, prometheus, hephaestus, code) before "
        "sending prompts."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "cwd": {
                "type": "string",
                "description": (
                    "Workspace directory for agent discovery. "
                    "Defaults to config default_cwd."
                ),
            },
        },
        "required": [],
    },
}

ACP_SEND_SCHEMA = {
    "name": "acp_send",
    "description": (
        "Send a prompt to an ACP agent (e.g. OpenCode's sisyphus, prometheus, "
        "hephaestus, atlas, code) and get the response. The agent can autonomously "
        "read/write files, run terminal commands, and use tools in the workspace. "
        "Returns the agent's text response, tool call summary, execution plan, "
        "and token usage. Use session_id to continue multi-turn conversations."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "The message or task to send to the agent.",
            },
            "agent": {
                "type": "string",
                "description": (
                    "Agent/mode name (e.g. 'sisyphus', 'prometheus', 'code'). "
                    "Use acp_agents to discover available agents. "
                    "Omit for the workspace's default agent."
                ),
            },
            "cwd": {
                "type": "string",
                "description": (
                    "Workspace directory for the session. "
                    "Defaults to config default_cwd."
                ),
            },
            "session_id": {
                "type": "string",
                "description": (
                    "Session ID from a previous acp_send or acp_agents call "
                    "to continue an existing conversation. Omit for a new session."
                ),
            },
            "timeout": {
                "type": "integer",
                "description": "Max seconds to wait for the response (default: 600).",
            },
        },
        "required": ["prompt"],
    },
}

ACP_SESSIONS_SCHEMA = {
    "name": "acp_sessions",
    "description": (
        "List active ACP sessions. Sessions persist across prompts and can be "
        "resumed by passing session_id to acp_send. Useful for checking what "
        "conversations are in progress."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "cwd": {
                "type": "string",
                "description": "Filter sessions by workspace directory.",
            },
        },
        "required": [],
    },
}


# ═══════════════════════════════════════════════════════════════════════════
#  Plugin entry point
# ═══════════════════════════════════════════════════════════════════════════

def register(ctx):
    """Called by the Hermes plugin system on load."""
    ctx.register_tool(
        name="acp_agents",
        toolset="acp",
        schema=ACP_AGENTS_SCHEMA,
        handler=handle_acp_agents,
        description="Discover available ACP agents/modes",
        emoji="🤖",
    )
    ctx.register_tool(
        name="acp_send",
        toolset="acp",
        schema=ACP_SEND_SCHEMA,
        handler=handle_acp_send,
        description="Send a prompt to an ACP agent",
        emoji="📡",
    )
    ctx.register_tool(
        name="acp_sessions",
        toolset="acp",
        schema=ACP_SESSIONS_SCHEMA,
        handler=handle_acp_sessions,
        description="List active ACP sessions",
        emoji="📋",
    )
    logger.info("acp-client plugin loaded — toolset: acp")
