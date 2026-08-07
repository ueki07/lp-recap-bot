"""Client REST Discord minimal, sans connexion gateway.

C'est ce qui permet de tourner sur GitHub Actions : on lit et on écrit par
requêtes HTTP ponctuelles, au lieu de maintenir une websocket ouverte 24/7
(impossible sur Actions, où un job est plafonné à 6 h).

On réutilise `curl_cffi.requests` (déjà tiré par ugg.py) en mode HTTP normal,
sans impersonation : l'API Discord ne demande rien de particulier.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from curl_cffi import requests

API_BASE = "https://discord.com/api/v10"
TIMEOUT = 20
MAX_RETRIES = 5

log = logging.getLogger("lp-recap.discord")


class DiscordError(RuntimeError):
    pass


class DiscordClient:
    def __init__(self, token: str) -> None:
        self._session = requests.Session()
        self._headers = {
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            # Discord demande un User-Agent identifiable pour les bots.
            "User-Agent": "DiscordBot (https://github.com/, 1.0)",
        }

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        url = f"{API_BASE}{path}"
        for attempt in range(MAX_RETRIES):
            resp = self._session.request(
                method, url, headers=self._headers, timeout=TIMEOUT, **kwargs
            )

            if resp.status_code == 429:  # rate limit : Discord dit combien attendre
                retry_after = 1.0
                try:
                    retry_after = float(resp.json().get("retry_after", 1.0))
                except Exception:
                    pass
                log.warning("rate limit Discord, pause de %.1fs", retry_after)
                time.sleep(retry_after + 0.1)
                continue

            if resp.status_code in (500, 502, 503, 504):
                time.sleep(2 ** attempt)
                continue

            if resp.status_code == 401:
                raise DiscordError("token Discord invalide (401)")
            if resp.status_code == 403:
                raise DiscordError(
                    f"permission refusée sur {path} (403). Vérifie que le bot est "
                    "sur le serveur et qu'il a accès au salon."
                )
            if resp.status_code >= 400:
                raise DiscordError(f"{method} {path} -> {resp.status_code} {resp.text[:200]}")

            if resp.status_code == 204 or not resp.content:
                return None
            return resp.json()

        raise DiscordError(f"{method} {path} : abandon après {MAX_RETRIES} tentatives")

    def get_messages(
        self,
        channel_id: int,
        after: int | None = None,
        before: int | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """Messages du salon, du plus ancien au plus récent.

        `after` : ID de message (snowflake) ; Discord ne renvoie que ce qui est
        postérieur. C'est ce qui évite de retraiter d'anciennes commandes.
        `before` : l'inverse, pour remonter l'historique page par page.
        """
        params = {"limit": str(min(limit, 100))}
        if after:
            params["after"] = str(after)
        if before:
            params["before"] = str(before)
        messages = self._request("GET", f"/channels/{channel_id}/messages", params=params)
        # L'API renvoie du plus récent au plus ancien : on remet dans l'ordre.
        return list(reversed(messages or []))

    def send_message(
        self, channel_id: int, content: str | None = None, embed: dict | None = None,
        reply_to: int | None = None,
    ) -> dict:
        payload: dict[str, Any] = {}
        if content:
            payload["content"] = content
        if embed:
            payload["embeds"] = [embed]
        if reply_to:
            payload["message_reference"] = {"message_id": str(reply_to)}
            payload["allowed_mentions"] = {"parse": []}
        return self._request("POST", f"/channels/{channel_id}/messages", json=payload)

    def add_reaction(self, channel_id: int, message_id: int, emoji: str) -> None:
        from urllib.parse import quote

        self._request(
            "PUT",
            f"/channels/{channel_id}/messages/{message_id}/reactions/{quote(emoji)}/@me",
        )

    def get_me(self) -> dict:
        return self._request("GET", "/users/@me")
