import asyncio
from pathlib import Path
from playwright.async_api import async_playwright

CHROME_PATH = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
OUTPUT_DIR = Path("F:/AI/AgentMake/AgentProjects/WebHub/temp").resolve()

async def run_browser_automation():
    print("[1/5] Starting Google Chrome automation...")
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            executable_path=CHROME_PATH,
            headless=True,  # Run in background for automated test
        )
        context = await browser.new_context(viewport={"width": 1280, "height": 800})
        page = await context.new_page()

        print("[2/5] Navigating to login page...")
        await page.goto("http://localhost:3100/login")
        await page.wait_for_load_state("networkidle")
        
        # Fill in login form
        print("[3/5] Performing admin login...")
        await page.fill("input[name='username']", "admin")
        await page.fill("input[name='password']", "admin123")
        await page.click("button[type='submit']")

        # Wait for redirect to library or home
        await page.wait_for_timeout(2000)
        await page.wait_for_load_state("networkidle")
        print(f"[LOGIN SUCCESS] Current URL: {page.url}")

        # Navigate to Library
        print("[4/5] Navigating to /library page...")
        await page.goto("http://localhost:3100/library")
        await page.wait_for_timeout(1500)
        
        screenshot_lib = OUTPUT_DIR / "chrome_test_library.png"
        await page.screenshot(path=str(screenshot_lib), full_page=True)
        print(f"[SCREENSHOT SAVED] Library page -> {screenshot_lib}")

        # Navigate to Provider Settings
        print("[5/5] Navigating to /settings/providers page...")
        await page.goto("http://localhost:3100/settings/providers")
        await page.wait_for_timeout(1500)

        screenshot_prov = OUTPUT_DIR / "chrome_test_providers.png"
        await page.screenshot(path=str(screenshot_prov), full_page=True)
        print(f"[SCREENSHOT SAVED] Provider Settings page -> {screenshot_prov}")

        await browser.close()
        print("[AUTOMATION COMPLETE] Full Chrome E2E test finished successfully!")

if __name__ == "__main__":
    asyncio.run(run_browser_automation())
