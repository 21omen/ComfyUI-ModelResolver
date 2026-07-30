// ComfyUI-ModelResolver – Frontend
// Sidebar panel: scan -> confirm candidates -> download with progress.
// Actively missing models (backend analysis) are kept separate from
// "missing but disabled" (frontend scan over muted/bypassed nodes).

import { app } from "../../scripts/app.js";

// -------------------------------------------------------------- strings --

const STRINGS = {
    panel_title: "Model Resolver",
    panel_tooltip: "Find and download missing models",
    check_workflow: "Check workflow",
    missing_active: (n) => `Missing in active graph (${n})`,
    search_sources: "Search sources",
    download_selected: "Download selected",
    not_found_queries: "Not found. Queries: ",
    source_error: "Source error: ",
    search_manually: "Search manually: ",
    enter_below: "Then enter the result under \u201CAdd manually\u201C below.",
    folder_unknown: "folder unknown",
    found_local: (f) => `Found locally as: ${f}`,
    renamed_head: (n) => `Present, just named differently (${n})`,
    available_as: (f) => `Present as: ${f}`,
    no_download_select: "No download needed \u2013 just select this file in the node.",
    verify_hash: "Verify identity via hash",
    verifying_local: "Verifying\u2026 (hashing local file)",
    no_online_hash: "No online source found for hash comparison. Local SHA256: ",
    inactive_head: (n) => `Missing, but disabled (${n})`,
    inactive_node: (node) => `Node: ${node} (muted/bypassed \u2013 no download needed)`,
    none_missing: "No missing models in active graph (or not checked yet).",
    none_missing_see_renamed:
        "No missing models \u2013 see \u201Cpresent, named differently\u201C below.",
    add_manually: "Add manually",
    ph_source: "Source: download URL or local path",
    ph_target: "Target name, e.g. my_lora.safetensors",
    ph_folder: "Folder type",
    ph_checksum: "Checksum (optional, MD5 or SHA256)",
    download_import: "Download / Import",
    required_fields: "Source, target name and folder type are required.",
    checksum_unclear: "Checksum unclear: 32 hex chars = MD5, 64 = SHA256.",
    enqueued: "Enqueued.",
    downloads: "Downloads",
    pause: "Pause",
    cancel: "Cancel",
    resume: "Resume",
    paused_label: "paused",
    cancelled_label: "cancelled",
    done: "done",
    checksum_ok: " \u2713 checksum",
    error: "Error",
    weitere_quellen: (n) => ` +${n} more source(s)`,
    scan_failed: "Scan failed: ",
    search_failed: "Search failed: ",
    download_failed: "Download failed: ",
    toast_missing: (n) => `${n} missing model(s) in active graph`,
};

const t = (key, ...args) => {
    const s = STRINGS[key] ?? key;
    return typeof s === "function" ? s(...args) : s;
};

const MODEL_EXT = /\.(safetensors|sft|ckpt|pt|pth|bin|gguf|onnx)$/i;
const fmtMB = (b) => (b ? `${Math.round(b / 1048576)} MB` : "?");

const state = {
    missing: [],      // actively missing (backend)
    renamed: [],      // present locally, just named differently (backend)
    inactive: [],     // missing but disabled (frontend scan)
    resolved: {},     // filename -> resolve result
    selected: {},     // filename -> chosen candidate index
    verify: {},       // filename -> verification result
    folders: null,    // real folder types from backend
    pollTimer: null,
    el: null,
};

// ---------------------------------------------------------------- Backend --

async function apiPost(path, body) {
    const res = await fetch(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    return data;
}

async function runScan() {
    const p = await app.graphToPrompt();
    const prompt = p.output ?? p;
    const result = await apiPost("/modelresolver/analyze", { prompt });
    state.missing = result.missing || [];
    state.renamed = result.available_renamed || [];
    state.inactive = scanInactiveNodes();
    state.resolved = {};
    state.selected = {};
    state.verify = {};
    return result;
}

function scanInactiveNodes() {
    // muted (2) / bypassed (4) Nodes: Modell-Widgets, deren Wert nicht in
    // der aktuellen Optionsliste steht -> fehlt, aber gerade deaktiviert.
    const out = [];
    for (const node of app.graph?._nodes || []) {
        if (node.mode !== 2 && node.mode !== 4) continue;
        for (const w of node.widgets || []) {
            const vals = w.options?.values;
            if (!Array.isArray(vals)) continue;
            if (
                typeof w.value === "string" &&
                MODEL_EXT.test(w.value) &&
                !vals.includes(w.value)
            ) {
                out.push({ filename: w.value, node: node.title || node.type });
            }
        }
    }
    return out;
}

async function runResolve() {
    const data = await apiPost("/modelresolver/resolve", { missing: state.missing });
    for (const r of data.results || []) {
        state.resolved[r.filename] = r;
        if (r.candidates?.length) state.selected[r.filename] = 0;
    }
}

async function runDownload() {
    const jobs = [];
    for (const [fname, idx] of Object.entries(state.selected)) {
        const r = state.resolved[fname];
        const c = r?.candidates?.[idx];
        if (!c) continue;
        jobs.push({
            filename: c.file_name,
            folder_type: r.folder_type,
            download_url: c.download_url,
            sha256: c.sha256,
            size_bytes: c.size_bytes,
            source: c.source,
        });
    }
    if (!jobs.length) return;
    await apiPost("/modelresolver/download", { jobs });
    startPolling();
}

function startPolling() {
    stopPolling();
    state.pollTimer = setInterval(async () => {
        try {
            const res = await fetch("/modelresolver/download/status");
            const data = await res.json();
            renderJobs(data.jobs || []);
            const active = (data.jobs || []).some((j) =>
                ["queued", "downloading", "importing", "verifying"].includes(j.status)
            );
            if (!active) {
                stopPolling();
                await app.refreshComboInNodes?.();
                render(); // Neu scannen waere praeziser; Refresh reicht fuers UI
            }
        } catch (e) {
            console.warn("[ModelResolver] status poll failed", e);
        }
    }, 1500);
}

function stopPolling() {
    if (state.pollTimer) clearInterval(state.pollTimer);
    state.pollTimer = null;
}

// -------------------------------------------------------------------- UI --

function h(tag, attrs = {}, ...children) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
        if (k === "style") Object.assign(el.style, v);
        else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
        else el.setAttribute(k, v);
    }
    for (const c of children.flat()) {
        if (c == null) continue;
        el.append(c.nodeType ? c : document.createTextNode(c));
    }
    return el;
}

const S = {
    pad: { padding: "10px", fontSize: "12px", overflowY: "auto", height: "100%" },
    btn: {
        padding: "6px 10px", margin: "2px 4px 8px 0", cursor: "pointer",
        border: "1px solid var(--border-color, #555)", borderRadius: "4px",
        background: "var(--comfy-input-bg, #333)", color: "var(--input-text, #ddd)",
    },
    card: {
        border: "1px solid var(--border-color, #444)", borderRadius: "6px",
        padding: "8px", marginBottom: "8px",
        background: "var(--comfy-menu-bg, #202020)",
    },
    head: { fontWeight: "bold", margin: "10px 0 6px" },
    mono: { fontFamily: "monospace", fontSize: "11px", wordBreak: "break-all" },
    dim: { opacity: "0.7" },
    ok: { color: "#7c7" }, warn: { color: "#cc7" }, err: { color: "#c77" },
    bar: (pct) => ({
        height: "6px", borderRadius: "3px", background: "#345",
        position: "relative", overflow: "hidden", margin: "4px 0",
    }),
    barFill: (pct) => ({
        position: "absolute", inset: "0", width: `${pct}%`,
        background: "#5a5", transition: "width .5s",
    }),
};

function confBadge(c) {
    const map = { high: S.ok, medium: S.warn, low: S.err };
    return h("span", { style: { ...map[c.confidence], marginLeft: "6px" } },
        `[${c.confidence}]`);
}

async function sendControl(jobId, action) {
    try {
        await apiPost("/modelresolver/download/control",
            { job_id: jobId, action });
        startPolling();  // refresh status view
    } catch (err) {
        console.warn("[ModelResolver] control failed:", err);
    }
}

function jobControls(j) {
    const smallBtn = { ...S.btn, padding: "2px 8px", margin: "4px 4px 0 0",
        fontSize: "11px" };
    const active = ["queued", "downloading", "importing", "verifying"];
    const btns = [];
    if (active.includes(j.status)) {
        btns.push(h("button", { style: smallBtn,
            onclick: () => sendControl(j.id, "pause") }, t("pause")));
        btns.push(h("button", { style: smallBtn,
            onclick: () => sendControl(j.id, "cancel") }, t("cancel")));
    } else if (j.status === "paused") {
        btns.push(h("button", { style: smallBtn,
            onclick: () => sendControl(j.id, "resume") }, t("resume")));
        btns.push(h("button", { style: smallBtn,
            onclick: () => sendControl(j.id, "cancel") }, t("cancel")));
    }
    return btns.length ? h("div", {}, ...btns) : null;
}

function renderJobs(jobs) {
    const box = state.el?.querySelector("#mr-jobs");
    if (!box) return;
    box.replaceChildren(
        h("div", { style: S.head }, t("downloads")),
        ...jobs.map((j) => {
            const pct = j.bytes_total
                ? Math.round((j.bytes_done / j.bytes_total) * 100) : 0;
            const st =
                j.status === "done" ? h("span", { style: S.ok },
                    `${t("done")}${j.checksum_verified || j.sha256_verified ? t("checksum_ok") : ""}`)
                : j.status === "error" ? h("span", { style: S.err }, j.error || t("error"))
                : j.status === "cancelled" ? h("span", { style: S.dim }, t("cancelled_label"))
                : j.status === "paused" ? h("span", { style: S.warn }, `${t("paused_label")} ${pct}%`)
                : h("span", { style: S.dim }, `${j.status} ${pct}%`);
            return h("div", { style: S.card },
                h("div", { style: S.mono }, j.filename),
                h("div", { style: S.bar() }, h("div", { style: S.barFill(pct) })),
                st,
                jobControls(j));
        })
    );
}

function render() {
    const el = state.el;
    if (!el) return;
    el.replaceChildren();
    el.append(
        h("button", { style: S.btn, onclick: async (e) => {
            e.target.disabled = true;
            try { await runScan(); render(); }
            catch (err) { alert(t("scan_failed") + err.message); }
            finally { e.target.disabled = false; }
        }}, t("check_workflow")),
    );

    if (state.missing.length) {
        el.append(h("div", { style: S.head },
            t("missing_active", state.missing.length)));
        for (const m of state.missing) {
            const r = state.resolved[m.filename];
            const card = h("div", { style: S.card },
                h("div", { style: S.mono }, m.filename),
                h("div", { style: S.dim }, `→ ${m.folder_type ?? t("folder_unknown")}`),
            );
            if (r) {
                if (r.status === "resolved") {
                    r.candidates.forEach((c, i) => {
                        card.append(h("label", { style: { display: "block", margin: "4px 0" } },
                            h("input", {
                                type: "radio", name: `mr-${m.filename}`,
                                ...(state.selected[m.filename] === i ? { checked: "" } : {}),
                                onchange: () => { state.selected[m.filename] = i; },
                            }),
                            ` ${c.source}: ${c.model_name} (${fmtMB(c.size_bytes)})`,
                            confBadge(c),
                            c.also_available?.length
                                ? h("span", { style: S.dim },
                                    t("weitere_quellen", c.also_available.length)) : null,
                        ));
                    });
                } else if (r.status === "not_found") {
                    card.append(h("div", { style: S.warn },
                        t("not_found_queries") + r.queries_tried.join(" | ")));
                    // Use the distilled query (no extension) — CivArchive's
                    // filename search returns nothing for "...safetensors".
                    const q = (r.queries_tried && r.queries_tried[0])
                        || m.filename.split(/[\\/]/).pop().replace(/\.[^.]+$/, "");
                    card.append(h("div", { style: { marginTop: "4px" } },
                        t("search_manually"),
                        h("a", {
                            href: `https://civarchive.com/search?q=${encodeURIComponent(q)}`,
                            target: "_blank", style: { marginRight: "10px" },
                        }, "CivArchive"),
                        h("a", {
                            href: `https://www.runninghub.ai/models?keyword=${encodeURIComponent(q)}`,
                            target: "_blank",
                        }, "RunningHub"),
                        h("div", { style: S.dim },
                            t("enter_below")),
                    ));
                } else {
                    card.append(h("div", { style: S.err },
                        t("source_error") + (r.errors || []).join("; ")));
                }
            }
            el.append(card);
        }
        const hasCandidates = state.missing.some(
            (m) => state.resolved[m.filename]?.candidates?.length
        );
        const buttons = [
            h("button", { style: S.btn, onclick: async (e) => {
                e.target.disabled = true;
                try { await runResolve(); render(); }
                catch (err) { alert(t("search_failed") + err.message); }
                finally { e.target.disabled = false; }
            }}, t("search_sources")),
        ];
        if (hasCandidates) {
            buttons.push(h("button", { style: S.btn, onclick: async () => {
                try { await runDownload(); }
                catch (err) { alert(t("download_failed") + err.message); }
            }}, t("download_selected")));
        }
        el.append(...buttons);
    } else {
        el.append(h("div", { style: { ...S.dim, margin: "8px 0" } },
            state.renamed.length
                ? t("none_missing_see_renamed")
                : t("none_missing")));
    }

    if (state.renamed.length) {
        el.append(h("div", { style: S.head },
            t("renamed_head", state.renamed.length)));
        for (const m of state.renamed) {
            const v = state.verify[m.filename];
            const card = h("div", { style: S.card },
                h("div", { style: S.mono }, m.filename),
                h("div", { style: S.ok },
                    t("available_as", m.available_as.filename)),
                h("div", { style: S.dim },
                    t("no_download_select")),
            );
            if (v) {
                const vs = v.status === "verified" ? S.ok
                    : v.status === "mismatch" ? S.err : S.warn;
                card.append(h("div", { style: { ...vs, marginTop: "4px" } }, v.message));
            }
            card.append(h("button", { style: S.btn, onclick: async (e) => {
                e.target.disabled = true;
                e.target.textContent = t("verifying_local");
                try {
                    // Online-Hash beschaffen, dann lokal vergleichen
                    const r = await apiPost("/modelresolver/resolve",
                        { missing: [{ filename: m.filename, folder_type: m.folder_type }] });
                    const cand = r.results?.[0]?.candidates?.[0];
                    const body = {
                        folder_type: m.available_as.folder_type,
                        local_filename: m.available_as.filename,
                    };
                    if (cand?.sha256) body.sha256 = cand.sha256;
                    const res = await apiPost("/modelresolver/verify", body);
                    if (!cand?.sha256 && res.status === "hashed") {
                        res.message = t("no_online_hash") + res.sha256.slice(0, 16) + "…";
                    }
                    state.verify[m.filename] = res;
                    render();
                } catch (err) {
                    state.verify[m.filename] =
                        { status: "mismatch", message: `${t("error")}: ${err.message}` };
                    render();
                }
            }}, t("verify_hash")));
            el.append(card);
        }
    }

    if (state.inactive.length) {
        el.append(h("div", { style: S.head },
            t("inactive_head", state.inactive.length)));
        for (const m of state.inactive) {
            el.append(h("div", { style: { ...S.card, ...S.dim } },
                h("div", { style: S.mono }, m.filename),
                h("div", {}, t("inactive_node", m.node))));
        }
    }

    el.append(renderManualSection());
    el.append(h("div", { id: "mr-jobs" }));
}

const FOLDER_TYPES_FALLBACK = [
    "loras", "checkpoints", "diffusion_models", "vae", "controlnet",
    "upscale_models", "text_encoders", "embeddings", "clip_vision", "ipadapter",
];

async function loadFolders() {
    try {
        const res = await fetch("/modelresolver/folders");
        const data = await res.json();
        if (Array.isArray(data.folders) && data.folders.length) {
            state.folders = data.folders;
            if (state.el) render();  // refresh dropdown if panel already open
        }
    } catch (e) {
        console.warn("[ModelResolver] folder list unavailable, using fallback", e);
    }
}

function renderManualSection() {
    // Manual job: URL or local path -> downloader verifies + files it.
    // Prefill folder type from the first still-missing entry if any.
    const folderList = state.folders?.length ? state.folders : FOLDER_TYPES_FALLBACK;
    const prefill = state.missing.find((m) => m.folder_type)?.folder_type
        || (folderList.includes("loras") ? "loras" : folderList[0]);
    const inp = (ph, attrs = {}) => h("input", {
        placeholder: ph, style: {
            display: "block", width: "100%", margin: "3px 0", padding: "4px",
            background: "var(--comfy-input-bg, #333)",
            color: "var(--input-text, #ddd)",
            border: "1px solid var(--border-color, #555)", borderRadius: "3px",
            boxSizing: "border-box", fontSize: "11px",
        }, ...attrs,
    });
    const src = inp(t("ph_source"));
    const name = inp(t("ph_target"));
    // Real <select> instead of <datalist>: always shows the full list and
    // highlights the preselected type (datalist filters by current input).
    const folder = h("select", {
        style: {
            display: "block", width: "100%", margin: "3px 0", padding: "4px",
            background: "var(--comfy-input-bg, #333)",
            color: "var(--input-text, #ddd)",
            border: "1px solid var(--border-color, #555)", borderRadius: "3px",
            boxSizing: "border-box", fontSize: "11px",
        },
    }, ...folderList.map((f) => h("option",
        f === prefill ? { value: f, selected: "" } : { value: f }, f)));
    const sum = inp(t("ph_checksum"));
    const msg = h("div", { style: { ...S.dim, minHeight: "14px" } });

    return h("div", {},
        h("div", { style: S.head }, t("add_manually")),
        h("div", { style: S.card },
            src, name, folder, sum,
            h("button", { style: S.btn, onclick: async () => {
                const s = src.value.trim();
                const n = name.value.trim();
                const f = folder.value.trim();
                const c = sum.value.trim().toLowerCase();
                if (!s || !n || !f) {
                    msg.textContent = t("required_fields");
                    return;
                }
                const job = { filename: n, folder_type: f, source: "manual" };
                if (/^https?:\/\//i.test(s)) job.download_url = s;
                else job.source_path = s;
                if (/^[0-9a-f]{64}$/.test(c)) job.sha256 = c;
                else if (/^[0-9a-f]{32}$/.test(c)) job.md5 = c;
                else if (c) {
                    msg.textContent =
                        t("checksum_unclear");
                    return;
                }
                try {
                    await apiPost("/modelresolver/download", { jobs: [job] });
                    msg.textContent = t("enqueued");
                    startPolling();
                } catch (err) {
                    msg.textContent = `${t("error")}: ${err.message}`;
                }
            }}, t("download_import")),
            msg,
        ),
    );
}

// -------------------------------------------------------- Registrierung --

app.registerExtension({
    name: "ModelResolver",

    async setup() {
        try {
            const res = await fetch("/modelresolver/ping");
            const data = await res.json();
            console.log(`[ModelResolver] backend reachable: v${data.version}`);
        } catch (err) {
            console.warn("[ModelResolver] backend ping failed:", err);
        }
        loadFolders();

        const sidebar = app.extensionManager?.registerSidebarTab;
        if (sidebar) {
            app.extensionManager.registerSidebarTab({
                id: "modelResolver",
                icon: "pi pi-download",
                title: t("panel_title"),
                tooltip: t("panel_tooltip"),
                type: "custom",
                render: (el) => {
                    state.el = el;
                    Object.assign(el.style, S.pad);
                    try {
                        render();
                    } catch (e) {
                        console.error("[ModelResolver] render failed:", e);
                        el.textContent = "Model Resolver: render error (see console).";
                    }
                },
            });
        } else {
            console.warn("[ModelResolver] sidebar API unavailable – frontend too old? Panel not shown.");
        }
    },

    async afterConfigureGraph() {
        // Auto-Check nach dem Laden eines Workflows
        try {
            await runScan();
            if (state.el) render();
            if (state.missing.length) {
                const toast = app.extensionManager?.toast;
                toast?.add?.({
                    severity: "warn",
                    summary: t("panel_title"),
                    detail: t("toast_missing", state.missing.length),
                    life: 6000,
                });
            }
        } catch (e) {
            console.warn("[ModelResolver] auto-scan failed:", e);
        }
    },
});
