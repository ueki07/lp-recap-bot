"""Point d'entrée GitHub Actions : une passe, puis on sort.

Publie le récap quotidien si l'heure est passée et qu'il ne l'a pas déjà fait.
Sert aussi d'outil de moissonnage (`--harvest`).

**Il n'y a volontairement aucune commande ici.** L'unique interface utilisateur
est celle des slash commands `/lp` de `bot.py` : deux syntaxes concurrentes
(`/lp` et `!lp`) créaient de la confusion pour rien. Actions ne fait donc que
publier, et les commandes exigent que `bot.py` tourne.

`data/players.json` et `data/state.json` sont réécrits puis commités par le
workflow : c'est la persistance du bot, il n'y a pas de base de données.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time as _time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from config import env_flag, env_int, env_str

from discord_api import DiscordClient, DiscordError
from recap import build_embed, compute_recap, format_window, recap_window
from store import DEFAULT_REGION, Player, PlayerStore, region_label
from ugg import QUEUE_FLEX, QUEUE_SOLO, PlayerNotFound, UggClient, UggError

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)-7s | %(message)s"
)
log = logging.getLogger("lp-recap")

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

TOKEN = env_str("DISCORD_TOKEN")
COMMAND_CHANNEL_ID = env_int("COMMAND_CHANNEL_ID")
RECAP_CHANNEL_ID = env_int("RECAP_CHANNEL_ID")
RECAP_HOUR = env_int("RECAP_HOUR", 9)
RECAP_DELAY_MINUTES = env_int("RECAP_DELAY_MINUTES", 5)
TZ = ZoneInfo(env_str("TIMEZONE", "Europe/Paris"))
INCLUDE_FLEX = env_flag("INCLUDE_FLEX")
QUEUES = [QUEUE_SOLO, QUEUE_FLEX] if INCLUDE_FLEX else [QUEUE_SOLO]

DATA_DIR = Path(env_str("DATA_DIR", str(ROOT / "data")))
PLAYERS_FILE = DATA_DIR / "players.json"
STATE_FILE = DATA_DIR / "state.json"

# Politesse : pause entre deux joueurs. Trop court (0.4s), u.gg renvoie
# des 500 en rafale sur une vingtaine de profils.
DELAY_BETWEEN_PLAYERS = 2.0


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


# ──────────────────────── moissonnage de l'historique ────────────────────────

# Les embeds DPM.LOL portent le Riot ID complet dans `author.name`
# (ex. « MY NAME IS TITUS#NBO »). C'est plus fiable que de parser l'URL
# dpm.lol, où le séparateur `-` est ambigu quand le pseudo en contient un.
#
# Le tag accepte les espaces : `Miss Kitoko#KC W` est un Riot ID valide, vérifié
# sur u.gg. Un motif alphanumérique strict écartait ces joueurs du moissonnage
# en silence — ils n'apparaissaient jamais dans le récap sans qu'on sache pourquoi.
_RIOT_ID_RE = re.compile(r"^\s*(?P<name>.+?)\s*#\s*(?P<tag>[^#]{2,16}?)\s*$")


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
