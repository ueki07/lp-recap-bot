"""Client de l'API GraphQL (non documentée) de u.gg.

u.gg est derrière Cloudflare : un client HTTP classique (requests, aiohttp…) se
prend un `403 cf-mitigated: challenge`, même avec tous les headers d'un vrai
Chrome — c'est le *fingerprint TLS* qui est vérifié, pas les en-têtes. `curl_cffi`
rejoue le handshake de Chrome, ce qui suffit à passer. C'est l'unique raison de
cette dépendance : ne pas la remplacer par `requests` sans retester.

Endpoint : POST https://u.gg/api  (GraphQL, sans clé ni cookie).
Introspection activée, pratique pour explorer le schéma si ça bouge un jour.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Iterator

from curl_cffi import requests

from config import env_int

API_URL = "https://u.gg/api"
IMPERSONATE = "chrome"
TIMEOUT = 20

# u.gg renvoie des 500 par intermittence quand on enchaîne les requêtes.
MAX_RETRIES = 5
RETRY_BACKOFF = 2.0  # secondes, doublé à chaque tentative (2, 4, 8, 16)
RETRYABLE_STATUS = {429, 500, 502, 503, 504}

# Saison en cours. u.gg *exige* seasonIds (sans lui : `bad_params`).
SEASON_ID = env_int("UGG_SEASON_ID", 26)

QUEUE_SOLO = 420
QUEUE_FLEX = 440
QUEUE_LABELS = {"ranked_solo_5x5": "SoloQ", "ranked_flex_sr": "Flex"}

PAGE_SIZE = 20  # taille de page renvoyée par fetchPlayerMatchSummaries

# Quand u.gg n'arrive pas à déduire le LP d'une game (le "? LP" affiché sur le
# site), il renvoie une valeur sentinelle aberrante (-9991 observé). On écarte
# donc tout delta hors d'une fourchette plausible plutôt que de tester -9991,
# la sentinelle n'étant pas documentée.
LP_PLAUSIBLE_MAX = 500

_MATCHES_QUERY = """
query($u:String!, $t:String!, $r:String!, $p:Int!, $s:[Int!], $q:[Int!]) {
  fetchPlayerMatchSummaries(
    regionId: $r, riotUserName: $u, riotTagLine: $t,
    page: $p, seasonIds: $s, queueType: $q, processLp: true
  ) {
    totalNumMatches
    matchSummaries {
      matchId
      win
      queueType
      matchCreationTime
      lpInfo { lp placement promotedTo { tier rank } }
    }
  }
}
"""

_RANKS_QUERY = """
query($u:String!, $t:String!, $r:String!, $s:Int!) {
  fetchProfileRanks(regionId: $r, riotUserName: $u, riotTagLine: $t, seasonId: $s) {
    rankScores { queueType tier rank lp wins losses }
  }
}
"""

_TIER_SHORT = {
    "IRON": "F", "BRONZE": "B", "SILVER": "S", "GOLD": "G", "PLATINUM": "P",
    "EMERALD": "E", "DIAMOND": "D", "MASTER": "M", "GRANDMASTER": "GM",
    "CHALLENGER": "C",
}
_RANK_NUM = {"I": "1", "II": "2", "III": "3", "IV": "4"}
# Master+ n'a pas de division : afficher "M 320 LP", pas "M1 320 LP".
_APEX = {"MASTER", "GRANDMASTER", "CHALLENGER"}


class UggError(RuntimeError):
    """Erreur renvoyée par l'API u.gg."""


class PlayerNotFound(UggError):
    """Le Riot ID est inconnu de u.gg (ou la région est mauvaise)."""


@dataclass(frozen=True)
class Match:
    match_id: int
    win: bool
    queue: str
    created_ms: int
    lp: int | None  # None = u.gg n'a pas su déterminer le delta


@dataclass(frozen=True)
class RankScore:
    queue: str
    tier: str
    rank: str
    lp: int
    wins: int
    losses: int

    def short(self) -> str:
        """`DIAMOND`/`III`/80 -> `D3 · 80 LP`."""
        tier = _TIER_SHORT.get(self.tier.upper(), self.tier.title())
        if self.tier.upper() in _APEX:
            return f"{tier} · {self.lp} LP"
        return f"{tier}{_RANK_NUM.get(self.rank, self.rank)} · {self.lp} LP"


class UggClient:
    """Client synchrone. Le bot l'appelle via `asyncio.to_thread`.

    curl_cffi n'expose pas d'API async stable selon les versions, et le volume
    de requêtes est minuscule (une poignée par joueur, une fois par jour) : un
    client sync poussé dans un thread est plus simple et plus robuste.
    """

    def __init__(self) -> None:
        self._session = requests.Session()

    def _post(self, query: str, variables: dict) -> dict:
        """Requête GraphQL, avec réessais sur les erreurs transitoires.

        Enchaîner une vingtaine de joueurs fait répondre 500 à u.gg de façon
        intermittente : sans réessai, le récap quotidien perdrait silencieusement
        des joueurs.

        `player_info_not_found` est réessayé lui aussi, contre toute attente :
        u.gg le renvoie parfois pour un compte qui existe (vérifié). Seules les
        vraies erreurs de requête (`bad_params`) et le 403 Cloudflare échouent
        immédiatement — elles ne guériront pas.
        """
        last_error: Exception | None = None

        for attempt in range(MAX_RETRIES):
            if attempt:
                time.sleep(RETRY_BACKOFF * (2 ** (attempt - 1)))

            try:
                resp = self._session.post(
                    API_URL,
                    json={"query": query, "variables": variables},
                    impersonate=IMPERSONATE,
                    timeout=TIMEOUT,
                )
            except Exception as exc:  # réseau, TLS, timeout…
                last_error = UggError(f"u.gg injoignable : {exc}")
                continue

            if resp.status_code == 403:
                raise UggError(
                    "u.gg renvoie 403 (challenge Cloudflare). L'impersonation TLS "
                    "ne passe plus : mets curl_cffi à jour, ou essaie une autre "
                    "cible que 'chrome'."
                )
            if resp.status_code in RETRYABLE_STATUS:
                last_error = UggError(f"u.gg a répondu {resp.status_code}")
                continue
            if resp.status_code != 200:
                raise UggError(f"u.gg a répondu {resp.status_code}")

            try:
                payload = resp.json()
            except Exception as exc:
                last_error = UggError("réponse u.gg illisible (pas du JSON)")
                continue

            if payload.get("errors"):
                message = payload["errors"][0].get("message", "erreur inconnue")
                if message == "player_info_not_found":
                    # Contre-intuitif mais vérifié : u.gg renvoie parfois cette
                    # erreur à tort quand il est sous charge, pour un compte qui
                    # existe bel et bien. On réessaie donc avant de conclure —
                    # sinon un joueur disparaît silencieusement du récap.
                    last_error = PlayerNotFound(message)
                    continue
                raise UggError(message)

            return payload.get("data") or {}

        raise last_error or UggError("u.gg : échec après réessais")

    def fetch_ranks(self, name: str, tag: str, region: str) -> dict[str, RankScore]:
        """Rank actuel par file. Sert aussi à valider un Riot ID à l'ajout."""
        data = self._post(
            _RANKS_QUERY,
            {"u": name, "t": tag, "r": region, "s": SEASON_ID},
        )
        node = data.get("fetchProfileRanks") or {}
        scores = {}
        for raw in node.get("rankScores") or []:
            if not raw.get("tier"):
                continue  # file non classée cette saison
            scores[raw["queueType"]] = RankScore(
                queue=raw["queueType"],
                tier=raw["tier"],
                rank=raw.get("rank") or "",
                lp=raw.get("lp") or 0,
                wins=raw.get("wins") or 0,
                losses=raw.get("losses") or 0,
            )
        return scores

    def iter_matches(
        self,
        name: str,
        tag: str,
        region: str,
        queues: list[int],
        max_pages: int = 5,
    ) -> Iterator[Match]:
        """Parcourt l'historique, page par page, du plus récent au plus ancien.

        C'est un générateur : l'appelant s'arrête dès qu'il sort de sa fenêtre
        temporelle, ce qui évite de tirer des pages inutiles.
        """
        for page in range(1, max_pages + 1):
            data = self._post(
                _MATCHES_QUERY,
                {
                    "u": name, "t": tag, "r": region,
                    "p": page, "s": [SEASON_ID], "q": queues,
                },
            )
            node = data.get("fetchPlayerMatchSummaries") or {}
            summaries = node.get("matchSummaries") or []
            if not summaries:
                return

            for raw in summaries:
                lp_info = raw.get("lpInfo") or {}
                lp = lp_info.get("lp")
                if lp is None or abs(lp) > LP_PLAUSIBLE_MAX:
                    lp = None
                yield Match(
                    match_id=raw["matchId"],
                    win=bool(raw["win"]),
                    queue=raw.get("queueType") or "",
                    created_ms=int(raw["matchCreationTime"]),
                    lp=lp,
                )

            if len(summaries) < PAGE_SIZE:
                return  # dernière page
