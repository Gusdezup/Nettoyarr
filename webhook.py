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
Nécessite que /media soit monté dans ce conteneur (en écriture si tu
utilises aussi la suppression des hardlinks d'un torrent orphelin) pour
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
import stat
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
    "LINKS_SCAN_ROOT",
    "TAUTULLI_URL", "TAUTULLI_API_KEY",
    "TRACKER_NAMES",
    "PROWLARR_URL", "PROWLARR_API_KEY",
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
        "LINKS_SCAN_ROOT": os.environ.get("LINKS_SCAN_ROOT", "/media"),
        "TAUTULLI_URL": os.environ.get("TAUTULLI_URL", ""),
        "TAUTULLI_API_KEY": os.environ.get("TAUTULLI_API_KEY", ""),
        "TRACKER_NAMES": json.loads(os.environ.get("TRACKER_NAMES", "{}")),
        "PROWLARR_URL": os.environ.get("PROWLARR_URL", ""),
        "PROWLARR_API_KEY": os.environ.get("PROWLARR_API_KEY", ""),
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
    global LINKS_SCAN_ROOT
    global TAUTULLI_URL, TAUTULLI_API_KEY
    global TRACKER_NAMES
    global PROWLARR_URL, PROWLARR_API_KEY
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
    LINKS_SCAN_ROOT = cfg.get("LINKS_SCAN_ROOT") or "/media"
    old_sources = (globals().get("TAUTULLI_URL"), globals().get("TAUTULLI_API_KEY"),
                   globals().get("RADARR_INSTANCES"), globals().get("SONARR_INSTANCES"))
    TAUTULLI_URL = (cfg.get("TAUTULLI_URL") or "").rstrip("/")
    TAUTULLI_API_KEY = cfg.get("TAUTULLI_API_KEY") or ""
    TRACKER_NAMES = cfg.get("TRACKER_NAMES") if isinstance(cfg.get("TRACKER_NAMES"), dict) else {}
    old_prowlarr = (globals().get("PROWLARR_URL"), globals().get("PROWLARR_API_KEY"))
    PROWLARR_URL = (cfg.get("PROWLARR_URL") or "").rstrip("/")
    PROWLARR_API_KEY = cfg.get("PROWLARR_API_KEY") or ""
    if old_prowlarr != (PROWLARR_URL, PROWLARR_API_KEY) and INVENTORY["data"]:
        # Prowlarr modifié depuis /config : seuls les noms des trackers sont
        # recalculés (quelques secondes), pas tout l'inventaire.
        threading.Thread(target=prowlarr_refresh_inventory_names, daemon=True).start()
    if old_sources != (TAUTULLI_URL, TAUTULLI_API_KEY, RADARR_INSTANCES, SONARR_INSTANCES):
        # Sources de l'explorateur modifiées depuis /config : l'inventaire en
        # cache n'est plus fiable, il sera reconstruit à la prochaine ouverture.
        INVENTORY["data"] = None

    logging.getLogger().setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
    qb = QBClient(QBIT_URL, QBIT_USER, QBIT_PASS, QBIT_API_KEY)
    qbit_invalidate_cache()  # l'instance qBit a pu changer : le cache n'est plus fiable
    if LINKS_INDEX["root"] is not None and LINKS_INDEX["root"] != LINKS_SCAN_ROOT:
        links_index_refresh_async()  # dossier analysé modifié depuis /config


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
        "LINKS_SCAN_ROOT": LINKS_SCAN_ROOT,
        "TAUTULLI_URL": TAUTULLI_URL, "TAUTULLI_API_KEY": TAUTULLI_API_KEY,
        "TRACKER_NAMES": TRACKER_NAMES,
        "PROWLARR_URL": PROWLARR_URL, "PROWLARR_API_KEY": PROWLARR_API_KEY,
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

    def get_trackers(self, hash_):
        self.ensure_login()
        try:
            return json.loads(self._req(f"/api/v2/torrents/trackers?hash={hash_}"))
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


def radarr_find_movie(torrent_hash, torrent_name, sizes, step=_noop_step, errors=None):
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
            if errors is not None:
                errors.append(f"Radarr {instance['url']} : {e}")
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


def sonarr_build_file_index(progress=None, errors=None):
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
            if errors is not None:
                errors.append(f"Sonarr {instance['url']} : {e}")
    for n, (instance, s) in enumerate(work, 1):
        if progress and (n % 5 == 0 or n == len(work)):
            progress(n, len(work))
        try:
            episodes = sonarr_get_episodes(instance, s["id"])
            files = sonarr_get_episode_files(instance, s["id"])
        except Exception as e:
            log.warning(f"  Impossible de lire les épisodes de « {s.get('title','?')} » : {e}")
            if errors is not None:
                errors.append(f"Sonarr {instance['url']} « {s.get('title', '?')} » : {e}")
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


def sonarr_find_episodes(torrent_hash, torrent_name, sizes, step=_noop_step, errors=None):
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
        progress=lambda i, n: step("Recherche des épisodes : bibliothèques Sonarr complètes", i, n),
        errors=errors,
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


# Tailles de fichiers dont un job lancé depuis qBittorrent s'occupe déjà.
# Supprimer N épisodes dans Sonarr déclenche N webhooks "File Delete", et
# chacun relançait une recherche qBit complète — inutile (le job supprime
# lui-même ces torrents) et bloquante (une seule recherche à la fois : le job
# attendait que tous les webhooks soient passés). Les webhooks sautent donc
# la partie qBit pour ces tailles ; le reste de leur travail (Seerr,
# unmonitor) est inchangé.
RESERVED_SIZES = {}  # taille -> nombre de jobs qui la réservent
RESERVED_LOCK = threading.Lock()


def reserve_sizes(sizes):
    with RESERVED_LOCK:
        for s_ in sizes:
            RESERVED_SIZES[s_] = RESERVED_SIZES.get(s_, 0) + 1


def release_sizes(sizes):
    with RESERVED_LOCK:
        for s_ in sizes:
            n = RESERVED_SIZES.get(s_, 0) - 1
            if n > 0:
                RESERVED_SIZES[s_] = n
            else:
                RESERVED_SIZES.pop(s_, None)


def find_and_delete_by_file_size(target_size, label=""):
    """Point d'entrée des webhooks Radarr/Sonarr."""
    with RESERVED_LOCK:
        reserved = target_size in RESERVED_SIZES
    if reserved:
        log.info(f"  Recherche qBit ignorée{f' ({label})' if label else ''} : déjà prise en charge par le nettoyage lancé depuis qBittorrent")
        return {}
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


def tracker_auto_name(host):
    """Nom lisible déduit de l'adresse d'announce : le nom de domaine sans
    sous-domaine ni extension (tracker.hdf.world → « Hdf »). Même règle que
    autoTrackerName() dans la page de l'explorateur, à garder alignées."""
    host = (host or "").strip()
    if not host or host == "?" or host.replace(".", "").isdigit():
        return host or "?"
    parts = host.split(".")
    base = parts[-2] if len(parts) >= 2 else parts[0]
    return base[:1].upper() + base[1:]


def _domain_keys(url_or_host):
    """Clés de rapprochement d'une URL ou d'un hôte, de la plus précise à la
    plus large : hôte exact, domaine (2 derniers niveaux), nom de domaine seul
    (couvre un site qui a changé d'extension)."""
    v = (url_or_host or "").strip()
    host = urllib.parse.urlparse(v).hostname if "://" in v else v.split("/")[0].split(":")[0]
    host = (host or "").lower().strip(".")
    if not host or host.replace(".", "").isdigit():
        return []
    parts = host.split(".")
    keys = [("host", host)]
    if len(parts) >= 2:
        keys += [("domain", ".".join(parts[-2:])), ("label", parts[-2])]
    return keys


def prowlarr_index():
    """{clé de domaine: nom de l'indexeur} d'après Prowlarr (URL du site,
    anciennes URL et baseUrl configurée de chaque indexeur)."""
    indexers = http_json(f"{PROWLARR_URL}/api/v1/indexer",
                         headers={"X-Api-Key": PROWLARR_API_KEY}, timeout=20) or []
    index = {}
    for ix in indexers:
        name = (ix.get("name") or "").strip()
        if not name:
            continue
        urls = list(ix.get("indexerUrls") or []) + list(ix.get("legacyUrls") or [])
        urls += [f["value"] for f in ix.get("fields") or []
                 if f.get("name") == "baseUrl" and isinstance(f.get("value"), str)]
        for u in urls:
            for k in _domain_keys(u):
                index.setdefault(k, name)
    return index


def prowlarr_names_for(hosts, warnings=None):
    """{hôte d'announce: nom de l'indexeur Prowlarr} pour les hôtes reconnus."""
    if not (PROWLARR_URL and PROWLARR_API_KEY):
        return {}
    try:
        index = prowlarr_index()
    except Exception as e:
        log.warning(f"Prowlarr injoignable ({PROWLARR_URL}) : {e} — noms des trackers automatiques")
        if warnings is not None:
            warnings.append(f"Prowlarr injoignable, noms des trackers automatiques : {e}")
        return {}
    out, unknown = {}, []
    for h in sorted(hosts):
        name = next((index[k] for k in _domain_keys(h) if k in index), None)
        if name:
            out[h] = name
        elif h and h != "?":
            unknown.append(h)
    if unknown:
        log.info(f"Prowlarr : aucun indexeur reconnu pour {', '.join(unknown)} (nom automatique)")
        if warnings is not None:
            warnings.append("Prowlarr : aucun indexeur reconnu pour " + ", ".join(unknown[:8])
                            + (" …" if len(unknown) > 8 else "") + " — nom automatique, à renommer dans ⚙️ Configuration si besoin")
    return out


def _inventory_hosts(data):
    return {t["host"] for r in data.get("releases", []) for t in r.get("trackers", []) if t.get("host")}


def prowlarr_refresh_inventory_names():
    data = INVENTORY["data"]
    if data:
        data["prowlarr_names"] = prowlarr_names_for(_inventory_hosts(data))
        log.info(f"Noms Prowlarr recalculés : {len(data['prowlarr_names'])} tracker(s) reconnu(s)")


def render_tracker_rows(names):
    """Une ligne par tracker : ceux déjà renommés, ceux en cours dans
    qBittorrent et ceux du dernier inventaire. Nom laissé vide = nom Prowlarr,
    sinon nom automatique (affiché en grisé)."""
    def esc(v):
        return html.escape(str(v))

    names = names if isinstance(names, dict) else {}
    # Adresses : qBittorrent directement (indépendant de l'explorateur), plus
    # celles de l'inventaire s'il existe (il voit aussi les torrents dont le
    # tracker n'a pas encore été joint). Noms : Prowlarr interrogé à l'instant.
    seen = _inventory_hosts(INVENTORY["data"] or {})
    try:
        seen |= {urllib.parse.urlparse(t.get("tracker") or "").hostname for t in qb.get_torrents()} - {None}
    except Exception as e:
        log.warning(f"/config : liste des trackers qBittorrent indisponible ({e})")
    found = prowlarr_names_for(seen)
    default = lambda h: found.get(h) or tracker_auto_name(h)

    # Adresses que Prowlarr (ou le nom automatique) rattache au même tracker =
    # une seule ligne, et le nom saisi s'applique à toutes. Le regroupement ne
    # dépend JAMAIS des noms saisis à la main : chaque ligne reste corrigeable.
    groups = {}
    for h in set(names) | (seen - {"?"}):
        groups.setdefault(default(h), []).append(h)
    rows = []
    for dflt in sorted(groups, key=str.lower):
        hosts = sorted(groups[dflt])
        manual = next((names[h] for h in hosts if names.get(h)), "")
        src = "Prowlarr" if any(h in found for h in hosts) else "nom automatique"
        label = f"{dflt} ({src}) — {', '.join(hosts)}"
        rows.append(f'''    <div class="instance-row">
      <input type="text" value="{esc(label)}" readonly title="{esc(label)}">
      <input type="text" name="TRACKER_NAME[]" placeholder="{esc(dflt)}" value="{esc(manual)}" title="Laisser vide pour garder « {esc(dflt)} »">
      <input type="hidden" name="TRACKER_HOSTS[]" value="{esc(' '.join(hosts))}">
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
    tracker_rows = render_tracker_rows(cfg.get("TRACKER_NAMES")) or (
        "    <small>Aucun tracker trouvé : qBittorrent injoignable ? (voir les logs)</small>")
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
<p><a href="/explorer" style="color:#e8e8e8">🔎 Ouvrir l'explorateur (choisir quoi supprimer)</a></p>
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

  <h2>Tautulli</h2>
  <label>TAUTULLI_URL <small>(dernière date de visionnage dans l'explorateur — laisser vide pour s'en passer)</small>
    <input type="text" name="TAUTULLI_URL" placeholder="http://localhost:8181" value="{esc(cfg['TAUTULLI_URL'])}"></label>
  <label>TAUTULLI_API_KEY<input type="text" name="TAUTULLI_API_KEY" value="{esc(cfg['TAUTULLI_API_KEY'])}"></label>

  <h2>Prowlarr</h2>
  <label>PROWLARR_URL <small>(noms des trackers dans l'explorateur, d'après les indexeurs — laisser vide pour s'en passer)</small>
    <input type="text" name="PROWLARR_URL" placeholder="http://localhost:9696" value="{esc(cfg['PROWLARR_URL'])}"></label>
  <label>PROWLARR_API_KEY<input type="text" name="PROWLARR_API_KEY" value="{esc(cfg['PROWLARR_API_KEY'])}"></label>

  <h2>Noms des trackers</h2>
  <small>Nom affiché dans l'explorateur pour chaque adresse d'announce. Laisser vide pour garder le nom
    proposé en grisé (Prowlarr s'il reconnaît l'adresse, sinon automatique). Les règles de l'explorateur
    restent liées à l'adresse : renommer n'en casse aucune.</small>
  <div class="instance-list">
{tracker_rows}
  </div>

  <h2>Hardlinks sur le NAS</h2>
  <label>LINKS_SCAN_ROOT <small>(dossier parcouru pour retrouver les autres hardlinks d'un torrent orphelin)</small>
    <input type="text" name="LINKS_SCAN_ROOT" value="{esc(cfg['LINKS_SCAN_ROOT'])}"></label>
  <small>Doit être monté dans le conteneur Nettoyarr <b>au même chemin que dans qBittorrent</b>, en écriture
    (ex. <code>/volume1/media:/media</code>), sinon les chemins remontés par qBittorrent ne correspondent à rien.</small>

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
    def __init__(self, torrent_hash, with_links=True):
        self.id = uuid.uuid4().hex[:12]
        self.hash = torrent_hash
        self.with_links = with_links
        self.created = self.updated = time.time()
        self.state = "queued"  # queued → running → done
        self.step = "En attente…"
        self.current = None
        self.total = None
        self.http_status = None
        self.result = None
        self.pending_links = None  # hardlinks proposés à la suppression (confirmés via /delete-links)
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


def create_delete_job(torrent_hash, with_links=True):
    job = Job(torrent_hash, with_links)
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
        code, payload = delete_by_hash(job.hash, job.update, with_links=job.with_links)
    except Exception as e:
        log.exception(f"/delete-by-hash ({job.hash[:8]}) : erreur inattendue")
        code, payload = 500, {"ok": False, "error": f"Erreur interne nettoyarr : {e}"}
    finally:
        DELETE_LOCK.release()
    if isinstance(payload, dict):
        job.pending_links = payload.pop("_pending_links", None)
    log.info(f"/delete-by-hash ({job.hash[:8]}) : {payload}")
    if code == 200 and isinstance(payload, dict) and payload.get("ok") and not DRY_RUN:
        inventory_forget(job.hash)
    job.finish(code, payload)


def delete_by_hash(torrent_hash, step=_noop_step, with_links=True):
    """Action immédiate et explicite (clic utilisateur) : pas de délai de
    grâce ni de vérification disque, contrairement au polling.

    Film d'abord (Radarr, un seul fichier suffit), puis séries (Sonarr, TOUS
    les fichiers du torrent, pour couvrir un pack de saison en un clic).
    Les torrents qBit correspondants (doublons cross-seed compris) sont
    supprimés directement ici, sans attendre le webhook de l'*arr — le
    webhook repassera derrière sans rien trouver, ce qui est sans effet.

    Si ni Radarr ni Sonarr ne connaissent le torrent (copie cross-seed
    isolée, téléchargement manuel…), il est supprimé quand même dans qBit
    et sur le NAS (voir delete_orphan).

    Dans tous les cas (sauf with_links=False), les AUTRES hardlinks des
    fichiers supprimés sont recherchés ensuite, et ceux qui ne servent à
    aucun torrent restant sont proposés à la suppression, sur confirmation.

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

    arr_errors = []  # *arr injoignable pendant un parcours complet → pas de fallback orphelin

    # 1) Film (Radarr)
    if RADARR_INSTANCES:
        found = radarr_find_movie(torrent_hash, torrent_name, sizes, step, errors=arr_errors)
        if found:
            instance, movie = found
            title = movie.get("title", "?")
            movie_size = (movie.get("movieFile") or {}).get("size")

            def radarr_action():
                step(f"Suppression de « {title} » dans Radarr")
                radarr_delete_movie(instance, movie["id"])

            removed, links = qbit_cleanup_around(
                [movie_size], title, radarr_action, step, qbit_progress, qbit_wait,
                with_links=with_links, arr_paths=[(movie.get("movieFile") or {}).get("path")],
            )
            # Seerr est nettoyé par le webhook "On Movie Delete" que Radarr
            # déclenche suite à cette suppression (voir handle_item_delete).
            return 200, _attach_links({"ok": True, "kind": "movie", "title": title, "torrents": removed,
                                       "targets": ["Radarr", "qBittorrent", "NAS", "Seerr"]}, links)

    # 2) Série (Sonarr)
    if SONARR_INSTANCES:
        matches, seen = [], set()
        for m in sonarr_find_episodes(torrent_hash, torrent_name, sizes, step, errors=arr_errors):
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

            removed, links = qbit_cleanup_around(
                [m[3].get("size") for m in matches], matches[0][1].get("title", ""),
                sonarr_action, step, qbit_progress, qbit_wait,
                with_links=with_links, arr_paths=[m[3].get("path") for m in matches],
            )

            summary = ", ".join(deleted_titles[:5]) + (f" (+{len(deleted_titles) - 5} autres)" if len(deleted_titles) > 5 else "")
            # Seerr volontairement PAS nettoyé ici (voir docstring en tête).
            return 200, _attach_links({"ok": True, "kind": "episodes", "count": total, "summary": summary,
                                       "torrents": removed, "targets": ["Sonarr", "qBittorrent", "NAS"]}, links)

    # 3) Torrent orphelin : géré par aucun *arr (copie cross-seed isolée,
    # téléchargement manuel…). On supprime quand même dans qBit + NAS,
    # SAUF si un *arr n'a pas pu être interrogé : « aucun match » ne
    # voudrait alors rien dire.
    if arr_errors:
        return 503, {"ok": False, "error": (
            "Aucun match, mais un *arr n'a pas pu être interrogé — rien supprimé par sécurité : "
            + " ; ".join(arr_errors[:3]))}
    return delete_orphan(torrent, step, qbit_progress, qbit_wait, with_links=with_links)



# ── Hardlinks restants et torrents orphelins ────────────────────────────────
# Supprimer un torrent (ou un fichier de la bibliothèque) ne libère de
# l'espace que si PLUS AUCUN autre lien ne pointe vers le même inode. Linux ne
# sait pas retrouver les chemins d'un inode sans parcourir le disque : on
# maintient donc un index {(dev, inode): [chemins]} des gros fichiers,
# sauvegardé dans /app/data (disponible dès le démarrage), mis à jour toutes
# les heures en ne relisant que les dossiers modifiés, et reconstruit en
# entier une fois par semaine. À l'usage, l'index sert d'indice et chaque
# chemin est revérifié ; le nombre de liens attendu (st_nlink relu APRÈS la
# suppression, via un descripteur ouvert au préalable) dit si l'index était
# complet. S'il manque des liens, parcours ciblé du disque en secours.
ORPHAN_MIN_FILE_SIZE = 50 * 1024 * 1024  # nfo/srt/sample ignorés pour les doublons et les hardlinks
LINKS_INDEX_UPDATE = 3600            # mise à jour incrémentale (dossiers modifiés seulement)
LINKS_INDEX_FULL = 7 * 24 * 3600     # reconstruction complète de sécurité
LINKS_INDEX_PATH = os.path.join(os.path.dirname(CONFIG_PATH), "links-index.json.gz")

# map : {(dev, inode): [chemins]} de tous les fichiers ≥ 50 Mo, dérivé de dirs.
# dirs : {chemin_dossier: [mtime_ns, [sous-dossiers], [[nom, dev, inode, taille], ...]]}
LINKS_INDEX = {"map": None, "dirs": None, "root": None, "full": 0.0, "updated": 0.0}
LINKS_INDEX_LOCK = threading.Lock()


# #recycle (corbeille Synology) volontairement exclu : ce qui s'y trouve sera
# libéré au vidage de la corbeille, Nettoyarr n'a pas à y toucher.
SKIP_DIR_NAMES = {"@eaDir", "#snapshot", "#recycle"}


def _scan_dir(d, root_dev):
    """Lit UN dossier : sous-dossiers (même volume, hors dossiers Synology)
    et gros fichiers réguliers avec leur inode."""
    subdirs, files = [], []
    with os.scandir(d) as it:
        for e in it:
            try:
                if e.is_dir(follow_symlinks=False):
                    if e.name not in SKIP_DIR_NAMES and e.stat(follow_symlinks=False).st_dev == root_dev:
                        subdirs.append(e.name)
                elif e.is_file(follow_symlinks=False):
                    st = e.stat(follow_symlinks=False)
                    if st.st_size >= ORPHAN_MIN_FILE_SIZE:
                        files.append([e.name, st.st_dev, st.st_ino, st.st_size])
            except OSError:
                continue
    return subdirs, files


def _walk_index(root, old_dirs):
    """Parcours de l'arborescence en réutilisant tout dossier dont la date de
    modification n'a pas changé : l'ajout, la suppression ou le renommage
    d'une entrée modifie le mtime du dossier qui la contient, donc un dossier
    inchangé a exactement les mêmes sous-dossiers et fichiers qu'avant. Seuls
    les dossiers modifiés sont relus ; pour les autres, un simple stat.
    old_dirs vide → reconstruction complète."""
    root_dev = os.stat(root).st_dev
    new_dirs, stack, rescanned = {}, [root], 0
    while stack:
        d = stack.pop()
        try:
            st = os.stat(d)
        except OSError:
            continue
        if st.st_dev != root_dev:
            continue
        old = old_dirs.get(d)
        if old and old[0] == st.st_mtime_ns:
            entry = old
        else:
            try:
                subdirs, files = _scan_dir(d, root_dev)
            except OSError:
                continue
            entry = [st.st_mtime_ns, subdirs, files]
            rescanned += 1
        new_dirs[d] = entry
        stack.extend(os.path.join(d, s) for s in entry[1])
    return new_dirs, rescanned


def _index_map(dirs):
    idx = {}
    for d, (_, _, files) in dirs.items():
        for name, dev, ino, _size in files:
            idx.setdefault((dev, ino), []).append(os.path.join(d, name))
    return idx


def _links_index_save():
    import gzip
    data = {"version": 1, "root": LINKS_INDEX["root"], "full": LINKS_INDEX["full"],
            "updated": LINKS_INDEX["updated"], "dirs": LINKS_INDEX["dirs"]}
    tmp = LINKS_INDEX_PATH + ".tmp"
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, LINKS_INDEX_PATH)


def _links_index_load():
    """Recharge l'index sauvegardé : disponible immédiatement au démarrage.
    Un index un peu ancien reste sûr — chaque chemin est revérifié à l'usage,
    et le compte de liens signale tout lien manquant."""
    import gzip
    try:
        with gzip.open(LINKS_INDEX_PATH, "rt", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return False
    except Exception as e:
        log.warning(f"Index des hardlinks sauvegardé illisible ({e}), il sera reconstruit")
        return False
    if data.get("version") != 1 or data.get("root") != LINKS_SCAN_ROOT:
        return False
    dirs = data.get("dirs") or {}
    LINKS_INDEX.update(dirs=dirs, map=_index_map(dirs), root=data["root"],
                       full=data.get("full", 0.0), updated=data.get("updated", 0.0))
    age = (time.time() - LINKS_INDEX["updated"]) / 3600
    log.info(f"Index des hardlinks rechargé : {len(LINKS_INDEX['map'])} gros fichier(s) indexé(s), "
             f"{len(dirs)} dossier(s), mis à jour il y a {age:.1f} h")
    return True


def links_index_update(full=False):
    """Met l'index à jour : incrémental par défaut (dossiers modifiés
    seulement), complet si demandé, si aucun index n'existe, si le dossier
    analysé a changé, ou si la dernière reconstruction complète date de plus
    de LINKS_INDEX_FULL. Un seul passage à la fois ; un appel pendant un
    passage en cours est ignoré."""
    if not LINKS_INDEX_LOCK.acquire(blocking=False):
        return
    try:
        root = LINKS_SCAN_ROOT
        if not os.path.isdir(root):
            log.info(f"Index des hardlinks : {root} non monté, index désactivé")
            return
        full = (full or LINKS_INDEX["dirs"] is None or LINKS_INDEX["root"] != root
                or time.time() - LINKS_INDEX["full"] > LINKS_INDEX_FULL)
        t0 = time.time()
        dirs, rescanned = _walk_index(root, {} if full else LINKS_INDEX["dirs"])
        now = time.time()
        LINKS_INDEX.update(dirs=dirs, map=_index_map(dirs), root=root, updated=now,
                           full=now if full else LINKS_INDEX["full"])
        kind = "construit" if full else "mis à jour"
        log.info(f"Index des hardlinks {kind} : {len(LINKS_INDEX['map'])} gros fichier(s) indexé(s), "
                 f"{rescanned}/{len(dirs)} dossier(s) relu(s), en {now - t0:.0f}s")
        if full or rescanned:
            try:
                _links_index_save()
            except Exception as e:
                log.warning(f"Sauvegarde de l'index des hardlinks impossible ({e})")
    except Exception as e:
        log.warning(f"Mise à jour de l'index des hardlinks impossible : {e}")
    finally:
        LINKS_INDEX_LOCK.release()


def links_index_refresh_async():
    threading.Thread(target=links_index_update, daemon=True).start()


def links_index_loop():
    _links_index_load()
    while True:
        links_index_update()
        time.sleep(LINKS_INDEX_UPDATE)


def _significant_sizes(sizes):
    """Tailles des « vrais » fichiers (≥ 50 Mo). Repli sur toutes les tailles
    pour un torrent qui n'a que des petits fichiers."""
    sizes = {s for s in sizes if s}
    big = {s for s in sizes if s >= ORPHAN_MIN_FILE_SIZE}
    return big or sizes, bool(big)


def _torrent_file_paths(info, files):
    save = info.get("save_path") or ""
    return [(os.path.normpath(os.path.join(save, f.get("name", ""))), f.get("size")) for f in files]


def links_prepare(delete_hashes, kept_hashes, infos, sizes, known_files=None, extra_own=()):
    """Relevé AVANT suppression. Pour chaque inode des fichiers concernés, on
    garde un descripteur ouvert : il maintient l'inode en vie et permet de
    relire son nombre de liens réel une fois les suppressions faites.
    extra_own : chemins côté *arr (fichier de la bibliothèque), exclus des
    résultats s'ils existent encore (DRY_RUN)."""
    known_files = known_files or {}
    ctx = {"targets": {}, "own": {}, "held": {}, "missing": 0, "checked": 0}

    def add_own(path, st):
        key = (st.st_dev, st.st_ino)
        t = ctx["targets"].get(key)
        if t is None:
            try:
                fd = os.open(path, os.O_RDONLY)
            except OSError:
                fd = None
            t = ctx["targets"][key] = {"size": st.st_size, "nlink0": st.st_nlink, "fd": fd}
        ctx["own"][path] = key

    for h in delete_hashes:
        info = infos.get(h)
        if not info:
            continue
        files = known_files.get(h) or qb.get_files(h)
        for path, size in _torrent_file_paths(info, files):
            if size not in sizes:
                continue
            ctx["checked"] += 1
            try:
                st = os.stat(path)  # suit un éventuel symlink cross-seed jusqu'au vrai fichier
            except OSError:
                ctx["missing"] += 1
                continue
            add_own(path, st)
    for p in extra_own:
        if not p:
            continue
        p = os.path.normpath(p)
        try:
            st = os.stat(p)
        except OSError:
            continue
        if (st.st_dev, st.st_ino) in ctx["targets"]:
            ctx["own"][p] = (st.st_dev, st.st_ino)
    for h in kept_hashes:
        info = infos.get(h)
        if not info:
            continue
        for path, size in _torrent_file_paths(info, qb.get_files(h)):
            if size in sizes:
                ctx["held"][path] = info.get("name", h[:8])
    return ctx


def _links_close(ctx):
    for t in ctx["targets"].values():
        if t.get("fd") is not None:
            try:
                os.close(t["fd"])
            except OSError:
                pass
            t["fd"] = None


def _is_link_of(path, key):
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode) and (st.st_dev, st.st_ino) == key


def links_scan(ctx, step):
    """À appeler APRÈS les suppressions (*arr et qBit). Renvoie le bilan des
    liens restants ; les liens supprimables sont mis de côté (_pending) pour
    une suppression sur confirmation (/delete-links)."""
    try:
        return _links_scan(ctx, step)
    finally:
        _links_close(ctx)


def _links_scan(ctx, step):
    root = LINKS_SCAN_ROOT
    base = {"dry_run": DRY_RUN}
    if ctx["checked"] and ctx["missing"] == ctx["checked"]:
        return {**base, "error": (f"fichiers du torrent introuvables dans le conteneur — monte le dossier "
                                  f"de qBittorrent au même chemin dans Nettoyarr (ex. {root})")}
    targets = ctx["targets"]
    if not targets:
        return base

    # Nombre d'AUTRES liens encore présents, par inode. Les suppressions
    # côté qBit sont asynchrones : on laisse quelques secondes pour que nos
    # propres chemins disparaissent, puis on retire du compte ceux qui
    # existent encore (DRY_RUN, ou qBit en retard).
    step("Vérification du nombre de liens restants (inodes)")
    deadline = time.time() + (0 if DRY_RUN else 10)
    while True:
        alive = [p for p, k in ctx["own"].items() if _is_link_of(p, k)]
        if not alive or time.time() >= deadline:
            break
        time.sleep(0.5)
    own_alive = {}
    for p in alive:
        own_alive[ctx["own"][p]] = own_alive.get(ctx["own"][p], 0) + 1
    for key, t in targets.items():
        nlink = os.fstat(t["fd"]).st_nlink if t["fd"] is not None else t["nlink0"]
        t["expected"] = max(0, nlink - own_alive.get(key, 0))

    total_expected = sum(t["expected"] for t in targets.values())
    log.info(f"  Liens restants après suppression : {total_expected} sur {len(targets)} fichier(s)")
    if total_expected == 0:
        return {**base, "freed": True}
    if not os.path.isdir(root):
        return {**base, "error": f"{root} non monté dans le conteneur, recherche des autres hardlinks impossible"}

    # Recherche via l'index uniquement (instantané), chaque chemin revérifié.
    # Jamais de parcours du disque : un lien absent de l'index est presque
    # toujours dans la corbeille Synology (volontairement ignorée) ou créé
    # depuis la dernière mise à jour ; il est signalé comme non localisé.
    found = {key: set() for key in targets}
    idx = LINKS_INDEX["map"] if LINKS_INDEX["root"] == root else None
    if idx is not None:
        for key in targets:
            for p in idx.get(key, []):
                if p not in ctx["own"] and _is_link_of(p, key):
                    found[key].add(p)

    deletable, held, pending = [], [], {"links": [], "expected": {}, "sizes": {}}
    not_found = 0
    for key, t in targets.items():
        k = f"{key[0]}:{key[1]}"
        pending["expected"][k], pending["sizes"][k] = t["expected"], t["size"]
        not_found += max(0, t["expected"] - len(found[key]))
        for p in sorted(found[key]):
            if p in ctx["held"]:
                held.append({"path": p, "torrent": ctx["held"][p], "size": t["size"]})
            else:
                deletable.append({"path": p, "size": t["size"]})
                pending["links"].append({"path": p, "dev": key[0], "ino": key[1], "size": t["size"]})

    if not_found and idx is not None:
        links_index_refresh_async()  # rattrape un éventuel lien récent pour les prochaines fois

    res = {**base, "remaining": total_expected, "deletable": deletable, "held": held, "_pending": pending}
    if not_found:
        res["not_found"] = not_found
    if idx is None:
        res["index_pending"] = True
    log.info(f"  Hardlinks restants : {len(deletable)} supprimable(s), {len(held)} retenu(s) par qBit"
             + (f", {not_found} non localisé(s)" if not_found else "")
             + (" — index pas encore prêt" if idx is None else ""))
    return res


def _attach_links(payload, links):
    """Ajoute le bilan des hardlinks au résultat, et met de côté les liens
    supprimables pour run_delete_job (jamais renvoyés tels quels au client)."""
    if links is None:
        return payload
    pending = links.pop("_pending", None)
    payload["links"] = links
    if pending and pending["links"]:
        payload["_pending_links"] = pending
    return payload


def delete_orphan(torrent, step, qbit_progress, qbit_wait, with_links=True):
    """Torrent qu'aucun Radarr/Sonarr ne gère : suppression dans qBit (avec
    fichiers) du torrent et de ses doublons cross-seed, puis bilan des autres
    hardlinks (voir links_scan).

    Doublons : un autre torrent n'est supprimé que si TOUS ses gros fichiers
    sont inclus dans celui-ci (copie identique ou sous-ensemble). Un torrent
    plus large (ex. INTEGRALE contenant la saison sélectionnée) est conservé :
    le supprimer effacerait du contenu qui n'a pas été demandé."""
    h0 = torrent["hash"]
    files0 = qb.get_files(h0)
    ref, only_big = _significant_sizes(f.get("size") for f in files0)

    step("Torrent orphelin : recherche des doublons cross-seed")
    matches = qbit_find_torrents_by_sizes(ref, progress=qbit_progress, wait_cb=qbit_wait)
    infos = {t["hash"]: t for t in qb.get_torrents()}

    to_delete, kept = [h0], []
    for h, (name, _) in matches.items():
        if h == h0:
            continue
        sizes_h = {s for s, _ in QBIT_FILES_CACHE.get(h, []) if s}
        if only_big:
            sizes_h = {s for s in sizes_h if s >= ORPHAN_MIN_FILE_SIZE}
        if sizes_h and sizes_h <= ref:
            log.info(f"  Doublon cross-seed : {name} ({h[:8]})")
            to_delete.append(h)
        else:
            log.warning(f"  Chevauchement partiel avec un torrent plus large, conservé : {name} ({h[:8]})")
            kept.append(h)

    ctx = None
    if with_links:
        step("Inventaire des fichiers sur le NAS")
        ctx = links_prepare(to_delete, kept, infos, ref, {h0: files0})
    try:
        step("Suppression dans qBittorrent (avec fichiers)")
        qb.delete_torrents(to_delete)
        links = links_scan(ctx, step) if ctx else None
    finally:
        if ctx:
            _links_close(ctx)

    payload = {
        "ok": True, "kind": "orphan",
        "title": torrent.get("name", "?"),
        "torrents": len(to_delete),
        "kept": [infos.get(h, {}).get("name", h[:8]) for h in kept],
        "targets": ["qBittorrent", "NAS"],
    }
    return 200, _attach_links(payload, links)


def delete_links(pending):
    """Suppression, après confirmation manuelle, des hardlinks proposés.
    Seuls les chemins relevés côté serveur sont acceptés (jamais un chemin
    fourni par le client), et chacun est revérifié : même inode, fichier
    régulier — sinon ignoré."""
    deleted, errors, count = [], [], {}
    for l in pending["links"]:
        path = l["path"]
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            errors.append(f"{path} : déjà absent")
            continue
        except OSError as e:
            errors.append(f"{path} : {e}")
            continue
        if (st.st_dev, st.st_ino) != (l["dev"], l["ino"]) or not stat.S_ISREG(st.st_mode):
            errors.append(f"{path} : fichier modifié depuis l'analyse, ignoré")
            continue
        if DRY_RUN:
            log.info(f"[DRY-RUN] Suppression du hardlink {path}")
        else:
            try:
                os.remove(path)
            except OSError as e:
                errors.append(f"{path} : {e}")
                continue
            log.info(f"  Hardlink supprimé : {path}")
        deleted.append(path)
        k = f"{l['dev']}:{l['ino']}"
        count[k] = count.get(k, 0) + 1
    freed = sum(pending["sizes"][k] for k, n in count.items() if n >= pending["expected"].get(k, 1 << 60))
    return {"ok": not errors, "deleted": deleted, "errors": errors, "freed": freed, "dry_run": DRY_RUN}


def qbit_cleanup_around(sizes, label, arr_action, step, qbit_progress, qbit_wait,
                        with_links=False, arr_paths=()):
    """Repère les torrents qBit correspondants AVANT l'action côté *arr,
    puis les supprime APRÈS. Le repérage préalable (instantané grâce au
    cache) donne un décompte fiable : sinon le webhook que l'*arr déclenche
    pendant la suppression nettoie qBit en premier, et le job ne voyait plus
    rien (« 0 torrent » alors que tout avait bien été supprimé).
    Pendant l'action, les tailles concernées sont réservées : les webhooks
    que l'*arr déclenche en retour n'y relancent pas de recherche qBit.
    Avec with_links, relève aussi les inodes avant suppression pour faire
    ensuite le bilan des autres hardlinks (voir links_scan).
    Renvoie (nombre de torrents supprimés, bilan des hardlinks ou None)."""
    step("Nettoyage qBittorrent : repérage des torrents")
    found = qbit_find_torrents_by_sizes(sizes, progress=qbit_progress, wait_cb=qbit_wait)
    ctx = None
    if with_links and found:
        step("Inventaire des fichiers sur le NAS")
        infos = {t["hash"]: t for t in qb.get_torrents()}
        ctx = links_prepare(list(found), [], infos, {s for s in sizes if s}, extra_own=arr_paths)
    reserved = {s_ for s_ in sizes if s_}
    reserve_sizes(reserved)  # les webhooks déclenchés par arr_action sauteront la recherche qBit
    try:
        arr_action()
        step("Nettoyage qBittorrent")
        if found:
            for h, (name, fnames) in found.items():
                log.info(f"  Match trouvé ({len(fnames)} fichier(s), ex. {fnames[0]}) : {name} ({h[:8]})")
            qb.delete_torrents(list(found))
        # Filet de sécurité : torrent ajouté entre-temps avec les mêmes fichiers
        extra = find_and_delete_by_sizes(sizes, label=label, progress=qbit_progress, wait_cb=qbit_wait)
        release_sizes(reserved)
        reserved = set()
        links = links_scan(ctx, step) if ctx else None
    finally:
        release_sizes(reserved)
        if ctx:
            _links_close(ctx)
    return len(set(found) | set(extra)), links



# ── Explorateur : inventaire des releases et critères de choix ──────────────
# Une « release » = un groupe de torrents qui partagent au moins un gros
# fichier à l'octet près (cross-seed : un torrent par tracker pour le même
# contenu, plus éventuellement un pack qui le contient). L'inventaire est
# calculé en job d'arrière-plan (progression via /job), gardé en mémoire, et
# filtré ensuite côté navigateur — changer un critère est instantané.
# La suppression passe par /delete-by-hash, exactement comme le bouton qBit :
# l'inventaire ne sert qu'à choisir, jamais à décider quoi effacer.
INVENTORY = {"data": None, "job": None}
INVENTORY_LOCK = threading.Lock()
TAUTULLI_TVDB_CACHE = {}  # rating_key Plex d'une série -> tvdbId


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _norm_title(t):
    import unicodedata
    t = unicodedata.normalize("NFKD", str(t or "")).encode("ascii", "ignore").decode().lower()
    return "".join(c for c in t if c.isalnum())


def _iso_ts(v):
    """Date ISO des *arr ("2024-05-01T10:00:00.1234567Z") → timestamp."""
    import datetime
    import re
    if not v:
        return None
    v = re.sub(r"\.\d+", "", str(v)).replace("Z", "+00:00")
    try:
        d = datetime.datetime.fromisoformat(v)
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=datetime.timezone.utc)
    return int(d.timestamp())


def _instance_label(kind, instance):
    port = urllib.parse.urlparse(instance["url"]).port
    return f"{kind} :{port}" if port else kind


def tautulli_api(cmd, timeout=60, **params):
    q = urllib.parse.urlencode({"apikey": TAUTULLI_API_KEY, "cmd": cmd, **params})
    data = http_json(f"{TAUTULLI_URL}/api/v2?{q}", timeout=timeout)
    resp = (data or {}).get("response") or {}
    if resp.get("result") != "success":
        raise RuntimeError(f"Tautulli {cmd} : {resp.get('message') or 'réponse invalide'}")
    return resp.get("data")


def _tautulli_show_tvdb(rating_key):
    if rating_key in TAUTULLI_TVDB_CACHE:
        return TAUTULLI_TVDB_CACHE[rating_key]
    try:
        meta = tautulli_api("get_metadata", rating_key=rating_key) or {}
    except Exception as e:
        log.debug(f"  Tautulli get_metadata {rating_key} : {e}")
        return None
    tvdb = None
    for g in meta.get("guids") or []:  # agent Plex récent : ["tvdb://12345", ...]
        if str(g).startswith("tvdb://"):
            tvdb = _int(str(g)[7:])
    guid = str(meta.get("guid") or "")  # ancien agent : com.plexapp.agents.thetvdb://12345?lang=fr
    if not tvdb and "thetvdb://" in guid:
        tvdb = _int(guid.split("thetvdb://", 1)[1].split("/")[0].split("?")[0])
    if tvdb:
        TAUTULLI_TVDB_CACHE[rating_key] = tvdb
    return tvdb


def _tautulli_history(media_type, step, label):
    """Toutes les lectures d'un type (paginé, gardé léger : 128 Mo de RAM)."""
    start, page, total = 0, 1000, None
    while True:
        data = tautulli_api("get_history", media_type=media_type, start=start, length=page) or {}
        rows = data.get("data") or []
        total = _int(data.get("recordsFiltered")) or 0
        yield from rows
        start += len(rows)
        step(label, min(start, total), total or None)
        if not rows or start >= total:
            return


def tautulli_collect(step):
    """Derniers visionnages, en 0 (présent dans Plex, jamais vu) ou timestamp.
    L'historique Tautulli est relié par rating_key quand l'item existe
    toujours dans Plex, sinon par titre : les rating_key changent quand une
    bibliothèque Plex est recréée, et l'historique d'avant serait perdu."""
    libs = tautulli_api("get_libraries") or []
    movie_libs = [l for l in libs if l.get("section_type") == "movie"]
    show_libs = [l for l in libs if l.get("section_type") == "show"]

    movies_by_size, movies_by_title, size_of_rk = {}, {}, {}
    lib_by_size, lib_by_title, lib_by_tvdb = {}, {}, {}  # -> {noms de bibliothèques Plex}
    for i, lib in enumerate(movie_libs, 1):
        step("Tautulli : bibliothèques de films", i - 1, len(movie_libs))
        name = lib.get("section_name") or f"section {lib['section_id']}"
        data = tautulli_api("get_library_media_info", section_id=lib["section_id"], length=100000) or {}
        for m in data.get("data") or []:
            lp = _int(m.get("last_played")) or 0
            size = _int(m.get("file_size"))
            if size:
                movies_by_size[size] = max(movies_by_size.get(size, 0), lp)
                size_of_rk[str(m.get("rating_key"))] = size
                lib_by_size.setdefault(size, set()).add(name)
            key = (_norm_title(m.get("title")), str(m.get("year") or ""))
            movies_by_title[key] = max(movies_by_title.get(key, 0), lp)
            lib_by_title.setdefault(key, set()).add(name)
    for r in _tautulli_history("movie", step, "Tautulli : historique des films"):
        t = _int(r.get("stopped")) or _int(r.get("date")) or 0
        size = size_of_rk.get(str(r.get("rating_key")))
        if size:
            movies_by_size[size] = max(movies_by_size[size], t)
        else:
            key = (_norm_title(r.get("title")), str(r.get("year") or ""))
            movies_by_title[key] = max(movies_by_title.get(key, 0), t)

    shows = []
    for lib in show_libs:
        name = lib.get("section_name") or f"section {lib['section_id']}"
        data = tautulli_api("get_library_media_info", section_id=lib["section_id"], length=100000) or {}
        shows.extend(dict(sh, _lib=name) for sh in data.get("data") or [])
    tvdb_of_rk, tvdb_of_title = {}, {}
    for i, sh in enumerate(shows, 1):
        step("Tautulli : identification des séries", i - 1, len(shows))
        rk = str(sh.get("rating_key"))
        tvdb = _tautulli_show_tvdb(rk)
        if tvdb:
            tvdb_of_rk[rk] = tvdb
            tvdb_of_title[_norm_title(sh.get("title"))] = tvdb
            lib_by_tvdb.setdefault(tvdb, set()).add(sh["_lib"])

    episodes, unlinked = {}, set()
    for r in _tautulli_history("episode", step, "Tautulli : historique des épisodes"):
        s, e = _int(r.get("parent_media_index")), _int(r.get("media_index"))
        if s is None or e is None:
            continue
        tvdb = (tvdb_of_rk.get(str(r.get("grandparent_rating_key")))
                or tvdb_of_title.get(_norm_title(r.get("grandparent_title"))))
        if not tvdb:
            unlinked.add(r.get("grandparent_title") or "?")
            continue
        t = _int(r.get("stopped")) or _int(r.get("date")) or 0
        k = (tvdb, s, e)
        episodes[k] = max(episodes.get(k, 0), t)

    return {
        "movies_by_size": movies_by_size, "movies_by_title": movies_by_title,
        "episodes": episodes, "shows_in_plex": set(tvdb_of_rk.values()),
        "unlinked_shows": sorted(unlinked),
        "lib_by_size": lib_by_size, "lib_by_title": lib_by_title, "lib_by_tvdb": lib_by_tvdb,
        "libraries": sorted(l.get("section_name") or "" for l in movie_libs + show_libs),
    }


def _torrent_tracker(t):
    """(domaine du tracker, seeders) d'un torrent — un tracker par torrent
    chez Gus ; s'il y en a plusieurs, on garde le moins seedé (prudent)."""
    host = urllib.parse.urlparse(t.get("tracker") or "").hostname
    seeds = _int(t.get("num_complete"))
    if host:
        return host, (seeds if seeds is not None and seeds >= 0 else None)
    best = None  # tracker pas (encore) joint : on regarde la liste détaillée
    for tr in qb.get_trackers(t["hash"]):
        url = tr.get("url") or ""
        if url.startswith("** ["):  # DHT / PeX / LSD
            continue
        h = urllib.parse.urlparse(url).hostname or url
        n = _int(tr.get("num_seeds"))
        n = n if n is not None and n >= 0 else None
        if best is None or (n is not None and (best[1] is None or n < best[1])):
            best = (h, n)
    return best or ("?", None)


def build_inventory(step):
    warnings = []

    step("qBittorrent : liste des torrents")
    torrents = qb.get_torrents()
    tinfo = {t["hash"]: t for t in torrents}
    while not QBIT_FILES_LOCK.acquire(timeout=1):
        step("qBittorrent : attente d'une analyse en cours",
             QBIT_SCAN_PROGRESS["done"] or None, QBIT_SCAN_PROGRESS["total"] or None)
    try:
        for h in [h for h in QBIT_FILES_CACHE if h not in tinfo]:
            QBIT_FILES_CACHE.pop(h, None)
        _qbit_fill_cache(torrents, progress=lambda i, n: step("qBittorrent : analyse des fichiers", i, n))
    finally:
        QBIT_FILES_LOCK.release()

    # Gros fichiers de chaque torrent (les petits ne servent qu'à fusionner
    # à tort des torrents sans rapport : nfo, srt, sample)
    sizes_of, ignored = {}, 0
    for h in tinfo:
        big = {s for s, _ in QBIT_FILES_CACHE.get(h, []) if s and s >= ORPHAN_MIN_FILE_SIZE}
        if big:
            sizes_of[h] = big
        else:
            ignored += 1

    parent = {h: h for h in sizes_of}

    def find(h):
        while parent[h] != h:
            parent[h] = parent[parent[h]]
            h = parent[h]
        return h

    owner = {}
    for h, ss in sizes_of.items():
        for s in ss:
            if s in owner:
                parent[find(h)] = find(owner[s])
            else:
                owner[s] = h
    groups = {}
    for h in sizes_of:
        groups.setdefault(find(h), []).append(h)

    # Radarr : taille de fichier -> film
    radarr_idx = {}
    for i, inst in enumerate(RADARR_INSTANCES, 1):
        step("Radarr : bibliothèques", i - 1, len(RADARR_INSTANCES))
        try:
            for m in radarr_get_movies(inst):
                mf = m.get("movieFile") or {}
                if mf.get("size"):
                    radarr_idx[mf["size"]] = (inst, m, mf)
        except Exception as e:
            warnings.append(f"Radarr {inst['url']} injoignable : {e}")

    # Sonarr : taille de fichier -> épisode (index complet existant)
    sonarr_idx = {}
    if SONARR_INSTANCES:
        errs = []
        sonarr_idx = sonarr_build_file_index(
            progress=lambda i, n: step("Sonarr : bibliothèques", i, n), errors=errs)
        warnings.extend(errs[:5])

    tau = None
    if TAUTULLI_URL and TAUTULLI_API_KEY:
        try:
            tau = tautulli_collect(step)
            if tau["unlinked_shows"]:
                warnings.append("Tautulli : historique non relié à une série Plex actuelle pour "
                                + ", ".join(tau["unlinked_shows"][:8])
                                + (" …" if len(tau["unlinked_shows"]) > 8 else ""))
        except Exception as e:
            warnings.append(f"Tautulli injoignable, visionnages ignorés : {e}")

    step("Regroupement des releases")
    releases = []
    seen_trackers = 0
    for hs in groups.values():
        hs.sort(key=lambda h: (-len(sizes_of[h]), tinfo[h].get("added_on") or 0))
        sizes = set().union(*(sizes_of[h] for h in hs))
        movies = {}
        eps = {}
        for s in sizes:
            if s in radarr_idx:
                inst, m, mf = radarr_idx[s]
                movies[(inst["url"], m["id"])] = (inst, m, mf)
            elif s in sonarr_idx:
                inst, ser, ep, ef = sonarr_idx[s]
                eps[(inst["url"], ef["id"])] = (inst, ser, ep, ef)

        imported, last_played, played_known, libraries = [], [], False, set()
        lib_paths = set()  # dossier racine côté bibliothèque (parent du dossier du film / de la série)
        if movies:
            kind = "movie"
            items = sorted(movies.values(), key=lambda x: x[1].get("title", ""))
            inst, m, _ = items[0]
            title = f"{m.get('title', '?')} ({m.get('year', '?')})"
            if len(items) > 1:
                title += f" +{len(items) - 1} film(s)"
            sub = _instance_label("Radarr", inst)
            for _, m, mf in items:
                imported.append(_iso_ts(mf.get("dateAdded")))
                if m.get("path"):
                    lib_paths.add(os.path.dirname(m["path"].rstrip("/")))
                if tau:
                    # Taille ET titre : l'historique d'avant une recréation de
                    # la bibliothèque Plex n'est relié que par le titre.
                    found = [tau["movies_by_size"].get(mf.get("size"))]
                    found += [tau["movies_by_title"].get((_norm_title(t), str(m.get("year") or "")))
                              for t in (m.get("title"), m.get("originalTitle")) if t]
                    found = [x for x in found if x is not None]
                    libraries |= tau["lib_by_size"].get(mf.get("size"), set())
                    for t in (m.get("title"), m.get("originalTitle")):
                        if t:
                            libraries |= tau["lib_by_title"].get((_norm_title(t), str(m.get("year") or "")), set())
                    if found:
                        played_known = True
                        last_played.append(max(found))
        elif eps:
            kind = "episodes"
            items = list(eps.values())
            by_series = {}
            for inst, ser, ep, ef in items:
                by_series.setdefault((inst["url"], ser["id"]), (inst, ser, []))[2].append(
                    (ef.get("seasonNumber", ep.get("seasonNumber")), ep.get("episodeNumber")))
                imported.append(_iso_ts(ef.get("dateAdded")))
                if ser.get("path"):
                    lib_paths.add(os.path.dirname(ser["path"].rstrip("/")))
                if tau and ser.get("tvdbId") in tau["shows_in_plex"]:
                    played_known = True
                    libraries |= tau["lib_by_tvdb"].get(ser["tvdbId"], set())
                    last_played.append(tau["episodes"].get(
                        (ser["tvdbId"], ep.get("seasonNumber"), ep.get("episodeNumber")), 0))
            inst, ser, numbers = sorted(by_series.values(), key=lambda x: -len(x[2]))[0]
            seasons = sorted({s for s, _ in numbers if isinstance(s, int)})
            if len(numbers) == 1 and seasons and isinstance(numbers[0][1], int):
                where = f"S{seasons[0]:02d}E{numbers[0][1]:02d}"
            elif len(seasons) == 1:
                where = f"S{seasons[0]:02d} ({len(numbers)} ép.)"
            elif seasons:
                where = f"S{seasons[0]:02d}–S{seasons[-1]:02d} ({len(numbers)} ép.)"
            else:
                where = f"{len(numbers)} ép."
            title = f"{ser.get('title', '?')} {where}"
            if len(by_series) > 1:
                title += f" +{len(by_series) - 1} série(s)"
            sub = _instance_label("Sonarr", inst)
        else:
            kind, title, sub = "orphan", tinfo[hs[0]].get("name", "?"), "aucun *arr"
            if tau:  # film ajouté à Plex hors Radarr : reconnu par la taille du fichier
                for s in sizes:
                    if s in tau["movies_by_size"]:
                        played_known = True
                        last_played.append(tau["movies_by_size"][s])
                        libraries |= tau["lib_by_size"].get(s, set())

        imported = [t for t in imported if t]
        imp_src = "arr"
        if not imported:  # orphelin : date d'ajout du plus ancien torrent du groupe
            imported = [tinfo[h].get("added_on") for h in hs if tinfo[h].get("added_on")]
            imp_src = "qbit"

        trackers = []
        for h in hs:
            host, seeds = _torrent_tracker(tinfo[h])
            seen_trackers += 1
            trackers.append({"host": host, "seeds": seeds, "hash": h,
                             "name": tinfo[h].get("name", ""), "state": tinfo[h].get("state", "")})

        releases.append({
            "id": hs[0], "hashes": hs, "kind": kind, "title": title, "sub": sub,
            "name": tinfo[hs[0]].get("name", ""),
            "category": sorted({tinfo[h].get("category") or "" for h in hs} - {""}),
            "size": sum(sizes),
            "trackers": trackers,
            "imported": min(imported) if imported else None, "imported_src": imp_src,
            # None = inconnu (pas dans Plex / pas de Tautulli), 0 = jamais vu
            "last_played": max(last_played) if played_known else None,
            "libraries": sorted(libraries),
            "lib_paths": sorted(lib_paths),
            "qbit_paths": sorted({(tinfo[h].get("save_path") or "").rstrip("/") for h in hs} - {""}),
        })

    if PROWLARR_URL and PROWLARR_API_KEY:
        step("Prowlarr : noms des indexeurs")
    prowlarr_names = prowlarr_names_for({t["host"] for r in releases for t in r["trackers"]}, warnings)

    all_lib = sorted({p for r in releases for p in r["lib_paths"]})
    all_qbit = sorted({p for r in releases for p in r["qbit_paths"]})
    return {
        "releases": releases,
        "prowlarr_names": prowlarr_names,
        "paths": {"lib": all_lib, "qbit": all_qbit},
        "stats": {"torrents": len(tinfo), "ignored_small": ignored, "releases": len(releases),
                  "tautulli": bool(tau), "warnings": warnings,
                  "libraries": tau["libraries"] if tau else []},
    }


def run_inventory_job(job):
    job.state = "running"
    t0 = time.time()
    try:
        data = build_inventory(job.update)
        data["built_at"] = int(time.time())
        data["duration"] = round(time.time() - t0)
        INVENTORY["data"] = data
        log.info(f"Inventaire de l'explorateur : {len(data['releases'])} release(s) en {data['duration']}s"
                 + (f" — avertissements : {data['stats']['warnings']}" if data['stats']['warnings'] else ""))
        job.finish(200, {"ok": True, "releases": len(data["releases"])})
    except Exception as e:
        log.exception("Inventaire de l'explorateur : erreur")
        job.finish(500, {"ok": False, "error": str(e)})


def start_inventory_job():
    with INVENTORY_LOCK:
        job = INVENTORY["job"]
        if job and job.state != "done":
            return job
        job = Job("inventory", with_links=False)
        with JOBS_LOCK:
            JOBS[job.id] = job
        INVENTORY["job"] = job
    threading.Thread(target=run_inventory_job, args=(job,), daemon=True).start()
    return job


def inventory_status():
    job, data = INVENTORY["job"], INVENTORY["data"]
    if job and job.state != "done":
        return {"state": "building", "job_id": job.id}
    if data:
        out = {"state": "ready", "dry_run": DRY_RUN, "tracker_names": TRACKER_NAMES, **data}
        if job and job.result and not job.result.get("ok"):
            out["last_error"] = job.result.get("error")
        return out
    if job and job.result and not job.result.get("ok"):
        return {"state": "error", "error": job.result.get("error")}
    return {"state": "empty"}


EXPLORER_PREFS_PATH = os.path.join(os.path.dirname(CONFIG_PATH), "explorer.json")
EXPLORER_RULES_MAX = 60  # règles par groupe, garde-fou contre un POST démesuré


def explorer_prefs_load():
    try:
        with open(EXPLORER_PREFS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _clean_rules(node, depth=0):
    """Arbre de règles de l'explorateur, nettoyé avant écriture :
    {"match": "all"|"any", "rules": [règle | sous-groupe]}, une règle étant
    {"f": champ, "op": opérateur, "v": valeur}. Un seul niveau de sous-groupe
    (au-delà, ignoré). Le sens des champs n'est connu que de la page : ici,
    on ne garde que la forme."""
    if not isinstance(node, dict):
        return None
    out = {"match": "any" if node.get("match") == "any" else "all", "rules": []}
    for r in (node.get("rules") or [])[:EXPLORER_RULES_MAX]:
        if not isinstance(r, dict):
            continue
        if "rules" in r:
            sub = _clean_rules(r, depth + 1) if depth == 0 else None
            if sub is not None:
                out["rules"].append(sub)
            continue
        v = r.get("v", "")
        if not isinstance(v, (str, int, float, bool)) or len(str(v)) > 200:
            v = ""
        out["rules"].append({"f": str(r.get("f", ""))[:30], "op": str(r.get("op", ""))[:30], "v": v})
    return out


def explorer_prefs_save(prefs):
    """Derniers critères de l'explorateur (arbre de règles), repris par
    défaut à la visite suivante (tout navigateur)."""
    clean = {str(k)[:40]: v for k, v in prefs.items()
             if isinstance(v, (str, bool, int, float)) and len(str(v)) <= 200}
    rules = _clean_rules(prefs.get("rules"))
    if rules is not None:
        clean["rules"] = rules
    tmp = EXPLORER_PREFS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(clean, f, ensure_ascii=False)
    os.replace(tmp, EXPLORER_PREFS_PATH)


def inventory_forget(torrent_hash):
    """Retire de l'inventaire en cache la release d'un torrent supprimé."""
    data = INVENTORY["data"]
    if data:
        data["releases"] = [r for r in data["releases"] if torrent_hash not in r["hashes"]]


# Page de l'explorateur : HTML/JS autonome (stdlib, aucune dépendance), les
# données arrivent par /inventory. Chaîne brute (r"""…""") : les \n du JS
# passent tels quels.
EXPLORER_HTML = r"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.85em' font-size='90'>🧹</text></svg>">
<title>Nettoyarr — Explorateur</title>
<style>
  :root { --bg:#15181c; --panel:#1c2025; --field:#22262b; --line:#3a3f45; --text:#e8e8e8; --muted:#9aa0a6;
          --accent:#c0392b; --keep:#e0a040; --ok:#2d8a4e; }
  * { box-sizing:border-box; }
  body { font-family:system-ui,sans-serif; background:var(--bg); color:var(--text); margin:0; padding:20px 20px 90px; }
  a { color:var(--text); }
  header { display:flex; align-items:baseline; gap:16px; flex-wrap:wrap; }
  h1 { font-size:1.4em; margin:0 0 4px; }
  .dry { background:#3a2e1e; border:1px solid #5a4a2d; padding:2px 8px; border-radius:4px; font-size:.85em; }
  .muted { color:var(--muted); font-size:.85em; }
  .criteria { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:12px 16px 14px; margin:14px 0; }
  .criteria select, .criteria input { padding:6px 7px; background:var(--field); border:1px solid var(--line);
    border-radius:4px; color:var(--text); font:inherit; }
  .rhead { display:flex; align-items:center; gap:8px; flex-wrap:wrap; }
  .rrow { display:flex; gap:8px; align-items:center; flex-wrap:wrap; margin-top:8px; }
  .rrow .f { width:250px; max-width:100%; }
  .rrow .op { width:200px; max-width:100%; }
  .rrow .v { flex:1; min-width:180px; max-width:440px; }
  .rrow.idle .v { border-style:dashed; }
  .rgroup { margin-top:10px; padding:6px 12px 10px; border-left:3px solid var(--keep); background:#191c21; border-radius:0 6px 6px 0; }
  .rtools { display:flex; gap:8px; margin-top:10px; flex-wrap:wrap; }
  .rtools button { padding:5px 10px; font-size:.9em; }
  button.rdel { padding:4px 8px; background:transparent; border-color:transparent; color:var(--muted); }
  button.rdel:hover { color:var(--text); border-color:var(--line); }
  .bar { display:flex; gap:10px; align-items:center; flex-wrap:wrap; margin-bottom:10px; }
  button { padding:7px 14px; background:var(--field); color:var(--text); border:1px solid var(--line); border-radius:6px; cursor:pointer; font:inherit; }
  button.primary { background:var(--accent); border-color:var(--accent); color:#fff; }
  button:disabled { opacity:.45; cursor:default; }
  button:focus-visible, input:focus-visible, select:focus-visible { outline:2px solid var(--keep); outline-offset:1px; }
  #status { min-height:1.3em; }
  .progress { height:6px; background:var(--line); border-radius:3px; overflow:hidden; margin-top:6px; max-width:520px; display:none; }
  .progress > div { height:100%; width:0; background:var(--accent); transition:width .3s; }
  .warn { background:#3a2e1e; border:1px solid #5a4a2d; border-radius:6px; padding:8px 12px; margin:8px 0; font-size:.9em; white-space:pre-line; }
  .tablewrap { overflow-x:auto; border:1px solid var(--line); border-radius:8px; }
  table { width:100%; border-collapse:collapse; font-size:.92em; }
  th { text-align:left; background:var(--panel); position:sticky; top:0; padding:8px; border-bottom:1px solid var(--line);
       cursor:pointer; user-select:none; white-space:nowrap; font-weight:600; }
  th.nosort { cursor:default; }
  td { padding:7px 8px; border-bottom:1px solid #262a30; vertical-align:top; }
  tr:hover td { background:#1a1e23; }
  tr.sel td { background:#2a1d1c; }
  tr.done td { opacity:.4; text-decoration:line-through; }
  .title { font-weight:600; }
  .rname { color:var(--muted); font-size:.82em; overflow-wrap:anywhere; }
  .num { text-align:right; white-space:nowrap; font-variant-numeric:tabular-nums; }
  .kind { font-size:.8em; padding:1px 6px; border-radius:3px; background:var(--field); border:1px solid var(--line); white-space:nowrap; }
  .kind.orphan { border-color:#5a4a2d; }
  .chip { display:inline-block; margin:1px 4px 1px 0; padding:1px 7px; border-radius:10px; font-size:.82em;
          background:var(--field); border:1px solid var(--line); white-space:nowrap; font-variant-numeric:tabular-nums; }
  .chip.keep { border-color:var(--keep); color:var(--keep); }
  .chip.unknown { color:var(--muted); border-style:dashed; }
  footer { position:fixed; left:0; right:0; bottom:0; background:var(--panel); border-top:1px solid var(--line);
           padding:12px 20px; display:flex; gap:16px; align-items:center; flex-wrap:wrap; }
  #log { white-space:pre-line; font-size:.9em; line-height:1.5; margin-top:14px; }
  .modal { position:fixed; inset:0; background:rgba(0,0,0,.55); display:flex; align-items:center; justify-content:center; z-index:10; }
  .modal > div { background:var(--field); border:1px solid var(--line); border-radius:8px; padding:20px 24px; max-width:640px; width:92%; }
  .modal .msg { white-space:pre-line; line-height:1.5; max-height:60vh; overflow-y:auto; overflow-wrap:anywhere; margin:10px 0 18px; }
  .modal .row { display:flex; justify-content:flex-end; gap:8px; }
  @media (prefers-reduced-motion: reduce) { .progress > div { transition:none; } }
</style>
</head>
<body>
<header>
  <div>
    <h1>🧹 Nettoyarr — Explorateur</h1>
    <div class="muted" id="built"></div>
  </div>
  <span class="dry" id="dry" hidden>DRY_RUN actif : rien ne sera réellement supprimé</span>
  <span style="flex:1"></span>
  <a href="/config">⚙️ Configuration</a>
</header>

<div class="criteria" id="criteria"></div>

<div class="bar">
  <button id="refresh">↻ Actualiser l'inventaire</button>
  <span id="count" class="muted"></span>
</div>
<div id="status"></div>
<div class="progress" id="progress"><div></div></div>
<div id="warnings"></div>

<div class="tablewrap">
<table>
  <thead><tr>
    <th class="nosort"><input type="checkbox" id="all" title="Tout cocher (lignes affichées)"></th>
    <th data-k="kind">Type</th>
    <th data-k="title">Release</th>
    <th data-k="size" class="num">Taille</th>
    <th data-k="seeds">Trackers (seeders)</th>
    <th data-k="imported">Importé</th>
    <th data-k="last_played">Vu le</th>
  </tr></thead>
  <tbody id="rows"></tbody>
</table>
</div>
<div id="log"></div>

<footer>
  <span id="selinfo">Aucune release cochée</span>
  <button class="primary" id="delete" disabled>🧹 Supprimer la sélection</button>
</footer>

<script>
"use strict";
const $ = (id) => document.getElementById(id);
const GIB = 1024 ** 3;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const fmtSize = (b) => (b / GIB).toFixed(1).replace(".", ",") + " Gio";
const fmtDate = (ts) => new Date(ts * 1000).toLocaleDateString("fr-FR");
const KIND = { movie: "Film", episodes: "Série", orphan: "Sans *arr" };

let DATA = null, BUSY = false;
const SELECTED = new Set();
const DONE = new Set();
let SORT = { k: "size", dir: -1 };

function el(tag, attrs = {}, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v; else if (k === "text") e.textContent = v; else e.setAttribute(k, v);
  }
  for (const c of children) if (c != null) e.append(c);
  return e;
}

async function api(path, opts) {
  try {
    const r = await fetch(path, opts);
    let data = null;
    try { data = await r.json(); } catch (e) {}
    return { status: r.status, data };
  } catch (e) {
    return { status: 0, data: null, error: String(e) };
  }
}

// ── Progression d'un job serveur (/job) ─────────────────────────────────
function setStatus(text, current, total) {
  $("status").textContent = text || "";
  const bar = $("progress");
  if (typeof total === "number" && total > 0 && typeof current === "number") {
    bar.style.display = "block";
    bar.firstElementChild.style.width = Math.round((100 * current) / total) + "%";
    $("status").textContent += ` — ${current}/${total}`;
  } else {
    bar.style.display = text ? "block" : "none";
    bar.firstElementChild.style.width = text ? "100%" : "0";
  }
}

async function follow(jobId, prefix = "") {
  let failures = 0;
  for (;;) {
    await sleep(1000);
    const r = await api(`/job?id=${encodeURIComponent(jobId)}`);
    if (r.status === 404) return { status: 404, data: r.data };
    if (r.status !== 200 || !r.data) {
      if (++failures >= 10) return { status: r.status, data: { ok: false, error: "suivi du job interrompu, vérifie les logs" } };
      continue;
    }
    failures = 0;
    if (r.data.state === "done") return { status: 200, data: r.data.result, jobId };
    setStatus(prefix + r.data.step, r.data.current, r.data.total);
  }
}

// ── Inventaire ──────────────────────────────────────────────────────────
async function load(allowBuild = true) {
  const r = await api("/inventory");
  const d = r.data;
  if (!d) { setStatus(`Nettoyarr injoignable (HTTP ${r.status})`); return; }
  if (d.state === "building") {
    await follow(d.job_id, "Inventaire : ");
    return load(false);
  }
  if (d.state === "empty" && allowBuild) return refresh();
  if (d.state === "error" || d.state === "empty") {
    setStatus("");
    $("warnings").replaceChildren(el("div", { class: "warn", text: "❌ Inventaire impossible : " + (d.error || "inconnu") }));
    return;
  }
  setStatus("");
  DATA = d;
  for (const id of [...SELECTED]) if (!DATA.releases.some((x) => x.id === id)) SELECTED.delete(id);
  $("dry").hidden = !d.dry_run;
  const st = d.stats;
  $("built").textContent =
    `Inventaire du ${new Date(d.built_at * 1000).toLocaleString("fr-FR")} (${d.duration} s) : ` +
    `${st.releases} releases pour ${st.torrents} torrents` +
    (st.tautulli ? "" : " — visionnages et bibliothèques Plex indisponibles : Tautulli non configuré (⚙️ Configuration)");
  const warns = [...(st.warnings || [])];
  if (d.last_error) warns.unshift("Dernière actualisation en échec, inventaire précédent affiché : " + d.last_error);
  $("warnings").replaceChildren(...warns.map((w) => el("div", { class: "warn", text: "⚠️ " + w })));
  buildRules(); // listes de choix (trackers, catégories…) issues de l'inventaire
  render();
}

async function refresh() {
  $("refresh").disabled = true;
  const r = await api("/inventory/refresh", { method: "POST" });
  if (r.data && r.data.job_id) await follow(r.data.job_id, "Inventaire : ");
  $("refresh").disabled = false;
  return load(false);
}

// ── Règles de sélection ─────────────────────────────────────────────────
// Arbre : { match: "all"|"any", rules: [ {f, op, v} | {match, rules: [...]} ] }
// (un seul niveau de sous-groupe). Une règle sans valeur est INACTIVE : ni
// vraie ni fausse, pour qu'une ligne en cours de saisie ne vide pas le
// tableau. Un groupe sans aucune règle active est inactif lui aussi.
const OPS = {
  text:    [["has", "contient"], ["nhas", "ne contient pas"]],
  list:    [["is", "est"], ["isnt", "n'est pas"]],
  tracker: [["on", "en seed sur"], ["off", "pas en seed sur"], ["only", "seulement sur"]],
  num:     [["gt", "supérieur à"], ["lt", "inférieur à"]],
  date:    [["before", "avant le"], ["after", "après le"]],
  played:  [["before", "avant le"], ["after", "après le"], ["never", "jamais vu"], ["unknown", "absent de Plex"]],
};
const VALUELESS = new Set(["never", "unknown"]);
const FIELDS = {
  q:         { label: "Texte (partout)", type: "text", ph: "titre, release, tracker, catégorie, dossier…" },
  title:     { label: "Titre", type: "text" },
  name:      { label: "Nom des torrents", type: "text", ph: "ex. MULTi, 2160p, -GROUPE" },
  tracker:   { label: "Tracker", type: "tracker" },
  category:  { label: "Catégorie qBittorrent", type: "list" },
  arr:       { label: "Instance *arr", type: "list" },
  kind:      { label: "Type", type: "list" },
  lib:       { label: "Bibliothèque Plex", type: "list" },
  path:      { label: "Dossier", type: "list" },
  size:      { label: "Taille (Gio)", type: "num", step: "0.5" },
  seeds_all: { label: "Seeders sur tous les trackers", type: "num", step: "1" },
  seeds_any: { label: "Seeders sur au moins un tracker", type: "num", step: "1" },
  ntrackers: { label: "Nombre de trackers", type: "num", step: "1" },
  imported:  { label: "Importé", type: "date" },
  played:    { label: "Dernier visionnage", type: "played" },
};

let RULES = { match: "all", rules: [] };
const dateTs = (v) => (v ? Date.parse(v + "T00:00:00") / 1000 : null);
const newRule = (f = "size") => ({ f, op: OPS[FIELDS[f].type][0][0], v: "" });
const isActive = (x) => !!FIELDS[x.f] && (VALUELESS.has(x.op) || (x.v !== "" && x.v != null));
const uniqOpts = (arr) => [...new Set(arr)].filter(Boolean).sort((a, b) => a.localeCompare(b, "fr")).map((x) => [x, x]);

// Nom affiché d'un tracker : celui saisi dans /config, sinon le nom de
// l'indexeur Prowlarr, sinon le nom de domaine sans sous-domaine ni extension
// (même règle que tracker_auto_name).
function autoTrackerName(host) {
  if (!host || host === "?" || /^[\d.]+$/.test(host)) return host || "?";
  const parts = host.split(".");
  const base = parts.length >= 2 ? parts[parts.length - 2] : parts[0];
  return base.charAt(0).toUpperCase() + base.slice(1);
}
const trackerName = (host) =>
  ((DATA && DATA.tracker_names) || {})[host] || ((DATA && DATA.prowlarr_names) || {})[host] || autoTrackerName(host);

function optionsFor(f) {
  if (f === "kind") return Object.entries(KIND);
  if (!DATA) return [];
  const R = DATA.releases;
  switch (f) {
    case "tracker": {
      // Un choix par tracker (nom), même s'il a plusieurs adresses d'announce.
      // La valeur enregistrée est une de ses adresses : renommer le tracker
      // dans /config ne casse pas la règle.
      const byName = {};
      for (const h of R.flatMap((r) => r.trackers.map((t) => t.host))) {
        if (h && !(trackerName(h) in byName)) byName[trackerName(h)] = h;
      }
      return Object.entries(byName).map(([n, h]) => [h, n]).sort((a, b) => a[1].localeCompare(b[1], "fr"));
    }
    case "category": return uniqOpts(R.flatMap((r) => r.category));
    case "arr": return uniqOpts(R.map((r) => r.sub));
    case "lib": return [...(DATA.stats.libraries || []).map((l) => [l, l]), ["__none__", "hors Plex"]];
    case "path": {
      const p = DATA.paths || { lib: [], qbit: [] };
      return [...p.lib.map((x) => ["lib:" + x, x + " (bibliothèque)"]), ...p.qbit.map((x) => ["qbit:" + x, x + " (qBittorrent)"])];
    }
  }
  return [];
}

function fieldText(r, f) {
  if (f === "title") return r.title;
  if (f === "name") return [r.name, ...r.trackers.map((t) => t.name)].join("\n");
  return [r.title, r.name, ...r.trackers.map((t) => `${t.name} ${t.host} ${trackerName(t.host)}`), r.category.join(" "), r.sub,
          r.libraries.join(" "), r.lib_paths.join(" "), r.qbit_paths.join(" ")].join("\n");
}

function fieldValues(r, f) {
  switch (f) {
    case "tracker": return r.trackers.map((t) => trackerName(t.host));
    case "category": return r.category;
    case "arr": return [r.sub];
    case "kind": return [r.kind];
    case "lib": return r.libraries.length ? r.libraries : ["__none__"];
    case "path": return [...r.lib_paths.map((p) => "lib:" + p), ...r.qbit_paths.map((p) => "qbit:" + p)];
  }
  return [];
}

// Renvoie true / false, ou null si la règle est inactive.
function testRule(r, x) {
  if (!isActive(x)) return null;
  const { f, op, v } = x;
  const type = FIELDS[f].type;
  if (type === "text") {
    const hit = fieldText(r, f).toLowerCase().includes(String(v).toLowerCase());
    return op === "nhas" ? !hit : hit;
  }
  if (type === "list" || type === "tracker") {
    const vals = fieldValues(r, f);
    const v = type === "tracker" ? trackerName(x.v) : x.v; // comparaison par nom de tracker
    if (op === "only") return vals.length > 0 && vals.every((x) => x === v);
    const hit = vals.includes(v);
    return op === "isnt" || op === "off" ? !hit : hit;
  }
  if (type === "num") {
    const n = Number(v);
    if (isNaN(n)) return null;
    const cmp = (a) => (op === "gt" ? a > n : a < n);
    if (f === "size") return cmp(r.size / GIB);
    if (f === "ntrackers") return cmp(new Set(r.trackers.map((t) => trackerName(t.host))).size);
    // Peu de seeders = précieux. Un tracker au nombre inconnu ne satisfait
    // jamais la règle : dans le doute, la release n'est pas proposée.
    const known = (t) => typeof t.seeds === "number";
    if (f === "seeds_all") return r.trackers.length > 0 && r.trackers.every((t) => known(t) && cmp(t.seeds));
    return r.trackers.some((t) => known(t) && cmp(t.seeds));
  }
  if (type === "date" || type === "played") {
    const lp = type === "played" ? r.last_played : r.imported;
    if (op === "never") return lp === 0;
    if (op === "unknown") return lp === null;
    const ts = dateTs(v);
    if (ts === null || isNaN(ts) || !lp) return false; // date inconnue ou jamais vu : ni avant ni après
    return op === "before" ? lp < ts : lp >= ts + 86400;
  }
  return null;
}

function evalGroup(r, g) {
  let active = 0;
  for (const x of g.rules) {
    const res = x.rules ? evalGroup(r, x) : testRule(r, x);
    if (res === null) continue;
    active++;
    if (g.match === "any" && res) return true;
    if (g.match !== "any" && !res) return false;
  }
  return active ? g.match !== "any" : null;
}

const matches = (r) => evalGroup(r, RULES) !== false;

// Seuil de seeders le plus haut des règles « supérieur à » : sert à surligner
// dans le tableau les trackers en dessous (torrents précieux).
function seedsThreshold(g = RULES) {
  let t = null;
  for (const x of g.rules) {
    const v = x.rules ? seedsThreshold(x)
      : (x.f.startsWith("seeds_") && x.op === "gt" && isActive(x) ? Number(x.v) : null);
    if (v !== null && !isNaN(v)) t = t === null ? v : Math.max(t, v);
  }
  return t;
}

// Conversion des anciens critères fixes (data/explorer.json d'avant les règles).
function migrate(v) {
  if (v && v.rules && Array.isArray(v.rules.rules)) return v.rules;
  const R = { match: "all", rules: [] };
  if (v) {
    const add = (f, op, val) => R.rules.push({ f, op, v: String(val) });
    if (v["c-size"]) add("size", "gt", v["c-size"]);
    if (v["c-seeds"]) add(v["c-seeds-mode"] === "any" ? "seeds_any" : "seeds_all", "gt", v["c-seeds"]);
    if (v["c-imported"]) add("imported", "before", v["c-imported"]);
    if (v["c-played"]) {
      const sub = { match: "any", rules: [{ f: "played", op: "before", v: v["c-played"] }] };
      if (v["c-never"]) sub.rules.push({ f: "played", op: "never", v: "" });
      if (v["c-unknown"]) sub.rules.push({ f: "played", op: "unknown", v: "" });
      R.rules.push(sub);
    }
    if (v["c-lib"]) add("lib", "is", v["c-lib"]);
    if (v["c-path"]) add("path", "is", v["c-path"]);
    if (v["c-type"]) add("kind", "is", v["c-type"]);
    if (v["c-q"]) add("q", "has", v["c-q"]);
  }
  if (!R.rules.length) R.rules.push(newRule());
  return R;
}

let saveTimer = null;
function saveRules() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => {
    api("/explorer/criteria", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ rules: RULES }) });
  }, 600);
}

function changed(rebuild = false) {
  if (rebuild) buildRules();
  saveRules();
  render();
}

function buildRules() {
  $("criteria").replaceChildren(groupEl(RULES, null));
}

function groupEl(g, parent) {
  const root = parent === null;
  const sel = el("select", { "aria-label": "Logique du groupe" },
    el("option", { value: "all", text: "toutes" }), el("option", { value: "any", text: "au moins une" }));
  sel.value = g.match === "any" ? "any" : "all";
  sel.onchange = () => { g.match = sel.value; changed(); };
  const head = el("div", { class: "rhead" },
    el("span", { text: root ? "Releases qui remplissent" : "Groupe : remplit" }), sel,
    el("span", { text: "des règles suivantes" }));
  if (!root) {
    const del = el("button", { class: "rdel", title: "Supprimer ce groupe", text: "🗑" });
    del.onclick = () => { parent.rules.splice(parent.rules.indexOf(g), 1); changed(true); };
    head.append(el("span", { style: "flex:1" }), del);
  }
  const box = el("div", { class: root ? "" : "rgroup" }, head);
  for (const x of g.rules) box.append(x.rules ? groupEl(x, g) : ruleEl(x, g));

  const add = el("button", { text: "+ Règle" });
  add.onclick = () => { g.rules.push(newRule()); changed(true); };
  const tools = el("div", { class: "rtools" }, add);
  if (root) {
    const addG = el("button", { text: "+ Groupe", title: "Sous-groupe avec sa propre logique (ex. « au moins une » de plusieurs règles)" });
    addG.onclick = () => { g.rules.push({ match: "any", rules: [newRule("tracker")] }); changed(true); };
    const reset = el("button", { text: "Tout effacer" });
    reset.onclick = () => { RULES = { match: "all", rules: [newRule()] }; changed(true); };
    tools.append(addG, reset);
  }
  box.append(tools);
  return box;
}

function ruleEl(x, g) {
  if (!FIELDS[x.f]) Object.assign(x, newRule());
  const def = FIELDS[x.f];
  if (!OPS[def.type].some(([k]) => k === x.op)) x.op = OPS[def.type][0][0];

  const fSel = el("select", { class: "f", "aria-label": "Critère" },
    ...Object.entries(FIELDS).map(([k, d]) => el("option", { value: k, text: d.label })));
  fSel.value = x.f;
  fSel.onchange = () => { Object.assign(x, newRule(fSel.value)); changed(true); };

  const opSel = el("select", { class: "op", "aria-label": "Opérateur" },
    ...OPS[def.type].map(([k, t]) => el("option", { value: k, text: t })));
  opSel.value = x.op;
  opSel.onchange = () => { x.op = opSel.value; if (VALUELESS.has(x.op)) x.v = ""; changed(true); };

  let input;
  if (def.type === "list" || def.type === "tracker") {
    const opts = optionsFor(x.f);
    if (def.type === "tracker" && x.v !== "") {
      // Règle enregistrée sur une autre adresse du même tracker : même choix.
      const same = opts.find(([, n]) => n === trackerName(x.v));
      if (same) x.v = same[0];
    }
    if (x.v !== "" && !opts.some(([k]) => k === x.v)) opts.push([x.v, (def.type === "tracker" ? trackerName(x.v) : x.v) + " (absent de l'inventaire)"]);
    input = el("select", { class: "v", "aria-label": "Valeur" },
      el("option", { value: "", text: "— choisir —" }), ...opts.map(([k, t]) => el("option", { value: k, text: t })));
  } else {
    const kind = def.type === "num" ? "number" : def.type === "text" ? "text" : "date";
    input = el("input", { class: "v", type: kind, "aria-label": "Valeur" });
    if (kind === "number") { input.min = "0"; input.step = def.step || "1"; }
    if (def.ph) input.placeholder = def.ph;
  }
  input.value = x.v;
  input.hidden = VALUELESS.has(x.op);

  const row = el("div", { class: "rrow" + (isActive(x) ? "" : " idle") }, fSel, opSel, input);
  input.addEventListener("input", () => {
    x.v = input.value;
    row.classList.toggle("idle", !isActive(x));
    changed();
  });
  const del = el("button", { class: "rdel", title: "Supprimer cette règle", text: "🗑" });
  del.onclick = () => { g.rules.splice(g.rules.indexOf(x), 1); changed(true); };
  row.append(del);
  return row;
}

function minSeeds(r) {
  const known = r.trackers.map((t) => t.seeds).filter((s) => typeof s === "number");
  return known.length ? Math.min(...known) : -1;
}

function sortValue(r, k) {
  if (k === "seeds") return minSeeds(r);
  if (k === "last_played") return r.last_played === null ? -1 : r.last_played;
  if (k === "imported") return r.imported || 0;
  if (k === "title" || k === "kind") return String(r[k]).toLowerCase();
  return r[k];
}

function visible() {
  if (!DATA) return [];
  const list = DATA.releases.filter((r) => DONE.has(r.id) || matches(r));
  list.sort((a, b) => {
    const x = sortValue(a, SORT.k), y = sortValue(b, SORT.k);
    return (x < y ? -1 : x > y ? 1 : 0) * SORT.dir;
  });
  return list;
}

// ── Tableau ─────────────────────────────────────────────────────────────
function render() {
  const thr = seedsThreshold();
  const list = visible();
  const frag = document.createDocumentFragment();
  for (const r of list) {
    const done = DONE.has(r.id);
    const cb = el("input", { type: "checkbox" });
    cb.checked = SELECTED.has(r.id);
    cb.disabled = done || BUSY;
    cb.onchange = () => { cb.checked ? SELECTED.add(r.id) : SELECTED.delete(r.id); render(); };

    const chips = el("td");
    for (const t of r.trackers) {
      const unknown = typeof t.seeds !== "number";
      const keep = thr !== null && !unknown && t.seeds <= thr;
      chips.append(el("span", {
        class: "chip" + (unknown ? " unknown" : keep ? " keep" : ""),
        title: `${t.host}\n${t.name}\n${t.state}` + (keep ? "\nPeu de seeders : torrent précieux sur ce tracker" : ""),
        text: `${trackerName(t.host)} ${unknown ? "?" : t.seeds}`,
      }));
    }

    let played = "—", playedTitle = "Absent de Plex, ou Tautulli non configuré";
    if (r.last_played === 0) { played = "jamais vu"; playedTitle = ""; }
    else if (r.last_played) { played = fmtDate(r.last_played); playedTitle = ""; }

    const tr = el("tr", { class: (SELECTED.has(r.id) ? "sel" : "") + (done ? " done" : "") },
      el("td", {}, cb),
      el("td", {}, el("span", { class: "kind" + (r.kind === "orphan" ? " orphan" : ""), text: KIND[r.kind] })),
      el("td", {},
        el("div", { class: "title", text: r.title,
          title: [...r.lib_paths.map((p) => "Bibliothèque : " + p), ...r.qbit_paths.map((p) => "qBittorrent : " + p)].join("\n") }),
        el("div", { class: "rname", text: [r.kind === "orphan" ? "" : r.name, r.sub, r.libraries.join(", ") || "hors Plex", r.category.join(", ")].filter(Boolean).join(" · ") })),
      el("td", { class: "num", text: fmtSize(r.size) }),
      chips,
      el("td", { class: "num", title: r.imported_src === "qbit" ? "Date d'ajout dans qBittorrent (aucun *arr)" : "", text: r.imported ? fmtDate(r.imported) + (r.imported_src === "qbit" ? "*" : "") : "—" }),
      el("td", { class: "num", title: playedTitle, text: played }),
    );
    frag.append(tr);
  }
  $("rows").replaceChildren(frag);

  const shown = list.filter((r) => !DONE.has(r.id));
  const shownSize = shown.reduce((a, r) => a + r.size, 0);
  $("count").textContent = DATA ? `${shown.length} release(s) correspondante(s) sur ${DATA.releases.length} — ${fmtSize(shownSize)}` : "";
  $("all").checked = shown.length > 0 && shown.every((r) => SELECTED.has(r.id));
  $("all").disabled = BUSY;

  const sel = DATA ? DATA.releases.filter((r) => SELECTED.has(r.id)) : [];
  const selSize = sel.reduce((a, r) => a + r.size, 0);
  $("selinfo").textContent = sel.length ? `${sel.length} release(s) cochée(s) — ${fmtSize(selSize)}` : "Aucune release cochée";
  $("delete").disabled = BUSY || !sel.length;
  for (const th of document.querySelectorAll("th[data-k]")) {
    th.textContent = th.textContent.replace(/ [▲▼]$/, "") + (th.dataset.k === SORT.k ? (SORT.dir > 0 ? " ▲" : " ▼") : "");
  }
}

// ── Popups ──────────────────────────────────────────────────────────────
function modal(message, { cancel = true, okLabel = "OK" } = {}) {
  return new Promise((resolve) => {
    const ok = el("button", { class: "primary", text: okLabel });
    const row = el("div", { class: "row" });
    const box = el("div", { class: "modal" }, el("div", {}, el("strong", { text: "🧹 Nettoyarr" }), el("div", { class: "msg", text: message }), row));
    const close = (v) => { box.remove(); resolve(v); };
    if (cancel) { const c = el("button", { text: "Annuler" }); c.onclick = () => close(false); row.append(c); }
    ok.onclick = () => close(true);
    row.append(ok);
    document.body.append(box);
    ok.focus();
  });
}

// ── Suppression (mêmes endpoints que le bouton qBittorrent) ─────────────
function formatLinks(l) {
  if (!l) return "";
  if (l.error) return `\n   ⚠️ autres hardlinks non vérifiés : ${l.error}`;
  if (l.freed) return "\n   💾 espace disque libéré";
  const out = [`\n   🔗 ${l.remaining} autre(s) lien(s) ailleurs sur le NAS`];
  if (l.deletable && l.deletable.length) out.push(` (${l.deletable.length} supprimable(s), proposé à la fin)`);
  if (l.not_found) out.push(`, ${l.not_found} non localisé(s) (corbeille Synology ?)`);
  return out.join("");
}

function formatResult(d, rel) {
  if (d && d.ok) {
    const qbit = typeof d.torrents === "number" ? ` (${d.torrents} torrent(s) qBit)` : "";
    if (d.kind === "movie") return `✅ ${d.title} : supprimé de ${d.targets.join(" / ")}${qbit}${formatLinks(d.links)}`;
    if (d.kind === "episodes") return `✅ ${d.summary} : ${d.count} épisode(s) supprimé(s) de ${d.targets.join(" / ")}${qbit}${formatLinks(d.links)}`;
    if (d.kind === "orphan") {
      const kept = d.kept && d.kept.length ? `\n   ⚠️ conservé (torrent plus large) : ${d.kept.join(", ")}` : "";
      return `✅ ${d.title} : supprimé de ${d.targets.join(" / ")}${qbit}${kept}${formatLinks(d.links)}`;
    }
    if (d.kind === "gone") return null; // doublon déjà nettoyé avec le torrent principal
  }
  return `❌ ${rel.title} : ${(d && d.error) || "erreur inconnue"}`;
}

function logLine(text) {
  $("log").textContent += ($("log").textContent ? "\n" : "") + text;
}

// Juste avant de supprimer : inventaire reconstruit (état réel de qBittorrent,
// des *arr et de Tautulli) et règles réévaluées sur chaque release cochée.
// Une release qui ne respecte plus les règles (ex. une copie HDF ajoutée par
// cross-seed depuis le dernier inventaire) est retirée de la sélection.
// Renvoie la liste à supprimer, ou null pour tout annuler.
async function revalidate(rels) {
  setStatus("Vérification avant suppression : actualisation de l'inventaire…");
  const start = await api("/inventory/refresh", { method: "POST" });
  if (start.data && start.data.job_id) await follow(start.data.job_id, "Vérification avant suppression : ");
  const r = await api("/inventory");
  setStatus("");
  const d = r.data;
  if (!d || d.state !== "ready" || d.last_error) {
    await modal("❌ Actualisation de l'inventaire impossible juste avant la suppression : rien n'a été supprimé.\n\n" +
      ((d && (d.last_error || d.error)) || `HTTP ${r.status}`), { cancel: false });
    return null;
  }
  DATA = d;
  buildRules();
  const byHash = new Map();
  for (const x of DATA.releases) for (const h of x.hashes) byHash.set(h, x);
  const kept = [], dropped = [], seen = new Set();
  for (const old of rels) {
    const now = old.hashes.map((h) => byHash.get(h)).find(Boolean);
    if (!now) { dropped.push(`• ${old.title} : plus présente dans qBittorrent`); continue; }
    if (seen.has(now.id)) continue; // deux anciennes releases regroupées en une seule
    seen.add(now.id);
    if (!matches(now)) { dropped.push(`• ${now.title} : ne respecte plus les règles`); continue; }
    kept.push(now);
  }
  SELECTED.clear();
  for (const x of kept) SELECTED.add(x.id);
  render();
  if (!dropped.length) return kept;
  const msg = `Après actualisation, ${dropped.length} release(s) retirée(s) de la sélection :\n\n${dropped.join("\n")}`;
  if (!kept.length) { await modal(msg + "\n\nRien à supprimer.", { cancel: false }); return null; }
  const size = kept.reduce((a, x) => a + x.size, 0);
  const go = await modal(msg + `\n\nSupprimer les ${kept.length} release(s) restante(s) (${fmtSize(size)}) ?`, { okLabel: "Supprimer" });
  return go ? kept : null;
}

async function deleteSelected() {
  let rels = DATA.releases.filter((r) => SELECTED.has(r.id) && !DONE.has(r.id));
  if (!rels.length) return;
  const total = rels.reduce((a, r) => a + r.size, 0);
  const shown = rels.slice(0, 15).map((r) => `• ${r.title} (${fmtSize(r.size)})`).join("\n");
  const ok = await modal(
    `Supprimer définitivement ${rels.length} release(s), ${fmtSize(total)} :\n\n${shown}` +
    (rels.length > 15 ? `\n… +${rels.length - 15} autre(s)` : "") +
    "\n\nFichiers, qBittorrent (tous les trackers), Radarr/Sonarr, et Seerr pour les films." +
    (DATA.dry_run ? "\n\nDRY_RUN actif : simulation seulement." : ""),
    { okLabel: "Supprimer" });
  if (!ok) return;

  BUSY = true; render();
  rels = await revalidate(rels);
  if (!rels) { BUSY = false; render(); return; }
  $("log").textContent = "";
  const pendingLinks = [], failed = [];
  let doneSize = 0, doneCount = 0;
  for (let i = 0; i < rels.length; i++) {
    const rel = rels[i];
    const prefix = `${i + 1}/${rels.length} — ${rel.title} : `;
    // Torrent principal d'abord : il emporte normalement ses doublons
    // cross-seed ; les suivants ne sont traités que s'ils ont survécu.
    for (let j = 0; j < rel.hashes.length; j++) {
      setStatus(prefix + "démarrage…");
      const start = await api(`/delete-by-hash?hash=${encodeURIComponent(rel.hashes[j])}&async=1`);
      const jobId = start.data && start.data.job_id;
      const res = jobId ? await follow(jobId, prefix) : { data: start.data || { ok: false, error: `HTTP ${start.status}` } };
      const line = formatResult(res.data, rel);
      if (line) logLine(line);
      const links = res.data && res.data.links;
      if (res.jobId && links && links.deletable && links.deletable.length) {
        pendingLinks.push({ jobId: res.jobId, name: rel.title, links: links.deletable });
      }
      if (!(res.data && res.data.ok)) { failed.push(line || `❌ ${rel.title}`); break; } // échec : on ne touche pas aux doublons
      if (j === rel.hashes.length - 1 || res.data.kind !== "gone") DONE.add(rel.id);
    }
    if (DONE.has(rel.id)) { SELECTED.delete(rel.id); doneCount++; doneSize += rel.size; }
    render();
  }
  setStatus("");
  BUSY = false; render();

  const linkLines = pendingLinks.length ? await confirmLinks(pendingLinks) : [];
  const dry = DATA.dry_run ? "\n\nDRY_RUN : simulation, rien n'a réellement été supprimé." : "";
  const recap =
    `${doneCount ? "✅" : "❌"} ${doneCount}/${rels.length} release(s) supprimée(s) — ${fmtSize(doneSize)}` +
    (failed.length ? `\n\nÉchecs (${failed.length}) :\n${failed.join("\n")}` : "") +
    (linkLines.length ? `\n\nHardlinks :\n${linkLines.join("\n")}` : "") +
    `\n\nDétail :\n${$("log").textContent}` + dry;
  logLine(dry ? dry.trim() : "\nTerminé.");
  await modal(recap, { cancel: false });
}

async function confirmLinks(pending) {
  const all = pending.flatMap((p) => p.links);
  const total = all.reduce((a, l) => a + l.size, 0);
  const shown = all.slice(0, 12).map((l) => `• ${l.path} (${fmtSize(l.size)})`).join("\n");
  const ok = await modal(
    "Ces fichiers sont d'autres hardlinks des fichiers supprimés, hors qBittorrent — " +
    `tant qu'ils existent, l'espace disque n'est pas libéré :\n\n${shown}` +
    (all.length > 12 ? `\n… +${all.length - 12} autre(s)` : "") +
    `\n\nLes supprimer aussi (${all.length} fichier(s), ${fmtSize(total)}) ?`,
    { okLabel: "Supprimer les hardlinks" });
  if (!ok) return ["non supprimés (refusé)"];
  const lines = [];
  for (const p of pending) {
    const r = await api(`/delete-links?job=${encodeURIComponent(p.jobId)}`, { method: "POST" });
    const d = r.data;
    if (!d || d.error) { lines.push(`❌ ${p.name} : ${(d && d.error) || "HTTP " + r.status}`); continue; }
    const freed = d.freed ? `, ${fmtSize(d.freed)} libérés` : "";
    lines.push(`${d.errors.length ? "⚠️" : "🔗"} ${p.name} : ${d.deleted.length} hardlink(s) supprimé(s)${freed}${d.dry_run ? " [DRY-RUN]" : ""}`);
    for (const e of d.errors) lines.push(`   ${e}`);
  }
  return lines;
}

// ── Branchements ────────────────────────────────────────────────────────
// Les règles sont enregistrées côté Nettoyarr (data/explorer.json) : elles
// deviennent celles par défaut à la visite suivante, quel que soit l'appareil.
for (const th of document.querySelectorAll("th[data-k]")) {
  th.onclick = () => {
    const k = th.dataset.k;
    SORT = SORT.k === k ? { k, dir: -SORT.dir } : { k, dir: k === "title" || k === "kind" ? 1 : -1 };
    render();
  };
}
$("all").onchange = () => {
  for (const r of visible()) if (!DONE.has(r.id)) $("all").checked ? SELECTED.add(r.id) : SELECTED.delete(r.id);
  render();
};
$("refresh").onclick = () => { DONE.clear(); refresh(); };
$("delete").onclick = deleteSelected;
api("/explorer/criteria").then((r) => {
  RULES = migrate(r.data && typeof r.data === "object" ? r.data : null);
  buildRules();
  load();
});
</script>
</body>
</html>
"""


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
        if self.path in ("/", ""):
            self.send_response(303)
            self.send_header("Location", "/explorer")
            self.end_headers()
            return
        if self.path.startswith("/explorer/criteria"):
            if not check_auth(self):
                return
            self._send_json(200, explorer_prefs_load())
            return
        if self.path.startswith("/explorer"):
            if not check_auth(self):
                return
            body = EXPLORER_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/inventory"):
            if not check_auth(self):
                return
            self._send_json(200, inventory_status())
            return
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
        # links=0 : pas de bilan des autres hardlinks (plus rapide). Défaut : activé.
        with_links = (params.get("links") or ["1"])[0] != "0"

        if not torrent_hash:
            self._send_json(400, {"ok": False, "error": "Paramètre 'hash' manquant"})
            return

        job = create_delete_job(torrent_hash, with_links)
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
        # keep_blank_values : un champ laissé vide doit rester à sa place, sinon
        # les listes répétées (TRACKER_NAME[], *_API_KEY[]) se décalent d'un cran
        # et les valeurs suivantes sont attribuées à la mauvaise ligne.
        params = urllib.parse.parse_qs(raw.decode("utf-8"), keep_blank_values=True)

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
        new_cfg["LINKS_SCAN_ROOT"] = field("LINKS_SCAN_ROOT", "/media").strip().rstrip("/") or "/media"
        new_cfg["TAUTULLI_URL"] = field("TAUTULLI_URL").strip().rstrip("/")
        new_cfg["TAUTULLI_API_KEY"] = field("TAUTULLI_API_KEY").strip()
        new_cfg["PROWLARR_URL"] = field("PROWLARR_URL").strip().rstrip("/")
        new_cfg["PROWLARR_API_KEY"] = field("PROWLARR_API_KEY").strip()
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
        new_cfg["TRACKER_NAMES"] = {
            h: n.strip()
            for hs, n in zip(params.get("TRACKER_HOSTS[]", []), params.get("TRACKER_NAME[]", []))
            if n.strip()
            for h in hs.split()
        }

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

    def _handle_delete_links(self):
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        job = JOBS.get((params.get("job") or [""])[0])
        if not job:
            self._send_json(404, {"ok": False, "error": "Job inconnu (expiré, ou conteneur redémarré entre-temps)"})
            return
        pending, job.pending_links = job.pending_links, None  # usage unique
        if not pending:
            self._send_json(404, {"ok": False, "error": "Aucun hardlink en attente pour ce job (déjà traité ?)"})
            return
        result = delete_links(pending)
        log.info(f"/delete-links ({job.hash[:8]}) : {result}")
        self._send_json(200, result)

    def do_POST(self):
        if self.path.startswith("/explorer/criteria"):
            if not check_auth(self):
                return
            length = min(int(self.headers.get("Content-Length", 0)), 16384)
            try:
                explorer_prefs_save(json.loads(self.rfile.read(length).decode("utf-8") or "{}"))
                self._send_json(200, {"ok": True})
            except Exception as e:
                self._send_json(400, {"ok": False, "error": str(e)})
            return

        if self.path.startswith("/inventory/refresh"):
            if not check_auth(self):
                return
            self._send_json(202, {"ok": True, "job_id": start_inventory_job().id})
            return

        if self.path.startswith("/delete-links"):
            if not check_auth(self):
                return
            self._handle_delete_links()
            return

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
    threading.Thread(target=links_index_loop, daemon=True).start()

    if QBIT_POLL_ENABLED:
        if not RADARR_INSTANCES:
            log.warning("QBIT_POLL_ENABLED=true mais RADARR_INSTANCES est vide, le poll ne trouvera jamais rien")
        threading.Thread(target=qbit_poll_loop, daemon=True).start()
    else:
        log.info("Poll qBit désactivé (QBIT_POLL_ENABLED=false)")

    server = ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), Handler)
    log.info(f"En écoute sur :{LISTEN_PORT} (/health, /config, /explorer, /inventory, /delete-by-hash, /job, /delete-links, /file-delete, /item-delete)")
    server.serve_forever()
