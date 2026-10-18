# Nettoyarr 🧹

Suppression synchronisée entre Radarr/Sonarr et qBittorrent : supprimer un
film ou une série, dans un sens comme dans l'autre, cascade automatiquement
partout — fichiers, torrents qBittorrent (y compris les doublons
cross-seed), et entrée Seerr/Overseerr — sans ménage manuel dans trois
interfaces différentes. Un explorateur aide en plus à choisir quoi
supprimer, selon des règles combinables.

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
- **Explorateur** : une page web qui liste toutes les releases avec leurs
  trackers, seeders, dates d'import et de dernier visionnage, pour choisir
  quoi supprimer selon des règles (taille, trackers, visionnage…), puis
  supprimer la sélection en un clic.

## Fonctionnalités

- Nettoyage synchronisé dans les deux sens (Radarr/Sonarr ↔ qBittorrent)
- Suppression de tous les doublons cross-seed d'un coup (matching par
  taille de fichier exacte, pas par taille totale de torrent)
- Nettoyage automatique de l'entrée Seerr/Overseerr correspondante
- Prise en charge de plusieurs instances Radarr/Sonarr en parallèle
- Explorateur des releases avec règles de sélection cumulables (ET/OU,
  groupes), noms des trackers repris de Prowlarr, dernier visionnage via
  Tautulli, et revérification des règles juste avant toute suppression
- Sélection multiple de torrents dans le menu qBittorrent, avec suivi de
  progression en temps réel (étape en cours, barre, chrono)
- Torrents orphelins (gérés par aucun Radarr/Sonarr, ex. copie cross-seed
  isolée) : supprimés quand même dans qBittorrent et sur le NAS
- Bilan des autres hardlinks des fichiers supprimés, proposés à la
  suppression après confirmation, pour libérer réellement l'espace disque
- Identification rapide depuis qBittorrent : historique Radarr/Sonarr
  (`downloadId`), puis analyse du nom de release (`/api/v3/parse`), le
  parcours complet de la bibliothèque n'étant qu'un dernier recours
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
      - /volume1/media:/media   # même chemin que dans le conteneur qBittorrent
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
là que vivent la config persistée, les règles de l'explorateur et l'index
des hardlinks. Le montage des médias est nécessaire pour le bilan des
hardlinks (voir plus bas).

Après `docker-compose up -d`, tout le reste (qBittorrent, Seerr, TMDB,
Tautulli, Prowlarr, instances Radarr/Sonarr, authentification...) se
configure sur `http://<ip-du-nas>:9999/config` — un formulaire web, pas de
JSON à taper à la main pour les instances Radarr/Sonarr (champs URL + clé
API séparés, avec boutons pour en ajouter/retirer).

## Authentification

Par défaut, toutes les routes sauf `/health` (`/explorer`, `/config`,
`/delete-by-hash`, `/job`, `/delete-links`, `/file-delete`, `/item-delete`…)
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

Le navigateur demande les identifiants à l'ouverture de l'explorateur ou de
`/config`.

## Explorateur

`http://<ip-du-nas>:9999/` (ou `/explorer`) ouvre l'explorateur : une aide
au choix des suppressions, pensée pour une bibliothèque qui seed beaucoup.

### Releases

Nettoyarr regroupe les torrents qBittorrent en **releases** : des torrents
qui partagent au moins un fichier de 50 Mo ou plus, à l'octet près. Une
release réunit donc toutes les copies cross-seed d'un même contenu (un
torrent par tracker), plus un éventuel pack qui la contient. Pour chaque
release, l'explorateur affiche :

- le titre Radarr/Sonarr (ou le nom du torrent, sans *arr) et sa taille ;
- chaque tracker avec son nombre de seeders ;
- la date d'import (Radarr/Sonarr, ou d'ajout dans qBittorrent sans *arr) ;
- la date du dernier visionnage et la bibliothèque Plex, si Tautulli est
  configuré.

L'inventaire est construit en arrière-plan (bouton « Actualiser
l'inventaire », progression affichée), puis filtré dans le navigateur :
changer un critère est instantané. Compter une dizaine de secondes pour
une actualisation une fois le cache qBittorrent rempli, plusieurs minutes
pour la toute première après un démarrage.

### Règles

Les critères se composent en règles : chaque ligne associe un critère, un
opérateur et une valeur. En tête, « toutes » (ET) ou « au moins une » (OU).
**+ Groupe** ajoute un sous-bloc avec sa propre logique, l'équivalent de
parenthèses :

```
toutes :
  Taille (Gio)        supérieur à     10
  Tracker             pas en seed sur HD-Forever
  [Groupe] au moins une :
     Dernier visionnage   jamais vu
     Dernier visionnage   avant le 01/01/2025
```

Critères disponibles : texte (partout, titre, nom des torrents), tracker
(en seed sur / pas en seed sur / seulement sur), catégorie qBittorrent,
instance *arr, type, bibliothèque Plex, dossier, taille, seeders (sur tous
les trackers ou sur au moins un), nombre de trackers, date d'import,
dernier visionnage (avant / après / jamais vu / absent de Plex).

Une règle sans valeur est ignorée. Les règles sont enregistrées côté
serveur (`data/explorer.json`) et reprises sur n'importe quel appareil.

« Pas en seed sur » s'évalue sur **tous** les torrents de la release : une
seule copie sur le tracker exclu suffit à protéger la release.

### Noms des trackers

Les trackers sont affichés par nom plutôt que par adresse d'announce :

1. nom saisi à la main dans `/config` (section « Noms des trackers »),
   prioritaire ;
2. sinon, nom de l'indexeur **Prowlarr** dont une URL correspond à
   l'adresse d'announce (hôte exact, puis domaine, puis nom de domaine
   seul — `tracker.exemple.org` est reconnu pour un indexeur
   `https://exemple.org/`) ;
3. sinon, nom déduit du domaine.

Les adresses qui reviennent au même tracker sont regroupées et traitées
comme un seul tracker. Les règles restent liées à l'adresse : renommer un
tracker n'en casse aucune.

### Suppression

Coche les releases, puis « Supprimer la sélection ». Juste avant de
supprimer, Nettoyarr **reconstruit l'inventaire et réévalue les règles**
sur chaque release cochée : une release qui ne les respecte plus (par
exemple parce que cross-seed vient d'ajouter une copie sur un tracker
exclu) ou qui a disparu est retirée de la sélection, et une seconde
confirmation liste les retraits. Si l'actualisation échoue, rien n'est
supprimé.

La suppression passe ensuite par le même chemin que le bouton qBittorrent
(`/delete-by-hash`), une release à la fois : Radarr/Sonarr, qBittorrent
(toutes les copies), NAS, Seerr pour les films, puis bilan des hardlinks.

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
3. **Déclare l'adresse de ton qBittorrent dans les réglages du script**,
   pas dans le code : ces réglages survivent aux mises à jour, alors qu'une
   ligne `@match` éditée à la main est écrasée à chaque copier-coller d'une
   nouvelle version.
   - **Violentmonkey** : éditeur du script → onglet **Configuration** →
     ajoute `http://<ip-qbittorrent>:8081/*` dans les `@match`
     personnalisés et décoche la conservation des `@match` d'origine.
   - **Tampermonkey** : éditeur du script → onglet **Paramètres** →
     **Correspondances de l'utilisateur**, et décoche « Correspondances
     d'origine ».

   `@connect *` n'a pas à être modifié. Tampermonkey demande une seule fois
   l'autorisation de joindre ton Nettoyarr, Violentmonkey ne l'utilise pas.
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

Le conteneur écoute sur le port `9999` et expose les routes suivantes
(configuration des webhooks Radarr/Sonarr détaillée dans la section
suivante) :

| Route                  | Déclenché par                                                      | Rôle |
|-------------------------|----------------------------------------------------------------------|------|
| `/file-delete`          | `On Movie File Delete` (Radarr) / `On Episode File Delete` (Sonarr)   | Remplacement de qualité (upgrade) : un seul fichier remplacé, l'item reste. |
| `/item-delete`          | `On Movie Delete` (Radarr) / `On Series Delete` (Sonarr)              | Suppression complète : nettoyage qBittorrent **et** Seerr. |
| `/delete-by-hash`       | Menu contextuel qBittorrent (userscript), explorateur                 | Suppression d'un torrent et de sa release, cascade vers Radarr/Sonarr. Avec `?async=1`, répond immédiatement un `job_id` ; `?links=0` saute le bilan des hardlinks. |
| `/job`                  | Userscript, explorateur (suivi, toutes les secondes)                  | État d'un nettoyage ou d'un inventaire en cours : étape, compteur, pourcentage, temps écoulé, résultat final. |
| `/delete-links`         | Userscript, explorateur (après confirmation manuelle)                 | Suppression des autres hardlinks des fichiers supprimés, relevés par le serveur pendant le job. |
| `/explorer`             | Navigateur (`/` y redirige)                                            | Explorateur des releases. |
| `/inventory`            | Explorateur                                                            | Inventaire des releases (`GET`), reconstruction en arrière-plan (`POST /inventory/refresh`). |
| `/explorer/criteria`    | Explorateur                                                            | Lecture/enregistrement des règles de sélection. |
| `/config`               | —                                                                      | Interface web de configuration. |
| `/health`               | —                                                                      | Vérifie la connexion à qBittorrent (toujours ouvert, sans auth). |

Les webhooks `/file-delete` et `/item-delete` répondent immédiatement à
Radarr/Sonarr et font leur traitement en arrière-plan : un traitement long
ne bloque plus jamais l'*arr. Les listes de fichiers des torrents qBit
sont mises en cache (préchargé au démarrage) : un hash donné a toujours
la même liste de fichiers, inutile de la redemander à chaque recherche.

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

### Torrents orphelins

Si ni Radarr ni Sonarr ne connaissent un torrent (copie cross-seed isolée
dont l'original a été supprimé à la main, téléchargement manuel…), le
bouton qBittorrent et l'explorateur le suppriment quand même dans
qBittorrent et sur le NAS, avec ses doublons cross-seed. Garde-fous :

- si un Radarr/Sonarr n'a pas pu être interrogé, **rien n'est supprimé**
  (un « aucun match » ne voudrait alors rien dire) ;
- un autre torrent n'est supprimé comme doublon que si **tous** ses gros
  fichiers (≥ 50 Mo) sont inclus dans le torrent sélectionné : un torrent
  plus large (ex. une intégrale contenant la saison sélectionnée) est
  conservé et signalé.

### Autres hardlinks (espace disque)

Supprimer un hardlink ne libère aucun espace tant que d'autres liens vers
le même fichier existent (copie d'origine restée dans `downloads/` après un
retrait sans les fichiers, corbeille Radarr…). Depuis le bouton qBittorrent
ou l'explorateur, pour **toutes** les suppressions (film, épisodes ou
orphelin), Nettoyarr fait donc le bilan des liens restants :

1. avant suppression, il relève l'inode de chaque fichier et garde un
   descripteur ouvert dessus ;
2. après suppression (*arr + qBittorrent), il relit le nombre de liens
   réellement restants : s'il est à zéro, l'espace est libéré, rien à
   chercher ;
3. sinon il les retrouve via un **index** des fichiers de 50 Mo ou plus de
   `LINKS_SCAN_ROOT`, chaque chemin étant revérifié. L'index est sauvegardé
   dans `data/links-index.json.gz` (disponible dès le démarrage), mis à
   jour toutes les heures en ne relisant que les dossiers modifiés, et
   reconstruit en entier une fois par semaine. Un lien absent de l'index
   (corbeille Synology, lien créé depuis la dernière mise à jour) est
   signalé comme non localisé, sans parcours du disque, et l'index est
   rafraîchi pour la fois suivante ;
4. les liens qui ne servent à aucun torrent restant sont listés dans une
   **seconde confirmation**, et ne sont supprimés qu'après ton accord,
   seulement s'il s'agit toujours du même fichier.
   Les fichiers annexes de la release (`.nfo`, images, sous-titres) présents
   dans ces copies sont proposés avec eux — repérés par inode, jamais par
   leur nom — et le dossier de la copie est supprimé s'il ne contient plus
   rien d'autre.

L'index reste sur le volume de `LINKS_SCAN_ROOT` et ignore `#snapshot`,
`#recycle` et `@eaDir` : ce qui est dans la corbeille Synology sera libéré à
son vidage, Nettoyarr n'y touche pas. La toute première construction de
l'index peut prendre plusieurs minutes ; tant qu'elle n'est pas terminée,
les liens restants sont signalés comme non localisés.

Pour ça, Nettoyarr doit voir les fichiers **au même chemin que
qBittorrent**, en écriture :

```yaml
    volumes:
      - ./data:/app/data
      - /volume1/media:/media   # même montage que le conteneur qBittorrent
```

Sans ce montage, le torrent est quand même supprimé, mais la recherche des
autres hardlinks est ignorée (signalé dans le résultat). Les webhooks
Radarr/Sonarr (`/file-delete`, `/item-delete`) ne font pas ce bilan : il
n'y a personne pour confirmer.

## Configuration des webhooks Radarr/Sonarr

À répéter pour **chaque instance** (radarr, radarr-animes, sonarr,
sonarr-enfants...) — chacune a besoin de ses 2 propres webhooks, Nettoyarr
ne les crée pas automatiquement.

### Dans Radarr

`Settings > Connect > +` → choisir **Webhook** :

1. **Premier webhook** (upgrades de qualité) :
   - Name : au choix, ex. `Nettoyarr — File Delete`
   - URL : `http://<ip-de-ton-nas>:9999/file-delete`
   - Method : `POST`
   - Cocher uniquement **On Movie File Delete**
2. **Deuxième webhook** (suppressions complètes) :
   - Name : au choix, ex. `Nettoyarr — Item Delete`
   - URL : `http://<ip-de-ton-nas>:9999/item-delete`
   - Method : `POST`
   - Cocher uniquement **On Movie Delete**

Clique sur **Test** avant de sauvegarder — un `200 OK` confirme que
Radarr arrive à joindre le conteneur.

### Dans Sonarr

Même principe, `Settings > Connect > +` → **Webhook** :

1. **Premier webhook** : URL `http://<ip-de-ton-nas>:9999/file-delete`,
   coche uniquement **On Episode File Delete**.
2. **Deuxième webhook** : URL `http://<ip-de-ton-nas>:9999/item-delete`,
   coche uniquement **On Series Delete**.

### Si l'authentification est activée (`AUTH_USER`/`AUTH_PASS`)

Renseigne les mêmes identifiants dans les champs **Username**/**Password**
du webhook, disponibles dans le même formulaire que l'URL — sinon Radarr/
Sonarr recevront un `401` et les webhooks échoueront silencieusement (pas
d'erreur visible ailleurs que dans les logs Radarr/Sonarr eux-mêmes et
dans les logs du conteneur Nettoyarr).

### Vérifier que ça fonctionne

Avant de tester en conditions réelles, passe `LOG_LEVEL=DEBUG` sur
`/config` et regarde les logs du conteneur (`docker logs -f nettoyarr`)
lors d'une suppression de test — tu dois voir le payload JSON brut reçu,
qui confirme que le webhook est bien arrivé et avec quels champs.

## Variables d'environnement

Toutes ces variables ne servent que de **valeurs de départ pour le tout
premier démarrage** (avant que `/app/data/config.json` existe) —
ensuite, `/config` fait autorité et ces lignes n'ont plus d'effet, même
après un `docker-compose down && up -d`.

| Variable           | Défaut                     | Description |
|--------------------|-----------------------------|--------------|
| `DRY_RUN`          | `true`                       | Ne supprime rien réellement, log seulement |
| `QBIT_URL`         | `http://localhost:8081`     | URL de l'interface qBittorrent |
| `QBIT_API_KEY`     | —                            | Clé API (Bearer, qBittorrent >= 5.2.0) — recommandé |
| `QBIT_USER`        | —                            | Fallback : login classique par cookie de session |
| `QBIT_PASS`        | —                            | Fallback : mot de passe |
| `SEERR_URL`        | `http://seerr:5055`         | URL de l'instance Seerr/Overseerr |
| `SEERR_API_KEY`    | —                            | Clé API Seerr |
| `TMDB_API_KEY`     | —                            | Requis uniquement pour la conversion tvdbId → tmdbId (Sonarr) |
| `TAUTULLI_URL`     | —                            | Optionnel : dernier visionnage et bibliothèque Plex dans l'explorateur |
| `TAUTULLI_API_KEY` | —                            | Clé API Tautulli |
| `PROWLARR_URL`     | —                            | Optionnel : noms des trackers dans l'explorateur, d'après les indexeurs |
| `PROWLARR_API_KEY` | —                            | Clé API Prowlarr |
| `TRACKER_NAMES`    | `{}`                         | JSON `{"adresse d'announce": "nom"}`, seulement comme valeur de départ — préfère `/config` |
| `AUTH_USER`        | —                            | Identifiant HTTP Basic Auth — voir section Authentification |
| `AUTH_PASS`        | —                            | Mot de passe HTTP Basic Auth |
| `LOG_LEVEL`        | `INFO`                       | Passer à `DEBUG` pour voir les payloads webhook bruts |
| `RADARR_INSTANCES` | `[]`                         | JSON, seulement comme valeur de départ — préfère `/config` |
| `SONARR_INSTANCES` | `[]`                         | Idem |
| `LINKS_SCAN_ROOT`  | `/media`                     | Dossier indexé pour retrouver les autres hardlinks des fichiers supprimés (monté au même chemin que dans qBittorrent) |
| `LISTEN_PORT`      | `9999`                       | Port d'écoute du webhook listener |

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
