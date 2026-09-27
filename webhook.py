#!/usr/bin/env python3
"""
arr-cleanup — nettoie qBittorrent (+ doublons cross-seed) et Seerr
automatiquement quand Radarr/Sonarr supprime un fichier ou un item.

À configurer dans Radarr/Sonarr (Settings > Connect > Webhook), une entrée
par instance (radarr, radarr-animes, radarr-enfants, sonarr, sonarr-enfants) :

  1) Webhook déclenché sur "On Movie File Delete" (Radarr)
                        / "On Episode File Delete" (Sonarr)
     URL: http://arr-cleanup:9999/file-delete
     → utile pour les remplacements de qualité (upgrade), qui remplacent
       un seul fichier sans supprimer l'item entier.

  2) Webhook déclenché sur "On Movie Delete" (Radarr)
                        / "On Series Delete" (Sonarr)
     URL: http://arr-cleanup:9999/item-delete
     → gère le nettoyage qBit (via movieFolderSize/équivalent) ET Seerr
       pour une suppression complète. C'est celui-ci qui fait le gros du
       travail : Radarr ne déclenche pas "On Movie File Delete" comme
       événement séparé lors d'une suppression complète du film/série,
       seule la taille totale du dossier est disponible via item-delete.

IMPORTANT : démarre d'abord avec DRY_RUN=true et regarde les logs pour
vérifier que le matching qBit et les IDs Seerr correspondent bien à ton
setup avant de passer en DRY_RUN=false. Les noms exacts des champs dans
les payloads webhook peuvent varier légèrement selon la version de
Radarr/Sonarr — ne fais pas confiance aveuglément, vérifie dans les logs
du conteneur.

Passe LOG_LEVEL=DEBUG pour voir le payload JSON brut de chaque webhook
reçu — utile pour vérifier quels champs sont réellement envoyés par ta
version de Radarr/Sonarr avant d'ajuster la logique de matching.

Suppression déclenchée depuis qBittorrent (bouton Tampermonkey / delete-by-hash) :
essaie d'abord un match Radarr (film), puis Sonarr (série) si aucun film ne
correspond. Fournir RADARR_INSTANCES et/ou SONARR_INSTANCES (JSON) :
  RADARR_INSTANCES=[{"url":"http://localhost:7878","api_key":"..."}]
  SONARR_INSTANCES=[{"url":"http://localhost:8989","api_key":"..."}]

Pour les séries, TOUS les fichiers du torrent sont traités (pas juste le
premier) — un pack de saison complet supprime chaque episodeFile
individuellement dans Sonarr. Comportement volontairement prudent :
- Chaque épisode supprimé est aussi passé "unmonitored" (sinon Sonarr peut
  le re-rechercher automatiquement, un risque encore plus insidieux que
  pour Radarr puisqu'il peut se déclencher sans aucune action de ta part).
- Une SAISON n'est passée "unmonitored" que si elle ne contient plus AUCUN
  épisode avec fichier après la suppression (typiquement : tu as supprimé
  le pack complet). Une suppression partielle ne touche jamais la saison.
- La série elle-même n'est jamais désinscrite ni son monitoring modifié,
  quel que soit le nombre d'épisodes supprimés — les saisons futures
  continuent d'être détectées et téléchargées normalement.
- Seerr n'est jamais nettoyé depuis ce chemin (qBit) pour une série :
  supprimer des épisodes ne veut pas forcément dire "je ne veux plus
  cette série". Le nettoyage Seerr reste réservé à une suppression
  complète déclenchée depuis Sonarr lui-même (webhook item-delete).

Suppression déclenchée depuis qBittorrent, ancienne voie par polling
(optionnel, films uniquement, désactivée par défaut) :
qBittorrent n'ayant pas de webhook de suppression, on peut aussi scruter
périodiquement la liste des torrents pour détecter les disparitions, puis
demander à Radarr de supprimer le film correspondant (deleteFiles=true).
Activer avec QBIT_POLL_ENABLED=true (nécessite RADARR_INSTANCES).
Nécessite que /media soit monté en lecture seule dans ce conteneur pour
la vérification de sécurité (le script refuse d'agir si le fichier existe
encore sur le disque, signe que seul le torrent a été retiré sans les
fichiers). Sans ce montage, la vérification est ignorée avec un warning.
"""

import base64
import hmac
import html
import json
import logging
import os
import threading
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "9999"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("arr-cleanup")

# ── Config dynamique (fichier JSON, éditable via /config sans toucher au
# docker-compose) ────────────────────────────────────────────────────────────
# Au premier démarrage, la config est initialisée à partir des variables
# d'environnement (compatibilité avec les setups existants), puis sauvegardée
# dans CONFIG_PATH. Tous les démarrages suivants lisent ce fichier — modifier
# les variables d'environnement dans le compose n'a alors plus d'effet tant
# que le fichier existe. Toute modification faite depuis /config est
# appliquée à chaud (pas besoin de redémarrer le conteneur), et persistée
# sur disque pour survivre à un `docker-compose down && up -d`.
#
# CONFIG_PATH doit pointer vers un fichier sur un volume monté en écriture
# (ex: /app/data/config.json), distinct du montage :ro de webhook.py lui-même.
CONFIG_PATH = os.environ.get("CONFIG_PATH", "/app/data/config.json")
CONFIG_LOCK = threading.Lock()

CONFIG_KEYS = [
    "QBIT_URL", "QBIT_USER", "QBIT_PASS", "QBIT_API_KEY",
    "SEERR_URL", "SEERR_API_KEY", "TMDB_API_KEY",
    "DRY_RUN", "LOG_LEVEL",
    "QBIT_POLL_ENABLED", "QBIT_POLL_INTERVAL", "QBIT_POLL_GRACE_SECONDS",
    "RADARR_INSTANCES", "SONARR_INSTANCES",
    "AUTH_USER", "AUTH_PASS",
]


def _config_from_env():
    """Valeurs par défaut/de repli, lues depuis les variables d'environnement
    — utilisées seulement tant qu'aucun CONFIG_PATH n'existe encore."""
    return {
        "QBIT_URL": os.environ.get("QBIT_URL", "http://localhost:8081"),
        "QBIT_USER": os.environ.get("QBIT_USER", ""),
        "QBIT_PASS": os.environ.get("QBIT_PASS", ""),
        "QBIT_API_KEY": os.environ.get("QBIT_API_KEY", ""),
        "SEERR_URL": os.environ.get("SEERR_URL", "http://seerr:5055"),
        "SEERR_API_KEY": os.environ.get("SEERR_API_KEY", ""),
        "TMDB_API_KEY": os.environ.get("TMDB_API_KEY", ""),
        "DRY_RUN": os.environ.get("DRY_RUN", "true").lower() == "true",
        "LOG_LEVEL": os.environ.get("LOG_LEVEL", "INFO").upper(),
        "QBIT_POLL_ENABLED": os.environ.get("QBIT_POLL_ENABLED", "false").lower() == "true",
        "QBIT_POLL_INTERVAL": int(os.environ.get("QBIT_POLL_INTERVAL", "60")),
        "QBIT_POLL_GRACE_SECONDS": int(os.environ.get("QBIT_POLL_GRACE_SECONDS", "300")),
        "RADARR_INSTANCES": json.loads(os.environ.get("RADARR_INSTANCES", "[]")),
        "SONARR_INSTANCES": json.loads(os.environ.get("SONARR_INSTANCES", "[]")),
        "AUTH_USER": os.environ.get("AUTH_USER", ""),
        "AUTH_PASS": os.environ.get("AUTH_PASS", ""),
    }


def save_config(cfg):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    tmp_path = CONFIG_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    os.replace(tmp_path, CONFIG_PATH)  # écriture atomique


def load_config():
    cfg = _config_from_env()
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                saved = json.load(f)
            cfg.update({k: v for k, v in saved.items() if k in CONFIG_KEYS})
            log.info(f"Config chargée depuis {CONFIG_PATH}")
        except Exception as e:
            log.error(f"Config {CONFIG_PATH} illisible ({e}) — repli sur les variables d'environnement")
    else:
        try:
            save_config(cfg)
            log.info(
                f"Première initialisation : config sauvegardée dans {CONFIG_PATH} "
                "(à partir des variables d'environnement). Elle sera désormais éditable "
                "depuis /config sans toucher au docker-compose."
            )
        except Exception as e:
            log.warning(
                f"Impossible d'écrire {CONFIG_PATH} ({e}) — vérifie que ce chemin est sur "
                "un volume monté en écriture. La config restera basée sur les variables "
                "d'environnement tant que ce n'est pas corrigé."
            )
    return cfg


def apply_config(cfg):
    """Recharge les globals utilisés dans tout le script à partir de cfg.
    Appelé au démarrage, puis après chaque sauvegarde depuis /config — c'est
    ce qui permet une prise en compte à chaud, sans redémarrer le conteneur."""
    global QBIT_URL, QBIT_USER, QBIT_PASS, QBIT_API_KEY
    global SEERR_URL, SEERR_API_KEY, TMDB_API_KEY
    global DRY_RUN, LOG_LEVEL
    global QBIT_POLL_ENABLED, QBIT_POLL_INTERVAL, QBIT_POLL_GRACE_SECONDS
    global RADARR_INSTANCES, SONARR_INSTANCES
    global AUTH_USER, AUTH_PASS
    global qb

    QBIT_URL = cfg["QBIT_URL"]
    QBIT_USER = cfg["QBIT_USER"]
    QBIT_PASS = cfg["QBIT_PASS"]
    QBIT_API_KEY = cfg["QBIT_API_KEY"]
    SEERR_URL = cfg["SEERR_URL"]
    SEERR_API_KEY = cfg["SEERR_API_KEY"]
    TMDB_API_KEY = cfg["TMDB_API_KEY"]
    DRY_RUN = cfg["DRY_RUN"]
    LOG_LEVEL = cfg["LOG_LEVEL"]
    QBIT_POLL_ENABLED = cfg["QBIT_POLL_ENABLED"]
    QBIT_POLL_INTERVAL = cfg["QBIT_POLL_INTERVAL"]
    QBIT_POLL_GRACE_SECONDS = cfg["QBIT_POLL_GRACE_SECONDS"]
    RADARR_INSTANCES = cfg["RADARR_INSTANCES"]
    SONARR_INSTANCES = cfg["SONARR_INSTANCES"]
    AUTH_USER = cfg["AUTH_USER"]
    AUTH_PASS = cfg["AUTH_PASS"]

    logging.getLogger().setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
    qb = QBClient(QBIT_URL, QBIT_USER, QBIT_PASS, QBIT_API_KEY)
    qbit_invalidate_cache()  # l'instance qBit a pu changer : le cache n'est plus fiable


def current_config():
    """Reconstruit un dict cfg à partir des globals actuels — utilisé comme
    base de fusion lors d'une sauvegarde partielle depuis /config, et pour
    pré-remplir le formulaire."""
    return {
        "QBIT_URL": QBIT_URL, "QBIT_USER": QBIT_USER,
        "QBIT_PASS": QBIT_PASS, "QBIT_API_KEY": QBIT_API_KEY,
        "SEERR_URL": SEERR_URL, "SEERR_API_KEY": SEERR_API_KEY,
        "TMDB_API_KEY": TMDB_API_KEY,
        "DRY_RUN": DRY_RUN, "LOG_LEVEL": LOG_LEVEL,
        "QBIT_POLL_ENABLED": QBIT_POLL_ENABLED,
        "QBIT_POLL_INTERVAL": QBIT_POLL_INTERVAL,
        "QBIT_POLL_GRACE_SECONDS": QBIT_POLL_GRACE_SECONDS,
        "RADARR_INSTANCES": RADARR_INSTANCES, "SONARR_INSTANCES": SONARR_INSTANCES,
        "AUTH_USER": AUTH_USER, "AUTH_PASS": AUTH_PASS,
    }


# ── Client qBittorrent ──────────────────────────────────────────────────────
class QBClient:
    """Deux modes d'authentification :
    - api_key fourni (qBittorrent >= 5.2.0) : header Authorization: Bearer,
      stateless, pas de login/cookie nécessaire. Recommandé.
    - sinon : login classique par cookie de session (username/password),
      avec Referer/Origin pour passer la protection CSRF de qBittorrent."""

    def __init__(self, url, user, password, api_key=""):
        self.url = url.rstrip("/")
        self.user = user
        self.password = password
        self.api_key = api_key
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar())
        )
        self._logged_in = False

    def _req(self, path, data=None, method="GET", timeout=15):
        url = f"{self.url}{path}"
        body = urllib.parse.urlencode(data).encode() if data else None
        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("Referer", self.url)
        req.add_header("Origin", self.url)
        if self.api_key:
            req.add_header("Authorization", f"Bearer {self.api_key}")
        r = self._opener.open(req, timeout=timeout)
        return r.read().decode()

    def login(self):
        if self.api_key:
            self._logged_in = True  # rien à faire, la clé API est envoyée à chaque requête
            return
        resp = self._req(
            "/api/v2/auth/login",
            {"username": self.user, "password": self.password},
            method="POST",
        )
        if resp.strip() != "Ok.":
            raise RuntimeError(f"Login qBittorrent échoué: {resp}")
        self._logged_in = True

    def ensure_login(self):
        if not self._logged_in:
            self.login()

    def get_torrents(self):
        self.ensure_login()
        return json.loads(self._req("/api/v2/torrents/info"))

    def get_torrent(self, hash_):
        """Infos d'un torrent précis, ou None s'il n'existe plus dans qBit."""
        self.ensure_login()
        lst = json.loads(self._req(f"/api/v2/torrents/info?hashes={hash_}"))
        return lst[0] if lst else None

    def get_files(self, hash_):
        self.ensure_login()
        try:
            return json.loads(self._req(f"/api/v2/torrents/files?hash={hash_}"))
        except Exception:
            return []

    def delete_torrents(self, hashes):
        if not hashes:
            return
        hashes_param = "|".join(hashes)
        if DRY_RUN:
            log.info(f"[DRY-RUN] Suppression qBit (avec fichiers) : {hashes_param}")
            return
        self.ensure_login()
        self._req(
            "/api/v2/torrents/delete",
            {"hashes": hashes_param, "deleteFiles": "true"},
            method="POST",
        )
        qbit_invalidate_cache(hashes)
        log.info(f"Torrent(s) supprimé(s) dans qBittorrent : {hashes_param}")


qb = None  # instancié par apply_config(), au démarrage puis à chaque sauvegarde /config


# ── Cache des listes de fichiers qBittorrent ────────────────────────────────
# La liste des fichiers d'un torrent est immuable pour un hash donné : on ne
# la demande donc qu'UNE fois à qBittorrent, puis on la garde en mémoire.
# Sans ce cache, chaque recherche par taille faisait un appel
# /api/v2/torrents/files par torrent (~1300 appels, plus de 3 minutes sur
# le NAS), et ce à CHAQUE épisode supprimé d'un pack de saison.
# Le verrou garantit qu'un seul scan tourne à la fois : les autres
# recherches attendent puis réutilisent le cache tout juste rempli.
QBIT_FILES_CACHE = {}          # hash -> [(taille, nom_fichier), ...]
QBIT_FILES_LOCK = threading.Lock()
QBIT_SCAN_PROGRESS = {"done": 0, "total": 0}  # lisible par les jobs en attente


def qbit_invalidate_cache(hashes=None):
    if hashes is None:
        QBIT_FILES_CACHE.clear()
    else:
        for h in hashes:
            QBIT_FILES_CACHE.pop(h, None)


def _qbit_fill_cache(torrents, progress=None):
    """Complète le cache pour les torrents pas encore connus.
    À appeler avec QBIT_FILES_LOCK détenu."""
    todo = [t for t in torrents if t["hash"] not in QBIT_FILES_CACHE]
    QBIT_SCAN_PROGRESS["done"], QBIT_SCAN_PROGRESS["total"] = 0, len(todo)
    try:
        for i, t in enumerate(todo, 1):
            files = qb.get_files(t["hash"])
            # Jamais de liste vide en cache (métadonnées pas encore reçues,
            # erreur API…) : on retentera au prochain scan.
            if files:
                QBIT_FILES_CACHE[t["hash"]] = [
                    (f.get("size"), os.path.basename(f.get("name", ""))) for f in files
                ]
            QBIT_SCAN_PROGRESS["done"] = i
            if progress and (i % 10 == 0 or i == len(todo)):
                progress(i, len(todo))
    finally:
        QBIT_SCAN_PROGRESS["done"] = QBIT_SCAN_PROGRESS["total"] = 0


def qbit_find_torrents_by_sizes(target_sizes, progress=None, wait_cb=None):
    """Renvoie {hash: (nom_torrent, [fichiers correspondants])} pour tous
    les torrents qui contiennent au moins un fichier dont la taille est
    EXACTEMENT l'une de target_sizes (cf. find_and_delete_by_sizes)."""
    targets = {s for s in target_sizes if s}
    if not targets:
        return {}
    min_target = min(targets)

    # Un autre scan est peut-être en cours (préchargement au démarrage,
    # webhook concurrent) : on attend, en remontant sa progression.
    while not QBIT_FILES_LOCK.acquire(timeout=1):
        if wait_cb:
            wait_cb(QBIT_SCAN_PROGRESS["done"], QBIT_SCAN_PROGRESS["total"])
    try:
        torrents = qb.get_torrents()
        live = {t["hash"] for t in torrents}
        for h in [h for h in QBIT_FILES_CACHE if h not in live]:
            QBIT_FILES_CACHE.pop(h, None)
        # Pré-filtre gratuit : un torrent plus petit que le plus petit
        # fichier recherché ne peut pas le contenir.
        candidates = [t for t in torrents if (t.get("total_size") or t.get("size") or 0) >= min_target]
        _qbit_fill_cache(candidates, progress)
    finally:
        QBIT_FILES_LOCK.release()

    matches = {}
    for t in candidates:
        for size, fname in QBIT_FILES_CACHE.get(t["hash"], []):
            if size in targets:
                matches.setdefault(t["hash"], (t.get("name", "?"), []))[1].append(fname)
    return matches


def qbit_cache_warmup():
    """Préchargement au démarrage, en arrière-plan : le premier clic sur le
    bouton qBit n'a alors plus à payer le scan complet."""
    try:
        t0 = time.time()
        with QBIT_FILES_LOCK:
            _qbit_fill_cache(qb.get_torrents())
        log.info(f"Cache qBit préchargé : {len(QBIT_FILES_CACHE)} torrent(s) en {time.time() - t0:.0f}s")
    except Exception as e:
        log.warning(f"Préchargement du cache qBit impossible ({e}) — il se remplira à la première recherche")


# ── Client Radarr (pour le polling qBit uniquement) ─────────────────────────
def radarr_get_movies(instance):
    req = urllib.request.Request(f"{instance['url'].rstrip('/')}/api/v3/movie")
    req.add_header("X-Api-Key", instance["api_key"])
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def radarr_delete_movie(instance, movie_id):
    if DRY_RUN:
        log.info(f"[DRY-RUN] Suppression Radarr movie id={movie_id} sur {instance['url']}")
        return
    url = f"{instance['url'].rstrip('/')}/api/v3/movie/{movie_id}?deleteFiles=true"
    req = urllib.request.Request(url, method="DELETE")
    req.add_header("X-Api-Key", instance["api_key"])
    urllib.request.urlopen(req, timeout=20)
    log.info(f"  Suppression demandée à Radarr : movie id={movie_id} sur {instance['url']}")


def find_movie_by_file_size(size):
    """Cherche, sur toutes les instances Radarr configurées, un film dont
    le fichier importé a exactement cette taille."""
    for instance in RADARR_INSTANCES:
        try:
            movies = radarr_get_movies(instance)
        except Exception as e:
            log.warning(f"  Impossible d'interroger Radarr ({instance['url']}) : {e}")
            continue
        for m in movies:
            mf = m.get("movieFile")
            if mf and mf.get("size") == size:
                return instance, m
    return None


def _noop_step(*_args, **_kwargs):
    pass


def arr_get(instance, path, timeout=20):
    req = urllib.request.Request(f"{instance['url'].rstrip('/')}{path}")
    req.add_header("X-Api-Key", instance["api_key"])
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode()
        return json.loads(raw) if raw.strip() else None


def arr_history_by_download_id(instance, torrent_hash):
    """Historique Radarr/Sonarr lié à ce torrent (downloadId = hash du
    torrent en majuscules). Ne couvre que le torrent réellement grabbé par
    l'*arr : un doublon injecté par cross-seed n'y figure jamais, d'où les
    replis ensuite. Filtré côté client aussi, au cas où une version de
    l'API ignorerait le paramètre downloadId."""
    try:
        data = arr_get(instance, f"/api/v3/history?downloadId={torrent_hash.upper()}&pageSize=50")
    except Exception as e:
        log.debug(f"  Historique indisponible sur {instance['url']} : {e}")
        return []
    records = data.get("records", []) if isinstance(data, dict) else (data or [])
    return [r for r in records if (r.get("downloadId") or "").lower() == torrent_hash.lower()]


def arr_parse_title(instance, title):
    """Demande à Radarr/Sonarr d'identifier un nom de release (/api/v3/parse).
    Renvoie {"movie": ...} ou {"series": ...} si l'item est dans la bibliothèque."""
    try:
        return arr_get(instance, "/api/v3/parse?title=" + urllib.parse.quote(title)) or {}
    except Exception as e:
        log.debug(f"  Parse impossible sur {instance['url']} : {e}")
        return {}


def radarr_find_movie(torrent_hash, torrent_name, sizes, step=_noop_step):
    """Identifie le film correspondant à un torrent, du plus rapide au plus
    lent : historique Radarr (downloadId), puis analyse du nom de release,
    puis parcours complet des bibliothèques en dernier recours.
    Garde-fou inchangé : quel que soit le chemin, le film n'est retenu que si
    la taille de son fichier est EXACTEMENT celle d'un fichier du torrent."""
    size_set = set(sizes)

    def size_matches(movie):
        return (movie.get("movieFile") or {}).get("size") in size_set

    def fetch(instance, movie_id):
        try:
            return arr_get(instance, f"/api/v3/movie/{movie_id}")
        except Exception as e:
            log.debug(f"  Lecture film id={movie_id} impossible sur {instance['url']} : {e}")
            return None

    step("Recherche du film : historique Radarr")
    for instance in RADARR_INSTANCES:
        movie_ids = {r.get("movieId") for r in arr_history_by_download_id(instance, torrent_hash) if r.get("movieId")}
        for movie_id in movie_ids:
            movie = fetch(instance, movie_id)
            if movie and size_matches(movie):
                log.info(f"  Film identifié via l'historique : « {movie.get('title', '?')} » sur {instance['url']}")
                return instance, movie

    if torrent_name:
        step("Recherche du film : analyse du nom de release")
        for instance in RADARR_INSTANCES:
            parsed = (arr_parse_title(instance, torrent_name) or {}).get("movie") or {}
            if parsed.get("id"):
                movie = fetch(instance, parsed["id"])
                if movie and size_matches(movie):
                    log.info(f"  Film identifié via le nom : « {movie.get('title', '?')} » sur {instance['url']}")
                    return instance, movie

    total = len(RADARR_INSTANCES)
    for i, instance in enumerate(RADARR_INSTANCES, 1):
        step("Recherche du film : bibliothèques Radarr complètes", i - 1, total)
        try:
            movies = radarr_get_movies(instance)
        except Exception as e:
            log.warning(f"  Impossible d'interroger Radarr ({instance['url']}) : {e}")
            continue
        for m in movies:
            if size_matches(m):
                log.info(f"  Film identifié via la bibliothèque complète : « {m.get('title', '?')} » sur {instance['url']}")
                return instance, m
    return None


# ── Client Sonarr (séries) ───────────────────────────────────────────────────
def sonarr_get_series(instance):
    req = urllib.request.Request(f"{instance['url'].rstrip('/')}/api/v3/series")
    req.add_header("X-Api-Key", instance["api_key"])
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def sonarr_get_series_detail(instance, series_id):
    req = urllib.request.Request(f"{instance['url'].rstrip('/')}/api/v3/series/{series_id}")
    req.add_header("X-Api-Key", instance["api_key"])
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def sonarr_get_episodes(instance, series_id):
    req = urllib.request.Request(f"{instance['url'].rstrip('/')}/api/v3/episode?seriesId={series_id}")
    req.add_header("X-Api-Key", instance["api_key"])
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def sonarr_get_episode_files(instance, series_id):
    req = urllib.request.Request(f"{instance['url'].rstrip('/')}/api/v3/episodefile?seriesId={series_id}")
    req.add_header("X-Api-Key", instance["api_key"])
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def sonarr_build_file_index(progress=None):
    """Construit un index {taille_octets: (instance, series, episode,
    episodeFile)} pour toutes les instances Sonarr configurées. Un seul
    passage sur toute la bibliothèque, réutilisé ensuite pour chaque
    fichier d'un même torrent — indispensable pour les packs de saison
    (jusqu'à 24 fichiers) sans rescanner Sonarr 24 fois."""
    index = {}
    work = []
    for instance in SONARR_INSTANCES:
        try:
            work.extend((instance, s) for s in sonarr_get_series(instance))
        except Exception as e:
            log.warning(f"  Impossible d'interroger Sonarr ({instance['url']}) : {e}")
    for n, (instance, s) in enumerate(work, 1):
        if progress and (n % 5 == 0 or n == len(work)):
            progress(n, len(work))
        try:
            episodes = sonarr_get_episodes(instance, s["id"])
            files = sonarr_get_episode_files(instance, s["id"])
        except Exception as e:
            log.warning(f"  Impossible de lire les épisodes de « {s.get('title','?')} » : {e}")
            continue
        episodes_by_file_id = {
            e["episodeFileId"]: e for e in episodes if e.get("episodeFileId")
        }
        for ef in files:
            size = ef.get("size")
            episode = episodes_by_file_id.get(ef.get("id"))
            if size and episode:
                index[size] = (instance, s, episode, ef)
    return index


def sonarr_matches_for_series(instance, series, size_set):
    """Épisodes d'UNE série dont le fichier a exactement l'une des tailles."""
    episodes = sonarr_get_episodes(instance, series["id"])
    files = sonarr_get_episode_files(instance, series["id"])
    by_file_id = {e["episodeFileId"]: e for e in episodes if e.get("episodeFileId")}
    out = []
    for ef in files:
        episode = by_file_id.get(ef.get("id"))
        if episode and ef.get("size") in size_set:
            out.append((instance, series, episode, ef))
    return out


def sonarr_find_episodes(torrent_hash, torrent_name, sizes, step=_noop_step):
    """Même logique que radarr_find_movie : historique (downloadId), puis
    analyse du nom de release, puis index complet de toutes les séries en
    dernier recours. Le match final reste toujours la taille exacte."""
    size_set = set(sizes)
    tried = set()

    def try_series(instance, series_id):
        key = (instance["url"], series_id)
        if key in tried:
            return []
        tried.add(key)
        try:
            series = sonarr_get_series_detail(instance, series_id)
            return sonarr_matches_for_series(instance, series, size_set)
        except Exception as e:
            log.warning(f"  Lecture de la série id={series_id} impossible sur {instance['url']} : {e}")
            return []

    step("Recherche des épisodes : historique Sonarr")
    for instance in SONARR_INSTANCES:
        series_ids = {r.get("seriesId") for r in arr_history_by_download_id(instance, torrent_hash) if r.get("seriesId")}
        for series_id in series_ids:
            found = try_series(instance, series_id)
            if found:
                log.info(f"  Série identifiée via l'historique : « {found[0][1].get('title', '?')} » sur {instance['url']}")
                return found

    if torrent_name:
        step("Recherche des épisodes : analyse du nom de release")
        for instance in SONARR_INSTANCES:
            parsed = (arr_parse_title(instance, torrent_name) or {}).get("series") or {}
            if parsed.get("id"):
                found = try_series(instance, parsed["id"])
                if found:
                    log.info(f"  Série identifiée via le nom : « {found[0][1].get('title', '?')} » sur {instance['url']}")
                    return found

    step("Recherche des épisodes : bibliothèques Sonarr complètes")
    index = sonarr_build_file_index(
        progress=lambda i, n: step("Recherche des épisodes : bibliothèques Sonarr complètes", i, n)
    )
    return [index[s] for s in sizes if s in index]


def sonarr_delete_episode_file(instance, episode_file_id):
    if DRY_RUN:
        log.info(f"[DRY-RUN] Suppression Sonarr episodeFile id={episode_file_id} sur {instance['url']}")
        return
    url = f"{instance['url'].rstrip('/')}/api/v3/episodefile/{episode_file_id}"
    req = urllib.request.Request(url, method="DELETE")
    req.add_header("X-Api-Key", instance["api_key"])
    urllib.request.urlopen(req, timeout=20)
    log.info(f"  Suppression demandée à Sonarr : episodeFile id={episode_file_id} sur {instance['url']}")


def sonarr_set_episodes_monitored(instance, episode_ids, monitored):
    if not episode_ids:
        return
    if DRY_RUN:
        log.info(f"[DRY-RUN] Sonarr episode(s) {episode_ids} monitored={monitored} sur {instance['url']}")
        return
    url = f"{instance['url'].rstrip('/')}/api/v3/episode/monitor"
    body = json.dumps({"episodeIds": episode_ids, "monitored": monitored}).encode()
    req = urllib.request.Request(url, data=body, method="PUT")
    req.add_header("X-Api-Key", instance["api_key"])
    req.add_header("Content-Type", "application/json")
    urllib.request.urlopen(req, timeout=20)
    log.info(f"  Episode(s) {episode_ids} passé(s) monitored={monitored} sur {instance['url']}")


def sonarr_maybe_unmonitor_season(instance, series_id, season_number, series_title=""):
    """Si plus aucun épisode de cette saison n'a de fichier (après la
    suppression en cours), bascule la SAISON en unmonitored. Ne touche
    jamais la série elle-même : les autres saisons et les épisodes à
    venir continuent d'être surveillés normalement."""
    try:
        episodes = sonarr_get_episodes(instance, series_id)
    except Exception as e:
        log.warning(f"  Impossible de vérifier la saison {season_number} de « {series_title} » : {e}")
        return

    season_episodes = [e for e in episodes if e.get("seasonNumber") == season_number]
    if not season_episodes or any(e.get("hasFile") for e in season_episodes):
        return  # il reste au moins un fichier dans cette saison, on n'y touche pas

    if DRY_RUN:
        log.info(
            f"[DRY-RUN] Saison {season_number} de « {series_title} » passée "
            f"unmonitored (plus aucun fichier) sur {instance['url']}"
        )
        return

    try:
        series = sonarr_get_series_detail(instance, series_id)
    except Exception as e:
        log.warning(f"  Impossible de récupérer la série id={series_id} : {e}")
        return

    changed = False
    for season in series.get("seasons", []):
        if season.get("seasonNumber") == season_number and season.get("monitored"):
            season["monitored"] = False
            changed = True
    if not changed:
        return

    url = f"{instance['url'].rstrip('/')}/api/v3/series/{series_id}"
    req = urllib.request.Request(url, data=json.dumps(series).encode(), method="PUT")
    req.add_header("X-Api-Key", instance["api_key"])
    req.add_header("Content-Type", "application/json")
    urllib.request.urlopen(req, timeout=20)
    log.info(f"  Saison {season_number} de « {series_title} » passée unmonitored sur {instance['url']}")


def file_still_on_disk(path):
    """Vérifie si le fichier existe encore. Si le point de montage /media
    n'est pas accessible depuis ce conteneur, on ne peut pas vérifier —
    on log un avertissement et on laisse passer plutôt que de bloquer."""
    try:
        return os.path.exists(path)
    except OSError as e:
        log.warning(f"  Impossible de vérifier l'existence de {path} : {e}")
        return False


def handle_qbit_removal(hash_, sizes):
    log.info(f"Torrent disparu de qBit depuis {QBIT_POLL_GRACE_SECONDS}s ({hash_[:8]}), recherche du film correspondant…")
    for size in sizes:
        found = find_movie_by_file_size(size)
        if not found:
            continue
        instance, movie = found
        title = movie.get("title", "?")
        movie_file_path = (movie.get("movieFile") or {}).get("path")

        if movie_file_path and file_still_on_disk(movie_file_path):
            log.warning(
                f"  « {title} » : le fichier existe toujours sur le disque "
                f"({movie_file_path}) — le torrent a peut-être été retiré sans "
                "supprimer les fichiers dans qBit. Rien touché côté Radarr."
            )
            return

        log.info(f"  Correspond à « {title} » sur {instance['url']}")
        radarr_delete_movie(instance, movie["id"])
        return

    log.info("  Aucun film Radarr ne correspond à ce torrent (pas géré par un *arr ?)")


def qbit_poll_loop():
    log.info(
        f"Poll qBit démarré (intervalle={QBIT_POLL_INTERVAL}s, "
        f"grâce={QBIT_POLL_GRACE_SECONDS}s, {len(RADARR_INSTANCES)} instance(s) Radarr)"
    )
    file_size_cache = {}   # hash -> [tailles de fichiers]
    pending_removal = {}   # hash -> timestamp de première absence détectée
    prev_hashes = None

    while True:
        try:
            torrents = qb.get_torrents()
            current_hashes = {t["hash"] for t in torrents}

            if prev_hashes is None:
                log.info(f"Initialisation poll qBit : {len(torrents)} torrent(s) en cache")
                for t in torrents:
                    files = qb.get_files(t["hash"])
                    file_size_cache[t["hash"]] = [f.get("size") for f in files if f.get("size")]
                prev_hashes = current_hashes
                time.sleep(QBIT_POLL_INTERVAL)
                continue

            # Nouveaux torrents → mise en cache de leurs tailles de fichiers
            for h in current_hashes - prev_hashes:
                t = next((x for x in torrents if x["hash"] == h), None)
                if t:
                    files = qb.get_files(h)
                    file_size_cache[h] = [f.get("size") for f in files if f.get("size")]
                pending_removal.pop(h, None)

            now = time.time()
            for h in prev_hashes - current_hashes:
                pending_removal.setdefault(h, now)

            for h in list(pending_removal.keys()):
                if h in current_hashes:
                    pending_removal.pop(h)  # réapparu entre-temps, on annule
                    continue
                if now - pending_removal[h] >= QBIT_POLL_GRACE_SECONDS:
                    handle_qbit_removal(h, file_size_cache.get(h, []))
                    pending_removal.pop(h)
                    file_size_cache.pop(h, None)

            prev_hashes = current_hashes

        except Exception as e:
            log.error(f"Erreur poll qBit : {e}")

        time.sleep(QBIT_POLL_INTERVAL)


def find_and_delete_by_sizes(target_sizes, label="", progress=None, wait_cb=None):
    """Cherche tous les torrents (y compris doublons cross-seed) contenant
    au moins un fichier de taille EXACTEMENT identique à l'une des
    target_sizes, et les supprime. Renvoie {hash: (nom, [fichiers])}.

    On compare fichier par fichier, jamais la taille totale d'un torrent :
    un torrent peut contenir des extras (sample.mkv, nfo) que Radarr/Sonarr
    n'importent jamais dans la bibliothèque média, donc la taille totale
    d'un torrent avec extras ne correspondra jamais à movieFolderSize/
    episodeFile.size qui, eux, ne comptent que le fichier vidéo réellement
    hardlinké. Un hardlink garantit un octet-pour-octet identique sur ce
    fichier précis, peu importe le reste du contenu du torrent."""
    sizes = [s for s in target_sizes if s]
    if not sizes:
        log.warning(f"  Pas de taille exploitable{f' ({label})' if label else ''}, abandon du matching qBit")
        return {}

    matches = qbit_find_torrents_by_sizes(sizes, progress=progress, wait_cb=wait_cb)
    if not matches:
        log.info(f"  Aucun torrent qBit ne correspond{f' à {label}' if label else ''} (tailles : {sizes[:5]})")
        return {}

    for h, (name, fnames) in matches.items():
        log.info(f"  Match trouvé ({len(fnames)} fichier(s), ex. {fnames[0]}) : {name} ({h[:8]})")
    qb.delete_torrents(list(matches))
    return matches


def find_and_delete_by_file_size(target_size, label=""):
    return find_and_delete_by_sizes([target_size], label=label)


# ── Client HTTP générique (arr / Seerr / TMDB) ──────────────────────────────
def http_json(url, headers=None, method="GET", timeout=15):
    req = urllib.request.Request(url, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode()
            return json.loads(raw) if raw.strip() else None
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def seerr_delete_media(tmdb_id, media_type):
    """media_type: 'movie' ou 'tv'"""
    if not SEERR_API_KEY or not tmdb_id:
        log.warning("  SEERR_API_KEY ou tmdb_id manquant, nettoyage Seerr ignoré")
        return

    headers = {"X-Api-Key": SEERR_API_KEY}
    info = http_json(f"{SEERR_URL}/api/v1/{media_type}/{tmdb_id}", headers=headers)
    media_info = (info or {}).get("mediaInfo")
    if not media_info or not media_info.get("id"):
        log.info("  Pas d'entrée Seerr trouvée pour cet item (déjà propre ?)")
        return

    seerr_id = media_info["id"]
    if DRY_RUN:
        log.info(f"[DRY-RUN] Suppression Seerr media id={seerr_id}")
        return
    http_json(f"{SEERR_URL}/api/v1/media/{seerr_id}", headers=headers, method="DELETE")
    log.info(f"  Entrée Seerr supprimée (media id={seerr_id})")


def tvdb_to_tmdb(tvdb_id):
    if not TMDB_API_KEY or not tvdb_id:
        return None
    url = (
        f"https://api.themoviedb.org/3/find/{tvdb_id}"
        f"?api_key={TMDB_API_KEY}&external_source=tvdb_id"
    )
    data = http_json(url)
    results = (data or {}).get("tv_results") or []
    return results[0]["id"] if results else None


# ── Handlers webhook ─────────────────────────────────────────────────────────
def sonarr_find_instance_and_series_by_tvdb(tvdb_id):
    """Identifie sans ambiguïté quelle instance Sonarr gère cette série, en
    comparant le tvdbId (fiable, présent dans les payloads réels) plutôt
    que de tenter une action sur toutes les instances à l'aveugle — les ID
    internes Sonarr (série, épisode) sont propres à chaque instance et
    peuvent coïncider par hasard entre deux instances différentes."""
    if not tvdb_id:
        return None
    for instance in SONARR_INSTANCES:
        try:
            series_list = sonarr_get_series(instance)
        except Exception as e:
            log.warning(f"  Impossible d'interroger Sonarr ({instance['url']}) : {e}")
            continue
        for s in series_list:
            if s.get("tvdbId") == tvdb_id:
                return instance, s
    return None


def handle_file_delete(payload):
    """On Movie File Delete (Radarr) / On Episode File Delete (Sonarr)."""
    movie_file = payload.get("movieFile")
    episode_file = payload.get("episodeFile")
    file_obj = movie_file or episode_file

    if not file_obj:
        log.warning("Payload file-delete sans movieFile/episodeFile, ignoré")
        return

    basename = os.path.basename(file_obj.get("relativePath") or file_obj.get("path") or "")
    size = file_obj.get("size")
    title = (payload.get("movie") or payload.get("series") or {}).get("title", "?")

    log.info(f"Fichier supprimé pour « {title} » : {basename} ({size} octets)")
    find_and_delete_by_file_size(size, label=basename)

    # Épisode Sonarr : même garde-fou que sur le chemin qBit, pour que la
    # protection anti-regrab s'applique peu importe où la suppression a
    # été déclenchée (directement dans Sonarr, ou ailleurs).
    # NON VÉRIFIÉ avec un vrai payload Sonarr à ce stade — noms de champs
    # devinés par analogie avec l'API REST, à confirmer en LOG_LEVEL=DEBUG
    # au premier test réel sur un épisode.
    series = payload.get("series")
    episodes = payload.get("episodes") or ([payload["episode"]] if payload.get("episode") else [])
    if series and episodes and SONARR_INSTANCES:
        found = sonarr_find_instance_and_series_by_tvdb(series.get("tvdbId"))
        if not found:
            log.warning(
                f"  Impossible d'identifier avec certitude quelle instance Sonarr gère "
                f"« {series.get('title','?')} » (tvdbId={series.get('tvdbId')}) — unmonitor ignoré"
            )
        else:
            instance, real_series = found
            series_id = real_series["id"]  # ID interne à CETTE instance, pas celui du payload
            try:
                for ep in episodes:
                    ep_id = ep.get("id")
                    if ep_id is None:
                        continue
                    sonarr_set_episodes_monitored(instance, [ep_id], False)
                season_numbers = {ep.get("seasonNumber") for ep in episodes if ep.get("seasonNumber") is not None}
                for season_number in season_numbers:
                    sonarr_maybe_unmonitor_season(instance, series_id, season_number, series.get("title", "?"))
            except Exception as e:
                log.error(f"  Erreur lors de l'unmonitor Sonarr : {e}")


def handle_item_delete(payload):
    """On Movie Delete (Radarr) / On Series Delete (Sonarr)."""
    movie = payload.get("movie")
    series = payload.get("series")

    if movie:
        title = movie.get("title", "?")
        tmdb_id = movie.get("tmdbId")
        folder_size = payload.get("movieFolderSize")
        log.info(f"Film supprimé de Radarr : « {title} » (tmdbId={tmdb_id})")
        find_and_delete_by_file_size(folder_size, label=title)
        seerr_delete_media(tmdb_id, "movie")
    elif series:
        title = series.get("title", "?")
        tvdb_id = series.get("tvdbId")
        # Nom de champ non confirmé pour Sonarr (pas encore testé en réel) —
        # on tente les variantes les plus probables, à vérifier dans les
        # logs DEBUG lors du premier test réel sur une série.
        folder_size = (
            payload.get("seriesFolderSize")
            or payload.get("folderSize")
            or payload.get("deletedFilesSize")
        )
        log.info(f"Série supprimée de Sonarr : « {title} » (tvdbId={tvdb_id})")
        find_and_delete_by_file_size(folder_size, label=title)
        tmdb_id = series.get("tmdbId") or tvdb_to_tmdb(tvdb_id)
        if tmdb_id:
            seerr_delete_media(tmdb_id, "tv")
        else:
            log.warning("  Pas de tmdbId dans le payload et conversion tvdbId → tmdbId impossible, nettoyage Seerr ignoré")
    else:
        log.warning("Payload item-delete sans movie/series, ignoré")


def render_instance_rows(instances, prefix):
    """Une ligne par instance déjà configurée (URL + clé API dans des champs
    séparés) — plus de JSON à lire ou taper à la main."""
    def esc(v):
        return html.escape(str(v))

    if not isinstance(instances, list):
        instances = []
    rows = []
    for inst in instances:
        rows.append(f'''    <div class="instance-row">
      <input type="text" name="{prefix}_URL[]" placeholder="http://localhost:7878" value="{esc(inst.get('url',''))}">
      <input type="text" name="{prefix}_API_KEY[]" placeholder="clé API" value="{esc(inst.get('api_key',''))}">
      <button type="button" class="remove-btn" onclick="this.parentElement.remove()" title="Retirer cette instance">🗑</button>
    </div>''')
    return "\n".join(rows)


def render_config_page(cfg, saved=False, error=""):
    """Page HTML simple, sans dépendance externe (stdlib uniquement, comme le
    reste du projet) — formulaire unique qui couvre toute la config, plus
    besoin d'éditer le docker-compose.yml pour un réglage courant."""
    def esc(v):
        return html.escape(str(v))

    checked = lambda b: "checked" if b else ""

    banner = ""
    if error:
        banner = f'<div class="banner error">❌ {esc(error)}</div>'
    elif saved:
        banner = '<div class="banner ok">✅ Config sauvegardée et appliquée à chaud — aucun redémarrage du conteneur nécessaire.</div>'

    auth_warning = ""
    if not (cfg["AUTH_USER"] or cfg["AUTH_PASS"]):
        auth_warning = (
            '<div class="banner warn">⚠️ Authentification désactivée — cette page et l\'endpoint '
            '<code>/delete-by-hash</code> sont accessibles sans identifiants à quiconque atteint ce '
            'port. Renseigne AUTH_USER/AUTH_PASS ci-dessous si ce port est exposé au-delà de ton '
            'réseau local.</div>'
        )

    radarr_rows = render_instance_rows(cfg["RADARR_INSTANCES"], "RADARR")
    sonarr_rows = render_instance_rows(cfg["SONARR_INSTANCES"], "SONARR")

    return f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.85em' font-size='90'>🧹</text></svg>">
<title>Nettoyarr — Config</title>
<style>
  body {{ font-family: system-ui, sans-serif; background:#15181c; color:#e8e8e8; max-width:720px; margin:24px auto; padding:0 16px; }}
  h1 {{ font-size:1.4em; }}
  h2 {{ font-size:1.05em; color:#c0392b; border-bottom:1px solid #333; padding-bottom:4px; margin-top:28px; }}
  label {{ display:block; margin-top:12px; font-size:0.9em; color:#aaa; }}
  input[type=text], input[type=number], textarea {{
    width:100%; box-sizing:border-box; padding:8px; margin-top:4px;
    background:#22262b; border:1px solid #3a3f45; border-radius:4px; color:#e8e8e8; font-family:inherit;
  }}
  textarea {{ font-family: ui-monospace, monospace; font-size:0.85em; min-height:90px; }}
  .checkbox-row {{ display:flex; align-items:center; gap:8px; margin-top:12px; }}
  .checkbox-row input {{ width:auto; }}
  .checkbox-row label {{ margin:0; color:#e8e8e8; }}
  small {{ color:#888; display:block; margin-top:2px; }}
  button {{ margin-top:24px; padding:10px 20px; background:#c0392b; color:#fff; border:none; border-radius:6px; font-size:1em; cursor:pointer; }}
  .banner {{ padding:10px 14px; border-radius:6px; margin-bottom:16px; }}
  .banner.ok {{ background:#1e3a24; border:1px solid #2d5a36; }}
  .banner.error {{ background:#3a1e1e; border:1px solid #5a2d2d; }}
  .banner.warn {{ background:#3a2e1e; border:1px solid #5a4a2d; }}
  .banner code {{ background:#22262b; padding:1px 5px; border-radius:3px; }}
  .instance-list {{ margin-top:8px; }}
  .instance-row {{ display:flex; gap:8px; align-items:center; margin-top:8px; }}
  .instance-row input {{ margin-top:0; }}
  .instance-row input:first-child {{ flex:1.3; }}
  .instance-row input:nth-child(2) {{ flex:1; }}
  .remove-btn {{ margin-top:0; padding:8px 12px; background:#3a1e1e; color:#e8e8e8; border:1px solid #5a2d2d; border-radius:4px; font-size:1em; cursor:pointer; flex:none; }}
  .add-btn {{ margin-top:10px; padding:8px 14px; background:#22262b; color:#e8e8e8; border:1px solid #3a3f45; border-radius:4px; font-size:0.9em; cursor:pointer; }}
</style>
</head>
<body>
<h1>🧹 Nettoyarr — Configuration</h1>
{banner}
{auth_warning}
<form method="POST" action="/config">

  <h2>Authentification</h2>
  <label>AUTH_USER <small>(laisser AUTH_USER et AUTH_PASS vides désactive l'authentification)</small>
    <input type="text" name="AUTH_USER" value="{esc(cfg['AUTH_USER'])}"></label>
  <label>AUTH_PASS<input type="text" name="AUTH_PASS" value="{esc(cfg['AUTH_PASS'])}"></label>
  <small>Protège cette page, <code>/delete-by-hash</code>, <code>/file-delete</code> et <code>/item-delete</code>
    (pas <code>/health</code>). Compatible avec les champs Username/Password des webhooks Radarr/Sonarr —
    pense à les renseigner là-bas aussi si tu actives l'auth ici. Pense aussi à mettre à jour
    AUTH_USER/AUTH_PASS dans le userscript qBittorrent.</small>

  <h2>qBittorrent</h2>
  <label>QBIT_URL<input type="text" name="QBIT_URL" value="{esc(cfg['QBIT_URL'])}"></label>
  <label>QBIT_API_KEY <small>(recommandé, qBittorrent ≥ 5.2.0 — laisse vide pour utiliser user/pass)</small>
    <input type="text" name="QBIT_API_KEY" value="{esc(cfg['QBIT_API_KEY'])}"></label>
  <label>QBIT_USER <small>(repli si pas de clé API)</small><input type="text" name="QBIT_USER" value="{esc(cfg['QBIT_USER'])}"></label>
  <label>QBIT_PASS<input type="text" name="QBIT_PASS" value="{esc(cfg['QBIT_PASS'])}"></label>

  <h2>Seerr</h2>
  <label>SEERR_URL<input type="text" name="SEERR_URL" value="{esc(cfg['SEERR_URL'])}"></label>
  <label>SEERR_API_KEY<input type="text" name="SEERR_API_KEY" value="{esc(cfg['SEERR_API_KEY'])}"></label>

  <h2>TMDB</h2>
  <label>TMDB_API_KEY <small>(requis seulement pour la conversion tvdbId → tmdbId des séries)</small>
    <input type="text" name="TMDB_API_KEY" value="{esc(cfg['TMDB_API_KEY'])}"></label>

  <h2>Comportement</h2>
  <div class="checkbox-row">
    <input type="checkbox" id="DRY_RUN" name="DRY_RUN" {checked(cfg['DRY_RUN'])}>
    <label for="DRY_RUN">DRY_RUN — ne rien supprimer réellement, log seulement</label>
  </div>
  <label>LOG_LEVEL
    <select name="LOG_LEVEL" style="width:100%;padding:8px;margin-top:4px;background:#22262b;border:1px solid #3a3f45;border-radius:4px;color:#e8e8e8;">
      {"".join(f'<option value="{lvl}" {"selected" if cfg["LOG_LEVEL"]==lvl else ""}>{lvl}</option>' for lvl in ("DEBUG","INFO","WARNING","ERROR"))}
    </select>
  </label>

  <h2>Poll qBittorrent (optionnel, films uniquement)</h2>
  <div class="checkbox-row">
    <input type="checkbox" id="QBIT_POLL_ENABLED" name="QBIT_POLL_ENABLED" {checked(cfg['QBIT_POLL_ENABLED'])}>
    <label for="QBIT_POLL_ENABLED">QBIT_POLL_ENABLED</label>
  </div>
  <label>QBIT_POLL_INTERVAL (secondes)<input type="number" name="QBIT_POLL_INTERVAL" value="{esc(cfg['QBIT_POLL_INTERVAL'])}"></label>
  <label>QBIT_POLL_GRACE_SECONDS<input type="number" name="QBIT_POLL_GRACE_SECONDS" value="{esc(cfg['QBIT_POLL_GRACE_SECONDS'])}"></label>

  <h2>Instances Radarr</h2>
  <div id="RADARR-instances" class="instance-list">
{radarr_rows}
  </div>
  <button type="button" class="add-btn" onclick="addInstance('RADARR')">+ Ajouter une instance Radarr</button>

  <h2>Instances Sonarr</h2>
  <div id="SONARR-instances" class="instance-list">
{sonarr_rows}
  </div>
  <button type="button" class="add-btn" onclick="addInstance('SONARR')">+ Ajouter une instance Sonarr</button>

  <br><button type="submit">💾 Sauvegarder et appliquer</button>
</form>
<script>
function addInstance(prefix) {{
  const container = document.getElementById(prefix + '-instances');
  const row = document.createElement('div');
  row.className = 'instance-row';

  const urlInput = document.createElement('input');
  urlInput.type = 'text';
  urlInput.name = prefix + '_URL[]';
  urlInput.placeholder = 'http://localhost:7878';

  const keyInput = document.createElement('input');
  keyInput.type = 'text';
  keyInput.name = prefix + '_API_KEY[]';
  keyInput.placeholder = 'clé API';

  const delBtn = document.createElement('button');
  delBtn.type = 'button';
  delBtn.className = 'remove-btn';
  delBtn.title = 'Retirer cette instance';
  delBtn.textContent = '🗑';
  delBtn.onclick = function () {{ row.remove(); }};

  row.appendChild(urlInput);
  row.appendChild(keyInput);
  row.appendChild(delBtn);
  container.appendChild(row);
  urlInput.focus();
}}
</script>
</body>
</html>"""


# ── Authentification HTTP Basic ─────────────────────────────────────────────
# Protège tout sauf /health (aucune donnée sensible, utile pour un healthcheck
# Docker simple sans identifiants). Désactivée tant qu'AUTH_USER/AUTH_PASS
# sont vides (comportement par défaut, pour ne pas casser les setups
# existants) — mais un bandeau d'avertissement s'affiche alors sur /config
# pour que ça ne passe pas inaperçu. Compatible nativement avec les champs
# Username/Password des webhooks Radarr/Sonarr (Settings > Connect), et avec
# GM_xmlhttpRequest côté userscript (en-tête Authorization envoyé directement,
# sans déclencher la popup de login native du navigateur).
def auth_enabled():
    return bool(AUTH_USER or AUTH_PASS)


def check_auth(handler):
    """True si la requête est autorisée à continuer. Sinon, envoie déjà la
    réponse 401 elle-même (avec WWW-Authenticate pour déclencher la popup de
    login du navigateur en cas d'accès direct à /config) et renvoie False —
    l'appelant doit alors simplement `return`."""
    if not auth_enabled():
        return True

    header = handler.headers.get("Authorization", "")
    if header.startswith("Basic "):
        try:
            decoded = base64.b64decode(header[6:]).decode("utf-8")
            user, _, password = decoded.partition(":")
        except Exception:
            user, password = "", ""
        # Comparaison à temps constant : évite qu'un attaquant déduise les
        # identifiants corrects octet par octet en mesurant le temps de
        # réponse (timing attack) sur un simple ==.
        if hmac.compare_digest(user, AUTH_USER) and hmac.compare_digest(password, AUTH_PASS):
            return True

    body = b"Authentification requise"
    handler.send_response(401)
    handler.send_header("WWW-Authenticate", 'Basic realm="Nettoyarr"')
    handler.send_header("Content-Type", "text/plain; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)
    return False


# ── Suppression depuis qBittorrent (bouton userscript) : jobs suivis ────────
# /delete-by-hash?async=1 lance le nettoyage en arrière-plan et répond
# immédiatement avec un job_id ; le userscript interroge ensuite /job?id=…
# chaque seconde pour afficher l'étape en cours et une vraie progression.
# Sans async=1, la requête attend la fin et renvoie le résultat directement
# (comportement historique, pratique pour curl).
# Les jobs sont exécutés UN PAR UN (DELETE_LOCK) : deux torrents d'une même
# saison sélectionnés ensemble ne se marchent plus dessus.
JOBS = {}
JOBS_LOCK = threading.Lock()
DELETE_LOCK = threading.Lock()
JOB_TTL_SECONDS = 3600


class Job:
    def __init__(self, torrent_hash):
        self.id = uuid.uuid4().hex[:12]
        self.hash = torrent_hash
        self.created = self.updated = time.time()
        self.state = "queued"  # queued → running → done
        self.step = "En attente…"
        self.current = None
        self.total = None
        self.http_status = None
        self.result = None
        self.done_event = threading.Event()

    def update(self, step, current=None, total=None):
        self.step, self.current, self.total = step, current, total
        self.updated = time.time()

    def finish(self, http_status, result):
        self.http_status, self.result = http_status, result
        self.state = "done"
        self.updated = time.time()
        self.done_event.set()

    def to_dict(self):
        now = time.time()
        percent = None
        if self.total:
            percent = max(0, min(100, round(100 * (self.current or 0) / self.total)))
        return {
            "ok": True, "id": self.id, "hash": self.hash, "state": self.state,
            "step": self.step, "current": self.current, "total": self.total,
            "percent": percent,
            "elapsed": round(now - self.created),
            "idle": round(now - self.updated),  # secondes sans nouvelle progression
            "result": self.result,
        }


def create_delete_job(torrent_hash):
    job = Job(torrent_hash)
    with JOBS_LOCK:
        now = time.time()
        for jid in [j for j, x in JOBS.items() if x.state == "done" and now - x.updated > JOB_TTL_SECONDS]:
            JOBS.pop(jid, None)
        JOBS[job.id] = job
    threading.Thread(target=run_delete_job, args=(job,), daemon=True).start()
    return job


def run_delete_job(job):
    if not DELETE_LOCK.acquire(blocking=False):
        job.update("En attente de la fin du nettoyage précédent…")
        DELETE_LOCK.acquire()
    try:
        job.state = "running"
        code, payload = delete_by_hash(job.hash, job.update)
    except Exception as e:
        log.exception(f"/delete-by-hash ({job.hash[:8]}) : erreur inattendue")
        code, payload = 500, {"ok": False, "error": f"Erreur interne nettoyarr : {e}"}
    finally:
        DELETE_LOCK.release()
    log.info(f"/delete-by-hash ({job.hash[:8]}) : {payload}")
    job.finish(code, payload)


def delete_by_hash(torrent_hash, step=_noop_step):
    """Action immédiate et explicite (clic utilisateur) : pas de délai de
    grâce ni de vérification disque, contrairement au polling.

    Film d'abord (Radarr, un seul fichier suffit), puis séries (Sonarr, TOUS
    les fichiers du torrent, pour couvrir un pack de saison en un clic).
    Les torrents qBit correspondants (doublons cross-seed compris) sont
    supprimés directement ici, sans attendre le webhook de l'*arr — le
    webhook repassera derrière sans rien trouver, ce qui est sans effet.

    Renvoie (code_http, payload_json)."""
    if not RADARR_INSTANCES and not SONARR_INSTANCES:
        return 500, {"ok": False, "error": "Ni RADARR_INSTANCES ni SONARR_INSTANCES ne sont configurés côté nettoyarr"}

    step("Lecture du torrent dans qBittorrent")
    try:
        torrent = qb.get_torrent(torrent_hash)
    except Exception as e:
        return 500, {"ok": False, "error": f"qBittorrent injoignable : {e}"}
    if not torrent:
        # Cas typique : doublon cross-seed d'un torrent traité juste avant
        # dans la même sélection, déjà supprimé par ce nettoyage.
        return 200, {"ok": True, "kind": "gone",
                     "message": "déjà absent de qBittorrent (doublon nettoyé juste avant ?)"}

    torrent_name = torrent.get("name", "")
    cached = QBIT_FILES_CACHE.get(torrent_hash)
    if cached:
        sizes = sorted({size for size, _ in cached if size})
    else:
        sizes = sorted({f.get("size") for f in qb.get_files(torrent_hash) if f.get("size")})
    if not sizes:
        return 404, {"ok": False, "error": "Liste de fichiers vide (métadonnées pas encore reçues ?)"}

    def qbit_progress(i, n):
        step("Nettoyage qBittorrent : analyse des torrents", i, n)

    def qbit_wait(i, n):
        step("Nettoyage qBittorrent : attente d'une analyse en cours", i or None, n or None)

    # 1) Film (Radarr)
    if RADARR_INSTANCES:
        found = radarr_find_movie(torrent_hash, torrent_name, sizes, step)
        if found:
            instance, movie = found
            title = movie.get("title", "?")
            movie_size = (movie.get("movieFile") or {}).get("size")

            def radarr_action():
                step(f"Suppression de « {title} » dans Radarr")
                radarr_delete_movie(instance, movie["id"])

            removed = qbit_cleanup_around([movie_size], title, radarr_action, step, qbit_progress, qbit_wait)
            # Seerr est nettoyé par le webhook "On Movie Delete" que Radarr
            # déclenche suite à cette suppression (voir handle_item_delete).
            return 200, {"ok": True, "kind": "movie", "title": title, "torrents": removed,
                         "targets": ["Radarr", "qBittorrent", "NAS", "Seerr"]}

    # 2) Série (Sonarr)
    if SONARR_INSTANCES:
        matches, seen = [], set()
        for m in sonarr_find_episodes(torrent_hash, torrent_name, sizes, step):
            key = (m[0]["url"], m[3]["id"])
            if key not in seen:
                seen.add(key)
                matches.append(m)

        if matches:
            total = len(matches)
            deleted_titles = []

            def sonarr_action():
                touched_seasons = set()  # (instance_url, series_id, season_number, titre)
                for i, (instance, series, episode, ep_file) in enumerate(matches, 1):
                    step("Suppression des épisodes dans Sonarr", i - 1, total)
                    sonarr_delete_episode_file(instance, ep_file["id"])
                    sonarr_set_episodes_monitored(instance, [episode["id"]], False)
                    touched_seasons.add((instance["url"], series["id"], ep_file.get("seasonNumber"), series.get("title", "?")))
                    season_num, ep_num = ep_file.get("seasonNumber"), episode.get("episodeNumber")
                    if isinstance(season_num, int) and isinstance(ep_num, int):
                        deleted_titles.append(f"{series.get('title', '?')} S{season_num:02d}E{ep_num:02d}")
                    else:
                        deleted_titles.append(series.get("title", "?"))
                step("Suppression des épisodes dans Sonarr", total, total)

                step("Vérification des saisons dans Sonarr")
                instances_by_url = {inst["url"]: inst for inst in SONARR_INSTANCES}
                for url, series_id, season_number, series_title in touched_seasons:
                    sonarr_maybe_unmonitor_season(instances_by_url[url], series_id, season_number, series_title)

            removed = qbit_cleanup_around(
                [m[3].get("size") for m in matches], matches[0][1].get("title", ""),
                sonarr_action, step, qbit_progress, qbit_wait,
            )

            summary = ", ".join(deleted_titles[:5]) + (f" (+{len(deleted_titles) - 5} autres)" if len(deleted_titles) > 5 else "")
            # Seerr volontairement PAS nettoyé ici (voir docstring en tête).
            return 200, {"ok": True, "kind": "episodes", "count": total, "summary": summary,
                         "torrents": removed, "targets": ["Sonarr", "qBittorrent", "NAS"]}

    return 404, {"ok": False, "error": "Aucun film ou épisode Radarr/Sonarr ne correspond à ce torrent"}


def qbit_cleanup_around(sizes, label, arr_action, step, qbit_progress, qbit_wait):
    """Repère les torrents qBit correspondants AVANT l'action côté *arr,
    puis les supprime APRÈS. Le repérage préalable (instantané grâce au
    cache) donne un décompte fiable : sinon le webhook que l'*arr déclenche
    pendant la suppression nettoie qBit en premier, et le job ne voyait plus
    rien (« 0 torrent » alors que tout avait bien été supprimé).
    Renvoie le nombre total de torrents supprimés."""
    step("Nettoyage qBittorrent : repérage des torrents")
    found = qbit_find_torrents_by_sizes(sizes, progress=qbit_progress, wait_cb=qbit_wait)
    arr_action()
    step("Nettoyage qBittorrent")
    if found:
        for h, (name, fnames) in found.items():
            log.info(f"  Match trouvé ({len(fnames)} fichier(s), ex. {fnames[0]}) : {name} ({h[:8]})")
        qb.delete_torrents(list(found))  # sans effet sur ceux déjà retirés par le webhook
    # Filet de sécurité : torrent ajouté entre-temps avec les mêmes fichiers
    extra = find_and_delete_by_sizes(sizes, label=label, progress=qbit_progress, wait_cb=qbit_wait)
    return len(set(found) | set(extra))


def run_webhook(handler_func, payload, path):
    try:
        handler_func(payload)
    except Exception:
        log.exception(f"Erreur traitement webhook {path}")


# ── Serveur HTTP ─────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # on utilise notre propre logger

    def do_GET(self):
        if self.path.startswith("/health"):
            try:
                torrents = qb.get_torrents()
                msg = f"OK — connecté à qBittorrent, {len(torrents)} torrent(s) trouvé(s)"
                log.info(f"/health : {msg}")
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(msg.encode())
            except Exception as e:
                msg = f"ERREUR — connexion qBittorrent impossible : {e}"
                log.error(f"/health : {msg}")
                self.send_response(500)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(msg.encode())
        elif self.path.startswith("/delete-by-hash"):
            if not check_auth(self):
                return
            self._handle_delete_by_hash()
        elif self.path.startswith("/job"):
            if not check_auth(self):
                return
            self._handle_job()
        elif self.path.startswith("/config"):
            if not check_auth(self):
                return
            self._handle_config_get()
        else:
            self.send_response(404)
            self.end_headers()

    def _send_json(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_delete_by_hash(self):
        """Appelé par le menu contextuel qBittorrent (userscript).
        ?async=1 : répond tout de suite {"ok": true, "job_id": …}, progression
        à suivre sur /job?id=…. Sinon : attend la fin et renvoie le résultat
        final (JSON {"ok": true/false, ...}), comme avant."""
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        torrent_hash = (params.get("hash") or [""])[0].strip().lower()
        async_mode = (params.get("async") or ["0"])[0] == "1"

        if not torrent_hash:
            self._send_json(400, {"ok": False, "error": "Paramètre 'hash' manquant"})
            return

        job = create_delete_job(torrent_hash)
        if async_mode:
            self._send_json(202, {"ok": True, "job_id": job.id})
            return
        job.done_event.wait()
        self._send_json(job.http_status, job.result)

    def _handle_job(self):
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        job = JOBS.get((params.get("id") or [""])[0])
        if not job:
            self._send_json(404, {"ok": False, "error": "Job inconnu (expiré, ou conteneur redémarré entre-temps)"})
            return
        self._send_json(200, job.to_dict())

    def _handle_config_get(self):
        qs = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(qs)
        saved = params.get("saved", ["0"])[0] == "1"
        error = (params.get("error") or [""])[0]
        self._send_config_page(current_config(), saved=saved, error=error)

    def _send_config_page(self, cfg, saved=False, error=""):
        body = render_config_page(cfg, saved=saved, error=error).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_config_post(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""
        params = urllib.parse.parse_qs(raw.decode("utf-8"))

        def field(name, default=""):
            return (params.get(name) or [default])[0]

        new_cfg = dict(current_config())  # base : config actuelle, on ne modifie que le formulaire soumis
        new_cfg["AUTH_USER"] = field("AUTH_USER").strip()
        new_cfg["AUTH_PASS"] = field("AUTH_PASS")
        new_cfg["QBIT_URL"] = field("QBIT_URL").strip()
        new_cfg["QBIT_USER"] = field("QBIT_USER").strip()
        new_cfg["QBIT_PASS"] = field("QBIT_PASS")
        new_cfg["QBIT_API_KEY"] = field("QBIT_API_KEY").strip()
        new_cfg["SEERR_URL"] = field("SEERR_URL").strip()
        new_cfg["SEERR_API_KEY"] = field("SEERR_API_KEY").strip()
        new_cfg["TMDB_API_KEY"] = field("TMDB_API_KEY").strip()
        # Cases à cocher : absentes du POST si décochées, donc leur présence
        # dans params (peu importe la valeur) veut dire "coché".
        new_cfg["DRY_RUN"] = "DRY_RUN" in params
        new_cfg["QBIT_POLL_ENABLED"] = "QBIT_POLL_ENABLED" in params

        log_level = field("LOG_LEVEL", new_cfg["LOG_LEVEL"]).upper()
        if log_level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
            self._send_config_page(new_cfg, error="LOG_LEVEL invalide (attendu DEBUG/INFO/WARNING/ERROR)")
            return
        new_cfg["LOG_LEVEL"] = log_level

        try:
            new_cfg["QBIT_POLL_INTERVAL"] = int(field("QBIT_POLL_INTERVAL", str(new_cfg["QBIT_POLL_INTERVAL"])))
            new_cfg["QBIT_POLL_GRACE_SECONDS"] = int(field("QBIT_POLL_GRACE_SECONDS", str(new_cfg["QBIT_POLL_GRACE_SECONDS"])))
        except ValueError:
            self._send_config_page(new_cfg, error="QBIT_POLL_INTERVAL et QBIT_POLL_GRACE_SECONDS doivent être des nombres entiers")
            return

        def parse_instances(prefix):
            """Reconstruit la liste [{"url":..., "api_key":...}] depuis les
            champs répétés PREFIX_URL[] / PREFIX_API_KEY[] du formulaire —
            une ligne vide (url ET clé vides) est simplement ignorée."""
            urls = params.get(f"{prefix}_URL[]", [])
            keys = params.get(f"{prefix}_API_KEY[]", [])
            instances = []
            for u, k in zip(urls, keys):
                u, k = u.strip(), k.strip()
                if not u and not k:
                    continue
                instances.append({"url": u, "api_key": k})
            return instances

        new_cfg["RADARR_INSTANCES"] = parse_instances("RADARR")
        new_cfg["SONARR_INSTANCES"] = parse_instances("SONARR")

        with CONFIG_LOCK:
            try:
                save_config(new_cfg)
            except Exception as e:
                self._send_config_page(new_cfg, error=f"Échec de la sauvegarde sur disque : {e}")
                return
            apply_config(new_cfg)

        log.info("Config mise à jour depuis /config (appliquée à chaud, sans redémarrage)")
        self.send_response(303)
        self.send_header("Location", "/config?saved=1")
        self.end_headers()

    def do_POST(self):
        if self.path.startswith("/config"):
            if not check_auth(self):
                return
            self._handle_config_post()
            return

        if (self.path.startswith("/file-delete") or self.path.startswith("/item-delete")) and not check_auth(self):
            return

        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode())
        except Exception:
            payload = {}

        log.debug(f"Payload reçu sur {self.path} : {json.dumps(payload)[:2000]}")

        if self.path.startswith("/file-delete"):
            handler_func = handle_file_delete
        elif self.path.startswith("/item-delete"):
            handler_func = handle_item_delete
        else:
            self.send_response(404)
            self.end_headers()
            return

        # Réponse IMMÉDIATE, traitement en arrière-plan : Radarr/Sonarr
        # attendent la réponse du webhook, et un traitement long (scan qBit)
        # les bloquait — un pack de saison supprimé épisode par épisode
        # prenait alors ~40 s par épisode.
        self.send_response(200)
        self.end_headers()
        threading.Thread(target=run_webhook, args=(handler_func, payload, self.path), daemon=True).start()


if __name__ == "__main__":
    apply_config(load_config())
    log.info(f"Démarrage — DRY_RUN={DRY_RUN}")
    threading.Thread(target=qbit_cache_warmup, daemon=True).start()

    if QBIT_POLL_ENABLED:
        if not RADARR_INSTANCES:
            log.warning("QBIT_POLL_ENABLED=true mais RADARR_INSTANCES est vide, le poll ne trouvera jamais rien")
        threading.Thread(target=qbit_poll_loop, daemon=True).start()
    else:
        log.info("Poll qBit désactivé (QBIT_POLL_ENABLED=false)")

    server = ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), Handler)
    log.info(f"En écoute sur :{LISTEN_PORT} (/health, /config, /delete-by-hash, /job, /file-delete, /item-delete)")
    server.serve_forever()
