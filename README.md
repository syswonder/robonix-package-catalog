# Robonix Package Catalog

This repository is the Robonix community package catalog. Browse it at
**https://packages.robonix.ai/**.

Package source and robot deployment manifests stay in their own GitHub
repositories. The only manual catalog input is the root-level
[`catalog.yaml`](catalog.yaml). Ordinary packages go under `packages:`;
whole-robot deploy repositories go under `robots:`:

```yaml
packages:
  - name: robonix.service.mapping
    repo: https://github.com/syswonder/service-map-rbnx

robots:
  - name: robonix.robot.agilex.ranger_mini_v3
    repo: https://github.com/syswonder/robot-agilex-ranger_mini_v3
```

To submit a community package or robot deployment, add one `name` + `repo`
entry to the correct section in `catalog.yaml`. That is the whole submission:
everything else on the website is derived from the manifest in your own
repository.

## Website

- Homepage: https://packages.robonix.ai/
- Package page: https://packages.robonix.ai/packages/
- Robot deployment page: https://packages.robonix.ai/robots/
- Full catalog API: `GET https://packages.robonix.ai/api/v1/catalog.json`
- Package list API: `GET https://packages.robonix.ai/api/v1/packages.json`
- Robot deployment API: `GET https://packages.robonix.ai/api/v1/robots.json`
- Search index API: `GET https://packages.robonix.ai/api/v1/search.json`
- Package detail API: `GET https://packages.robonix.ai/api/v1/package/<package-name>.json`
- Package detail page: `https://packages.robonix.ai/packages/<package-name>/`
- Robot detail page: `https://packages.robonix.ai/robots/<robot-name>/`

The catalog is hosted on GitHub Pages, so these are static JSON resources
with stable API-style paths. Clients should treat the shape below as the v1
contract.

## API Reference

All endpoints are static JSON resources served from GitHub Pages. Use
`GET`; no API key is required. There are no server-side query parameters
because Pages is static. Filter by name, kind, tag, maintainer, or
capability on the client using the returned JSON.

| Method | Path | Parameters | Response |
| --- | --- | --- | --- |
| `GET` | `/api/v1/catalog.json` | none | combined catalog object with both ordinary packages and robot deployments |
| `GET` | `/api/v1/packages.json` | none | ordinary primitive/service/skill packages only |
| `GET` | `/api/v1/robots.json` | none | robot deployment entries only |
| `GET` | `/api/v1/search.json` | none | plain combined catalog array, intended for client-side search/filter indexes |
| `GET` | `/api/v1/package/<package-name>.json` | `package-name`: exact catalog `name`, URL-encoded | one ordinary package or robot deployment object; missing entries return GitHub Pages `404` |

Package object fields:

| Field | Type | Meaning |
| --- | --- | --- |
| `name` | string | canonical package name, e.g. `robonix.service.mapping` |
| `version` | string | package version from `package_manifest.yaml` |
| `description` | string | short package description |
| `license` | string | SPDX license identifier; legacy entries without one are exposed as `NOASSERTION` |
| `tags` | string[] | UI/search tags |
| `maintainers` | string[] | maintainers in `Name <email@domain>` format |
| `repo` | string | GitHub repository URL |
| `repo_name` | string | repository name without owner |
| `default_branch` | string | package repository default branch used for indexing |
| `kind` | string | `primitive`, `service`, `skill`, or `robot` inferred from catalog name |
| `catalog_type` | string | `package` for ordinary packages, `robot` for whole-robot deployments |
| `manifest` | string | source manifest path, usually `package_manifest.yaml` or `robonix_manifest.yaml` |
| `capabilities` | string[] | declared Robonix contract IDs |
| `deploy_dependencies` | object[] | robot deployment dependencies parsed from `robonix_manifest.yaml` |
| `deployment_status` | string | robot dependency health: `ok` or `warning` |
| `deployment_warnings` | object[] | robot manifest or dependency warnings with section, name, source, and reason |
| `readme_url` | string | GitHub README URL for the indexed branch |
| `preview_image_url` | string | optional robot preview discovered at `assets/robot.jpg`; empty when absent |

### JavaScript

```js
const base = 'https://packages.robonix.ai/api/v1';
const res = await fetch(`${base}/packages.json`);
const catalog = await res.json();
const mapping = catalog.packages.find(p => p.name === 'robonix.service.mapping');

const detail = await fetch(`${base}/package/${encodeURIComponent(mapping.name)}.json`)
  .then(r => r.json());
```

### curl

```bash
curl -s https://packages.robonix.ai/api/v1/packages.json
curl -s https://packages.robonix.ai/api/v1/package/robonix.service.mapping.json
```

### Python

```python
import urllib.request, json

base = 'https://packages.robonix.ai/api/v1'
catalog = json.load(urllib.request.urlopen(f'{base}/packages.json'))
mapping = next(p for p in catalog['packages'] if p['name'] == 'robonix.service.mapping')
detail = json.load(urllib.request.urlopen(f"{base}/package/{mapping['name']}.json"))
```

### API schema

`GET /api/v1/packages.json` returns:

```json
{
  "api_version": "1",
  "generated_at": "2026-07-06T12:00:00+00:00",
  "packages": [
    {
      "name": "robonix.service.mapping",
      "version": "0.4.0",
      "description": "Map and SLAM service package for Robonix.",
      "license": "MulanPSL-2.0",
      "tags": ["service", "mapping", "slam"],
      "maintainers": ["wheatfox <wheatfox17@icloud.com>"],
      "repo": "https://github.com/syswonder/service-map-rbnx",
      "repo_name": "service-map-rbnx",
      "default_branch": "main",
      "kind": "service",
      "capabilities": ["robonix/service/map/save_map"],
      "readme_url": "https://github.com/syswonder/service-map-rbnx/blob/main/README.md"
    }
  ]
}
```

`GET /api/v1/robots.json` returns robot deployments under a top-level `robots[]` field.

`GET /api/v1/search.json` returns the combined catalog entries as a plain array.

`GET /api/v1/package/<package-name>.json` returns one package object.

## Package Manifest

Each package repository must provide a root-level `package_manifest.yaml`.
The catalog builder reads these fields from that file:

- `package.name`
- `package.version`
- `package.description`
- `package.license`
- `package.tags`
- `package.maintainers`
- `capabilities[].name`

The `package.name` in `package_manifest.yaml` must exactly match the name in
`catalog.yaml`.

Each `capabilities[].name` is a contract id under `robonix/<kind>/`, and the
kind is the package's own: a package provides primitives, services or skills,
never a mix. The contract is either one of Robonix's (`capabilities/` in
`syswonder/robonix`) or defined in the package by a TOML under `capabilities/`
with a `[contract]` table, whose IDL sits in `capabilities/lib/`. A new or
changed entry that breaks these rules fails CI; an entry already in the
catalog is listed with a warning.

## Robot Deployment Manifest

Robot deployment repositories are indexed from root-level `robonix_manifest.yaml`.
They do not need a separate `package_manifest.yaml`. The catalog metadata lives
under a top-level `catalog:` block with the same fields as package metadata:

```yaml
manifestVersion: 1
name: robonix-ranger-mini-deploy
catalog:
  name: robonix.robot.agilex.ranger_mini_v3
  version: 0.1.0
  description: Robonix deploy manifest for the AgileX Ranger Mini v3 robot.
  license: Apache-2.0
  tags: [robot, deploy, agilex, ranger_mini_v3]
  maintainers:
    - wheatfox <wheatfox17@icloud.com>
```

A robot deployment repository may add `assets/robot.jpg`. When present,
the catalog exposes its raw URL as `preview_image_url`, then generates
380 px and 720 px WebP previews for responsive robot list cards. Repositories
without the file keep
the same metadata and layout without an image placeholder.

The builder also parses `primitive:`, `service:`, and `skill:` entries from
`robonix_manifest.yaml` into `deploy_dependencies[]`, linking dependencies
back to cataloged ordinary packages when their repository is known. Each
dependency includes `resolution` (`catalog`, `robonix_source`,
`robonix_deploy`, `robot_repository`, or `unresolved`) and
`resolution_warning`. A source is
portable when it resolves to a cataloged repository, uses the exact
`${ROBONIX_SOURCE_PATH}/...` source-tree root, uses the exact
`${ROBONIX_DEPLOY_DIR}/...` boot-deployment root, or stays inside the
robot repository through a relative path. Unresolved sources produce CI
warnings and a report without failing catalog generation. For local paths,
the builder also checks the corresponding GitHub repository tree: the
resolved directory must exist and contain the selected package manifest
(`package_manifest.yaml` by default, with legacy `robonix_manifest.yaml`
accepted when no override is selected). `${ROBONIX_DEPLOY_DIR}` resolves
to the robot repository root, while `${ROBONIX_SOURCE_PATH}` resolves to
the default branch of `https://github.com/syswonder/robonix`.

## Building the site

The website is build output. It is generated into `public/`, published
straight to GitHub Pages from the workflow artifact, and never committed —
the deployed site is the only copy. This README is hand-maintained; nothing
in the repository is auto-generated.

```bash
python -m pip install pyyaml markdown bleach pygments pymdown-extensions pillow
GITHUB_TOKEN=$(gh auth token) python scripts/build_catalog.py
python -m http.server -d public 8899
```

The builder reads every package repository through the GitHub API, so a token
is required to stay clear of anonymous rate limits. CI runs the same command
on pushes to `main`, on catalog pull requests, and on a daily schedule.
