import json, os, re, threading, time, traceback
from collections import defaultdict
from datetime import date, datetime
from urllib.parse import urlencode, unquote
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

app = FastAPI(title="Acme Appliances Ledger Agent")
IST = ZoneInfo("Asia/Kolkata")
CACHE = {"at": 0, "orders": None, "rates": None}
LOCK = threading.Lock()

class Question(BaseModel):
    question: str = Field(min_length=1, max_length=2000)

def request_path(path):
    sign = os.environ.get("LEDGER_QUIZ_SIGN")
    if not sign:
        raise RuntimeError("Set LEDGER_QUIZ_SIGN in deployment environment variables")
    query = urlencode({"email": os.environ.get("LEDGER_EMAIL", "23f2001912@ds.study.iitm.ac.in"), "quizSign": unquote(sign), "questionId": "q-ledger-agent-server", "path": path})
    url = "https://exam.sanand.workers.dev/questionData?" + query
    with urlopen(Request(url, headers={"User-Agent": "AcmeLedgerAgent/1.0"}), timeout=9) as resp:
        raw = resp.read().decode("utf-8")
    try:
        obj = json.loads(raw)
        if isinstance(obj, list) and obj and all(isinstance(n, int) and 0 <= n <= 255 for n in obj):
            return bytes(obj).decode("utf-8")
        if isinstance(obj, str):
            return obj
    except (json.JSONDecodeError, ValueError):
        pass
    if raw.strip() and re.fullmatch(r"(?:\s*\d{1,3})+\s*", raw):
        try:
            return bytes(int(n) for n in raw.split()).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            pass
    return raw

def timestamp(value):
    try:
        d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=IST)
    except (TypeError, ValueError):
        return datetime.min.replace(tzinfo=IST)

def parse_orders(raw):
    rows = []
    for line in raw.splitlines():
        try:
            obj = json.loads(line.strip())
            if isinstance(obj, dict) and "id" in obj: rows.append(obj)
            elif isinstance(obj, list): rows.extend(x for x in obj if isinstance(x, dict) and "id" in x)
        except (json.JSONDecodeError, ValueError):
            continue
    if not rows:
        obj = json.loads(raw)
        if isinstance(obj, list): rows = [x for x in obj if isinstance(x, dict) and "id" in x]
        elif isinstance(obj, dict): rows = obj.get("orders", [])
    latest = {}
    for row in rows:
        oid = str(row.get("id", ""))
        if oid not in latest or timestamp(row.get("updated_at")) >= timestamp(latest[oid].get("updated_at")):
            latest[oid] = row
    if not latest: raise RuntimeError("Order export empty or unparseable")
    return list(latest.values())

def load_data():
    if CACHE["orders"] is not None and time.time() - CACHE["at"] < 300:
        return CACHE["orders"], CACHE["rates"]
    with LOCK:
        if CACHE["orders"] is not None and time.time() - CACHE["at"] < 300:
            return CACHE["orders"], CACHE["rates"]
        orders = parse_orders(request_path("/export"))
        rates = json.loads(request_path("/rates")).get("usd_per_unit", {})
        if not rates: raise RuntimeError("Exchange rates missing")
        CACHE.update(at=time.time(), orders=orders, rates=rates)
        return orders, rates

def usd(row, rates):
    return float(row.get("amount", 0) or 0) * float(rates.get(str(row.get("currency", "USD")).upper(), 0) or 0)

def cash(value): return round(value + 1e-9, 2)

MONTHS = {"january":1,"jan":1,"february":2,"feb":2,"march":3,"mar":3,"april":4,"apr":4,"may":5,"june":6,"jun":6,"july":7,"jul":7,"august":8,"aug":8,"september":9,"sep":9,"sept":9,"october":10,"oct":10,"november":11,"nov":11,"december":12,"dec":12}

def date_window(q):
    for name, month in MONTHS.items():
        m = re.search(r"\b" + re.escape(name) + r"\s+(20\d{2})\b", q)
        if m:
            y = int(m.group(1)); return date(y, month, 1), date(y + (month == 12), month % 12 + 1, 1)
    m = re.search(r"\b(20\d{2})-(0[1-9]|1[0-2])\b", q)
    if m:
        y, mo = map(int, m.groups()); return date(y, mo, 1), date(y + (mo == 12), mo % 12 + 1, 1)
    return None

def select_orders(q, orders):
    rows = list(orders)
    regions = sorted({str(x.get("region","")) for x in rows if x.get("region")}, key=len, reverse=True)
    products = sorted({str(x.get("product","")) for x in rows if x.get("product")}, key=len, reverse=True)
    customers = sorted({str(x.get("customer","")) for x in rows if x.get("customer")}, key=len, reverse=True)
    region = next((x for x in regions if re.search(r"\b"+re.escape(x.lower())+r"\b", q)), None)
    product = next((x for x in products if x.lower() in q), None)
    customer = next((x for x in customers if re.search(r"\b"+re.escape(x.lower())+r"\b", q)), None)
    if region: rows = [x for x in rows if str(x.get("region","")).lower() == region.lower()]
    if product: rows = [x for x in rows if str(x.get("product","")).lower() == product.lower()]
    if customer: rows = [x for x in rows if str(x.get("customer","")).lower() == customer.lower()]
    window = date_window(q)
    if window:
        start, end = window
        rows = [x for x in rows if start <= timestamp(x.get("created_at")).astimezone(IST).date() < end]
    days = re.findall(r"\b20\d{2}-\d{2}-\d{2}\b", q)
    if len(days) >= 2:
        try:
            start, end = map(date.fromisoformat, days[:2])
            rows = [x for x in rows if start <= timestamp(x.get("created_at")).astimezone(IST).date() <= end]
        except ValueError: pass
    return rows

def answer_question(original):
    q = original.lower().strip()
    orders, rates = load_data()
    rows = select_orders(q, orders)
    if not rows: return "No matching orders were found."
    if any(w in q for w in ("refund","refunded","refunds")):
        matches = [x for x in rows if str(x.get("status","")).lower() == "refunded"]
        if any(w in q for w in ("how many","count","number of")): return str(len(matches))
        return f"Total refunds were ${cash(sum(usd(x,rates) for x in matches)):.2f} USD across {len(matches)} refunded orders."
    if any(w in q for w in ("void","voided","cancelled","canceled")):
        matches = [x for x in rows if str(x.get("status","")).lower() in ("void","voided","cancelled","canceled")]
        if any(w in q for w in ("how many","count","number of")): return str(len(matches))
        return f"There were {len(matches)} void or cancelled orders totaling ${cash(sum(usd(x,rates) for x in matches)):.2f} USD."
    paid = [x for x in rows if str(x.get("status","")).lower() == "paid"]
    if any(w in q for w in ("how many","count","number of orders","number of transactions")): return str(len(paid))
    if any(w in q for w in ("quantity","units sold","how many items","number of units")): return str(sum(int(x.get("qty",0) or 0) for x in paid))
    field = "product" if any(w in q for w in ("by product","product breakdown","which product","best-selling","best selling")) else ("customer" if any(w in q for w in ("by customer","customer breakdown","which customer")) else ("region" if any(w in q for w in ("by region","region breakdown","which region")) else None))
    ranking = any(w in q for w in ("most","top","highest","best-selling","best selling","least","lowest"))
    if field or ranking:
        field = field or ("product" if "product" in q or "item" in q else ("customer" if "customer" in q else "region"))
        totals = defaultdict(float)
        for x in paid: totals[str(x.get(field,"Unknown"))] += usd(x,rates)
        if totals:
            low = any(w in q for w in ("least","lowest","fewest"))
            name = (min if low else max)(totals, key=totals.get)
            return f"{name} had the {'lowest' if low else 'highest'} paid revenue at ${cash(totals[name]):.2f} USD."
        return "No paid orders matched those filters."
    total = sum(usd(x,rates) for x in paid)
    if any(w in q for w in ("average","avg")):
        return f"${cash(total/len(paid)):.2f} USD average per paid order." if paid else "No paid orders matched those filters."
    if any(w in q for w in ("revenue","sales","sold","income","total amount","total value","how much","total")):
        return f"${cash(total):.2f} USD"
    return f"Found {len(rows)} matching orders. Ask about revenue, refunds, order count, quantity, or product/customer/region."

@app.get("/")
def root(): return {"service":"Acme Appliances order ledger","status":"ok","endpoint":"POST /answer"}

@app.get("/health")
def health(): return {"ok":True}

@app.post("/answer")
@app.post("/api/index")
def answer(body: Question):
    try: return {"answer":answer_question(body.question)}
    except Exception as exc:
        print("LEDGER_ANSWER_FAILURE:", repr(exc))
        traceback.print_exc()
        raise HTTPException(status_code=503, detail="Ledger data unavailable: "+type(exc).__name__)
