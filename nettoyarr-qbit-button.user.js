// ==UserScript==
// @name         Nettoyarr — Supprimer depuis qBittorrent
// @namespace    nettoyarr
// @version      0.8.0
// @description  Ajoute une entrée "🧹 Nettoyarr" directement dans le menu contextuel (clic droit) de qBittorrent WebUI, pour supprimer un ou plusieurs torrents (films et/ou séries, y compris packs de saison, sélection multiple) aussi dans Radarr/Sonarr/Seerr via nettoyarr
// ⚠️ NE PAS ÉDITER @match ICI : déclare l'adresse de ton qBittorrent dans les
// réglages du script de l'extension, qui survivent aux mises à jour :
// - Violentmonkey : éditeur du script → onglet « Configuration » → ajoute
//   http://<ip-qbittorrent>:<port>/* dans les @match personnalisés, et
//   décoche la conservation des @match d'origine.
// - Tampermonkey : éditeur du script → onglet « Paramètres » → « Correspondances
//   de l'utilisateur » (et décoche « Correspondances d'origine »).
// L'URL de Nettoyarr et les identifiants d'authentification (si activée) se
// règlent via le menu du script : « ⚙️ Configurer Nettoyarr (URL / auth) ».
// @match        http://TON_URL_QBITTORRENT:8081/*
// @grant        GM_xmlhttpRequest
// @grant        GM_setValue
// @grant        GM_getValue
// @grant        GM_registerMenuCommand
// @grant        unsafeWindow
// @connect      *
// @run-at       document-idle
// ==/UserScript==

(function () {
  "use strict";

  // ── Config persistante (survit au remplacement du corps du script) ─────
  // NETTOYARR_URL / AUTH_USER / AUTH_PASS sont stockés dans le stockage de
  // l'extension (attaché à ce script via @namespace), pas dans le code lui-
  // même — coller une future mise à jour par-dessus ne les efface donc pas.
  // Réglage : clic sur l'icône Tampermonkey/Violentmonkey dans la barre
  // d'outils → menu de ce script → "⚙️ Configurer Nettoyarr (URL / auth)".
  // @match et @connect (l'adresse de qBittorrent), eux, restent en dur dans
  // les métadonnées ci-dessus — contrainte technique des userscripts, lus
  // avant même l'exécution — mais changent rarement une fois réglés.
  function getConfig() {
    return {
      nettoyarrUrl: GM_getValue("nettoyarrUrl", "http://TON_URL_NETTOYARR:9999"),
      authUser: GM_getValue("authUser", ""),
      authPass: GM_getValue("authPass", ""),
    };
  }

  GM_registerMenuCommand("⚙️ Configurer Nettoyarr (URL / auth)", () => {
    const cfg = getConfig();
    const url = prompt("URL de ton conteneur Nettoyarr :", cfg.nettoyarrUrl);
    if (url === null) return; // annulé
    const user = prompt("AUTH_USER (laisser vide si authentification désactivée) :", cfg.authUser);
    if (user === null) return;
    const pass = prompt("AUTH_PASS (laisser vide si authentification désactivée) :", cfg.authPass);
    if (pass === null) return;
    GM_setValue("nettoyarrUrl", url.trim());
    GM_setValue("authUser", user.trim());
    GM_setValue("authPass", pass);
    alert("🧹 Nettoyarr : config sauvegardée.");
  });

  const MENU_ID = "torrentsTableMenu";
  const ITEM_ID = "nettoyarrMenuItem";

  const POLL_INTERVAL_MS = 1000; // fréquence de suivi du job côté serveur
  const MAX_POLL_FAILURES = 10;  // ~10 s sans pouvoir joindre nettoyarr → abandon du suivi
  const IDLE_WARNING_S = 60;     // au-delà, on signale que rien n'a bougé (sans abandonner)

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const formatElapsed = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;

  // ── Popup custom (remplace confirm()/alert() natifs) ────────────────────
  // Les boîtes natives affichent toujours l'origine de la page ("192.168.
  // 1.20:8081") en en-tête, imposé par le navigateur — impossible à retirer
  // tant qu'on les utilise. D'où cette popup maison, stylée sobrement pour
  // s'intégrer à l'interface sombre de qBittorrent.
  // Tous les styles passent par element.style (CSSOM) : la CSP de la WebUI
  // (default-src 'self') bloquerait un <style> injecté.
  function buildModalShell() {
    const overlay = document.createElement("div");
    overlay.style.cssText =
      "position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:99999;" +
      "display:flex;align-items:center;justify-content:center;";

    const box = document.createElement("div");
    box.style.cssText =
      "background:#22262b;color:#e8e8e8;border:1px solid #3a3f45;border-radius:8px;" +
      "padding:20px 24px;max-width:480px;min-width:320px;" +
      "font-family:system-ui,sans-serif;font-size:16px;box-shadow:0 8px 28px rgba(0,0,0,.6);";

    const title = document.createElement("div");
    title.textContent = "🧹 Nettoyarr";
    title.style.cssText = "font-weight:600;font-size:19px;margin-bottom:12px;";

    const row = document.createElement("div");
    row.style.cssText = "display:flex;justify-content:flex-end;gap:8px;";

    box.appendChild(title);
    overlay.appendChild(box);
    return { overlay, box, row };
  }

  function makeButton(label, primary) {
    const btn = document.createElement("button");
    btn.textContent = label;
    btn.style.cssText = primary
      ? "padding:8px 16px;background:#c0392b;color:#fff;border:none;border-radius:6px;cursor:pointer;"
      : "padding:8px 16px;background:#3a3f45;color:#e8e8e8;border:none;border-radius:6px;cursor:pointer;";
    return btn;
  }

  function showModal(message, { withCancel = false } = {}) {
    return new Promise((resolve) => {
      const { overlay, box, row } = buildModalShell();

      const body = document.createElement("div");
      body.textContent = message;
      body.style.cssText = "font-size:16px;white-space:pre-line;line-height:1.5;margin-bottom:20px;overflow-wrap:anywhere;max-height:60vh;overflow-y:auto;";

      const close = (result) => {
        document.body.removeChild(overlay);
        resolve(result);
      };

      if (withCancel) {
        const cancelBtn = makeButton("Annuler", false);
        cancelBtn.onclick = () => close(false);
        row.appendChild(cancelBtn);
      }
      const okBtn = makeButton("OK", true);
      okBtn.onclick = () => close(true);
      row.appendChild(okBtn);

      box.appendChild(body);
      box.appendChild(row);
      document.body.appendChild(overlay);
      okBtn.focus();
    });
  }

  const nettoyarrConfirm = (message) => showModal(message, { withCancel: true });
  const nettoyarrAlert = (message) => showModal(message, { withCancel: false });

  // Popup à boutons multiples, avec case à cocher et/ou champ de saisie
  // optionnels. Renvoie { value, checked, text }.
  function askModal(message, { buttons = [], check = null, input = null, handle = null } = {}) {
    return new Promise((resolve) => {
      const { overlay, box, row } = buildModalShell();
      const body = document.createElement("div");
      body.textContent = message;
      body.style.cssText = "font-size:16px;white-space:pre-line;line-height:1.5;margin-bottom:16px;overflow-wrap:anywhere;max-height:55vh;overflow-y:auto;";
      box.appendChild(body);

      let field = null, cb = null;
      if (input) {
        const lab = document.createElement("label");
        lab.textContent = input.label + " ";
        lab.style.cssText = "display:block;margin-bottom:16px;";
        field = document.createElement("input");
        field.type = "number";
        field.min = "1";
        field.value = String(input.value ?? "");
        field.style.cssText = "width:7em;padding:6px;background:#15181c;color:#e8e8e8;border:1px solid #3a3f45;border-radius:4px;font-size:16px;";
        lab.appendChild(field);
        box.appendChild(lab);
      }
      if (check) {
        const lab = document.createElement("label");
        lab.style.cssText = "display:flex;gap:8px;align-items:center;margin-bottom:18px;cursor:pointer;";
        cb = document.createElement("input");
        cb.type = "checkbox";
        cb.checked = !!check.checked;
        lab.appendChild(cb);
        lab.appendChild(document.createTextNode(check.label));
        box.appendChild(lab);
      }

      let closed = false;
      const close = (value) => {
        if (closed) return;
        closed = true;
        overlay.remove();
        resolve({ value, checked: cb ? cb.checked : false, text: field ? field.value : "" });
      };
      if (handle) handle.close = () => close(null); // fermeture externe (question réglée ailleurs)
      let focus = null;
      for (const b of buttons) {
        const btn = makeButton(b.label, !!b.primary);
        btn.onclick = () => close(b.value);
        row.appendChild(btn);
        if (b.primary) focus = btn;
      }
      box.appendChild(row);
      document.body.appendChild(overlay);
      (field || focus || row.lastElementChild).focus();
    });
  }

  // ── Popup de progression ────────────────────────────────────────────────
  // Affichée dès la confirmation, mise à jour à partir de l'état réel du job
  // côté serveur (/job) : étape en cours, compteur quand l'étape en a un
  // (barre qui se remplit), sinon barre qui défile en continu. Chrono du
  // temps écoulé en permanence. Les résultats s'y ajoutent au fil de l'eau,
  // puis le bouton OK apparaît à la fin.
  function createProgressModal(torrentCount) {
    const { overlay, box, row } = buildModalShell();

    const header = document.createElement("div");
    header.style.cssText = "font-size:14px;color:#aaa;margin-bottom:6px;overflow-wrap:anywhere;";

    const stepEl = document.createElement("div");
    stepEl.style.cssText = "font-size:16px;line-height:1.4;margin-bottom:10px;min-height:1.4em;";

    const track = document.createElement("div");
    track.style.cssText =
      "position:relative;height:8px;background:#3a3f45;border-radius:4px;overflow:hidden;";
    const bar = document.createElement("div");
    bar.style.cssText =
      "position:absolute;left:0;top:0;height:100%;width:0;background:#c0392b;border-radius:4px;transition:width .3s;";
    track.appendChild(bar);

    const meta = document.createElement("div");
    meta.style.cssText =
      "display:flex;justify-content:space-between;font-size:13px;color:#aaa;margin-top:6px;";
    const percentEl = document.createElement("span");
    const elapsedEl = document.createElement("span");
    meta.appendChild(percentEl);
    meta.appendChild(elapsedEl);

    const idleEl = document.createElement("div");
    idleEl.style.cssText = "font-size:13px;color:#e0a040;margin-top:6px;min-height:0;";

    const results = document.createElement("div");
    results.style.cssText =
      "font-size:15px;white-space:pre-line;line-height:1.5;margin-top:14px;" +
      "max-height:40vh;overflow-y:auto;overflow-wrap:anywhere;";

    // Annuler : arrête vraiment (copie en cours effacée, torrents suivants
    // abandonnés) — sauf une suppression déjà commencée, menée à son terme.
    // Page séparée : le serveur poursuit seul, la page /jobs prend le relais.
    const actRow = document.createElement("div");
    actRow.style.cssText = "display:flex;justify-content:flex-end;gap:8px;margin-top:12px;";
    const cancelBtn = makeButton("Annuler", false);
    const pageBtn = makeButton("Continuer dans une page séparée", false);
    actRow.appendChild(cancelBtn);
    actRow.appendChild(pageBtn);
    const showOverlay = () => { overlay.style.display = "flex"; };
    const hint = document.createElement("div");
    hint.textContent =
      "Tu peux fermer cet onglet : Nettoyarr va au bout de la sélection tout seul. " +
      "Seule exception : une question (disque à identifier, disque plein…) met la suite en attente " +
      "jusqu'à ta réponse, sur la page séparée.";
    hint.style.cssText = "font-size:13px;color:#aaa;line-height:1.4;margin-top:12px;";

    const progressBlock = document.createElement("div");
    progressBlock.appendChild(header);
    progressBlock.appendChild(stepEl);
    progressBlock.appendChild(track);
    progressBlock.appendChild(meta);
    progressBlock.appendChild(idleEl);
    progressBlock.appendChild(actRow);
    progressBlock.appendChild(hint);

    box.appendChild(progressBlock);
    box.appendChild(results);
    document.body.appendChild(overlay);

    // Barre indéterminée : Web Animations API (non concernée par la CSP)
    let anim = null;
    function setIndeterminate(on) {
      if (on && !anim) {
        bar.style.transition = "none";
        bar.style.width = "30%";
        anim = bar.animate(
          [{ transform: "translateX(-100%)" }, { transform: "translateX(340%)" }],
          { duration: 1400, iterations: Infinity, easing: "ease-in-out" }
        );
      } else if (!on && anim) {
        anim.cancel();
        anim = null;
        bar.style.transition = "width .3s";
      }
    }

    const t0 = Date.now();
    const tick = () => {
      elapsedEl.textContent = "⏱ " + formatElapsed((Date.now() - t0) / 1000);
    };
    tick();
    const timer = setInterval(tick, 250);
    setIndeterminate(true);

    return {
      setTorrent(index, name) {
        header.textContent = torrentCount > 1 ? `Torrent ${index}/${torrentCount} — ${name}` : name;
      },
      setStep(text, current, total, idle, unit) {
        const hasCount = typeof total === "number" && total > 0 && typeof current === "number";
        stepEl.textContent = hasCount && unit !== "bytes" ? `${text} — ${current}/${total}` : text;
        if (hasCount) {
          setIndeterminate(false);
          const pct = Math.max(0, Math.min(100, Math.round((100 * current) / total)));
          bar.style.width = pct + "%";
          percentEl.textContent = pct + " %";
        } else {
          setIndeterminate(true);
          percentEl.textContent = "";
        }
        idleEl.textContent =
          typeof idle === "number" && idle >= IDLE_WARNING_S
            ? `⚠️ Aucune progression depuis ${idle} s — nettoyarr travaille toujours, rien n'est annulé`
            : "";
      },
      onCancel(fn) { cancelBtn.onclick = () => { cancelBtn.disabled = true; cancelBtn.textContent = "Annulation…"; fn(); }; },
      onDetach(fn) { pageBtn.onclick = fn; },
      setCancellable(on) { if (cancelBtn.textContent === "Annuler") cancelBtn.disabled = !on; },
      close() { clearInterval(timer); setIndeterminate(false); overlay.remove(); },
      addResult(text) {
        results.textContent += (results.textContent ? "\n\n" : "") + text;
        results.scrollTop = results.scrollHeight;
      },
      finish() {
        clearInterval(timer);
        setIndeterminate(false);
        showOverlay();
        const total = formatElapsed((Date.now() - t0) / 1000);
        box.removeChild(progressBlock);
        const done = document.createElement("div");
        done.textContent = `Terminé en ${total}`;
        done.style.cssText = "font-size:13px;color:#aaa;margin-top:14px;margin-bottom:16px;";
        box.appendChild(done);
        return new Promise((resolve) => {
          const okBtn = makeButton("OK", true);
          okBtn.onclick = () => {
            document.body.removeChild(overlay);
            resolve();
          };
          row.appendChild(okBtn);
          box.appendChild(row);
          okBtn.focus();
        });
      },
    };
  }

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

  // ── Appels nettoyarr (cross-origin, CSP contourné via GM_xmlhttpRequest) ─
  // Si AUTH_USER/AUTH_PASS sont renseignés, l'en-tête Authorization est
  // envoyé directement dans la requête — GM_xmlhttpRequest permet ça sans
  // déclencher la popup de login native du navigateur (contrairement à un
  // fetch() classique face à un 401).
  function nettoyarrGet(path, opts = {}) {
    return nettoyarrRequest("GET", path, opts);
  }

  function nettoyarrRequest(method, path, { timeout = 30000, body = undefined } = {}) {
    const cfg = getConfig();
    const headers = {};
    if (cfg.authUser || cfg.authPass) {
      headers["Authorization"] = "Basic " + btoa(`${cfg.authUser}:${cfg.authPass}`);
    }
    return new Promise((resolve) => {
      GM_xmlhttpRequest({
        method,
        url: `${cfg.nettoyarrUrl}${path}`,
        headers,
        data: body,
        timeout,
        onload: (res) => {
          let data = null;
          try {
            data = JSON.parse(res.responseText);
          } catch (e) {
            // réponse non-JSON : on retombe sur le texte brut via raw
          }
          resolve({ status: res.status, data, raw: res.responseText });
        },
        onerror: (err) =>
          resolve({ status: 0, data: null, raw: "Impossible de joindre nettoyarr : " + JSON.stringify(err) }),
        ontimeout: () =>
          resolve({ status: 0, data: null, raw: `Pas de réponse de nettoyarr en ${timeout / 1000} s` }),
      });
    });
  }

  const formatSize = (b) => (b / 1024 ** 3).toFixed(2).replace(".", ",") + " Gio";

  // ── Sauvegarde préalable sur disque externe ─────────────────────────────
  async function backupEnabled() {
    const r = await nettoyarrGet("/backup/status", { timeout: 8000 });
    return !!(r.data && r.data.enabled);
  }

  function formatBackup(b) {
    if (!b) return "";
    if (b.kind === "skipped_backup") return "⚠️ supprimé sans sauvegarde (choix manuel)\n";
    if (b.kind === "listed") return `💾 déjà sauvegardé sur le disque ${b.disk} (${b.listing}), pas de nouvelle copie\n`;
    if (!b.ok) return "";
    const how = b.kind === "already" ? "déjà présent sur" : "copié sur";
    const lst = b.listing_added ? `, ajouté à ${String(b.listing).split("/").pop()}`
      : String(b.listing).startsWith("ÉCHEC") ? `\n⚠️ listing non mis à jour : ${b.listing}` : "";
    return `💾 ${how} le disque ${b.disk} (${formatSize(b.size)}${b.seconds ? `, ${b.seconds} s` : ""})${lst}\n`;
  }

  // Question posée par le serveur pendant la sauvegarde (numéro du disque,
  // disque plein…). Popup non bloquante : si la réponse est donnée depuis la
  // page /jobs, elle se ferme d'elle-même.
  function openQuestion(job, handle) {
    const q = job.question;
    const post = (body) =>
      nettoyarrRequest("POST", `/job/answer?id=${encodeURIComponent(job.id)}&q=${encodeURIComponent(q.id)}`,
        { body: JSON.stringify(body) });
    if (q.type === "identify") {
      askModal(
        `${job.name}\n\nDisque d'archive non identifié branché sur ${q.path} (${formatSize(q.free)} libres sur ${formatSize(q.total)}).\n\n` +
        "Quel est son numéro ? Un fichier repère sera créé à sa racine, et la release ajoutée au listing " +
        "disqueN….txt correspondant (créé s'il n'existe pas)." + (q.note ? `\n\n❌ ${q.note}` : ""),
        { handle, input: { label: "Disque n°", value: q.suggested },
          buttons: [{ label: "Ne pas supprimer", value: "abort" }, { label: "Identifier et sauvegarder", value: "identify", primary: true }] }
      ).then((a) => { if (a.value) post({ choice: a.value, disk: a.text }); });
      return;
    }
    const msg = q.type === "disk"
      ? `${job.name}\n\nAucun disque d'archive branché avec assez de place (${formatSize(q.size)} nécessaires).\n\n` +
        (q.disks || []).map((x) => `• ${x.path} : ${x.usable ? `disque ${x.disk || "?"}, ${formatSize(x.free)} libres` : x.reason}`).join("\n") +
        "\n\nBranche un disque dans le dock, puis réessaie."
      : `${job.name}\n\n❌ Sauvegarde impossible :\n${q.error}\n\nLe torrent n'a pas été supprimé.`;
    askModal(msg, { handle, buttons: [
      { label: "Ne pas supprimer", value: "abort" },
      { label: "Supprimer sans sauvegarder", value: "skip" },
      { label: "Réessayer", value: "retry", primary: true }] }
    ).then((a) => { if (a.value) post({ choice: a.value }); });
  }

  // Suit une sélection traitée par le serveur (file d'attente). Renvoie la
  // liste des hardlinks à proposer, ou null si l'utilisateur est passé sur
  // la page séparée.
  async function followBatch(batch, hashes, ui) {
    const reported = new Set();
    const pendingLinks = [];
    let detached = false, question = null, failures = 0;
    ui.onCancel(() => nettoyarrRequest("POST", `/job/cancel?batch=${encodeURIComponent(batch)}`));
    ui.onDetach(() => {
      detached = true;
      if (question) question.handle.close();
      window.open(`${getConfig().nettoyarrUrl}/jobs?batch=${encodeURIComponent(batch)}`, "_blank");
      ui.close();
    });
    for (;;) {
      if (detached) return null;
      const r = await nettoyarrGet(`/jobs.json?batch=${encodeURIComponent(batch)}`, { timeout: 10000 });
      if (detached) return null;
      if (r.status !== 200 || !r.data || !r.data.jobs) {
        if (++failures >= MAX_POLL_FAILURES) {
          ui.addResult(`❌ Suivi interrompu (${r.raw || "HTTP " + r.status}) — la suite continue côté serveur : ${getConfig().nettoyarrUrl}/jobs`);
          return pendingLinks;
        }
        ui.setStep(`Connexion à nettoyarr perdue, nouvel essai (${failures}/${MAX_POLL_FAILURES})…`);
        await sleep(POLL_INTERVAL_MS);
        continue;
      }
      failures = 0;
      const jobs = r.data.jobs;
      for (const j of jobs) {
        if (j.state !== "done" || reported.has(j.id)) continue;
        reported.add(j.id);
        ui.addResult(formatResult({ status: 200, data: j.result }, j.name));
        const links = j.result && j.result.links;
        if (j.links_pending && links && links.deletable && links.deletable.length) {
          pendingLinks.push({ jobId: j.id, name: j.name, links: links.deletable });
        }
      }
      ui.setCancellable(jobs.some((j) => j.cancellable));
      const idx = jobs.findIndex((j) => j.state !== "done");
      const cur = jobs[idx];
      const qid = cur && cur.state === "waiting" && cur.question ? cur.question.id : null;
      if (question && question.id !== qid) { question.handle.close(); question = null; }
      if (!cur) return pendingLinks;
      ui.setTorrent(idx + 1, cur.name);
      if (qid) {
        ui.setStep("En attente de ta réponse…");
        if (!question) {
          question = { id: qid, handle: {} };
          openQuestion(cur, question.handle);
        }
      } else {
        ui.setStep(cur.step, cur.current, cur.total, cur.idle, cur.unit);
      }
      await sleep(POLL_INTERVAL_MS);
    }
  }

  // Bilan des autres hardlinks : ce qui retient encore l'espace disque une
  // fois le torrent supprimé (film, épisodes ou orphelin).
  function formatLinks(links) {
    if (!links) return "";
    if (links.error) return `\n⚠️ Autres hardlinks non vérifiés : ${links.error}`;
    const dry = links.dry_run ? " [DRY-RUN]" : "";
    if (links.freed) return `\n💾 aucun autre lien vers ces fichiers : espace disque libéré${dry}`;
    const out = [`🔗 ${links.remaining} autre(s) lien(s) vers ces fichiers ailleurs sur le NAS${dry}`];
    if (links.deletable && links.deletable.length) {
      const total = links.deletable.reduce((a, l) => a + l.size, 0);
      const extras = links.deletable.filter((l) => l.companion).length;
      const annex = extras ? ` + ${extras} fichier(s) annexe(s) (nfo, jpg…)` : "";
      out.push(`   • ${links.deletable.length - extras} localisé(s) hors qBittorrent${annex} (${formatSize(total)}) — suppression proposée à la fin`);
    }
    if (links.held && links.held.length) {
      const names = [...new Set(links.held.map((l) => l.torrent))].join(", ");
      out.push(`   • retenu(s) par un torrent conservé (chevauchement partiel) : ${names}`);
    }
    if (links.not_found) {
      out.push(links.index_pending
        ? `   • ${links.not_found} non localisé(s) : index des hardlinks pas encore prêt (redémarrage récent)`
        : `   • ${links.not_found} non localisé(s) : probablement dans la corbeille Synology (#recycle, ignorée), libéré(s) à son vidage`);
    }
    return "\n" + out.join("\n");
  }

  function formatResult(r, name) {
    const d = r.data;
    const line = formatResultCore(r, name);
    return (d ? formatBackup(d.backup) : "") + line;
  }

  function formatResultCore(r, name) {
    const d = r.data;
    if (d && d.kind === "cancelled") return `⏹ ${name} : ${d.error}`;
    if (d && d.kind === "skipped") return `⏭️ ${name} : ${d.error}`;
    if (d && d.ok) {
      const qbit = typeof d.torrents === "number" ? ` (${d.torrents} torrent${d.torrents > 1 ? "s" : ""} qBit)` : "";
      if (d.kind === "movie") {
        return `✅ ${d.title} : supprimé de ${d.targets.join(" / ")}${qbit}${formatLinks(d.links)}`;
      }
      if (d.kind === "episodes") {
        const unit = d.count > 1 ? "épisodes supprimés" : "épisode supprimé";
        return `✅ ${d.summary} : ${d.count} ${unit} de ${d.targets.join(" / ")}${qbit}${formatLinks(d.links)}`;
      }
      if (d.kind === "orphan") {
        const dup = d.torrents > 1 ? ` (+${d.torrents - 1} doublon(s) cross-seed)` : "";
        const kept = d.kept && d.kept.length ? `\n⚠️ Conservé (torrent plus large) : ${d.kept.join(", ")}` : "";
        return `✅ ${d.title} : non géré par Radarr/Sonarr, supprimé de ${d.targets.join(" / ")}${dup}${kept}${formatLinks(d.links)}`;
      }
      if (d.kind === "gone") {
        return `ℹ️ ${name} : ${d.message}`;
      }
    }
    const err = (d && d.error) || r.raw || `erreur HTTP ${r.status}`;
    return `❌ ${name} : ${err}`;
  }

  // Deuxième confirmation, explicite : les autres hardlinks des fichiers
  // supprimés (hors qBittorrent) retiennent encore l'espace disque. Le
  // serveur ne supprime que les chemins qu'il a lui-même relevés pour ce job.
  async function confirmLinks(pending) {
    const all = pending.flatMap((p) => p.links);
    const total = all.reduce((a, l) => a + l.size, 0);
    const shown = all.slice(0, 12).map((l) => `• ${l.path} (${l.companion ? "annexe" : formatSize(l.size)})`).join("\n");
    const more = all.length > 12 ? `\n… +${all.length - 12} autre(s)` : "";
    const ok = await nettoyarrConfirm(
      `Ces fichiers sont d'autres hardlinks des fichiers supprimés, hors qBittorrent — ` +
      `tant qu'ils existent, l'espace disque n'est pas libéré :\n\n${shown}${more}\n\n` +
      `Les supprimer aussi (${all.length} fichier(s), ${formatSize(total)}) ?`
    );
    if (!ok) return;

    const lines = [];
    for (const p of pending) {
      const r = await nettoyarrRequest("POST", `/delete-links?job=${encodeURIComponent(p.jobId)}`);
      const d = r.data;
      if (!d) {
        lines.push(`❌ ${p.name} : ${r.raw || "erreur HTTP " + r.status}`);
        continue;
      }
      if (d.error) {
        lines.push(`❌ ${p.name} : ${d.error}`);
        continue;
      }
      const dry = d.dry_run ? " [DRY-RUN]" : "";
      const freed = d.freed ? `, ${formatSize(d.freed)} libérés` : "";
      lines.push(`${d.errors.length ? "⚠️" : "✅"} ${p.name} : ${d.deleted.length} fichier(s) supprimé(s)${freed}${dry}`);
      for (const err of d.errors) lines.push(`   ${err}`);
      for (const dir of d.dirs_removed || []) lines.push(`   🗑️ dossier supprimé : ${dir}`);
      for (const k of d.dirs_kept || []) lines.push(`   📁 dossier conservé, contient encore : ${k.left.join(", ")} (${k.path})`);
    }
    await nettoyarrAlert(lines.join("\n"));
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

    const canBackup = await backupEnabled();
    // Releases déjà présentes dans un listing de disque d'archive : signalées
    // ici, et jamais recopiées même si la case est cochée.
    let saved = "";
    if (canBackup) {
      const chk = await nettoyarrRequest("POST", "/backup/check", { body: JSON.stringify({ hashes }) });
      const found = (chk.data && chk.data.found) || {};
      const lines = hashes.filter((h) => found[h]).map((h) =>
        `• ${names[h] || h.slice(0, 8)} → disque ${found[h].disks.join(", ")}`);
      if (lines.length) saved = `\n\n💾 Déjà sauvegardé (ne sera pas recopié) :\n${lines.join("\n")}`;
    }
    const confirm = await askModal(
      `Supprimer définitivement :\n${label}\n\n(fichiers, qBittorrent, Radarr/Sonarr, et Seerr pour les films)${saved}`,
      { check: canBackup ? { label: "💾 Sauvegarder d'abord sur un disque d'archive (dock)" } : null,
        buttons: [{ label: "Annuler", value: false }, { label: "OK", value: true, primary: true }] }
    );
    if (!confirm.value) return;
    const withBackup = confirm.checked;

    // Toute la sélection est confiée au serveur, qui la traite dans l'ordre
    // (sauvegarde puis suppression, torrent après torrent) : fermer l'onglet
    // ou passer sur la page séparée n'interrompt rien.
    const start = await nettoyarrRequest("POST", "/batch", {
      body: JSON.stringify({ hashes, names, backup: withBackup }),
    });
    if (!(start.data && start.data.batch)) {
      await nettoyarrAlert("❌ " + ((start.data && start.data.error) || start.raw || `HTTP ${start.status}`));
      return;
    }
    const ui = createProgressModal(hashes.length);
    const pendingLinks = await followBatch(start.data.batch, hashes, ui);
    if (pendingLinks === null) return; // suivi poursuivi sur la page /jobs
    await ui.finish();
    if (pendingLinks.length) await confirmLinks(pendingLinks);
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
