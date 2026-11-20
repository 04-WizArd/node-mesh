// NodeMesh file manager. All user-controlled data is rendered with
// textContent / DOM APIs only: never innerHTML (stored-XSS regression guard).
"use strict";

(() => {
  const config = document.getElementById("config").dataset;
  const csrfToken = document.querySelector('meta[name="csrf-token"]').content;
  const maxUpload = Number(config.maxUpload) || 0;

  const el = (id) => document.getElementById(id);
  const dropzone = el("dropzone");
  const input = el("file-input");
  const uploads = el("uploads");
  const table = el("files-table");
  const tbody = el("files-body");
  const empty = el("files-empty");
  const listError = el("files-error");

  const MESSAGES = {
    authentication_required: "Votre session a expiré. Veuillez vous reconnecter.",
    file_too_large: "Fichier trop volumineux.",
    replication_quorum_not_met: "Pas assez de nœuds de stockage disponibles : fichier non enregistré.",
    no_replica_available: "Aucune réplique disponible pour le moment.",
    upload_in_progress: "Un envoi identique est déjà en cours.",
    idempotency_key_reused: "Conflit d'envoi : réessayez.",
    not_found: "Fichier introuvable.",
    network: "Erreur réseau.",
  };
  const message = (code) => MESSAGES[code] || "Une erreur est survenue.";

  function formatSize(bytes) {
    const units = ["o", "Ko", "Mo", "Go", "To"];
    let value = bytes;
    let unit = 0;
    while (value >= 1024 && unit < units.length - 1) {
      value /= 1024;
      unit += 1;
    }
    return `${value.toLocaleString("fr-FR", { maximumFractionDigits: unit ? 1 : 0 })} ${units[unit]}`;
  }

  function node(tag, { text, className, attrs } = {}) {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    if (className) element.className = className;
    for (const [key, value] of Object.entries(attrs || {})) element.setAttribute(key, value);
    return element;
  }

  // crypto.randomUUID() only exists in secure contexts (HTTPS/localhost);
  // getRandomValues() is available everywhere.
  function randomKey() {
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
  }

  if (maxUpload) el("limit-hint").textContent = `Taille maximale : ${formatSize(maxUpload)}`;

  // ---- listing -----------------------------------------------------------
  async function loadFiles() {
    listError.hidden = true;
    let response;
    try {
      response = await fetch(config.filesUrl, { credentials: "same-origin" });
    } catch {
      listError.textContent = message("network");
      listError.hidden = false;
      return;
    }
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      listError.textContent = message(data.error);
      listError.hidden = false;
      return;
    }
    tbody.replaceChildren(...data.files.map(fileRow));
    table.hidden = data.files.length === 0;
    empty.hidden = data.files.length !== 0;
  }

  function fileRow(file) {
    const row = node("tr");
    row.append(node("td", { text: file.name, className: "name", attrs: { title: `SHA-256 : ${file.sha256}` } }));
    row.append(node("td", { text: formatSize(file.size) }));

    const rep = file.replication;
    const replicaCell = node("td", {
      text: `${rep.healthy}/${rep.desired}`,
      className: rep.under_replicated ? "status-warn" : "status-ok",
    });
    if (rep.under_replicated) replicaCell.title = "Réplication incomplète : réparation automatique en attente";
    row.append(replicaCell);

    row.append(node("td", { text: new Date(file.created_at).toLocaleString("fr-FR") }));

    const actions = node("td", { className: "actions" });
    const download = node("a", { text: "Télécharger", className: "btn btn-small", attrs: { href: file.download_url } });
    const remove = node("button", { text: "Supprimer", className: "btn btn-small btn-danger", attrs: { type: "button" } });
    remove.addEventListener("click", () => deleteFile(file, remove));
    actions.append(download, remove);
    row.append(actions);
    return row;
  }

  async function deleteFile(file, button) {
    if (!window.confirm(`Supprimer « ${file.name} » ?`)) return;
    button.disabled = true;
    const response = await fetch(`${config.filesUrl}${file.id}/`, {
      method: "DELETE",
      credentials: "same-origin",
      headers: { "X-CSRFToken": csrfToken },
    }).catch(() => null);
    if (!response || (!response.ok && response.status !== 404)) {
      button.disabled = false;
      listError.textContent = message("network");
      listError.hidden = false;
      return;
    }
    await loadFiles();
  }

  // ---- uploads (sequential, one request per file, with real progress) ------
  function uploadOne(file) {
    const item = node("li");
    const name = node("span", { text: file.name, className: "name" });
    const status = node("span", { text: "En attente…", className: "muted" });
    const bar = node("progress", { attrs: { max: "100", value: "0" } });
    item.append(name, status, bar);
    uploads.prepend(item);

    const finish = (text, cls) => {
      status.textContent = text;
      status.className = cls;
      bar.remove();
    };

    if (maxUpload && file.size > maxUpload) {
      finish(message("file_too_large"), "status-error");
      return Promise.resolve();
    }

    return new Promise((resolve) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", config.filesUrl);
      xhr.setRequestHeader("X-CSRFToken", csrfToken);
      xhr.setRequestHeader("X-Filename", encodeURIComponent(file.name));
      xhr.setRequestHeader("Idempotency-Key", randomKey());
      xhr.setRequestHeader("Content-Type", file.type || "application/octet-stream");
      xhr.upload.addEventListener("progress", (event) => {
        if (!event.lengthComputable) return;
        const percent = Math.round((event.loaded / event.total) * 100);
        bar.value = percent;
        status.textContent = percent < 100 ? `${percent} %` : "Réplication…";
      });
      xhr.addEventListener("load", () => {
        let data = {};
        try {
          data = JSON.parse(xhr.responseText);
        } catch {
          /* non-JSON error page */
        }
        if (xhr.status === 201 || xhr.status === 200) {
          const rep = data.file.replication;
          if (rep.under_replicated) {
            finish(`Enregistré (${rep.stored}/${rep.desired} répliques, réparation programmée)`, "status-warn");
          } else {
            finish(`Enregistré (${rep.stored}/${rep.desired} répliques)`, "status-ok");
          }
        } else {
          finish(message(data.error), "status-error");
        }
        resolve();
      });
      xhr.addEventListener("error", () => {
        finish(message("network"), "status-error");
        resolve();
      });
      xhr.send(file);
    });
  }

  async function uploadAll(files) {
    for (const file of files) {
      await uploadOne(file);
    }
    input.value = "";
    await loadFiles();
  }

  input.addEventListener("change", () => uploadAll([...input.files]));
  dropzone.addEventListener("dragover", (event) => {
    event.preventDefault();
    dropzone.classList.add("dragging");
  });
  dropzone.addEventListener("dragleave", () => dropzone.classList.remove("dragging"));
  dropzone.addEventListener("drop", (event) => {
    event.preventDefault();
    dropzone.classList.remove("dragging");
    uploadAll([...event.dataTransfer.files]);
  });
  el("refresh").addEventListener("click", loadFiles);

  // ---- cluster status (staff only) ------------------------------------------
  const HEALTH = {
    healthy: ["opérationnel", "status-ok"],
    unknown: ["inconnu", "muted"],
    unreachable: ["injoignable", "status-warn"],
    suspect: ["présumé en panne", "status-error"],
  };
  const ADMIN = { active: "actif", draining: "en retrait", disabled: "désactivé" };

  async function loadNodes() {
    if (!config.nodesUrl) return;
    const response = await fetch(config.nodesUrl, { credentials: "same-origin" }).catch(() => null);
    if (!response || !response.ok) return;
    const data = await response.json();
    el("nodes").replaceChildren(
      ...data.nodes.map((n) => {
        const [label, cls] = HEALTH[n.health] || [n.health, "muted"];
        const item = node("li");
        item.append(node("strong", { text: n.node_id }), node("br"));
        item.append(node("span", { text: label, className: `badge ${cls}` }), node("br"));
        const free = n.disk_free_bytes == null ? "espace libre inconnu" : `${formatSize(n.disk_free_bytes)} libres`;
        item.append(node("small", { text: `${ADMIN[n.admin_state] || n.admin_state} · ${free}`, className: "muted" }));
        return item;
      }),
    );
  }

  loadFiles();
  loadNodes();
})();
