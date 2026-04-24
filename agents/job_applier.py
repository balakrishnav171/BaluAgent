"""Job Applier Agent — reads job_queue.json, applies to remote jobs, logs results."""
import json
import logging
import re
import smtplib
import ssl
import uuid
from datetime import datetime, timezone
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

MAX_PER_RUN = 30

CANDIDATE = {
    "name": "Balakrishna V",
    "email": "balakrishnah1busa@gmail.com",
    "phone": "512-534-1748",
    "linkedin": "https://www.linkedin.com/in/balakrishna-valluri-bala/",
    "github": "https://github.com/balakrishnav171",
    "rate_c2c": "$85/hr",
    "rate_fte": "$170K+",
    "auth": "H1-B Transfer / C2C",
    "location": "Irving, TX — Remote Only",
    "availability": "Immediate",
    "resume_path": "/Users/balakrishnavalluri/Desktop/2026 Resumes/Balakrishna Site Reliability Engineer 2026.pdf",
}

EMAIL_SUBJECT = (
    "Senior SRE / Platform Engineer — Remote | CKA | AZ-400 | Terraform Certified"
)

EMAIL_BODY_TEMPLATE = """\
Hi {hiring_manager},

I'm a Senior SRE with 12+ years across AWS, Azure, Kubernetes, and Terraform, \
currently at CareFirst BlueCross BlueShield via Resource9 Group.

I came across your {title} opening at {company} and believe I'm a strong fit.

Quick wins I've delivered:
- MTTR: 45-90 min → 15-30 min using Datadog APM + BigPanda correlation
- Alert fatigue: cut by 40% with symptom-based alerting
- Infra automation: 80%+ via Terraform + ArgoCD pipelines
- Datadog spend: $8K → $3K/month through tag-based cost optimization

Certifications: CKA, AZ-400, HashiCorp Terraform Associate, Datadog SRE, FinOps Professional

Open to C2C ($85/hr) or FTE ($170K+). Remote only. Available immediately.
Resume attached.

LinkedIn: {linkedin}
GitHub:   {github}

— Balu
{phone}
"""

_REMOTE_KEYWORDS = {"remote", "work from home", "wfh", "distributed", "anywhere"}
_ONSITE_KEYWORDS = {"onsite", "on-site", "on site", "in office", "in-office", "hybrid"}

# Non-USA location signals — skip these
_NON_USA_SIGNALS = [
    "united kingdom", "uk ", " uk,", "london", "manchester", "edinburgh",
    "canada", "ontario", "toronto", "vancouver", "quebec",
    "australia", "sydney", "melbourne", "brisbane",
    "india", "bangalore", "hyderabad", "pune", "delhi", "mumbai",
    "germany", "berlin", "munich", "hamburg",
    "netherlands", "amsterdam", "austria", "vienna",
    "france", "paris", "spain", "madrid", "portugal", "lisbon",
    "poland", "warsaw", "romania", "bucharest",
    "brazil", "são paulo", "colombia", "bogota", "argentina",
    "philippines", "singapore", "malaysia", "indonesia",
    "mexico", "latam", "emea", "apac",
]

_SALARY_LOW_PATTERNS = [
    "up to $100", "up to $110", "up to $120", "up to $130",
    "$80,000", "$90,000", "$100,000", "$110,000", "$120,000",
]


def _is_usa_remote(job: dict) -> bool:
    """Return True only if job is remote AND in USA."""
    location = (job.get("formattedLocation") or job.get("location") or "").lower()
    title    = (job.get("jobtitle") or "").lower()
    snippet  = (job.get("snippet") or "").lower()
    source   = (job.get("source") or "")

    # Hard reject: non-USA locations regardless of source
    if any(sig in location for sig in _NON_USA_SIGNALS):
        return False

    # Hard reject: explicit onsite/hybrid in title or location
    if any(k in location + " " + title for k in _ONSITE_KEYWORDS):
        return False

    # Remotive is remote-only — accept everything that's not non-USA
    if source == "remotive":
        return True

    # Indeed: trust is_remote=True flag; is_remote=False = not remote
    if source == "indeed":
        if job.get("is_remote") is True:
            return True
        if job.get("is_remote") is False:
            # Double-check: indeed sometimes wrong, accept if title/snippet says remote
            return any(k in title + " " + snippet for k in _REMOTE_KEYWORDS)
        return any(k in location + " " + snippet for k in _REMOTE_KEYWORDS)

    # LinkedIn: jobspy always returns is_remote=False — ignore that flag entirely
    if source == "linkedin":
        # Explicit remote signals in title or snippet → accept
        if any(k in title + " " + snippet for k in _REMOTE_KEYWORDS):
            return True
        # Empty location = jobspy couldn't determine; we searched "Remote" → accept
        if not location:
            return True
        # "United States" or bare state = could be remote
        if location in ("united states",) or (
            len(location.split(",")) == 1 and not any(sig in location for sig in _NON_USA_SIGNALS)
        ):
            return True
        # Specific US city with no onsite signal → accept (many remote roles show city HQ)
        us_state_abbrs = {"al","ak","az","ar","ca","co","ct","de","fl","ga","hi","id","il",
                          "in","ia","ks","ky","la","me","md","ma","mi","mn","ms","mo","mt",
                          "ne","nv","nh","nj","nm","ny","nc","nd","oh","ok","or","pa","ri",
                          "sc","sd","tn","tx","ut","vt","va","wa","wv","wi","wy","dc"}
        parts = [p.strip().lower() for p in location.split(",")]
        if len(parts) == 2 and parts[-1].strip() in us_state_abbrs:
            return True
        if len(parts) == 2 and "united states" in parts[-1]:
            return True
        return False

    # Fallback: check for explicit remote keywords
    return any(k in location + " " + snippet for k in _REMOTE_KEYWORDS)


def _salary_too_low(job: dict) -> bool:
    text = (job.get("snippet") or "").lower()
    return any(p.lower() in text for p in _SALARY_LOW_PATTERNS)


def _extract_apply_email(job: dict) -> str | None:
    import re
    text = job.get("snippet") or ""
    match = re.search(r"[\w.+-]+@[\w-]+\.[a-zA-Z]{2,}", text)
    return match.group(0) if match else None


class JobApplierAgent:
    """Reads job_queue.json, filters, and applies to remote jobs."""

    def __init__(
        self,
        smtp_host: str,
        smtp_port: int,
        smtp_user: str,
        smtp_password: str,
        queue_path: str = "job_queue.json",
        applied_path: str = "applied_jobs.json",
        blocklist_path: str = "blocklist.json",
        max_per_run: int = MAX_PER_RUN,
    ):
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.smtp_user = smtp_user
        self.smtp_password = smtp_password
        self.queue_path = Path(queue_path)
        self.applied_path = Path(applied_path)
        self.blocklist_path = Path(blocklist_path)
        self.max_per_run = max_per_run

        self._applied: list[dict] = self._load_json(self.applied_path, default=[])
        # Only treat successfully applied jobs as done — blocked/failed can be retried
        self._applied_urls: set[str] = {
            j.get("url", "") for j in self._applied if j.get("status") == "applied"
        }
        blocklist = self._load_json(self.blocklist_path, default={"companies": [], "domains": []})
        self._blocked_companies: set[str] = {c.lower() for c in blocklist.get("companies", [])}
        self._blocked_domains: set[str] = {d.lower() for d in blocklist.get("domains", [])}

    # ── I/O helpers ──────────────────────────────────────────────────────────

    def _load_json(self, path: Path, default: Any) -> Any:
        if path.exists():
            try:
                return json.loads(path.read_text())
            except Exception as e:
                logger.warning(f"Failed to read {path}: {e}")
        return default

    def _save_applied(self) -> None:
        self.applied_path.write_text(json.dumps(self._applied, indent=2))

    # ── Filters ──────────────────────────────────────────────────────────────

    def _is_already_applied(self, job: dict) -> bool:
        return job.get("url", "") in self._applied_urls

    def _is_blocked(self, job: dict) -> bool:
        company = (job.get("company") or "").lower()
        if company in self._blocked_companies:
            return True
        url = (job.get("url") or "").lower()
        return any(d in url for d in self._blocked_domains)

    # ── Application methods ───────────────────────────────────────────────────

    def _apply_email(self, job: dict, to_addr: str) -> tuple[bool, str]:
        """Send email application with resume attached. Returns (success, notes)."""
        if not self.smtp_user or not self.smtp_password:
            return False, "SMTP credentials not configured"

        body = EMAIL_BODY_TEMPLATE.format(
            hiring_manager="Hiring Manager",
            title=job.get("jobtitle") or job.get("title") or "the role",
            company=job.get("company") or "your company",
            linkedin=CANDIDATE["linkedin"],
            github=CANDIDATE["github"],
            phone=CANDIDATE["phone"],
        )
        msg = MIMEMultipart()
        msg["Subject"] = EMAIL_SUBJECT
        msg["From"] = self.smtp_user
        msg["To"] = to_addr
        msg["Reply-To"] = CANDIDATE["email"]
        msg.attach(MIMEText(body, "plain"))

        resume_path = Path(CANDIDATE["resume_path"])
        if resume_path.exists():
            with open(resume_path, "rb") as f:
                part = MIMEApplication(f.read(), _subtype="pdf")
                part.add_header(
                    "Content-Disposition", "attachment",
                    filename=resume_path.name,
                )
                msg.attach(part)
        else:
            logger.warning(f"  Resume not found at {resume_path} — sending without attachment")

        try:
            ctx = ssl.create_default_context()
            with smtplib.SMTP(self.smtp_host, self.smtp_port) as server:
                server.ehlo()
                server.starttls(context=ctx)
                server.login(self.smtp_user, self.smtp_password)
                server.sendmail(self.smtp_user, to_addr, msg.as_string())
            logger.info(f"  Email sent to {to_addr} for {job.get('company')}")
            return True, f"Email sent to {to_addr}"
        except Exception as e:
            logger.error(f"  Email failed: {e}")
            return False, f"Email error: {e}"

    def _resolve_url(self, short_url: str) -> str:
        """Follow redirects to get the final URL (e.g. grnh.se → greenhouse.io)."""
        try:
            r = httpx.get(short_url, follow_redirects=True, timeout=10)
            return str(r.url)
        except Exception:
            return short_url

    def _apply_direct_career(self, job: dict, direct_url: str) -> tuple[bool, str]:
        """Route to the right apply strategy based on the career page domain."""
        # Resolve short links (grnh.se, etc.)
        if "grnh.se" in direct_url or len(direct_url.split("/")) <= 4:
            resolved = self._resolve_url(direct_url)
        else:
            resolved = direct_url

        domain = resolved.lower()

        # Greenhouse: use their public API (no browser, no CAPTCHA)
        if "greenhouse.io" in domain or "boards.greenhouse" in domain:
            return self._greenhouse_api_apply(resolved)

        # Lever: API first, then browser fallback
        if "jobs.lever.co" in domain or "lever.co/jobs" in domain:
            return self._lever_api_apply(resolved)

        # Workday needs an account
        if "myworkdayjobs.com" in domain or "workday.com" in domain:
            return False, f"Workday requires account — blocked"

        # iCIMS / BambooHR / ADP needs account
        if any(x in domain for x in ["icims.com", "bamboohr.com", "adp.com", "successfactors"]):
            return False, f"ATS requires account — blocked"

        # Generic: stealth browser
        return self._stealth_browser_apply(resolved)

    def _greenhouse_api_apply(self, url: str) -> tuple[bool, str]:
        """Submit Greenhouse application via stealth browser (API requires auth)."""
        return self._stealth_browser_apply(url)

    def _lever_api_apply(self, url: str) -> tuple[bool, str]:
        """Submit to Lever apply API — no browser."""
        try:
            # https://jobs.lever.co/{company}/{job_id}
            m = re.search(r"jobs\.lever\.co/([^/]+)/([a-f0-9-]{36})", url)
            if not m:
                return self._stealth_browser_apply(url)

            company = m.group(1)
            job_id  = m.group(2)
            api_url = f"https://jobs.lever.co/{company}/{job_id}/apply"

            resume = Path(CANDIDATE["resume_path"])
            if not resume.exists():
                return False, "Resume file not found"

            files = {
                "name":    (None, CANDIDATE["name"]),
                "email":   (None, CANDIDATE["email"]),
                "phone":   (None, CANDIDATE["phone"]),
                "org":     (None, "CareFirst BlueCross BlueShield"),
                "urls[LinkedIn]": (None, CANDIDATE["linkedin"]),
                "urls[GitHub]":   (None, CANDIDATE["github"]),
                "resume":  (resume.name, resume.read_bytes(), "application/pdf"),
                "comments":(None, (
                    "Senior SRE | CKA | AZ-400 | Terraform Certified | "
                    "MTTR 45-90→15-30 min | Remote | C2C $85/hr | FTE $170K+"
                )),
            }
            resp = httpx.post(api_url, files=files, timeout=20,
                              headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"})
            if resp.status_code in (200, 201):
                return True, f"Lever API submitted: {company}/{job_id}"
            return False, f"Lever API {resp.status_code}: {resp.text[:80]}"
        except Exception as e:
            return False, f"Lever API error: {e}"

    def _stealth_browser_apply(self, url: str) -> tuple[bool, str]:
        """Stealth Playwright browser — handles Greenhouse, Lever, and generic forms."""
        try:
            from playwright.sync_api import sync_playwright
            try:
                from playwright_stealth import stealth_sync
                has_stealth = True
            except ImportError:
                has_stealth = False

            with sync_playwright() as pw:
                browser = pw.chromium.launch(
                    channel="chrome",
                    headless=True,
                    args=["--disable-blink-features=AutomationControlled"],
                )
                ctx = browser.new_context(
                    viewport={"width": 1280, "height": 800},
                    locale="en-US",
                    timezone_id="America/Chicago",
                    user_agent=(
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    ),
                )
                page = ctx.new_page()
                if has_stealth:
                    stealth_sync(page)
                page.add_init_script(
                    "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
                )
                page.goto(url, timeout=30000)
                page.wait_for_timeout(3000)

                if page.query_selector("iframe[src*='recaptcha'], div.g-recaptcha"):
                    return False, f"CAPTCHA — blocked: {url[:60]}"

                domain = url.lower()

                # Greenhouse job board form
                if "greenhouse.io" in domain or "job-boards.greenhouse" in domain:
                    # Click "Apply for this Job" if on listing page
                    apply_link = page.query_selector("a#app-apply, a[data-provides='job-application-link']")
                    if apply_link:
                        apply_link.click()
                        page.wait_for_timeout(2000)
                    return self._greenhouse_apply(page, url)

                # Lever
                if "lever.co" in domain:
                    return self._lever_apply(page, url)

                return self._generic_form_apply(page, url)
        except Exception as e:
            return False, f"Stealth browser error: {e}"

    def _greenhouse_apply(self, page: Any, url: str) -> tuple[bool, str]:
        """Fill Greenhouse standard application form."""
        try:
            # Greenhouse fields
            fills = [
                ("#first_name",                 "Balakrishna"),
                ("#last_name",                  "V"),
                ("#email",                      CANDIDATE["email"]),
                ("#phone",                      CANDIDATE["phone"]),
                ("input[name='job_application[first_name]']", "Balakrishna"),
                ("input[name='job_application[last_name]']",  "V"),
                ("input[name='job_application[email]']",      CANDIDATE["email"]),
                ("input[name='job_application[phone]']",      CANDIDATE["phone"]),
            ]
            filled = 0
            for sel, val in fills:
                el = page.query_selector(sel)
                if el:
                    el.fill(val)
                    filled += 1

            # LinkedIn URL field
            for sel in ["#job_application_linkedin_profile", "input[name*='linkedin']"]:
                el = page.query_selector(sel)
                if el:
                    el.fill(CANDIDATE["linkedin"])

            # Resume upload
            resume = Path(CANDIDATE["resume_path"])
            if resume.exists():
                upload = page.query_selector("input[type='file'][name*='resume'], input[type='file'][id*='resume']")
                if upload:
                    upload.set_input_files(str(resume))
                    page.wait_for_timeout(1500)

            # Submit
            submit = page.query_selector("input#submit_app, button[type='submit']")
            if submit and filled >= 2:
                submit.click()
                page.wait_for_timeout(2500)
                return True, f"Greenhouse form submitted: {url[:70]}"
            if filled == 0:
                return False, f"No Greenhouse fields found: {url[:60]}"
            return False, f"Greenhouse: filled {filled} fields but no submit button"
        except Exception as e:
            return False, f"Greenhouse error: {e}"

    def _lever_apply(self, page: Any, url: str) -> tuple[bool, str]:
        """Fill Lever standard application form."""
        try:
            # Click Apply if on job description page
            apply_btn = page.query_selector("a.postings-btn, a[data-qa='btn-apply-bottom']")
            if apply_btn:
                apply_btn.click()
                page.wait_for_timeout(2000)

            fills = [
                ("input[name='name']",       CANDIDATE["name"]),
                ("input[name='email']",      CANDIDATE["email"]),
                ("input[name='phone']",      CANDIDATE["phone"]),
                ("input[name='org']",        "CareFirst BlueCross BlueShield"),
                ("input[name='urls[LinkedIn]']", CANDIDATE["linkedin"]),
                ("input[name='urls[GitHub]']",   CANDIDATE["github"]),
            ]
            filled = 0
            for sel, val in fills:
                el = page.query_selector(sel)
                if el:
                    el.fill(val)
                    filled += 1

            resume = Path(CANDIDATE["resume_path"])
            if resume.exists():
                upload = page.query_selector("input[type='file']")
                if upload:
                    upload.set_input_files(str(resume))
                    page.wait_for_timeout(1500)

            submit = page.query_selector("button[type='submit'], input[type='submit']")
            if submit and filled >= 2:
                submit.click()
                page.wait_for_timeout(2500)
                return True, f"Lever form submitted: {url[:70]}"
            return False, f"Lever: filled {filled} fields, no submit found"
        except Exception as e:
            return False, f"Lever error: {e}"

    def _generic_form_apply(self, page: Any, url: str) -> tuple[bool, str]:
        """Try generic field detection on unknown career page."""
        try:
            field_map = {
                "input[type='text'][name*='first']":  "Balakrishna",
                "input[type='text'][name*='last']":   "V",
                "input[type='email']":                CANDIDATE["email"],
                "input[type='tel']":                  CANDIDATE["phone"],
                "input[name*='linkedin']":            CANDIDATE["linkedin"],
            }
            filled = 0
            for sel, val in field_map.items():
                el = page.query_selector(sel)
                if el:
                    el.fill(val)
                    filled += 1

            resume = Path(CANDIDATE["resume_path"])
            if resume.exists():
                upload = page.query_selector("input[type='file']")
                if upload:
                    upload.set_input_files(str(resume))
                    page.wait_for_timeout(1500)

            submit = page.query_selector("button[type='submit'], input[type='submit']")
            if submit and filled >= 2:
                submit.click()
                page.wait_for_timeout(2000)
                return True, f"Generic form submitted ({filled} fields): {url[:70]}"
            return False, f"Generic: filled {filled} fields — form too complex or no submit"
        except Exception as e:
            return False, f"Generic form error: {e}"

    def _apply_browser(self, job: dict) -> tuple[bool, str]:
        """Playwright-based browser apply (LinkedIn Easy Apply / Indeed / direct)."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return False, "playwright not installed — run: pip install playwright && playwright install chromium"

        url = job.get("url", "")
        source = job.get("source", "")
        li_session   = Path("linkedin_session.json")
        in_session   = Path("indeed_session.json")

        try:
            with sync_playwright() as pw:
                launch_kwargs: dict = {"headless": True}
                browser = pw.chromium.launch(**launch_kwargs)
                if source == "linkedin" and li_session.exists():
                    ctx = browser.new_context(storage_state=str(li_session))
                elif source == "indeed" and in_session.exists():
                    ctx = browser.new_context(storage_state=str(in_session))
                else:
                    ctx = browser.new_context()

                page = ctx.new_page()
                page.set_extra_http_headers({"User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                )})
                page.goto(url, timeout=30000)
                page.wait_for_timeout(2000)

                if source == "linkedin" and "linkedin.com" in url:
                    return self._linkedin_easy_apply(page, job)
                elif source == "indeed" and "indeed.com" in url:
                    return self._indeed_apply(page, job)
                else:
                    return self._direct_apply(page, job)
        except Exception as e:
            return False, f"Browser error: {e}"

    def _linkedin_easy_apply(self, page: Any, job: dict) -> tuple[bool, str]:
        try:
            # Check login state
            if "authwall" in page.url or "login" in page.url:
                return False, "LinkedIn session expired — rerun setup_linkedin.py"

            # Look for Easy Apply first (native LinkedIn flow)
            easy_btn = (
                page.query_selector("button.jobs-apply-button[aria-label*='Easy Apply']")
                or page.query_selector("button[aria-label*='Easy Apply']")
            )

            # Fall back to any Apply button (external company page)
            if not easy_btn:
                ext_btn = (
                    page.query_selector(".apply-button")
                    or page.query_selector("button.jobs-apply-button")
                    or page.query_selector("a[data-tracking-control-name*='apply']")
                )
                if ext_btn:
                    # May open a new tab or navigate — capture destination
                    with page.context.expect_page(timeout=8000) as new_page_info:
                        ext_btn.click()
                    try:
                        new_page = new_page_info.value
                        new_page.wait_for_load_state("domcontentloaded", timeout=15000)
                        dest_url = new_page.url
                        return self._apply_direct_career(job, dest_url)
                    except Exception:
                        pass
                    # Fallback: same-tab navigation
                    page.wait_for_timeout(3000)
                    if page.url != job.get("url", ""):
                        return self._apply_direct_career(job, page.url)
                return False, "No Easy Apply button — external apply or not logged in"

            btn = easy_btn

            btn.click()
            page.wait_for_timeout(2500)

            # Fill phone if prompted
            phone_field = page.query_selector("input[id*='phoneNumber']")
            if phone_field:
                phone_field.fill(CANDIDATE["phone"])

            # Step through multi-page Easy Apply modal
            for _step in range(6):
                if page.query_selector("iframe[src*='recaptcha']"):
                    return False, "CAPTCHA detected on Easy Apply"

                # Look for Next / Review / Submit buttons in order of finality
                submit_btn = page.query_selector("button[aria-label='Submit application']")
                if submit_btn:
                    submit_btn.click()
                    page.wait_for_timeout(2000)
                    return True, "LinkedIn Easy Apply submitted"

                review_btn = page.query_selector("button[aria-label='Review your application']")
                if review_btn:
                    review_btn.click()
                    page.wait_for_timeout(1500)
                    continue

                next_btn = page.query_selector("button[aria-label='Continue to next step']")
                if next_btn:
                    next_btn.click()
                    page.wait_for_timeout(1500)
                    continue

                # No recognisable button — form is complex
                return False, "Multi-step form too complex — manual apply needed"

            return False, "Easy Apply steps exceeded — manual apply needed"
        except Exception as e:
            return False, f"LinkedIn apply error: {e}"

    def _indeed_apply(self, page: Any, job: dict) -> tuple[bool, str]:
        try:
            page.wait_for_timeout(2000)

            # 1. Try Indeed Easy Apply (requires session)
            easy_btn = page.query_selector("#indeedApplyButton, .ia-IndeedApplyButton, button[data-indeed-apply]")
            if easy_btn:
                easy_btn.click()
                page.wait_for_timeout(3000)
                if page.query_selector("iframe[src*='recaptcha']"):
                    return False, "CAPTCHA on Indeed Easy Apply"
                # Fill basics if form opened
                for sel, val in [
                    ("input[name='applicant.name']", CANDIDATE["name"]),
                    ("input[name='applicant.emailAddress']", CANDIDATE["email"]),
                    ("input[name='applicant.phoneNumber']", CANDIDATE["phone"]),
                ]:
                    el = page.query_selector(sel)
                    if el:
                        el.fill(val)
                submit = page.query_selector("button[data-testid='ia-continueButton']") or \
                         page.query_selector("button:has-text('Submit')")
                if submit:
                    submit.click()
                    page.wait_for_timeout(2000)
                    return True, "Indeed Easy Apply submitted"
                return False, "Indeed form too complex — needs manual apply"

            # 2. Try external apply link on the job page
            ext_btn = (
                page.query_selector("a.icl-Button--primary[href*='apply']")
                or page.query_selector("a[data-testid='apply-button-container']")
                or page.query_selector("a:has-text('Apply on company site')")
                or page.query_selector("a:has-text('Apply now')")
            )
            if ext_btn:
                href = ext_btn.get_attribute("href") or ""
                if href:
                    return self._direct_apply_url(page, job, href)

            return False, "No apply button found on Indeed page — needs session login"
        except Exception as e:
            return False, f"Indeed apply error: {e}"

    def _direct_apply_url(self, page: Any, job: dict, url: str) -> tuple[bool, str]:
        """Follow an external apply link and attempt to fill the company form."""
        try:
            page.goto(url, timeout=25000)
            page.wait_for_timeout(2000)
            if page.query_selector("iframe[src*='recaptcha']"):
                return False, "CAPTCHA on company careers page"
            # Greenhouse / Lever / Workday common selectors
            for sel, val in [
                ("input[name='job_application[first_name]']", "Balakrishna"),
                ("input[name='job_application[last_name]']", "V"),
                ("input[name='job_application[email]']", CANDIDATE["email"]),
                ("input[name='job_application[phone]']", CANDIDATE["phone"]),
                ("input#first_name", "Balakrishna"),
                ("input#last_name", "V"),
                ("input#email", CANDIDATE["email"]),
                ("input#phone", CANDIDATE["phone"]),
            ]:
                el = page.query_selector(sel)
                if el:
                    el.fill(val)
            # Greenhouse submit
            submit = page.query_selector("input[type='submit']") or page.query_selector("button[type='submit']")
            if submit:
                submit.click()
                page.wait_for_timeout(2000)
                return True, f"Applied via company careers page: {url[:80]}"
            return False, f"Company form found but no submit button: {url[:60]}"
        except Exception as e:
            return False, f"Company site apply error: {e}"

    def _direct_apply(self, page: Any, job: dict) -> tuple[bool, str]:
        try:
            selectors = [
                "a[href*='apply']", "button:has-text('Apply')",
                "a:has-text('Apply Now')", "a:has-text('Apply for this job')",
            ]
            for sel in selectors:
                el = page.query_selector(sel)
                if el:
                    el.click()
                    page.wait_for_timeout(2000)
                    return False, "Direct apply page opened — needs form fill (complex)"
            return False, "No apply button found on careers page"
        except Exception as e:
            return False, f"Direct apply error: {e}"

    # ── Logging ───────────────────────────────────────────────────────────────

    def _log_result(self, job: dict, method: str, status: str, notes: str) -> None:
        entry = {
            "job_id": str(uuid.uuid4())[:8],
            "company": job.get("company", ""),
            "title": job.get("jobtitle") or job.get("title") or "",
            "url": job.get("url", ""),
            "applied_at": datetime.now(timezone.utc).isoformat(),
            "method": method,
            "status": status,
            "notes": notes,
            "match_score": job.get("match_score", 0),
            "source": job.get("source", ""),
        }
        self._applied.append(entry)
        self._applied_urls.add(job.get("url", ""))
        self._save_applied()
        logger.info(f"  Logged [{status}] {entry['company']} — {entry['title']}")

    # ── Summary email ─────────────────────────────────────────────────────────

    def _send_summary(self, results: list[dict]) -> None:
        applied = [r for r in results if r["status"] == "applied"]
        skipped = [r for r in results if r["status"] == "skipped"]
        blocked = [r for r in results if r["status"] == "blocked"]
        failed  = [r for r in results if r["status"] == "failed"]

        top5 = sorted(applied, key=lambda x: x.get("match_score", 0), reverse=True)[:5]
        top5_text = "\n".join(
            f"  {i+1}. {r['company']} — {r['title']} (score: {int(r.get('match_score',0)*100)}%)"
            for i, r in enumerate(top5)
        ) or "  (none)"

        body = f"""\
BaluAgent-Applier Run Summary — {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}

Jobs applied:        {len(applied)}
Jobs skipped:        {len(skipped)} (not remote / below salary / already applied)
Jobs blocked:        {len(blocked)} (CAPTCHA / form-too-complex / no button)
Jobs failed:         {len(failed)}

Top 5 Applied:
{top5_text}

Full log: applied_jobs.json
"""
        if not self.smtp_user or not self.smtp_password:
            logger.warning("SMTP not configured — skipping summary email")
            return

        msg = MIMEMultipart()
        msg["Subject"] = f"BaluAgent Applied {len(applied)} Jobs Today"
        msg["From"] = self.smtp_user
        msg["To"] = CANDIDATE["email"]
        msg.attach(MIMEText(body, "plain"))
        try:
            ctx = ssl.create_default_context()
            with smtplib.SMTP(self.smtp_host, self.smtp_port) as server:
                server.ehlo()
                server.starttls(context=ctx)
                server.login(self.smtp_user, self.smtp_password)
                server.sendmail(self.smtp_user, CANDIDATE["email"], msg.as_string())
            logger.info(f"Summary email sent — {len(applied)} applied")
        except Exception as e:
            logger.error(f"Summary email failed: {e}")

    # ── Main run ──────────────────────────────────────────────────────────────

    def run(self) -> dict:
        logger.info("BaluAgent-Applier starting...")
        queue: list[dict] = self._load_json(self.queue_path, default=[])
        if not queue:
            logger.warning(f"{self.queue_path} is empty — run BaluAgent-Finder first")
            return {"applied": 0, "skipped": 0, "blocked": 0, "failed": 0}

        logger.info(f"Queue: {len(queue)} jobs loaded")
        results: list[dict] = []
        applied_count = 0

        queue_sorted = sorted(queue, key=lambda j: j.get("match_score", 0), reverse=True)

        for job in queue_sorted:
            if applied_count >= self.max_per_run:
                logger.info(f"Reached {self.max_per_run}-job limit — stopping")
                break

            company = job.get("company", "?")
            title   = job.get("jobtitle") or job.get("title") or "?"
            url     = job.get("url", "")

            # Skip guards
            if self._is_already_applied(job):
                logger.info(f"Skip (already applied): {company} — {title}")
                results.append({**job, "status": "skipped", "method": "none", "notes": "already applied"})
                continue

            if self._is_blocked(job):
                logger.info(f"Skip (blocklist): {company}")
                results.append({**job, "status": "skipped", "method": "none", "notes": "on blocklist"})
                self._log_result(job, "none", "skipped", "on blocklist")
                continue

            if not _is_usa_remote(job):
                logger.info(f"Skip (not remote): {company} — {title}")
                results.append({**job, "status": "skipped", "method": "none", "notes": "not remote"})
                self._log_result(job, "none", "skipped", "not remote")
                continue

            if _salary_too_low(job):
                logger.info(f"Skip (salary too low): {company} — {title}")
                results.append({**job, "status": "skipped", "method": "none", "notes": "salary below threshold"})
                self._log_result(job, "none", "skipped", "salary below threshold")
                continue

            logger.info(f"Applying → {company} — {title} ({url[:60]})")

            # Method 1: email apply if address found in JD
            apply_email = _extract_apply_email(job)
            if apply_email:
                success, notes = self._apply_email(job, apply_email)
                method = "email"
                status = "applied" if success else "failed"
            elif job.get("url_direct"):
                # Method 2: direct company careers page (no portal login needed)
                success, notes = self._apply_direct_career(job, job["url_direct"])
                method = "direct"
                status = "applied" if success else "blocked"
            else:
                # Method 3: portal browser (LinkedIn session / Indeed session)
                success, notes = self._apply_browser(job)
                method = "browser"
                status = "applied" if success else "blocked"

            results.append({**job, "status": status, "method": method, "notes": notes})
            self._log_result(job, method, status, notes)

            if success:
                applied_count += 1

        self._send_summary(results)

        summary = {
            "applied": sum(1 for r in results if r["status"] == "applied"),
            "skipped": sum(1 for r in results if r["status"] == "skipped"),
            "blocked": sum(1 for r in results if r["status"] == "blocked"),
            "failed":  sum(1 for r in results if r["status"] == "failed"),
            "total_processed": len(results),
        }
        logger.info(f"Done: {summary}")
        return summary

    def get_state(self) -> dict[str, Any]:
        return {
            "agent": "JobApplierAgent",
            "queue_path": str(self.queue_path),
            "applied_path": str(self.applied_path),
            "max_per_run": self.max_per_run,
            "total_applied_ever": len(self._applied),
        }
