# -*- coding: utf-8 -*-
"""Общий HTTP-клиент для Ollama с гарантированным wall-clock таймаутом:
запрос выполняется в демоне-потоке; если дедлайн истёк — бросаем TimeoutError
и не ждём поток (сокет может дожить на сервере, процесс не блокируется)."""
import json
import threading
import urllib.request

_INFLIGHT = 0
_LOCK = threading.Lock()
_MAX_INFLIGHT = 1


def http_json(url: str, body: dict | None, deadline: float,
              headers=None) -> dict:
    global _INFLIGHT
    with _LOCK:
        if _INFLIGHT >= _MAX_INFLIGHT:
            raise TimeoutError("ollama: другой запрос уже в полёте")
        _INFLIGHT += 1
    try:
        return _http_json_locked(url, body, deadline, headers)
    finally:
        with _LOCK:
            _INFLIGHT -= 1


def _http_json_locked(url: str, body: dict | None, deadline: float,
                      headers=None) -> dict:
    headers = dict(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    box = {}

    def _work():
        try:
            req = urllib.request.Request(url, data=data, headers=headers)
            with urllib.request.urlopen(req, timeout=max(5.0, deadline)) as resp:
                box["ok"] = json.loads(resp.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            box["err"] = e

    th = threading.Thread(target=_work, daemon=True)
    th.start()
    th.join(deadline)
    if "ok" in box:
        return box["ok"]
    if "err" in box:
        raise box["err"]
    raise TimeoutError(f"ollama deadline {deadline:.0f}s exceeded")
