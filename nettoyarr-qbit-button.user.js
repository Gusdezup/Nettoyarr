// ==UserScript==
// @name         Nettoyarr — Supprimer depuis qBittorrent
// @namespace    nettoyarr
// @version      0.3.0
// @description  Ajoute un bouton dans la WebUI qBittorrent pour supprimer un film ou une série (y compris pack de saison) aussi dans Radarr/Sonarr/Seerr via nettoyarr
// @match        http://192.168.1.20:8081/*
// @grant        GM_xmlhttpRequest
// @connect      192.168.1.20
// @run-at       document-start
// ==/UserScript==

(function () {
  "use strict";

  // Adresse de nettoyarr, à ajuster si besoin
  const NETTOYARR_URL = "http://192.168.1.20:9999";

  function getSelectedTorrentHash() {
    // Le hash est affiché en clair dans le panneau de détails "Général"
    // du torrent sélectionné, sous la forme "Info hash v1 : <hash>".
    try {
      const text = document.body.innerText;
      const idx = text.indexOf("Info hash v1");
      if (idx !== -1) {
        const around = text.slice(idx, idx + 200);
        const m = around.match(/[0-9a-fA-F]{40}/);
        if (m) return m[0];
      }
    } catch (e) {}
    return null;
  }

  function addButton() {
    if (document.getElementById("nettoyarr-btn")) return; // déjà injecté

    const btn = document.createElement("button");
    btn.id = "nettoyarr-btn";
    btn.textContent = "🧹 Nettoyarr";
    btn.style.cssText =
      "position:fixed;bottom:16px;right:16px;z-index:99999;" +
      "padding:10px 16px;background:#c0392b;color:#fff;border:none;" +
      "border-radius:6px;font-size:14px;cursor:pointer;box-shadow:0 2px 6px rgba(0,0,0,.4);";

    btn.onclick = async () => {
      let hash = getSelectedTorrentHash();

      if (!hash) {
        hash = prompt(
          "Impossible de détecter automatiquement le torrent sélectionné " +
          "(assure-toi qu'un torrent est bien sélectionné, onglet « Général » ouvert).\n" +
          "Sinon colle le hash ici :"
        );
        if (!hash) return;
      }

      btn.textContent = "⏳ En cours…";
      btn.disabled = true;

      // GM_xmlhttpRequest s'exécute hors du contexte de la page qBittorrent,
      // donc hors de portée de son Content-Security-Policy (default-src
      // 'self') qui bloquait fetch()/XMLHttpRequest classiques.
      GM_xmlhttpRequest({
        method: "GET",
        url: `${NETTOYARR_URL}/delete-by-hash?hash=${encodeURIComponent(hash.trim())}`,
        onload: (res) => {
          alert((res.status < 400 ? "✅ " : "❌ ") + res.responseText);
          btn.textContent = "🧹 Nettoyarr";
          btn.disabled = false;
        },
        onerror: (err) => {
          alert("❌ Impossible de joindre nettoyarr : " + JSON.stringify(err));
          btn.textContent = "🧹 Nettoyarr";
          btn.disabled = false;
        },
      });
    };

    document.body.appendChild(btn);
  }

  // La WebUI qBittorrent charge son contenu dynamiquement, on réessaie
  // jusqu'à ce que le body soit prêt.
  const interval = setInterval(() => {
    if (document.body) {
      addButton();
      clearInterval(interval);
    }
  }, 500);
})();
