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
| Hébergement | aucun | un process 24/7 |
| Coût | gratuit (repo public = minutes illimitées) | VPS / Railway / Raspberry Pi |
| Commandes | texte : `!lp add Pseudo#TAG` | slash : `/lp add`, avec autocomplétion |
| Latence commande | jusqu'à ~5-15 min (cron) | instantanée |

**Pourquoi pas de slash commands sur Actions** : elles exigent une connexion
gateway ouverte en permanence, or un job Actions est plafonné à 6 h. C'est
structurellement incompatible. Le mode Actions lit donc les commandes par
requêtes REST à chaque tick du cron — pas de websocket, pas de serveur.

---

## Mode GitHub Actions (recommandé)

### 1. Créer le bot Discord

Sur [discord.com/developers/applications](https://discord.com/developers/applications) →
*New Application* → onglet **Bot** :

- *Reset Token* → copier le token.
- **Activer `MESSAGE CONTENT INTENT`** (section *Privileged Gateway Intents*).
  Sans lui, Discord renvoie un `content` vide et le bot ne verra jamais les
  commandes. En dessous de 100 serveurs, c'est une simple case à cocher, aucune
  vérification n'est demandée. Le bot le détecte et le dit dans les logs si tu
  oublies.

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

**Secrets** — `DISCORD_TOKEN`, `RECAP_CHANNEL_ID`, `COMMAND_CHANNEL_ID`
(les deux salons peuvent être le même).

**Variables** (facultatif) — `RECAP_HOUR`, `TIMEZONE`, `INCLUDE_FLEX`,
`UGG_SEASON_ID`.

C'est tout. Le workflow tourne toutes les 5 min et se déclenche aussi à la main
via *Actions > LP recap > Run workflow*.

### Commandes

```
!lp add Pseudo#TAG [region]   suivre un profil (euw1 par défaut)
!lp remove Pseudo#TAG          arrêter de le suivre
!lp list                       profils suivis + rank actuel
!lp recap [n]                  récap à la demande (n jours en arrière)
!lp help
```

Le bot réagit ✅ au message une fois la commande traitée. `!lp add` valide le
profil auprès de u.gg avant de l'enregistrer : un tag ou une région faux sont
refusés tout de suite.

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
| `COMMAND_CHANNEL_ID` | — | Salon où lire les `!lp ...` (mode Actions). |
| `GUILD_ID` | — | Synchro instantanée des slash commands (mode bot). |
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

**Idempotence.** Le cron tourne toutes les 5 min mais le récap n'est publié
qu'une fois : `data/state.json` mémorise la fenêtre déjà traitée. Même chose
pour les commandes, via le curseur `last_message_id` — au premier lancement, le
bot pose le curseur sans rejouer l'historique du salon.

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
