import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "build_catalog", ROOT / "scripts" / "build_catalog.py"
)
BUILD_CATALOG = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BUILD_CATALOG)


class _PublishedResponse:
    """Minimal stand-in for the urlopen context manager in tests."""

    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def read(self) -> bytes:
        return self._body


def published(payload: dict) -> _PublishedResponse:
    return _PublishedResponse(payload)


class MarkdownRenderingTests(unittest.TestCase):
    def test_markdown_inside_centered_html_div_is_rendered(self):
        rendered = BUILD_CATALOG.render_markdown(
            '<div align="center">\n\n'
            "# Example Service\n\n"
            "[中文文档](README-CN.md)\n\n"
            "![Python](docs/python-badge.svg)\n\n"
            "</div>",
            "https://github.com/syswonder/example-service",
            "main",
        )

        self.assertIn('<div align="center">', rendered)
        self.assertIn("<h1>Example Service</h1>", rendered)
        self.assertIn(
            'href="https://github.com/syswonder/example-service/blob/main/README-CN.md"',
            rendered,
        )
        self.assertIn(
            'src="https://raw.githubusercontent.com/syswonder/example-service/main/docs/python-badge.svg"',
            rendered,
        )
        self.assertNotIn("# Example Service", rendered)

    def test_html_like_text_inside_fenced_code_is_not_modified(self):
        rendered = BUILD_CATALOG.render_markdown(
            "```html\n<div>\n```",
            "https://github.com/syswonder/example-service",
            "main",
        )

        self.assertNotIn('markdown="1"', rendered)

    def test_yaml_fence_is_highlighted_and_relative_image_is_preserved(self):
        rendered = BUILD_CATALOG.render_markdown(
            "1. Configure the deployment:\n\n"
            "   ```yaml\n"
            "   manifestVersion: 1\n"
            "   ```\n\n"
            "![Robot](assets/robot.jpg)",
            "https://github.com/syswonder/example-robot",
            "main",
        )

        self.assertIn('class="highlight"', rendered)
        self.assertIn('class="nt"', rendered)
        self.assertIn("raw.githubusercontent.com/syswonder/example-robot/main/assets/robot.jpg", rendered)
        self.assertIn("<img", rendered)

    def test_optional_readme_is_empty_only_for_a_real_404(self):
        with mock.patch.object(BUILD_CATALOG, "github_optional_json", return_value=None):
            value = BUILD_CATALOG.load_remote_text(
                "syswonder", "missing", "README.md", "main", required=False
            )

        self.assertEqual(value, "")

    def test_optional_readme_does_not_swallow_transport_failure(self):
        with mock.patch.object(
            BUILD_CATALOG,
            "github_optional_json",
            side_effect=RuntimeError("transport failed"),
        ):
            with self.assertRaisesRegex(RuntimeError, "transport failed"):
                BUILD_CATALOG.load_remote_text(
                    "syswonder", "example", "README.md", "main", required=False
                )


class RobotListingTests(unittest.TestCase):
    def test_robot_preview_renders_in_the_listing_row(self):
        package = {
            "name": "robonix.robot.example",
            "version": "0.1.0",
            "kind": "robot",
            "description": "Example robot",
            "license": "Apache-2.0",
            "repo": "https://github.com/syswonder/example-robot",
            "repo_name": "example-robot",
            "default_branch": "main",
            "catalog_type": "robot",
            "tags": ["robot"],
            "maintainers": ["Example <example@example.com>"],
            "capabilities": [],
            "deploy_dependencies": [],
            "preview_image_url": "https://raw.githubusercontent.com/syswonder/example-robot/main/assets/robot.jpg",
        }

        with tempfile.TemporaryDirectory() as tmp:
            public = Path(tmp)
            BUILD_CATALOG.render_listing_page(
                public,
                "2026-07-14T00:00:00+00:00",
                [package],
                page="robots",
                title="Robot deployments",
                lede="Complete robot deployments.",
                noun="robot deployments",
            )
            rendered = (public / "robots" / "index.html").read_text()

        self.assertIn('class="entry-shot"', rendered)
        self.assertIn('width="380" height="285"', rendered)
        self.assertIn('loading="lazy" decoding="async"', rendered)
        # The card carries its kind's colour, so a grid of them reads as
        # distinct families rather than one uniform wall.
        self.assertIn('class="entry-card k-robot"', rendered)
        self.assertIn('class="kind-tile"', rendered)

    def test_robot_preview_is_resized_to_responsive_webp_assets(self):
        image_buffer = io.BytesIO()
        Image.new("RGB", (1600, 900), (39, 54, 137)).save(image_buffer, "JPEG", quality=95)
        package = {
            "name": "robonix.robot.example",
            "preview_image_url": "https://raw.githubusercontent.com/syswonder/example/main/assets/robot.jpg",
        }

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            BUILD_CATALOG, "download_bytes", return_value=image_buffer.getvalue()
        ):
            public = Path(tmp)
            BUILD_CATALOG.prepare_preview_images(public, [package])
            small = public / package["_preview_image_380"]
            large = public / package["_preview_image_720"]

            self.assertTrue(small.is_file())
            self.assertTrue(large.is_file())
            with Image.open(small) as preview:
                self.assertEqual(preview.format, "WEBP")
                self.assertEqual(preview.size, (380, 285))
            with Image.open(large) as preview:
                self.assertEqual(preview.size, (720, 540))

    def test_listing_page_has_filters_and_only_local_assets(self):
        package = {
            "name": "robonix.robot.example",
            "version": "0.1.0",
            "kind": "robot",
            "description": "Example robot",
            "license": "Apache-2.0",
            "repo": "https://github.com/syswonder/example-robot",
            "repo_name": "example-robot",
            "default_branch": "main",
            "catalog_type": "robot",
            "tags": ["robot"],
            "maintainers": ["Example <example@example.com>"],
            "capabilities": [],
            "deploy_dependencies": [],
        }

        with tempfile.TemporaryDirectory() as tmp:
            public = Path(tmp)
            BUILD_CATALOG.render_listing_page(
                public,
                "2026-07-14T00:00:00+00:00",
                [package],
                page="robots",
                title="Robot deployments",
                lede="Complete robot deployments.",
                noun="robot deployments",
            )
            rendered = (public / "robots" / "index.html").read_text()

        self.assertIn('data-listing="robot deployments"', rendered)
        self.assertIn("data-tag-filter=", rendered)
        self.assertIn("data-clear", rendered)
        self.assertIn("data-sort", rendered)
        self.assertIn('data-empty', rendered)
        # Every asset is served from this host: the catalog is read mostly from
        # mainland China, where third-party CDNs are unreliable.
        self.assertNotIn("//cdn.", rendered)
        self.assertNotIn("fonts.googleapis.com", rendered)
        self.assertIn('href="../assets/vendor/bootstrap/bootstrap.min.css"', rendered)
        self.assertIn('href="../assets/site.css"', rendered)
        self.assertIn('src="../assets/site.js"', rendered)
        self.assertIn(
            '<link rel="icon" type="image/svg+xml" href="../assets/robonix-mark.svg">',
            rendered,
        )

    def test_homepage_leads_with_skills_and_keeps_search_in_the_navbar(self):
        package = {
            "name": "robonix.service.example",
            "version": "0.1.0",
            "kind": "service",
            "description": "Example service",
            "license": "Apache-2.0",
            "repo": "https://github.com/syswonder/example-service",
            "repo_name": "example-service",
            "default_branch": "main",
            "catalog_type": "package",
            "manifest": "package_manifest.yaml",
            "tags": ["service"],
            "maintainers": ["Example <example@example.com>"],
            "capabilities": ["robonix/service/example"],
            "deploy_dependencies": [],
        }

        with tempfile.TemporaryDirectory() as tmp:
            public = Path(tmp)
            BUILD_CATALOG.render_site(public, "2026-07-14T00:00:00+00:00", [package])
            rendered = (public / "index.html").read_text()
            api_viewer = (public / "api" / "view" / "index.html").read_text()
            copied_mark = (public / "assets" / "robonix-mark.svg").read_bytes()

        # Skills are the applications of the platform, so the homepage shows
        # real skill entries first rather than a link to a filtered list.
        self.assertIn("<h2 class=\"mb-1\">Skills</h2>", rendered)
        self.assertLess(rendered.index(">Skills<"), rendered.index("The substrate"))
        self.assertIn('href="packages/?kind=skill"', rendered)
        self.assertIn('href="packages/?kind=primitive"', rendered)
        self.assertIn('href="packages/?kind=service"', rendered)
        self.assertIn('href="submit/"', rendered)
        self.assertIn('href="api/view/"', rendered)

        # Search belongs in the navbar, which is the same on every page. The
        # homepage body must not grow a second search box of its own.
        self.assertIn('id="omnisearch"', rendered)
        body = rendered[rendered.index("<main>"):]
        self.assertNotIn('type="search"', body)

        self.assertIn("fetch(file)", api_viewer)
        self.assertIn("../v1/${resource}.json", api_viewer)
        self.assertIn('class="site-footer', rendered)
        self.assertEqual(copied_mark, (ROOT / "assets" / "robonix-mark.svg").read_bytes())
        self.assertIn(
            '<link rel="icon" type="image/svg+xml" href="assets/robonix-mark.svg">',
            rendered,
        )
        self.assertIn(
            '<link rel="icon" type="image/svg+xml" href="../../assets/robonix-mark.svg">',
            api_viewer,
        )

    def test_catalog_workflow_supports_manual_refresh(self):
        workflow = (ROOT / ".github/workflows/catalog.yml").read_text()
        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("python scripts/build_catalog.py", workflow)
        self.assertGreaterEqual(workflow.count("- assets/**"), 2)

    def test_theme_toggle_follows_the_system_and_is_on_every_page_type(self):
        package = {
            "name": "robonix.service.example",
            "version": "0.1.0",
            "kind": "service",
            "description": "Example service",
            "license": "Apache-2.0",
            "repo": "https://github.com/syswonder/example-service",
            "repo_name": "example-service",
            "default_branch": "main",
            "catalog_type": "package",
            "manifest": "package_manifest.yaml",
            "tags": ["service"],
            "maintainers": ["Example <example@example.com>"],
            "capabilities": ["robonix/service/example"],
            "deploy_dependencies": [],
            "_readme_markdown": "",
        }

        with tempfile.TemporaryDirectory() as tmp:
            public = Path(tmp)
            BUILD_CATALOG.render_site(public, "2026-07-27T00:00:00+00:00", [package])
            BUILD_CATALOG.render_package_pages(
                public, "2026-07-27T00:00:00+00:00", [package]
            )
            pages = [
                public / "index.html",
                public / "packages" / "index.html",
                public / "api" / "view" / "index.html",
                public / "packages" / "robonix.service.example" / "index.html",
            ]

            for page in pages:
                rendered = page.read_text()
                self.assertIn("robonix-catalog-theme", rendered)
                self.assertIn("data-theme-toggle", rendered)
                # Resolved inline before first paint, or a dark-mode reader
                # gets a white flash while the stylesheet loads.
                self.assertIn("prefers-color-scheme: dark", rendered)
                self.assertIn("dataset.bsTheme", rendered)
                self.assertLess(
                    rendered.index("dataset.bsTheme"), rendered.index("<body>")
                )
                # No stored choice means "follow the OS", so nothing is written
                # to storage until the reader actually picks a side.
                self.assertNotIn("localStorage.setItem", rendered)

    def test_api_writer_keeps_extensionless_resource_and_json_preview(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "api" / "v1" / "catalog"
            BUILD_CATALOG.write_api(target, {"api_version": "1"})

            self.assertTrue(target.is_file())
            self.assertTrue(target.with_name("catalog.json").is_file())


class CatalogMetadataTests(unittest.TestCase):
    def test_missing_catalog_mapping_is_a_hard_failure(self):
        with self.assertRaises(SystemExit):
            BUILD_CATALOG.validate_catalog_metadata(
                "robonix.robot.example", None, "robonix.robot.example"
            )

    def test_license_is_preserved(self):
        metadata = {
            "name": "robonix.robot.example",
            "version": "0.1.0",
            "description": "Example robot",
            "license": "Apache-2.0",
            "tags": ["robot"],
            "maintainers": ["Example <example@example.com>"],
        }

        _, _, license_name, _, _, _ = BUILD_CATALOG.validate_catalog_metadata(
            "robonix.robot.example", metadata, "robonix.robot.example"
        )

        self.assertEqual(license_name, "Apache-2.0")

    def test_missing_license_for_new_entry_is_a_hard_failure(self):
        metadata = {
            "name": "robonix.robot.new",
            "version": "0.1.0",
            "description": "New robot",
            "tags": ["robot"],
            "maintainers": ["Example <example@example.com>"],
        }

        with self.assertRaises(SystemExit):
            BUILD_CATALOG.validate_catalog_metadata(
                "robonix.robot.new", metadata, "robonix.robot.new"
            )

    def test_preexisting_wheeltec_license_exception_uses_noassertion(self):
        metadata = {
            "name": "robonix.robot.wheeltec.r550",
            "version": "0.1.0",
            "description": "Legacy robot",
            "tags": ["robot"],
            "maintainers": ["Example <example@example.com>"],
        }

        _, _, license_name, _, _, _ = BUILD_CATALOG.validate_catalog_metadata(
            "robonix.robot.wheeltec.r550", metadata, "robonix.robot.wheeltec.r550"
        )

        self.assertEqual(license_name, "NOASSERTION")

    def test_empty_tags_is_a_hard_failure(self):
        metadata = {
            "name": "robonix.service.example",
            "version": "0.1.0",
            "description": "Example service",
            "license": "Apache-2.0",
            "tags": [],
            "maintainers": ["Example <example@example.com>"],
        }

        with self.assertRaises(SystemExit):
            BUILD_CATALOG.validate_catalog_metadata(
                "robonix.service.example", metadata, "robonix.service.example"
            )

    def test_name_mismatch_is_strict_by_default(self):
        metadata = {
            "name": "robonix.service.wrong",
            "version": "0.1.0",
            "description": "Example service",
            "license": "Apache-2.0",
            "tags": ["service"],
            "maintainers": ["Example <example@example.com>"],
        }

        with self.assertRaises(SystemExit):
            BUILD_CATALOG.validate_catalog_metadata(
                "robonix.service.example", metadata, "robonix.service.example"
            )

    def test_unchanged_base_entry_name_mismatch_becomes_warning(self):
        entry = {
            "name": "robonix.service.example",
            "repo": "https://github.com/syswonder/service-example-rbnx",
        }
        metadata = {
            "name": "robonix.service.wrong",
            "version": "0.1.0",
            "description": "Example service",
            "license": "Apache-2.0",
            "tags": ["service"],
            "maintainers": ["Example <example@example.com>"],
        }
        with tempfile.TemporaryDirectory() as tmp:
            catalog = Path(tmp) / "catalog.yaml"
            catalog.write_text(
                "packages:\n"
                "  - name: robonix.service.example\n"
                "    repo: https://github.com/syswonder/service-example-rbnx\n"
            )
            baseline_entry = {**entry, "_catalog_type": "package"}
            baseline_keys = {BUILD_CATALOG.catalog_entry_key(baseline_entry)}
            with mock.patch.object(
                BUILD_CATALOG,
                "load_remote_manifest",
                return_value=("main", {"package": metadata, "capabilities": []}, ""),
            ):
                packages = BUILD_CATALOG.collect(catalog, baseline_keys)
                catalog.write_text(
                    "packages:\n"
                    "  - name: robonix.service.example\n"
                    "    repo: https://github.com/syswonder/service-example-renamed\n"
                )
                with self.assertRaises(SystemExit):
                    BUILD_CATALOG.collect(catalog, baseline_keys)

        self.assertEqual(packages[0]["catalog_status"], "warning")
        self.assertEqual(
            packages[0]["catalog_warnings"][0]["type"],
            "manifest_name_mismatch",
        )

    def test_catalog_warning_reaches_ci_api_listing_and_detail(self):
        warning = {
            "type": "manifest_name_mismatch",
            "manifest_name": "robonix.service.wrong",
            "expected_name": "robonix.service.example",
            "reason": (
                "manifest catalog name is 'robonix.service.wrong', expected "
                "'robonix.service.example'"
            ),
        }
        package = {
            "name": "robonix.service.example",
            "version": "0.1.0",
            "kind": "service",
            "description": "Example service",
            "license": "Apache-2.0",
            "repo": "https://github.com/syswonder/service-example-rbnx",
            "repo_name": "service-example-rbnx",
            "default_branch": "main",
            "catalog_type": "package",
            "catalog_status": "warning",
            "catalog_warnings": [warning],
            "manifest": "package_manifest.yaml",
            "tags": ["service"],
            "maintainers": ["Example <example@example.com>"],
            "capabilities": [],
            "deploy_dependencies": [],
            "deployment_warnings": [],
            "preview_image_url": "",
            "_readme_markdown": "",
        }

        with tempfile.TemporaryDirectory() as tmp:
            public = Path(tmp) / "public"
            summary = Path(tmp) / "summary.md"
            api = Path(tmp) / "package.json"
            stderr = io.StringIO()
            BUILD_CATALOG.render_listing_page(
                public,
                "2026-08-01T00:00:00+00:00",
                [package],
                page="packages",
                title="Packages",
                lede="Reusable packages.",
                noun="packages",
            )
            BUILD_CATALOG.render_package_pages(
                public, "2026-08-01T00:00:00+00:00", [package]
            )
            BUILD_CATALOG.write_json(api, package)
            with mock.patch.dict(
                BUILD_CATALOG.os.environ,
                {
                    "GITHUB_ACTIONS": "true",
                    "GITHUB_STEP_SUMMARY": str(summary),
                },
            ), mock.patch.object(BUILD_CATALOG.sys, "stderr", stderr):
                count = BUILD_CATALOG.report_catalog_warnings([package])

            listing = (public / "packages" / "index.html").read_text()
            detail = (
                public / "packages" / "robonix.service.example" / "index.html"
            ).read_text()
            api_data = api.read_text()
            report = summary.read_text()

        self.assertEqual(count, 1)
        self.assertIn("::warning title=Catalog name mismatch", stderr.getvalue())
        self.assertIn("## Catalog metadata warning report", report)
        # Issues are shown on the entry's page, not in the listing.
        self.assertNotIn("found while indexing", listing)
        self.assertIn("manifest catalog name is", detail)
        self.assertIn("found while indexing this entry", detail)
        self.assertIn('"catalog_status": "warning"', api_data)
        self.assertIn('"manifest_name_mismatch"', api_data)


class DeploymentDependencyWarningTests(unittest.TestCase):
    def test_load_remote_submodule_resolves_canonical_pinned_repository(self):
        payload = {
            # GitHub documents this legacy "file" value for submodules.
            "type": "file",
            "submodule_git_url": "https://github.com/syswonder/service-map-rbnx.git",
            "sha": "48adfc3ac138443e5ba9347639aeba2e11650ea5",
        }

        with mock.patch.object(
            BUILD_CATALOG,
            "github_optional_json",
            return_value=payload,
        ) as fetch:
            result = BUILD_CATALOG.load_remote_submodule(
                "https://github.com/syswonder/robot-example",
                "third_party/service map",
                "feature/catalog fix",
            )

        self.assertEqual(
            result,
            (
                "https://github.com/syswonder/service-map-rbnx",
                "48adfc3ac138443e5ba9347639aeba2e11650ea5",
            ),
        )
        fetch.assert_called_once_with(
            "https://api.github.com/repos/syswonder/robot-example/contents/"
            "third_party/service%20map?ref=feature%2Fcatalog+fix"
        )

    def test_local_dependency_path_accepts_pinned_git_submodule(self):
        robot_repo = "https://github.com/syswonder/robot-example"
        submodule_repo = "https://github.com/syswonder/service-map-rbnx"
        submodule_sha = "48adfc3ac138443e5ba9347639aeba2e11650ea5"
        submodule_path = "third_party/service-map-rbnx"
        packages = [
            {
                "name": "robonix.robot.example",
                "repo": robot_repo,
                "default_branch": "main",
                "catalog_type": "robot",
                "deployment_warnings": [],
                "deploy_dependencies": [
                    {
                        "section": "service",
                        "name": "mapping",
                        "path": f"${{ROBONIX_DEPLOY_DIR}}/{submodule_path}",
                        "manifest": "",
                        "resolution": "robonix_deploy",
                        "resolution_warning": "",
                    },
                    {
                        "section": "service",
                        "name": "mapping_alias",
                        "path": f"${{ROBONIX_DEPLOY_DIR}}/{submodule_path}",
                        "manifest": "",
                        "resolution": "robonix_deploy",
                        "resolution_warning": "",
                    },
                ],
            }
        ]

        def tree_for(repo_url, branch):
            if (repo_url, branch) == (robot_repo, "main"):
                return {submodule_path: "commit"}
            if (repo_url, branch) == (submodule_repo, submodule_sha):
                return {"package_manifest.yaml": "blob"}
            self.fail(f"unexpected tree request: {repo_url}@{branch}")

        with mock.patch.object(
            BUILD_CATALOG,
            "load_remote_repository_tree",
            side_effect=tree_for,
        ) as load_tree, mock.patch.object(
            BUILD_CATALOG,
            "load_remote_submodule",
            return_value=(submodule_repo, submodule_sha),
        ) as load_submodule:
            BUILD_CATALOG.validate_deploy_dependency_paths(packages)

        self.assertEqual(packages[0]["deployment_status"], "ok")
        self.assertEqual(packages[0]["deployment_warnings"], [])
        self.assertEqual(packages[0]["deploy_dependencies"][0]["resolution_warning"], "")
        self.assertEqual(packages[0]["deploy_dependencies"][1]["resolution_warning"], "")
        load_submodule.assert_called_once_with(robot_repo, submodule_path, "main")
        self.assertEqual(load_tree.call_count, 2)

    def test_local_dependency_path_rejects_unresolved_gitlink(self):
        submodule_path = "third_party/unknown"
        packages = [
            {
                "name": "robonix.robot.example",
                "repo": "https://github.com/syswonder/robot-example",
                "default_branch": "main",
                "catalog_type": "robot",
                "deployment_warnings": [],
                "deploy_dependencies": [
                    {
                        "section": "service",
                        "name": "unknown",
                        "path": submodule_path,
                        "manifest": "",
                        "resolution": "robot_repository",
                        "resolution_warning": "",
                    }
                ],
            }
        ]

        with mock.patch.object(
            BUILD_CATALOG,
            "load_remote_repository_tree",
            return_value={submodule_path: "commit"},
        ), mock.patch.object(
            BUILD_CATALOG,
            "load_remote_submodule",
            return_value=None,
        ):
            BUILD_CATALOG.validate_deploy_dependency_paths(packages)

        warning = packages[0]["deploy_dependencies"][0]["resolution_warning"]
        self.assertIn("is a Git submodule", warning)
        self.assertIn("repository URL and pinned commit", warning)
        self.assertEqual(packages[0]["deployment_status"], "warning")

    def test_local_dependency_git_submodule_requires_root_manifest(self):
        robot_repo = "https://github.com/syswonder/robot-example"
        submodule_repo = "https://github.com/syswonder/service-example-rbnx"
        submodule_sha = "0123456789abcdef0123456789abcdef01234567"
        submodule_path = "third_party/service-example-rbnx"
        packages = [
            {
                "name": "robonix.robot.example",
                "repo": robot_repo,
                "default_branch": "main",
                "catalog_type": "robot",
                "deployment_warnings": [],
                "deploy_dependencies": [
                    {
                        "section": "service",
                        "name": "example",
                        "path": submodule_path,
                        "manifest": "",
                        "resolution": "robot_repository",
                        "resolution_warning": "",
                    }
                ],
            }
        ]

        def tree_for(repo_url, branch):
            if (repo_url, branch) == (robot_repo, "main"):
                return {submodule_path: "commit"}
            if (repo_url, branch) == (submodule_repo, submodule_sha):
                return {"README.md": "blob"}
            self.fail(f"unexpected tree request: {repo_url}@{branch}")

        with mock.patch.object(
            BUILD_CATALOG,
            "load_remote_repository_tree",
            side_effect=tree_for,
        ), mock.patch.object(
            BUILD_CATALOG,
            "load_remote_submodule",
            return_value=(submodule_repo, submodule_sha),
        ):
            BUILD_CATALOG.validate_deploy_dependency_paths(packages)

        warning = packages[0]["deploy_dependencies"][0]["resolution_warning"]
        self.assertIn(f"points to {submodule_repo}@{submodule_sha}", warning)
        self.assertIn("at the submodule root", warning)
        self.assertEqual(packages[0]["deployment_status"], "warning")

    def test_local_dependency_paths_exist_and_select_package_manifests(self):
        robot_repo = "https://github.com/syswonder/robot-example"
        packages = [
            {
                "name": "robonix.robot.example",
                "repo": robot_repo,
                "default_branch": "main",
                "catalog_type": "robot",
                "deployment_warnings": [],
                "deploy_dependencies": [
                    {
                        "section": "primitive",
                        "name": "valid_local",
                        "path": "./primitives/valid",
                        "manifest": "",
                        "resolution": "robot_repository",
                        "resolution_warning": "",
                    },
                    {
                        "section": "primitive",
                        "name": "missing_deploy",
                        "path": "${ROBONIX_DEPLOY_DIR}/robot_example/primitives/missing",
                        "manifest": "",
                        "resolution": "robonix_deploy",
                        "resolution_warning": "",
                    },
                    {
                        "section": "service",
                        "name": "not_a_package",
                        "path": "./services/plain_directory",
                        "manifest": "",
                        "resolution": "robot_repository",
                        "resolution_warning": "",
                    },
                    {
                        "section": "service",
                        "name": "target_manifest",
                        "path": "${ROBONIX_DEPLOY_DIR}/services/target",
                        "manifest": "package_manifest.jetson.yaml",
                        "resolution": "robonix_deploy",
                        "resolution_warning": "",
                    },
                    {
                        "section": "service",
                        "name": "builtin",
                        "path": "${ROBONIX_SOURCE_PATH}/services/speech",
                        "manifest": "",
                        "resolution": "robonix_source",
                        "resolution_warning": "",
                    },
                ],
            }
        ]
        robot_tree = {
            "primitives/valid": "tree",
            "primitives/valid/package_manifest.yaml": "blob",
            "services/plain_directory": "tree",
            "services/plain_directory/README.md": "blob",
            "services/target": "tree",
            "services/target/package_manifest.jetson.yaml": "blob",
        }
        source_tree = {
            "services/speech": "tree",
            "services/speech/package_manifest.yaml": "blob",
        }

        def tree_for(repo_url, branch):
            self.assertIn(branch, {"main", "dev"})
            return source_tree if repo_url == BUILD_CATALOG.ROBONIX_SOURCE_REPO else robot_tree

        with mock.patch.object(
            BUILD_CATALOG, "load_remote_default_branch", return_value="dev"
        ) as default_branch, mock.patch.object(
            BUILD_CATALOG, "load_remote_repository_tree", side_effect=tree_for
        ) as load_tree:
            BUILD_CATALOG.validate_deploy_dependency_paths(packages)

        dependencies = {dep["name"]: dep for dep in packages[0]["deploy_dependencies"]}
        self.assertEqual(dependencies["valid_local"]["resolution_warning"], "")
        self.assertEqual(dependencies["target_manifest"]["resolution_warning"], "")
        self.assertEqual(dependencies["builtin"]["resolution_warning"], "")
        self.assertIn(
            "does not exist",
            dependencies["missing_deploy"]["resolution_warning"],
        )
        self.assertIn(
            "${ROBONIX_DEPLOY_DIR} (the robot repository root)",
            dependencies["missing_deploy"]["resolution_warning"],
        )
        self.assertIn(
            "not a usable Robonix package",
            dependencies["not_a_package"]["resolution_warning"],
        )
        self.assertEqual(packages[0]["deployment_status"], "warning")
        self.assertEqual(len(packages[0]["deployment_warnings"]), 2)
        default_branch.assert_called_once_with(BUILD_CATALOG.ROBONIX_SOURCE_REPO)
        self.assertEqual(load_tree.call_count, 2)

    def test_local_dependency_path_accepts_legacy_package_manifest(self):
        packages = [
            {
                "name": "robonix.robot.example",
                "repo": "https://github.com/syswonder/robot-example",
                "default_branch": "main",
                "catalog_type": "robot",
                "deployment_warnings": [],
                "deploy_dependencies": [
                    {
                        "section": "skill",
                        "name": "legacy",
                        "path": "skills/legacy",
                        "manifest": "",
                        "resolution": "robot_repository",
                        "resolution_warning": "",
                    }
                ],
            }
        ]
        tree = {
            "skills/legacy": "tree",
            "skills/legacy/robonix_manifest.yaml": "blob",
        }

        with mock.patch.object(
            BUILD_CATALOG, "load_remote_repository_tree", return_value=tree
        ):
            BUILD_CATALOG.validate_deploy_dependency_paths(packages)

        self.assertEqual(packages[0]["deployment_status"], "ok")
        self.assertEqual(packages[0]["deployment_warnings"], [])

    def test_invalid_robot_manifest_uses_cached_metadata_and_becomes_warning(self):
        cached = {
            "name": "robonix.robot.example",
            "version": "0.1.0",
            "description": "Example robot",
            "license": "Apache-2.0",
            "tags": ["robot"],
            "maintainers": ["Example <example@example.com>"],
        }
        entry = {
            "name": "robonix.robot.example",
            "repo": "https://github.com/syswonder/example-robot",
            "_catalog_type": "robot",
            "manifest": "robonix_manifest.yaml",
        }

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            BUILD_CATALOG, "read_catalog", return_value=[entry]
        ), mock.patch.object(
            BUILD_CATALOG,
            "load_remote_manifest",
            side_effect=BUILD_CATALOG.InvalidRemoteManifestError(
                "Invalid robonix_manifest.yaml at line 7, column 5: expected ':'.",
                branch="main",
                readme="# Example",
            ),
        ), mock.patch.object(
            BUILD_CATALOG,
            "github_optional_json",
            return_value=None,
        ), mock.patch.object(
            BUILD_CATALOG.urllib.request, "urlopen", return_value=published(cached)
        ) as urlopen:
            packages = BUILD_CATALOG.collect(Path("catalog.yaml"))

        # Nothing generated is committed, so the last good metadata for a robot
        # whose manifest just broke can only come from the published site.
        self.assertIn(
            "packages.robonix.ai/api/v1/package/robonix.robot.example.json",
            urlopen.call_args.args[0],
        )

        robot = packages[0]
        self.assertEqual(robot["version"], "0.1.0")
        self.assertEqual(robot["deploy_dependencies"], [])
        self.assertEqual(robot["deployment_status"], "warning")
        self.assertEqual(robot["deployment_warnings"][0]["section"], "manifest")
        self.assertIn("last-known-good", robot["deployment_warnings"][0]["reason"])

    def test_dependency_resolution_is_strict_and_catalog_aware(self):
        packages = [
            {
                "name": "robonix.primitive.example",
                "repo": "https://github.com/syswonder/primitive-example-rbnx",
                "catalog_type": "package",
            },
            {
                "name": "robonix.robot.example",
                "repo": "https://github.com/syswonder/robot-example",
                "catalog_type": "robot",
                "deploy_dependencies": [
                    {
                        "section": "primitive",
                        "name": "cataloged",
                        "repo": "https://github.com/syswonder/primitive-example-rbnx.git",
                        "path": "",
                    },
                    {
                        "section": "service",
                        "name": "builtin",
                        "repo": "",
                        "path": "${ROBONIX_SOURCE_PATH}/services/speech",
                    },
                    {
                        "section": "service",
                        "name": "wrong_builtin_variable",
                        "repo": "",
                        "path": "${ROBONIX_SOURCE}/services/speech",
                    },
                    {
                        "section": "skill",
                        "name": "bundled_dot",
                        "repo": "",
                        "path": "./skills/greet",
                    },
                    {
                        "section": "primitive",
                        "name": "bundled_plain",
                        "repo": "",
                        "path": "primitives/camera",
                    },
                    {
                        "section": "primitive",
                        "name": "absolute",
                        "repo": "",
                        "path": "/home/robot/primitives/description",
                    },
                    {
                        "section": "primitive",
                        "name": "deploy_dir",
                        "repo": "",
                        "path": "${ROBONIX_DEPLOY_DIR}/packages/chassis",
                    },
                    {
                        "section": "primitive",
                        "name": "escaping",
                        "repo": "",
                        "path": "../shared/chassis",
                    },
                    {
                        "section": "service",
                        "name": "unindexed",
                        "repo": "https://github.com/syswonder/service-unindexed-rbnx",
                        "path": "",
                    },
                    {
                        "section": "service",
                        "name": "missing",
                        "repo": "",
                        "path": "",
                    },
                ],
            },
        ]

        BUILD_CATALOG.annotate_deploy_dependencies(packages)

        robot = packages[1]
        dependencies = {dep["name"]: dep for dep in robot["deploy_dependencies"]}
        self.assertEqual(dependencies["cataloged"]["resolution"], "catalog")
        self.assertEqual(
            dependencies["cataloged"]["package_name"], "robonix.primitive.example"
        )
        self.assertEqual(dependencies["builtin"]["resolution"], "robonix_source")
        self.assertEqual(dependencies["bundled_dot"]["resolution"], "robot_repository")
        self.assertEqual(dependencies["bundled_plain"]["resolution"], "robot_repository")
        self.assertEqual(dependencies["wrong_builtin_variable"]["resolution"], "unresolved")
        self.assertIn(
            "unsupported environment root ${ROBONIX_SOURCE}",
            dependencies["wrong_builtin_variable"]["resolution_warning"],
        )
        self.assertIn(
            "host-specific absolute path",
            dependencies["absolute"]["resolution_warning"],
        )
        self.assertEqual(dependencies["deploy_dir"]["resolution"], "robonix_deploy")
        self.assertEqual(dependencies["deploy_dir"]["resolution_warning"], "")
        self.assertIn("escapes the robot repository", dependencies["escaping"]["resolution_warning"])
        self.assertIn("not indexed by catalog.yaml", dependencies["unindexed"]["resolution_warning"])
        self.assertIn("No url or path", dependencies["missing"]["resolution_warning"])
        self.assertEqual(robot["deployment_status"], "warning")
        self.assertEqual(len(robot["deployment_warnings"]), 5)

    def test_robot_list_and_detail_render_warning_reasons(self):
        robot = {
            "name": "robonix.robot.example",
            "version": "0.1.0",
            "kind": "robot",
            "description": "Example robot",
            "license": "Apache-2.0",
            "repo": "https://github.com/syswonder/example-robot",
            "repo_name": "example-robot",
            "default_branch": "main",
            "catalog_type": "robot",
            "manifest": "robonix_manifest.yaml",
            "tags": ["robot"],
            "maintainers": ["Example <example@example.com>"],
            "capabilities": [],
            "deploy_dependencies": [
                {
                    "section": "primitive",
                    "name": "robot_description",
                    "repo": "",
                    "path": "/home/robot/primitives/robot_description",
                    "branch": "",
                    "manifest": "",
                }
            ],
            "preview_image_url": "",
            "_readme_markdown": "",
        }
        BUILD_CATALOG.annotate_deploy_dependencies([robot])

        with tempfile.TemporaryDirectory() as tmp:
            public = Path(tmp)
            BUILD_CATALOG.render_listing_page(
                public,
                "2026-07-27T00:00:00+00:00",
                [robot],
                page="robots",
                title="Robot deployments",
                lede="Complete robot deployments.",
                noun="robot deployments",
            )
            BUILD_CATALOG.render_package_pages(
                public, "2026-07-27T00:00:00+00:00", [robot]
            )
            listing = (public / "robots" / "index.html").read_text()
            detail = (
                public / "robots" / "robonix.robot.example" / "index.html"
            ).read_text()

        self.assertNotIn("found while indexing", listing)
        self.assertIn('class="warn-box mt-3" role="note"', detail)
        self.assertIn("robot_description", detail)
        self.assertIn("host-specific absolute path", detail)
        # The unresolved dependency is flagged again where it is listed.
        self.assertIn('class="dep-reason"', detail)

    def test_ci_warning_annotations_and_summary_do_not_fail(self):
        packages = [
            {
                "name": "robonix.robot.example",
                "deployment_warnings": [
                    {
                        "section": "primitive",
                        "name": "robot_description",
                        "source": "/home/robot/description",
                        "reason": "Uses a host-specific absolute path.",
                    }
                ],
            }
        ]

        with tempfile.TemporaryDirectory() as tmp:
            summary = Path(tmp) / "summary.md"
            stderr = io.StringIO()
            with mock.patch.dict(
                BUILD_CATALOG.os.environ,
                {
                    "GITHUB_ACTIONS": "true",
                    "GITHUB_STEP_SUMMARY": str(summary),
                },
            ), mock.patch.object(BUILD_CATALOG.sys, "stderr", stderr):
                count = BUILD_CATALOG.report_deployment_warnings(packages)

            report = summary.read_text()

        self.assertEqual(count, 1)
        self.assertIn("::warning title=Unresolved deployment dependency", stderr.getvalue())
        self.assertIn("robonix.robot.example", stderr.getvalue())
        self.assertIn("## Robot deployment warning report", report)
        self.assertIn("Found **1 robot deployment warning**", report)
        self.assertIn("/home/robot/description", report)


if __name__ == "__main__":
    unittest.main()


class ProvidedGroupsTest(unittest.TestCase):
    def test_skills_are_counted_per_contract_namespace(self):
        caps = [f"robonix/skill/unitree_go2_{a}/{op}" for a in ("hello", "dance")
                for op in ("driver", "execute", "status", "cancel")]
        caps.append("robonix/lifecycle/driver")
        groups = BUILD_CATALOG.group_capabilities("skill", caps)
        self.assertEqual([g["namespace"] for g in groups],
                         ["robonix/skill/unitree_go2_hello", "robonix/skill/unitree_go2_dance"])
        self.assertEqual(BUILD_CATALOG.provided_count({"kind": "skill", "capabilities": caps}), 2)

    def test_one_namespace_is_one_skill(self):
        caps = ["robonix/skill/explore/driver", "robonix/skill/explore/explore",
                "robonix/skill/explore/explore/status", "robonix/skill/explore/explore/cancel"]
        self.assertEqual(BUILD_CATALOG.provided_count({"kind": "skill", "capabilities": caps}), 1)

    def test_only_contracts_of_the_package_kind_count(self):
        """The shared lifecycle driver, or a misspelled prefix, is no
        primitive and no capability."""
        for caps in (["robonix/lifecycle/driver"],
                     ["robinix/primitive/lidar/lidar", "robinix/primitive/lidar/driver"]):
            package = {"kind": "primitive", "capabilities": caps}
            self.assertEqual(BUILD_CATALOG.provided_count(package), 0)
            self.assertEqual(BUILD_CATALOG.interface_count(package), 0)
        package = {"kind": "skill", "capabilities": [
            "robonix/skill/explore/explore", "robonix/skill/explore/status",
            "robonix/lifecycle/driver"]}
        self.assertEqual(BUILD_CATALOG.interface_count(package), 2)



class LayoutProblemsTest(unittest.TestCase):
    def test_service_in_a_skill_package_is_reported(self):
        caps = ["robonix/skill/unitree_go2_hello/execute",
                "robonix/service/unitree_go2_sport_runtime/execute",
                "robonix/lifecycle/driver"]
        problems = BUILD_CATALOG.layout_problems("skill", caps, None, "package_manifest.yaml")
        self.assertEqual([p["type"] for p in problems], ["mixed_kinds"])

    def test_several_of_one_kind_are_fine(self):
        for kind in ("skill", "service", "primitive"):
            caps = [f"robonix/{kind}/a/execute", f"robonix/{kind}/b/execute"]
            self.assertEqual(BUILD_CATALOG.layout_problems(kind, caps, None, "package_manifest.yaml"), [])

    def test_catalog_manifest_must_match_the_package_manifest(self):
        caps = ["robonix/skill/a/execute"]
        root = caps + ["robonix/skill/legacy/execute"]
        problems = BUILD_CATALOG.layout_problems("skill", caps, root, "package_manifest.catalog.yaml")
        self.assertEqual([p["type"] for p in problems], ["manifest_mismatch"])
        root = caps + ["robonix/service/runtime/execute"]
        problems = BUILD_CATALOG.layout_problems("skill", caps, root, "package_manifest.catalog.yaml")
        self.assertEqual([p["type"] for p in problems], ["mixed_kinds", "manifest_mismatch"])
        self.assertEqual(BUILD_CATALOG.layout_problems("skill", caps, list(caps), "package_manifest.catalog.yaml"), [])


class ContractProblemsTest(unittest.TestCase):
    ROBONIX = ({"robonix/primitive/lidar/lidar"}, {"lidar/srv/Snap.srv"}, ("common_interfaces/",))
    REPO = "https://github.com/example/primitive-example-rbnx"

    def problems(self, caps, files):
        tree = {path: "blob" for path in files}
        with mock.patch.object(BUILD_CATALOG, "robonix_contracts", return_value=self.ROBONIX), \
                mock.patch.object(BUILD_CATALOG, "load_remote_repository_tree", return_value=tree), \
                mock.patch.object(BUILD_CATALOG, "raw_text", side_effect=lambda _r, _b, p: files[p]):
            return BUILD_CATALOG.contract_problems(self.REPO, "main", {"capabilities": caps},
                                                   "package_manifest.yaml")

    def test_misspelled_prefix_is_reported(self):
        problems = self.problems([{"name": "robinix/primitive/lidar/lidar"}], {})
        self.assertEqual([p["type"] for p in problems], ["capability_name"])

    def test_robonix_and_package_contracts_pass(self):
        files = {
            "capabilities/snap.v1.toml": '[contract]\nid = "robonix/primitive/lidar/snap"\nidl = "x/srv/Snap.srv"\n',
            "capabilities/lib/x/srv/Snap.srv": "",
            "capabilities/scan.v1.toml": '[contract]\nid = "robonix/primitive/lidar/scan"\n'
                                         'idl = "common_interfaces/sensor_msgs/msg/LaserScan.msg"\n',
        }
        caps = [{"name": "robonix/primitive/lidar/lidar"}, {"name": "robonix/primitive/lidar/snap"},
                {"name": "robonix/primitive/lidar/scan", "path": "capabilities/scan.v1.toml"}]
        self.assertEqual(self.problems(caps, files), [])

    def test_undefined_contract_and_missing_idl_are_reported(self):
        files = {
            "capabilities/a.v1.toml": 'id = "robonix/skill/x/a"\n[mcp]\nidl = "lib/srv/A.srv"\n',
            "capabilities/b.v1.toml": '[contract]\nid = "robonix/skill/x/b"\nidl = "x/srv/B.srv"\n',
        }
        caps = [{"name": "robonix/skill/x/a", "path": "capabilities/a.v1.toml"},
                {"name": "robonix/skill/x/b"}, {"name": "robonix/skill/x/c"},
                {"name": "robonix/skill/x/d", "path": "capabilities/d.v1.toml"}]
        reasons = [p["reason"] for p in self.problems(caps, files)]
        self.assertEqual(len(reasons), 4)
        self.assertIn("no [contract] table", reasons[0])
        self.assertIn("IDL x/srv/B.srv", reasons[1])
        self.assertIn("no contract defines robonix/skill/x/c", reasons[2])
        self.assertIn("does not exist", reasons[3])
