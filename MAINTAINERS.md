# Maintaining the registry

## Reviewing a pull request

1. Wait for the **Check** workflow. It has downloaded the package, checked its hash, and made
   sure Litus would accept the entry built from its `plugin.json`.
2. Read the plugin's `plugin.json` (in the package) and, for a process plugin, its source code.
   See "What the review looks at" in the README.
3. Install it with your own `litus-cli` and try it. The pull request's branch builds a registry
   of its own, served on this computer:

   ```sh
   gh pr checkout <number>
   python scripts/smoke.py --cli path/to/litus-cli <id>
   ```

   That installs the plugin as a user would, through Litus's own checks. To use it in the
   desktop app, install the downloaded package from **Plugin Manager › Settings**. It is in
   `.cache/packages/<sha256>.litusplugin`.
4. Merge. The **Deploy** workflow publishes it, and Litus users see it within about five
   minutes (the registry is cached for five minutes).

CI can't run `litus-cli` itself: the Litus repository is private, and pull requests must never
run with secrets. Once `litus-cli` has downloadable releases, the smoke test can move into the
Check workflow.

## Deployment

This repository publishes all of **https://litus.tellusai.com/** with GitHub Pages: the Litus
landing page (`site/`, which the app's About dialog links to) and the plugin registry under
`/api/plugins/`. `/api/update.json` is reserved for an update check: don't use it for anything
else.

The **Deploy** workflow runs for every push to `main`. It:

1. checks out the `gh-pages` branch, which GitHub Pages serves;
2. copies `site/` over it;
3. runs `python scripts/build.py --site pages`, which copies new packages to
   `api/plugins/packages/<id>/<id>-<version>.litusplugin` and writes `registry.json` and
   `blocklist.json` beside them;
4. commits and pushes `gh-pages`, using the repository's own token. Pages publishes it within
   a minute or two.

Packages are kept on `gh-pages` for good because Litus's address for each one must keep
working: released copies of Litus have the registry's address compiled in, and published
package files never change or go away. The build refuses a different file under a published
name. Don't edit `gh-pages` by hand. A file removed from `site/` stays published until you
delete it from `gh-pages` yourself.

Settings that must stay as they are:
- **Settings › Pages:** source *Deploy from a branch*, `gh-pages`, `/ (root)`; custom domain
  `litus.tellusai.com`; *Enforce HTTPS* on. `site/CNAME` keeps the domain.
- **DNS (Cloudflare):** `litus` is a CNAME to `zfdong.github.io`, *DNS only*.
- **Settings › Branches:** `main` requires a pull request and the **Check** workflow.

### When the site gets large

GitHub Pages publishes at most 1 GB, and suggests a repository stay under about 1 GB too.
Packages build up, since none is ever deleted. `build.py` warns at 800 MB and refuses to go
over 1 GB. Before then, keep new packages elsewhere and leave the old ones where they are:
attach them to releases of this repository, or put them in Cloudflare R2, and have
`build.py` publish their absolute addresses in `package` (Litus accepts an `https://` address
there). GitHub Pages also has a soft limit of 100 GB of traffic a month, which heavy downloads
of large plugins would reach first.

## Withdrawing a plugin

Add it to `blocklist.json` with a reason, which users see:

```json
{ "blocked": [ { "id": "some-plugin", "reason": "Sends project data to an undisclosed server" } ] }
```

Leave out `sha256` to withdraw every version, or give it to withdraw one package. Litus
fetches the blocklist (cached for five minutes) when the user browses the registry, unloads a
withdrawn plugin then, and keeps it unloaded at every start after that.
Also remove `registry/<id>.json` to take it off Browse. Never delete the package file from the
website.
