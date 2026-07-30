# ComfyUI-ModelResolver

Detects models that are **actually** missing in the active workflow graph
(muted, bypassed and disconnected nodes are ignored) and resolves them via
Civitai, Hugging Face and manual sources — with exact-filename matching and
SHA256/MD5 verification.

Unlike the built-in "missing models" dialog, this only offers to download
what the workflow really needs, distinguishes "missing" from "present but
just named differently", and verifies every download by checksum before it
lands in your models folder.

## Features

- **Accurate analysis** — walks the active graph (reachable from outputs),
  skips muted/bypassed nodes and dead islands, and detects model references
  generically via each node's `INPUT_TYPES`, so custom-node loaders work too.
- **Multi-source resolution** — searches Civitai (with optional NSFW opt-in)
  and Hugging Face, verifies candidates by exact filename and file type, and
  de-duplicates identical files across sources by SHA256.
- **Verified downloads** — resumable (HTTP range), streaming SHA256/MD5,
  checksum verified before the file is moved into place. Civitai token and
  Hugging Face token handled per host.
- **Manual add / import** — download from any URL or import a local file
  (e.g. a browser download), with checksum verification and folder selection.
- **Present-but-renamed detection** — recognises files you already have that
  the workflow references under a browser/OS duplicate name (e.g. ` (2)`),
  and lets you verify identity by hash instead of re-downloading.

## Installation

Copy the `ComfyUI-ModelResolver` folder into `ComfyUI/custom_nodes/` and
restart ComfyUI. No extra dependencies (uses aiohttp, shipped with ComfyUI).

## Configuration

On first start a `config.json` is created in the extension folder:

- `civitai_api_key` — for Civitai downloads (create at civitai.com/user/account)
- `hf_token` — optional, only for gated Hugging Face repos
- `include_nsfw` — opt-in, searches Civitai with `nsfw=true`
- `sources` — toggle civitai / huggingface
- `max_parallel_downloads` — how many downloads run at once (1-8, default 2)

**Never commit `config.json`** — it contains your API key and is git-ignored.

## Usage

Open a workflow. The **Model Resolver** sidebar tab shows what's missing.
Click *Search sources*, pick candidates, *Download selected*. Files are
verified and placed in the correct folder.

**After a download finishes, the node is not updated automatically.** Refresh
the node list (press `R` on the canvas, or reload) and then pick the newly
downloaded file in the loader node's dropdown — the red "Error" on the node
clears once a valid file is selected. This is a ComfyUI behaviour: adding a
file to a folder doesn't change what a node currently points to.

Running downloads can be paused, resumed or cancelled from the download list.
Pausing keeps the partial file (it resumes where it left off); cancelling
discards it.

### Add manually

Some files aren't on Civitai or Hugging Face — for example models hosted only
on ModelScope, RunningHub or a mirror found via [CivArchive](https://civarchive.com).
For these, use the **Add manually** section:

- **Source** — a direct download URL, *or* a local file path (to import a file
  you already downloaded in your browser; the original is copied, not moved).
- **Target name** — the exact filename the workflow expects.
- **Folder type** — where it goes (the list is populated from your actual
  model folders, including custom-node folders; it is preselected from the
  missing entry when possible).
- **Checksum** *(optional)* — an MD5 or SHA256 (auto-detected by length).
  If given, the download is verified against it before being placed; a
  mismatch is rejected and nothing broken lands in your folder.

When a search comes up empty, the panel also offers direct **CivArchive** and
**RunningHub** search links for the distilled filename, so you can grab the URL
and checksum and drop them straight into *Add manually*.

### Already have the file under a different name?

If the workflow references a file you already have but under a browser/OS
duplicate name (e.g. `model (2).safetensors`), it appears under *Present, just
named differently* rather than as missing — no download needed, just select
your existing file in the node. *Verify identity via hash* confirms it really
is the same file.

## License

MIT
