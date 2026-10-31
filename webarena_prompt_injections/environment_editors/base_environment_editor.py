# Copyright (c) Meta Platforms, Inc. and affiliates.
import os
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

# Debug screenshots / HTML dumps go here (wasp/logs), not /tmp, so they persist and are trackable.
LOG_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "logs"))
os.makedirs(LOG_DIR, exist_ok=True)


class WebArenaEditorException(Exception):
    """Exception raised for an error when editing the page.

    Attributes:
        message (str): The error message.
    """

    def __init__(self, message):
        """Initialize the WebArenaEditorException with a message.

        Args:
            message (str): The error message.
        """
        self.message = message
        super().__init__(self.message)


class BaseWebArenaEditor:
    """A base class to automate actions in a WebArena environment through a browser using Playwright."""

    def __init__(
        self,
        headless: bool = True,
    ) -> None:
        """Initialize the WebArenaEditor.

        Args:
            headless (bool, optional): Whether to run the browser in headless mode. Defaults to True.
        """
        self.headless = headless

    def __enter__(self):
        """Enter the runtime context related to the WebArenaEditor.

        Starts Playwright, launches the browser, and creates a new page.

        Returns:
            WebArenaEditor: The initialized editor instance.
        """
        self.playwright = sync_playwright().start()
        # Headless-on-a-GPU-less-server screenshot stability: software render (--disable-gpu) + /tmp shm
        # (--disable-dev-shm-usage) are the real fixes; --force-gpu-mem-available-mb is a harmless extra.
        # (Playwright Python has no headless='shell'; that is a Node-only option.)
        self.browser = self.playwright.chromium.launch(
            headless=self.headless,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--force-gpu-mem-available-mb=4096",
                "--hide-scrollbars",
            ],
        )

        self.context = self.browser.new_context()
        # On a host with no internet, external subresources (gravatar, fonts, CDNs) never return, so a
        # page's `load`/networkidle never fires -> goto times out. Block anything not on the local site.
        self.context.route("**/*", self._route_filter)

        self.page = self.context.new_page()
        return self

    def _allowed_hosts(self):
        """Hosts whose requests are let through: localhost plus whatever site domain the subclass holds."""
        hosts = {"localhost", "127.0.0.1", "::1"}
        for attr in ("gitlab_domain", "reddit_domain", "base_url", "domain"):
            value = getattr(self, attr, None)
            host = urlparse(value).hostname if value else None
            if host:
                hosts.add(host)
        return hosts

    def _route_filter(self, route):
        """Let local-site requests through; abort external ones so page load never hangs on them.
        Only http(s) is filtered -- data:/blob:/ws:/about: have no external host and must pass untouched,
        or the page's own JS (which uses them) breaks."""
        parsed = urlparse(route.request.url)
        if parsed.scheme not in ("http", "https"):
            route.continue_()
        elif (parsed.hostname or "") in self._allowed_hosts():
            route.continue_()
        else:
            route.abort()

    def _safe_screenshot(self, path):
        """Best-effort debug screenshot: wait for the page to finish loading and settle briefly first
        (headless capture fails on a not-yet-painted page), and never raise -- it is only a debug aid."""
        try:
            self.page.wait_for_load_state("load", timeout=5000)
        except Exception:
            pass
        try:
            self.page.wait_for_timeout(500)
        except Exception:
            pass
        try:
            self.page.screenshot(path=path)
        except Exception:
            pass

    def close(self):
        """Close the browser page, context, and the browser itself, as well as stop Playwright."""
        if hasattr(self, "page") and self.page:
            self.page.close()
        if hasattr(self, "context") and self.context:
            self.context.close()
        if hasattr(self, "browser") and self.browser:
            self.browser.close()
        if hasattr(self, "playwright") and self.playwright:
            self.playwright.stop()

    def __exit__(self, exc_type, exc_value, traceback):
        """Exit the runtime context, ensuring all resources are properly released."""
        self.close()

    def login(self, username: str, password: str) -> bool:
        raise NotImplementedError(
            "You must define the login function in the base classes"
        )

    def _simple_type_and_submit(
        self,
        url_of_action: str,
        selector_of_field_to_type_in: str,
        text_to_type: str,
        selector_of_submit_button: str,
    ):
        """Navigate to a page, fill in a field, and submit a form.

        Args:
            url_of_action (str): The URL of the page where the action will be performed.
            selector_of_field_to_type_in (str): CSS selector of the field to type text into.
            text_to_type (str): The text to be typed into the field.
            selector_of_submit_button (str): CSS selector of the button to submit the form.

        Raises:
            WebArenaEditorException: If the field to type into is not found.
        """
        self.page.goto(url_of_action, wait_until="load")

        try:
            self.page.wait_for_selector(selector_of_field_to_type_in, timeout=10000)
        except:
            self._safe_screenshot(f"{LOG_DIR}/debug_edit_submission.png")
            raise WebArenaEditorException(
                "[WebArenaEditor] Field to edit not found. Taking a screenshot for debugging."
            )

        self.page.fill(selector_of_field_to_type_in, text_to_type)
        self.page.click(selector_of_submit_button)
        self.page.wait_for_load_state("load")