#!/usr/bin/env python3
"""Check and build the Litus plugin registry.

    python scripts/build.py --check                     # check every entry (downloads packages)
    python scripts/build.py --check hello-litus         # check only these entries
    python scripts/build.py --check --changed origin/main   # check the entries a branch changes
    python scripts/build.py --site ../tellus/litus      # build into the site (litus.tellusai.com)

Each `registry/<id>.json` names a package by its address and SHA-256. The package is downloaded
and checked against that hash, and the published entry is made from the package's own
`plugin.json`, so what Litus lists is what the package says. Building copies each package to
`api/plugins/packages/<id>/<id>-<version>.litusplugin` in the site (a published package never
changes: a different file under a published name is refused) and writes `registry.json` and
`blocklist.json` beside them.

Litus skips registry entries it can't use without a word, so this script refuses them instead,
and writes nothing unless everything checks out. Only the Python standard library is used.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REGISTRY_DIR = ROOT / "registry"
BLOCKLIST = ROOT / "blocklist.json"
CACHE_DIR = ROOT / ".cache" / "packages"

PLUGINS_DIR = Path("api/plugins")  # inside the site
LIVE_PACKAGES = "https://litus.tellusai.com/api/plugins/packages/"

MAX_REGISTRY_BYTES = 64 * 1024 * 1024  # what Litus downloads at most
MAX_PACKAGE_BYTES = 64 * 1024 * 1024   # what Litus installs at most
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_BLOCKLIST_BYTES = 4 * 1024 * 1024

ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
# As Litus's manifest::valid_python: a minimum (">=3.12") or an exact version ("3.12", "3.12.4").
PYTHON_RE = re.compile(r"^(>=\s*)?3(\.\d{1,3}){0,2}$")
ALLOWED_PERMISSIONS = {"edit", "files", "network"}
ALLOWED_RUNTIMES = {"process", "skills"}  # absent: WebAssembly

# The fields of a registry/<id>.json file.
ENTRY_KEYS = {"id", "source", "categories"}
SOURCE_KEYS = {"url", "sha256"}

# Fields copied from plugin.json into the published entry, in output order.
COPIED_FIELDS = [
    "id", "name", "version", "description", "author", "homepage", "license",
    "categories", "permissions", "minLitusVersion", "i18n", "runtime",
]


class BuildError(Exception):
    pass


# --------------------------------------------------------------------------- source entries

def load_source(path: Path) -> tuple[dict | None, list[str]]:
    """Reads registry/<id>.json; returns it and what's wrong with it."""
    where = f"registry/{path.name}"
    try:
        entry = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, [f"{where}: not valid UTF-8 JSON: {exc}"]
    if not isinstance(entry, dict):
        return None, [f"{where}: must be a JSON object"]
    errors = []
    extra = sorted(set(entry) - ENTRY_KEYS)
    if extra:
        errors.append(f"{where}: unknown fields {extra}; allowed: {sorted(ENTRY_KEYS)}")
    pid = entry.get("id")
    if not (isinstance(pid, str) and ID_RE.match(pid)):
        errors.append(f"{where}: id must start with a-z, then a-z, 0-9 or '-', max 64 chars")
    elif path.name != f"{pid}.json":
        errors.append(f"{where}: the file must be named after its id ({pid}.json)")
    source = entry.get("source")
    if not isinstance(source, dict):
        errors.append(f"{where}: source must be an object with url and sha256")
    else:
        extra = sorted(set(source) - SOURCE_KEYS)
        if extra:
            errors.append(f"{where}: unknown source fields {extra}")
        url = source.get("url")
        if not (isinstance(url, str) and url.startswith("https://")):
            errors.append(f"{where}: source.url must be an https:// address")
        if not (isinstance(source.get("sha256"), str) and SHA256_RE.match(source["sha256"])):
            errors.append(f"{where}: source.sha256 must be 64 lower-case hex characters")
    if "categories" in entry and not (
            isinstance(entry["categories"], list)
            and all(isinstance(c, str) and c.strip() for c in entry["categories"])):
        errors.append(f"{where}: categories must be a list of texts")
    return entry, errors


def load_sources(ids: list[str] | None = None) -> tuple[list[dict], list[str]]:
    """Every entry (all are checked for form); `ids` limits which are returned."""
    entries, errors = [], []
    for path in sorted(REGISTRY_DIR.glob("*.json")):
        entry, errs = load_source(path)
        errors += errs
        if entry is not None and not errs:
            entries.append(entry)
    if ids is not None:
        known = {e["id"] for e in entries}
        errors += [f"registry/{i}.json: no such entry" for i in ids if i not in known
                   and not (REGISTRY_DIR / f"{i}.json").exists()]
        entries = [e for e in entries if e["id"] in ids]
    return entries, errors


def changed_ids(base: str) -> list[str]:
    """The ids whose registry/<id>.json the current branch adds or changes against `base`."""
    out = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=AMR", f"{base}...HEAD", "--", "registry/"],
        cwd=ROOT, check=True, capture_output=True, text=True).stdout
    return sorted(Path(p).stem for p in out.split() if p.endswith(".json"))


# --------------------------------------------------------------------------- packages

def download(url: str, limit: int = MAX_PACKAGE_BYTES) -> bytes | None:
    """The file at `url`, or None when the server says it isn't there (404)."""
    req = urllib.request.Request(url, headers={"User-Agent": "litus-plugin-registry"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            if not resp.geturl().startswith("https://"):
                raise BuildError(f"{url} redirected away from https")
            data = resp.read(limit + 1)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise BuildError(f"{url}: HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise BuildError(f"{url}: {exc}") from exc
    if len(data) > limit:
        raise BuildError(f"{url} is larger than {limit // (1024 * 1024)} MB")
    return data


def fetch_package(source: dict) -> bytes:
    """The package `source` names, checked against its SHA-256 (cached by hash)."""
    sha = source["sha256"]
    cached = CACHE_DIR / f"{sha}.litusplugin"
    if cached.is_file():
        data = cached.read_bytes()
        if hashlib.sha256(data).hexdigest() == sha:
            return data
    data = download(source["url"])
    if data is None:
        raise BuildError(f"{source['url']}: not found (HTTP 404)")
    got = hashlib.sha256(data).hexdigest()
    if got != sha:
        raise BuildError(f"{source['url']}: its SHA-256 is {got}, not {sha} as the entry says")
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cached.write_bytes(data)
    return data


def read_manifest(package: bytes) -> dict:
    """plugin.json from the zip root, or from inside a single top-level folder."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(package))
    except zipfile.BadZipFile as exc:
        raise BuildError(f"the package is not a zip archive: {exc}") from exc
    with zf:
        names = [n for n in zf.namelist() if not n.startswith("__MACOSX/")]
        if "plugin.json" in names:
            member = "plugin.json"
        else:
            tops = {n.split("/", 1)[0] for n in names}
            if len(tops) != 1 or f"{next(iter(tops))}/plugin.json" not in names:
                raise BuildError("plugin.json must be at the package's root or inside a single "
                                 f"top-level folder (top-level entries: {sorted(tops)})")
            member = f"{next(iter(tops))}/plugin.json"
        if zf.getinfo(member).file_size > MAX_MANIFEST_BYTES:
            raise BuildError(f"{member} is larger than 1 MB")
        try:
            manifest = json.loads(zf.read(member).decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BuildError(f"{member} is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(manifest, dict):
        raise BuildError("plugin.json must contain a JSON object")
    return manifest


def build_entry(manifest: dict, package: bytes, categories: list[str] | None = None) -> dict:
    """The published registry entry for a package, from its manifest."""
    entry = {k: manifest[k] for k in COPIED_FIELDS if manifest.get(k) is not None}
    if categories is not None:
        entry["categories"] = categories

    # Python version: "python": ">=3.12" or "python": {"version": ">=3.12", ...}.
    python = manifest.get("python")
    if isinstance(python, dict):
        python = python.get("version", ">=3.12")
    if python is not None:
        entry["python"] = str(python).strip()

    # Only the name's and description's translations: the rest stays in the package.
    if isinstance(entry.get("i18n"), dict):
        keep = {entry.get("name"), entry.get("description")} - {None}
        i18n = {lang: {k: v for k, v in table.items() if k in keep}
                for lang, table in entry["i18n"].items() if isinstance(table, dict)}
        i18n = {lang: table for lang, table in i18n.items() if table}
        if i18n:
            entry["i18n"] = i18n
        else:
            del entry["i18n"]

    for key in ("id", "version"):
        if not isinstance(entry.get(key), str):
            raise BuildError(f"plugin.json is missing a text '{key}'")
    pid, ver = entry["id"], entry["version"]
    entry["package"] = f"packages/{pid}/{pid}-{ver}.litusplugin"
    entry["sha256"] = hashlib.sha256(package).hexdigest()
    entry["size"] = len(package)
    return entry


# --------------------------------------------------------------------------- validation

def _is_text(v) -> bool:
    return isinstance(v, str) and v.strip() != ""


def validate_entry(entry, where: str) -> list[str]:
    """Every reason Litus would skip this published entry."""
    if not isinstance(entry, dict):
        return [f"{where}: entry is not an object"]
    errors = []
    for key in ("id", "name", "version", "package", "sha256"):
        if not _is_text(entry.get(key)):
            errors.append(f"{where}: '{key}' is missing or empty")

    if _is_text(entry.get("id")) and not ID_RE.match(entry["id"]):
        errors.append(f"{where}: id must start with a-z, then a-z, 0-9 or '-', max 64 chars")
    for key in ("version", "minLitusVersion"):
        if key in entry and not (isinstance(entry[key], str) and VERSION_RE.match(entry[key])):
            errors.append(f"{where}: {key} must look like 1.2.3 (got {entry[key]!r})")
    if _is_text(entry.get("sha256")) and not SHA256_RE.match(entry["sha256"]):
        errors.append(f"{where}: sha256 must be 64 lower-case hex characters")

    pkg = entry.get("package")
    if _is_text(pkg):
        if "://" in pkg and not pkg.startswith("https://"):
            errors.append(f"{where}: package must be https:// or a relative path (got {pkg!r})")
        if "\\" in pkg:
            errors.append(f"{where}: package path must use '/' separators")

    if "homepage" in entry and not (
            isinstance(entry["homepage"], str)
            and entry["homepage"].startswith(("http://", "https://"))):
        errors.append(f"{where}: homepage must start with http:// or https://")

    if "permissions" in entry:
        perms = entry["permissions"]
        if not isinstance(perms, list) or not all(isinstance(p, str) for p in perms):
            errors.append(f"{where}: permissions must be a list of texts")
        else:
            bad = sorted(set(perms) - ALLOWED_PERMISSIONS)
            if bad:
                errors.append(f"{where}: unknown permissions {bad}; allowed: "
                              f"{sorted(ALLOWED_PERMISSIONS)}")

    runtime = entry.get("runtime")
    if "runtime" in entry and runtime not in ALLOWED_RUNTIMES:
        errors.append(f"{where}: runtime must be left out (WebAssembly) or one of "
                      f"{sorted(ALLOWED_RUNTIMES)} (got {runtime!r})")
    if "python" in entry:
        if runtime != "process":
            errors.append(f"{where}: python is only allowed when runtime is 'process'")
        if not (isinstance(entry["python"], str) and PYTHON_RE.match(entry["python"])):
            errors.append(f"{where}: python must look like >=3.12, 3.12 or 3.12.4 "
                          f"(got {entry['python']!r})")

    for key in ("description", "author", "license"):
        if key in entry and not isinstance(entry[key], str):
            errors.append(f"{where}: {key} must be a text")
    if "categories" in entry and not (
            isinstance(entry["categories"], list)
            and all(isinstance(c, str) for c in entry["categories"])):
        errors.append(f"{where}: categories must be a list of texts")
    if "size" in entry and not (
            isinstance(entry["size"], int) and not isinstance(entry["size"], bool)
            and entry["size"] >= 0):
        errors.append(f"{where}: size must be a whole number of bytes")

    if "i18n" in entry:
        i18n = entry["i18n"]
        translatable = {entry.get("name"), entry.get("description")} - {None}
        if not isinstance(i18n, dict):
            errors.append(f"{where}: i18n must map languages to translations")
        else:
            for lang, table in i18n.items():
                if not isinstance(table, dict) or not all(
                        isinstance(k, str) and isinstance(v, str) for k, v in table.items()):
                    errors.append(f"{where}: i18n['{lang}'] must map texts to texts")
                    continue
                extra = sorted(set(table) - translatable)
                if extra:
                    errors.append(f"{where}: i18n['{lang}'] may only translate the name and "
                                  f"description; unexpected keys {extra}")
    return errors


def load_blocklist(path: Path = BLOCKLIST) -> tuple[dict, list[str]]:
    """blocklist.json and what's wrong with it."""
    try:
        raw = path.read_bytes()
        data = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {}, [f"blocklist.json: {exc}"]
    errors = []
    if len(raw) > MAX_BLOCKLIST_BYTES:
        errors.append("blocklist.json is larger than 4 MB")
    blocked = data.get("blocked") if isinstance(data, dict) else None
    if not isinstance(blocked, list):
        return data, errors + ["blocklist.json must be an object with a 'blocked' list"]
    for i, b in enumerate(blocked):
        where = f"blocklist.json: blocked[{i}]"
        if not isinstance(b, dict):
            errors.append(f"{where} is not an object")
            continue
        extra = sorted(set(b) - {"id", "sha256", "reason"})
        if extra:
            errors.append(f"{where}: unknown fields {extra}")
        if not (isinstance(b.get("id"), str) and ID_RE.match(b["id"])):
            errors.append(f"{where}: id is missing or malformed")
        if "sha256" in b and not (isinstance(b["sha256"], str) and SHA256_RE.match(b["sha256"])):
            errors.append(f"{where}: sha256 must be 64 lower-case hex characters")
        if not _is_text(b.get("reason")):
            errors.append(f"{where}: give a reason (users see it)")
    return data, errors


# --------------------------------------------------------------------------- check and build

def serialize(data) -> bytes:
    return (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def prepare(source: dict, existing: Path | None, live: str | None) -> tuple[dict, bytes]:
    """The published entry and package bytes for one registry/<id>.json."""
    pid = source["id"]
    data = None
    if existing is not None:
        # Already in the site (a version published before): use it, without downloading.
        cand = sorted(existing.glob(f"{pid}-*.litusplugin"))
        for c in cand:
            if hashlib.sha256(c.read_bytes()).hexdigest() == source["source"]["sha256"]:
                data = c.read_bytes()
                break
    if data is None:
        data = fetch_package(source["source"])
    manifest = read_manifest(data)
    if manifest.get("id") != pid:
        raise BuildError(f"the package's plugin.json says id {manifest.get('id')!r}, "
                         f"but the entry is {pid!r}")
    entry = build_entry(manifest, data, source.get("categories"))
    errors = validate_entry(entry, f"{pid} {entry['version']}")
    if errors:
        raise BuildError("the package would make an entry Litus skips:\n    "
                         + "\n    ".join(errors))

    name = f"{pid}-{entry['version']}.litusplugin"
    published = None
    if existing is not None and (existing / name).is_file():
        published = (existing / name).read_bytes()
    elif live is not None:
        published = download(f"{live}{pid}/{name}")
    if published is not None and published != data:
        raise BuildError(f"{name} is already published with different contents. Published "
                         "packages never change: give the new package a new version.")
    return entry, data


def run(ids: list[str] | None, site: Path | None, live: str | None) -> list[dict]:
    """Checks the entries (`ids`, or all) and, given a site, builds into it."""
    sources, errors = load_sources(ids)
    _, blocklist_errors = load_blocklist()
    errors += blocklist_errors

    plugins_dir = site / PLUGINS_DIR if site is not None else None
    entries, packages = [], {}
    for source in sources:
        pid = source["id"]
        existing = plugins_dir / "packages" / pid if plugins_dir is not None else None
        try:
            entry, data = prepare(source, existing, live)
        except BuildError as exc:
            errors.append(f"registry/{pid}.json: {exc}")
            continue
        entries.append(entry)
        packages[entry["package"]] = data

    registry = {"plugins": sorted(entries, key=lambda e: e["id"])}
    out = serialize(registry)
    if len(out) > MAX_REGISTRY_BYTES:
        errors.append(f"registry.json would be {len(out)} bytes; Litus reads at most 64 MB")
    if errors:
        raise BuildError("\n  ".join(["the registry doesn't check out:"] + errors))

    if plugins_dir is not None:
        for rel, data in packages.items():
            target = plugins_dir / rel
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
        for name, content in (("registry.json", out),
                              ("blocklist.json", BLOCKLIST.read_bytes())):
            target = plugins_dir / name
            if not target.exists() or target.read_bytes() != content:
                tmp = target.with_suffix(".json.tmp")
                tmp.write_bytes(content)
                tmp.replace(target)
    return entries


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("ids", nargs="*", help="entries to check (default: all)")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="check, without writing anything")
    mode.add_argument("--site", type=Path, help="the site folder to build into")
    ap.add_argument("--changed", metavar="BASE",
                    help="check only the entries the current branch changes against BASE")
    ap.add_argument("--offline", action="store_true",
                    help="don't compare with the packages already live on litus.tellusai.com")
    args = ap.parse_args(argv)

    if args.site is not None and (args.ids or args.changed):
        ap.error("--site builds every entry")
    ids = args.ids or None
    if args.changed:
        ids = sorted(set(ids or []) | set(changed_ids(args.changed)))
        if not ids:
            print("No registry entries changed.")
    live = None if args.offline or args.site is not None else LIVE_PACKAGES
    try:
        entries = run(ids, args.site, live)
    except BuildError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    for e in entries:
        print(f"OK  {e['id']:<28} {e['version']:<10} {e.get('runtime', 'wasm'):<8} {e['name']}")
    if args.site is not None:
        n = len(entries)
        print(f"Built {n} plugin{'' if n == 1 else 's'} into {args.site / PLUGINS_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
