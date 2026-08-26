# Changelog

Toutes les modifications notables de ce projet seront documentées ici.

Le format suit [Keep a Changelog](https://keepachangelog.com/fr/1.0.0/),
et ce projet respecte le [Semantic Versioning](https://semver.org/lang/fr/).

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
