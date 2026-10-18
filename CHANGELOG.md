# Changelog

Toutes les modifications notables de ce projet seront documentées ici.

Le format suit [Keep a Changelog](https://keepachangelog.com/fr/1.0.0/),
et ce projet respecte le [Semantic Versioning](https://semver.org/lang/fr/).

## [0.6.1] - 2026-10-18

### Fixed
- **Hardlinks : fichiers annexes oubliés.** Quand une release supprimée avait une autre copie hardlinkée ailleurs sur le NAS, seuls les gros fichiers (≥ 50 Mo, ceux de l'index) de cette copie étaient proposés à la suppression : le `.nfo`, les images, les sous-titres et le dossier restaient sur place. Les petits fichiers du torrent sont désormais relevés aussi (inode et chemin relatif), puis recherchés **par inode** dans le dossier de la release de chaque copie retrouvée. Un fichier ajouté à côté par un autre outil n'est jamais proposé ; un fichier également utilisé par un torrent conservé n'est pas touché.
- **Dossier de la copie supprimé une fois vide**, de bas en haut, jamais au-dessus du dossier de la release ni de `LINKS_SCAN_ROOT`. Un dossier qui ne contient plus que des métadonnées Synology (`@eaDir`) est considéré comme vide ; un dossier qui contient encore autre chose est conservé et signalé. Si la copie n'a pas la structure du torrent, ou pour un torrent sans dossier, seul le dossier du fichier est examiné, sans suppression de dossier.
- `nettoyarr-qbit-button.user.js` : le fichier publié en 0.6.0 était une version antérieure (adresse à éditer dans `@connect`, bilan des hardlinks réservé aux orphelins). Il est remplacé par la bonne version : `@connect *`, `@match` personnalisés, bilan des hardlinks pour films, épisodes et orphelins.

### Changed
- `/delete-links` renvoie `dirs_removed` et `dirs_kept` ; les liens proposés portent `companion: true` pour les fichiers annexes.
- Userscript et explorateur : fichiers annexes comptés à part dans le bilan, marqués « annexe » dans la confirmation, dossiers supprimés ou conservés indiqués dans le résultat.

## [0.6.0] - 2026-10-09

### Added
- **Explorateur** (`/explorer`, désormais page d'accueil : `/` y redirige) : aide au choix des suppressions. Un inventaire regroupe les torrents qBittorrent en **releases** (torrents qui partagent au moins un fichier ≥ 50 Mo à l'octet près : copies cross-seed, un torrent par tracker, plus un éventuel pack qui les contient) et les relie à Radarr/Sonarr (titre, date d'import), à Tautulli (dernier visionnage, bibliothèque Plex) et à leurs trackers avec leurs seeders. Inventaire calculé en arrière-plan avec progression (`POST /inventory/refresh`, état sur `/inventory`), puis filtré côté navigateur : changer un critère est instantané. Tableau triable, sélection multiple, suppression par lot via `/delete-by-hash` (même chemin que le bouton qBittorrent), bilan des hardlinks et récapitulatif final. Bandeau visible quand `DRY_RUN` est actif.
- **Règles de sélection cumulables** dans l'explorateur : chaque ligne associe un critère, un opérateur et une valeur, combinées par « toutes » (ET) ou « au moins une » (OU), avec des sous-groupes ayant leur propre logique (un niveau). Critères : texte partout, titre, nom des torrents (contient / ne contient pas), tracker (en seed sur / pas en seed sur / seulement sur), catégorie qBittorrent, instance *arr, type, bibliothèque Plex, dossier (est / n'est pas), taille, seeders sur tous les trackers ou sur au moins un, nombre de trackers (supérieur / inférieur), date d'import et dernier visionnage (avant / après, jamais vu, absent de Plex). Une règle sans valeur est ignorée. Les règles sont enregistrées côté serveur (`data/explorer.json`, `GET`/`POST /explorer/criteria`) et reprises sur tout appareil.
- **Revalidation avant suppression** : au clic sur « Supprimer », l'inventaire est reconstruit et les règles réévaluées sur chaque release cochée. Une release qui ne les respecte plus (ex. copie ajoutée par cross-seed sur un tracker exclu depuis le dernier inventaire) ou qui a disparu est retirée de la sélection, avec une seconde confirmation listant les retraits. Si l'actualisation échoue, rien n'est supprimé.
- **Noms des trackers** : nom de l'indexeur Prowlarr (nouveaux réglages `PROWLARR_URL`/`PROWLARR_API_KEY`, rapprochement de l'adresse d'announce avec les URL de l'indexeur par hôte, domaine puis nom de domaine), sinon nom déduit du domaine, et renommage manuel prioritaire depuis `/config` (`TRACKER_NAMES`). Les adresses d'un même tracker sont regroupées et traitées comme un seul tracker. Les règles restent liées à l'adresse : renommer n'en casse aucune.
- **Tautulli** (`TAUTULLI_URL`/`TAUTULLI_API_KEY`, optionnel) : dernier visionnage par film/épisode et bibliothèque Plex. Historique relié par `rating_key`, ou par titre quand une bibliothèque Plex a été recréée.
- **Torrents orphelins** (gérés par aucun Radarr/Sonarr, ex. copie cross-seed isolée) : `/delete-by-hash` les supprime désormais dans qBittorrent et sur le NAS au lieu de répondre 404 sans rien faire (`kind: "orphan"`). Un autre torrent n'est supprimé comme doublon que si tous ses fichiers ≥ 50 Mo sont inclus dans le torrent sélectionné ; un torrent plus large est conservé et signalé.
- **Bilan des autres hardlinks** après toute suppression depuis qBittorrent ou l'explorateur (film, épisodes ou orphelin) : inode relevé avant suppression avec un descripteur ouvert, nombre de liens réellement restants relu après. Les liens restants sont retrouvés via un **index** des fichiers ≥ 50 Mo de `LINKS_SCAN_ROOT` (nouveau réglage, défaut `/media`) : sauvegardé dans `data/links-index.json.gz` et disponible dès le démarrage, mis à jour toutes les heures en ne relisant que les dossiers modifiés, reconstruit en entier une fois par semaine. Chaque chemin est revérifié ; un lien absent de l'index est signalé comme non localisé, sans parcours du disque. Les liens utilisés par aucun torrent restant sont proposés à la suppression.
- Route `POST /delete-links?job=…` (protégée par l'auth) : supprime, après confirmation, uniquement les hardlinks relevés côté serveur pour ce job, à usage unique, avec revérification de l'inode. Respecte `DRY_RUN`.
- `/delete-by-hash?links=0` : suppression sans bilan des hardlinks.
- `/config` : sections Tautulli, Prowlarr et « Noms des trackers » (une ligne par tracker, adresses lues directement dans qBittorrent, nom Prowlarr ou automatique affiché par défaut), lien vers l'explorateur.
- `nettoyarr-qbit-button.user.js` : bilan des hardlinks dans le résultat (espace libéré, liens restants, espace retenu par un torrent conservé) et seconde confirmation listant les fichiers à supprimer avec leur taille.

### Changed
- Recherche depuis qBittorrent : une erreur d'interrogation d'un Radarr/Sonarr pendant le parcours complet est désormais remontée ; dans ce cas le fallback orphelin est refusé (503) plutôt que de conclure à « aucun match ».
- Index des hardlinks limité au système de fichiers de `LINKS_SCAN_ROOT`, dossiers `#snapshot`, `#recycle` et `@eaDir` ignorés : les snapshots Synology visibles faisaient relire toute la bibliothèque une fois par snapshot, et la corbeille est laissée à l'utilisateur (espace récupéré à son vidage).
- Popups du userscript : contenu défilant au-delà de 60 % de la hauteur d'écran.
- `nettoyarr-qbit-button.user.js` : `@connect *` au lieu d'une adresse à éditer ; l'adresse de qBittorrent se déclare désormais dans les `@match` personnalisés de l'extension (Violentmonkey : onglet Configuration ; Tampermonkey : Correspondances de l'utilisateur), qui ne sont plus écrasés par le copier-coller d'une mise à jour.

### Fixed
- `/config` : un champ laissé vide dans une liste répétée (ex. clé API vide d'une instance Radarr/Sonarr) décalait toutes les valeurs suivantes d'une ligne à la sauvegarde. Les champs vides sont désormais conservés à leur place.

### Docs
- `README.md` : sections « Explorateur », « Torrents orphelins » et « Autres hardlinks », montage `/media` en écriture, variables `LINKS_SCAN_ROOT`, `TAUTULLI_*`, `PROWLARR_*`, `TRACKER_NAMES`, nouvelles routes, installation du userscript via les `@match` personnalisés.

## [0.5.3] - 2026-09-27

### Added
- Suivi de progression en temps réel depuis qBittorrent : `/delete-by-hash?async=1` lance le nettoyage en arrière-plan et renvoie un `job_id` ; nouvelle route `/job?id=…` (protégée par l'auth) qui expose l'étape en cours, le compteur, le pourcentage, le temps écoulé et le temps sans progression. Sans `async=1`, `/delete-by-hash` garde son comportement synchrone.
- `nettoyarr-qbit-button.user.js` : popup de progression ouverte dès la confirmation — étape en cours, barre qui se remplit (ou défile quand l'étape n'a pas de compteur), chrono, `Torrent i/n` en sélection multiple, résultats ajoutés au fil de l'eau, durée totale à la fin. Avertissement après 60 s sans progression (sans annuler).
- Identification ciblée depuis qBittorrent : historique Radarr/Sonarr (`/api/v3/history?downloadId=<hash>`), puis analyse du nom de release (`/api/v3/parse`), le parcours complet des bibliothèques n'étant plus qu'un dernier recours. Garde-fou inchangé : correspondance à l'octet près obligatoire sur tous les chemins.
- Cache des listes de fichiers qBittorrent (immuables pour un hash donné), préchargé au démarrage en arrière-plan ; un seul scan à la fois, les autres recherches réutilisent le cache.
- Résultat de `/delete-by-hash` : nombre de torrents qBit supprimés (`torrents`), et `kind: "gone"` pour un torrent déjà absent de qBittorrent (typiquement un doublon cross-seed nettoyé juste avant dans la même sélection).

### Changed
- `/delete-by-hash` supprime lui-même les torrents qBit correspondants (repérés avant la suppression côté Radarr/Sonarr, supprimés après), au lieu de dépendre uniquement du webhook de l'*arr.
- Les nettoyages depuis qBittorrent sont exécutés un par un, côté serveur comme côté userscript (fin du `Promise.all`).

### Fixed
- Webhooks `/file-delete` et `/item-delete` traités en arrière-plan avec réponse immédiate : Sonarr restait bloqué pendant le scan qBit (~40 s par épisode sur un pack de saison, plusieurs minutes au total).
- Scan qBit de plusieurs minutes à chaque épisode supprimé (un appel `torrents/files` par torrent, ~1300 appels) : remplacé par le cache.
- Sélection multiple de doublons cross-seed d'une même saison : les requêtes parallèles se faisaient concurrence (404 parasite sur la seconde, réponse jamais reçue pour la première).
- Popup du userscript qui pouvait rester sans réponse indéfiniment : timeouts explicites (lancement, suivi du job).

## [0.5.1] - 2026-08-27

### Added
- `nettoyarr-qbit-button.user.js` : `NETTOYARR_URL`/`AUTH_USER`/`AUTH_PASS` ne sont plus codés en dur dans le script — ils sont désormais stockés dans le stockage de l'extension (`GM_setValue`/`GM_getValue`, attaché au script via `@namespace`), réglables via un nouveau menu "⚙️ Configurer Nettoyarr (URL / auth)" accessible depuis l'icône Tampermonkey/Violentmonkey. Coller une future mise à jour du script par-dessus l'ancienne ne réinitialise plus ces valeurs.

### Changed
- `@match`/`@connect` (adresse de qBittorrent) restent en dur dans les métadonnées — contrainte technique des userscripts, lus avant l'exécution du script — mais changent rarement une fois réglés, contrairement à l'URL Nettoyarr ou aux identifiants d'auth.

### Fixed
- `nettoyarr-qbit-button.user.js` : les noms de torrent longs sans espaces (uniquement des points, ex. `Titre.Serie.S01E12.1080p.WEB-Raws`) débordaient du cadre de la popup de confirmation/résultat au lieu de passer à la ligne, faute de point de coupure reconnu par le CSS par défaut. Ajout d'`overflow-wrap:anywhere` pour forcer le retour à la ligne même sans espace.

### Docs
- `README.md` : réécriture avec sections Installation rapide, Authentification, et Bouton qBittorrent (installation du userscript + configuration de l'URL/auth via le menu de l'extension). Mise à jour du tableau des variables d'environnement (ajout `AUTH_USER`/`AUTH_PASS`, `DRY_RUN` en tête, valeurs par défaut `localhost`).

## [0.5.0] - 2026-08-27

### Added
- Authentification HTTP Basic, optionnelle, via `AUTH_USER`/`AUTH_PASS` (éditables depuis `/config`, comme le reste de la config). Protège `/config` (GET+POST), `/delete-by-hash`, `/file-delete` et `/item-delete` — `/health` reste toujours ouvert (aucune donnée sensible, utile pour un healthcheck Docker simple).
- Bandeau d'avertissement persistant sur `/config` tant qu'`AUTH_USER`/`AUTH_PASS` sont vides (auth désactivée par défaut, pour ne pas casser les setups existants).
- `nettoyarr-qbit-button.user.js` : constantes `AUTH_USER`/`AUTH_PASS` (vides par défaut) — envoie l'en-tête `Authorization: Basic` via `GM_xmlhttpRequest` si renseignées, sans déclencher la popup de login native du navigateur.
- `docker-compose-example.yml` : section commentée documentant `AUTH_USER`/`AUTH_PASS`.

### Notes
- Compatible nativement avec les champs Username/Password des webhooks Radarr/Sonarr (`Settings > Connect`) — à renseigner côté Radarr/Sonarr si l'auth est activée côté Nettoyarr, sinon `/file-delete`/`/item-delete` répondront 401.
- Comparaison des identifiants à temps constant (`hmac.compare_digest`) pour éviter une timing attack.

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
