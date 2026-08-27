# Changelog

Toutes les modifications notables de ce projet seront documentées ici.

Le format suit [Keep a Changelog](https://keepachangelog.com/fr/1.0.0/),
et ce projet respecte le [Semantic Versioning](https://semver.org/lang/fr/).

## [0.4.1] - 2026-08-27

### Fixed
- Remplacement des valeurs par défaut personnelles (IP privée `192.168.1.20`) par des placeholders neutres (`localhost`) dans `webhook.py` : valeur de repli de `QBIT_URL`, commentaires d'exemple `RADARR_INSTANCES`/`SONARR_INSTANCES`, et placeholders des champs URL d'instance dans la page `/config` (HTML et JS). Ces valeurs ne servaient que de brouillon temporaire au tout premier démarrage (écrasées dès la première sauvegarde depuis `/config`), mais affichaient une IP étrangère à quiconque d'autre déployait le projet.
- `nettoyarr-qbit-button.user.js` : remplacement de l'IP d'exemple `192.168.1.20` par des placeholders explicites (`TON_URL_QBITTORRENT` pour `@match`/`@connect`, `TON_URL_NETTOYARR` pour `NETTOYARR_URL`) — contrairement à `webhook.py`, `@match`/`@connect` sont des métadonnées statiques lues par le gestionnaire de userscript (Tampermonkey/Violentmonkey) avant même l'exécution, donc pas configurables depuis `/config` ; chacun doit éditer ces lignes pour pointer vers son propre serveur.

## [0.4.0] - 2026-08-27

### Added
- Interface de configuration web sur `/config` (GET pour le formulaire, POST pour sauvegarder) : toutes les variables (qBittorrent, Seerr, TMDB, `DRY_RUN`, `LOG_LEVEL`, poll qBit, `RADARR_INSTANCES`/`SONARR_INSTANCES`) sont désormais éditables sans toucher au `docker-compose.yml` ni redémarrer le conteneur
- Config persistée dans un fichier JSON (`CONFIG_PATH`, défaut `/app/data/config.json`) sur un nouveau volume en écriture ; au premier démarrage, initialisée depuis les variables d'environnement puis sauvegardée — les variables d'env deviennent alors de simples valeurs par défaut pour un premier démarrage, plus la source de vérité
- Instances Radarr/Sonarr éditables via des champs URL + clé API dédiés (plus de JSON à taper à la main)
- `docker-compose-example.yml` : version vierge et commentée du compose, à committer dans le repo pour tout nouvel utilisateur, avec distinction claire entre ce qui est indispensable (volume `data`, `DRY_RUN`) et ce qui est optionnel
- Le bouton flottant est remplacé par une vraie entrée **"🧹 Nettoyarr"** injectée dans le menu contextuel natif de qBittorrent (clic droit sur un torrent), à côté de "Retirer", "Copier", etc.
- **Support de la sélection multiple** : clic droit sur plusieurs torrents sélectionnés déclenche une suppression pour chacun (`torrentsTable.selectedRowsIds()` renvoie tous les hashes sélectionnés, plus seulement le premier) — lève la limitation notée dans les "Known limitations" de la v0.3.0
- Popup de confirmation et de résultat custom (HTML, stylée), à la place des `confirm()`/`alert()` natifs du navigateur qui affichaient toujours l'origine de la page en en-tête
- `/delete-by-hash` répond désormais en JSON structuré (`ok`, `title`/`summary`, `targets`, ...) au lieu d'un texte libre destiné aux logs — le script construit son propre message précis, avec les cibles réellement nettoyées (Seerr **uniquement pour les films**, jamais pour un épisode supprimé par ce chemin — cohérent avec le choix assumé documenté en tête de `webhook.py`)
- Nom du/des torrent(s) affiché dans la confirmation et le résultat, récupéré directement via l'API qBittorrent (`/api/v2/torrents/info`, même origine que la page, pas besoin de `GM_xmlhttpRequest` pour cet appel)

### Changed
- `docker-compose.yml` : ajout du volume `data` (écriture) requis par la nouvelle config persistée
- `nettoyarr-qbit-button.user.js` : accès à `torrentsTable` via `unsafeWindow` (objet de la page qBittorrent, hors du contexte isolé du userscript) au lieu de lire le hash depuis le presse-papier ou une popup

### Removed
- Le bouton flottant en bas à droite de l'écran, la lecture du presse-papier, et la popup de saisie manuelle du hash — plus nécessaires

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
