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
"""

import json
import logging
import os
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
        else:
            self.send_response(404)
            self.end_headers()

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
    server = ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), Handler)
    log.info(f"En écoute sur :{LISTEN_PORT} (/health, /file-delete, /item-delete)")
    server.serve_forever()
