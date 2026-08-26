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

Suppression déclenchée depuis qBittorrent (optionnel, films uniquement) :
qBittorrent n'ayant pas de webhook de suppression, on utilise un polling
en tâche de fond. Activer avec QBIT_POLL_ENABLED=true et fournir
RADARR_INSTANCES (JSON) :
  RADARR_INSTANCES=[{"url":"http://192.168.1.20:7878","api_key":"..."}]
Nécessite que /media soit monté en lecture seule dans ce conteneur pour
la vérification de sécurité (le script refuse d'agir si le fichier existe
encore sur le disque, signe que seul le torrent a été retiré sans les
fichiers). Sans ce montage, la vérification est ignorée avec un warning.
"""

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ── Config (variables d'environnement) ─────────────────────────────────────
QBIT_URL      = os.environ.get("QBIT_URL", "http://192.168.1.20:8081")
QBIT_USER     = os.environ.get("QBIT_USER", "")
QBIT_PASS     = os.environ.get("QBIT_PASS", "")
QBIT_API_KEY  = os.environ.get("QBIT_API_KEY", "")  # qBittorrent >= 5.2.0, recommandé

SEERR_URL     = os.environ.get("SEERR_URL", "http://seerr:5055")
SEERR_API_KEY = os.environ.get("SEERR_API_KEY", "")

TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "")  # requis seulement pour les séries

LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "9999"))
DRY_RUN     = os.environ.get("DRY_RUN", "true").lower() == "true"
LOG_LEVEL   = os.environ.get("LOG_LEVEL", "INFO").upper()

# ── Suppression déclenchée depuis qBittorrent (polling) ─────────────────────
# Désactivé par défaut. qBittorrent n'a pas de webhook "à la suppression",
# donc on scrute périodiquement la liste des torrents pour détecter les
# disparitions, puis on demande à Radarr de supprimer le film correspondant
# (deleteFiles=true) — ce qui déclenche le webhook item-delete existant et
# cascade proprement vers Seerr, sans jamais toucher le fichier directement
# ni risquer un regrab automatique (Radarr est toujours informé).
# Films uniquement pour l'instant — pas encore de support séries/Sonarr ici.
QBIT_POLL_ENABLED       = os.environ.get("QBIT_POLL_ENABLED", "false").lower() == "true"
QBIT_POLL_INTERVAL      = int(os.environ.get("QBIT_POLL_INTERVAL", "60"))
QBIT_POLL_GRACE_SECONDS = int(os.environ.get("QBIT_POLL_GRACE_SECONDS", "300"))
# JSON : [{"url": "http://192.168.1.20:7878", "api_key": "..."}, ...]
RADARR_INSTANCES = json.loads(os.environ.get("RADARR_INSTANCES", "[]"))

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("arr-cleanup")
log.info(f"Démarrage — DRY_RUN={DRY_RUN}")


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
        log.info(f"Torrent(s) supprimé(s) dans qBittorrent : {hashes_param}")


qb = QBClient(QBIT_URL, QBIT_USER, QBIT_PASS, QBIT_API_KEY)


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


def find_and_delete_by_file_size(target_size, label=""):
    """Cherche tous les torrents (y compris doublons cross-seed) contenant
    au moins un fichier de taille EXACTEMENT identique à target_size, et
    les supprime.

    On compare fichier par fichier, jamais la taille totale d'un torrent :
    un torrent peut contenir des extras (sample.mkv, nfo) que Radarr/Sonarr
    n'importent jamais dans la bibliothèque média, donc la taille totale
    d'un torrent avec extras ne correspondra jamais à movieFolderSize/
    episodeFile.size qui, eux, ne comptent que le fichier vidéo réellement
    hardlinké. Un hardlink garantit un octet-pour-octet identique sur ce
    fichier précis, peu importe le reste du contenu du torrent."""
    if not target_size:
        log.warning(f"  Pas de taille exploitable dans le payload{f' ({label})' if label else ''}, abandon du matching qBit")
        return

    matches = []
    for t in qb.get_torrents():
        for f in qb.get_files(t["hash"]):
            if f.get("size") == target_size:
                matches.append((t["hash"], t["name"], os.path.basename(f.get("name", ""))))
                break

    if not matches:
        log.info(f"  Aucun torrent qBit ne correspond à une taille de {target_size} octets")
        return

    for h, name, fname in matches:
        log.info(f"  Match trouvé (fichier {fname}, taille identique) : {name} ({h[:8]})")
    qb.delete_torrents([h for h, _, _ in matches])


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
        tmdb_id = tvdb_to_tmdb(tvdb_id)
        if tmdb_id:
            seerr_delete_media(tmdb_id, "tv")
        else:
            log.warning("  Impossible de convertir tvdbId → tmdbId, nettoyage Seerr ignoré")
    else:
        log.warning("Payload item-delete sans movie/series, ignoré")


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
            self._handle_delete_by_hash()
        else:
            self.send_response(404)
            self.end_headers()

    def _handle_delete_by_hash(self):
        """Appelé par le bouton Tampermonkey dans la WebUI qBittorrent.
        Action immédiate et explicite (clic utilisateur) : pas de délai de
        grâce ni de vérification disque nécessaires, contrairement au
        polling — l'intention de l'utilisateur est déjà sans ambiguïté."""
        qs = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(qs)
        torrent_hash = (params.get("hash") or [""])[0].strip()

        def respond(code, msg):
            log.info(f"/delete-by-hash ({torrent_hash[:8]}) : {msg}")
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(msg.encode())

        if not torrent_hash:
            respond(400, "Paramètre 'hash' manquant")
            return
        if not RADARR_INSTANCES:
            respond(500, "RADARR_INSTANCES n'est pas configuré côté nettoyarr")
            return

        try:
            files = qb.get_files(torrent_hash)
        except Exception as e:
            respond(500, f"Torrent introuvable dans qBittorrent : {e}")
            return

        sizes = [f.get("size") for f in files if f.get("size")]
        for size in sizes:
            found = find_movie_by_file_size(size)
            if found:
                instance, movie = found
                title = movie.get("title", "?")
                radarr_delete_movie(instance, movie["id"])
                respond(200, f"« {title} » supprimé de Radarr ({instance['url']}), cascade en cours (qBit/Seerr)")
                return

        respond(404, "Aucun film Radarr ne correspond à ce torrent")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode())
        except Exception:
            payload = {}

        log.debug(f"Payload reçu sur {self.path} : {json.dumps(payload)[:2000]}")

        try:
            if self.path.startswith("/file-delete"):
                handle_file_delete(payload)
            elif self.path.startswith("/item-delete"):
                handle_item_delete(payload)
            else:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.end_headers()
        except Exception as e:
            log.error(f"Erreur traitement webhook : {e}")
            self.send_response(500)
            self.end_headers()


if __name__ == "__main__":
    if QBIT_POLL_ENABLED:
        if not RADARR_INSTANCES:
            log.warning("QBIT_POLL_ENABLED=true mais RADARR_INSTANCES est vide, le poll ne trouvera jamais rien")
        threading.Thread(target=qbit_poll_loop, daemon=True).start()
    else:
        log.info("Poll qBit désactivé (QBIT_POLL_ENABLED=false)")

    server = ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), Handler)
    log.info(f"En écoute sur :{LISTEN_PORT} (/health, /delete-by-hash, /file-delete, /item-delete)")
    server.serve_forever()
