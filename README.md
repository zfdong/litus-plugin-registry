# Litus plugin registry

This repository lists the plugins that every copy of [Litus](https://litus.tellusai.com/) offers
under **Plugins › Plugin Manager › Browse**. It holds one small entry per plugin, not the
plugins' code. Each plugin lives in its own repository.

The registry is published at `https://litus.tellusai.com/api/plugins/registry.json`.

| To… | Go to |
|---|---|
| Write a plugin | [litus-plugin](https://github.com/zfdong/litus-plugin): the SDK and the plugin author's guide |
| List a plugin for everyone | This repository: [Submitting a plugin](#submitting-a-plugin) |
| Share a plugin with your own team | No need to list it: give people the `.litusplugin` file, or host a registry of your own (see the guide) |

## Submitting a plugin

1. **Release it.** Build your plugin's `.litusplugin` package and attach it to a release of your
   plugin's repository, e.g.
   `https://github.com/you/coastal-tools/releases/download/v1.0.0/coastal-tools-1.0.0.litusplugin`.
2. **Get its SHA-256:**

   ```sh
   sha256sum coastal-tools-1.0.0.litusplugin                         # Linux
   shasum -a 256 coastal-tools-1.0.0.litusplugin                     # macOS
   (Get-FileHash coastal-tools-1.0.0.litusplugin).Hash.ToLower()     # Windows PowerShell
   ```

3. **Add `registry/<id>.json`**, named after the `id` in your `plugin.json`:

   ```json
   {
     "id": "coastal-tools",
     "source": {
       "url": "https://github.com/you/coastal-tools/releases/download/v1.0.0/coastal-tools-1.0.0.litusplugin",
       "sha256": "<64 lower-case hex characters>"
     },
     "categories": ["Coastal"]
   }
   ```

   That's all the entry holds. The name, version, description, permissions and translations
   that Litus shows come from the package's own `plugin.json`. `categories` is optional; it
   replaces the manifest's.

4. **Check it**, with Python 3.9 or later and nothing to install:

   ```sh
   python scripts/build.py --check coastal-tools
   ```

   It downloads the package, checks it against the hash, and refuses anything Litus would
   skip.

5. **Open a pull request.** The checks run on it, and a maintainer reviews it.

### Updating a plugin

Release the new version, then open a pull request that changes the entry's `url` and `sha256`.
A published package never changes: a new package needs a new version in `plugin.json`. Older
versions stay available for download; the registry lists the newest.

### What the review looks at

- **What the plugin does** matches its name and description, and it works: the maintainer
  installs it in Litus.
- **Permissions.** It asks only for those it uses. `network` gets a closer look: a plugin with
  it can send what it reads from the user's project to addresses of its choosing, so the
  description should say what it sends where.
- **Process plugins** (`"runtime": "process"`) aren't sandboxed: they run with the user's own
  rights. Their source code is read, so it must be public, and the package must be built from
  it.
- **The package** is attached to a release of the plugin's own repository, and its licence
  allows Litus users to use it.

A listed plugin can be withdrawn from every copy of Litus if it turns out to be harmful or
broken (see `blocklist.json`).

## How it works

- `registry/<id>.json`: the entries.
- `blocklist.json`: withdrawn plugins (every version, or one package). Litus checks it when it
  browses the registry and at startup.
- `scripts/build.py`: checks the entries and builds the published registry.
- `scripts/smoke.py`: installs plugins with a real `litus-cli`, as a user would.
- `.github/workflows/check.yml`: checks every pull request's entries (no secrets, read-only).
- `.github/workflows/deploy.yml`: publishes `main` to litus.tellusai.com.

Maintainers: see [MAINTAINERS.md](MAINTAINERS.md).
