import importlib.util
import io
import json
import tempfile
import unittest
import zlib
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
        self.assertIn('width="380" height="238"', rendered)
        self.assertIn('loading="lazy" decoding="async"', rendered)
        # The card carries its kind's colour, so a grid of them reads as
        # distinct families rather than one uniform wall.
        self.assertIn('class="entry-card k-robot"', rendered)
        self.assertIn('class="kind-tile"', rendered)

    def test_promo_images_are_resized_to_responsive_webp_assets(self):
        image_buffer = io.BytesIO()
        Image.new("RGB", (1600, 900), (39, 54, 137)).save(image_buffer, "JPEG", quality=95)
        urls = [
            "https://raw.githubusercontent.com/syswonder/example/main/assets/promo/01.jpg",
            "https://raw.githubusercontent.com/syswonder/example/main/assets/promo/02.jpg",
        ]
        package = {"name": "robonix.robot.example", "promo_image_urls": urls}

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            BUILD_CATALOG, "download_bytes", return_value=image_buffer.getvalue()
        ):
            public = Path(tmp)
            BUILD_CATALOG.prepare_promo_images(public, [package])
            small = public / package["_preview_image_380"]
            large = public / package["_preview_image_720"]

            self.assertTrue(small.is_file())
            self.assertTrue(large.is_file())
            with Image.open(small) as preview:
                self.assertEqual(preview.format, "WEBP")
                self.assertEqual(preview.size, (380, 238))
            with Image.open(large) as preview:
                self.assertEqual(preview.size, (720, 450))
            # Every promo image gets a gallery copy, aspect ratio kept.
            for url in urls:
                asset, width, height = package["_gallery_images"][url]
                self.assertEqual((width, height), (1280, 720))
                with Image.open(public / asset) as gallery:
                    self.assertEqual(gallery.size, (1280, 720))

    def test_promo_image_download_failure_only_warns(self):
        url = "https://raw.githubusercontent.com/syswonder/example/main/assets/promo/01.png"
        package = {"name": "robonix.skill.example", "promo_image_urls": [url]}
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            BUILD_CATALOG, "download_bytes",
            side_effect=BUILD_CATALOG.urllib.error.URLError("offline"),
        ), mock.patch.object(BUILD_CATALOG.sys, "stderr", stderr):
            BUILD_CATALOG.prepare_promo_images(Path(tmp), [package])

        self.assertIn("could not optimize promo image", stderr.getvalue())
        self.assertNotIn("_preview_image_380", package)
        # The gallery falls back to the raw image.
        self.assertIn(f'src="{url}"', BUILD_CATALOG.render_promo_gallery(package, "../../"))

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
        self.assertIn("<h2 class=\"mb-1\">Skill packages</h2>", rendered)
        self.assertLess(rendered.index(">Skill packages<"), rendered.index("The substrate"))
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
            ), mock.patch.object(
                BUILD_CATALOG, "load_remote_images",
                return_value={"promo": [], "icon": "", "problems": [], "data": {}},
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


class PackagePathTests(unittest.TestCase):
    REPO = "https://github.com/syswonder/robonix"
    ENTRY = {"name": "robonix.service.memory", "repo": REPO, "path": "services/memory"}
    META = {
        "name": "robonix.service.memory", "version": "0.1.0", "description": "Memory service",
        "license": "Apache-2.0", "tags": ["service"], "maintainers": ["Example <example@example.com>"],
    }

    def test_paths_are_package_directories(self):
        entries = BUILD_CATALOG.parse_catalog({"packages": [self.ENTRY]})
        self.assertEqual(BUILD_CATALOG.validate_catalog_entries(entries)[0]["path"], "services/memory")
        for path in (None, 1, "", "/services/memory", "services/memory/", ".", "..",
                     "services/../memory", "services/./memory", "services//memory",
                     "services\\memory", "C:/memory", " services/memory"):
            with self.subTest(path=path), self.assertRaises(SystemExit), \
                    mock.patch("sys.stderr", new=io.StringIO()):
                BUILD_CATALOG.validate_catalog_entries([{**self.ENTRY, "path": path}])
        robot = {**self.ENTRY, "name": "robonix.robot.example", "_catalog_type": "robot"}
        with self.assertRaises(SystemExit), mock.patch("sys.stderr", new=io.StringIO()):
            BUILD_CATALOG.validate_catalog_entries([robot])

    def test_repository_path_pairs_and_names_are_unique(self):
        sibling = {**self.ENTRY, "name": "robonix.service.speech", "path": "services/speech"}
        root = {"name": "robonix.service.root", "repo": self.REPO}
        self.assertEqual(BUILD_CATALOG.validate_catalog_entries([self.ENTRY, sibling, root]),
                         [self.ENTRY, sibling, root])
        for duplicate in ({**self.ENTRY, "name": "robonix.service.other"},
                          {**sibling, "name": self.ENTRY["name"]}):
            with self.subTest(duplicate=duplicate), self.assertRaises(SystemExit), \
                    mock.patch("sys.stderr", new=io.StringIO()):
                BUILD_CATALOG.validate_catalog_entries([self.ENTRY, duplicate])
        with self.assertRaises(SystemExit), mock.patch("sys.stderr", new=io.StringIO()):
            BUILD_CATALOG.validate_catalog_entries([root, {**root, "name": "robonix.service.other"}])

    def test_pr_baseline_includes_the_package_path(self):
        base = "packages:\n  - name: robonix.service.memory\n    repo: " + self.REPO + "\n    path: services/memory\n"
        with mock.patch.dict(BUILD_CATALOG.os.environ, {
            "GITHUB_EVENT_NAME": "pull_request", "GITHUB_REPOSITORY": "syswonder/robonix-package-catalog",
        }), mock.patch.object(BUILD_CATALOG, "read_catalog", return_value=[self.ENTRY]), \
                mock.patch.object(BUILD_CATALOG, "pull_request_base_ref", return_value="base-sha"), \
                mock.patch.object(BUILD_CATALOG, "load_remote_text", return_value=base) as read:
            keys = BUILD_CATALOG.catalog_baseline_keys(Path("catalog.yaml"))
        read.assert_called_once_with("syswonder", "robonix-package-catalog", "catalog.yaml", "base-sha", required=True)
        self.assertIn(BUILD_CATALOG.catalog_entry_key(self.ENTRY), keys)
        for path in ("services/speech", ""):
            self.assertNotIn(BUILD_CATALOG.catalog_entry_key({**self.ENTRY, "path": path}), keys)
        with mock.patch.object(BUILD_CATALOG, "load_remote_manifest",
                               return_value=("dev", {"package": {**self.META, "name": "wrong"}}, "")), \
                mock.patch.object(BUILD_CATALOG, "load_remote_images",
                                  return_value={"promo": [], "icon": "", "problems": [], "data": {}}), \
                self.assertRaises(SystemExit), mock.patch("sys.stderr", new=io.StringIO()):
            BUILD_CATALOG.collect_entry({**self.ENTRY, "path": "services/moved"}, keys)

    def test_manifest_readme_and_default_manifest_are_relative_to_package(self):
        for path in ("", "services/memory"):
            for manifest in ("package_manifest.yaml", "deploy/package_manifest.yaml"):
                with self.subTest(path=path, manifest=manifest):
                    entry = {"name": self.ENTRY["name"], "repo": self.REPO, "manifest": manifest}
                    if path:
                        entry["path"] = path
                    prefix = f"{path}/" if path else ""
                    readme = "[Config](config.spec)\n\n![Icon](assets/icon.svg)"
                    def read(_owner, _repo, file, _branch, *, required):
                        if file == prefix + "README.md":
                            self.assertFalse(required)
                            return readme
                        self.assertIn(file, (prefix + manifest, prefix + "package_manifest.yaml"))
                        return BUILD_CATALOG.yaml.safe_dump({"package": self.META, "capabilities": []})

                    with mock.patch.object(BUILD_CATALOG, "github_json", return_value={"default_branch": "dev"}), \
                            mock.patch.object(BUILD_CATALOG, "load_remote_text", side_effect=read) as load, \
                            mock.patch.object(BUILD_CATALOG, "load_remote_images",
                                              return_value={"promo": [], "icon": "", "problems": [], "data": {}}) as images:
                        package = BUILD_CATALOG.collect_entry(entry, set())
                    load.assert_any_call("syswonder", "robonix", prefix + manifest, "dev", required=True)
                    if manifest != "package_manifest.yaml":
                        load.assert_any_call("syswonder", "robonix", prefix + "package_manifest.yaml", "dev", required=False)
                    images.assert_called_once_with(self.REPO, "dev", path)
                    self.assertEqual(package.get("path", ""), path)
                    self.assertEqual("path" in package, bool(path))
                    self.assertEqual(package["readme_url"], f"{self.REPO}/blob/dev/{prefix}README.md")
                    with tempfile.TemporaryDirectory() as tmp:
                        public = Path(tmp)
                        BUILD_CATALOG.render_package_pages(public, "2026-10-24", [package])
                        detail = (public / "packages" / self.ENTRY["name"] / "index.html").read_text()
                        BUILD_CATALOG.write_json(public / "package.json", {k: v for k, v in package.items() if not k.startswith("_")})
                        data = json.loads((public / "package.json").read_text())
                    source = f"{self.REPO}/tree/dev/{path}" if path else self.REPO
                    self.assertIn(f'href="{source}"', detail)
                    self.assertIn(f'href="{self.REPO}/blob/dev/{prefix}README.md"', detail)
                    self.assertIn(f'href="{self.REPO}/blob/dev/{prefix}config.spec"', detail)
                    self.assertIn(f'src="https://raw.githubusercontent.com/syswonder/robonix/dev/{prefix}assets/icon.svg"', detail)
                    self.assertEqual(data.get("path", ""), path)

    def test_invalid_yaml_reads_the_package_readme(self):
        with mock.patch.object(BUILD_CATALOG, "github_json", return_value={"default_branch": "dev"}), \
                mock.patch.object(BUILD_CATALOG, "load_remote_text", side_effect=["package: [", "Package README"]) as read:
            with self.assertRaises(BUILD_CATALOG.InvalidRemoteManifestError) as error:
                BUILD_CATALOG.load_remote_manifest(self.REPO, "package_manifest.yaml", "services/memory")
        self.assertEqual(error.exception.readme, "Package README")
        read.assert_called_with("syswonder", "robonix", "services/memory/README.md", "dev", required=False)


class DeploymentDependencyWarningTests(unittest.TestCase):
    def test_source_dependencies_match_the_repository_and_package_path(self):
        source_repo = BUILD_CATALOG.ROBONIX_SOURCE_REPO
        packages = [
            {"name": "robonix.service.memory", "repo": source_repo, "path": "services/memory"},
            {"name": "robonix.service.speech", "repo": source_repo, "path": "services/speech"},
            {"name": "robonix.service.other", "repo": "https://github.com/example/other", "path": "services/other"},
        ]
        robot = {"name": "robonix.robot.example", "repo": "https://github.com/example/robot",
                 "default_branch": "main", "catalog_type": "robot",
                 "deploy_dependencies": BUILD_CATALOG.collect_deploy_dependencies("robonix.robot.example", {
                     "service": [
                         {"name": "memory", "path": "${ROBONIX_SOURCE_PATH}/services/memory"},
                         {"name": "speech", "path": "${ROBONIX_SOURCE_PATH}/services/speech"},
                         {"name": "other", "path": "${ROBONIX_SOURCE_PATH}/services/other"},
                         {"name": "url", "url": source_repo, "path": "${ROBONIX_SOURCE_PATH}/services/memory"},
                     ],
                 })}
        packages.append(robot)
        BUILD_CATALOG.annotate_deploy_dependencies(packages)
        memory, speech, other, url = robot["deploy_dependencies"]
        for dep in (memory, speech):
            self.assertEqual(dep["resolution"], "catalog")
            self.assertEqual(dep["package_name"], f"robonix.service.{dep['name']}")
            self.assertEqual(dep["package_url"], f"../robonix.service.{dep['name']}/")
        self.assertEqual(other["resolution"], "robonix_source")
        self.assertEqual(url["resolution"], "unresolved")
        for path in ("services/memory/", "services/./memory"):
            self.assertEqual(BUILD_CATALOG.classify_deploy_dependency(
                {"path": "${ROBONIX_SOURCE_PATH}/" + path},
                {(source_repo, "services/memory"): "robonix.service.memory"},
            ), ("catalog", "robonix.service.memory", ""))
        tree = {"services/memory": "tree", "services/memory/package_manifest.yaml": "blob"}
        with mock.patch.object(BUILD_CATALOG, "load_remote_default_branch", return_value="dev"), \
                mock.patch.object(BUILD_CATALOG, "load_remote_repository_tree", return_value=tree) as load:
            BUILD_CATALOG.validate_deploy_dependency_paths(packages)
        load.assert_called_once_with(source_repo, "dev")
        self.assertEqual(memory["resolution_warning"], "")
        self.assertIn("does not exist", speech["resolution_warning"])
        packages.insert(0, {"name": "robonix.service.root", "repo": source_repo})
        BUILD_CATALOG.annotate_deploy_dependencies(packages)
        self.assertEqual(url["package_name"], "robonix.service.root")

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
            "load_remote_images",
            return_value={"promo": ["https://raw.githubusercontent.com/syswonder/example-robot/main/assets/promo/01.png"],
                          "icon": "", "problems": [], "data": {}},
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

    def test_listing_row_carries_the_skill_count(self):
        """site.js sums data-provides to say "17 packages, 23 skills"."""
        caps = [f"robonix/skill/unitree_go2_{a}/execute" for a in ("hello", "dance")]
        row = BUILD_CATALOG.render_entry_row({
            "name": "robonix.skill.go2", "kind": "skill", "description": "", "license": "MIT",
            "version": "0.1.0", "repo": "https://github.com/example/go2", "tags": [],
            "maintainers": [], "capabilities": caps,
        })
        self.assertIn('data-provides="2"', row)

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

    def problems(self, caps, files, path=""):
        tree = {path: "blob" for path in files}
        with mock.patch.object(BUILD_CATALOG, "robonix_contracts", return_value=self.ROBONIX), \
                mock.patch.object(BUILD_CATALOG, "load_remote_repository_tree", return_value=tree), \
                mock.patch.object(BUILD_CATALOG, "raw_text", side_effect=lambda _r, _b, p: files[p]):
            return BUILD_CATALOG.contract_problems(self.REPO, "main", {"capabilities": caps},
                                                   "package_manifest.yaml", path)

    def test_contracts_and_idl_are_scoped_to_the_package_directory(self):
        files = {
            "services/memory/capabilities/a.toml": '[contract]\nid = "robonix/service/memory/a"\nidl = "srv/A.srv"\n',
            "services/memory/capabilities/lib/srv/A.srv": "",
            "services/speech/capabilities/b.toml": '[contract]\nid = "robonix/service/speech/b"\n',
            "capabilities/c.toml": '[contract]\nid = "robonix/service/root/c"\n',
        }
        caps = [{"name": "robonix/service/memory/a", "path": "capabilities/a.toml"}]
        self.assertEqual(self.problems(caps, files, "services/memory"), [])
        caps += [{"name": "robonix/service/speech/b"}, {"name": "robonix/service/root/c"}]
        self.assertEqual(len(self.problems(caps, files, "services/memory")), 2)
        del files["services/memory/capabilities/lib/srv/A.srv"]
        self.assertIn("IDL srv/A.srv", self.problems(caps[:1], files, "services/memory")[0]["reason"])

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


def encoded(size=(1600, 1000), mode="RGB", fmt="PNG", **params) -> bytes:
    out = io.BytesIO()
    Image.new(mode, size).save(out, fmt, **params)
    return out.getvalue()


SVG = b'<svg xmlns="http://www.w3.org/2000/svg"/>'


class ArtworkSpecTest(unittest.TestCase):
    """The artwork rules, worded as Robonix Hub's server/src/artwork.rs."""

    def rules(self, problems):
        return [f"{p['file']}: {p['rule']}" for p in problems]

    def test_a_layout_is_read_in_order_by_set(self):
        sets, problems = BUILD_CATALOG.promo_layout(
            ["assets/icon.png", "assets/promo/02.jpg", "assets/promo/01.png", "assets/promo/.gitkeep"])
        self.assertEqual((sets, problems), ({"": ["assets/promo/01.png", "assets/promo/02.jpg"]}, []))
        sets, _ = BUILD_CATALOG.promo_layout(["assets/promo/zh/01.png", "assets/promo/en/01.png"])
        self.assertEqual(sorted(sets), ["en", "zh"])
        self.assertEqual(BUILD_CATALOG.promo_layout(["assets/icon.svg"]), ({}, []))

    def test_a_layout_that_breaks_a_rule_names_the_file_and_the_rule(self):
        layout = BUILD_CATALOG.promo_layout
        self.assertEqual(self.rules(layout(
            ["assets/promo/1.png", "assets/promo/01.PNG", "assets/promo/02.webp", "assets/promo/11.png"])[1]), [
            "assets/promo/1.png: named 01.png to 10.png, or .jpg, in lower case",
            "assets/promo/01.PNG: named 01.png to 10.png, or .jpg, in lower case",
            "assets/promo/02.webp: named 01.png to 10.png, or .jpg, in lower case",
            "assets/promo/11.png: numbered 01 to 10",
        ])
        self.assertEqual(self.rules(layout(
            ["assets/promo/01.png", "assets/promo/01.jpg", "assets/promo/03.png"])[1]), [
            "assets/promo/01.jpg: another file is already number 01",
            "assets/promo/: 02 is missing: numbers run from 01 without gaps",
        ])
        sets, problems = layout(["assets/promo/01.png", "assets/promo/zh/01.png", "assets/promo/en/01.png",
                                 "assets/promo/en/02.png", "assets/promo/fr/01.png"])
        self.assertEqual(sets, {})
        self.assertEqual(self.rules(problems), [
            "assets/promo/fr/01.png: only zh/ and en/ may be inside promo/",
            "assets/promo/: images without a language and zh/ or en/ together; use one or the other",
            "assets/promo/: zh/ has 1 images and en/ has 2; both need the same numbers",
        ])

    def test_a_promo_image_is_exactly_1600_by_1000_opaque_srgb_and_named_for_its_format(self):
        check = BUILD_CATALOG.check_promo
        self.assertEqual(check("assets/promo/01.png", encoded()), [])
        self.assertEqual(check("assets/promo/01.jpg", encoded(fmt="JPEG")), [])
        rule = lambda path, data: [p["rule"] for p in check(path, data)]
        self.assertEqual(rule("assets/promo/en/02.png", encoded((1920, 1080))), ["1920×1080, expected 1600×1000"])
        self.assertEqual(rule("assets/promo/01.png", encoded(mode="RGBA")), ["has an alpha channel"])
        self.assertEqual(rule("assets/promo/01.jpg", encoded()), ["a Png file under a .jpg name"])
        self.assertEqual(rule("assets/promo/01.png", b"text"), ["not an image"])
        p3 = encoded(icc_profile=b"\0" * 40 + b"Display P3")
        self.assertEqual(rule("assets/promo/01.png", p3), ["colour profile is not sRGB"])
        self.assertEqual(rule("assets/promo/01.png", encoded(icc_profile=b"\0" * 40 + b"sRGB IEC61966-2.1")), [])
        with mock.patch.object(BUILD_CATALOG, "PROMO_MAX_BYTES", 10):
            self.assertIn("MB, at most 2 MB", rule("assets/promo/01.png", encoded())[0])

    def test_a_promo_image_is_8_bit_rgb_by_its_header(self):
        rule = lambda path, data: [p["rule"] for p in BUILD_CATALOG.check_promo(path, data)]
        sixteen = bytearray(encoded())
        sixteen[24] = 16  # IHDR bit depth; the rules read the header, not the pixels
        sixteen[29:33] = zlib.crc32(bytes(sixteen[12:29])).to_bytes(4, "big")
        self.assertEqual(rule("assets/promo/01.png", bytes(sixteen)), ["16-bit RGB pixels, expected 8-bit RGB"])
        # One colour: Pillow writes the palette at depth 1.
        self.assertEqual(rule("assets/promo/01.png", encoded(mode="P")), ["1-bit palette pixels, expected 8-bit RGB"])
        self.assertEqual(rule("assets/promo/01.png", encoded(mode="L")), ["8-bit grayscale pixels, expected 8-bit RGB"])
        self.assertEqual(rule("assets/promo/01.png", encoded(mode="I;16")), ["16-bit grayscale pixels, expected 8-bit RGB"])
        self.assertEqual(rule("assets/promo/01.jpg", encoded(mode="CMYK", fmt="JPEG")), ["8-bit CMYK pixels, expected 8-bit RGB"])
        self.assertEqual(rule("assets/promo/01.jpg", encoded(mode="L", fmt="JPEG")), ["8-bit grayscale pixels, expected 8-bit RGB"])
        self.assertEqual(BUILD_CATALOG.image_header(encoded(fmt="JPEG")), (1600, 1000, ""))

    def test_an_icon_is_an_svg_or_a_512_png(self):
        check = BUILD_CATALOG.check_icon
        self.assertEqual(check("assets/icon.svg", SVG), [])
        self.assertEqual(check("assets/icon.png", encoded((512, 512), "RGBA")), [])
        self.assertEqual(check("assets/icon.png", encoded((256, 256)))[0]["rule"], "256×256, expected 512×512")
        self.assertEqual(check("assets/icon.svg", b"<html>")[0]["rule"], "not an SVG document")

    def checked(self, files: dict[str, bytes]):
        return BUILD_CATALOG.check_artwork(list(files), files.get)

    def test_any_violation_drops_the_whole_set_or_the_icon(self):
        good = {"assets/icon.svg": SVG, "assets/promo/01.png": encoded(), "assets/promo/02.jpg": encoded(fmt="JPEG")}
        result = self.checked(good)
        self.assertEqual((result["promo"], result["icon"], result["problems"]),
                         ({"": ["assets/promo/01.png", "assets/promo/02.jpg"]}, "assets/icon.svg", []))
        result = self.checked({**good, "assets/promo/02.jpg": encoded((1920, 1080), fmt="JPEG"),
                               "assets/icon.png": encoded((512, 512))})
        self.assertEqual((result["promo"], result["icon"]), ({}, ""))
        self.assertEqual(self.rules(result["problems"]), [
            "assets/: both icon.svg and icon.png; keep one",
            "assets/promo/02.jpg: 1920×1080, expected 1600×1000",
        ])
        self.assertEqual(self.rules(self.checked({"assets/icon.jpg": b"x"})["problems"]),
                         ["assets/icon.jpg: the icon is icon.svg or icon.png"])

    def test_absent_artwork_is_skipped_and_robot_jpg_stands_in_only_without_promo(self):
        self.assertEqual(self.checked({"assets/README.md": b""})["problems"], [])
        result = self.checked({"assets/robot.jpg": encoded((4000, 3000), fmt="JPEG")})
        self.assertEqual(result["robot"], "assets/robot.jpg")
        self.assertEqual(self.rules(result["problems"]),
                         ["assets/robot.jpg: deprecated: use assets/promo/01.jpg at 1600×1000 instead"])
        result = self.checked({"assets/robot.jpg": b"x", "assets/promo/01.png": encoded((10, 10))})
        self.assertEqual((result["robot"], result["promo"]), ("", {}))


class PromoImagesTest(unittest.TestCase):
    REPO = "https://github.com/example/robot-example"
    RAW = "https://raw.githubusercontent.com/example/robot-example/main/"

    def listing(self, path, *names):
        return [{"name": n, "type": "dir" if "." not in n else "file",
                 "download_url": None if "." not in n else f"{self.RAW}{path}/{n}"} for n in names]

    def images(self, tree: dict, files: dict, path=""):
        def fetch(url, optional):
            self.assertTrue(optional)
            path = url.split("/contents/", 1)[1].split("?", 1)[0]
            return tree.get(path)

        with mock.patch.object(BUILD_CATALOG, "github_fetch", side_effect=fetch), \
                mock.patch.object(BUILD_CATALOG, "download_bytes", side_effect=lambda u: files[u[len(self.RAW):]]):
            return BUILD_CATALOG.load_remote_images(self.REPO, "main", path)

    def test_artwork_is_read_under_the_package_directory(self):
        path = "services/memory"
        tree = {f"{path}/assets": self.listing(f"{path}/assets", "promo", "icon.svg"),
                f"{path}/assets/promo": self.listing(f"{path}/assets/promo", "01.png")}
        files = {f"{path}/assets/icon.svg": SVG, f"{path}/assets/promo/01.png": encoded()}
        got = self.images(tree, files, path)
        self.assertEqual(got["icon"], f"{self.RAW}{path}/assets/icon.svg")
        self.assertEqual(got["promo"], [f"{self.RAW}{path}/assets/promo/01.png"])
        self.assertEqual(got["problems"], [])

    def test_the_site_shows_the_english_set_and_keeps_the_bytes(self):
        tree = {"assets": self.listing("assets", "promo", "icon.svg", "robot.jpg"),
                "assets/promo": self.listing("assets/promo", "zh", "en"),
                "assets/promo/zh": self.listing("assets/promo/zh", "01.png"),
                "assets/promo/en": self.listing("assets/promo/en", "01.png")}
        files = {"assets/icon.svg": SVG, "assets/promo/zh/01.png": encoded(), "assets/promo/en/01.png": encoded()}
        got = self.images(tree, files)
        self.assertEqual(got["promo"], [self.RAW + "assets/promo/en/01.png"])
        self.assertEqual(got["icon"], self.RAW + "assets/icon.svg")
        self.assertEqual(got["problems"], [])
        self.assertIn(self.RAW + "assets/promo/en/01.png", got["data"])

    def test_the_deprecated_robot_photo_stands_in_without_promo(self):
        got = self.images({"assets": self.listing("assets", "robot.jpg")}, {"assets/robot.jpg": encoded(fmt="JPEG")})
        self.assertEqual(got["promo"], [self.RAW + "assets/robot.jpg"])
        self.assertEqual(self.images({}, {}), {"promo": [], "icon": "", "problems": [], "data": {}})

    def collect(self, baseline: bool, images: dict) -> list[dict]:
        entry = {"name": "robonix.robot.example", "repo": self.REPO,
                 "_catalog_type": "robot", "manifest": "robonix_manifest.yaml"}
        manifest = {"catalog": {
            "name": "robonix.robot.example", "version": "0.1.0", "description": "Example robot",
            "license": "Apache-2.0", "tags": ["robot"], "maintainers": ["Example <example@example.com>"],
        }}
        baseline_keys = {BUILD_CATALOG.catalog_entry_key(entry)} if baseline else set()
        with mock.patch.object(BUILD_CATALOG, "read_catalog", return_value=[entry]), \
                mock.patch.object(BUILD_CATALOG, "load_remote_manifest", return_value=("main", manifest, "")), \
                mock.patch.object(BUILD_CATALOG, "load_remote_images", return_value=images):
            return BUILD_CATALOG.collect(Path("catalog.yaml"), baseline_keys)

    def test_a_new_entry_breaking_the_spec_fails_and_a_listed_one_only_warns(self):
        bad = {"promo": [], "icon": "", "data": {},
               "problems": [{"file": "assets/promo/01.png", "rule": "1920×1080, expected 1600×1000"}]}
        with self.assertRaises(SystemExit), mock.patch("sys.stderr", new=io.StringIO()) as err:
            self.collect(baseline=False, images=bad)
        self.assertIn("assets/promo/01.png: 1920×1080, expected 1600×1000", err.getvalue())
        robot = self.collect(baseline=True, images=bad)[0]
        self.assertEqual(robot["catalog_status"], "ok")
        self.assertEqual(robot["promo_image_urls"], [])
        with mock.patch("sys.stderr", new=io.StringIO()) as err:
            self.assertEqual(BUILD_CATALOG.report_artwork_problems([robot]), 1)
        self.assertIn("robonix.robot.example: assets/promo/01.png: 1920×1080", err.getvalue())

    def test_a_robot_needs_no_promo_image(self):
        none = {"promo": [], "icon": "", "problems": [], "data": {}}
        robot = self.collect(baseline=False, images=none)[0]
        self.assertEqual((robot["catalog_status"], robot["promo_image_urls"]), ("ok", []))
        deprecated = {**none, "problems": [{"file": "assets/robot.jpg", "rule": BUILD_CATALOG.DEPRECATED_ROBOT_PHOTO}]}
        self.assertEqual(self.collect(baseline=False, images=deprecated)[0]["catalog_status"], "ok")

    def test_card_shows_the_first_promo_image_only_when_there_is_one(self):
        package = {
            "name": "robonix.skill.example", "kind": "skill", "description": "", "license": "MIT",
            "version": "0.1.0", "repo": "https://github.com/example/skill-example", "tags": [],
            "maintainers": [], "capabilities": [], "promo_image_urls": [], "preview_image_url": "",
        }
        self.assertNotIn("entry-shot", BUILD_CATALOG.render_entry_row(package))

        urls = [self.RAW + "assets/promo/01.png", self.RAW + "assets/promo/02.png"]
        package.update(promo_image_urls=urls, preview_image_url=urls[0],
                       _preview_image_380="assets/previews/robonix.skill.example-380.webp",
                       _preview_image_720="assets/previews/robonix.skill.example-720.webp",
                       _gallery_images={urls[0]: ("assets/previews/robonix.skill.example-1-1280.webp", 1280, 720)})
        row = BUILD_CATALOG.render_entry_row(package)
        self.assertIn('<div class="entry-shot"><img src="../assets/previews/robonix.skill.example-380.webp"', row)

        gallery = BUILD_CATALOG.render_promo_gallery(package, "../../")
        self.assertEqual(gallery.count("<li>"), 2)
        self.assertIn(f'<a href="{urls[0]}"><img src="../../assets/previews/robonix.skill.example-1-1280.webp"', gallery)
        self.assertIn('width="1280" height="720"', gallery)
        self.assertIn(f'<a href="{urls[1]}"><img src="{urls[1]}"', gallery)


class CapabilityTransportTests(unittest.TestCase):
    def test_reads_one_or_several_transports(self):
        manifest = {"capabilities": [
            {"name": "a/b/c", "transport": "mcp"},
            {"name": "a/b/d", "transport": ["grpc", "ros2"]},
            {"name": "a/b/e"},
        ]}
        self.assertEqual(BUILD_CATALOG.capability_transports(manifest),
                         {"a/b/c": ["mcp"], "a/b/d": ["grpc", "ros2"]})
        problems = BUILD_CATALOG.transport_problems(manifest)
        self.assertEqual([p["type"] for p in problems], ["missing_transport"])
        self.assertIn("a/b/e", problems[0]["reason"])

    def test_rejects_unknown_or_repeated_transports(self):
        for bad in ("http", ["grpc", "grpc"], []):
            manifest = {"capabilities": [{"name": "a/b/c", "transport": bad}]}
            self.assertEqual([p["type"] for p in BUILD_CATALOG.transport_problems(manifest)],
                             ["invalid_transport"], bad)

    def test_page_lists_each_capabilitys_transports(self):
        package = {"transports": {"a/b/c": ["grpc", "ros2"]}}
        self.assertEqual(BUILD_CATALOG.capability_item(package, "a/b/c"),
                         '<li>a/b/c <span class="cap-transport">gRPC</span> <span class="cap-transport">ROS 2</span></li>')
        self.assertEqual(BUILD_CATALOG.capability_item(package, "a/b/d"), "<li>a/b/d</li>")
