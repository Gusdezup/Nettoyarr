# Changelog

Toutes les modifications notables de ce projet seront documentées ici.

Le format suit [Keep a Changelog](https://keepachangelog.com/fr/1.0.0/),
et ce projet respecte le [Semantic Versioning](https://semver.org/lang/fr/).

## [0.3.0] - 2026-08-26

### Added
- Support complet des séries (Sonarr) sur le bouton qBittorrent : identifie et supprime chaque fichier-épisode correspondant, y compris pour un pack de saison complet en un seul clic (12 épisodes validés en conditions réelles)
- Chaque épisode supprimé est automatiquement passé en `unmonitored` (Sonarr ne le recherchera plus jamais automatiquement)
- Une saison entièrement vidée de ses fichiers est automatiquement passée en `unmonitored` — jamais en cas de suppression partielle, jamais la série elle-même
- Identification fiable de l'instance Sonarr concernée via `tvdbId` (évite toute action accidentelle sur la mauvaise instance quand plusieurs Sonarr sont configurés)
- `SONARR_INSTANCES` (même format que `RADARR_INSTANCES`) pour le bouton qBittorrent

### Fixed
- Utilisation directe de `tmdbId` quand il est présent dans le payload Sonarr, au lieu de systématiquement repasser par une conversion TMDB

### Known limitations
- `seriesFolderSize` (suppression complète d'une série via Sonarr) reste non confirmé par un test réel — à valider avant de s'y fier
- Le nettoyage Seerr pour une série n'a pas encore été testé en conditions réelles
- Le bouton qBittorrent ne gère qu'un seul torrent sélectionné à la fois (pas de sélection multiple)

## [0.2.0] - 2026-08-26

### Added
- Bouton dans la WebUI qBittorrent (userscript Tampermonkey/Violentmonkey) pour déclencher la suppression depuis qBit, avec cascade automatique vers Radarr/Seerr
- Endpoint `/delete-by-hash` : recherche le film correspondant sur toutes les instances Radarr configurées (`RADARR_INSTANCES`) et déclenche sa suppression
- Endpoint `/health` pour tester la connexion qBittorrent sans passer par Radarr/Sonarr
- Support de l'authentification par clé API qBittorrent (`QBIT_API_KEY`, qBittorrent ≥ 5.2.0)
- `LOG_LEVEL` configurable (passer en `DEBUG` pour voir le payload brut de chaque webhook)

### Fixed
- Matching qBittorrent corrigé : comparaison par taille de fichier individuel plutôt que taille totale du torrent (les extras comme les samples/nfo faussaient la comparaison) ou nom de fichier (renommé par Radarr/Sonarr à l'import)
- Parsing JSON qui plantait sur les réponses vides de l'API Seerr (ex: `204 No Content` après une suppression réussie)
- Ajout des headers `Referer`/`Origin` requis par qBittorrent ≥ 4.3.9 pour l'authentification par login/mot de passe

### Changed
- Le nettoyage qBittorrent pour une suppression complète (`item-delete`) se base maintenant sur `movieFolderSize` (taille du fichier vidéo seul), et non plus sur l'événement `On Movie/Episode File Delete` qui ne se déclenche pas lors d'une suppression complète

## [0.1.1] - 2026-08-24

### Added
- `Dockerfile` : image `python:3-alpine` embarquant `webhook.py`, sans dépendance externe (stdlib uniquement).
- Publication automatique de l'image sur `ghcr.io/gusdezup/nettoyarr` via GitHub Actions à chaque tag `v*`.

## [0.1.0] - 2026-08-24

### Added
- Webhook listener Python (`webhook.py`) avec deux routes par instance Radarr/Sonarr : `/file-delete` et `/item-delete`.
- Matching qBittorrent fichier par fichier (taille exacte), robuste aux extras des torrents cross-seedés.
- Suppression automatique de tous les torrents/doublons correspondants dans qBittorrent (`deleteFiles=true`).
- Nettoyage automatique de l'entrée média correspondante dans Seerr.
- Conversion tvdbId → tmdbId via TMDB pour le nettoyage Seerr côté séries (Sonarr).
- Authentification qBittorrent double mode : clé API (Bearer, >= 5.2.0) ou login classique par cookie de session avec headers CSRF.
- Mode `DRY_RUN` pour tester sans rien supprimer réellement.
- Endpoint `/health` pour vérifier la connectivité qBittorrent.
