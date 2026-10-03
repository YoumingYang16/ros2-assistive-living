"""Trusted-host communication contract; no built-in transport or network calls.

The application must configure a provider key explicitly and implement its own
transport. Receipt authentication establishes the provider's report only. It
does not prove a recipient read a message, arrived, or completed physical care.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import hmac
import json
import re
from typing import Protocol

from .contracts import CommandError


RECEIPT_FIELDS = {"version", "receipt_id", "request_id", "contact_id", "provider", "status", "occurred_at", "delivery_id"}


class AssistanceTransport(Protocol):
    """Implement outside the default application, using explicit user consent.

    ``send`` must return a signed receipt, never an optimistic success flag.
    Asynchronous providers may call the host's receipt handler later instead.
    This protocol is not invoked by AssistiveService or by any default route.
    """

    def send(self, envelope: dict) -> dict: ...


def _canonical(body):
    try:
        return json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise CommandError("通信回执必须是有限 JSON 数据") from exc


def sign_receipt(body: dict, key: bytes) -> dict:
    """Provider-side helper. Keys belong in host configuration, never the UI."""
    if not isinstance(key, bytes) or len(key) < 32:
        raise CommandError("通信验证密钥至少需要 32 字节")
    if not isinstance(body, dict) or set(body) != RECEIPT_FIELDS:
        raise CommandError("通信回执字段不匹配")
    return {**body, "signature": hmac.new(key, _canonical(body), hashlib.sha256).hexdigest()}


class DeliveryReceiptVerifier:
    def __init__(self, provider: str, key: bytes, *, clock=None, max_age_seconds=300):
        if not isinstance(provider, str) or not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_.-]{0,63}", provider):
            raise CommandError("通信提供方标识无效")
        if not isinstance(key, bytes) or len(key) < 32:
            raise CommandError("通信验证密钥至少需要 32 字节")
        if isinstance(max_age_seconds, bool) or not isinstance(max_age_seconds, int) or not 1 <= max_age_seconds <= 3600:
            raise CommandError("通信回执有效期须在 1 到 3600 秒之间")
        self.provider = provider
        self._key = key
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.max_age_seconds = max_age_seconds

    def verify(self, receipt: dict) -> dict:
        if not isinstance(receipt, dict) or set(receipt) != RECEIPT_FIELDS | {"signature"}:
            raise CommandError("通信回执字段不匹配")
        body = {key: value for key, value in receipt.items() if key != "signature"}
        if not isinstance(body["version"], int) or body["version"] != 1 or isinstance(body["version"], bool):
            raise CommandError("通信回执协议版本不支持")
        if body["provider"] != self.provider:
            raise CommandError("通信提供方不匹配")
        for field in ("receipt_id", "request_id", "contact_id", "delivery_id"):
            value = body[field]
            if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9_.:-]{1,128}", value):
                raise CommandError(f"通信回执 {field} 无效")
        if not isinstance(body["status"], str) or body["status"] not in {"delivered", "acknowledged", "failed"}:
            raise CommandError("通信回执状态不支持")
        signature = receipt["signature"]
        if not isinstance(signature, str) or not re.fullmatch(r"[0-9a-f]{64}", signature):
            raise CommandError("通信回执签名无效")
        expected = hmac.new(self._key, _canonical(body), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise CommandError("通信回执签名验证失败")
        try:
            occurred = datetime.fromisoformat(body["occurred_at"].replace("Z", "+00:00"))
            now = self._clock()
            if occurred.tzinfo is None or now.tzinfo is None:
                raise ValueError("timezone required")
            age = (now - occurred).total_seconds()
            if age < -30 or age > self.max_age_seconds:
                raise ValueError("outside receipt window")
        except (TypeError, ValueError, AttributeError, OverflowError) as exc:
            raise CommandError("通信回执时间无效或已超出有效期") from exc
        return body
