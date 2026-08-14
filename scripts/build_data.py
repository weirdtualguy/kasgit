import csv, json, re
from urllib.parse import urlparse

CATEGORY_MAP = {
    "core protocol": "Core",
    "wallet": "Wallet",
    "explorer": "Explorer",
    "mining/pool": "Mining",
    "sdk/library": "SDK",
    "cli tooling": "CLI",
    "api/indexer": "API",
    "krc20/token standard": "KRC20",
    "documentation": "Docs",
    "infrastructure": "Infra",
    "other (defi)": "DeFi",
    "other (dapp)": "dApp",
    "other (dapp/social)": "dApp",
    "programmability/covenants": "Programmability",
    "covenants": "Programmability",
    "other (toccata programmability)": "Programmability",
    "other": "Other",
}

def map_category(raw):
    key = raw.strip().lower()
    return CATEGORY_MAP.get(key, "Other")

def map_org_type(raw):
    r = raw.strip().lower()
    if r.startswith("official"):
        return "Official"
    if r.startswith("company-affiliated"):
        return "Company"
    if r.startswith("community org"):
        return "Community"
    if r.startswith("independent/company"):
        return "Independent"
    if r.startswith("independent contributor") or r.startswith("independent"):
        return "Independent"
    if r.startswith("uncertain"):
        return "Uncertain"
    return "Independent"

def tier_for(org_type):
    return {"Official": 1, "Company": 1, "Community": 2, "Independent": 3, "Uncertain": 3}.get(org_type, 3)

def map_status(raw):
    r = raw.lower()
    if "archiv" in r:
        return "Archived"
    if "deprecat" in r:
        return "Deprecated"
    if "stale" in r:
        return "Stale"
    if "slowing" in r:
        return "Slowing"
    if "active-ish" in r or "uncertain" in r:
        return "Unconfirmed"
    if r.strip().startswith("active"):
        return "Active"
    return "Unconfirmed"

CONF_ORDER = ["Medium-High", "Low-Medium", "High", "Medium", "Low"]

def map_confidence(raw):
    r = raw
    for token in CONF_ORDER:
        if token.lower() in r.lower():
            return token
    return "Medium"

def is_org_row(name, url):
    parsed = urlparse(url)
    if parsed.netloc != "github.com":
        return False
    parts = [p for p in parsed.path.split("/") if p]
    return len(parts) == 1

def slugify(text):
    s = re.sub(r"[^a-zA-Z0-9]+", "-", text.strip().lower()).strip("-")
    return s

import os
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSV_PATH = os.path.join(BASE, "data", "kaspa_github_ecosystem_inventory.csv")
OUT_PATH = os.path.join(BASE, "assets", "js", "data.js")

rows = []
with open(CSV_PATH, newline="", encoding="utf-8") as f:
    reader = csv.DictReader(f)
    for raw in reader:
        name = raw["name"].strip()
        url = raw["url"].strip()
        org_type_raw = raw["org_type"].strip()
        category_raw = raw["category"].strip()
        description = raw["description"].strip()
        last_activity_raw = raw["last_activity"].strip()
        confidence_raw = raw["confidence"].strip()
        verified_raw = (raw.get("verified") or "").strip().lower()
        verified_at_raw = (raw.get("verified_at") or "").strip()
        verified = verified_raw in ("yes", "true", "1", "y")

        org_type = map_org_type(org_type_raw)
        category = map_category(category_raw)
        status = map_status(last_activity_raw)
        confidence = map_confidence(confidence_raw)
        tier = tier_for(org_type)
        entry_type = "Org" if is_org_row(name, url) else "Repo"

        parsed = urlparse(url)
        path_parts = [p for p in parsed.path.split("/") if p]
        if parsed.netloc == "github.com" and len(path_parts) >= 2:
            repo_path = f"{path_parts[0]}/{path_parts[1]}"
        elif parsed.netloc == "github.com" and len(path_parts) == 1:
            repo_path = path_parts[0]
        else:
            repo_path = name

        tags = [org_type.lower(), category.lower()]

        rows.append({
            "id": slugify(name),
            "name": name,
            "repoPath": repo_path,
            "url": url,
            "description": description,
            "category": category,
            "rawCategory": category_raw,
            "orgType": org_type,
            "rawOrgType": org_type_raw,
            "tier": tier,
            "status": status,
            "rawStatus": last_activity_raw,
            "confidence": confidence,
            "rawConfidence": confidence_raw,
            "type": entry_type,
            "tags": tags,
            "verified": verified,
            "verifiedAt": verified_at_raw or None,
        })

# Ensure the primary kaspanet org itself is represented in the registry
if not any(r["repoPath"] == "kaspanet" and r["type"] == "Org" for r in rows):
    rows.insert(0, {
        "id": "kaspanet",
        "name": "kaspanet",
        "repoPath": "kaspanet",
        "url": "https://github.com/kaspanet",
        "description": "Primary Kaspa GitHub organization.",
        "category": "Core",
        "rawCategory": "core protocol",
        "orgType": "Official",
        "rawOrgType": "official (kaspanet org)",
        "tier": 1,
        "status": "Active",
        "rawStatus": "Active — primary organization",
        "confidence": "High",
        "rawConfidence": "High",
        "type": "Org",
        "tags": ["official", "core"],
        "verified": False,
        "verifiedAt": None,
    })

categories_present = sorted(set(r["category"] for r in rows))
statuses_present = sorted(set(r["status"] for r in rows))

with open(OUT_PATH, "w", encoding="utf-8") as f:
    f.write("// Auto-generated from data/kaspa_github_ecosystem_inventory.csv\n")
    f.write("// Source: AI-assisted research inventory of the public Kaspa GitHub ecosystem.\n")
    f.write("// Regenerate with: python3 scripts/build_data.py\n\n")
    f.write("const REGISTRY_ROWS = ")
    f.write(json.dumps(rows, indent=2, ensure_ascii=False))
    f.write(";\n\n")
    f.write("const REPOS = REGISTRY_ROWS.filter(row => row.type === \"Repo\");\n\n")
    f.write("const REGISTRY_STATUSES = ")
    f.write(json.dumps(statuses_present, ensure_ascii=False))
    f.write(";\n\n")
    f.write("const REGISTRY_CATEGORIES = ")
    f.write(json.dumps(categories_present, ensure_ascii=False))
    f.write(";\n")

print("rows:", len(rows))
print("categories:", categories_present)
print("statuses:", statuses_present)
