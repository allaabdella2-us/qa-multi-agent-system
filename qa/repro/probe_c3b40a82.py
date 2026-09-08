"""Throwaway probe #2: does the accessible name survive typing?

SURFACE's claim: "Its only name source disappears the moment the user types."
Probe #1 already showed the browser computes name = "Search by reference" from
the placeholder. This checks whether that name persists once the field has a value.
"""

from playwright.sync_api import sync_playwright

WEB = "http://localhost:5173"


def name_of(page, testid: str) -> str:
    """Accessible name as Chromium computes it, via aria snapshot."""
    return page.get_by_test_id(testid).aria_snapshot()


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(f"{WEB}/login")
        page.evaluate("() => window.localStorage.setItem('corvid.token', 'probe-token')")
        page.goto(f"{WEB}/orders")
        page.wait_for_selector("[data-testid='orders-search']")

        box = page.get_by_test_id("orders-search")

        print("BEFORE typing:", name_of(page, "orders-search"))
        print("  matched by role+name:",
              page.get_by_role("searchbox", name="Search by reference").count())

        box.click()
        box.type("ORD")
        page.wait_for_timeout(200)

        print("value now:", box.input_value())
        print("AFTER typing:", name_of(page, "orders-search"))
        print("  matched by role+name:",
              page.get_by_role("searchbox", name="Search by reference").count())
        print("  placeholder attr still present:", box.get_attribute("placeholder"))

        # Chromium's own computed accessible name, straight from the a11y tree
        # (independent of Playwright's snapshot formatting).
        cdp = page.context.new_cdp_session(page)
        cdp.send("Accessibility.enable")
        doc = cdp.send("DOM.getDocument")
        node = cdp.send("DOM.querySelector", {
            "nodeId": doc["root"]["nodeId"],
            "selector": "[data-testid='orders-search']",
        })
        ax = cdp.send("Accessibility.getPartialAXTree", {
            "nodeId": node["nodeId"], "fetchRelatives": False,
        })
        for n in ax["nodes"]:
            if n.get("name"):
                print("  CDP AX name:", n["name"].get("value"),
                      "| from:", [s.get("type") for s in n["name"].get("sources", [])
                                  if s.get("value")])

        browser.close()


if __name__ == "__main__":
    main()
