"""One-time LinkedIn session capture. Run this once to save your login cookies."""
from playwright.sync_api import sync_playwright

SESSION_FILE = "linkedin_session.json"

print("Opening browser — log in to LinkedIn if prompted, then press Enter here.")
print("If you're already logged in the browser will show your feed automatically.\n")

with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=False, slow_mo=500)
    ctx = browser.new_context()
    page = ctx.new_page()
    page.goto("https://www.linkedin.com/feed/", timeout=30000)
    page.wait_for_timeout(4000)

    if "feed" in page.url or "mynetwork" in page.url:
        print("Detected: already logged in.")
    else:
        print("Not logged in — please log in in the browser window.")
        print("Press Enter here once you are on the LinkedIn feed.")
        input()

    ctx.storage_state(path=SESSION_FILE)
    browser.close()

print(f"\nSession saved to {SESSION_FILE}. You can now run: python3 main.py apply")
