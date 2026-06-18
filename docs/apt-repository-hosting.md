# Hosting the `.deb` packages in an APT repository

> Status: **idea / not implemented.** Captured for a future revision. The app
> already ships the AppStream metadata this would consume (see
> `scripts/build-linux-installer.sh`, the `--deb` path) — this doc is about the
> *distribution* side that turns that metadata into a rich storefront page.

## Why we'd do this — the problem it solves

When you open a downloaded `.deb` directly in GNOME **App Center** (the
pre-install page), it shows the raw package name (`puckutilityapp`), **"Unknown
publisher"**, no license, and a generic icon — even though the `.deb` embeds a
valid AppStream metainfo file, a multi-size icon, and a `Maintainer:` field.

That is **not** something we can fix from inside the `.deb`. That page is drawn
by PackageKit reading the Debian `control` file and then matching the package
against the **system's AppStream _catalog_**. An out-of-repo, not-yet-installed
package has no catalog entry, so every "rich" field falls back to a default:

| Field on the page | Where it really comes from |
| --- | --- |
| Title (`puckutilityapp`) | package name (control), *not* the AppStream `<name>` |
| Publisher ("Unknown") | AppStream catalog `<developer>` — **not** the deb `Maintainer` |
| License ("unknown") | AppStream catalog `<project_license>` |
| Icon (generic) | AppStream catalog icon cache — App Center does **not** extract icons from an uninstalled `.deb` |

The embedded metadata still works **after install** (dock/launcher icon, the
installed-app view in App Center) — but to make the **pre-install storefront
page** rich, the package must be served from an **APT repository that ships an
AppStream catalog**. That catalog is generated from exactly the metainfo + icons
we already embed.

## The key fact: an APT repo is just static files

No database, no PHP, no special server. It's a tree of files served over
HTTP(S). Any of these work:

- a plain **nginx / Apache** directory on a web server you already run,
- **object storage** (S3, Cloudflare R2, Backblaze B2) behind a CDN,
- **GitHub Pages** / any static host,
- a **Launchpad PPA** (Canonical hosts it for free — but builds from source on
  their infra, so it needs a different, source-package build flow; see the end).

The only "active" requirements are HTTPS, a GPG signature on the repo's
`Release` file, and regenerating the indexes whenever you publish a new `.deb`.

## How much space

Almost entirely the `.deb` files; the metadata is rounding error.

| Item | Size |
| --- | --- |
| `puckutilityapp_*.deb` | ~**175 MB** (measured) |
| `pucktunerapp_*.deb` | ~**175–220 MB** (similar; has pandas/matplotlib) |
| Repo index (`Packages.gz`, `Release`, `InRelease`) | a few **KB** |
| AppStream catalog (`Components-amd64.yml.gz` + `icons-*.tar.gz`) | tens of **KB**, up to ~**1–2 MB** |

The `.deb`s are large because each is a PyInstaller one-file bundle embedding the
whole Python 3.13 runtime + wxPython + the scientific stack. (A "thin" `.deb`
that `Depends:` on system Python would be a few MB — but that reintroduces the
cross-Ubuntu dependency problems we deliberately solved, so the self-contained
~175 MB is the safe trade.)

**Planning math** — both apps, keeping the latest **3 versions** each:

```
3 versions × 2 apps × ~200 MB  ≈  1.2 GB
```

Keep only the current release of each → **~0.4 GB**. Disk grows with *retained
versions*, not downloads (download bandwidth = one `.deb` size per user, separate
from storage).

## Repository layout (pool-based, AppStream-capable)

```
repo/                                        (this is your web root, e.g. https://apt.barrett.com/)
├── pool/main/p/
│   ├── puckutilityapp/puckutilityapp_1.3.0_amd64.deb
│   └── pucktunerapp/pucktunerapp_1.0.0_amd64.deb
└── dists/stable/
    ├── Release            # index of indexes + checksums
    ├── Release.gpg        # detached signature  (apt trust)
    ├── InRelease          # inline-signed Release (preferred by modern apt)
    └── main/
        ├── binary-amd64/
        │   ├── Packages
        │   └── Packages.gz
        └── dep11/                          # the AppStream catalog (DEP-11)
            ├── Components-amd64.yml.gz      # ← makes the App Center page rich
            ├── icons-64x64.tar.gz
            └── icons-128x128.tar.gz
```

`stable` is the *suite* and `main` the *component* — pick any names; they just
have to match the client's `sources.list` line.

## Publishing workflow (starting recipe — refine when implementing)

These are the moving parts, not a finished script. Two index families get
regenerated on every publish: the **apt** indexes and the **AppStream** catalog.

### 1. Drop the new `.deb` into the pool
```bash
cp build/deb/puckutilityapp_1.3.0_amd64.deb repo/pool/main/p/puckutilityapp/
```

### 2. Generate apt indexes (`apt-ftparchive`, from the `apt-utils` package)
```bash
cd repo
apt-ftparchive packages pool > dists/stable/main/binary-amd64/Packages
gzip -kf dists/stable/main/binary-amd64/Packages
apt-ftparchive release dists/stable > dists/stable/Release
```
> Alternatives that manage the pool + signing for you: **`aptly`** or
> **`reprepro`**. Both are more ergonomic than raw `apt-ftparchive` once you have
> more than a couple of packages.

### 3. Generate the AppStream catalog (the rich-page payload)
Modern, simpler than the old `appstream-generator`: **`appstreamcli compose`**
(ships with the `appstream` package). Point it at the unpacked package tree(s);
it harvests `usr/share/metainfo/*.xml` + `usr/share/icons/...` and emits the
DEP-11 YAML + icon tarballs.
```bash
appstreamcli compose \
  --origin barrett-stable \
  --result-root dists/stable/main/dep11/ \
  --data-dir   dists/stable/main/dep11/ \
  --prefix /usr \
  /path/to/unpacked/puckutilityapp /path/to/unpacked/pucktunerapp
```
(Unpack a `.deb` with `dpkg-deb -x pkg.deb /path/to/unpacked/pkg`.) Then re-run
`apt-ftparchive release` so the new `dep11/` checksums land in `Release`.

### 4. Sign `Release` (GPG — required for apt trust)
One-time: create a signing key (`gpg --full-generate-key`, RSA 4096, no
expiry or a long one). Export its public key to ship to users:
```bash
gpg --export 'Barrett Technology <bn@barrett.com>' > repo/barrett-archive-keyring.gpg
```
Per publish:
```bash
cd repo/dists/stable
gpg --default-key 'bn@barrett.com' --clearsign  -o InRelease   Release
gpg --default-key 'bn@barrett.com' -abs         -o Release.gpg  Release
```

### 5. Upload `repo/` to the web server
Plain file sync (`rsync`, `aws s3 sync`, etc.). Done.

## What end users do (one time)

```bash
# trust the signing key
sudo install -d /etc/apt/keyrings
curl -fsSL https://apt.barrett.com/barrett-archive-keyring.gpg \
  | sudo tee /etc/apt/keyrings/barrett.gpg >/dev/null

# add the repo
echo "deb [signed-by=/etc/apt/keyrings/barrett.gpg] https://apt.barrett.com stable main" \
  | sudo tee /etc/apt/sources.list.d/barrett.list

sudo apt update
sudo apt install puckutilityapp     # or pucktunerapp
```

After `apt update`, App Center indexes the DEP-11 catalog, and searching "Puck
Utility" shows the **full** page — Barrett icon, "Barrett Technology" publisher,
BSD-2-Clause license, description — **before** install. Updates flow through the
normal `apt upgrade` / App Center update path.

## Loose ends to decide when implementing

- **Hosting target**: own web server vs. object storage + CDN vs. Launchpad PPA.
- **Icon sharpness**: the catalog icons come from the embedded PNGs, currently
  upscaled from a 48 px source. Add a crisp **256×256 (or SVG)** `BarrettIcon`
  to `images/` first so the storefront icon is sharp.
- **Repo tooling**: raw `apt-ftparchive` (shown here) vs. `aptly`/`reprepro`
  (recommended once there are multiple packages/versions to prune).
- **Multi-arch / multi-suite**: only `amd64` today; add `binary-arm64` etc. and
  per-Ubuntu suites only if needed.
- **CI**: fold steps 1–5 into the release pipeline so publishing is one command.

## The Launchpad PPA alternative (no hosting)

Canonical hosts the repo for free and end users add it with
`add-apt-repository ppa:barrett/...`. Trade-off: PPAs **build from source** on
Launchpad's builders, so you'd need a Debian *source* package (`debian/` dir,
`debhelper` rules) instead of the PyInstaller `.deb` we build now — a different
build flow. Good if you want zero hosting and don't mind source-package
packaging; not a drop-in for the current pipeline.
