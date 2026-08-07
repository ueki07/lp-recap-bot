# 📉 LP Recap Bot

Poste chaque matin dans un salon Discord le récap des LP gagnés/perdus en SoloQ
par les joueurs suivis, sur la fenêtre **9h → 9h**. Classement du plus gros
loser au plus gros winner, parce que c'est le but.

```
📉 Récap LP · 5 → 6 août, 9h → 9h

💀 `underground k1ng#VVS` -42 LP · 1V-3D
📉 `MY NAME IS TITUS#NBO` -21 LP · 0V-1D
🔥 `Lordos#EUW`           +58 LP · 4V-1D

Total serveur
-5 LP sur 9 game(s), 3 joueur(s)
```

## Deux modes de déploiement

Le cœur (u.gg, calcul, stockage) est partagé ; seule la façade change.

| | **GitHub Actions** (`src/run_once.py`) | **Bot permanent** (`src/bot.py`) |
|---|---|---|
| Rôle | publie le récap quotidien | commandes `/lp` + récap |
| Hébergement | aucun | un process 24/7 |
| Coût | gratuit (repo public = minutes illimitées) | VPS / Oracle Cloud / Raspberry Pi |
| Commandes | **aucune** | slash : `/lp`, avec autocomplétion |

**Une seule interface utilisateur : les slash commands `/lp`.** Elles exigent une
connexion gateway permanente, donc elles ne fonctionnent que si `bot.py` tourne.
Actions ne sait pas les servir (un job y est plafonné à 6 h) : son rôle se limite
à publier le récap quotidien, de façon fiable et sans machine allumée.

Une couche de commandes texte `!lp` a existé côté Actions ; elle a été retirée.
Deux syntaxes concurrentes pour les mêmes actions créaient de la confusion, et la
latence du cron (jusqu'à une heure en pratique, GitHub throttlant les workflows
planifiés) les rendait pénibles à l'usage.

**Faire tourner les deux ensemble** : lancer `bot.py` avec `RECAP_ENABLED=0`,
sinon le récap est publié deux fois.

---

## Mode GitHub Actions (recommandé)

### 1. Créer le bot Discord

Sur [discord.com/developers/applications](https://discord.com/developers/applications) →
*New Application* → onglet **Bot** :

- *Reset Token* → copier le token.
- **`MESSAGE CONTENT INTENT`** n'est plus nécessaire depuis le retrait des
  commandes texte : les slash commands n'en ont pas besoin. Le moissonnage lit
  des embeds, pas du contenu de message.

Puis *OAuth2 > URL Generator* pour l'inviter :
- Scopes : `bot`
- Permissions : `Send Messages`, `Embed Links`, `Read Message History`,
  `Add Reactions`

### 2. Pousser le repo **en public**

Un repo privé est limité à 2000 min d'Actions par mois ; **un repo public a des
minutes illimitées**. Avec un cron toutes les 5 min, il faut être public.

> ⚠️ En public, `data/players.json` est visible de tous : ce sont les Riot ID
> de tes potes, rien de sensible, mais autant le savoir. Le token, lui, reste
> dans les secrets et n'est jamais commité.

### 3. Configurer le repo

*Settings > Secrets and variables > Actions* :

**Secrets** — `DISCORD_TOKEN`, `RECAP_CHANNEL_ID`. `COMMAND_CHANNEL_ID` ne sert
plus qu'au moissonnage (`--harvest`) ; sans lui, c'est `RECAP_CHANNEL_ID` qui est
scanné.

**Variables** (facultatif) — `RECAP_HOUR`, `TIMEZONE`, `INCLUDE_FLEX`,
`UGG_SEASON_ID`.

C'est tout. Le workflow tourne toutes les 5 min et se déclenche aussi à la main
via *Actions > LP recap > Run workflow*.


---

## Mode bot permanent

Si tu as où l'héberger, `src/bot.py` donne les vraies slash commands.

```bash
cp .env.example .env   # DISCORD_TOKEN, GUILD_ID, RECAP_CHANNEL_ID
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
./venv/bin/python src/bot.py
```

`GUILD_ID` synchronise les commandes sur ce serveur uniquement, donc
disponibles immédiatement (sinon jusqu'à 1 h de propagation globale). Ce mode
n'a pas besoin de l'intent Message Content.

```bash
docker build -t lp-recap-bot .
docker run -d --restart unless-stopped --env-file .env \
  -v "$PWD/data:/app/data" lp-recap-bot
```

Monter `data/` sur un volume persistant, sinon la liste des profils repart de
zéro à chaque redéploiement.

---

## D'où viennent les données

L'API GraphQL de u.gg (`POST https://u.gg/api`), non documentée, sans clé ni compte.

- `fetchPlayerMatchSummaries(..., processLp: true)` → le **delta LP signé de chaque
  game** (`lpInfo.lp`) + son timestamp. C'est exactement ce qu'affiche le site.
- `fetchProfileRanks(...)` → le rank actuel, et la validation d'un Riot ID.

L'introspection GraphQL est activée sur l'endpoint : si le schéma bouge, on peut
le réexplorer sans deviner.

### ⚠️ Cloudflare : pourquoi `curl_cffi` et pas `requests`

u.gg est derrière Cloudflare. Un client HTTP classique se prend un
`403 cf-mitigated: challenge` **même avec tous les en-têtes d'un vrai Chrome** —
c'est le *fingerprint TLS* qui est vérifié, pas les headers. `curl_cffi` rejoue
le handshake de Chrome (`impersonate="chrome"`) et passe.

C'est la partie fragile du projet. Si le bot se met à répondre
« u.gg renvoie 403 » : `pip install -U curl_cffi` d'abord, puis essayer une
autre cible d'impersonation (`chrome124`, `safari`…).

## Configuration

| Variable | Défaut | Rôle |
|---|---|---|
| `DISCORD_TOKEN` | — | **Requis.** Token du bot. |
| `RECAP_CHANNEL_ID` | — | **Requis.** Salon où publier le récap. |
| `COMMAND_CHANNEL_ID` | — | Salon scanné par `--harvest` (défaut : `RECAP_CHANNEL_ID`). |
| `GUILD_ID` | — | Synchro instantanée des slash commands (mode bot). |
| `RECAP_ENABLED` | `1` | `0` pour désactiver le récap (bot lancé à côté d'Actions). |
| `RECAP_HOUR` | `9` | Borne de la fenêtre. Récap publié à `RECAP_HOUR:05`. |
| `RECAP_DELAY_MINUTES` | `5` | Marge après la borne avant publication. |
| `TIMEZONE` | `Europe/Paris` | Fuseau des bornes (gère l'heure d'été seul). |
| `INCLUDE_FLEX` | `0` | `1` pour compter aussi la Flex, avec détail par file. |
| `UGG_SEASON_ID` | `26` | Saison u.gg. **À incrémenter chaque saison.** |

### 🔴 Le piège du changement de saison

`seasonIds` est **obligatoire** côté u.gg (sans lui : `bad_params`). Au passage à
la saison suivante, tant que `UGG_SEASON_ID` n'est pas incrémenté, le bot
renverra **0 game pour tout le monde, sans la moindre erreur**. C'est le seul
réglage à ne pas oublier.

## Détails d'implémentation

**Fenêtre 9h → 9h.** `recap_window()` rend toujours la dernière fenêtre de 24 h
*close* : lancé à 3h du matin, il récapitule l'avant-veille, jamais une journée
en cours. Publication à 9h05 pour laisser à u.gg le temps d'ingérer les
dernières games.

**Idempotence.** Le récap n'est publié qu'une fois par fenêtre :
`data/state.json` mémorise celle déjà traitée. Le cron est déclaré toutes les
5 min mais GitHub throttle les workflows planifiés — en pratique il tourne plutôt
une fois par heure, et le récap du matin peut donc arriver avec du retard.

**LP inconnu.** Quand u.gg n'arrive pas à déduire le delta d'une game (le `? LP`
affiché sur le site), il renvoie une valeur sentinelle aberrante (`-9991`
observé). Tout delta hors d'une fourchette plausible (±500) est écarté du total
et signalé à part (`2 game(s) LP inconnu`) plutôt que de pourrir le classement.
On ne teste pas `-9991` en dur : la sentinelle n'est pas documentée.

**Pagination.** L'historique est trié du plus récent au plus ancien ; on arrête
de tirer des pages dès la première game antérieure à la fenêtre. En pratique :
une seule requête par joueur.

**Persistance.** Pas de base de données : `data/players.json` et
`data/state.json` sont commités par le workflow (`[skip ci]`). Le push est
retenté avec rebase, deux jobs pouvant se chevaucher.

## Tester en local

```bash
# Calcul réel sur u.gg, aucun appel Discord :
./venv/bin/python src/run_once.py --dry-run

# Une passe complète (lit les commandes, publie si l'heure est passée) :
./venv/bin/python src/run_once.py

# Forcer la republication du récap :
./venv/bin/python src/run_once.py --force-recap
```

## Idées d'évolution

- Streaks : « 4e jour de perte consécutif » — nécessite d'historiser les récaps.
- Récap hebdo le lundi, classement mensuel.
- Rôle Discord « loser du jour » attribué automatiquement.
- Lier un profil u.gg à un membre Discord pour le mentionner nommément.
