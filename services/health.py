"""
services/health.py - quick "is the toolchain ready?" checks for the dashboard.

Both checks are cheap and bounded by short timeouts so the dashboard never hangs.
"""

import subprocess
import time

import ollama

from agents.agent3_analyzer import AnalysisAgent
from config import Config

_CACHE: dict = {"at": 0.0, "value": None}
_CACHE_SECONDS = 20


def _check_beyondseo(cfg: Config) -> dict:
    cmd = [*cfg.beyondseo_cmd, "--version"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=15, check=False)
    except FileNotFoundError:
        return {"ok": False, "detail": f"Command not found: {cmd[0]}"}
    except (subprocess.TimeoutExpired, OSError) as exc:
        return {"ok": False, "detail": str(exc)[:150]}
    if proc.returncode != 0:
        return {"ok": False, "detail": (proc.stderr or proc.stdout).strip()[:150] or f"exit code {proc.returncode}"}
    return {"ok": True, "detail": (proc.stdout or proc.stderr).strip()[:80]}


def _check_ollama(cfg: Config) -> dict:
    if not cfg.enable_agent3:
        return {"ok": None, "detail": "Not used (Agent 3 is disabled in config.json)"}
    try:
        client = ollama.Client(host=cfg.ollama_host, timeout=3)
        names = AnalysisAgent._model_names(client.list())
    except Exception as exc:  # noqa: BLE001 - any failure means "not reachable"
        return {"ok": False, "detail": f"Not reachable at {cfg.ollama_host}"}
    if not any(AnalysisAgent._same_model(cfg.ollama_model, name) for name in names):
        return {"ok": False, "detail": f"Model '{cfg.ollama_model}' is not installed (ollama pull {cfg.ollama_model})"}
    return {"ok": True, "detail": f"Model {cfg.ollama_model} ready"}


def system_status(cfg: Config) -> dict:
    now = time.monotonic()
    if _CACHE["value"] is not None and now - _CACHE["at"] < _CACHE_SECONDS:
        return _CACHE["value"]
    value = {
        "beyondseo": _check_beyondseo(cfg),
        "ollama": _check_ollama(cfg),
        "engine": "agent3" if (cfg.enable_agent3 and not cfg.enable_agent4) else "agent4" if cfg.enable_agent4 else "agent3",
    }
    _CACHE.update(at=now, value=value)
    return value
