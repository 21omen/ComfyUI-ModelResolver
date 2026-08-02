// ComfyUI-ModelResolver – Frontend
// Sidebar panel: scan -> confirm candidates -> download with progress.
// Actively missing models (backend analysis) are kept separate from
// "missing but disabled" (frontend scan over muted/bypassed nodes).

import { app } from "../../scripts/app.js";

// -------------------------------------------------------------- strings --

const STRINGS = {
    panel_title: "Model Resolver",
    // Sidebar rail is narrow and clips long labels mid-word.
    // Keep this short; the full name lives in the tooltip.
    tab_title: "Resolver",
    panel_tooltip: "Find and download missing models and custom nodes",
    check_workflow: "Check workflow",
    checking: "Checking\u2026",
    searching: "Searching\u2026",
    starting_download: "Starting\u2026",
    importing: "Importing\u2026",
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
    nc_head: (n) => `Not connected \u2013 not executed (${n})`,
    nc_node: (type, id) => `Node: ${type} (#${id})`,
    nc_missing:
        "Not found locally, but this node has no path to an output \u2013 it will "
        + "not run. ComfyUI still marks it red. Connect it to download it.",
    nc_present:
        "Present locally. This node has no path to an output, so it will not run.",
    not_checked: "Not checked yet \u2013 press \u201CCheck workflow\u201C.",
    none_missing: "No missing models in active graph.",
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
    missing_node_packs: (n) => `Missing custom-node packs (${n})`,
    provides_nodes: (nodes) => `Provides: ${nodes.join(", ")}`,
    registry_version: (v) => `Comfy Registry version: ${v}`,
    workflow_versions: (v) => `Workflow metadata: ${v.join(", ")}`,
    install_node_pack: "Install node pack",
    installing_node_pack: "Installing pack + requirements\u2026",
    confirm_node_install: (name) =>
        `Install ${name} from the Comfy Registry? Its code will be added to custom_nodes and its requirements.txt will be installed into ComfyUI's Python environment.`,
    node_installed: (v) => `Installed${v ? ` v${v}` : ""}. Restart ComfyUI to load it.`,
    requirements_installed: "requirements.txt installed successfully.",
    requirements_missing: "No requirements.txt in this pack.",
    requirements_failed: "The pack was downloaded, but requirements.txt failed:",
    unresolved_nodes: (n) => `Missing nodes without a Registry match (${n})`,
    no_registry_match: "No active Comfy Registry pack matched this node class.",
    model_scan_blocked: "Model scan unavailable until the missing nodes are installed: ",
    toast_missing_both: (models, nodes) =>
        `${models} missing model(s), ${nodes} missing custom node(s)`,
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
    notConnected: [], // in prompt, but unreachable from any output (backend)
    resolved: {},     // filename -> resolve result
    selected: {},     // filename -> chosen candidate index
    verify: {},       // filename -> verification result
    nodePacks: [],    // Registry packs needed by missing workflow node classes
    unresolvedNodes: [],
    nodeInstall: {},  // pack id -> installation result
    modelScanError: null,
    folders: null,    // real folder types from backend
    scanned: false,   // true once an analysis has actually run
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
    const nodeResult = await apiPost("/modelresolver/nodes/analyze",
        { nodes: collectWorkflowNodeRefs() });
    state.nodePacks = nodeResult.packs || [];
    state.unresolvedNodes = nodeResult.unresolved || [];
    state.nodeInstall = {};
    state.modelScanError = null;
    let result = { missing: [], available_renamed: [], not_connected: [] };
    try {
        const p = await app.graphToPrompt();
        const prompt = p.output ?? p;
        result = await apiPost("/modelresolver/analyze", { prompt });
    } catch (err) {
        if (!nodeResult.missing_node_count) throw err;
        state.modelScanError = err.message;
    }
    state.missing = result.missing || [];
    state.renamed = result.available_renamed || [];
    state.inactive = scanInactiveNodes();
    state.notConnected = result.not_connected || [];
    state.resolved = {};
    state.selected = {};
    state.verify = {};
    state.scanned = true;
    return result;
}

function collectWorkflowNodeRefs() {
    const refs = [];
    const keys = new Set();
    const add = (node, path) => {
        if (!node || typeof node !== "object") return;
        const serialized = node.last_serialization || node;
        const props = serialized.properties || node.properties || {};
        const classType = serialized.type
            || props["Node name for S&R"] || node.comfyClass || node.type;
        if (typeof classType !== "string" || !classType) return;
        const item = {
            class_type: classType,
            node_id: path ? `${path}:${node.id}` : node.id,
            cnr_id: typeof props.cnr_id === "string" ? props.cnr_id : null,
            aux_id: typeof props.aux_id === "string" ? props.aux_id : null,
            version: typeof props.ver === "string" ? props.ver : null,
            frontend_available: Boolean(
                globalThis.LiteGraph?.registered_node_types?.[classType]
                    || node.constructor?.type === classType
            ),
        };
        const key = `${item.node_id ?? ""}|${item.class_type}|${item.cnr_id ?? ""}|${item.aux_id ?? ""}`;
        if (!keys.has(key)) {
            keys.add(key);
            refs.push(item);
        }
    };
    const walk = (graph, path = "") => {
        const nodes = graph?.nodes || graph?._nodes || [];
        for (const node of nodes) {
            add(node, path);
            if (node.isSubgraphNode?.() && node.subgraph) {
                const childPath = path ? `${path}:${node.id}` : String(node.id);
                walk(node.subgraph, childPath);
            }
        }
    };
    walk(app.graph);
    return refs;
}

async function installNodePack(pack) {
    if (!confirm(t("confirm_node_install", pack.name))) return;
    const result = await apiPost("/modelresolver/nodes/install", {
        pack_id: pack.id,
        ...(pack.install_version ? { version: pack.install_version } : {}),
    });
    state.nodeInstall[pack.id] = result;
    render();
}

function scanInactiveNodes() {
    // muted (2) / bypassed (4) nodes: model widgets whose value is not in
    // the current options list -> missing, but currently disabled.
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

// Inline styles cannot express :hover/:active/:disabled, so a button gave no
// feedback at all on press - it even kept the pointer cursor while disabled.
// One stylesheet, injected once, covers those states.
function ensureStyles() {
    if (document.getElementById("mr-styles")) return;
    const st = document.createElement("style");
    st.id = "mr-styles";
    st.textContent = `
.mr-btn { transition: filter .1s ease, transform .05s ease; }
.mr-btn:hover:not(:disabled) { filter: brightness(1.3); }
.mr-btn:active:not(:disabled) { transform: translateY(1px); filter: brightness(.85); }
.mr-btn:disabled { opacity: .55; cursor: progress !important; filter: none; }
`;
    document.head.append(st);
}

// Async button with a visible busy state: disabled + label swap, restored
// afterwards. Guards against double submits while the request is in flight.
function busyBtn(label, busyLabel, fn) {
    return h("button", { class: "mr-btn", style: S.btn, onclick: async (e) => {
        const b = e.currentTarget;
        if (b.disabled) return;
        const prev = b.textContent;
        b.disabled = true;
        if (busyLabel) b.textContent = busyLabel;
        try { await fn(e); }
        finally { b.disabled = false; b.textContent = prev; }
    }}, label);
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
        btns.push(h("button", { class: "mr-btn", style: smallBtn,
            onclick: () => sendControl(j.id, "pause") }, t("pause")));
        btns.push(h("button", { class: "mr-btn", style: smallBtn,
            onclick: () => sendControl(j.id, "cancel") }, t("cancel")));
    } else if (j.status === "paused") {
        btns.push(h("button", { class: "mr-btn", style: smallBtn,
            onclick: () => sendControl(j.id, "resume") }, t("resume")));
        btns.push(h("button", { class: "mr-btn", style: smallBtn,
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
    ensureStyles();
    el.replaceChildren();
    el.append(
        busyBtn(t("check_workflow"), t("checking"), async () => {
            try { await runScan(); render(); }
            catch (err) { alert(t("scan_failed") + err.message); }
        }),
    );

    if (state.nodePacks.length) {
        el.append(h("div", { style: S.head },
            t("missing_node_packs", state.nodePacks.length)));
        for (const pack of state.nodePacks) {
            const installed = state.nodeInstall[pack.id];
            const card = h("div", { style: S.card },
                h("div", { style: { fontWeight: "bold" } }, pack.name),
                h("div", { style: S.mono }, pack.id),
                h("div", { style: S.dim }, t("provides_nodes", pack.node_types)),
                pack.latest_version
                    ? h("div", { style: S.dim }, t("registry_version", pack.latest_version))
                    : null,
                pack.requested_versions?.length
                    ? h("div", { style: S.dim },
                        t("workflow_versions", pack.requested_versions))
                    : null,
                pack.repository
                    ? h("a", {
                        href: pack.repository,
                        target: "_blank",
                        rel: "noopener noreferrer",
                    }, pack.repository)
                    : null,
            );
            if (installed) {
                const req = installed.requirements || {};
                card.append(h("div", {
                    style: installed.status === "requirements_failed" ? S.err : S.ok,
                }, t("node_installed", installed.version)));
                if (req.status === "installed") {
                    card.append(h("div", { style: S.ok }, t("requirements_installed")));
                } else if (req.status === "not_present") {
                    card.append(h("div", { style: S.dim }, t("requirements_missing")));
                } else if (req.status === "failed") {
                    card.append(
                        h("div", { style: S.err }, t("requirements_failed")),
                        h("pre", { style: { ...S.mono, whiteSpace: "pre-wrap" } }, req.output || ""),
                    );
                }
            } else {
                card.append(busyBtn(
                    t("install_node_pack"),
                    t("installing_node_pack"),
                    async () => {
                        try { await installNodePack(pack); }
                        catch (err) { alert(t("error") + ": " + err.message); }
                    },
                ));
            }
            el.append(card);
        }
    }

    if (state.unresolvedNodes.length) {
        el.append(h("div", { style: S.head },
            t("unresolved_nodes", state.unresolvedNodes.length)));
        for (const node of state.unresolvedNodes) {
            el.append(h("div", { style: S.card },
                h("div", { style: S.mono }, node.class_type),
                h("div", { style: S.warn }, node.reason || t("no_registry_match"))));
        }
    }

    if (state.modelScanError) {
        el.append(h("div", { style: { ...S.warn, margin: "8px 0" } },
            t("model_scan_blocked") + state.modelScanError));
    }

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
            busyBtn(t("search_sources"), t("searching"), async () => {
                try { await runResolve(); render(); }
                catch (err) { alert(t("search_failed") + err.message); }
            }),
        ];
        if (hasCandidates) {
            buttons.push(busyBtn(t("download_selected"), t("starting_download"),
                async () => {
                    try { await runDownload(); }
                    catch (err) { alert(t("download_failed") + err.message); }
                }));
        }
        el.append(...buttons);
    } else {
        el.append(h("div", { style: { ...S.dim, margin: "8px 0" } },
            !state.scanned
                ? t("not_checked")
                : state.renamed.length
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
            card.append(h("button", { class: "mr-btn", style: S.btn, onclick: async (e) => {
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

    if (state.notConnected.length) {
        el.append(h("div", { style: S.head },
            t("nc_head", state.notConnected.length)));
        for (const m of state.notConnected) {
            el.append(h("div", { style: { ...S.card, ...S.dim } },
                h("div", { style: S.mono }, m.filename),
                h("div", {}, t("nc_node", m.class_type, m.node_id)),
                h("div", { style: m.status === "missing" ? S.warn : S.dim },
                    m.status === "missing" ? t("nc_missing") : t("nc_present"))));
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
            busyBtn(t("download_import"), t("importing"), async () => {
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
            }),
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
                title: t("tab_title"),
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
            const missingNodes = state.nodePacks.reduce(
                (count, pack) => count + pack.node_types.length, 0
            ) + state.unresolvedNodes.length;
            if (state.missing.length || missingNodes) {
                const toast = app.extensionManager?.toast;
                toast?.add?.({
                    severity: "warn",
                    summary: t("panel_title"),
                    detail: missingNodes
                        ? t("toast_missing_both", state.missing.length, missingNodes)
                        : t("toast_missing", state.missing.length),
                    life: 6000,
                });
            }
        } catch (e) {
            console.warn("[ModelResolver] auto-scan failed:", e);
        }
    },
});
