// NodeMesh file manager.
// Security rule for this file: user-controlled data (file names, types...) is
// only ever rendered with textContent / DOM APIs. Never innerHTML, never inline
// handlers (stored-XSS regression guard, enforced by a test).
"use strict";

(() => {
  const SVG_NS = "http://www.w3.org/2000/svg";
  const PAGE_SIZE = 50;
  const config = document.getElementById("config").dataset;
  const csrfToken = document.querySelector('meta[name="csrf-token"]').content;
  const maxUpload = Number(config.maxUpload) || 0;
  const $ = (id) => document.getElementById(id);

  const MESSAGES = {
    authentication_required: "Votre session a expiré. Veuillez vous reconnecter.",
    file_too_large: "Fichier trop volumineux.",
    replication_quorum_not_met: "Pas assez de nœuds de stockage disponibles : fichier non enregistré.",
    no_replica_available: "Aucune réplique n'est disponible pour le moment. Réessayez plus tard.",
    upload_in_progress: "Un envoi identique est déjà en cours.",
    idempotency_key_reused: "Conflit d'envoi : réessayez.",
    not_found: "Ce fichier n'existe plus.",
    preview_unsupported: "L'aperçu n'est pas disponible pour ce type de fichier.",
    preview_too_large: "Ce fichier est trop volumineux pour un aperçu.",
    network: "Le serveur est injoignable. Vérifiez votre connexion.",
  };
  const message = (code) => MESSAGES[code] || "Une erreur inattendue est survenue.";

  // ---- small DOM helpers ------------------------------------------------------
  function h(tag, props = {}, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(props)) {
      if (value === undefined || value === null || value === false) continue;
      if (key === "text") node.textContent = value;
      else if (key === "className") node.className = value;
      else node.setAttribute(key, value === true ? "" : value);
    }
    node.append(...children.filter(Boolean));
    return node;
  }

  function icon(name, className = "") {
    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("class", `icon ${className}`.trim());
    svg.setAttribute("aria-hidden", "true");
    const use = document.createElementNS(SVG_NS, "use");
    use.setAttribute("href", `#i-${name}`);
    svg.append(use);
    return svg;
  }

  const toastNode = $("toast");
  let toastTimer;
  function toast(text) {
    toastNode.textContent = text;
    toastNode.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => (toastNode.hidden = true), 4000);
  }

  // ---- formatting ---------------------------------------------------------------
  const dateFormat = new Intl.DateTimeFormat("fr-FR", { dateStyle: "medium", timeStyle: "short" });
  const shortDate = new Intl.DateTimeFormat("fr-FR", { day: "numeric", month: "short" });

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

  const TYPES = [
    { kind: "image", icon: "image", label: "Image", ext: ["jpg", "jpeg", "png", "gif", "webp", "svg", "bmp", "tif", "tiff", "heic", "avif"], mime: /^image\// },
    { kind: "pdf", icon: "file-pdf", label: "PDF", ext: ["pdf"], mime: /^application\/pdf$/ },
    { kind: "sheet", icon: "sheet", label: "Tableur", ext: ["csv", "tsv", "xls", "xlsx", "ods"], mime: /spreadsheet|excel|csv/ },
    { kind: "archive", icon: "archive", label: "Archive", ext: ["zip", "tar", "gz", "tgz", "bz2", "xz", "7z", "rar"], mime: /zip|tar|compressed|archive/ },
    { kind: "audio", icon: "audio", label: "Audio", ext: ["mp3", "wav", "flac", "ogg", "m4a", "aac"], mime: /^audio\// },
    { kind: "video", icon: "video", label: "Vidéo", ext: ["mp4", "mov", "mkv", "webm", "avi"], mime: /^video\// },
    { kind: "code", icon: "file-code", label: "Code", ext: ["json", "yaml", "yml", "xml", "html", "css", "js", "ts", "py", "sh", "sql", "toml", "ini", "conf"], mime: /json|xml|javascript|x-sh|x-python/ },
    { kind: "text", icon: "file-text", label: "Texte", ext: ["txt", "md", "log", "rtf"], mime: /^text\// },
    { kind: "doc", icon: "file-text", label: "Document", ext: ["doc", "docx", "odt", "ppt", "pptx", "odp", "pages", "key"], mime: /word|presentation|opendocument/ },
  ];

  function fileType(file) {
    const dot = file.name.lastIndexOf(".");
    const ext = dot > 0 ? file.name.slice(dot + 1).toLowerCase() : "";
    const type =
      TYPES.find((t) => t.ext.includes(ext)) ||
      TYPES.find((t) => t.mime.test(file.content_type)) || { kind: "file", icon: "file", label: "Fichier" };
    const suffix = ext && type.kind !== "pdf" ? ` ${ext.toUpperCase()}` : "";
    return { ...type, ext, label: `${type.label}${suffix}` };
  }

  function replicationStatus(rep, { long = false } = {}) {
    const healthy = rep.healthy ?? rep.stored ?? 0;
    const cls = rep.under_replicated ? "status status-warn" : "status status-ok";
    const label = rep.under_replicated
      ? `${healthy}/${rep.desired} · réparation en attente`
      : `${healthy}/${rep.desired} répliques`;
    const node = h("span", {
      className: cls,
      title: rep.under_replicated
        ? `Seulement ${healthy} copie(s) saine(s) sur ${rep.desired} : le cluster la recopiera automatiquement.`
        : `Répliqué sur ${rep.desired} nœuds.`,
    });
    node.append(h("span", { className: "dot" }));
    node.append(long ? label : h("span", {}, `${healthy}/${rep.desired}`, h("span", { className: "status-label", text: rep.under_replicated ? " · à réparer" : " répliques" })));
    return node;
  }

  // ---- file listing -----------------------------------------------------------------
  const list = { files: [], count: 0, loading: false };
  const tableWrap = $("files-table-wrap");
  const tbody = $("files-body");

  function showListState(state, text = "") {
    $("files-loading").hidden = state !== "loading";
    $("files-error").hidden = state !== "error";
    $("files-empty").hidden = state !== "empty";
    tableWrap.hidden = state !== "table";
    if (state === "error") $("files-error-text").textContent = text;
  }

  async function api(url, options = {}) {
    let response;
    try {
      response = await fetch(url, { credentials: "same-origin", ...options });
    } catch (error) {
      if (error.name === "AbortError") throw error;
      return { ok: false, status: 0, data: { error: "network" } };
    }
    const data = response.status === 204 ? {} : await response.json().catch(() => ({}));
    if (response.status === 401) window.location.assign("/accounts/login/?next=/");
    return { ok: response.ok, status: response.status, data };
  }

  async function loadFiles({ append = false } = {}) {
    if (list.loading) return;
    list.loading = true;
    $("refresh").disabled = true;
    $("files-more").disabled = true;
    if (!append && !list.files.length) showListState("loading");
    const offset = append ? list.files.length : 0;
    const result = await api(`${config.filesUrl}?limit=${PAGE_SIZE}&offset=${offset}`);
    list.loading = false;
    $("refresh").disabled = false;
    $("files-more").disabled = false;
    if (!result.ok) {
      showListState("error", message(result.data.error));
      return;
    }
    list.files = append ? list.files.concat(result.data.files) : result.data.files;
    list.count = result.data.count;
    renderFiles();
  }

  function renderFiles() {
    $("files-count").textContent = list.count ? `(${list.count})` : "";
    $("files-more-wrap").hidden = list.files.length >= list.count;
    if (!list.files.length) {
      showListState("empty");
      tbody.replaceChildren();
      return;
    }
    tbody.replaceChildren(...list.files.map(fileRow));
    showListState("table");
  }

  function fileRow(file) {
    const type = fileType(file);
    const created = new Date(file.created_at);
    const canPreview = Boolean(file.preview && file.preview.kind);

    const name = h("button", { type: "button", className: "file-name", title: file.name, text: file.name });
    name.addEventListener("click", () => openViewer(file, name));
    const sub = h(
      "span",
      { className: "file-sub" },
      type.label,
      h("span", { className: "mobile-only", text: ` · ${formatSize(file.size)} · ${shortDate.format(created)}` }),
    );
    const nameCell = h(
      "td",
      {},
      h("div", { className: "file-cell" }, h("span", { className: "file-icon", "data-kind": type.kind }, icon(type.icon)), h("div", { className: "file-text" }, name, sub)),
    );

    const previewButton = h("button", {
      type: "button",
      className: "btn btn-quiet btn-icon",
      "aria-label": canPreview ? `Aperçu de « ${file.name} »` : `Détails de « ${file.name} »`,
      title: canPreview ? "Aperçu" : "Détails (aperçu indisponible)",
    }, icon("eye"));
    previewButton.addEventListener("click", () => openViewer(file, previewButton));
    const download = h("a", {
      className: "btn btn-quiet btn-icon",
      href: file.download_url,
      download: "",
      "aria-label": `Télécharger « ${file.name} »`,
      title: "Télécharger",
    }, icon("download"));
    const remove = h("button", {
      type: "button",
      className: "btn btn-quiet btn-icon btn-danger",
      "aria-label": `Supprimer « ${file.name} »`,
      title: "Supprimer",
    }, icon("trash"));
    remove.addEventListener("click", () => deleteFile(file, remove));

    return h(
      "tr",
      {},
      nameCell,
      h("td", { className: "col-size", text: formatSize(file.size), title: `${file.size.toLocaleString("fr-FR")} octets` }),
      h("td", { className: "col-date" }, h("time", { datetime: file.created_at, text: dateFormat.format(created) })),
      h("td", { className: "col-rep" }, replicationStatus(file.replication)),
      h("td", { className: "col-actions" }, h("div", { className: "actions" }, previewButton, download, remove)),
    );
  }

  async function deleteFile(file, button) {
    if (!window.confirm(`Supprimer « ${file.name} » ? Cette action est définitive.`)) return;
    button.disabled = true;
    const result = await api(`${config.filesUrl}${file.id}/`, {
      method: "DELETE",
      headers: { "X-CSRFToken": csrfToken },
    });
    if (!result.ok && result.status !== 404) {
      button.disabled = false;
      toast(message(result.data.error));
      return;
    }
    list.files = list.files.filter((f) => f.id !== file.id);
    list.count = Math.max(0, list.count - 1);
    renderFiles();
    toast(`« ${file.name} » a été supprimé.`);
    $("refresh").focus();
  }

  $("refresh").addEventListener("click", () => loadFiles());
  $("files-retry").addEventListener("click", () => loadFiles());
  $("files-more").addEventListener("click", () => loadFiles({ append: true }));

  // ---- uploads (sequential; one request per file, raw body, real progress) ------------
  const input = $("file-input");
  const dropzone = $("dropzone");
  const uploads = $("uploads");
  if (maxUpload) $("limit-hint").textContent = `Jusqu'à ${formatSize(maxUpload)} par fichier`;

  // crypto.randomUUID() only exists in secure contexts; getRandomValues() works everywhere.
  function randomKey() {
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
  }

  function uploadOne(file) {
    const type = fileType({ name: file.name, content_type: file.type || "" });
    const meta = h("span", { className: "meta muted", text: "En attente…" });
    const bar = h("progress", { max: "100", value: "0", "aria-label": `Progression de « ${file.name} »` });
    const item = h(
      "li",
      { className: "upload-item" },
      h("span", { className: "file-icon", "data-kind": type.kind }, icon(type.icon)),
      h("span", { className: "name", title: file.name, text: file.name }),
      meta,
      bar,
    );
    uploads.prepend(item);

    const finish = (text, cls) => {
      meta.textContent = text;
      meta.className = `meta ${cls}`;
      bar.remove();
    };
    if (maxUpload && file.size > maxUpload) {
      finish(message("file_too_large"), "is-error");
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
        meta.textContent = percent < 100 ? `${percent} % · ${formatSize(file.size)}` : "Réplication…";
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
          if (rep.under_replicated) finish(`Enregistré · ${rep.stored}/${rep.desired} répliques, réparation programmée`, "is-warn");
          else finish(`Enregistré · ${rep.stored}/${rep.desired} répliques`, "is-ok");
        } else {
          finish(message(data.error), "is-error");
        }
        resolve();
      });
      xhr.addEventListener("error", () => {
        finish(message("network"), "is-error");
        resolve();
      });
      xhr.send(file);
    });
  }

  async function uploadAll(files) {
    if (!files.length) return;
    for (const file of files) await uploadOne(file);
    input.value = "";
    await loadFiles();
  }

  $("upload-trigger").addEventListener("click", () => input.click());
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

  // ---- viewer (native <dialog>: focus trapping and Escape come from the browser) ---------
  const viewer = $("viewer");
  const stage = $("viewer-stage");
  const openLink = $("viewer-open");
  let opener = null;
  let pending = null; // AbortController of an in-flight preview request

  function note(iconName, title, detail) {
    return h("div", { className: "viewer-note" }, icon(iconName), h("strong", { text: title }), detail ? h("span", { text: detail }) : null);
  }

  async function explainFailure(url) {
    // The <img> error event carries no status: ask once more to read the error code.
    if (!pending) return;
    const result = await api(url, { signal: pending.signal }).catch(() => null);
    if (!result || !viewer.open) return;
    const detail = result.ok ? "L'image n'a pas pu être affichée par le navigateur." : message(result.data.error);
    stage.replaceChildren(note("alert", "Aperçu indisponible", detail));
  }

  function renderPreview(file) {
    const kind = file.preview && file.preview.kind;
    const url = file.preview && file.preview.url;
    openLink.hidden = !kind;
    if (kind) openLink.href = url;

    if (!kind) {
      const reason = file.preview && file.preview.unavailable_reason;
      stage.replaceChildren(note(fileType(file).icon, "Aucun aperçu", message(reason || "preview_unsupported")));
      return;
    }
    if (kind === "pdf") {
      stage.replaceChildren(
        note("file-pdf", "Document PDF", "Le PDF s'ouvre dans le lecteur de votre navigateur, dans un nouvel onglet."),
      );
      return;
    }
    stage.replaceChildren(note("eye", "Chargement de l'aperçu…"));
    if (kind === "image") {
      const img = h("img", { alt: `Aperçu de ${file.name}`, decoding: "async" });
      img.addEventListener("load", () => viewer.open && stage.replaceChildren(img));
      img.addEventListener("error", () => viewer.open && explainFailure(url));
      img.src = url;
      return;
    }
    // text
    fetch(url, { credentials: "same-origin", signal: pending.signal })
      .then(async (response) => {
        if (!response.ok) {
          const data = await response.json().catch(() => ({}));
          stage.replaceChildren(note("alert", "Aperçu indisponible", message(data.error)));
          return;
        }
        const text = await response.text();
        stage.replaceChildren(h("pre", { tabindex: "0", "aria-label": `Contenu de ${file.name}`, text }));
      })
      .catch((error) => {
        if (error.name !== "AbortError") stage.replaceChildren(note("alert", "Aperçu indisponible", message("network")));
      });
  }

  function renderMeta(file) {
    const type = fileType(file);
    const rows = [
      ["Type", h("span", {}, type.label, h("span", { className: "file-sub mono", text: file.content_type }))],
      ["Taille", `${formatSize(file.size)} (${file.size.toLocaleString("fr-FR")} octets)`],
      ["Ajouté le", dateFormat.format(new Date(file.created_at))],
      ["Réplication", replicationStatus(file.replication, { long: true })],
      ["Empreinte SHA-256", h("span", { className: "mono", text: file.sha256 })],
    ];
    $("viewer-meta").replaceChildren(...rows.map(([term, value]) => h("div", {}, h("dt", { text: term }), h("dd", {}, value))));
  }

  function openViewer(file, trigger) {
    opener = trigger;
    pending = new AbortController();
    $("viewer-title").textContent = file.name;
    $("viewer-icon").replaceChildren(...icon(fileType(file).icon).childNodes);
    $("viewer-download").href = file.download_url;
    $("viewer-download").setAttribute("download", "");
    renderMeta(file);
    renderPreview(file);
    viewer.showModal();
  }

  viewer.addEventListener("close", () => {
    if (pending) pending.abort();
    pending = null;
    stage.replaceChildren(); // also stops an image that is still loading
    if (opener && opener.isConnected) opener.focus();
  });
  $("viewer-close").addEventListener("click", () => viewer.close());
  viewer.addEventListener("click", (event) => {
    if (event.target === viewer) viewer.close(); // click on the backdrop
  });

  // ---- storage nodes (staff only) -----------------------------------------------------------
  const HEALTH = {
    healthy: ["Opérationnel", "status-ok"],
    unknown: ["Inconnu", ""],
    unreachable: ["Injoignable", "status-warn"],
    suspect: ["Présumé en panne", "status-error"],
  };
  const ADMIN = { active: "Actif", draining: "En retrait", disabled: "Désactivé" };

  async function loadNodes() {
    if (!config.nodesUrl) return;
    const result = await api(config.nodesUrl);
    const body = $("nodes-body");
    if (!result.ok) {
      body.replaceChildren(h("tr", {}, h("td", { colspan: "5", className: "muted", text: message(result.data.error) })));
      return;
    }
    body.replaceChildren(
      ...result.data.nodes.map((n) => {
        const [label, cls] = HEALTH[n.health] || [n.health, ""];
        const status = h("span", { className: `status ${cls}` }, h("span", { className: "dot" }), label);
        return h(
          "tr",
          {},
          h("td", {}, h("span", { className: "file-cell" }, icon("server"), h("strong", { text: n.node_id }))),
          h("td", {}, status),
          h("td", { text: ADMIN[n.admin_state] || n.admin_state }),
          h("td", { className: "num", text: n.disk_free_bytes == null ? "—" : formatSize(n.disk_free_bytes) }),
          h("td", { className: "col-date", text: n.last_seen_at ? dateFormat.format(new Date(n.last_seen_at)) : "Jamais" }),
        );
      }),
    );
  }

  loadFiles();
  loadNodes();
})();
