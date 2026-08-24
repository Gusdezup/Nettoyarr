# Changelog

Toutes les modifications notables de ce projet seront documentées ici.

Le format suit [Keep a Changelog](https://keepachangelog.com/fr/1.0.0/),
et ce projet respecte le [Semantic Versioning](https://semver.org/lang/fr/).

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
