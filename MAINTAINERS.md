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

The **Deploy** workflow runs for every push to `main`. It:

1. checks out the website repository, `zfdong/tellus`;
2. runs `python scripts/build.py --site tellus/litus`, which copies new packages to
   `litus/api/plugins/packages/<id>/<id>-<version>.litusplugin` and writes `registry.json` and
   `blocklist.json` beside them;
3. commits and pushes `tellus`. Cloudflare Pages deploys it.

Packages are kept in the website repository because Litus's address for each package must
keep working: released copies of Litus have the registry's address compiled in, and published
package files never change or go away. The build refuses a different file under a published
name.

The workflow needs the secret **`TELLUS_DEPLOY_TOKEN`**: a fine-grained personal access token
(GitHub › Settings › Developer settings › Fine-grained tokens) with access to `zfdong/tellus`
only and the permission **Contents: Read and write**. Renew it before it expires; until then a
failed Deploy run is the sign.

Don't edit `litus/api/plugins/` in the website repository by hand: the next deploy writes over
`registry.json` and `blocklist.json`.

Protect `main` here (Settings › Branches): require a pull request and the **Check** workflow
before merging.

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
