# Nettoyarr 🧹

Webhook listener Python qui automatise le nettoyage complet quand un film
ou une série est supprimé depuis Radarr ou Sonarr : torrents qBittorrent
(y compris les doublons cross-seed) **et** entrée Seerr/Overseerr, en un
seul coup, sans ménage manuel dans trois interfaces différentes.

## Pourquoi

Sur une stack *arr avec cross-seed actif, supprimer un film "à la main"
veut dire : le supprimer dans Radarr, retrouver et supprimer le ou les
torrents correspondants dans qBittorrent (souvent plusieurs, un par
tracker via cross-seed), puis nettoyer l'entrée dans Seerr/Overseerr — qui,
soit dit en passant, ne le fait pas tout seul même via son bouton "Remove
from Radarr" (bug connu upstream : l'action met à jour le statut interne
de Seerr sans réellement appeler l'API Radarr).

Nettoyarr écoute les webhooks Radarr/Sonarr et fait ce ménage
automatiquement.

## Architecture

Le conteneur écoute sur le port `9999` et expose deux routes, à configurer
dans Radarr/Sonarr (`Settings > Connect > Webhook`) pour chaque instance
(radarr, radarr-animes, radarr-enfants, sonarr, sonarr-enfants...) :

| Route          | Déclenché par                                                      | Rôle |
|----------------|----------------------------------------------------------------------|------|
| `/file-delete` | `On Movie File Delete` (Radarr) / `On Episode File Delete` (Sonarr)   | Remplacement de qualité (upgrade) : un seul fichier remplacé, l'item reste. |
| `/item-delete` | `On Movie Delete` (Radarr) / `On Series Delete` (Sonarr)              | Suppression complète : nettoyage qBittorrent **et** Seerr. |
| `/health`      | —                                                                      | Vérifie la connexion à qBittorrent. |

### Pourquoi deux routes séparées

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

## Configuration

Variables d'environnement lues par `webhook.py` :

| Variable        | Défaut                        | Description |
|-----------------|--------------------------------|--------------|
| `QBIT_URL`      | `http://192.168.1.20:8081`     | URL de l'interface qBittorrent |
| `QBIT_API_KEY`  | —                               | Clé API (Bearer, qBittorrent >= 5.2.0) — recommandé |
| `QBIT_USER`     | —                               | Fallback : login classique par cookie de session |
| `QBIT_PASS`     | —                               | Fallback : mot de passe |
| `SEERR_URL`     | `http://seerr:5055`            | URL de l'instance Seerr/Overseerr |
| `SEERR_API_KEY` | —                               | Clé API Seerr |
| `TMDB_API_KEY`  | —                               | Requis uniquement pour la conversion tvdbId → tmdbId (Sonarr) |
| `LISTEN_PORT`   | `9999`                          | Port d'écoute du webhook listener |
| `DRY_RUN`       | `true`                          | Ne supprime rien réellement, log seulement |
| `LOG_LEVEL`     | `INFO`                          | Passer à `DEBUG` pour voir les payloads webhook bruts |

## ⚠️ Avant de passer en prod

**Démarre toujours avec `DRY_RUN=true`** et regarde les logs
(`LOG_LEVEL=DEBUG`) pour vérifier que :
- le matching qBittorrent trouve bien les bons torrents (et pas d'autres) ;
- les IDs Seerr correspondent bien à ton setup.

Les noms exacts des champs dans les payloads webhook peuvent varier
légèrement selon la version de Radarr/Sonarr — vérifie dans les logs
avant de passer en `DRY_RUN=false`.

## Roadmap

- **v0.2** : userscript Tampermonkey pour injecter un bouton
  "🧹 Nettoyarr" dans l'UI qBittorrent + endpoint `/delete-by-hash` pour
  déclencher le nettoyage directement depuis là (tri par taille de
  fichier).
