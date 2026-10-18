# Acme Appliances Ledger Agent

FastAPI service for questions about the assigned order ledger.

POST /answer with JSON {"question":"What was the total revenue in USD from the North region in March 2026?"}. Returns {"answer":"..."}. GET /health checks the function.

Vercel deployment: import this repository, set LEDGER_QUIZ_SIGN to the private quizSign value from your assigned questionData URL, set LEDGER_EMAIL to your registered email, and deploy. The function fetches the assigned export and rates, caches them briefly, deduplicates by latest updated_at, and counts only paid orders as revenue.
