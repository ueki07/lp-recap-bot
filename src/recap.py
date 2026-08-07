"""Calcul du récap LP sur une fenêtre glissante 9h -> 9h, et rendu Discord."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, tzinfo

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
    max_pages: int = 5,
) -> PlayerRecap:
    """Agrège les games d'un joueur sur la fenêtre. Bloquant (appeler en thread)."""
    result = PlayerRecap(player=player)
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)

    try:
        for match in client.iter_matches(
            player.name, player.tag, player.region, queues, max_pages=max_pages
        ):
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


# ───────────────────── fiche joueur sur une période ─────────────────────

@dataclass
class DayStat:
    day: date          # date de *début* de la fenêtre 9h -> 9h
    lp: int = 0
    wins: int = 0
    losses: int = 0

    @property
    def games(self) -> int:
        return self.wins + self.losses


@dataclass
class PlayerPeriod:
    player: Player
    days: int
    start: datetime
    end: datetime
    lp: int = 0
    wins: int = 0
    losses: int = 0
    unknown: int = 0
    per_queue: dict[str, int] = field(default_factory=dict)
    per_day: dict[date, DayStat] = field(default_factory=dict)
    rank: str | None = None
    error: str | None = None

    @property
    def games(self) -> int:
        return self.wins + self.losses

    @property
    def winrate(self) -> float:
        return 100 * self.wins / self.games if self.games else 0.0

    @property
    def days_played(self) -> int:
        return sum(1 for d in self.per_day.values() if d.games)


def compute_player_period(
    client: UggClient,
    player: Player,
    days: int,
    queues: list[int],
    tz: tzinfo,
    boundary_hour: int,
) -> PlayerPeriod:
    """Bilan d'un joueur sur les `days` dernières fenêtres 9h -> 9h."""
    now = datetime.now(tz)
    _, end = recap_window(now, boundary_hour, tz)
    start = end - timedelta(days=days)
    report = PlayerPeriod(player=player, days=days, start=start, end=end)

    # Un gros grinder peut dépasser 100 games sur 30 jours : on autorise assez
    # de pages pour couvrir la période, la boucle s'arrête de toute façon dès
    # qu'elle sort de la fenêtre.
    max_pages = max(5, min(40, days * 2))

    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)

    try:
        for match in client.iter_matches(
            player.name, player.tag, player.region, queues, max_pages=max_pages
        ):
            if match.created_ms >= end_ms:
                continue
            if match.created_ms < start_ms:
                break

            moment = datetime.fromtimestamp(match.created_ms / 1000, tz)
            # Rattacher la game à sa fenêtre : une game de 3h du matin appartient
            # à la journée précédente. On décale de `boundary_hour` heures.
            day = (moment - timedelta(hours=boundary_hour)).date()
            stat = report.per_day.setdefault(day, DayStat(day=day))

            if match.win:
                report.wins += 1
                stat.wins += 1
            else:
                report.losses += 1
                stat.losses += 1

            if match.lp is None:
                report.unknown += 1
            else:
                report.lp += match.lp
                stat.lp += match.lp
                label = QUEUE_LABELS.get(match.queue, match.queue)
                report.per_queue[label] = report.per_queue.get(label, 0) + match.lp

        scores = client.fetch_ranks(player.name, player.tag, player.region)
        solo = scores.get("ranked_solo_5x5")
        report.rank = solo.short() if solo else "unranked"
    except Exception as exc:
        report.error = str(exc)

    return report


def build_player_embed(report: PlayerPeriod) -> discord.Embed:
    player = report.player
    if report.error:
        return discord.Embed(
            title=f"⚠️ {player.riot_id}",
            description=f"Impossible de récupérer les données : {report.error}",
            color=0xE04F5F,
        )

    embed = discord.Embed(
        title=f"{_mood(report.lp)} {player.riot_id} · {report.days} derniers jours",
        url=player.profile_url,
        color=0xE04F5F if report.lp < 0 else 0x43B581,
    )

    if not report.games:
        embed.description = (
            f"Aucune game sur la période "
            f"({report.start.day}/{report.start.month} → {report.end.day}/{report.end.month})."
        )
        if report.rank:
            embed.set_footer(text=f"{report.rank} · données u.gg")
        return embed

    head = [f"**{report.lp:+d} LP** sur la période"]
    if len(report.per_queue) > 1:
        head.append(" · ".join(f"{lbl} {val:+d}" for lbl, val in report.per_queue.items()))
    embed.description = "\n".join(head)

    embed.add_field(
        name="Bilan",
        value=(
            f"{report.wins}V-{report.losses}D · {report.games} games · "
            f"{report.winrate:.0f}% WR\n"
            f"{report.days_played} jour(s) joué(s) sur {report.days}"
        ),
        inline=False,
    )

    played = [d for d in report.per_day.values() if d.games]
    best = max(played, key=lambda d: d.lp)
    worst = min(played, key=lambda d: d.lp)
    embed.add_field(
        name="Extrêmes",
        value=(
            f"🔥 meilleur jour : **{best.lp:+d} LP** le {best.day.day} "
            f"{MONTHS_FR[best.day.month - 1]} ({best.wins}V-{best.losses}D)\n"
            f"💀 pire jour : **{worst.lp:+d} LP** le {worst.day.day} "
            f"{MONTHS_FR[worst.day.month - 1]} ({worst.wins}V-{worst.losses}D)"
        ),
        inline=False,
    )

    recent = sorted(played, key=lambda d: d.day, reverse=True)[:10]
    embed.add_field(
        name=f"Jour par jour ({min(len(played), 10)} derniers joués)",
        value="\n".join(
            f"`{d.day.day:>2}/{d.day.month:<2}` **{d.lp:+4d} LP** · {d.wins}V-{d.losses}D"
            for d in recent
        ),
        inline=False,
    )

    footer = [report.rank] if report.rank else []
    if report.unknown:
        footer.append(f"{report.unknown} game(s) LP inconnu")
    footer.append("données u.gg")
    embed.set_footer(text=" · ".join(footer))
    return embed


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
