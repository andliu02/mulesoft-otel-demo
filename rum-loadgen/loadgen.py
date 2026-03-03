"""
RUM Load Generator — Headless browser simulation for Elastic APM RUM data.

Uses Playwright to open the FNB Portal in a real Chromium browser,
triggering the Elastic APM RUM agent on every page load and user interaction.

Simulates:
  - Page loads (triggers page-load transactions + Web Vitals)
  - Tab navigation (triggers user-interaction spans)
  - Wire transfer submissions (triggers fetch spans)
  - ACH payment submissions
  - Customer 360 lookups
  - Account opening submissions
"""

import asyncio
import random
import logging
import os
import time
from datetime import datetime

from playwright.async_api import async_playwright

PORTAL_URL = os.getenv("PORTAL_URL", "http://fnb-portal:8080")
MIN_INTERVAL = float(os.getenv("MIN_INTERVAL", "3"))
MAX_INTERVAL = float(os.getenv("MAX_INTERVAL", "10"))
STARTUP_DELAY = int(os.getenv("STARTUP_DELAY", "30"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s [rum-loadgen] %(message)s",
)
logger = logging.getLogger("rum-loadgen")

ACCOUNTS = [f"ACC{i:08d}" for i in range(1, 101)]
CUSTOMERS = [f"CUST{i:06d}" for i in range(1, 101)]
COUNTRIES = ["US", "GB", "DE", "SG", "JP"]
CURRENCIES = ["USD", "EUR", "GBP"]
PURPOSES = ["TRADE", "INVESTMENT", "PERSONAL", "PAYROLL"]
SEC_CODES = ["PPD", "CCD", "CTX"]
FIRST_NAMES = ["James", "Sarah", "Michael", "Emily", "Robert", "Lisa", "David", "Jennifer"]
LAST_NAMES = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis"]


async def page_load(page):
    """Full page reload — generates page-load transaction + Web Vitals."""
    logger.info("Action: page load")
    await page.reload(wait_until="networkidle")
    await asyncio.sleep(2)  # let RUM agent send page-load data


async def switch_tab(page, tab_name):
    """Click a sidebar tab — generates user-interaction span."""
    tab_map = {
        "dashboard": "Dashboard",
        "wire": "Wire Transfer",
        "ach": "ACH Payment",
        "customer": "Customer 360",
        "account": "Open Account",
    }
    label = tab_map.get(tab_name, tab_name)
    logger.info(f"Action: switch to tab '{label}'")
    try:
        link = page.locator(f".sidebar a", has_text=label)
        await link.click()
        await asyncio.sleep(0.5)
    except Exception as e:
        logger.debug(f"Tab switch failed: {e}")


async def submit_wire(page):
    """Fill and submit a wire transfer form."""
    logger.info("Action: submit wire transfer")
    await switch_tab(page, "wire")
    await asyncio.sleep(0.3)

    await page.fill("#wire-src", random.choice(ACCOUNTS))
    await page.fill("#wire-dst", f"EXT{random.randint(10000000, 99999999)}")
    await page.fill("#wire-amt", str(round(random.uniform(1000, 250000), 2)))

    ccy_select = page.locator("#wire-ccy")
    await ccy_select.select_option(random.choice(CURRENCIES))

    country_select = page.locator("#wire-country")
    await country_select.select_option(random.choice(COUNTRIES))

    purpose_select = page.locator("#wire-purpose")
    await purpose_select.select_option(random.choice(PURPOSES))

    await page.click("#wire-btn")
    await asyncio.sleep(3)  # wait for response + RUM flush


async def submit_ach(page):
    """Fill and submit an ACH payment form."""
    logger.info("Action: submit ACH payment")
    await switch_tab(page, "ach")
    await asyncio.sleep(0.3)

    await page.fill("#ach-src", random.choice(ACCOUNTS))
    await page.fill("#ach-routing", f"0{random.randint(21000000, 29999999)}")
    await page.fill("#ach-dst", str(random.randint(10000000, 99999999)))
    await page.fill("#ach-amt", str(round(random.uniform(100, 25000), 2)))

    sec_select = page.locator("#ach-sec")
    await sec_select.select_option(random.choice(SEC_CODES))

    await page.click("#ach-btn")
    await asyncio.sleep(3)


async def lookup_customer(page):
    """Customer 360 lookup."""
    logger.info("Action: customer 360 lookup")
    await switch_tab(page, "customer")
    await asyncio.sleep(0.3)

    await page.fill("#cust-id", random.choice(CUSTOMERS))
    await page.click("#cust-btn")
    await asyncio.sleep(3)


async def open_account(page):
    """Open a new account."""
    logger.info("Action: open account")
    await switch_tab(page, "account")
    await asyncio.sleep(0.3)

    await page.fill("#acct-fname", random.choice(FIRST_NAMES))
    await page.fill("#acct-lname", random.choice(LAST_NAMES))
    await page.fill(
        "#acct-dob",
        f"{random.randint(1960, 2000)}-{random.randint(1,12):02d}-{random.randint(1,28):02d}",
    )
    await page.fill("#acct-deposit", str(round(random.uniform(500, 50000), 2)))

    type_select = page.locator("#acct-type")
    await type_select.select_option(random.choice(["CHECKING", "SAVINGS"]))

    await page.click("#acct-btn")
    await asyncio.sleep(3)


ACTIONS = [
    (page_load,        0.15),  # 15% full page reloads
    (submit_wire,      0.30),  # 30% wire transfers
    (submit_ach,       0.25),  # 25% ACH payments
    (lookup_customer,  0.20),  # 20% customer lookups
    (open_account,     0.10),  # 10% account opening
]


async def run():
    logger.info(f"RUM load generator starting in {STARTUP_DELAY}s (waiting for portal)...")
    await asyncio.sleep(STARTUP_DELAY)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )

        # Create 2 concurrent browser contexts to simulate multiple users
        num_users = int(os.getenv("NUM_USERS", "2"))
        logger.info(f"Launching {num_users} virtual users against {PORTAL_URL}")

        tasks = [simulate_user(browser, i + 1) for i in range(num_users)]
        await asyncio.gather(*tasks)


async def simulate_user(browser, user_id):
    """Simulate a single user session in a browser context."""
    context = await browser.new_context(
        viewport={"width": 1920, "height": 1080},
        user_agent=f"FNB-RUM-LoadGen/1.0 (User {user_id})",
    )
    page = await context.new_page()

    # Initial page load
    logger.info(f"[User {user_id}] Loading portal: {PORTAL_URL}")
    try:
        await page.goto(PORTAL_URL, wait_until="networkidle", timeout=60000)
    except Exception as e:
        logger.error(f"[User {user_id}] Failed to load portal: {e}")
        await asyncio.sleep(10)
        try:
            await page.goto(PORTAL_URL, wait_until="networkidle", timeout=60000)
        except Exception as e2:
            logger.error(f"[User {user_id}] Retry failed: {e2}")
            return

    logger.info(f"[User {user_id}] Portal loaded, starting interactions")
    await asyncio.sleep(3)  # let initial RUM page-load flush

    cycle = 0
    while True:
        cycle += 1
        try:
            # Pick a weighted random action
            fns = [f for f, _ in ACTIONS]
            weights = [w for _, w in ACTIONS]
            action = random.choices(fns, weights=weights, k=1)[0]
            await action(page)

            # Every 20 cycles, do a full page reload to reset state
            if cycle % 20 == 0:
                logger.info(f"[User {user_id}] Session refresh (cycle {cycle})")
                await page.goto(PORTAL_URL, wait_until="networkidle", timeout=60000)
                await asyncio.sleep(2)

        except Exception as e:
            logger.warning(f"[User {user_id}] Action failed: {e}")
            try:
                await page.goto(PORTAL_URL, wait_until="networkidle", timeout=60000)
                await asyncio.sleep(2)
            except Exception:
                logger.error(f"[User {user_id}] Recovery failed, waiting 30s")
                await asyncio.sleep(30)

        interval = random.uniform(MIN_INTERVAL, MAX_INTERVAL)
        await asyncio.sleep(interval)


if __name__ == "__main__":
    asyncio.run(run())
