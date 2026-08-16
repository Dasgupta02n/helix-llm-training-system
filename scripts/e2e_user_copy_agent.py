"""Copy a real C7X user end-to-end and log both sides.

Walks: sign in → Riu task → 5 gold → 20 synthetics → download library
→ train smallest Apache/MIT model → accept declaration → download adapter.

Writes:
  data/e2e_runs/<stamp>/steps.jsonl   machine log
  data/e2e_runs/<stamp>/report.md     human report (agent + system)

Does not print passwords or tokens.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

SMALLEST_MODEL_ID = "HuggingFaceTB/SmolLM2-1.7B-Instruct"

# Rotate domains so each run exercises a different gold type.
TASKS: dict[str, dict[str, str]] = {
    "refunds": {
        "role": (
            "Refund and return desk for a D2C apparel brand. "
            "Decide approve, deny, or escalate each request."
        ),
        "domain": (
            "Online clothing store. 30-day returns if unworn with tags. "
            "Final sale items and worn garments are not refunded. "
            "Escalate fraud, chargebacks, and anything over $200."
        ),
        "example_in": (
            "Customer bought a $48 sweater 12 days ago, tags still on, "
            "says it arrived with a pulled thread. Wants a full refund."
        ),
        "example_out": (
            "Approve a full refund and send a prepaid label. "
            "The item is inside 30 days, tags on, and a manufacturing defect "
            "is our fault. Note the defect in the return reason."
        ),
        "edge_1": (
            "Same sweater, day 41, tags on. Policy window closed — deny the refund, "
            "offer store credit once as goodwill, and say why."
        ),
        "edge_2": (
            "Customer already opened a chargeback while also emailing support. "
            "Escalate to payments; do not also issue a refund."
        ),
    },
    "it_helpdesk": {
        "role": (
            "IT helpdesk for a 200-person SaaS company. Classify each ticket: "
            "reset password, grant access, escalate hardware, or close as not IT."
        ),
        "domain": (
            "Internal IT. Password resets after identity check. Access grants "
            "need a manager name. Hardware goes to facilities. Payroll and "
            "HR questions are not IT — close and point to People Ops."
        ),
        "example_in": (
            "I cannot log into Okta after the weekend. Laptop is fine. "
            "I have my employee ID 4821."
        ),
        "example_out": (
            "Reset password. Identity is confirmed via employee ID. Send a "
            "one-time reset link and ask them to enroll MFA again if prompted."
        ),
        "edge_1": (
            "Someone asking for admin on the production database 'just for today'. "
            "Deny self-serve; escalate to security with the ticket id."
        ),
        "edge_2": (
            "Ticket is 'when is payday'. Close as not IT and point to People Ops. "
            "Do not reset anything."
        ),
    },
    "invoice_ap": {
        "role": (
            "Accounts payable desk. Code each vendor invoice to a GL account "
            "or flag it for review."
        ),
        "domain": (
            "B2B AP. Software subscriptions → 6400 Software. Office supplies "
            "→ 6100 Supplies. Anything over $5,000 or a new vendor → flag review. "
            "Duplicate invoice numbers → hold and do not pay."
        ),
        "example_in": (
            "Invoice INV-8831 from Notion, $144, monthly workspace plan, "
            "same vendor we paid last month."
        ),
        "example_out": (
            "Code to 6400 Software. Known vendor, under $5,000, recurring. "
            "Approve for payment on terms."
        ),
        "edge_1": (
            "New vendor, first invoice $12,400 for 'consulting'. Flag review; "
            "do not code or pay until procurement confirms the PO."
        ),
        "edge_2": (
            "Invoice number INV-200 already paid last Tuesday. Hold. "
            "Do not pay again. Note possible duplicate."
        ),
    },
    "insurance_claims": {
        "role": (
            "Auto-insurance first notice of loss. Route each claim: "
            "glass-only, collision, or SIU fraud review."
        ),
        "domain": (
            "Personal auto. Glass-only claims under $800 settle in glass. "
            "Collision with a police report goes to collision. "
            "Late report plus conflicting stories go to SIU."
        ),
        "example_in": (
            "Parked car, cracked windshield from a rock on the highway yesterday. "
            "No other damage. Estimate $420."
        ),
        "example_out": (
            "Glass-only. Under $800, no other damage, no injury. "
            "Send to glass vendor; no collision file."
        ),
        "edge_1": (
            "Two-car crash at a light, police report attached, airbags deployed. "
            "Route to collision and open a bodily-injury check."
        ),
        "edge_2": (
            "Claim filed 40 days later, story changed twice, other driver "
            "uncontactable. Route to SIU; do not pay yet."
        ),
    },
    "hr_leave": {
        "role": (
            "HR leave desk. Decide approve, deny, or request-more-info "
            "for each time-off request."
        ),
        "domain": (
            "15 vacation days per year. Sick leave needs no reason under 3 days. "
            "Parental leave is 12 weeks with 30 days notice. "
            "Blackout weeks around year-end close are denied unless sick."
        ),
        "example_in": (
            "Engineer wants 4 vacation days in June. Balance is 9 days. "
            "Team coverage is confirmed."
        ),
        "example_out": (
            "Approve. Balance covers it, not a blackout week, coverage confirmed. "
            "Log 4 days against vacation."
        ),
        "edge_1": (
            "Request for the last week of December, vacation. Blackout week — "
            "deny vacation; they may rebook January or use sick if actually ill."
        ),
        "edge_2": (
            "Parental leave starting in 10 days, no paperwork. Request more info: "
            "need the form and 30-day notice or an exception from HRBP."
        ),
    },
}
TASK_ORDER = list(TASKS.keys())


def pick_task(requested: str) -> tuple[str, dict[str, str]]:
    if requested:
        key = requested.strip().lower().replace("-", "_")
        if key not in TASKS:
            raise SystemExit(f"Unknown task {key}. Choose: {', '.join(TASK_ORDER)}")
        return key, TASKS[key]
    used: list[str] = []
    runs = ROOT / "data" / "e2e_runs"
    if runs.is_dir():
        for p in sorted(runs.iterdir()):
            meta = p / "task.json"
            if not meta.is_file():
                # First run had no task.json — it was refunds.
                if (p / "report.md").is_file():
                    used.append("refunds")
                continue
            try:
                used.append(json.loads(meta.read_text(encoding="utf-8")).get("id") or "")
            except Exception:
                pass
    for key in TASK_ORDER:
        if key not in used:
            return key, TASKS[key]
    nxt = TASK_ORDER[len(used) % len(TASK_ORDER)]
    return nxt, TASKS[nxt]


class DualLog:
    def __init__(self, out_dir: Path) -> None:
        self.out_dir = out_dir
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.jsonl = self.out_dir / "steps.jsonl"
        self.rows: list[dict] = []
        self.ux: list[str] = []

    def _write(self, row: dict) -> None:
        self.rows.append(row)
        with self.jsonl.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        who = row["actor"].upper()
        print(f"[{who}] {row['step']}: {row['action']}", flush=True)

    def agent(self, step: str, action: str, **detail: object) -> None:
        self._write(
            {
                "ts": datetime.now(timezone.utc).isoformat(),
                "actor": "agent",
                "step": step,
                "action": action,
                "detail": detail,
            }
        )

    def system(self, step: str, action: str, **detail: object) -> None:
        self._write(
            {
                "ts": datetime.now(timezone.utc).isoformat(),
                "actor": "system",
                "step": step,
                "action": action,
                "detail": detail,
            }
        )

    def ux_note(self, note: str) -> None:
        self.ux.append(note)
        self.agent("ux", "friction_or_insight", note=note)

    def report(self) -> Path:
        path = self.out_dir / "report.md"
        lines = [
            "# C7X e2e user-copy report",
            "",
            f"Started: {self.rows[0]['ts'] if self.rows else '—'}",
            f"Finished: {datetime.now(timezone.utc).isoformat()}",
            "",
            "## UX notes",
            "",
        ]
        if self.ux:
            lines.extend(f"- {n}" for n in self.ux)
        else:
            lines.append("- (none recorded)")
        lines += ["", "## Timeline", ""]
        for row in self.rows:
            det = json.dumps(row.get("detail") or {}, ensure_ascii=False)
            if len(det) > 800:
                det = det[:800] + "…"
            lines.append(
                f"- `{row['ts']}` **{row['actor']}** `{row['step']}` — "
                f"{row['action']} — {det}"
            )
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path


class Client:
    def __init__(self, base: str, log: DualLog) -> None:
        self.base = base.rstrip("/")
        self.log = log
        self.token = ""
        # Short default so a hung status GET cannot stall the whole run.
        self.http = httpx.Client(
            timeout=httpx.Timeout(60.0, connect=15.0),
            follow_redirects=True,
        )

    def close(self) -> None:
        self.http.close()

    def _headers(self) -> dict[str, str]:
        h = {"Accept": "application/json", "User-Agent": "C7X-e2e-user-copy/1.0"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def req(
        self,
        step: str,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
        expect: int = 200,
        timeout: float | None = None,
        retries: int = 3,
    ) -> httpx.Response:
        url = path if path.startswith("http") else f"{self.base}{path}"
        last_exc: Exception | None = None
        for attempt in range(1, retries + 1):
            self.log.agent(
                step,
                f"{method} {path}",
                has_body=bool(json_body),
                attempt=attempt,
                timeout=timeout,
            )
            t0 = time.monotonic()
            try:
                resp = self.http.request(
                    method,
                    url,
                    headers=self._headers(),
                    json=json_body,
                    timeout=timeout,
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                ms = int((time.monotonic() - t0) * 1000)
                self.log.system(
                    step,
                    f"{type(exc).__name__} in {ms}ms",
                    attempt=attempt,
                    retries=retries,
                    error=str(exc)[:300],
                )
                last_exc = exc
                if attempt >= retries:
                    raise
                time.sleep(min(8, 2 * attempt))
                continue
            ms = int((time.monotonic() - t0) * 1000)
            body: object
            ctype = (resp.headers.get("content-type") or "").lower()
            try:
                if "zip" in ctype or "octet-stream" in ctype:
                    body = {"bytes": len(resp.content), "content_type": ctype}
                else:
                    body = resp.json()
            except Exception:
                body = {"text": resp.text[:400]}
            slim = body
            if isinstance(body, dict):
                slim = {
                    k: body[k]
                    for k in body
                    if k
                    not in {
                        "access_token",
                        "hashed_password",
                        "html",
                        "declaration_html",
                    }
                }
                if "access_token" in body:
                    slim["access_token"] = "set" if body.get("access_token") else ""
            self.log.system(
                step,
                f"HTTP {resp.status_code} in {ms}ms",
                status=resp.status_code,
                expected=expect,
                attempt=attempt,
                body=slim if not isinstance(slim, list) else {"count": len(slim)},
            )
            if resp.status_code in {502, 503, 504} and attempt < retries:
                time.sleep(min(8, 2 * attempt))
                continue
            if expect and resp.status_code != expect and not (
                expect == 200 and resp.status_code < 300
            ):
                raise RuntimeError(
                    f"{method} {path} -> {resp.status_code}: {resp.text[:400]}"
                )
            return resp
        raise last_exc or RuntimeError(f"{method} {path} failed")


def parse_env() -> dict[str, str]:
    email = (
        os.getenv("E2E_USER_EMAIL")
        or os.getenv("BOOTSTRAP_ADMIN_EMAIL")
        or ""
    ).strip()
    password = (
        os.getenv("E2E_USER_PASSWORD")
        or os.getenv("BOOTSTRAP_ADMIN_PASSWORD")
        or ""
    ).strip()
    base = (
        os.getenv("E2E_BASE_URL")
        or os.getenv("C7X_BASE_URL")
        or os.getenv("HELIX_BASE_URL")
        or "https://c7xai.in"
    ).rstrip("/")
    return {"email": email, "password": password, "base": base}


def riu_say(cli: Client, slug: str, text: str) -> dict:
    cli.log.agent("riu", "user_message", text=text[:240])
    resp = cli.req(
        "riu",
        "POST",
        f"/api/t/{slug}/riu/message",
        json_body={"message": text},
    )
    data = resp.json()
    cli.log.system(
        "riu",
        "assistant_reply",
        phase=data.get("phase"),
        progress=data.get("progress"),
        last_job_id=data.get("last_job_id"),
        last_synth_job_id=data.get("last_synth_job_id"),
        action_results=data.get("action_results"),
        reply=(data.get("latest_reply") or "")[:500],
    )
    return data


def wait_job(cli: Client, slug: str, job_id: str, *, timeout: int) -> dict:
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        resp = cli.req("job_poll", "GET", f"/api/t/{slug}/jobs/{job_id}", expect=200)
        last = resp.json()
        st = last.get("status")
        cli.log.system(
            "job_poll",
            f"job {job_id} is {st}",
            batches_done=last.get("batches_completed"),
            total=last.get("total_batches"),
            error=last.get("error"),
        )
        if st in {"completed", "failed", "cancelled", "paused"}:
            return last
        time.sleep(8)
    raise TimeoutError(f"job {job_id} still {last.get('status')} after {timeout}s")


def wait_counts(
    cli: Client,
    slug: str,
    *,
    gold_min: int,
    synth_min: int,
    timeout: int,
) -> dict:
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        resp = cli.req("library", "GET", f"/api/t/{slug}/library/stats")
        last = resp.json()
        gold = int(last.get("gold_count") or last.get("gold") or 0)
        synth = int(last.get("synthetic_count") or last.get("synthetic") or 0)
        # stats shape may nest
        if not gold and isinstance(last.get("counts"), dict):
            gold = int(last["counts"].get("gold") or 0)
            synth = int(last["counts"].get("synthetic") or 0)
        cli.log.system("library", "stats", gold=gold, synthetic=synth, raw_keys=list(last)[:20])
        if gold >= gold_min and synth >= synth_min:
            return last
        time.sleep(10)
    return last


def extract_counts(stats: dict) -> tuple[int, int]:
    if isinstance(stats.get("counts"), dict):
        return int(stats["counts"].get("gold") or 0), int(
            stats["counts"].get("synthetic") or 0
        )
    gold = 0
    for k in ("gold_count", "gold", "verified_gold"):
        if stats.get(k) is not None:
            gold = int(stats[k])
            break
    synth = 0
    for k in ("synthetic_count", "synthetic", "synthetics"):
        if stats.get(k) is not None:
            synth = int(stats[k])
            break
    return gold, synth


def pick_smallest(models: list[dict]) -> dict:
    apache_mit = [
        m
        for m in models
        if str(m.get("license") or "") in {"Apache-2.0", "MIT"}
    ]
    pool = apache_mit or models
    return min(pool, key=lambda m: float(m.get("params_b") or 99))


def run(args: argparse.Namespace) -> int:
    creds = parse_env()
    if args.base:
        creds["base"] = args.base.rstrip("/")
    if not creds["email"] or not creds["password"]:
        raise SystemExit(
            "Set E2E_USER_EMAIL / E2E_USER_PASSWORD or BOOTSTRAP_ADMIN_* in .env"
        )

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = ROOT / "data" / "e2e_runs" / stamp
    task_id, task = pick_task(args.task)
    log = DualLog(out)
    (out / "task.json").write_text(
        json.dumps({"id": task_id, **task}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    cli = Client(creds["base"], log)
    log.agent(
        "boot",
        "starting user-copy run",
        base=creds["base"],
        email_set=True,
        task=task_id,
        gold_target=args.gold,
        synth_target=args.synth,
        train=not args.skip_train,
    )

    try:
        health = cli.req("boot", "GET", "/api/health").json()
        log.system("boot", "health", version=health.get("version"), env=health.get("env"))

        login = cli.req(
            "auth",
            "POST",
            "/api/auth/login",
            json_body={"email": creds["email"], "password": creds["password"]},
        ).json()
        cli.token = login["access_token"]
        log.agent("auth", "signed in", is_superadmin=login.get("is_superadmin"))

        me = cli.req("auth", "GET", "/api/auth/me").json()
        tenants = me.get("tenants") or []
        if not tenants and me.get("is_superadmin"):
            tenants = cli.req("auth", "GET", "/api/tenants").json()
        if not tenants:
            log.ux_note("Signed in but no workspace — a real user would be stuck.")
            raise RuntimeError("no workspace")
        slug = tenants[0].get("slug")
        log.agent("auth", "picked workspace", slug=slug, workspace_count=len(tenants))

        cli.req("riu", "POST", f"/api/t/{slug}/riu/session")
        script = [
            task["role"],
            task["domain"],
            f"{task['example_in']}\n{task['example_out']}",
            task["edge_1"],
            task["edge_2"],
            "no corpus — web research only",
            "skip materials",
            (
                f"I only need {args.gold} gold examples, then {args.synth} synthetics. "
                "Use the smallest Apache-2.0 model for a cheap test (SmolLM2 1.7B). "
                "Do not scale to thousands."
            ),
            "start 10" if args.gold <= 10 else "start",
        ]
        last = {}
        for msg in script:
            last = riu_say(cli, slug, msg)
            phase = last.get("phase")
            if phase == "review_seed":
                log.ux_note(
                    "No-source path entered seed review. Agent will say 'looks good' "
                    "on each gold — this is extra UX for a 5-row ask."
                )
                for _ in range(40):
                    last = riu_say(cli, slug, "looks good")
                    if last.get("phase") not in {"review_seed", "proof_review"}:
                        break
            if last.get("last_job_id") and phase in {"running", "confirm"}:
                break

        job_id = last.get("last_job_id")
        if not job_id:
            log.ux_note(
                "Riu still had no last_job_id after 'start 10'. "
                "Sending a plain 'start' once before the pipeline fallback."
            )
            last = riu_say(cli, slug, "start")
            job_id = last.get("last_job_id")
        if not job_id:
            log.ux_note(
                "Riu did not start a mining job after the scripted chat. "
                "Falling back to POST /jobs/pipeline batch_size="
                f"{args.gold}."
            )
            try:
                started = cli.req(
                    "mine",
                    "POST",
                    f"/api/t/{slug}/jobs/pipeline",
                    json_body={
                        "quality_mode": 3,
                        "batch_size": args.gold,
                        "total_batches": 1,
                        "auto_continue": True,
                    },
                ).json()
                job_id = (started.get("job") or {}).get("id")
            except RuntimeError as exc:
                log.ux_note(f"Direct pipeline start failed: {exc}")
                log.ux_note(
                    "System may require 'start 10' exploratory job when there is no corpus."
                )
                started = cli.req(
                    "mine",
                    "POST",
                    f"/api/t/{slug}/jobs/pipeline",
                    json_body={
                        "quality_mode": 3,
                        "batch_size": max(args.gold, 5),
                        "total_batches": 2 if args.gold <= 10 else 1,
                        "auto_continue": True,
                    },
                ).json()
                job_id = (started.get("job") or {}).get("id")

        if job_id:
            log.agent("mine", "waiting for mining job", job_id=job_id)
            job = wait_job(cli, slug, job_id, timeout=args.job_timeout)
            if job.get("status") != "completed":
                log.ux_note(f"Mining ended as {job.get('status')}: {job.get('error')}")

        stats = wait_counts(
            cli, slug, gold_min=args.gold, synth_min=0, timeout=args.job_timeout
        )
        gold_n, synth_n = extract_counts(stats)
        gold_list = cli.req(
            "library", "GET", f"/api/t/{slug}/library/gold?limit=50"
        ).json()
        if isinstance(gold_list, dict):
            gold_n = max(gold_n, int(gold_list.get("total") or 0))
            items = gold_list.get("items") or []
        else:
            items = gold_list
            gold_n = max(gold_n, len(items))
        log.agent("mine", "gold in library", count=gold_n)
        if gold_n < args.gold:
            log.ux_note(
                f"Wanted {args.gold} gold, library has {gold_n}. "
                "Quality gates or job size may have dropped rows."
            )
        if gold_n > args.gold:
            log.ux_note(
                f"Asked for {args.gold} gold but library has {gold_n}. "
                "Exploratory path is 5×2=10 — user cannot ask for exactly 5."
            )

        if gold_n == 0:
            log.ux_note("Zero gold — cannot synthesize or train. Stopping before spend.")
            log.report()
            return 2

        # 20 synthetics: 4 variations × 5 gold (or all gold we have, capped)
        use_golds = min(gold_n, args.gold)
        variations = max(1, (args.synth + use_golds - 1) // use_golds)
        log.agent(
            "synth",
            "request synthetics",
            variations_per_gold=variations,
            max_golds=use_golds,
            hoped=variations * use_golds,
        )
        try:
            syn = cli.req(
                "synth",
                "POST",
                f"/api/t/{slug}/library/synthesize",
                json_body={
                    "variations_per_gold": variations,
                    "max_golds": use_golds,
                    "use_llm": True,
                    "parameters": ["tone", "difficulty", "persona"],
                },
                timeout=240.0,
                retries=2,
            ).json()
            log.system("synth", "sync synthesize returned", **{
                k: syn.get(k) for k in ("ok", "created", "synthesized_count", "error") if k in syn
            })
        except RuntimeError as exc:
            log.ux_note(f"Sync synthesize failed ({exc}); trying async job.")
            started = cli.req(
                "synth",
                "POST",
                f"/api/t/{slug}/jobs/synthesis",
                json_body={
                    "quality_mode": 3,
                    "batch_size": use_golds,
                    "total_batches": 1,
                    "variations_per_gold": variations,
                    "parameters": ["tone", "difficulty", "persona"],
                },
            ).json()
            sid = (started.get("job") or {}).get("id")
            if sid:
                wait_job(cli, slug, sid, timeout=args.job_timeout)

        stats = wait_counts(
            cli, slug, gold_min=1, synth_min=args.synth, timeout=args.job_timeout
        )
        gold_n, synth_n = extract_counts(stats)
        syn_list = cli.req(
            "library", "GET", f"/api/t/{slug}/library/synthetic?limit=50"
        ).json()
        if isinstance(syn_list, dict):
            synth_n = max(synth_n, int(syn_list.get("total") or 0))
        log.agent("synth", "synthetics in library", count=synth_n)
        if synth_n < args.synth:
            log.ux_note(f"Wanted {args.synth} synthetics, have {synth_n}.")

        log.agent("download", "download full library zip")
        zip_resp = cli.req(
            "download",
            "GET",
            f"/api/t/{slug}/library/export-zip?scope=library",
        )
        lib_path = out / "library.zip"
        lib_path.write_bytes(zip_resp.content)
        log.system(
            "download",
            "library zip saved",
            bytes=len(zip_resp.content),
            counts=zip_resp.headers.get("x-helix-pack-counts"),
            empty=zip_resp.headers.get("x-helix-pack-empty"),
        )

        if args.skip_train:
            log.agent("train", "skipped by flag")
            log.report()
            return 0

        models = cli.req(
            "train", "GET", f"/api/t/{slug}/library/double-helix/models"
        ).json()
        catalog = models.get("models") or []
        smallest = pick_smallest(catalog)
        log.agent(
            "train",
            "picked smallest Apache/MIT model",
            id=smallest.get("id"),
            name=smallest.get("name"),
            params_b=smallest.get("params_b"),
            license=smallest.get("license"),
            recommended=smallest.get("recommended"),
            default_id=models.get("default_id"),
        )
        if smallest.get("id") != SMALLEST_MODEL_ID:
            log.ux_note(
                f"Expected {SMALLEST_MODEL_ID} as smallest; got {smallest.get('id')}."
            )
        if smallest.get("recommended"):
            log.ux_note("Smallest model is marked recommended — default should be 7B.")

        started = cli.req(
            "train",
            "POST",
            f"/api/t/{slug}/library/double-helix/train",
            json_body={
                "model_id": smallest.get("id"),
                "confirm": True,
                "include_synthetics": True,
            },
        ).json()
        train_id = (started.get("job") or {}).get("id")
        if not train_id:
            log.ux_note("Train did not return a job id.")
            log.report()
            return 3
        log.agent("train", "waiting for C7X-IO job", job_id=train_id)

        deadline = time.time() + args.train_timeout
        train = {}
        while time.time() < deadline:
            try:
                train = cli.req(
                    "train_poll",
                    "GET",
                    f"/api/t/{slug}/library/double-helix/train/{train_id}",
                    timeout=30.0,
                    retries=2,
                ).json().get("job") or {}
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                log.system(
                    "train_poll",
                    "poll failed; will retry next loop",
                    error=str(exc)[:300],
                )
                time.sleep(15)
                continue
            st = train.get("status")
            log.system(
                "train_poll",
                f"train is {st}",
                progress=train.get("progress") or train.get("progress_message"),
                error=train.get("error"),
            )
            if st in {"completed", "failed", "cancelled"}:
                break
            time.sleep(15)
        if train.get("status") != "completed":
            log.ux_note(
                f"Train ended as {train.get('status')}: {train.get('error')}"
            )
            log.report()
            return 4

        decl = cli.req(
            "train", "GET", f"/api/t/{slug}/library/double-helix/declaration"
        ).json()
        log.system("train", "declaration text", keys=list(decl)[:12])
        acc = cli.req(
            "train",
            "POST",
            f"/api/t/{slug}/library/double-helix/train/{train_id}/accept-declaration",
            json_body={"confirm": True},
        ).json()
        log.system("train", "declaration accepted", ok=acc.get("ok"))

        dl = cli.req(
            "train",
            "GET",
            f"/api/t/{slug}/library/double-helix/train/{train_id}/download",
        )
        model_path = out / "trained_model.zip"
        model_path.write_bytes(dl.content)
        log.system(
            "train",
            "trained zip saved",
            bytes=len(dl.content),
            path=str(model_path),
        )
        log.agent("done", "user-copy path finished", gold=gold_n, synth=synth_n)
        log.report()
        return 0
    except Exception as exc:
        log.ux_note(f"Run aborted: {exc}")
        log.system("error", type(exc).__name__, error=str(exc)[:500])
        log.report()
        raise
    finally:
        cli.close()
        print(f"report {log.out_dir / 'report.md'}", flush=True)
        print(f"jsonl  {log.jsonl}", flush=True)


def main() -> None:
    p = argparse.ArgumentParser(description="C7X real-user copy agent")
    p.add_argument("--base", default="")
    p.add_argument("--gold", type=int, default=5)
    p.add_argument("--synth", type=int, default=20)
    p.add_argument("--skip-train", action="store_true")
    p.add_argument(
        "--task",
        default="",
        help=f"Task domain. One of: {', '.join(TASK_ORDER)}. Default: next unused.",
    )
    p.add_argument("--job-timeout", type=int, default=1800)
    p.add_argument("--train-timeout", type=int, default=5400)
    args = p.parse_args()
    raise SystemExit(run(args))


if __name__ == "__main__":
    main()
