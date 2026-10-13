#!/usr/bin/env python3
import argparse
import base64
import concurrent.futures
import datetime as dt
import html
import io
import json
import os
import posixpath
import re
import shutil
import sys
import threading
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import bleach
import markdown
import yaml
from PIL import Image, ImageOps, UnidentifiedImageError
from pygments.formatters import HtmlFormatter


GITHUB_RE = re.compile(r"^https://github\.com/([^/]+)/([^/#?]+?)(?:\.git)?/?$")
MAINTAINER_RE = re.compile(r"^[^<>\n]+ <[^<>\s@]+@[^<>\s@]+\.[^<>\s@]+>$")
ROBONIX_SOURCE_RE = re.compile(r"^\$\{ROBONIX_SOURCE_PATH\}(?:/|$)")
ROBONIX_DEPLOY_RE = re.compile(r"^\$\{ROBONIX_DEPLOY_DIR\}(?:/|$)")
ENV_ROOT_RE = re.compile(r"^\$\{([^}]+)\}(?:/|$)")
WINDOWS_ABSOLUTE_RE = re.compile(r"^[A-Za-z]:[\\/]")
ROBONIX_SOURCE_REPO = "https://github.com/syswonder/robonix"
DEFAULT_PACKAGE_MANIFEST = "package_manifest.yaml"
LEGACY_PACKAGE_MANIFEST = "robonix_manifest.yaml"
SITE_ASSETS = (
    Path("assets/robonix-mark.svg"),
    Path("assets/site.css"),
    Path("assets/site.js"),
    Path("assets/submit.js"),
)
VENDOR_ASSETS = Path("assets/vendor")
LEGACY_MISSING_LICENSE = {"robonix.robot.wheeltec.r550"}
PREVIEW_SIZES = ((380, 285), (720, 540))
PREVIEW_WEBP_QUALITY = 76
# Indexing is almost entirely GitHub round trips, so entries are fetched
# concurrently. Kept well under GitHub's concurrent-request guidance, which
# asks callers to avoid bursts that trip the secondary rate limit.
COLLECT_WORKERS = int(os.environ.get("CATALOG_WORKERS", "8"))
# 5xx and 429 are GitHub telling us to come back; everything else in the 4xx
# range is a real answer about the resource.
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
GITHUB_ATTEMPTS = 4
GITHUB_RETRY_DELAY = 1.5


class InvalidRemoteManifestError(RuntimeError):
    def __init__(self, message: str, *, branch: str, readme: str) -> None:
        super().__init__(message)
        self.branch = branch
        self.readme = readme


def fail(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(1)


def github_fetch(url: str, *, optional: bool) -> dict | None:
    """Read one GitHub API resource as JSON.

    Retries transport failures and GitHub's own transient statuses with a
    linear backoff. A build reads hundreds of URLs over several concurrent
    connections, so a single dropped TLS handshake must not sink the run;
    a 4xx other than the optional 404 is a real answer and fails immediately.
    """
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "robonix-package-catalog",
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)

    last_error = ""
    for attempt in range(1, GITHUB_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            if optional and error.code == 404:
                return None
            body = error.read().decode("utf-8", "replace")
            last_error = f"{error.code}: {url}\n{body}"
            if error.code not in RETRYABLE_STATUS:
                break
        except Exception as error:  # transport, TLS, timeout, malformed body
            last_error = f"{url}: {error}"
        if attempt < GITHUB_ATTEMPTS:
            time.sleep(GITHUB_RETRY_DELAY * attempt)
    fail(f"GitHub request failed after {GITHUB_ATTEMPTS} attempts: {last_error}")


def github_json(url: str) -> dict:
    return github_fetch(url, optional=False)


def github_optional_json(url: str) -> dict | None:
    """Fetch an optional GitHub API resource, returning None only for 404."""
    return github_fetch(url, optional=True)


def download_bytes(url: str) -> bytes:
    headers = {"User-Agent": "robonix-package-catalog"}
    token = os.environ.get("GITHUB_TOKEN")
    if token and urllib.parse.urlparse(url).hostname in {
        "api.github.com",
        "raw.githubusercontent.com",
    }:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def prepare_preview_images(public_dir: Path, packages: list[dict]) -> None:
    preview_dir = public_dir / "assets" / "previews"
    for package in packages:
        source_url = package.get("preview_image_url")
        if not source_url:
            continue
        try:
            source_bytes = download_bytes(source_url)
            with Image.open(io.BytesIO(source_bytes)) as source:
                source = ImageOps.exif_transpose(source).convert("RGB")
                preview_dir.mkdir(parents=True, exist_ok=True)
                for width, height in PREVIEW_SIZES:
                    preview = ImageOps.fit(
                        source,
                        (width, height),
                        method=Image.Resampling.LANCZOS,
                    )
                    asset = f"assets/previews/{package_slug(package['name'])}-{width}.webp"
                    preview.save(
                        public_dir / asset,
                        "WEBP",
                        quality=PREVIEW_WEBP_QUALITY,
                        method=6,
                        optimize=True,
                    )
                    package[f"_preview_image_{width}"] = asset
        except (OSError, UnidentifiedImageError, urllib.error.URLError) as error:
            print(
                f"warning: {package['name']}: could not optimize preview image: {error}",
                file=sys.stderr,
            )


def parse_repo(url: str) -> tuple[str, str]:
    m = GITHUB_RE.match(url.strip())
    if not m:
        fail(f"repo must be a GitHub HTTPS URL: {url}")
    return m.group(1), m.group(2)


def load_remote_text(owner: str, repo: str, path: str, branch: str, *, required: bool) -> str:
    if required:
        content = github_json(
            f"https://api.github.com/repos/{owner}/{repo}/contents/{path}?ref={branch}"
        )
    else:
        content = github_optional_json(
            f"https://api.github.com/repos/{owner}/{repo}/contents/{path}?ref={branch}"
        )
        if content is None:
            return ""
    if content.get("encoding") != "base64" or "content" not in content:
        if required:
            fail(f"https://github.com/{owner}/{repo}: {path} is not base64 content")
        return ""
    return base64.b64decode(content["content"]).decode("utf-8")


def load_remote_manifest(repo_url: str, manifest_path: str) -> tuple[str, dict, str]:
    owner, repo = parse_repo(repo_url)
    meta = github_json(f"https://api.github.com/repos/{owner}/{repo}")
    branch = meta.get("default_branch")
    if not branch:
        fail(f"{repo_url}: missing default_branch")
    raw = load_remote_text(owner, repo, manifest_path, branch, required=True)
    try:
        manifest = yaml.safe_load(raw) or {}
    except yaml.YAMLError as e:
        readme = load_remote_text(owner, repo, "README.md", branch, required=False)
        mark = getattr(e, "problem_mark", None)
        location = (
            f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        )
        problem = getattr(e, "problem", None) or str(e).splitlines()[0]
        raise InvalidRemoteManifestError(
            f"Invalid {manifest_path}{location}: {problem}.",
            branch=branch,
            readme=readme,
        ) from e
    readme = load_remote_text(owner, repo, "README.md", branch, required=False)
    return branch, manifest, readme


def load_remote_default_branch(repo_url: str) -> str:
    owner, repo = parse_repo(repo_url)
    meta = github_json(f"https://api.github.com/repos/{owner}/{repo}")
    branch = meta.get("default_branch")
    if not isinstance(branch, str) or not branch:
        fail(f"{repo_url}: missing default_branch")
    return branch


def load_remote_repository_tree(repo_url: str, branch: str) -> dict[str, str]:
    """Return repository-relative paths and Git object types for one ref."""
    owner, repo = parse_repo(repo_url)
    ref = urllib.parse.quote(branch, safe="")
    payload = github_json(
        f"https://api.github.com/repos/{owner}/{repo}/git/trees/{ref}?recursive=1"
    )
    if payload.get("truncated"):
        fail(f"{repo_url}@{branch}: recursive Git tree is truncated")
    entries = payload.get("tree")
    if not isinstance(entries, list):
        fail(f"{repo_url}@{branch}: Git tree response is missing tree entries")
    return {
        entry["path"]: entry.get("type", "")
        for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("path"), str)
    }


def load_remote_submodule(
    repo_url: str,
    path: str,
    branch: str,
) -> tuple[str, str] | None:
    """Return a GitHub submodule's canonical repository URL and pinned SHA."""
    owner, repo = parse_repo(repo_url)
    encoded_path = urllib.parse.quote(path, safe="/")
    query = urllib.parse.urlencode({"ref": branch})
    payload = github_optional_json(
        f"https://api.github.com/repos/{owner}/{repo}/contents/{encoded_path}?{query}"
    )
    # GitHub's Contents API currently reports submodules as either "file" or
    # "submodule" depending on the API representation. The two dedicated
    # fields are the stable discriminator.
    if not isinstance(payload, dict):
        return None

    submodule_url = payload.get("submodule_git_url")
    sha = payload.get("sha")
    if not isinstance(submodule_url, str) or not isinstance(sha, str) or not sha:
        return None
    match = GITHUB_RE.match(submodule_url.strip())
    if not match:
        return None
    return f"https://github.com/{match.group(1)}/{match.group(2)}", sha


def package_slug(name: str) -> str:
    return name.replace("/", "_")


def parse_catalog(raw: dict) -> list[dict]:
    packages = raw.get("packages")
    if not isinstance(packages, list):
        fail("catalog.yaml must contain a top-level packages list")
    robots = raw.get("robots") or []
    if not isinstance(robots, list):
        fail("catalog.yaml robots must be a list when present")
    entries = []
    for entry in packages:
        if isinstance(entry, dict):
            entry = {**entry, "_catalog_type": "package"}
        entries.append(entry)
    for entry in robots:
        if isinstance(entry, dict):
            entry = {**entry, "_catalog_type": "robot", "manifest": entry.get("manifest", "robonix_manifest.yaml")}
        entries.append(entry)
    return entries


def read_catalog(path: Path) -> list[dict]:
    return parse_catalog(yaml.safe_load(path.read_text()) or {})


def catalog_entry_key(entry: dict) -> tuple[str, str, str, str] | None:
    """Return the fields that make a catalog entry unchanged from the base."""
    if not isinstance(entry, dict):
        return None
    name = entry.get("name")
    repo = entry.get("repo")
    if not isinstance(name, str) or not isinstance(repo, str):
        return None
    catalog_type = entry.get("_catalog_type") or (
        "robot" if name.startswith("robonix.robot.") else "package"
    )
    manifest = entry.get("manifest")
    if manifest is None:
        manifest = (
            "robonix_manifest.yaml"
            if catalog_type == "robot"
            else "package_manifest.yaml"
        )
    if not isinstance(manifest, str):
        return None
    return catalog_type, name, repo, manifest


def pull_request_base_ref() -> str:
    """Prefer the immutable PR base SHA over a branch name that may move."""
    event_path = os.environ.get("GITHUB_EVENT_PATH", "")
    if event_path:
        try:
            event = json.loads(Path(event_path).read_text(encoding="utf-8"))
            base_sha = event.get("pull_request", {}).get("base", {}).get("sha")
            if isinstance(base_sha, str) and base_sha:
                return base_sha
        except (OSError, json.JSONDecodeError):
            pass
    return os.environ.get("GITHUB_BASE_REF", "")


def catalog_baseline_keys(catalog_path: Path) -> set[tuple[str, str, str, str]]:
    """Load entries already accepted by the PR base, or current main build."""
    entries = read_catalog(catalog_path)
    if os.environ.get("GITHUB_EVENT_NAME") != "pull_request":
        return {key for entry in entries if (key := catalog_entry_key(entry))}

    repository = os.environ.get("GITHUB_REPOSITORY", "")
    base_ref = pull_request_base_ref()
    if repository.count("/") != 1 or not base_ref:
        fail("pull request build is missing GITHUB_REPOSITORY or GITHUB_BASE_REF")
    owner, repo = repository.split("/", 1)
    catalog_repo_path = catalog_path.as_posix()
    if catalog_path.is_absolute():
        catalog_repo_path = catalog_path.name
    raw = load_remote_text(owner, repo, catalog_repo_path, base_ref, required=True)
    try:
        baseline_entries = parse_catalog(yaml.safe_load(raw) or {})
    except yaml.YAMLError as error:
        fail(f"base {catalog_repo_path} is invalid YAML: {error}")
    return {
        key for entry in baseline_entries if (key := catalog_entry_key(entry))
    }


def norm_list(value, field: str, package_name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        fail(f"{package_name}: {field} must be a list of strings")
    return value


def validate_catalog_metadata(
    package_name: str,
    meta: dict,
    expected_name: str,
    *,
    allow_name_mismatch: bool = False,
) -> tuple[str, str, str, list[str], list[str], list[dict]]:
    if not isinstance(meta, dict):
        fail(f"{package_name}: catalog metadata must be a mapping")
    manifest_name = meta.get("name")
    version = meta.get("version")
    description = meta.get("description")
    license_name = meta.get("license")
    tags = meta.get("tags")
    maintainers = meta.get("maintainers")
    catalog_warnings = []
    if manifest_name != expected_name:
        reason = (
            f"manifest catalog name is {manifest_name!r}, expected {expected_name!r}"
        )
        if not allow_name_mismatch:
            fail(f"{package_name}: {reason}")
        catalog_warnings.append(
            {
                "type": "manifest_name_mismatch",
                "manifest_name": manifest_name,
                "expected_name": expected_name,
                "reason": reason,
            }
        )
    if not isinstance(version, str) or not version.strip():
        fail(f"{package_name}: version is required")
    if not isinstance(description, str) or not description.strip():
        fail(f"{package_name}: description is required")
    # Keep the one pre-existing exception explicit. Every newly submitted
    # package or robot must provide a non-empty SPDX license value.
    if license_name is None:
        if package_name not in LEGACY_MISSING_LICENSE:
            fail(f"{package_name}: license is required")
        license_name = "NOASSERTION"
        print(
            f"warning: {package_name}: license is missing; using NOASSERTION for backward compatibility",
            file=sys.stderr,
        )
    elif not isinstance(license_name, str) or not license_name.strip():
        fail(f"{package_name}: license must be a non-empty SPDX license string")
    tags = norm_list(tags, "tags", package_name)
    if not tags:
        fail(f"{package_name}: tags is required")
    maintainers = norm_list(maintainers, "maintainers", package_name)
    if not maintainers:
        fail(f"{package_name}: maintainers is required")
    for maintainer in maintainers:
        if not MAINTAINER_RE.match(maintainer):
            fail(
                f"{package_name}: maintainers entries must use 'Name <email@domain>' format: {maintainer!r}"
            )
    return version, description, license_name, tags, maintainers, catalog_warnings


def collect_capabilities(package_name: str, manifest: dict) -> list[str]:
    capabilities = manifest.get("capabilities") or []
    cap_names = []
    if not isinstance(capabilities, list):
        fail(f"{package_name}: capabilities must be a list")
    for cap in capabilities:
        if isinstance(cap, dict) and isinstance(cap.get("name"), str):
            cap_names.append(cap["name"])
        else:
            fail(f"{package_name}: each capability must contain name")
    return cap_names


KINDS = ("primitive", "service", "skill")


def layout_problems(kind: str, cap_names: list[str], root_caps: list[str] | None,
                    manifest_path: str) -> list[dict]:
    """How a package's contracts break the one-kind-per-package layout.

    A package is of one kind, named by its catalog name, and may provide any
    number of that kind, one contract namespace each: seven Go2 action skills,
    or the chassis and IMU one driver board reports. It must not mix kinds; a
    service a skill needs is a package of its own. When the catalog reads a manifest other
    than package_manifest.yaml, both must declare the same capabilities, or
    the catalog and the Hub show different packages. Shared contracts outside
    the three kinds (robonix/lifecycle/driver) are not checked.
    """
    problems = []
    if kind not in KINDS:
        return problems
    # Both manifests: a service left out of the catalog's manifest is still
    # in the package.
    declared = set(cap_names) | set(root_caps or [])
    foreign = sorted({c.split("/")[1] for c in declared
                      if c.startswith("robonix/") and c.count("/") >= 2
                      and c.split("/")[1] in KINDS and c.split("/")[1] != kind})
    for other in foreign:
        examples = sorted({"/".join(c.split("/")[:3]) for c in declared
                           if c.split("/")[1:2] == [other]})
        problems.append({
            "type": "mixed_kinds",
            "reason": f"a {kind} package declares {other} contracts ({', '.join(examples)}); "
                      f"put the {other} in its own package",
        })
    if root_caps is not None and set(root_caps) != set(cap_names):
        only_root = len(set(root_caps) - set(cap_names))
        only_here = len(set(cap_names) - set(root_caps))
        problems.append({
            "type": "manifest_mismatch",
            "reason": f"{manifest_path} and package_manifest.yaml declare different capabilities "
                      f"({only_root} only in package_manifest.yaml, {only_here} only in {manifest_path})",
        })
    return problems


CAPABILITY_RE = re.compile(r"^robonix/(primitive|service|skill|lifecycle)/")
LOCAL_CONTRACT_RE = re.compile(r"(^|/)capabilities/(?!lib/)[^ ]*\.toml$")

_robonix_contracts: tuple[set[str], set[str], tuple[str, ...]] | None = None
_robonix_contracts_lock = threading.Lock()


def raw_text(repo_url: str, branch: str, path: str) -> str:
    """One file at a ref, through raw.githubusercontent, which costs no API quota."""
    owner, repo = parse_repo(repo_url)
    url = (f"https://raw.githubusercontent.com/{owner}/{repo}/"
           f"{urllib.parse.quote(branch, safe='')}/{urllib.parse.quote(path)}")
    for attempt in range(1, GITHUB_ATTEMPTS + 1):
        try:
            return download_bytes(url).decode("utf-8")
        except Exception as error:
            if attempt == GITHUB_ATTEMPTS:
                fail(f"could not read {url}: {error}")
            time.sleep(GITHUB_RETRY_DELAY * attempt)
    return ""


def contract_of(text: str) -> dict | None:
    """The [contract] table of a contract TOML, if it has one with an id."""
    try:
        contract = tomllib.loads(text).get("contract")
    except tomllib.TOMLDecodeError:
        return None
    return contract if isinstance(contract, dict) and isinstance(contract.get("id"), str) else None


def robonix_contracts() -> tuple[set[str], set[str], tuple[str, ...]]:
    """The contract ids Robonix defines, its IDL files and its IDL submodules.

    Read once per build from the Robonix source tree: capabilities/**/*.toml
    outside lib/, and the IDL under capabilities/lib/, part of it submodules
    (common_interfaces) whose files the tree does not list.
    """
    global _robonix_contracts
    with _robonix_contracts_lock:
        if _robonix_contracts is None:
            branch = load_remote_default_branch(ROBONIX_SOURCE_REPO)
            tree = load_remote_repository_tree(ROBONIX_SOURCE_REPO, branch)
            lib = "capabilities/lib/"
            tomls = [p for p, t in tree.items() if t == "blob" and p.startswith("capabilities/")
                     and not p.startswith(lib) and p.endswith(".toml")]
            with concurrent.futures.ThreadPoolExecutor(max_workers=COLLECT_WORKERS) as pool:
                texts = list(pool.map(lambda p: raw_text(ROBONIX_SOURCE_REPO, branch, p), tomls))
            _robonix_contracts = (
                {c["id"] for c in map(contract_of, texts) if c},
                {p[len(lib):] for p, t in tree.items() if t == "blob" and p.startswith(lib)},
                tuple(p[len(lib):] + "/" for p, t in tree.items() if t == "commit" and p.startswith(lib)),
            )
        return _robonix_contracts


def contract_problems(repo: str, branch: str, manifest: dict, manifest_path: str) -> list[dict]:
    """Capabilities that no contract defines.

    A capability is a contract id, robonix/<kind>/<namespace>/<interface>:
    one of Robonix's own, or one the package defines in a TOML under its
    capabilities/ with the IDL beneath capabilities/lib/ (packaging spec
    §1.2). Without a contract rbnx codegen generates nothing for it and no
    one can tell what it takes. The Robonix contracts and the package tree
    are read only when a capability is not already accounted for.
    """
    problems = []
    named = []
    for cap in manifest.get("capabilities") or []:
        if not CAPABILITY_RE.match(cap["name"]):
            problems.append({
                "type": "capability_name",
                "reason": f"{cap['name']} is not a contract id; it must start with robonix/<kind>/",
            })
        else:
            named.append(cap)
    if not named:
        return problems
    robonix_ids, robonix_lib, robonix_submodules = robonix_contracts()
    pending = [cap for cap in named if cap["name"] not in robonix_ids]
    if not pending:
        return problems

    tree = load_remote_repository_tree(repo, branch)
    base = posixpath.dirname(manifest_path)
    paths = {cap["name"]: posixpath.normpath(posixpath.join(base, cap["path"]))
             for cap in pending if isinstance(cap.get("path"), str)}
    candidates = sorted({p for p, t in tree.items() if t == "blob" and LOCAL_CONTRACT_RE.search(p)}
                        | {p for p in paths.values() if p in tree})
    local = {}
    for path in candidates:
        contract = contract_of(raw_text(repo, branch, path))
        if contract:
            local.setdefault(contract["id"], (path, contract.get("idl")))

    for cap in pending:
        name = cap["name"]
        path = paths.get(name)
        if path and path not in tree:
            reason = f"{name}: its path {path} does not exist"
        elif name not in local:
            reason = (f"{name}: {path} has no [contract] table with this id" if path else
                      f"no contract defines {name}; add one under capabilities/ with its IDL "
                      f"in capabilities/lib/, or use a Robonix contract")
        else:
            toml_path, idl = local[name]
            lib = toml_path.rsplit("capabilities/", 1)[0] + "capabilities/lib/"
            if not isinstance(idl, str) or (lib + idl) in tree or idl in robonix_lib \
                    or idl.startswith(robonix_submodules):
                continue
            reason = f"{name}: its IDL {idl} is in neither {lib} nor Robonix's capabilities/lib/"
        problems.append({"type": "undefined_contract", "reason": reason})
    return problems


def group_capabilities(kind: str, cap_names: list[str]) -> list[dict]:
    """The skills (services, primitives) a package provides, one per contract
    namespace: robonix/skill/explore/{explore,status,cancel} is one skill,
    the Go2 package's seven robonix/skill/unitree_go2_<action>/... are seven.

    A capability counts as one interface; a namespace of the package's own
    kind groups the interfaces of one skill. Contracts outside that kind (a
    shared lifecycle driver) belong to no group.
    """
    groups: dict[str, list[str]] = {}
    prefix = f"robonix/{kind}/"
    for cap in cap_names:
        if not cap.startswith(prefix):
            continue
        namespace = cap[len(prefix):].split("/", 1)[0]
        if namespace:
            groups.setdefault(prefix + namespace, []).append(cap)
    return [{"namespace": ns, "capabilities": caps} for ns, caps in groups.items()]


def provided_count(package: dict) -> int:
    """How many skills (services, primitives) a package provides: one per
    contract namespace of its own kind. A package declaring only the shared
    lifecycle driver, or contracts outside its kind, provides none."""
    return len(provided_groups(package))


def interface_count(package: dict) -> int:
    """How many capabilities (interfaces) a package provides: those under its
    own kind's namespaces, so the shared lifecycle driver is not one."""
    return sum(len(group["capabilities"]) for group in provided_groups(package))


def provided_groups(package: dict) -> list[dict]:
    """`provides` as built, or derived again for an entry read from cache."""
    if "provides" in package:
        return package["provides"] or []
    return group_capabilities(package.get("kind", ""), package.get("capabilities", []))


def collect_deploy_dependencies(package_name: str, manifest: dict) -> list[dict]:
    deps = []
    for section in ("primitive", "service", "skill"):
        entries = manifest.get(section) or []
        if not isinstance(entries, list):
            fail(f"{package_name}: deploy section {section} must be a list")
        for item in entries:
            if not isinstance(item, dict):
                fail(f"{package_name}: deploy section {section} entries must be mappings")
            local_name = item.get("name")
            repo = item.get("url")
            path = item.get("path")
            if not isinstance(local_name, str) or not local_name.strip():
                fail(f"{package_name}: deploy dependency in {section} missing name")
            dep = {
                "section": section,
                "name": local_name,
                "repo": repo if isinstance(repo, str) else "",
                "path": path if isinstance(path, str) else "",
                "branch": item.get("branch") if isinstance(item.get("branch"), str) else "",
                "manifest": item.get("manifest") if isinstance(item.get("manifest"), str) else "",
            }
            deps.append(dep)
    return deps


def canonical_github_repo(url: str) -> str:
    """Normalize a GitHub repository URL for catalog dependency matching."""
    match = GITHUB_RE.match(url.strip())
    if not match:
        return ""
    return f"https://github.com/{match.group(1).lower()}/{match.group(2).lower()}"


def classify_deploy_dependency(dep: dict, catalog_repos: dict[str, str]) -> tuple[str, str, str]:
    """Return (resolution, package_name, warning) for one robot dependency."""
    repo = str(dep.get("repo") or "").strip()
    path = str(dep.get("path") or "").strip()
    source = repo or path

    if repo:
        canonical_repo = canonical_github_repo(repo)
        package_name = catalog_repos.get(canonical_repo, "") if canonical_repo else ""
        if package_name:
            return "catalog", package_name, ""
        if canonical_repo:
            return (
                "unresolved",
                "",
                f"Repository {repo!r} is not indexed by catalog.yaml; add the package to the Catalog or use a repository-local dependency.",
            )
        return (
            "unresolved",
            "",
            f"URL {repo!r} is not a cataloged GitHub repository; use a cataloged repository URL, "
            "${ROBONIX_SOURCE_PATH}/... for a Robonix built-in, "
            "${ROBONIX_DEPLOY_DIR}/... for the boot deployment, or a repository-relative path.",
        )

    if not path:
        return (
            "unresolved",
            "",
            "No url or path is declared; use a cataloged repository URL, "
            "${ROBONIX_SOURCE_PATH}/... for a Robonix built-in, "
            "${ROBONIX_DEPLOY_DIR}/... for the boot deployment, or a repository-relative path.",
        )
    if ROBONIX_SOURCE_RE.match(path):
        return "robonix_source", "", ""
    if ROBONIX_DEPLOY_RE.match(path):
        return "robonix_deploy", "", ""
    env_root = ENV_ROOT_RE.match(path)
    if env_root:
        return (
            "unresolved",
            "",
            f"Uses unsupported environment root ${{{env_root.group(1)}}}; Robonix accepts only "
            "${ROBONIX_SOURCE_PATH}/... for the source tree and ${ROBONIX_DEPLOY_DIR}/... "
            "for the boot deployment. Otherwise use a cataloged repository URL or "
            "repository-relative path.",
        )
    if posixpath.isabs(path) or WINDOWS_ABSOLUTE_RE.match(path) or path.startswith("~"):
        return (
            "unresolved",
            "",
            f"Uses host-specific absolute path {source!r}; use a cataloged repository URL, "
            "${ROBONIX_SOURCE_PATH}/... for a Robonix built-in, "
            "${ROBONIX_DEPLOY_DIR}/... for the boot deployment, or a repository-relative "
            "path such as ./primitives/robot_description.",
        )
    normalized_path = posixpath.normpath(path.replace("\\", "/"))
    if normalized_path == ".." or normalized_path.startswith("../"):
        return (
            "unresolved",
            "",
            f"Repository-relative path {path!r} escapes the robot repository; keep bundled packages inside the repository.",
        )
    return "robot_repository", "", ""


def annotate_deploy_dependencies(packages: list[dict]) -> None:
    catalog_repos = {
        canonical_github_repo(package["repo"]): package["name"]
        for package in packages
        if package.get("catalog_type") != "robot" and canonical_github_repo(package.get("repo", ""))
    }
    name_to_slug = {package["name"]: package_slug(package["name"]) for package in packages}
    for package in packages:
        if package.get("catalog_type") != "robot":
            continue
        warnings = list(package.get("_source_warnings", []))
        for dep in package.get("deploy_dependencies", []):
            resolution, dep_name, warning = classify_deploy_dependency(dep, catalog_repos)
            dep["resolution"] = resolution
            dep["package_name"] = dep_name
            dep["package_url"] = f"../{name_to_slug[dep_name]}/" if dep_name else ""
            dep["resolution_warning"] = warning
            if warning:
                source = dep.get("repo") or dep.get("path") or ""
                warnings.append(
                    {
                        "section": dep.get("section", ""),
                        "name": dep.get("name", ""),
                        "source": source,
                        "reason": warning,
                    }
                )
        package["deployment_status"] = "warning" if warnings else "ok"
        package["deployment_warnings"] = warnings


def dependency_repository_location(
    package: dict,
    dep: dict,
    robonix_source_branch: str,
) -> tuple[str, str, str, str] | None:
    """Resolve a local dependency to (repo, branch, path, root description)."""
    path = str(dep.get("path") or "").strip()
    resolution = dep.get("resolution")
    if resolution == "robonix_source":
        relative_path = ROBONIX_SOURCE_RE.sub("", path, count=1)
        return (
            ROBONIX_SOURCE_REPO,
            robonix_source_branch,
            relative_path,
            f"${{ROBONIX_SOURCE_PATH}} ({ROBONIX_SOURCE_REPO})",
        )
    if resolution == "robonix_deploy":
        relative_path = ROBONIX_DEPLOY_RE.sub("", path, count=1)
        return (
            package["repo"],
            package["default_branch"],
            relative_path,
            "${ROBONIX_DEPLOY_DIR} (the robot repository root)",
        )
    if resolution == "robot_repository":
        return (
            package["repo"],
            package["default_branch"],
            path,
            "the robot repository root",
        )
    return None


def validate_deploy_dependency_paths(packages: list[dict]) -> None:
    """Warn when a local deploy path is absent or is not a package directory."""
    tree_cache: dict[tuple[str, str], dict[str, str]] = {}
    submodule_cache: dict[tuple[str, str, str], tuple[str, str] | None] = {}
    source_branch = ""

    for package in packages:
        if package.get("catalog_type") != "robot":
            continue
        warnings = list(package.get("deployment_warnings", []))
        for dep in package.get("deploy_dependencies", []):
            if dep.get("resolution_warning"):
                continue
            if dep.get("resolution") == "robonix_source" and not source_branch:
                source_branch = load_remote_default_branch(ROBONIX_SOURCE_REPO)
            location = dependency_repository_location(package, dep, source_branch)
            if location is None:
                continue
            repo_url, branch, relative_path, root_description = location
            normalized_path = posixpath.normpath(relative_path.replace("\\", "/"))
            if normalized_path == ".":
                normalized_path = ""

            warning = ""
            if normalized_path == ".." or normalized_path.startswith("../"):
                warning = (
                    f"Resolved path {normalized_path!r} escapes {root_description}."
                )
            else:
                cache_key = (repo_url, branch)
                if cache_key not in tree_cache:
                    tree_cache[cache_key] = load_remote_repository_tree(repo_url, branch)
                tree = tree_cache[cache_key]
                package_tree = tree
                package_path = normalized_path
                package_repo_url = repo_url
                package_ref = branch
                entry_type = tree.get(normalized_path) if normalized_path else "tree"
                if entry_type == "commit":
                    submodule_key = (repo_url, branch, normalized_path)
                    if submodule_key not in submodule_cache:
                        submodule_cache[submodule_key] = load_remote_submodule(
                            repo_url,
                            normalized_path,
                            branch,
                        )
                    submodule = submodule_cache[submodule_key]
                    if submodule is None:
                        warning = (
                            f"Resolved path {normalized_path!r} is a Git submodule in "
                            f"{repo_url}@{branch}, but GitHub did not provide a "
                            "verifiable repository URL and pinned commit."
                        )
                    else:
                        package_repo_url, package_ref = submodule
                        submodule_tree_key = (package_repo_url, package_ref)
                        if submodule_tree_key not in tree_cache:
                            tree_cache[submodule_tree_key] = load_remote_repository_tree(
                                package_repo_url,
                                package_ref,
                            )
                        package_tree = tree_cache[submodule_tree_key]
                        package_path = ""

                directory_exists = entry_type == "tree" or (
                    entry_type == "commit" and not warning
                )
                if not directory_exists:
                    if not warning:
                        warning = (
                            f"Resolved path {normalized_path or '.'!r} does not exist in "
                            f"{repo_url}@{branch}; {root_description} is the path root."
                        )
                if not warning:
                    selected_manifest = str(dep.get("manifest") or "").strip()
                    manifest_names = (
                        [selected_manifest]
                        if selected_manifest
                        else [DEFAULT_PACKAGE_MANIFEST, LEGACY_PACKAGE_MANIFEST]
                    )
                    manifests = [
                        posixpath.join(package_path, name) if package_path else name
                        for name in manifest_names
                    ]
                    if not any(package_tree.get(path) == "blob" for path in manifests):
                        if selected_manifest:
                            expected = repr(selected_manifest)
                        else:
                            expected = (
                                f"{DEFAULT_PACKAGE_MANIFEST!r} or legacy "
                                f"{LEGACY_PACKAGE_MANIFEST!r}"
                            )
                        if entry_type == "commit":
                            warning = (
                                f"Resolved Git submodule {normalized_path!r} points to "
                                f"{package_repo_url}@{package_ref} but is not a usable "
                                f"Robonix package: expected {expected} at the submodule "
                                "root."
                            )
                        else:
                            warning = (
                                f"Resolved directory {normalized_path or '.'!r} exists in "
                                f"{package_repo_url}@{package_ref} but is not a usable "
                                f"Robonix package: expected {expected}."
                            )

            if warning:
                dep["resolution_warning"] = warning
                warnings.append(
                    {
                        "section": dep.get("section", ""),
                        "name": dep.get("name", ""),
                        "source": dep.get("path") or "",
                        "reason": warning,
                    }
                )
        package["deployment_status"] = "warning" if warnings else "ok"
        package["deployment_warnings"] = warnings


def github_command_escape(value: str) -> str:
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def markdown_cell(value: str) -> str:
    return value.replace("\\", "\\\\").replace("|", "\\|").replace("\r", " ").replace("\n", "<br>")


def report_deployment_warnings(packages: list[dict]) -> int:
    warnings = [
        (package["name"], warning)
        for package in packages
        for warning in package.get("deployment_warnings", [])
    ]
    github_actions = os.environ.get("GITHUB_ACTIONS", "").lower() == "true"
    for robot_name, warning in warnings:
        source = warning.get("source") or "(missing source)"
        message = (
            f"{robot_name}: {warning.get('section', '')} {warning.get('name', '')} "
            f"[{source}] — {warning.get('reason', '')}"
        )
        print(f"warning: {message}", file=sys.stderr)
        if github_actions:
            title = github_command_escape(
                f"Unresolved deployment dependency: {warning.get('name', '')}"
            )
            print(
                f"::warning title={title}::{github_command_escape(message)}",
                file=sys.stderr,
            )

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        lines = ["## Robot deployment warning report", ""]
        if not warnings:
            lines.append("All robot deployment dependencies have portable, catalog-resolvable sources.")
        else:
            warning_label = "warning" if len(warnings) == 1 else "warnings"
            lines.extend(
                [
                    f"Found **{len(warnings)} robot deployment {warning_label}**. "
                    "Catalog generation continues, but the robot maintainers should fix the reported manifest or source.",
                    "",
                    "| Robot | Section | Deployment package | Source | Reason |",
                    "| --- | --- | --- | --- | --- |",
                ]
            )
            for robot_name, warning in warnings:
                lines.append(
                    "| "
                    + " | ".join(
                        markdown_cell(str(value))
                        for value in (
                            robot_name,
                            warning.get("section", ""),
                            warning.get("name", ""),
                            warning.get("source") or "(missing source)",
                            warning.get("reason", ""),
                        )
                    )
                    + " |"
                )
        with open(summary_path, "a", encoding="utf-8") as summary:
            summary.write("\n".join(lines) + "\n")
    return len(warnings)


def report_catalog_warnings(packages: list[dict]) -> int:
    warnings = [
        (package["name"], warning)
        for package in packages
        for warning in package.get("catalog_warnings", [])
    ]
    github_actions = os.environ.get("GITHUB_ACTIONS", "").lower() == "true"
    for package_name, warning in warnings:
        message = f"{package_name}: {warning.get('reason', '')}"
        print(f"warning: {message}", file=sys.stderr)
        if github_actions:
            title = github_command_escape(f"Catalog name mismatch: {package_name}")
            print(
                f"::warning title={title}::{github_command_escape(message)}",
                file=sys.stderr,
            )

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        lines = ["## Catalog metadata warning report", ""]
        if not warnings:
            lines.append("All indexed manifest names match their catalog entries.")
        else:
            warning_label = "warning" if len(warnings) == 1 else "warnings"
            lines.extend(
                [
                    f"Found **{len(warnings)} catalog metadata {warning_label}**. "
                    "Catalog generation continues for entries already accepted on the base branch; new or modified entries remain strict.",
                    "",
                    "| Catalog entry | Manifest name | Expected name | Reason |",
                    "| --- | --- | --- | --- |",
                ]
            )
            for package_name, warning in warnings:
                lines.append(
                    "| "
                    + " | ".join(
                        markdown_cell(str(value))
                        for value in (
                            package_name,
                            warning.get("manifest_name"),
                            warning.get("expected_name", ""),
                            warning.get("reason", ""),
                        )
                    )
                    + " |"
                )
        with open(summary_path, "a", encoding="utf-8") as summary:
            summary.write("\n".join(lines) + "\n")
    return len(warnings)


def collect_entry(
    entry: dict,
    baseline_keys: set[tuple[str, str, str, str]],
) -> dict:
    """Fetch and validate one catalog entry into its rendered-page record.

    Every remote read for a single package happens here, which is what makes
    the entries safe to fetch concurrently: nothing is shared between them.
    Cross-entry checks (duplicates, dependency resolution) run in collect().
    """
    name = entry["name"]
    repo = entry["repo"]
    _, repo_name = parse_repo(repo)
    manifest_path = entry.get("manifest")
    if manifest_path is None:
        manifest_path = "robonix_manifest.yaml" if name.startswith("robonix.robot.") else "package_manifest.yaml"
    if not isinstance(manifest_path, str) or not manifest_path.strip():
        fail(f"{name}: manifest must be a non-empty string")
    catalog_type = entry.get("_catalog_type") or ("robot" if manifest_path == "robonix_manifest.yaml" or name.startswith("robonix.robot.") else "package")
    allow_name_mismatch = catalog_entry_key(entry) in baseline_keys
    source_warnings = []
    cached = None
    try:
        branch, manifest, readme = load_remote_manifest(repo, manifest_path)
    except InvalidRemoteManifestError as error:
        if catalog_type != "robot":
            fail(f"{repo}: {error}")
        # Nothing generated is committed any more, so the published site is
        # the only record of the last good metadata for this robot.
        cached_url = f"{SITE_URL}/api/v1/package/{urllib.parse.quote(name, safe='')}.json"
        try:
            with urllib.request.urlopen(cached_url, timeout=30) as response:
                cached = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, json.JSONDecodeError, OSError) as cache_error:
            fail(
                f"{repo}: {error} No last-known-good catalog metadata is "
                f"published at {cached_url}: {cache_error}"
            )
        branch = error.branch
        manifest = {}
        readme = error.readme
        source_warnings.append(
            {
                "section": "manifest",
                "name": manifest_path,
                "source": f"{repo}/blob/{branch}/{manifest_path}",
                "reason": (
                    f"{error} Showing last-known-good catalog metadata; "
                    "deployment dependencies cannot be inspected until the "
                    "robot manifest is fixed."
                ),
            }
        )
    preview_image_url = ""
    if catalog_type == "robot":
        owner, parsed_repo = parse_repo(repo)
        preview = github_optional_json(
            f"https://api.github.com/repos/{owner}/{parsed_repo}/contents/assets/robot.jpg?ref={urllib.parse.quote(branch, safe='')}"
        )
        if preview and preview.get("type") == "file":
            preview_image_url = preview.get("download_url") or ""
    if catalog_type == "robot":
        cap_names = []
        if cached is None:
            meta = manifest.get("catalog")
            (
                version,
                description,
                license_name,
                tags,
                maintainers,
                catalog_warnings,
            ) = validate_catalog_metadata(
                name,
                meta,
                name,
                allow_name_mismatch=allow_name_mismatch,
            )
            deploy_dependencies = collect_deploy_dependencies(name, manifest)
        else:
            version = cached.get("version", "")
            description = cached.get("description", "")
            license_name = cached.get("license", "")
            tags = cached.get("tags", [])
            maintainers = cached.get("maintainers", [])
            validate_catalog_metadata(
                name,
                {
                    "name": name,
                    "version": version,
                    "description": description,
                    "license": license_name,
                    "tags": tags,
                    "maintainers": maintainers,
                },
                name,
            )
            deploy_dependencies = []
            catalog_warnings = []
    else:
        package = manifest.get("package")
        if not isinstance(package, dict):
            fail(f"{name}: package_manifest.yaml missing package mapping")
        (
            version,
            description,
            license_name,
            tags,
            maintainers,
            catalog_warnings,
        ) = validate_catalog_metadata(
            name,
            package,
            name,
            allow_name_mismatch=allow_name_mismatch,
        )
        cap_names = collect_capabilities(name, manifest)
        deploy_dependencies = []
    kind = name.split(".")[1] if name.startswith("robonix.") and "." in name else ""
    if catalog_type != "robot":
        root_caps = None
        if manifest_path != "package_manifest.yaml" and cached is None:
            owner, _ = parse_repo(repo)
            raw = load_remote_text(owner, repo_name, "package_manifest.yaml", branch, required=False)
            try:
                root_manifest = yaml.safe_load(raw) if raw else None
            except yaml.YAMLError:
                root_manifest = None
            if isinstance(root_manifest, dict):
                root_caps = collect_capabilities(name, root_manifest)
        problems = layout_problems(kind, cap_names, root_caps, manifest_path)
        if kind in KINDS:
            problems += contract_problems(repo, branch, manifest, manifest_path)
        for problem in problems:
            # A new or changed entry has to fix this before it is merged; an
            # entry already in the catalog is listed with a warning.
            if not allow_name_mismatch:
                fail(f"{name}: {problem['reason']}")
            catalog_warnings.append(problem)
    return {
        "name": name,
        "version": version,
        "description": description,
        "license": license_name,
        "tags": tags,
        "repo": repo,
        "maintainers": maintainers,
        "repo_name": repo_name,
        "default_branch": branch,
        "kind": kind,
        "catalog_type": catalog_type,
        "catalog_status": "warning" if catalog_warnings else "ok",
        "catalog_warnings": catalog_warnings,
        "manifest": manifest_path,
        "capabilities": cap_names,
        "provides": group_capabilities(kind, cap_names),
        "deploy_dependencies": deploy_dependencies,
        "readme_url": f"{repo}/blob/{branch}/README.md",
        "preview_image_url": preview_image_url,
        "_readme_markdown": readme,
        "_source_warnings": source_warnings,
    }


def validate_catalog_entries(entries: list) -> list[dict]:
    """Check the shape of catalog.yaml before any network work starts.

    Duplicate names and repos are cross-entry facts, so they are settled here
    rather than inside the per-entry fetch that runs concurrently.
    """
    seen_names: set[str] = set()
    seen_repos: set[str] = set()
    checked = []
    for entry in entries:
        if not isinstance(entry, dict):
            fail("each catalog package entry must be a mapping")
        name = entry.get("name")
        repo = entry.get("repo")
        if not isinstance(name, str) or not name.strip():
            fail("catalog package entry missing name")
        if not isinstance(repo, str) or not repo.strip():
            fail(f"{name}: catalog package entry missing repo")
        if name in seen_names:
            fail(f"duplicate package name in catalog.yaml: {name}")
        if repo in seen_repos:
            fail(f"duplicate repo in catalog.yaml: {repo}")
        seen_names.add(name)
        seen_repos.add(repo)
        checked.append(entry)
    return checked


def collect(
    catalog_path: Path,
    baseline_keys: set[tuple[str, str, str, str]] | None = None,
) -> list[dict]:
    """Read catalog.yaml and fetch every listed repository.

    Indexing one entry costs several GitHub round trips and almost no CPU, so
    the entries are fetched concurrently; COLLECT_WORKERS stays well under
    GitHub's concurrency guidance. A SystemExit raised by fail() inside a
    worker surfaces from result() and still aborts the build.
    """
    entries = validate_catalog_entries(read_catalog(catalog_path))
    baseline_keys = baseline_keys or set()
    with concurrent.futures.ThreadPoolExecutor(max_workers=COLLECT_WORKERS) as pool:
        futures = [pool.submit(collect_entry, entry, baseline_keys) for entry in entries]
        out = [future.result() for future in futures]
    annotate_deploy_dependencies(out)
    validate_deploy_dependency_paths(out)
    out.sort(key=lambda x: x["name"])
    return out

def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def write_api(path: Path, data) -> None:
    """Write backward-compatible and browser-renderable JSON API resources."""
    write_json(path, data)
    write_json(path.with_name(f"{path.name}.json"), data)

SITE_URL = "https://packages.robonix.ai"
CATALOG_REPO = "https://github.com/syswonder/robonix-package-catalog"
DOCS_URL = "https://book.robonix.ai/"

# One sentence per provider kind, in the vocabulary the developer guide uses:
# capabilities are the interface, contracts are their shape, and primitive /
# service / skill are the three kinds of provider.
KIND_BLURB = {
    "primitive": "Hardware-facing providers. One package per device, exposing what it can do through the standard capability contracts.",
    "service": "Shared computation layered on top of primitives — mapping, navigation, perception, speech.",
    "skill": "Task-level providers that compose services and primitives into something a robot can be asked to do.",
}
KIND_ORDER = ("primitive", "service", "skill")

ICON_SEARCH = '<svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><circle cx="7" cy="7" r="4.4"/><path d="m10.4 10.4 3.1 3.1"/></svg>'
ICON_GITHUB = '<svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><path d="M8 .2a8 8 0 0 0-2.5 15.6c.4.07.55-.17.55-.38l-.01-1.34c-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82a7.4 7.4 0 0 1 4 0c1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48l-.01 2.19c0 .21.15.46.55.38A8 8 0 0 0 8 .2Z"/></svg>'
ICON_SUN = '<svg class="icon-light" width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><circle cx="8" cy="8" r="3.1"/><path d="M8 1v1.6M8 13.4V15M15 8h-1.6M2.6 8H1M12.9 3.1l-1.1 1.1M4.2 11.8l-1.1 1.1M12.9 12.9l-1.1-1.1M4.2 4.2 3.1 3.1"/></svg>'
ICON_MOON = '<svg class="icon-dark" width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"><path d="M13.5 9.6A5.8 5.8 0 0 1 6.4 2.5a5.9 5.9 0 1 0 7.1 7.1Z"/></svg>'
ICON_AUTO = '<svg class="icon-auto" width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"><circle cx="8" cy="8" r="5.4"/><path d="M8 2.6a5.4 5.4 0 0 1 0 10.8Z" fill="currentColor" stroke="none"/></svg>'
ICON_PLUS = '<svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M8 3.2v9.6M3.2 8h9.6"/></svg>'
ICON_JSON = '<svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M6.2 2.2H4.6A1.6 1.6 0 0 0 3 3.8v2.6c0 .9-.7 1.6-1.6 1.6.9 0 1.6.7 1.6 1.6v2.6a1.6 1.6 0 0 0 1.6 1.6h1.6"/><path d="M9.8 2.2h1.6A1.6 1.6 0 0 1 13 3.8v2.6c0 .9.7 1.6 1.6 1.6-.9 0-1.6.7-1.6 1.6v2.6a1.6 1.6 0 0 1-1.6 1.6H9.8"/></svg>'
ICON_ARROW = '<svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M3.4 8h9.2M9 4.4 12.6 8 9 11.6"/></svg>'


# One glyph per provider kind, plus the small monochrome marks used on the
# metadata line of a listing card.
KIND_GLYPH = {
    "primitive": '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"><rect x="4.5" y="4.5" width="7" height="7" rx="1"/><path d="M6.5 2v2.5M9.5 2v2.5M6.5 11.5V14M9.5 11.5V14M2 6.5h2.5M2 9.5h2.5M11.5 6.5H14M11.5 9.5H14" stroke-linecap="round"/></svg>',
    "service": '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"><path d="M8 1.8 14 5 8 8.2 2 5Z"/><path d="M2 8.2 8 11.4l6-3.2M2 11.2 8 14.4l6-3.2"/></svg>',
    "skill": '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"><path d="M8.6 1.5 3.4 9h3.4l-1.4 5.5L13 6.8H9.3Z"/></svg>',
    "robot": '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"><rect x="2.5" y="5" width="11" height="8" rx="2"/><path d="M8 2.2V5" stroke-linecap="round"/><circle cx="8" cy="2" r="1"/><path d="M6 8.5v1M10 8.5v1" stroke-linecap="round"/></svg>',
}
ICON_CAP = '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M5.5 2v3.2M10.5 2v3.2M3.6 5.2h8.8v2.2a4.4 4.4 0 0 1-8.8 0Z"/><path d="M8 11.8V14"/></svg>'
ICON_LICENSE = '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M8 1.8 13.2 4v4c0 3.1-2.2 5.2-5.2 6.2C5 13.2 2.8 11.1 2.8 8V4Z"/></svg>'
ICON_USER = '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="8" cy="5.4" r="2.6"/><path d="M3 13.4a5 5 0 0 1 10 0"/></svg>'
ICON_IMAGE = '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"><rect x="2" y="3" width="12" height="10" rx="2"/><circle cx="5.8" cy="6.6" r="1.1"/><path d="m2.6 11.6 3.3-3 3 2.4 2-1.7 2.5 2.3"/></svg>'


def render_tags(tags: list[str], *, interactive: bool) -> str:
    """Render tag pills.

    On listing pages a tag applies a filter, so it must be a real button; the
    detail pages have nothing to filter, so the same pill renders inert.
    """
    if interactive:
        return "".join(
            f'<button type="button" class="tag" data-tag-filter="{html.escape(t)}" aria-pressed="false">{html.escape(t)}</button>'
            for t in tags
        )
    return "".join(f'<span class="tag">{html.escape(t)}</span>' for t in tags)


def wrappable_name(name: str) -> str:
    """Escape a package name and mark where it may wrap.

    A dotted identifier is one unbreakable word as far as CSS is concerned, so
    a narrow card either overflows or breaks mid-segment. An explicit <wbr>
    after each separator keeps breaks on segment boundaries, where they read.
    """
    escaped = html.escape(name)
    for separator in (".", "_"):
        escaped = escaped.replace(separator, f"{separator}<wbr>")
    return escaped


def kind_chip(kind: str) -> str:
    safe = html.escape(kind)
    known = kind if kind in {"primitive", "service", "skill", "robot"} else "plain"
    return f'<span class="chip chip-{known}">{safe}</span>'


def render_head(root: str, title: str) -> str:
    """<head> contents, including the pre-paint theme resolution.

    The theme must be settled before first paint or a dark-mode reader gets a
    white flash, so this one script stays inline on every page.
    """
    return f"""<meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <script>
    (() => {{
      let stored = null;
      try {{ stored = localStorage.getItem('robonix-catalog-theme'); }} catch (_) {{}}
      // Only an explicit light/dark wins. Anything else — including the
      // legacy 'auto' written by the previous switcher — follows the system,
      // which a truthiness check would have pinned to light forever.
      const pref = stored === 'light' || stored === 'dark' ? stored : 'auto';
      const dark = pref === 'dark'
        || (pref === 'auto' && matchMedia('(prefers-color-scheme: dark)').matches);
      document.documentElement.dataset.bsTheme = dark ? 'dark' : 'light';
      document.documentElement.dataset.themePref = pref;
      // Arriving at ?kind=... must not flash the unfiltered list first. The
      // stylesheet hides non-matching rows off this attribute, before paint;
      // site.js clears it once it owns the filtering.
      const kind = new URLSearchParams(location.search).get('kind');
      if (['primitive', 'service', 'skill', 'robot'].includes(kind)) {{
        document.documentElement.dataset.prefilterKind = kind;
      }}
    }})();
  </script>
  <link rel="icon" type="image/svg+xml" href="{root}assets/robonix-mark.svg">
  <link rel="stylesheet" href="{root}assets/vendor/fonts/fonts.css">
  <link rel="stylesheet" href="{root}assets/vendor/bootstrap/bootstrap.min.css">
  <link rel="stylesheet" href="{root}assets/site.css">
  <link rel="stylesheet" href="{root}assets/highlight.css">"""


def render_navbar(root: str, current: str, *, search_target: str, search_placeholder: str) -> str:
    """Site-wide navbar: brand, always-present search, sections, utilities.

    Search belongs here rather than in the page body: it is the same action on
    every page. On listing pages the field filters in place; everywhere else it
    submits through to the packages listing. It is deliberately outside the
    collapse, so a phone still shows it while the sections fold behind the
    toggler — hiding the primary action behind a menu would be a regression.
    """

    def nav_link(path: str, label: str, page: str, kind: str = "") -> str:
        # The three kind links share one listing page, so which is current
        # depends on ?kind=; site.js settles that once the page is up.
        marker = f' data-nav-kind="{kind}"' if kind else ""
        active = ' aria-current="page"' if current == page and not kind else ""
        return (
            f'<li class="nav-item"><a class="nav-link" href="{root}{path}"{marker}{active}>'
            f"{label}</a></li>"
        )

    return f"""<nav class="navbar navbar-expand-lg sticky-top site-nav">
    <div class="container-xxl">
      <a class="navbar-brand" href="{root}">
        <img src="{root}assets/robonix-mark.svg" alt="" width="26" height="23">
        <span class="text-body">Robonix</span> <span>Packages</span>
      </a>
      <form class="site-search order-lg-1 mx-2 ms-lg-3 me-lg-auto" role="search"
            id="omnisearch-form" data-target="{root}{search_target}">
        {ICON_SEARCH}
        <label class="visually-hidden" for="omnisearch">Search the catalog</label>
        <input class="form-control" id="omnisearch" type="search" autocomplete="off"
               spellcheck="false" placeholder="{html.escape(search_placeholder)}">
        <kbd class="d-none d-lg-block">/</kbd>
      </form>
      <button class="navbar-toggler border-0 px-2 order-lg-3" type="button" data-bs-toggle="collapse"
              data-bs-target="#site-menu" aria-controls="site-menu" aria-expanded="false"
              aria-label="Toggle navigation">
        <span class="navbar-toggler-icon"></span>
      </button>
      <div class="collapse navbar-collapse flex-lg-grow-0 order-lg-2" id="site-menu">
        <ul class="navbar-nav align-items-lg-center">
          {nav_link('packages/?kind=skill', 'Skills', 'packages', 'skill')}
          {nav_link('packages/?kind=service', 'Services', 'packages', 'service')}
          {nav_link('packages/?kind=primitive', 'Primitives', 'packages', 'primitive')}
          {nav_link('robots/', 'Robots', 'robots')}
          {nav_link('api/view/', 'API', 'api')}
        </ul>
        <div class="d-flex align-items-center gap-2 mt-3 mt-lg-0 ms-lg-3">
          <a class="btn btn-primary btn-sm" href="{root}submit/">{ICON_PLUS} Submit</a>
          <a class="icon-btn" href="{CATALOG_REPO}" title="Catalog repository"
             aria-label="Catalog repository on GitHub">{ICON_GITHUB}</a>
          <button type="button" class="icon-btn" data-theme-toggle
                  aria-label="Colour theme" title="Colour theme">{ICON_AUTO}{ICON_SUN}{ICON_MOON}</button>
        </div>
      </div>
    </div>
  </nav>"""


def render_footer(generated_at: str) -> str:
    return f"""<footer class="site-footer py-4">
    <div class="container-xxl d-flex flex-wrap justify-content-between gap-2">
      <span>Robonix Package Catalog · indexed {html.escape(generated_at[:10])}</span>
      <span><a href="{CATALOG_REPO}">Source</a> · <a href="{DOCS_URL}">Documentation</a> · <a href="{CATALOG_REPO}/blob/main/catalog.yaml">catalog.yaml</a></span>
    </div>
  </footer>"""


def render_page(
    *,
    root: str,
    title: str,
    current: str,
    body: str,
    generated_at: str,
    search_placeholder: str = "Search packages, capabilities, robots",
    extra_scripts: str = "",
) -> str:
    """Assemble one complete page from the shared chrome and a body fragment."""
    search_target = "packages/" if current in {"", "submit", "api"} else ""
    return f"""<!doctype html>
<html lang="en">
<head>
  {render_head(root, title)}
</head>
<body>
  {render_navbar(root, current, search_target=search_target, search_placeholder=search_placeholder)}
  <main>
{body}
  </main>
  {render_footer(generated_at)}
  <script src="{root}assets/vendor/bootstrap/bootstrap.min.js" defer></script>
  <script src="{root}assets/site.js" defer></script>{extra_scripts}
</body>
</html>
"""


def detail_base(package: dict) -> str:
    return "robots" if package.get("catalog_type") == "robot" else "packages"


def entry_units(package: dict) -> tuple[int, str]:
    """Count the thing a page's rows are measured in.

    Ordinary packages advertise capabilities; robot deployments assemble
    packages. Both are the honest "size" signal for their kind of entry.
    """
    if package.get("catalog_type") == "robot":
        count = len(package.get("deploy_dependencies", []))
        return count, "dependency" if count == 1 else "dependencies"
    count = interface_count(package)
    unit = "capability" if count == 1 else "capabilities"
    provided = provided_count(package)
    if provided > 1:
        # One package, several skills: name both counts so the row says how
        # many skills it holds, not only how many interfaces.
        return count, f"capabilities in {provided} {package['kind']}s"
    return count, unit


def entry_search_text(package: dict) -> str:
    parts = [
        package["name"],
        package["version"],
        package["kind"],
        package["description"],
        package["license"],
        package["repo"],
        " ".join(package["maintainers"]),
        " ".join(package["tags"]),
        " ".join(package.get("capabilities", [])),
    ]
    parts += [
        " ".join([d.get("name", ""), d.get("repo", ""), d.get("path", "")])
        for d in package.get("deploy_dependencies", [])
    ]
    return " ".join(parts).lower()


def maintainer_names(package: dict) -> str:
    """Strip the <email> part; the listing has no room for addresses."""
    names = [m.split("<")[0].strip() or m for m in package["maintainers"]]
    return ", ".join(names)


def warning_entries(package: dict) -> list[str]:
    """Flatten catalog and deployment warnings into display strings."""
    items = [html.escape(w.get("reason", "")) for w in package.get("catalog_warnings", [])]
    for warning in package.get("deployment_warnings", []):
        label = f"{warning.get('section', '')} {warning.get('name', '')}".strip()
        prefix = f"<code>{html.escape(label)}</code>: " if label else ""
        items.append(prefix + html.escape(warning.get("reason", "")))
    return items


def preview_sources(package: dict, root: str) -> tuple[str, str]:
    """Return (src, srcset) for a robot preview, or ("", "") when it has none."""
    if package.get("_preview_image_380"):
        small = f"{root}{package['_preview_image_380']}"
        large = f"{root}{package['_preview_image_720']}"
        return html.escape(small), f"{html.escape(small)} 380w, {html.escape(large)} 720w"
    if package.get("preview_image_url"):
        return html.escape(package["preview_image_url"]), ""
    return "", ""


def render_entry_row(package: dict) -> str:
    """One card in a listing.

    Compact on purpose: an icon tile carrying the kind's colour, the name, two
    clamped lines of description, and a single metadata line. A robot leads
    with its photograph instead, the way an application listing would.
    Indexing issues are shown on the entry's own page, not here.
    """
    slug = html.escape(package_slug(package["name"]))
    kind = package["kind"]
    known = kind if kind in KIND_GLYPH else "primitive"
    unit_count, unit_label = entry_units(package)
    is_robot = package.get("catalog_type") == "robot"

    shot = ""
    if is_robot:
        src, srcset = preview_sources(package, "../")
        if src:
            srcset_attr = f' srcset="{srcset}" sizes="(max-width: 768px) 100vw, 380px"' if srcset else ""
            inner = (
                f'<img src="{src}"{srcset_attr} alt="" width="380" height="285" '
                f'loading="lazy" decoding="async">'
            )
        else:
            inner = f'<span class="entry-shot-empty">{ICON_IMAGE}</span>'
        shot = f'<div class="entry-shot">{inner}</div>'

    return f"""<li data-entry data-name="{html.escape(package['name'])}" data-kind="{html.escape(kind)}" data-units="{unit_count}" data-tags="{html.escape(' '.join(package['tags']))}" data-search="{html.escape(entry_search_text(package))}">
            <article class="entry-card k-{html.escape(known)}">
              {shot}
              <div class="entry-card-body">
                <div class="entry-card-top">
                  <span class="kind-tile" aria-hidden="true">{KIND_GLYPH[known]}</span>
                  <a class="entry-name stretched-link" href="{slug}/">{wrappable_name(package['name'])}</a>
                </div>
                <p class="entry-desc">{html.escape(package['description'])}</p>
                <div class="entry-meta">
                  <span class="m-kind">{KIND_GLYPH[known]}{html.escape(kind)}</span>
                  <span class="sep">·</span>
                  <span title="{unit_label}">{ICON_CAP}{unit_count}</span>
                  <span class="sep">·</span>
                  <span title="License">{ICON_LICENSE}{html.escape(package['license'])}</span>
                  <span class="sep">·</span>
                  <span title="Maintainer">{ICON_USER}{html.escape(maintainer_names(package))}</span>
                  <span class="sep">·</span>
                  <span class="entry-version">v{html.escape(package['version'])}</span>
                </div>
              </div>
            </article>
          </li>"""


def render_listing_page(
    public_dir: Path,
    generated_at: str,
    packages: list[dict],
    *,
    page: str,
    title: str,
    lede: str,
    noun: str,
) -> None:
    """Write /packages/ or /robots/: a sticky facet rail beside dense rows.

    The kind buttons carry no count: a package count there reads as the
    number of skills, and the list header already says how many packages
    match."""
    kind_counts: dict[str, int] = {}
    tag_counts: dict[str, int] = {}
    for package in packages:
        if package["kind"]:
            kind_counts[package["kind"]] = kind_counts.get(package["kind"], 0) + 1
        for tag in package["tags"]:
            tag_counts[tag] = tag_counts.get(tag, 0) + 1

    kind_options = "".join(
        f'<button type="button" class="facet-option k-{html.escape(kind if kind in KIND_GLYPH else "primitive")}" '
        f'data-kind-filter="{html.escape(kind)}" aria-pressed="false">'
        f'{KIND_GLYPH.get(kind, KIND_GLYPH["primitive"])}'
        f'<span>{html.escape(kind)}</span></button>'
        for kind, _count in sorted(kind_counts.items(), key=lambda kv: (-kv[1], kv[0]))
    )

    # A long tail of one-off tags pushes the useful facets off screen, so the
    # rail shows the common ones and hides the rest behind "Show all".
    ranked_tags = sorted(tag_counts.items(), key=lambda kv: (-kv[1], kv[0]))
    visible_tag_limit = 14
    tag_buttons = []
    for index, (tag, _count) in enumerate(ranked_tags):
        overflow = " hidden data-tag-overflow" if index >= visible_tag_limit else ""
        tag_buttons.append(
            f'<button type="button" class="tag" data-tag-filter="{html.escape(tag)}" aria-pressed="false"{overflow}>{html.escape(tag)}</button>'
        )
    tag_more = (
        f'<button type="button" class="facet-more mt-2" data-facet-more>Show all {len(ranked_tags)} tags</button>'
        if len(ranked_tags) > visible_tag_limit
        else ""
    )

    unit_sort_label = "Most dependencies" if page == "robots" else "Most capabilities"
    kind_facet = (
        f"""<div class="mb-4">
              <div class="facet-title mb-2">Kind</div>
              <div class="facet-pills">{kind_options}</div>
            </div>"""
        if len(kind_counts) > 1
        else ""
    )

    rows = "\n".join(render_entry_row(p) for p in packages)

    body = f"""    <div class="container-xxl">
      <header class="pt-4 pb-3">
        <h1 class="h3 mb-2">{html.escape(title)}</h1>
        <p class="text-secondary mb-0" style="max-width: 62ch">{html.escape(lede)}</p>
      </header>
      <div class="row g-4" data-listing="{html.escape(noun)}">
        <aside class="col-lg-3">
          <div class="facets">
            {kind_facet}
            <div class="mb-4">
              <div class="facet-title mb-2">Tags</div>
              <div class="d-flex flex-wrap gap-1">{''.join(tag_buttons)}</div>
              {tag_more}
            </div>
            <button type="button" class="btn btn-outline-secondary btn-sm" data-clear hidden>Clear filters</button>
          </div>
        </aside>
        <div class="col-lg-9">
          <div class="d-flex flex-wrap align-items-center justify-content-between gap-2 mb-3">
            <span class="result-count" data-count aria-live="polite"></span>
            <label class="sort-field d-flex align-items-center gap-2 mb-0">Sort
              <select class="form-select form-select-sm" data-sort aria-label="Sort order">
                <option value="name">Name</option>
                <option value="kind">Kind</option>
                <option value="units">{html.escape(unit_sort_label)}</option>
              </select>
            </label>
          </div>
          <ol class="entry-list d-flex flex-column gap-2" data-entry-list>
{rows}
          </ol>
          <div class="text-center text-secondary py-5" data-empty hidden>
            <p class="fw-semibold text-body mb-1">Nothing matches those filters.</p>
            <p class="mb-0">Try a shorter search term, or clear the kind and tag filters.</p>
          </div>
          <!-- Filled in by site.js. Every row is rendered above, so without
               JavaScript the whole list shows and an empty pager is correct. -->
          <nav class="mt-4" aria-label="Pagination" data-pager hidden>
            <ul class="pagination pagination-sm justify-content-center mb-0" data-pager-list></ul>
          </nav>
        </div>
      </div>
    </div>"""

    page_dir = public_dir / page
    page_dir.mkdir(parents=True, exist_ok=True)
    page_dir.joinpath("index.html").write_text(
        render_page(
            root="../",
            title=f"{title} · Robonix Package Catalog",
            current=page,
            body=body,
            generated_at=generated_at,
            search_placeholder=f"Filter {noun}",
        ),
        encoding="utf-8",
    )


def render_home(public_dir: Path, generated_at: str, packages: list[dict]) -> None:
    """Write the landing page.

    No search box in the page body — search lives in the navbar. The page is
    ordered by what a visitor most likely wants: the skills a robot can be
    asked to run, then the robots themselves, then the substrate those are
    built on, then how to publish and how to consume.
    """
    package_entries = [p for p in packages if p.get("catalog_type") != "robot"]
    robot_entries = [p for p in packages if p.get("catalog_type") == "robot"]
    capability_total = sum(interface_count(p) for p in package_entries)
    by_kind = {kind: [p for p in package_entries if p["kind"] == kind] for kind in KIND_ORDER}
    # Counted in skills (services, primitives), not packages: one package can
    # provide several, one per contract namespace.
    provided_by_kind = {kind: sum(provided_count(p) for p in by_kind[kind]) for kind in KIND_ORDER}

    # Skills are the applications of the platform, so the homepage shows real
    # skill packages rather than a link to a filtered list.
    skills = sorted(by_kind["skill"], key=lambda p: p["name"])
    shelf_limit = 7
    skill_cards = []
    for skill in skills[:shelf_limit]:
        slug = html.escape(package_slug(skill["name"]))
        count = interface_count(skill)
        unit = "capability" if count == 1 else "capabilities"
        provided = provided_count(skill)
        if provided > 1:
            unit = f"{unit} · {provided} skills"
        skill_cards.append(
            f"""<div class="col">
            <a class="card card-link-block h-100 p-3 d-flex flex-column gap-2" href="packages/{slug}/">
              <span class="card-name">{wrappable_name(skill['name'])}</span>
              <p class="card-desc mb-0">{html.escape(skill['description'])}</p>
              <span class="card-foot mt-auto">{count} {unit}</span>
            </a>
          </div>"""
        )
    if len(skills) > shelf_limit:
        skill_cards.append(
            f"""<div class="col">
            <a class="card card-link-block card-more h-100 d-flex flex-row align-items-center justify-content-center gap-2" href="packages/?kind=skill">
              <span>All {provided_by_kind['skill']} skills</span>{ICON_ARROW}
            </a>
          </div>"""
        )

    robot_cards = []
    for robot in robot_entries:
        slug = html.escape(package_slug(robot["name"]))
        src, srcset = preview_sources(robot, "")
        if src:
            srcset_attr = f' srcset="{srcset}" sizes="220px"' if srcset else ""
            shot = (
                f'<img src="{src}"{srcset_attr} alt="" width="380" height="285" '
                f'loading="lazy" decoding="async">'
            )
        else:
            shot = "<span>no preview</span>"
        robot_cards.append(
            f"""<a class="card card-link-block overflow-hidden" href="robots/{slug}/">
            <div class="robot-shot">{shot}</div>
            <div class="p-3">
              <span class="card-name d-block">{wrappable_name(robot['name'])}</span>
              <p class="card-desc mb-0 mt-1">{html.escape(robot['description'])}</p>
            </div>
          </a>"""
        )

    substrate_cards = []
    for kind in ("primitive", "service"):
        substrate_cards.append(
            f"""<div class="col">
            <a class="card card-link-block h-100 p-3" href="packages/?kind={kind}">
              <div class="d-flex align-items-center justify-content-between gap-2 mb-2">
                {kind_chip(kind)}
                <span class="card-foot">{provided_by_kind[kind]}</span>
              </div>
              <h3 class="h6 mb-2">{kind.capitalize()}s</h3>
              <p class="card-desc mb-3" style="-webkit-line-clamp: 4; line-clamp: 4">{html.escape(KIND_BLURB[kind])}</p>
              <span class="card-go d-inline-flex align-items-center gap-1 mt-auto">Browse {kind}s {ICON_ARROW}</span>
            </a>
          </div>"""
        )

    rail_links = []
    for kind in KIND_ORDER:
        rail_links.append(
            f'<a class="rail-link k-{kind}" href="packages/?kind={kind}">'
            f'{KIND_GLYPH[kind]}<span>{kind.capitalize()}s</span>'
            f'<span class="n">{provided_by_kind[kind]}</span></a>'
        )
    rail_links.append(
        f'<a class="rail-link k-robot" href="robots/">'
        f'{KIND_GLYPH["robot"]}<span>Robots</span>'
        f'<span class="n">{len(robot_entries)}</span></a>'
    )
    rail = f"""<nav class="home-rail" aria-label="Browse the catalog">
            <div class="facet-title mb-2">Browse</div>
            <div class="rail-links">{''.join(rail_links)}</div>
            <div class="facet-title mt-4 mb-2">Contribute</div>
            <div class="rail-links">
              <a class="rail-link" href="submit/">{ICON_PLUS}<span>Submit a package</span></a>
              <a class="rail-link" href="api/view/">{ICON_JSON}<span>Catalog API</span></a>
              <a class="rail-link" href="{DOCS_URL}">{ICON_ARROW}<span>Documentation</span></a>
            </div>
          </nav>"""

    body = f"""    <div class="container-xxl">
      <div class="row g-4">
        <aside class="col-lg-3">
          {rail}
        </aside>
        <div class="col-lg-9">
    <section class="hero">
      <div class="hero-glow" aria-hidden="true"></div>
      <div class="hero-marks" aria-hidden="true">
        <span class="hero-mark">{KIND_GLYPH['primitive']}</span>
        <span class="hero-mark">{KIND_GLYPH['service']}</span>
        <span class="hero-mark">{KIND_GLYPH['skill']}</span>
        <span class="hero-mark">{KIND_GLYPH['robot']}</span>
      </div>
      <h1 class="mb-3">The Robonix package catalog.</h1>
      <p class="hero-lede mb-4">Skills, services, primitives, and complete robot deployments,
      published by the community. Every entry is read straight from its own repository's
      manifest, so what you see here is what the package actually declares.</p>
      <div class="d-flex flex-wrap gap-4 mb-4">
        <span class="hero-stat d-flex align-items-baseline gap-2"><b>{provided_by_kind['skill']}</b><span>skills</span></span>
        <span class="hero-stat d-flex align-items-baseline gap-2"><b>{len(package_entries)}</b><span>packages</span></span>
        <span class="hero-stat d-flex align-items-baseline gap-2"><b>{len(robot_entries)}</b><span>robot deployments</span></span>
        <span class="hero-stat d-flex align-items-baseline gap-2"><b>{capability_total}</b><span>declared capabilities</span></span>
      </div>
      <div class="d-flex flex-wrap gap-2">
        <a class="btn btn-primary" href="packages/?kind=skill">Browse skills</a>
        <a class="btn btn-outline-secondary" href="submit/">{ICON_PLUS} Submit a package</a>
      </div>
    </section>

    <section class="mt-5">
      <div class="section-head d-flex flex-wrap align-items-baseline justify-content-between gap-3 mb-3">
        <div>
          <h2 class="mb-1">Skills</h2>
          <p class="mb-0">{html.escape(KIND_BLURB['skill'])}</p>
        </div>
        <a class="section-more d-inline-flex align-items-center gap-1" href="packages/?kind=skill">All skills {ICON_ARROW}</a>
      </div>
      <div class="row row-cols-1 row-cols-sm-2 row-cols-lg-3 row-cols-xl-4 g-3">
        {''.join(skill_cards)}
      </div>
    </section>

    <section class="mt-5">
      <div class="section-head d-flex flex-wrap align-items-baseline justify-content-between gap-3 mb-3">
        <div>
          <h2 class="mb-1">Robot deployments</h2>
          <p class="mb-0">Repositories that assemble a whole robot: body description, drivers, services, skills, runtime configuration.</p>
        </div>
        <a class="section-more d-inline-flex align-items-center gap-1" href="robots/">All robots {ICON_ARROW}</a>
      </div>
      <div class="robot-rail">
        {''.join(robot_cards)}
      </div>
    </section>

    <section class="mt-5">
      <div class="section-head d-flex flex-wrap align-items-baseline justify-content-between gap-3 mb-3">
        <div>
          <h2 class="mb-1">The substrate</h2>
          <p class="mb-0">What skills are built on. Providers of the same capability are interchangeable, so a skill written once runs on any robot that has them.</p>
        </div>
        <a class="section-more d-inline-flex align-items-center gap-1" href="packages/">All packages {ICON_ARROW}</a>
      </div>
      <div class="row row-cols-1 row-cols-md-2 g-3">
        {''.join(substrate_cards)}
      </div>
    </section>

    <section class="mt-5">
      <div class="row row-cols-1 row-cols-lg-2 g-3">
        <div class="col">
          <div class="card h-100 p-4">
            <h3 class="h6 mb-2">Publish a package</h3>
            <p class="text-secondary small mb-3">Your code stays in your repository. Listing it
            takes one entry in <code>catalog.yaml</code> — everything else is read from your manifest.</p>
            <pre class="snippet mb-3"><code>packages:
  - name: robonix.service.mapping
    repo: https://github.com/syswonder/service-map-rbnx</code></pre>
            <a class="btn btn-primary align-self-start mt-auto" href="submit/">{ICON_PLUS} Submit a package</a>
          </div>
        </div>
        <div class="col">
          <div class="card h-100 p-4">
            <h3 class="h6 mb-2">Read it as an API</h3>
            <p class="text-secondary small mb-3">The whole catalog is static JSON on the same host.
            No key, no rate limit, no query parameters — fetch it and filter on the client.</p>
            <pre class="snippet mb-3"><code>curl -s {SITE_URL}/api/v1/packages.json</code></pre>
            <a class="btn btn-outline-secondary align-self-start mt-auto" href="api/view/">{ICON_JSON} Explore the API</a>
          </div>
        </div>
      </div>
    </section>
        </div>
      </div>
    </div>"""

    (public_dir / "index.html").write_text(
        render_page(
            root="",
            title="Robonix Package Catalog",
            current="",
            body=body,
            generated_at=generated_at,
        ),
        encoding="utf-8",
    )


def render_submit_page(public_dir: Path, generated_at: str, packages: list[dict]) -> None:
    """Write /submit/.

    GitHub Pages is static, so the page cannot open a pull request itself. What
    it can do is everything up to that point: validate the entry against the
    same rules the builder enforces, emit the exact YAML, and hand the reader
    to GitHub's own edit-and-propose flow, which forks the catalog for them.
    """
    body = f"""    <div class="container-xxl">
      <nav class="crumbs pt-3"><a href="../">Catalog</a> / <span>Submit</span></nav>
      <header class="pt-2 pb-4">
        <h1 class="h3 mb-2">Submit a package</h1>
        <p class="text-secondary mb-0" style="max-width: 68ch">Listing a package takes one
        <code>name</code> + <code>repo</code> entry in <code>catalog.yaml</code>. Your code stays
        in your own repository; the catalog reads its manifest and rebuilds this site daily.</p>
      </header>

      <div class="row g-4">
        <div class="col-lg-7">
          <div class="submit-form d-flex flex-column gap-3">
            <div class="card p-4">
              <h2 class="aside-title mb-3">1 · Describe the entry</h2>
              <div class="mb-3">
                <span class="field-label d-block mb-2" id="entryKind">What are you listing?</span>
                <div class="btn-group btn-group-sm" role="group" aria-labelledby="entryKind">
                  <button type="button" class="btn btn-outline-secondary active" data-section="packages" aria-pressed="true">A package</button>
                  <button type="button" class="btn btn-outline-secondary" data-section="robots" aria-pressed="false">A whole robot</button>
                </div>
                <p class="field-help mt-2 mb-0" data-section-help>Primitives, services and skills all go under <code>packages:</code>.</p>
              </div>
              <div data-entry-row>
                <div class="field mb-3">
                  <label class="field-label d-block mb-2" for="entryName">Catalog name</label>
                  <input class="form-control submit-input" id="entryName" data-name type="text"
                         spellcheck="false" autocomplete="off" placeholder="robonix.skill.pick.vertical_grasp">
                  <p class="field-help mt-2 mb-0">Must match <code>package.name</code> in your manifest exactly.</p>
                  <p class="field-error mt-2 mb-0" data-error hidden></p>
                </div>
                <div class="field mb-3">
                  <label class="field-label d-block mb-2" for="entryRepo">GitHub repository</label>
                  <input class="form-control submit-input" id="entryRepo" data-repo type="url"
                         spellcheck="false" autocomplete="off" placeholder="https://github.com/owner/repo">
                  <p class="field-help mt-2 mb-0">Public repository with a root-level manifest on its default branch.</p>
                  <p class="field-error mt-2 mb-0" data-error hidden></p>
                </div>
              </div>
              <button type="button" class="btn btn-outline-secondary btn-sm align-self-start" data-add>{ICON_PLUS} Add another entry</button>
            </div>

            <div class="card p-4">
              <h2 class="aside-title mb-3">2 · Copy the YAML</h2>
              <pre class="snippet mb-3"><code data-yaml>packages:
  - name: robonix.service.mapping
    repo: https://github.com/syswonder/service-map-rbnx</code></pre>
              <button type="button" class="btn btn-outline-secondary btn-sm align-self-start" data-copy>Copy snippet</button>
            </div>

            <div class="card p-4">
              <h2 class="aside-title mb-3">3 · Open the pull request</h2>
              <p class="field-help mb-3">GitHub forks the catalog for you and turns the edit into a
              pull request. Paste your entry into the matching section, keeping the list in
              alphabetical order.</p>
              <div class="d-flex flex-wrap gap-2 mb-3">
                <a class="btn btn-primary" data-edit-link href="{CATALOG_REPO}/edit/main/catalog.yaml">{ICON_GITHUB} Edit catalog.yaml on GitHub</a>
                <a class="btn btn-outline-secondary" data-issue-link href="{CATALOG_REPO}/issues/new">Open a request instead</a>
              </div>
              <p class="field-help mb-0">Not comfortable with YAML? The second button files an issue
              with your details filled in, and a maintainer adds the entry.</p>
            </div>
          </div>
        </div>

        <div class="col-lg-5">
          <div class="d-flex flex-column gap-3">
            <div class="card p-4">
              <h2 class="aside-title mb-3">What CI checks</h2>
              <ul class="check-list mb-0 ps-3">
                <li>The repository is public and has a root-level
                <code>package_manifest.yaml</code> (or <code>robonix_manifest.yaml</code> for a robot).</li>
                <li><code>package.name</code> in that manifest matches the name you submit here.</li>
                <li><code>version</code>, <code>description</code>, <code>license</code>,
                <code>tags</code> and <code>maintainers</code> are all present.</li>
                <li>Maintainers are written as <code>Name &lt;email@domain&gt;</code>.</li>
                <li>Every declared capability names a real Robonix contract.</li>
                <li>For robots, each dependency resolves to a cataloged repository or a path
                inside the robot repository itself.</li>
              </ul>
            </div>
            <div class="card p-4">
              <h2 class="aside-title mb-3">Manifest template</h2>
              <pre class="snippet mb-3"><code>package:
  name: robonix.service.mapping
  version: 0.4.0
  description: Map and SLAM service package.
  license: MulanPSL-2.0
  tags: [service, mapping, slam]
  maintainers:
    - Your Name &lt;you@example.com&gt;

capabilities:
  - name: robonix/service/map/save_map</code></pre>
              <a class="section-more d-inline-flex align-items-center gap-1" href="{DOCS_URL}">Full packaging guide {ICON_ARROW}</a>
            </div>
          </div>
        </div>
      </div>
    </div>"""

    existing_json = json.dumps(sorted(p["name"] for p in packages))
    extra = (
        f'\n  <script id="catalog-names" type="application/json">{existing_json}</script>'
        '\n  <script src="../assets/submit.js" defer></script>'
    )

    submit_dir = public_dir / "submit"
    submit_dir.mkdir(parents=True, exist_ok=True)
    submit_dir.joinpath("index.html").write_text(
        render_page(
            root="../",
            title="Submit a package · Robonix Package Catalog",
            current="submit",
            body=body,
            generated_at=generated_at,
            extra_scripts=extra,
        ),
        encoding="utf-8",
    )


def render_api_viewer(public_dir: Path, generated_at: str) -> None:
    """Write /api/view/: the reference table plus a live JSON pane."""
    rows = [
        ("catalog", "/api/v1/catalog.json", "Both ordinary packages and robot deployments in one object."),
        ("packages", "/api/v1/packages.json", "Primitive, service and skill packages only."),
        ("robots", "/api/v1/robots.json", "Robot deployment entries only."),
        ("search", "/api/v1/search.json", "The combined catalog as a plain array, for client-side indexes."),
    ]
    table_rows = "".join(
        f'<tr><td><span class="method">GET</span></td>'
        f'<td><a href="?resource={key}"><code>{path}</code></a></td>'
        f"<td>{desc}</td></tr>"
        for key, path, desc in rows
    )
    table_rows += (
        '<tr><td><span class="method">GET</span></td>'
        "<td><code>/api/v1/package/&lt;name&gt;.json</code></td>"
        "<td>One package or robot deployment. Unknown names return a Pages 404.</td></tr>"
    )

    body = f"""    <div class="container-xxl">
      <nav class="crumbs pt-3"><a href="../../">Catalog</a> / <span>API</span></nav>
      <header class="pt-2 pb-4">
        <h1 class="h3 mb-2">Catalog API</h1>
        <p class="text-secondary mb-0" style="max-width: 72ch">Static JSON served from the same
        host as this site. Use <code>GET</code>; there is no key, no rate limit and no server-side
        query parameter — fetch a resource and filter it on the client.</p>
      </header>
      <div class="card overflow-hidden">
        <div class="table-responsive">
          <table class="table table-borderless api-table align-top mb-0">
            <thead><tr><th>Method</th><th>Resource</th><th>Returns</th></tr></thead>
            <tbody>{table_rows}</tbody>
          </table>
        </div>
      </div>
      <div class="mt-5">
        <div class="section-head mb-3">
          <h2 class="mb-1" id="apiTitle">Response</h2>
          <p class="mb-0"><code id="apiEndpoint">GET /api/v1/catalog.json</code></p>
        </div>
        <div class="card overflow-hidden">
          <div class="readme-head px-3 py-2 border-bottom"><span>JSON</span></div>
          <pre class="api-json mb-0"><code id="apiJson">Loading…</code></pre>
        </div>
      </div>
    </div>"""

    extra = """
  <script>
    (() => {
      const params = new URLSearchParams(window.location.search);
      const packageName = params.get('package');
      const allowed = new Set(['catalog', 'packages', 'robots', 'search']);
      const requested = params.get('resource') || 'catalog';
      const resource = allowed.has(requested) ? requested : 'catalog';
      const file = packageName
        ? `../v1/package/${encodeURIComponent(packageName)}.json`
        : `../v1/${resource}.json`;
      const endpoint = packageName
        ? `/api/v1/package/${packageName}.json`
        : `/api/v1/${resource}.json`;
      document.getElementById('apiTitle').textContent = packageName || 'Response';
      document.getElementById('apiEndpoint').textContent = `GET ${endpoint}`;
      fetch(file)
        .then((response) => {
          if (!response.ok) throw new Error(`HTTP ${response.status}`);
          return response.json();
        })
        .then((data) => {
          document.getElementById('apiJson').textContent = JSON.stringify(data, null, 2);
        })
        .catch((error) => {
          document.getElementById('apiJson').textContent = `Unable to load ${endpoint}: ${error.message}`;
        });
    })();
  </script>"""

    viewer_dir = public_dir / "api" / "view"
    viewer_dir.mkdir(parents=True, exist_ok=True)
    viewer_dir.joinpath("index.html").write_text(
        render_page(
            root="../../",
            title="Catalog API · Robonix Package Catalog",
            current="api",
            body=body,
            generated_at=generated_at,
            extra_scripts=extra,
        ),
        encoding="utf-8",
    )


def render_site(public_dir: Path, generated_at: str, packages: list[dict]) -> None:
    package_entries = [p for p in packages if p.get("catalog_type") != "robot"]
    robot_entries = [p for p in packages if p.get("catalog_type") == "robot"]
    public_dir.mkdir(parents=True, exist_ok=True)
    copy_assets(public_dir)
    prepare_preview_images(public_dir, packages)
    render_listing_page(
        public_dir,
        generated_at,
        package_entries,
        page="packages",
        title="Packages",
        lede="Reusable primitive, service and skill packages, indexed by capability, source and maintainer.",
        noun="packages",
    )
    render_listing_page(
        public_dir,
        generated_at,
        robot_entries,
        page="robots",
        title="Robot deployments",
        lede="Complete robot deployments, and the packages each one assembles.",
        noun="robot deployments",
    )
    render_home(public_dir, generated_at, packages)
    render_submit_page(public_dir, generated_at, packages)
    render_api_viewer(public_dir, generated_at)


def absolute_readme_url(value: str, repo_url: str, branch: str, *, raw: bool) -> str:
    """Resolve a README-relative link against the indexed repository root."""
    value = html.unescape(value)
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme or parsed.netloc or value.startswith(("#", "//")):
        return value
    owner, repo = parse_repo(repo_url)
    path = posixpath.normpath(parsed.path.lstrip("/"))
    if path in ("", "."):
        path = "README.md"
    escaped_branch = urllib.parse.quote(branch, safe="")
    escaped_path = urllib.parse.quote(path, safe="/")
    if raw:
        base = f"https://raw.githubusercontent.com/{owner}/{repo}/{escaped_branch}/{escaped_path}"
    else:
        base = f"{repo_url}/blob/{escaped_branch}/{escaped_path}"
    return urllib.parse.urlunsplit(("", "", base, parsed.query, parsed.fragment))


def rewrite_readme_urls(rendered: str, repo_url: str, branch: str) -> str:
    """Make repository-relative README images and links work on catalog pages."""
    def replace(pattern: str, source: str, *, raw: bool) -> str:
        def repl(match: re.Match) -> str:
            resolved = absolute_readme_url(match.group(2), repo_url, branch, raw=raw)
            return match.group(1) + html.escape(resolved, quote=True) + match.group(3)

        return re.sub(pattern, repl, source, flags=re.IGNORECASE)

    rendered = replace(r'(<img\b[^>]*\bsrc=")([^"]+)(")', rendered, raw=True)
    return replace(r'(<a\b[^>]*\bhref=")([^"]+)(")', rendered, raw=False)


def enable_markdown_in_html_divs(md: str) -> str:
    """Parse GitHub-style Markdown inside standalone HTML div containers."""
    rendered_lines = []
    fence_char = None
    fence_length = 0
    fence_pattern = re.compile(r"^ {0,3}(`{3,}|~{3,})")
    div_pattern = re.compile(
        r'^( {0,3})<div\b(?![^>]*\bmarkdown\s*=)([^>]*)>([ \t]*)$',
        re.IGNORECASE,
    )

    for line in md.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        ending = line[len(body) :]
        fence = fence_pattern.match(body)
        if fence:
            marker = fence.group(1)
            if fence_char is None:
                fence_char = marker[0]
                fence_length = len(marker)
            elif marker[0] == fence_char and len(marker) >= fence_length:
                fence_char = None
                fence_length = 0
        elif fence_char is None:
            body = div_pattern.sub(r'\1<div markdown="1"\2>\3', body)
        rendered_lines.append(body + ending)

    return "".join(rendered_lines)


def render_markdown(md: str, repo_url: str, branch: str) -> str:
    if not md.strip():
        return "<p>No README.md was found in this package repository.</p>"
    rendered = markdown.markdown(
        enable_markdown_in_html_divs(md),
        extensions=["tables", "md_in_html", "pymdownx.highlight", "pymdownx.superfences"],
        extension_configs={
            "pymdownx.highlight": {
                "css_class": "highlight",
                "guess_lang": False,
                "use_pygments": True,
            }
        },
    )
    rendered = rewrite_readme_urls(rendered, repo_url, branch)
    allowed_tags = set(bleach.sanitizer.ALLOWED_TAGS).union(
        {
            "div",
            "span",
            "p",
            "pre",
            "code",
            "img",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "ul",
            "ol",
            "li",
            "table",
            "thead",
            "tbody",
            "tr",
            "th",
            "td",
            "blockquote",
            "hr",
            "br",
        }
    )
    allowed_attrs = {
        "a": ["href", "title"],
        "code": ["class"],
        "div": ["class", "align"],
        "span": ["class"],
        "img": ["src", "alt", "title", "width", "height", "loading"],
        "th": ["align"],
        "td": ["align"],
    }
    return bleach.clean(rendered, tags=allowed_tags, attributes=allowed_attrs, strip=True)

def render_dependency_items(package: dict) -> str:
    """Render a robot's assembled packages, linking the ones we index."""
    items = []
    for dep in package.get("deploy_dependencies", []):
        if dep.get("package_name"):
            slug = html.escape(package_slug(dep["package_name"]))
            target = f'<a class="dep-target" href="../../packages/{slug}/">{html.escape(dep["package_name"])}</a>'
        elif dep.get("repo"):
            target = f'<a class="dep-target" href="{html.escape(dep["repo"])}">{html.escape(dep["repo"])}</a>'
        else:
            label = dep.get("path") or dep.get("name") or "unresolved"
            target = f'<span class="dep-target">{html.escape(label)}</span>'
        warning = dep.get("resolution_warning", "")
        reason = f'<span class="dep-reason">{html.escape(warning)}</span>' if warning else ""
        badge = '<span class="chip chip-warn">!</span>' if warning else ""
        section = dep.get("section", "")
        # The section is exactly a provider kind, so colour it like one.
        kind_class = f" k-{section}" if section in KIND_GLYPH else ""
        items.append(
            f'<li><div class="d-flex flex-wrap align-items-center gap-2">'
            f'<span class="dep-section{kind_class}">{html.escape(section)}</span>'
            f'<code class="dep-name">{html.escape(dep.get("name", ""))}</code>{badge}</div>'
            f"{target}{reason}</li>"
        )
    return "".join(items)


def render_package_pages(public_dir: Path, generated_at: str, packages: list[dict]) -> None:
    """Write one detail page per catalog entry.

    README-first with a sticky metadata rail: the README is what a reader came
    for, and the rail keeps version, license and capabilities in view while
    they scroll through it.
    """
    for p in packages:
        base = detail_base(p)
        is_robot = p.get("catalog_type") == "robot"
        package_dir = public_dir / base / package_slug(p["name"])
        package_dir.mkdir(parents=True, exist_ok=True)

        warnings = warning_entries(p)
        warning_block = ""
        if warnings:
            label = "issue" if len(warnings) == 1 else "issues"
            items = "".join(f"<li>{w}</li>" for w in warnings)
            warning_block = (
                f'<div class="warn-box mt-3" role="note">'
                f'<p class="fw-semibold mb-2">{len(warnings)} {label} found while indexing this entry</p>'
                f'<ul class="mb-0 ps-3">{items}</ul></div>'
            )

        if is_robot:
            provides_title = "Assembled packages"
            provides = render_dependency_items(p)
            provides_class = "dep-list"
            empty_note = "This deployment lists no packages."
        else:
            groups = provided_groups(p)
            if len(groups) > 1:
                # Several skills in one package: one block per skill, its
                # interfaces under it, so the page lists the skills by name.
                provides_title = f"{len(groups)} {p['kind'].capitalize()}s"
                grouped = set()
                parts = []
                for g in groups:
                    grouped.update(g["capabilities"])
                    caps = "".join(f"<li>{html.escape(c)}</li>" for c in g["capabilities"])
                    parts.append(
                        f'<li class="cap-group"><span class="cap-group-name">'
                        f'{html.escape(g["namespace"].rsplit("/", 1)[-1])}</span>'
                        f'<ul class="cap-list">{caps}</ul></li>'
                    )
                rest = [c for c in p["capabilities"] if c not in grouped]
                if rest:
                    caps = "".join(f"<li>{html.escape(c)}</li>" for c in rest)
                    parts.append(f'<li class="cap-group"><span class="cap-group-name">other</span>'
                                 f'<ul class="cap-list">{caps}</ul></li>')
                provides = "".join(parts)
                provides_class = "cap-groups"
            else:
                provides_title = "Capabilities"
                provides = "".join(f"<li>{html.escape(c)}</li>" for c in p["capabilities"])
                provides_class = "cap-list"
            empty_note = "This package declares no capability contracts."
        provides_block = (
            f'<ul class="{provides_class}">{provides}</ul>'
            if provides
            else f'<p class="text-secondary small mb-0">{empty_note}</p>'
        )

        src, srcset = preview_sources(p, "../../")
        shot = ""
        if src:
            srcset_attr = f' srcset="{srcset}" sizes="360px"' if srcset else ""
            shot = (
                f'<div class="robot-shot"><img src="{src}"{srcset_attr} alt="" '
                f'width="380" height="285" decoding="async"></div>'
            )

        tags_block = (
            f'<div class="card p-3"><h2 class="aside-title mb-2">Tags</h2>'
            f'<div class="d-flex flex-wrap gap-1">{render_tags(p["tags"], interactive=False)}</div></div>'
            if p["tags"]
            else ""
        )

        readme_html = render_markdown(
            p.get("_readme_markdown", ""), p["repo"], p["default_branch"]
        )
        api_href = f"../../api/view/?package={urllib.parse.quote(p['name'], safe='')}"
        # Same target as the readme_url field, derived here so rendering needs
        # only the repository and branch a page already shows.
        readme_href = f"{p['repo']}/blob/{p['default_branch']}/README.md"
        crumb_label = "Robots" if is_robot else "Packages"

        body = f"""    <div class="container-xxl">
      <nav class="crumbs pt-3">
        <a href="../../">Catalog</a> / <a href="../">{crumb_label}</a> / <span>{html.escape(p['name'])}</span>
      </nav>
      <header class="pt-3 pb-4 border-bottom">
        <div class="d-flex flex-wrap align-items-center gap-2">
          <h1 class="detail-name mb-0">{wrappable_name(p['name'])}</h1>
          {kind_chip(p['kind'])}
          <span class="chip chip-plain">v{html.escape(p['version'])}</span>
        </div>
        <p class="detail-desc mt-3 mb-0">{html.escape(p['description'])}</p>
        {warning_block}
        <div class="d-flex flex-wrap gap-2 mt-4">
          <a class="btn btn-primary" href="{html.escape(p['repo'])}">{ICON_GITHUB} View source</a>
          <a class="btn btn-outline-secondary" href="{html.escape(readme_href)}">Open README on GitHub</a>
          <a class="btn btn-outline-secondary" href="{api_href}">{ICON_JSON} JSON</a>
        </div>
      </header>
      <div class="row g-4 mt-0">
        <div class="col-lg-8">
          <div class="card overflow-hidden">
            <div class="readme-head d-flex align-items-center justify-content-between gap-3 px-4 py-2 border-bottom">
              <span>README</span>
              <span>{html.escape(p['repo_name'])}@{html.escape(p['default_branch'])}</span>
            </div>
            <div class="prose p-4">
{readme_html}
            </div>
          </div>
        </div>
        <aside class="col-lg-4">
          <div class="detail-aside d-flex flex-column gap-3">
            <div class="card overflow-hidden">
              {shot}
              <div class="p-3">
                <h2 class="aside-title mb-3">Entry</h2>
                <dl class="kv mb-0">
                  <dt>Version</dt><dd><code>{html.escape(p['version'])}</code></dd>
                  <dt>License</dt><dd><code>{html.escape(p['license'])}</code></dd>
                  <dt>Kind</dt><dd>{html.escape(p['kind'])}</dd>
                  <dt>Repository</dt><dd><a href="{html.escape(p['repo'])}">{html.escape(p['repo_name'])}</a></dd>
                  <dt>Branch</dt><dd><code>{html.escape(p['default_branch'])}</code></dd>
                  <dt>Manifest</dt><dd><code>{html.escape(p['manifest'])}</code></dd>
                  <dt>Maintainers</dt><dd>{html.escape(maintainer_names(p))}</dd>
                </dl>
              </div>
            </div>
            <div class="card p-3">
              <h2 class="aside-title mb-2">{provides_title}</h2>
              {provides_block}
            </div>
            {tags_block}
          </div>
        </aside>
      </div>
    </div>"""

        package_dir.joinpath("index.html").write_text(
            render_page(
                root="../../",
                title=f"{p['name']} · Robonix Package Catalog",
                current=base,
                body=body,
                generated_at=generated_at,
            ),
            encoding="utf-8",
        )


def copy_assets(public_dir: Path) -> None:
    """Copy the static assets every page links to.

    The stylesheet and scripts are real files rather than inline blocks, so a
    browser fetches them once for the whole site instead of re-parsing a copy
    embedded in each of the generated pages. Bootstrap is vendored rather than
    loaded from a CDN: the catalog is read mostly from mainland China, where
    third-party CDNs are unreliable.
    """
    asset_dir = public_dir / "assets"
    asset_dir.mkdir(parents=True, exist_ok=True)
    for asset in SITE_ASSETS:
        shutil.copyfile(asset, asset_dir / asset.name)
    shutil.copytree(VENDOR_ASSETS, asset_dir / "vendor", dirs_exist_ok=True)
    write_highlight_css(asset_dir / "highlight.css")


def write_highlight_css(path: Path) -> None:
    """Emit the Pygments colour rules for fenced code in rendered READMEs.

    Generated rather than vendored so the classes always match the Pygments
    that produced the markup. The dark variant is scoped under data-bs-theme,
    so both themes ship in one stylesheet.
    """
    light = HtmlFormatter(style="friendly").get_style_defs(".prose .highlight")
    dark = HtmlFormatter(style="monokai").get_style_defs(
        '[data-bs-theme="dark"] .prose .highlight'
    )
    path.write_text(
        "/* Generated by scripts/build_catalog.py from Pygments. */\n"
        f"{light}\n{dark}\n",
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--catalog", default="catalog.yaml")
    parser.add_argument("--public", default="public")
    args = parser.parse_args()

    generated_at = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    catalog_path = Path(args.catalog)
    packages = collect(catalog_path, catalog_baseline_keys(catalog_path))
    report_catalog_warnings(packages)
    report_deployment_warnings(packages)
    public = Path(args.public)
    if public.exists():
        shutil.rmtree(public)
    public_api = public / "api"
    public_packages = [
        {k: v for k, v in package.items() if not k.startswith("_")} for package in packages
    ]
    package_entries = [p for p in public_packages if p.get("catalog_type") != "robot"]
    robot_entries = [p for p in public_packages if p.get("catalog_type") == "robot"]
    catalog_payload = {"api_version": "1", "generated_at": generated_at, "packages": public_packages}
    package_payload = {"api_version": "1", "generated_at": generated_at, "packages": package_entries}
    robot_payload = {"api_version": "1", "generated_at": generated_at, "robots": robot_entries}
    write_json(public_api / "catalog.json", catalog_payload)
    write_json(public_api / "packages.json", package_payload)
    write_json(public_api / "robots.json", robot_payload)
    write_json(public_api / "search.json", public_packages)
    write_api(public_api / "v1" / "catalog", catalog_payload)
    write_api(public_api / "v1" / "packages", package_payload)
    write_api(public_api / "v1" / "robots", robot_payload)
    write_api(public_api / "v1" / "search", public_packages)
    for package in public_packages:
        write_json(public_api / "packages" / f"{package['name']}.json", package)
        write_api(public_api / "v1" / "package" / package["name"], package)
    render_site(public, generated_at, packages)
    render_package_pages(public, generated_at, packages)
    print(f"generated {len(packages)} package(s)")


if __name__ == "__main__":
    main()
