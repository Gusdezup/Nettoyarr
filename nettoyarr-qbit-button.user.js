// ==UserScript==
// @name         Nettoyarr — Supprimer depuis qBittorrent
// @namespace    nettoyarr
// @version      0.5.0
// @description  Ajoute une entrée "🧹 Nettoyarr" directement dans le menu contextuel (clic droit) de qBittorrent WebUI, pour supprimer un ou plusieurs torrents (films et/ou séries, y compris packs de saison, sélection multiple) aussi dans Radarr/Sonarr/Seerr via nettoyarr
// @match        http://192.168.1.20:8081/*
// @grant        GM_xmlhttpRequest
// @grant        unsafeWindow
// @connect      192.168.1.20
// @run-at       document-idle
// ==/UserScript==

(function () {
  "use strict";

  // Adresse de nettoyarr, à ajuster si besoin
  const NETTOYARR_URL = "http://192.168.1.20:9999";

  const MENU_ID = "torrentsTableMenu";
  const ITEM_ID = "nettoyarrMenuItem";

  // ── Popup custom (remplace confirm()/alert() natifs) ────────────────────
  // Les boîtes natives affichent toujours l'origine de la page ("192.168.
  // 1.20:8081") en en-tête, imposé par le navigateur — impossible à retirer
  // tant qu'on les utilise. D'où cette popup maison, stylée sobrement pour
  // s'intégrer à l'interface sombre de qBittorrent.
  function showModal(message, { withCancel = false } = {}) {
    return new Promise((resolve) => {
      const overlay = document.createElement("div");
      overlay.style.cssText =
        "position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:99999;" +
        "display:flex;align-items:center;justify-content:center;";

      const box = document.createElement("div");
      box.style.cssText =
        "background:#22262b;color:#e8e8e8;border:1px solid #3a3f45;border-radius:8px;" +
        "padding:20px 24px;max-width:440px;min-width:280px;" +
        "font-family:system-ui,sans-serif;font-size:16px;box-shadow:0 8px 28px rgba(0,0,0,.6);";

      const title = document.createElement("div");
      title.textContent = "🧹 Nettoyarr";
      title.style.cssText = "font-weight:600;font-size:19px;margin-bottom:12px;";

      const body = document.createElement("div");
      body.textContent = message;
      body.style.cssText = "font-size:16px;white-space:pre-line;line-height:1.5;margin-bottom:20px;";

      const row = document.createElement("div");
      row.style.cssText = "display:flex;justify-content:flex-end;gap:8px;";

      const close = (result) => {
        document.body.removeChild(overlay);
        resolve(result);
      };

      if (withCancel) {
        const cancelBtn = document.createElement("button");
        cancelBtn.textContent = "Annuler";
        cancelBtn.style.cssText =
          "padding:8px 16px;background:#3a3f45;color:#e8e8e8;border:none;border-radius:6px;cursor:pointer;";
        cancelBtn.onclick = () => close(false);
        row.appendChild(cancelBtn);
      }

      const okBtn = document.createElement("button");
      okBtn.textContent = "OK";
      okBtn.style.cssText =
        "padding:8px 16px;background:#c0392b;color:#fff;border:none;border-radius:6px;cursor:pointer;";
      okBtn.onclick = () => close(true);
      row.appendChild(okBtn);

      box.appendChild(title);
      box.appendChild(body);
      box.appendChild(row);
      overlay.appendChild(box);
      document.body.appendChild(overlay);
      okBtn.focus();
    });
  }

  const nettoyarrConfirm = (message) => showModal(message, { withCancel: true });
  const nettoyarrAlert = (message) => showModal(message, { withCancel: false });

  // ── Noms de torrents via l'API qBittorrent elle-même ────────────────────
  // Requête vers /api/v2/... : même origine que la page (port 8081), donc
  // ni CSP ni CORS ne posent de problème ici — un fetch() classique suffit,
  // pas besoin de GM_xmlhttpRequest comme pour l'appel à nettoyarr (port
  // 9999, cross-origin). Le cookie de session qBit est déjà présent.
  async function getTorrentNames(hashes) {
    const names = {};
    try {
      const res = await fetch(`/api/v2/torrents/info?hashes=${encodeURIComponent(hashes.join("|"))}`);
      if (res.ok) {
        const list = await res.json();
        for (const t of list) names[t.hash] = t.name;
      }
    } catch (e) {
      // repli silencieux : on affichera le hash tronqué à la place du nom
    }
    return names;
  }

  // ── Appel nettoyarr (cross-origin, CSP contourné via GM_xmlhttpRequest) ──
  function deleteHash(hash) {
    return new Promise((resolve) => {
      GM_xmlhttpRequest({
        method: "GET",
        url: `${NETTOYARR_URL}/delete-by-hash?hash=${encodeURIComponent(hash)}`,
        onload: (res) => {
          let data = null;
          try {
            data = JSON.parse(res.responseText);
          } catch (e) {
            // réponse non-JSON (ancienne version de nettoyarr ?) — on retombe
            // sur le texte brut plus bas via r.raw
          }
          resolve({ hash, status: res.status, data, raw: res.responseText });
        },
        onerror: (err) =>
          resolve({ hash, status: 0, data: null, raw: "Impossible de joindre nettoyarr : " + JSON.stringify(err) }),
      });
    });
  }

  function formatResult(r) {
    if (r.data && r.data.ok) {
      if (r.data.kind === "movie") {
        return `✅ ${r.data.title} : supprimé de ${r.data.targets.join(" / ")}`;
      }
      if (r.data.kind === "episodes") {
        const unit = r.data.count > 1 ? "épisodes supprimés" : "épisode supprimé";
        return `✅ ${r.data.summary} : ${r.data.count} ${unit} de ${r.data.targets.join(" / ")}`;
      }
    }
    const err = (r.data && r.data.error) || r.raw || `erreur HTTP ${r.status}`;
    return `❌ ${err}`;
  }

  async function onNettoyarrClick(e) {
    e.preventDefault();

    // torrentsTable est un objet de la PAGE (client.js de qBittorrent), pas
    // du script — un userscript tourne dans un contexte isolé par défaut,
    // d'où le passage obligatoire par unsafeWindow pour y accéder.
    const table = unsafeWindow.torrentsTable;
    if (!table || typeof table.selectedRowsIds !== "function") {
      await nettoyarrAlert(
        "Impossible d'accéder à torrentsTable (page qBittorrent) — " +
        "le script a peut-être besoin d'une mise à jour si la WebUI a changé."
      );
      return;
    }

    const hashes = table.selectedRowsIds();
    if (!hashes || hashes.length === 0) {
      await nettoyarrAlert("Aucun torrent sélectionné.");
      return;
    }

    const names = await getTorrentNames(hashes);
    const label =
      hashes.length === 1
        ? names[hashes[0]] || hashes[0]
        : hashes.map((h) => names[h] || h.slice(0, 8)).join("\n");

    const confirmed = await nettoyarrConfirm(
      `Supprimer définitivement :\n${label}\n\n(fichiers, qBittorrent, Radarr/Sonarr, et Seerr pour les films)`
    );
    if (!confirmed) return;

    const results = await Promise.all(hashes.map(deleteHash));
    await nettoyarrAlert(results.map(formatResult).join("\n\n"));
  }

  function injectMenuItem() {
    const menu = document.getElementById(MENU_ID);
    if (!menu || document.getElementById(ITEM_ID)) return false;

    const li = document.createElement("li");
    li.className = "separator"; // trait de séparation avant, comme les autres groupes du menu
    li.innerHTML = `<a href="#nettoyarr" id="${ITEM_ID}">
      <img src="images/edit-clear.svg" alt="Nettoyarr"> 🧹 Nettoyarr — Supprimer partout
    </a>`;
    menu.appendChild(li);

    document.getElementById(ITEM_ID).addEventListener("click", onNettoyarrClick);
    return true;
  }

  // Le menu existe généralement dès le chargement de la page (juste masqué
  // en CSS tant qu'aucun clic droit n'a eu lieu), mais on reste tolérant au
  // cas où qBittorrent le construise plus tard dynamiquement.
  const interval = setInterval(() => {
    if (injectMenuItem()) clearInterval(interval);
  }, 500);
})();
