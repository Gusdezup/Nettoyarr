# Nettoyarr 🧹

Suppression synchronisée entre Radarr/Sonarr et qBittorrent : supprimer un
film ou une série, dans un sens comme dans l'autre, cascade automatiquement
partout — fichiers, torrents qBittorrent (y compris les doublons
cross-seed), et entrée Seerr/Overseerr — sans ménage manuel dans trois
interfaces différentes.

## Pourquoi

Sur une stack *arr avec cross-seed actif, supprimer un film "à la main"
veut dire : le supprimer dans Radarr, retrouver et supprimer le ou les
torrents correspondants dans qBittorrent (souvent plusieurs, un par
tracker via cross-seed), puis nettoyer l'entrée dans Seerr/Overseerr — qui,
soit dit en passant, ne le fait pas tout seul même via son bouton "Remove
from Radarr" (bug connu upstream : l'action met à jour le statut interne
de Seerr sans réellement appeler l'API Radarr).

Nettoyarr fait ce ménage automatiquement, dans les deux sens :

- **Radarr/Sonarr → qBittorrent** : suppression d'un film/épisode/série
  depuis Radarr ou Sonarr → webhook → tous les torrents correspondants
  (y compris cross-seed) sont supprimés dans qBittorrent, et l'entrée
  Seerr correspondante est nettoyée (films uniquement, voir plus bas).
- **qBittorrent → Radarr/Sonarr** : clic droit sur un ou plusieurs
  torrents dans qBittorrent (menu contextuel injecté par un userscript) →
  le film est supprimé de Radarr, ou l'épisode/la saison passe en
  `unmonitored` sur Sonarr.

## Fonctionnalités

- Nettoyage synchronisé dans les deux sens (Radarr/Sonarr ↔ qBittorrent)
- Suppression de tous les doublons cross-seed d'un coup (matching par
  taille de fichier exacte, pas par taille totale de torrent)
- Nettoyage automatique de l'entrée Seerr/Overseerr correspondante
- Prise en charge de plusieurs instances Radarr/Sonarr en parallèle
- Sélection multiple de torrents dans le menu qBittorrent
- Interface de configuration web (`/config`), éditable à chaud sans
  toucher au `docker-compose.yml` ni redémarrer le conteneur
- Mode `DRY_RUN` pour tester sans rien supprimer réellement
- Authentification HTTP Basic optionnelle sur les endpoints sensibles

## Installation rapide

```yaml
services:
  nettoyarr:
    image: ghcr.io/gusdezup/nettoyarr:latest
    container_name: nettoyarr
    ports:
      - 9999:9999
    restart: unless-stopped
    environment:
      - DRY_RUN=true
    volumes:
      - ./data:/app/data
    deploy:
      resources:
        limits:
          memory: 128M
```

Voir [`docker-compose-example.yml`](./docker-compose-example.yml) pour la
version commentée avec toutes les variables optionnelles.

`DRY_RUN=true` est la seule variable vraiment utile à définir ici : c'est
un filet de sécurité pour le tout premier démarrage (avant que `/app/data`
contienne une config), à repasser à `false` depuis `/config` une fois que
tu as vérifié les logs. Le volume `./data` est, lui, indispensable : c'est
là que vit la config persistée.

Après `docker-compose up -d`, tout le reste (qBittorrent, Seerr, TMDB,
instances Radarr/Sonarr, authentification...) se configure sur
`http://<ip-du-nas>:9999/config` — un formulaire web, pas de JSON à taper
à la main pour les instances Radarr/Sonarr (champs URL + clé API séparés,
avec boutons pour en ajouter/retirer).

## Authentification

Par défaut, `/config`, `/delete-by-hash`, `/file-delete` et `/item-delete`
sont accessibles **sans authentification** à quiconque atteint le port
`9999` (`/health` reste toujours ouvert, healthcheck sans donnée
sensible). Un bandeau d'avertissement s'affiche sur `/config` tant que
c'est le cas.

**Si ce port est accessible au-delà de ton réseau local, active
l'authentification** : renseigne `AUTH_USER`/`AUTH_PASS` sur `/config`
(HTTP Basic Auth). Une fois activée, pense à répercuter les mêmes
identifiants à 3 endroits :

1. **Radarr/Sonarr** (`Settings > Connect` → ton webhook `file-delete`/
   `item-delete`) : champs Username/Password du webhook — nativement
   supportés, rien à coder.
2. **Le userscript qBittorrent** (voir section suivante) : menu
   "⚙️ Configurer Nettoyarr" de l'extension.
3. Rien côté `/health` — cet endpoint reste volontairement ouvert.

## Bouton qBittorrent (userscript)

[`nettoyarr-qbit-button.user.js`](./nettoyarr-qbit-button.user.js) ajoute
une entrée **"🧹 Nettoyarr"** dans le menu contextuel (clic droit) de
qBittorrent WebUI, pour déclencher la suppression cascade directement
depuis là — un ou plusieurs torrents à la fois, films et/ou séries (packs
de saison complets pris en charge).

### Installation

1. Installe l'extension [Tampermonkey](https://www.tampermonkey.net/) ou
   [Violentmonkey](https://violentmonkey.github.io/) sur ton navigateur.
2. Ouvre `nettoyarr-qbit-button.user.js`, copie tout le contenu, crée un
   nouveau script dans l'extension et colle-le (ou utilise "Importer un
   fichier" si l'extension le propose).
3. **Édite les 2 lignes suivantes** en tête du script, avec l'adresse
   réelle de ton qBittorrent :
   ```
   // @match        http://TON_URL_QBITTORRENT:8081/*
   // @connect      TON_URL_QBITTORRENT
   ```
   Ces deux lignes sont des métadonnées lues par l'extension **avant même
   l'exécution du script** (elles décident sur quelle page l'injecter) —
   c'est la seule chose à éditer directement dans le code, et ça ne
   change quasiment jamais une fois réglé.
4. Sauvegarde, recharge la page qBittorrent WebUI.

### Configuration (URL Nettoyarr et identifiants)

L'URL de ton conteneur Nettoyarr et les identifiants d'authentification
(si activée, voir ci-dessus) **ne se mettent pas dans le code** — ils se
règlent une fois via un menu, et survivent au remplacement du script par
une future mise à jour :

1. Clique sur l'icône Tampermonkey/Violentmonkey dans la barre d'outils.
2. Sous le nom du script, clique sur **"⚙️ Configurer Nettoyarr
   (URL / auth)"**.
3. Renseigne successivement : l'URL de ton conteneur Nettoyarr (ex.
   `http://192.168.1.20:9999`), puis `AUTH_USER`, puis `AUTH_PASS`
   (laisse ces deux derniers vides si l'authentification est désactivée
   côté conteneur).

Si tu ne vois pas cette entrée de menu, vérifie que la version installée
correspond bien au fichier du repo (`Ctrl+A` dans l'éditeur du script pour
comparer), et recharge complètement l'onglet qBittorrent après toute mise
à jour du script.

## Architecture

Le conteneur écoute sur le port `9999` et expose deux routes webhook, à
configurer dans Radarr/Sonarr (`Settings > Connect > Webhook`) pour chaque
instance (radarr, radarr-animes, radarr-enfants, sonarr, sonarr-enfants...) :

| Route             | Déclenché par                                                      | Rôle |
|--------------------|----------------------------------------------------------------------|------|
| `/file-delete`     | `On Movie File Delete` (Radarr) / `On Episode File Delete` (Sonarr)   | Remplacement de qualité (upgrade) : un seul fichier remplacé, l'item reste. |
| `/item-delete`     | `On Movie Delete` (Radarr) / `On Series Delete` (Sonarr)              | Suppression complète : nettoyage qBittorrent **et** Seerr. |
| `/delete-by-hash`  | Menu contextuel qBittorrent (userscript)                              | Suppression déclenchée depuis qBittorrent, cascade vers Radarr/Sonarr. |
| `/config`          | —                                                                      | Interface web de configuration. |
| `/health`          | —                                                                      | Vérifie la connexion à qBittorrent (toujours ouvert, sans auth). |

### Pourquoi deux routes séparées pour les webhooks Radarr/Sonarr

Lors d'une suppression complète, Radarr/Sonarr ne déclenchent **pas**
l'événement "File Delete" séparément — seul l'événement de suppression
d'item est envoyé, avec la taille totale du dossier (`movieFolderSize`
côté Radarr). D'où la distinction entre les deux routes.

### Matching qBittorrent : fichier par fichier, jamais taille totale

Un torrent cross-seedé peut contenir des extras (`sample.mkv`, `.nfo`...)
que Radarr/Sonarr n'importent jamais dans la bibliothèque. La taille
totale d'un torrent avec extras ne correspondra donc jamais exactement à
`movieFolderSize`. Le matching se fait donc **fichier par fichier** : on
compare la taille du fichier vidéo réellement hardlinké avec celle de
chaque fichier de chaque torrent qBittorrent. Un hardlink garantit une
taille identique à l'octet près, peu importe le reste du contenu du
torrent — ça permet de retrouver et supprimer tous les doublons
cross-seedés d'un coup.

## Variables d'environnement

Toutes ces variables ne servent que de **valeurs de départ pour le tout
premier démarrage** (avant que `/app/data/config.json` existe) —
ensuite, `/config` fait autorité et ces lignes n'ont plus d'effet, même
après un `docker-compose down && up -d`.

| Variable        | Défaut                        | Description |
|-----------------|--------------------------------|--------------|
| `DRY_RUN`       | `true`                          | Ne supprime rien réellement, log seulement |
| `QBIT_URL`      | `http://localhost:8081`        | URL de l'interface qBittorrent |
| `QBIT_API_KEY`  | —                               | Clé API (Bearer, qBittorrent >= 5.2.0) — recommandé |
| `QBIT_USER`     | —                               | Fallback : login classique par cookie de session |
| `QBIT_PASS`     | —                               | Fallback : mot de passe |
| `SEERR_URL`     | `http://seerr:5055`            | URL de l'instance Seerr/Overseerr |
| `SEERR_API_KEY` | —                               | Clé API Seerr |
| `TMDB_API_KEY`  | —                               | Requis uniquement pour la conversion tvdbId → tmdbId (Sonarr) |
| `AUTH_USER`     | —                               | Identifiant HTTP Basic Auth — voir section Authentification |
| `AUTH_PASS`     | —                               | Mot de passe HTTP Basic Auth |
| `LOG_LEVEL`     | `INFO`                          | Passer à `DEBUG` pour voir les payloads webhook bruts |
| `RADARR_INSTANCES` | `[]`                         | JSON, seulement comme valeur de départ — préfère `/config` |
| `SONARR_INSTANCES` | `[]`                         | Idem |
| `LISTEN_PORT`   | `9999`                          | Port d'écoute du webhook listener |

## ⚠️ Avant de passer en prod

**Démarre toujours avec `DRY_RUN=true`** et regarde les logs
(`LOG_LEVEL=DEBUG`) pour vérifier que :
- le matching qBittorrent trouve bien les bons torrents (et pas d'autres) ;
- les IDs Seerr correspondent bien à ton setup.

Les noms exacts des champs dans les payloads webhook peuvent varier
légèrement selon la version de Radarr/Sonarr — vérifie dans les logs
avant de passer en `DRY_RUN=false`.

## Statut du projet

Encore en phase de test et de développement actif — en particulier, le
nettoyage Seerr et la suppression complète pour les séries (Sonarr) sont
peu testés en conditions réelles. Les retours de bugs et remarques sont
bienvenus, notamment sur des configurations différentes de la mienne.
