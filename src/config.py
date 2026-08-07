"""Lecture des variables d'environnement, tolérante aux valeurs vides.

Docker transmet une variable déclarée sans valeur (`GUILD_ID=` dans un fichier
`env_file`) comme une **chaîne vide**, pas comme une variable absente. Du coup
`int(os.getenv("GUILD_ID", "0"))` ne prend pas son défaut et lève :

    ValueError: invalid literal for int() with base 10: ''

Le bot crashait au démarrage, en boucle, pour une ligne laissée vide dans le
`.env`. D'où ces accesseurs, à utiliser partout plutôt que `os.getenv` brut.
"""

from __future__ import annotations

import os


def env_int(name: str, default: int = 0) -> int:
    """Entier depuis l'environnement. Vide, absent ou illisible -> `default`."""
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def env_str(name: str, default: str = "") -> str:
    return (os.getenv(name) or "").strip() or default


def env_flag(name: str, default: bool = False) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on", "oui")
