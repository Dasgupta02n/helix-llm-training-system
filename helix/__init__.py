"""C7X — gold-data studio (c7xai.in).

Layout
------
helix/api/          HTTP app, auth, jobs, library, Riu, studio
helix/services/     domain logic (cost, Riu, train, gather, library)
                    Riu: riu.py facade → riu_session / riu_estimate / riu_actions
                    + riu_seed_review
helix/web/          public HTML + studio CSS
                    studio JS: static/js/{core,auth,account,jobs,library,riu,home,boot}.js
helix/db/           models, migrate, session
helix/agents/       judge-loop catalog + runner
helix/packaging/    PEFT load script shipped in trained zips

User path
---------
public site → /app signup (admin gate) → Riu setup → mine/synth jobs
→ My data (gold + synthetics, stored apart) → download and/or C7X-IO train
Usage counter = 2 × billed service spend (model + gather + compute + other).
"""

__version__ = "2.0.12"
