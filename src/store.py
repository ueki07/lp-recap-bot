"""Persistance des profils suivis (fichier JSON).

Volontairement bête : quelques dizaines de profils au maximum, une écriture
seulement quand quelqu'un tape `/lp add` ou `/lp remove`. Écriture atomique
(tmp + rename) pour ne pas corrompre le fichier si le process est tué pendant
un redémarrage.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

DEFAULT_REGION = "euw1"

# Régions Riot acceptées par u.gg (regionId).
REGIONS = [
    "euw1", "eun1", "na1", "kr", "br1", "jp1", "la1", "la2",
    "oc1", "tr1", "ru", "ph2", "sg2", "th2", "tw2", "vn2", "me1",
]

# Nom d'usage : personne ne dit « EUW1 ».
REGION_LABELS = {
    "euw1": "EUW", "eun1": "EUNE", "na1": "NA", "br1": "BR", "jp1": "JP",
    "la1": "LAN", "la2": "LAS", "oc1": "OCE", "tr1": "TR", "ph2": "PH",
    "sg2": "SG", "th2": "TH", "tw2": "TW", "vn2": "VN", "me1": "ME",
}


def region_label(region: str) -> str:
    return REGION_LABELS.get(region.lower(), region.upper())


@dataclass(frozen=True)
class Player:
    name: str          # partie avant le # du Riot ID
    tag: str           # partie après le #
    region: str = DEFAULT_REGION
    added_by: int = 0  # user id Discord, pour savoir qui a ajouté qui

    @property
    def riot_id(self) -> str:
        return f"{self.name}#{self.tag}"

    @property
    def key(self) -> str:
        """Clé de comparaison insensible à la casse et à la région."""
        return f"{self.name.lower()}#{self.tag.lower()}"

    @property
    def profile_url(self) -> str:
        slug = f"{self.name.replace(' ', '%20')}-{self.tag}"
        return f"https://u.gg/lol/profile/{self.region}/{slug}/overview"


# Ponctuation qu'on retire en fin de tag. La correction automatique de macOS/iOS
# ajoute volontiers un point : `Miss Kitoko#KC W.` était refusé comme introuvable,
# sans le moindre indice sur la vraie cause. Aucun tag Riot ne finit par là.
_TRAILING_PUNCT = ".,;:!?)»\"'"


def parse_riot_id(raw: str) -> tuple[str, str]:
    """`Lordos#EUW` -> `("Lordos", "EUW")`. Lève ValueError si mal formé."""
    raw = raw.strip().lstrip("@")
    if "#" not in raw:
        raise ValueError(
            "Il faut le Riot ID complet, avec le tag : `Pseudo#TAG` "
            "(visible en haut de ton profil u.gg ou dans le client LoL)."
        )
    name, _, tag = raw.rpartition("#")
    name = name.strip()
    tag = tag.strip().rstrip(_TRAILING_PUNCT).strip()
    if not name or not tag:
        raise ValueError("Riot ID incomplet : il manque le pseudo ou le tag.")
    return name, tag


class PlayerStore:
    """Liste de profils suivis, partagée par tout le serveur Discord."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = asyncio.Lock()
        self._players: list[Player] = []

    def load(self) -> None:
        if not self.path.exists():
            self._players = []
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            self._players = []
            return
        self._players = [
            Player(
                name=item["name"],
                tag=item["tag"],
                region=item.get("region", DEFAULT_REGION),
                added_by=item.get("added_by", 0),
            )
            for item in raw.get("players", [])
        ]

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"players": [asdict(p) for p in self._players]}
        # tmp + rename : jamais de fichier à moitié écrit.
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def all(self) -> list[Player]:
        return list(self._players)

    def find(self, riot_id: str) -> Player | None:
        key = riot_id.strip().lower()
        return next((p for p in self._players if p.key == key), None)

    # Deux entrypoints partagent ce store : run_once.py (GitHub Actions, purement
    # synchrone) et bot.py (gateway, async). Le cœur est synchrone ; les variantes
    # async ne font que l'envelopper dans un lock pour sérialiser les écritures
    # concurrentes entre deux slash commands.

    def add_sync(self, player: Player) -> bool:
        """Retourne False si le profil était déjà suivi."""
        if any(p.key == player.key for p in self._players):
            return False
        self._players.append(player)
        self._write()
        return True

    def remove_sync(self, riot_id: str) -> Player | None:
        """Retourne le profil retiré, ou None s'il n'était pas suivi."""
        key = riot_id.strip().lower()
        match = next((p for p in self._players if p.key == key), None)
        if match is None:
            return None
        self._players = [p for p in self._players if p.key != key]
        self._write()
        return match

    async def add(self, player: Player) -> bool:
        async with self._lock:
            return self.add_sync(player)

    async def remove(self, riot_id: str) -> Player | None:
        async with self._lock:
            return self.remove_sync(riot_id)
