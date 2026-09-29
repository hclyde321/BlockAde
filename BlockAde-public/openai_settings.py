"""Process-local OpenAI credentials; never persisted or returned to clients."""
import os
import re
import threading

_lock = threading.RLock()
_key = ""
_model = ""


def credentials():
    with _lock:
        return (_key or os.environ.get("OPENAI_API_KEY", "").strip(),
                _model or os.environ.get("OPENAI_MATCH_MODEL", "").strip() or "gpt-6-sol")


def status():
    with _lock:
        key, model = credentials()
        return {"configured": bool(key), "model": model,
                "source": "session" if _key else "environment" if key else "none"}


def configure(payload):
    global _key, _model
    key = payload.get("api_key", "")
    model = payload.get("model", "")
    if not isinstance(key, str) or not isinstance(model, str):
        raise ValueError("API 金鑰與模型名稱必須是文字。")
    key, model = key.strip(), model.strip()
    if key and (len(key) > 1024 or not re.fullmatch(r"[!-~]+", key)):
        raise ValueError("API 金鑰格式不正確，請重新貼上完整金鑰。")
    if model and not re.fullmatch(r"[A-Za-z0-9._:-]{1,120}", model):
        raise ValueError("模型名稱格式不正確。")
    with _lock:
        if not key and not credentials()[0]:
            raise ValueError("請輸入 OpenAI API 金鑰。")
        if key:
            _key = key
        _model = model
        return status()


def clear():
    global _key, _model
    with _lock:
        _key = _model = ""
        return status()
