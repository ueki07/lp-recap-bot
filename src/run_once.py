"""Point d'entrée GitHub Actions : une passe, puis on sort.

À chaque tick du cron, ce script :
  1. lit les nouveaux messages du salon de commandes (REST, pas de gateway) ;
  2. exécute les `!lp add|remove|list|recap` trouvés ;
  3. publie le récap quotidien si l'heure est passée et qu'il ne l'a pas déjà fait.

`data/players.json` et `data/state.json` sont réécrits puis commités par le
workflow : c'est la persistance du bot, il n'y a pas de base de données.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time as _time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from discord_api import DiscordClient, DiscordError
from recap import build_embed, compute_recap, format_window, recap_window
from store import (
    DEFAULT_REGION, REGIONS, Player, PlayerStore, parse_riot_id, region_label,
)
from ugg import QUEUE_FLEX, QUEUE_SOLO, PlayerNotFound, UggClient, UggError

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)-7s | %(message)s"
)
log = logging.getLogger("lp-recap")

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

TOKEN = os.getenv("DISCORD_TOKEN", "")
COMMAND_CHANNEL_ID = int(os.getenv("COMMAND_CHANNEL_ID", "0") or 0)
RECAP_CHANNEL_ID = int(os.getenv("RECAP_CHANNEL_ID", "0") or 0)
RECAP_HOUR = int(os.getenv("RECAP_HOUR", "9"))
RECAP_DELAY_MINUTES = int(os.getenv("RECAP_DELAY_MINUTES", "5"))
TZ = ZoneInfo(os.getenv("TIMEZONE", "Europe/Paris"))
INCLUDE_FLEX = os.getenv("INCLUDE_FLEX", "0") == "1"
QUEUES = [QUEUE_SOLO, QUEUE_FLEX] if INCLUDE_FLEX else [QUEUE_SOLO]

DATA_DIR = Path(os.getenv("DATA_DIR", ROOT / "data"))
PLAYERS_FILE = DATA_DIR / "players.json"
STATE_FILE = DATA_DIR / "state.json"

PREFIX = "!lp"
DELAY_BETWEEN_PLAYERS = 2.0

HELP = (
    "**Commandes LP**\n"
    "`!lp add Pseudo#TAG [region]` — suivre un profil (région : `euw1` par défaut)\n"
    "`!lp remove Pseudo#TAG` — arrêter de le suivre\n"
    "`!lp list` — profils suivis et leur rank\n"
    "`!lp recap [n]` — récap à la demande (`n` = nombre de jours en arrière)\n"
)


# ─────────────────────────────────── état ───────────────────────────────────

def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        log.warning("state.json illisible, on repart de zéro")
        return {}


def save_state(state: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


# ────────────────────────────────── commandes ──────────────────────────────────

def split_riot_id_and_region(argument: str) -> tuple[str, str]:
    """`underground k1ng#VVS euw1` -> `("underground k1ng#VVS", "euw1")`.

    Le pseudo peut contenir des espaces : on ne peut donc pas se contenter de
    découper sur l'espace. On regarde si le dernier mot est une région connue.
    """
    parts = argument.strip().split()
    if len(parts) >= 2 and parts[-1].lower() in REGIONS:
        return " ".join(parts[:-1]), parts[-1].lower()
    return argument.strip(), DEFAULT_REGION


def cmd_add(store: PlayerStore, ugg: UggClient, argument: str) -> str:
    if not argument.strip():
        return "❌ Il manque le Riot ID. Ex : `!lp add Lordos#EUW`"

    raw_id, region = split_riot_id_and_region(argument)
    try:
        name, tag = parse_riot_id(raw_id)
    except ValueError as exc:
        return f"❌ {exc}"

    player = Player(name=name, tag=tag, region=region)
    if store.find(player.key):
        return f"ℹ️ `{player.riot_id}` est déjà suivi."

    # Validation auprès de u.gg avant enregistrement : évite de traîner des
    # profils fantômes qui feraient échouer le récap tous les jours.
    try:
        ranks = ugg.fetch_ranks(name, tag, region)
    except PlayerNotFound:
        return (
            f"❌ `{player.riot_id}` introuvable sur u.gg en **{region_label(region)}**.\n"
            "Vérifie le tag et la région."
        )
    except UggError as exc:
        return f"⚠️ u.gg ne répond pas : {exc}"

    store.add_sync(player)
    solo = ranks.get("ranked_solo_5x5")
    rank_line = solo.short() if solo else "non classé en SoloQ"
    return (
        f"✅ `{player.riot_id}` ({region_label(region)}) ajouté — {rank_line}\n"
        f"**{len(store.all())}** profil(s) suivi(s)"
    )


def cmd_remove(store: PlayerStore, argument: str) -> str:
    if not argument.strip():
        return "❌ Il manque le Riot ID. Ex : `!lp remove Lordos#EUW`"
    removed = store.remove_sync(argument.strip())
    if removed is None:
        return f"❌ `{argument.strip()}` n'était pas suivi. Voir `!lp list`."
    return f"🗑️ `{removed.riot_id}` retiré. **{len(store.all())}** restant(s)."


def cmd_list(store: PlayerStore, ugg: UggClient) -> str:
    players = store.all()
    if not players:
        return "Aucun profil suivi. Ajoute-en un avec `!lp add Pseudo#TAG`."

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

    width = max(len(p.riot_id) for p, _ in rows)
    body = "\n".join(
        f"`{p.riot_id:<{width}}`  {rank}  ·  {region_label(p.region)}" for p, rank in rows
    )
    return f"📋 **Profils suivis ({len(rows)})**\n{body}"


def compute_recaps(ugg: UggClient, players: list[Player], start: datetime, end: datetime):
    results = []
    for index, player in enumerate(players):
        if index:
            _time.sleep(DELAY_BETWEEN_PLAYERS)
        results.append(compute_recap(ugg, player, start, end, QUEUES, TZ))
    return results


def build_recap_payload(store: PlayerStore, ugg: UggClient, offset_days: int = 0):
    now = datetime.now(TZ)
    start, end = recap_window(now, RECAP_HOUR, TZ)
    if offset_days:
        shift = timedelta(days=offset_days)
        start, end = start - shift, end - shift

    players = store.all()
    if not players:
        return None, start, end

    recaps = compute_recaps(ugg, players, start, end)
    for entry in recaps:
        if entry.error:
            log.warning("récap KO pour %s : %s", entry.player.riot_id, entry.error)
    return build_embed(recaps, start, end, show_queue_split=INCLUDE_FLEX), start, end


def handle_command(
    store: PlayerStore, ugg: UggClient, discord: DiscordClient, message: dict
) -> None:
    content = (message.get("content") or "").strip()
    body = content[len(PREFIX):].strip()
    verb, _, argument = body.partition(" ")
    verb = verb.lower()
    channel_id = int(message["channel_id"])
    message_id = int(message["id"])
    author = (message.get("author") or {}).get("username", "?")

    log.info("commande de %s : %s", author, content)

    if verb in ("add", "ajoute", "+"):
        reply = cmd_add(store, ugg, argument)
    elif verb in ("remove", "rm", "delete", "-"):
        reply = cmd_remove(store, argument)
    elif verb in ("list", "ls", "liste"):
        reply = cmd_list(store, ugg)
    elif verb == "recap":
        try:
            offset = int(argument.strip() or 0)
        except ValueError:
            offset = 0
        offset = max(0, min(offset, 30))
        embed, start, end = build_recap_payload(store, ugg, offset)
        if embed is None:
            discord.send_message(
                channel_id,
                content=f"Aucun profil suivi ({format_window(start, end)}).",
                reply_to=message_id,
            )
        else:
            discord.send_message(channel_id, embed=embed.to_dict(), reply_to=message_id)
        discord.add_reaction(channel_id, message_id, "✅")
        return
    elif verb in ("help", "aide", ""):
        reply = HELP
    else:
        reply = f"❓ Commande inconnue : `{verb}`\n\n{HELP}"

    discord.send_message(channel_id, content=reply, reply_to=message_id)
    discord.add_reaction(channel_id, message_id, "✅")


def process_commands(
    store: PlayerStore, ugg: UggClient, discord: DiscordClient, state: dict
) -> None:
    if not COMMAND_CHANNEL_ID:
        return

    after = state.get("last_message_id")
    messages = discord.get_messages(COMMAND_CHANNEL_ID, after=after)
    if not messages:
        return

    # Premier lancement : on mémorise juste le point de départ, sans rejouer
    # tout l'historique du salon.
    if after is None:
        state["last_message_id"] = messages[-1]["id"]
        log.info("initialisation : curseur posé sur le message %s", messages[-1]["id"])
        return

    blind = 0
    for message in messages:
        state["last_message_id"] = message["id"]
        if (message.get("author") or {}).get("bot"):
            continue
        content = (message.get("content") or "").strip()
        if not content:
            blind += 1
            continue
        if not content.lower().startswith(PREFIX):
            continue
        try:
            handle_command(store, ugg, discord, message)
        except Exception:
            log.exception("commande en échec : %s", content)

    if blind and blind == len([m for m in messages if not (m.get("author") or {}).get("bot")]):
        log.error(
            "Tous les messages lus ont un contenu vide : l'intent MESSAGE CONTENT "
            "n'est probablement pas activé. Developer Portal > ton application > "
            "Bot > Privileged Gateway Intents > Message Content Intent."
        )


# ──────────────────────── moissonnage de l'historique ────────────────────────

# Les embeds DPM.LOL portent le Riot ID complet dans `author.name`
# (ex. « MY NAME IS TITUS#NBO »). C'est plus fiable que de parser l'URL
# dpm.lol, où le séparateur `-` est ambigu quand le pseudo en contient un.
_RIOT_ID_RE = re.compile(r"^\s*(?P<name>.+?)\s*#\s*(?P<tag>[A-Za-z0-9]{2,8})\s*$")


def harvest_riot_ids(discord: DiscordClient, channel_id: int, max_pages: int) -> dict:
    """Remonte tout l'historique du salon et collecte les Riot ID distincts."""
    found: dict[str, tuple[str, str]] = {}
    before = None
    scanned = 0

    for page in range(max_pages):
        batch = discord.get_messages(channel_id, before=before, limit=100)
        if not batch:
            break
        scanned += len(batch)
        for message in batch:
            for embed in message.get("embeds") or []:
                raw = ((embed.get("author") or {}).get("name") or "").strip()
                match = _RIOT_ID_RE.match(raw)
                if not match:
                    continue
                name, tag = match.group("name"), match.group("tag")
                found.setdefault(f"{name.lower()}#{tag.lower()}", (name, tag))
        before = batch[0]["id"]  # batch est trié du plus ancien au plus récent
        log.info("page %d : %d messages scannés, %d Riot ID distincts",
                 page + 1, scanned, len(found))

    log.info("moissonnage terminé : %d messages, %d Riot ID", scanned, len(found))
    return found


def run_harvest(store: PlayerStore, ugg: UggClient, discord: DiscordClient,
                channel_id: int, max_pages: int, region: str) -> int:
    found = harvest_riot_ids(discord, channel_id, max_pages)
    if not found:
        log.error("aucun Riot ID trouvé — le salon est-il le bon ?")
        return 1

    added, already, rejected = [], [], []
    for index, (name, tag) in enumerate(sorted(found.values())):
        if store.find(f"{name}#{tag}"):
            already.append(f"{name}#{tag}")
            continue
        if index:
            _time.sleep(DELAY_BETWEEN_PLAYERS)
        try:
            ugg.fetch_ranks(name, tag, region)
        except PlayerNotFound:
            rejected.append(f"{name}#{tag} (introuvable en {region_label(region)})")
            continue
        except UggError as exc:
            rejected.append(f"{name}#{tag} ({exc})")
            continue
        store.add_sync(Player(name=name, tag=tag, region=region))
        added.append(f"{name}#{tag}")

    print("\n=== MOISSONNAGE ===")
    print(f"ajoutés   ({len(added)}) : " + (", ".join(added) or "—"))
    print(f"déjà là   ({len(already)}) : " + (", ".join(already) or "—"))
    print(f"rejetés   ({len(rejected)}) : " + ("; ".join(rejected) or "—"))
    print(f"total suivi : {len(store.all())}")
    return 0


# ─────────────────────────────── récap quotidien ───────────────────────────────

def maybe_post_recap(
    store: PlayerStore, ugg: UggClient, discord: DiscordClient, state: dict, force: bool
) -> None:
    now = datetime.now(TZ)
    start, end = recap_window(now, RECAP_HOUR, TZ)
    window_key = end.date().isoformat()

    if not force:
        if state.get("last_recap") == window_key:
            return  # déjà publié pour cette fenêtre
        publish_at = end + timedelta(minutes=RECAP_DELAY_MINUTES)
        if now < publish_at:
            return  # on laisse à u.gg le temps d'ingérer les dernières games

    embed, start, end = build_recap_payload(store, ugg)
    if embed is None:
        log.info("aucun profil suivi, pas de récap")
        state["last_recap"] = window_key
        return

    discord.send_message(RECAP_CHANNEL_ID, embed=embed.to_dict())
    state["last_recap"] = window_key
    log.info("récap publié pour la fenêtre %s", format_window(start, end))


# ─────────────────────────────────── main ───────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="Une passe du bot LP recap.")
    parser.add_argument("--force-recap", action="store_true",
                        help="publier le récap même s'il l'a déjà été")
    parser.add_argument("--no-commands", action="store_true",
                        help="ignorer les commandes, ne faire que le récap")
    parser.add_argument("--dry-run", action="store_true",
                        help="afficher le récap dans la console sans rien envoyer")
    parser.add_argument("--harvest", action="store_true",
                        help="remonter l'historique du salon et ajouter tous les "
                             "Riot ID trouvés dans les embeds")
    parser.add_argument("--harvest-pages", type=int, default=60,
                        help="pages de 100 messages à remonter (défaut : 60)")
    args = parser.parse_args()

    missing = [n for n, v in (("DISCORD_TOKEN", TOKEN),
                              ("RECAP_CHANNEL_ID", RECAP_CHANNEL_ID)) if not v]
    if missing and not args.dry_run:
        log.error("variables manquantes : %s", ", ".join(missing))
        return 1

    store = PlayerStore(PLAYERS_FILE)
    store.load()
    ugg = UggClient()
    log.info("%d profil(s) suivi(s)", len(store.all()))

    if args.dry_run:
        embed, start, end = build_recap_payload(store, ugg)
        print(f"\n=== {format_window(start, end)} ===")
        if embed is None:
            print("aucun profil suivi")
        else:
            print(embed.title)
            print(embed.description)
            for field in embed.fields:
                print(f"{field.name} : {field.value}")
        return 0

    state = load_state()
    discord = DiscordClient(TOKEN)

    if args.harvest:
        source = COMMAND_CHANNEL_ID or RECAP_CHANNEL_ID
        return run_harvest(store, ugg, discord, source, args.harvest_pages, DEFAULT_REGION)

    try:
        if not args.no_commands:
            process_commands(store, ugg, discord, state)
        maybe_post_recap(store, ugg, discord, state, force=args.force_recap)
    except DiscordError as exc:
        log.error("Discord : %s", exc)
        save_state(state)
        return 1
    finally:
        save_state(state)

    return 0


if __name__ == "__main__":
    sys.exit(main())
