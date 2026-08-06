"""Calcul du récap LP sur une fenêtre glissante 9h -> 9h, et rendu Discord."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, tzinfo

import discord

from store import Player
from ugg import QUEUE_LABELS, Match, UggClient

MONTHS_FR = [
    "janvier", "février", "mars", "avril", "mai", "juin",
    "juillet", "août", "septembre", "octobre", "novembre", "décembre",
]


def recap_window(now: datetime, boundary_hour: int, tz: tzinfo) -> tuple[datetime, datetime]:
    """Dernière fenêtre de 24 h close, bornée à `boundary_hour`.

    Appelé à 9h05 le 6 août, avec boundary_hour=9, ça rend
    [5 août 9h00, 6 août 9h00). Appelé à 3h du matin le 6, ça rend
    [4 août 9h00, 5 août 9h00) : on ne récapitule jamais une journée en cours.
    """
    end = datetime.combine(now.date(), time(boundary_hour), tzinfo=tz)
    if now < end:
        end -= timedelta(days=1)
    return end - timedelta(days=1), end


def format_window(start: datetime, end: datetime) -> str:
    if start.month == end.month:
        left = f"{start.day}"
    else:
        left = f"{start.day} {MONTHS_FR[start.month - 1]}"
    return (
        f"{left} → {end.day} {MONTHS_FR[end.month - 1]}, "
        f"{start.hour}h → {end.hour}h"
    )


@dataclass
class PlayerRecap:
    player: Player
    lp: int = 0
    wins: int = 0
    losses: int = 0
    unknown: int = 0  # games dont u.gg n'a pas su déduire le LP
    error: str | None = None
    per_queue: dict[str, int] = field(default_factory=dict)

    @property
    def games(self) -> int:
        return self.wins + self.losses

    @property
    def played(self) -> bool:
        return self.games > 0


def compute_recap(
    client: UggClient,
    player: Player,
    start: datetime,
    end: datetime,
    queues: list[int],
    tz: tzinfo,
) -> PlayerRecap:
    """Agrège les games d'un joueur sur la fenêtre. Bloquant (appeler en thread)."""
    result = PlayerRecap(player=player)
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)

    try:
        for match in client.iter_matches(player.name, player.tag, player.region, queues):
            if match.created_ms >= end_ms:
                continue  # game postérieure à la fenêtre (journée en cours)
            if match.created_ms < start_ms:
                break  # historique trié du plus récent au plus ancien : fini
            _accumulate(result, match)
    except Exception as exc:
        result.error = str(exc)

    return result


def _accumulate(result: PlayerRecap, match: Match) -> None:
    if match.win:
        result.wins += 1
    else:
        result.losses += 1
    if match.lp is None:
        result.unknown += 1
    else:
        result.lp += match.lp
        label = QUEUE_LABELS.get(match.queue, match.queue)
        result.per_queue[label] = result.per_queue.get(label, 0) + match.lp


def _mood(lp: int) -> str:
    if lp <= -50:
        return "💀"
    if lp < 0:
        return "📉"
    if lp == 0:
        return "😐"
    if lp < 50:
        return "📈"
    return "🔥"


def build_embed(
    recaps: list[PlayerRecap],
    start: datetime,
    end: datetime,
    show_queue_split: bool = False,
) -> discord.Embed:
    """Classement du plus gros loser au plus gros winner."""
    active = sorted((r for r in recaps if r.played), key=lambda r: r.lp)
    idle = [r for r in recaps if not r.played and r.error is None]
    broken = [r for r in recaps if r.error is not None]

    total_lp = sum(r.lp for r in active)
    total_games = sum(r.games for r in active)

    embed = discord.Embed(
        title=f"{_mood(total_lp)} Récap LP · {format_window(start, end)}",
        color=0xE04F5F if total_lp < 0 else 0x43B581,
    )

    if not active:
        embed.description = (
            "Personne n'a touché à la SoloQ sur cette fenêtre.\n"
            "*Journée saine. Suspect, mais saine.*"
        )
    else:
        width = max(len(r.player.riot_id) for r in active)
        lines = []
        for entry in active:
            record = f"{entry.wins}V-{entry.losses}D"
            line = (
                f"{_mood(entry.lp)} `{entry.player.riot_id:<{width}}` "
                f"**{entry.lp:+d} LP** · {record}"
            )
            if show_queue_split and len(entry.per_queue) > 1:
                split = ", ".join(f"{lbl} {val:+d}" for lbl, val in entry.per_queue.items())
                line += f" ({split})"
            if entry.unknown:
                line += f" · {entry.unknown} game(s) LP inconnu"
            lines.append(line)
        embed.description = "\n".join(lines)

        embed.add_field(
            name="Total serveur",
            value=f"**{total_lp:+d} LP** sur {total_games} game(s), {len(active)} joueur(s)",
            inline=False,
        )

    footer = []
    if idle:
        footer.append(f"{len(idle)} profil(s) sans game")
    if broken:
        footer.append(f"⚠️ {len(broken)} profil(s) en erreur")
    footer.append("données u.gg")
    embed.set_footer(text=" · ".join(footer))

    return embed
