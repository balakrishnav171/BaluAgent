"""One-time Indeed session capture. Run this once to save your login cookies."""
from playwright.sync_api import sync_playwright

SESSION_FILE = "indeed_session.json"

print("Opening Indeed in browser — log in, then press Enter here.")
print("If already logged in, the browser will show your feed automatically.\n")

with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=False, slow_mo=300)
    ctx = browser.new_context()
    page = ctx.new_page()
    page.goto("https://secure.indeed.com/auth", timeout=30000)
    page.wait_for_timeout(4000)

    if "indeed.com" in page.url and "auth" not in page.url:
        print("Detected: already logged in to Indeed.")
    else:
        print("Not logged in — please log in in the browser window.")
        print("Press Enter here once you see your Indeed home/job feed.")
        input()

    ctx.storage_state(path=SESSION_FILE)
    browser.close()

print(f"\nSession saved to {SESSION_FILE}. Indeed applies are now enabled.")
