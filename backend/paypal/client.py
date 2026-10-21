"""PayPal REST client: OAuth token caching + JSON requests."""
from __future__ import annotations

import threading
import time
from typing import Any

import requests

from backend import config


class PayPalError(RuntimeError):
    def __init__(self, status: int, body: Any):
        self.status = status
        self.body = body
        super().__init__(f"PayPal API error {status}: {body}")


def _body(r: requests.Response) -> Any:
    try:
        return r.json()
    except ValueError:
        return r.text


class PayPalClient:
    def __init__(self, client_id: str | None = None, client_secret: str | None = None,
                 base_url: str | None = None):
        self.client_id = client_id or config.PAYPAL_CLIENT_ID
        self.client_secret = client_secret or config.PAYPAL_CLIENT_SECRET
        self.base_url = (base_url or config.PAYPAL_BASE).rstrip("/")
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = threading.Lock()

    def token(self) -> str:
        with self._lock:
            if self._token and time.time() < self._expires_at - 120:
                return self._token
            if not (self.client_id and self.client_secret):
                raise PayPalError(0, "PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET are not set in .env")
            r = requests.post(
                f"{self.base_url}/v1/oauth2/token",
                auth=(self.client_id, self.client_secret),
                data={"grant_type": "client_credentials"},
                timeout=30,
            )
            if r.status_code >= 400:
                raise PayPalError(r.status_code, _body(r))
            data = r.json()
            self._token = data["access_token"]
            self._expires_at = time.time() + int(data.get("expires_in", 3600))
            return self._token

    def request(self, method: str, path: str, *, json: Any = None,
                params: dict | None = None, headers: dict | None = None) -> dict:
        h = {
            "Authorization": f"Bearer {self.token()}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        }
        if headers:
            h.update(headers)
        r = requests.request(method, f"{self.base_url}{path}", json=json,
                             params=params, headers=h, timeout=30)
        if r.status_code >= 400:
            raise PayPalError(r.status_code, _body(r))
        if not r.content:
            return {}
        try:
            return r.json()
        except ValueError:
            return {"raw": r.text}


paypal = PayPalClient()
