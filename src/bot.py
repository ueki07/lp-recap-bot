"""Bot Discord : récap quotidien des LP gagnés/perdus, données u.gg.

- Slash commands `/lp add|remove|list|recap` pour gérer les profils suivis.
- Tâche planifiée : chaque jour à RECAP_HOUR, récap de la fenêtre 9h -> 9h.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time as _time
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

from config import env_flag, env_hour, env_int, env_str

from recap import (
    PlayerRecap, build_embed, build_player_embed, compute_player_period,
    compute_recap, format_window, recap_window,
)
from store import (
    DEFAULT_REGION, REGIONS, Player, PlayerStore, parse_riot_id, region_label,
)
from ugg import QUEUE_FLEX, QUEUE_SOLO, PlayerNotFound, UggClient, UggError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
)
log = logging.getLogger("lp-recap")

# .env pour le dev local ; en prod les variables viennent de l'hébergeur.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

TOKEN = env_str("DISCORD_TOKEN")
GUILD_ID = env_int("GUILD_ID")
RECAP_CHANNEL_ID = env_int("RECAP_CHANNEL_ID")
RECAP_HOUR = env_hour("RECAP_HOUR", 9)
# À mettre à 0 pour lancer le bot en parallèle du cron GitHub Actions :
# les slash commands restent dispo, mais le récap n'est pas publié deux fois.
RECAP_ENABLED = env_flag("RECAP_ENABLED", True)
TZ = ZoneInfo(env_str("TIMEZONE", "Europe/Paris"))
DATA_FILE = Path(env_str("DATA_FILE", str(Path(__file__).resolve().parent.parent / "data" / "players.json")))
INCLUDE_FLEX = env_flag("INCLUDE_FLEX")

QUEUES = [QUEUE_SOLO, QUEUE_FLEX] if INCLUDE_FLEX else [QUEUE_SOLO]

# u.gg ingère les games avec un peu de retard : on laisse quelques minutes de
# marge après la borne de fin de fenêtre avant de publier.
RECAP_DELAY_MINUTES = env_int("RECAP_DELAY_MINUTES", 5)

# Politesse : pause entre deux joueurs. Trop court (0.4s), u.gg renvoie
# des 500 en rafale sur une vingtaine de profils.
DELAY_BETWEEN_PLAYERS = 2.0

store = PlayerStore(DATA_FILE)
ugg = UggClient()

intents = discord.Intents.default()
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)


# ─────────────────────────── calcul (hors event loop) ───────────────────────────

def _compute_all(
    players: list[Player], start: datetime, end: datetime, queues: list[int]
) -> list[PlayerRecap]:
    """Boucle sur tous les joueurs. Bloquant : exécuté dans un thread.

    Un seul thread pour tout le monde : `curl_cffi.Session` n'est pas garanti
    thread-safe, et le volume (quelques requêtes par joueur, une fois par jour)
    ne justifie pas de paralléliser.
    """
    results = []
    for index, player in enumerate(players):
        if index:
            _time.sleep(DELAY_BETWEEN_PLAYERS)
        results.append(compute_recap(ugg, player, start, end, queues, TZ))
    return results


async def build_recap_embed(
    offset_days: int = 0, queues: list[int] | None = None
) -> discord.Embed:
    queues = queues or QUEUES
    now = datetime.now(TZ)
    start, end = recap_window(now, RECAP_HOUR, TZ)
    if offset_days:
        shift = timedelta(days=offset_days)
        start, end = start - shift, end - shift

    players = store.all()
    if not players:
        embed = discord.Embed(
            title=f"Récap LP · {format_window(start, end)}",
            description=(
                "Aucun profil suivi pour l'instant.\n"
                "Ajoute-en un avec `/lp add riot_id:Pseudo#TAG`."
            ),
            color=0x99AAB5,
        )
        return embed

    recaps = await asyncio.to_thread(_compute_all, players, start, end, queues)
    for entry in recaps:
        if entry.error:
            log.warning("récap KO pour %s : %s", entry.player.riot_id, entry.error)
    # Détail par file dès que plusieurs files sont demandées.
    return build_embed(recaps, start, end, show_queue_split=len(queues) > 1)


# ────────────────────────────── slash commands ──────────────────────────────

lp = app_commands.Group(name="lp", description="Suivi des LP en SoloQ")


async def region_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    current = current.lower()
    matches = [r for r in REGIONS if current in r] or REGIONS
    return [app_commands.Choice(name=region_label(r), value=r) for r in matches[:25]]


async def tracked_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    current = current.lower()
    matches = [p for p in store.all() if current in p.riot_id.lower()]
    return [
        app_commands.Choice(name=p.riot_id, value=p.riot_id) for p in matches[:25]
    ]


@lp.command(name="add", description="Ajouter un profil au suivi")
@app_commands.describe(
    riot_id="Riot ID complet, avec le tag — ex : Lordos#EUW",
    region="Région du compte (euw1 par défaut)",
)
@app_commands.autocomplete(region=region_autocomplete)
async def lp_add(
    interaction: discord.Interaction, riot_id: str, region: str = DEFAULT_REGION
) -> None:
    await interaction.response.defer(thinking=True)

    try:
        name, tag = parse_riot_id(riot_id)
    except ValueError as exc:
        await interaction.followup.send(f"❌ {exc}")
        return

    region = region.lower().strip()
    if region not in REGIONS:
        await interaction.followup.send(
            f"❌ Région inconnue : `{region}`. Attendu : {', '.join(REGIONS)}."
        )
        return

    player = Player(name=name, tag=tag, region=region, added_by=interaction.user.id)

    if store.find(player.key):
        await interaction.followup.send(f"ℹ️ `{player.riot_id}` est déjà suivi.")
        return

    # On valide le profil auprès de u.gg avant de l'enregistrer : ça évite de
    # traîner des Riot ID fantômes qui feraient échouer le récap tous les jours.
    try:
        ranks = await asyncio.to_thread(ugg.fetch_ranks, name, tag, region)
    except PlayerNotFound:
        await interaction.followup.send(
            f"❌ `{player.riot_id}` introuvable sur u.gg en **{region_label(region)}**.\n"
            "Vérifie le tag et la région. Si le compte est tout neuf, il faut "
            "qu'il apparaisse au moins une fois sur u.gg."
        )
        return
    except UggError as exc:
        await interaction.followup.send(f"⚠️ u.gg ne répond pas : {exc}")
        return

    if not await store.add(player):
        await interaction.followup.send(f"ℹ️ `{player.riot_id}` est déjà suivi.")
        return

    solo = ranks.get("ranked_solo_5x5")
    rank_line = solo.short() if solo else "non classé en SoloQ"
    await interaction.followup.send(
        f"✅ [`{player.riot_id}`]({player.profile_url}) ({region_label(region)}) ajouté au suivi.\n"
        f"　{rank_line} · **{len(store.all())}** profil(s) suivi(s)"
    )


@lp.command(name="remove", description="Retirer un profil du suivi")
@app_commands.describe(riot_id="Profil à retirer")
@app_commands.autocomplete(riot_id=tracked_autocomplete)
async def lp_remove(interaction: discord.Interaction, riot_id: str) -> None:
    removed = await store.remove(riot_id)
    if removed is None:
        await interaction.response.send_message(
            f"❌ `{riot_id}` n'était pas suivi. Regarde `/lp list`.", ephemeral=True
        )
        return
    await interaction.response.send_message(
        f"🗑️ `{removed.riot_id}` retiré du suivi. "
        f"**{len(store.all())}** profil(s) restant(s)."
    )


@lp.command(name="list", description="Lister les profils suivis")
async def lp_list(interaction: discord.Interaction) -> None:
    players = store.all()
    if not players:
        await interaction.response.send_message(
            "Aucun profil suivi. Ajoute-en un avec `/lp add riot_id:Pseudo#TAG`.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(thinking=True)

    def _ranks() -> list[tuple[Player, str]]:
        rows = []
        for index, player in enumerate(players):
            if index:
                _time.sleep(DELAY_BETWEEN_PLAYERS)
            try:
                scores = ugg.fetch_ranks(player.name, player.tag, player.region)
                solo = scores.get("ranked_solo_5x5")
                rows.append((player, solo.short() if solo else "unranked"))
            except UggError:
                rows.append((player, "⚠️ indisponible"))
        return rows

    rows = await asyncio.to_thread(_ranks)
    width = max(len(p.riot_id) for p, _ in rows)
    body = "\n".join(
        f"• `{p.riot_id:<{width}}` {rank}  ·  {region_label(p.region)}" for p, rank in rows
    )
    embed = discord.Embed(
        title=f"📋 Profils suivis ({len(rows)})",
        description=body,
        color=0x5865F2,
    )
    await interaction.followup.send(embed=embed)


QUEUE_CHOICES = [
    app_commands.Choice(name="SoloQ", value="solo"),
    app_commands.Choice(name="Flex", value="flex"),
    app_commands.Choice(name="SoloQ + Flex", value="both"),
]


def queues_from_choice(value: str | None) -> list[int]:
    """`None` -> la config par défaut du bot (SoloQ, sauf INCLUDE_FLEX=1)."""
    if value == "flex":
        return [QUEUE_FLEX]
    if value == "both":
        return [QUEUE_SOLO, QUEUE_FLEX]
    if value == "solo":
        return [QUEUE_SOLO]
    return QUEUES


@lp.command(name="recap", description="Afficher le récap LP à la demande")
@app_commands.describe(
    jours="0 = dernière fenêtre close (défaut), 1 = celle d'avant, etc.",
    file="File à compter (SoloQ par défaut)",
)
@app_commands.choices(file=QUEUE_CHOICES)
async def lp_recap(
    interaction: discord.Interaction,
    jours: int = 0,
    file: app_commands.Choice[str] | None = None,
) -> None:
    if not 0 <= jours <= 30:
        await interaction.response.send_message(
            "❌ `jours` doit être compris entre 0 et 30.", ephemeral=True
        )
        return
    await interaction.response.defer(thinking=True)
    queues = queues_from_choice(file.value if file else None)
    embed = await build_recap_embed(offset_days=jours, queues=queues)
    await interaction.followup.send(embed=embed)


@lp.command(name="joueur", description="Bilan d'un joueur sur une période")
@app_commands.describe(
    riot_id="Le joueur (autocomplétion sur les profils suivis)",
    jours="Nombre de jours à couvrir (30 par défaut, 90 max)",
    file="File à compter (SoloQ par défaut)",
)
@app_commands.autocomplete(riot_id=tracked_autocomplete)
@app_commands.choices(file=QUEUE_CHOICES)
async def lp_joueur(
    interaction: discord.Interaction,
    riot_id: str,
    jours: int = 30,
    file: app_commands.Choice[str] | None = None,
) -> None:
    if not 1 <= jours <= 90:
        await interaction.response.send_message(
            "❌ `jours` doit être compris entre 1 et 90.", ephemeral=True
        )
        return

    # Un profil suivi d'abord ; sinon on accepte n'importe quel Riot ID.
    player = store.find(riot_id.strip().lower())
    if player is None:
        try:
            name, tag = parse_riot_id(riot_id)
        except ValueError as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
            return
        player = Player(name=name, tag=tag, region=DEFAULT_REGION)

    await interaction.response.defer(thinking=True)
    queues = queues_from_choice(file.value if file else None)
    report = await asyncio.to_thread(
        compute_player_period, ugg, player, jours, queues, TZ, RECAP_HOUR
    )
    if report.error:
        log.warning("fiche joueur KO pour %s : %s", player.riot_id, report.error)
    await interaction.followup.send(
        embed=build_player_embed(report, show_queue_split=len(queues) > 1)
    )


tree.add_command(lp)


@tree.error
async def on_command_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
) -> None:
    log.exception("commande en erreur", exc_info=error)
    message = "⚠️ Une erreur est survenue. Regarde les logs du bot."
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


# ─────────────────────────────── tâche quotidienne ───────────────────────────────

@tasks.loop(time=time(hour=RECAP_HOUR, minute=RECAP_DELAY_MINUTES, tzinfo=TZ))
async def daily_recap() -> None:
    channel = bot.get_channel(RECAP_CHANNEL_ID)
    if channel is None:
        log.error("salon %s introuvable — récap non publié", RECAP_CHANNEL_ID)
        return
    try:
        embed = await build_recap_embed()
        await channel.send(embed=embed)
        log.info("récap quotidien publié dans #%s", getattr(channel, "name", channel.id))
    except Exception:
        log.exception("échec de la publication du récap quotidien")


@daily_recap.before_loop
async def before_daily_recap() -> None:
    await bot.wait_until_ready()


@bot.event
async def on_ready() -> None:
    log.info("connecté en tant que %s", bot.user)
    if GUILD_ID:
        guild = discord.Object(id=GUILD_ID)
        tree.copy_global_to(guild=guild)
        await tree.sync(guild=guild)  # sync guild = commandes dispo immédiatement
        log.info("commandes synchronisées sur la guilde %s", GUILD_ID)
    else:
        await tree.sync()  # sync global : jusqu'à 1 h de propagation
        log.info("commandes synchronisées globalement")

    if not RECAP_ENABLED:
        log.info("récap quotidien DÉSACTIVÉ (RECAP_ENABLED=0) — slash commands seules")
    elif not daily_recap.is_running():
        daily_recap.start()
        log.info(
            "récap quotidien planifié à %02dh%02d (%s), fenêtre %dh -> %dh",
            RECAP_HOUR, RECAP_DELAY_MINUTES, TZ.key, RECAP_HOUR, RECAP_HOUR,
        )


def main() -> None:
    missing = [
        name for name, value in
        (("DISCORD_TOKEN", TOKEN), ("RECAP_CHANNEL_ID", RECAP_CHANNEL_ID))
        if not value
    ]
    if missing:
        raise SystemExit(
            f"Variables d'environnement manquantes : {', '.join(missing)}. "
            "Copie .env.example en .env et remplis-le."
        )
    store.load()
    log.info("%d profil(s) chargé(s) depuis %s", len(store.all()), DATA_FILE)
    bot.run(TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
