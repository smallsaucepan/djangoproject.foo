import io
import os
import re
from pathlib import Path
from unittest import skipUnless

from django.conf import settings
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from PIL import Image
from pixelmatch.contrib.PIL import pixelmatch
from playwright.sync_api import sync_playwright

from djangoproject.test_runner import selected_browsers
from djangoproject.urls.www import sitemaps
from docs.models import DocumentRelease, Release

from .settings.dev import HOST_SCHEME, PARENT_HOST

screenshots_dir = (
    Path(__file__).parent.joinpath(os.environ["SCREENSHOT_DIR"])
    if "SCREENSHOT_DIR" in os.environ
    else Path(__file__).parent.joinpath("tests", "screenshots")
)

class ReleaseMixin:
    @classmethod
    def setUpTestData(cls):
        r2, _ = Release.objects.get_or_create(version="2.0")
        DocumentRelease.objects.get_or_create(
            is_default=True,
            defaults={"lang": settings.DEFAULT_LANGUAGE_CODE, "release": r2},
        )


class GenerateScreenshotMixin:
    def generateScreenshot(self, location, page, variant, *, threshold=0.1):
        # Derive a friendly test name from the URL
        tokens = [f"{HOST_SCHEME}://", f".{PARENT_HOST}"]
        pattern = "|".join(map(re.escape, tokens))
        (*_, subdomain, path) = re.split(pattern, location)
        screen_name = f"{subdomain} {re.sub(r'/', ' ', path).strip()}"
        screen_name = re.sub(r"\s", "_", screen_name)

        baseline_path = self._screenshot_path(screen_name, variant, "baseline.png")
        current_path = self._screenshot_path(screen_name, variant, "current.png")
        diff_path = self._screenshot_path(screen_name, variant, "diff.png")

        baseline_path.parent.mkdir(parents=True, exist_ok=True)

        # Clean up first to avoid signalling any ambiguous test results.
        if current_path.exists():
            os.remove(current_path)
        if diff_path.exists():
            os.remove(diff_path)

        page.goto(self.live_server_url + path)
        page.wait_for_timeout(500)
        screenshot_bytes = page.screenshot(full_page=True)

        if os.environ["SCREENSHOT_MODE"] == "baseline":
            baseline = Image.open(io.BytesIO(screenshot_bytes))
            baseline.save(baseline_path)
            return
        elif not baseline_path.exists():
            print(
                f"Skipped {'/'.join([screen_name, *variant])}, baseline screenshot does not exist"
            )
            return

        current = Image.open(io.BytesIO(screenshot_bytes))
        current.save(current_path)

        baseline = Image.open(baseline_path)
        if baseline.size != current.size:
            # Resize both to the largest of both dimensions to enable
            # comparison.
            max_width = max(baseline.size[0], current.size[0])
            max_height = max(baseline.size[1], current.size[1])
            if max_width != baseline.size[0] or max_height != baseline.size[1]:
                canvas = Image.new("RGBA", (max_width, max_height), (0, 0, 0, 255))
                canvas.paste(baseline, (0, 0))
                baseline = canvas
            if max_width != current.size[0] or max_height != current.size[1]:
                canvas = Image.new("RGBA", (max_width, max_height), (0, 0, 0, 255))
                canvas.paste(current, (0, 0))
                current = canvas

            # self.fail(f"Screenshot {screen_name!r} dimensions differ from expected")

        diff = Image.new("RGBA", baseline.size)
        diff_ratio = pixelmatch(current, baseline, diff)
        if diff_ratio > 0:
            diff.save(diff_path)
            return f"Differences in {'/'.join([screen_name, *variant])}"

        # if diff_ratio > threshold:
        #     self.fail(f"Screenshot {screen_name!r} differs by {diff_ratio:.2%} (threshold {threshold:.2%})")

    def _screenshot_path(self, screen_name, variant, name):
        return Path().joinpath(
            screenshots_dir, screen_name, *variant, name
        )


@skipUnless(
    os.environ.get("SCREENSHOT_MODE"),
    "Set SCREENSHOT_MODE=baseline or compare to generate before and after screenshots.",
)
class ScreenshotTests(ReleaseMixin, GenerateScreenshotMixin, StaticLiveServerTestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = "true"
        super().setUpClass()
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch()
        cls.mac_user_agent = "Mozilla/5.0 (Macintosh) AppleWebKit"
        cls.windows_user_agent = "Mozilla/5.0 (Windows NT 10.0)"
        cls.mobile_linux_user_agent = "Mozilla/5.0 (Linux; Android 10; Mobile)"

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        super().setUp()
        self.setUpTestData()

    def test_screenshots(self):
        msgs = []

        diff_list_path = Path.joinpath(screenshots_dir, "diffs.txt")

        # Clean up first to avoid signalling any ambiguous test results.
        if diff_list_path.exists():
            os.remove(diff_list_path)

        for sitemap in sitemaps.values():
            for location in [url.get("location") for url in sitemap().get_urls()]:
                themes = ["dark", "light"]
                # https://www.browserstack.com/guide/common-screen-resolutions
                # 414, 768, 1366
                widths = [414, 768, 1366]

                page = self.browser.new_page(user_agent=self.mac_user_agent)
                self.browser.browser_type.name
                for theme in themes:
                    page.context.add_cookies(
                        [
                            {
                                "name": "theme",
                                "value": theme,
                                "domain": "localhost",
                                "path": "/",
                            }
                        ]
                    )
                    for width in widths:
                        page.set_viewport_size({"width": width, "height": 800})
                        variant = [self.browser.browser_type.name, theme, str(width)]

                        msg = self.generateScreenshot(location, page, variant)
                        if msg:
                            msgs.append(msg)

        if len(msgs) > 0:
            with open(diff_list_path, "w") as f:
                f.write("\n".join(msgs))
