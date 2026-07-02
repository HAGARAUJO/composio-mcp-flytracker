#!/usr/bin/env python3
"""Award fare data provider.
Fluxo: PostgreSQL (award_cache) → JSON (fallback) → Seed (ultimo caso)
Cache em Redis (300s TTL) para performance.
"""
import json, logging, os
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# ── PostgreSQL connection ─────────────────────────────────────────
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://auth_api:auth_api_2026_seguro@postgres:5432/buscador_passagens"
)

# Path to JSON fallback (generated from CSVs)
JSON_PATH = os.path.join(os.path.dirname(__file__), "award_data.json")

# ── Program name mapping ──────────────────────────────────────────
PROG_NAME_MAP = {
    "smiles": "Smiles",
    "tudoazul": "TudoAzul",
    "latam pass": "LATAM Pass",
    "aadvantage": "AAdvantage",
    "delta": "Delta SkyMiles",
    "united": "United MileagePlus",
    "flyingblue": "Flying Blue",
    "flying blue": "Flying Blue",
    "ba avios": "BA Avios",
    "iberia plus": "Iberia Plus",
    "tap miles&go": "TAP Miles&Go",
    "alaska": "Alaska",
    "aeroplan": "Aeroplan",
    "virginatlantic": "Virgin Atlantic",
    "virgin atlantic": "Virgin Atlantic",
    "emirates": "Emirates",
    "qatar": "Qatar",
    "etihad": "Etihad",
    "lufthansa": "Lufthansa",
    "jetblue": "JetBlue",
    "american": "AAdvantage",
    "velocity": "Velocity",
    "aeromexico": "Aeromexico",
}

def _normalize(prog: Optional[str]) -> str:
    if not prog: return "Unknown"
    return PROG_NAME_MAP.get(prog.strip().lower(), prog.strip())

def _availability(seats=None, miles=None) -> str:
    seats = seats or 0
    miles = miles or 0
    if seats >= 8: return "high"
    if seats >= 3: return "medium"
    if seats > 0: return "low"
    if miles < 80000: return "high"
    if miles < 200000: return "medium"
    return "low"

# ── Seed fallback ─────────────────────────────────────────────────
SEED_AWARD = {
    "GRU-JFK": {
        "economy": [{"program": "AAdvantage", "miles": 22000, "direct_miles": None, "seats": 6, "carriers": "AA", "taxes": 31, "availability": "high", "date": "2026-11-11", "source": "seed"}],
        "business": [{"program": "AAdvantage", "miles": 120000, "direct_miles": 120000, "seats": 4, "carriers": "AA", "taxes": 280, "availability": "medium", "date": "2026-11-11", "source": "seed"}],
    },
    "GRU-MIA": {
        "economy": [{"program": "AAdvantage", "miles": 25000, "direct_miles": None, "seats": 4, "carriers": "AA", "taxes": 51, "availability": "medium", "date": "2026-11-11", "source": "seed"}],
        "business": [{"program": "AAdvantage", "miles": 171500, "direct_miles": 171500, "seats": 2, "carriers": "AA", "taxes": 336, "availability": "low", "date": "2026-11-11", "source": "seed"}],
    },
}

SEED_COM = {}
SEED_DEALS = []

# ── Infra: PG connection pool (lazy, optional) ────────────────────
_pg_pool = None

async def _get_pool():
    global _pg_pool
    if _pg_pool is None:
        try:
            import asyncpg
            _pg_pool = await asyncpg.create_pool(DATABASE_URL.replace("+pg8000", ""), min_size=1, max_size=3, timeout=5)
        except Exception as e:
            logger.warning(f"PG pool failed: {e}")
            _pg_pool = None
    return _pg_pool

async def close_pg_pool():
    global _pg_pool
    if _pg_pool:
        await _pg_pool.close()
        _pg_pool = None

# ── Load JSON fallback ────────────────────────────────────────────
_json_cache = None

def _load_json():
    global _json_cache
    if _json_cache is not None:
        return _json_cache
    try:
        with open(JSON_PATH) as f:
            _json_cache = json.load(f)
        logger.info(f"Loaded award_data.json ({len(_json_cache.get('award', {}))} routes)")
        return _json_cache
    except Exception as e:
        logger.warning(f"JSON fallback failed: {e}")
        return None

# ── Query PostgreSQL ──────────────────────────────────────────────
CABINS = [
    ("economia", False, "economy"),
    ("premium", False, "premium"),
    ("business", True, "business"),
    ("primeira", False, "first_class"),
]

def _row_to_entry(row: dict, prefix: str, has_dir: bool) -> Optional[dict]:
    miles = row.get(f"{prefix}_milhas")
    if not miles or miles == 0:
        return None
    seats = row.get(f"{prefix}_assentos") or 0
    return {
        "program": _normalize(row.get("programa")),
        "miles": miles,
        "direct_miles": row.get(f"{prefix}_milhas_direto") if has_dir else None,
        "seats": seats,
        "carriers": row.get(f"{prefix}_cia") or "",
        "taxes": round(float(row.get(f"{prefix}_taxas") or 0), 2),
        "availability": _availability(seats, miles),
        "date": str(row.get("data_voo") or ""),
        "source": row.get("fonte") or "award_cache",
    }

async def _query_pg() -> Optional[dict]:
    pool = await _get_pool()
    if not pool:
        return None
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT data_voo, programa, origem, destino, fonte,
                       economia_milhas, economia_assentos, economia_cia, economia_taxas,
                       premium_milhas, premium_assentos, premium_cia, premium_taxas,
                       business_milhas, business_milhas_direto, business_assentos,
                       business_assentos_direto, business_cia, business_taxas,
                       primeira_milhas, primeira_assentos, primeira_cia, primeira_taxas
                FROM award_cache
                ORDER BY origem, destino, programa, data_voo DESC
            """)
    except Exception as e:
        logger.warning(f"PG query failed: {e}")
        return None

    from collections import defaultdict
    award: dict = defaultdict(lambda: defaultdict(list))
    for r in rows:
        row = dict(r)
        route = f"{row['origem']}-{row['destino']}"
        for prefix, has_dir, cabin_key in CABINS:
            entry = _row_to_entry(row, prefix, has_dir)
            if entry:
                existing = {e["program"] for e in award[route][cabin_key]}
                if entry["program"] not in existing:
                    award[route][cabin_key].append(entry)

    # Ordena por milhas
    result = {}
    for route, cabins in award.items():
        result[route] = {}
        for ck in ["economy", "premium", "business", "first_class"]:
            entries = cabins.get(ck, [])
            entries.sort(key=lambda x: x["miles"] or 999999999)
            result[route][ck] = entries
    return result

# ── Public API ────────────────────────────────────────────────────

async def fetch_award_data(redis_client=None) -> dict:
    """Busca dados de award: Redis → PG → JSON → Seed."""
    cache_key = "award_data_cache"

    # 1. Redis cache
    if redis_client:
        try:
            cached = await redis_client.get(cache_key)
            if cached:
                logger.info("From Redis cache")
                return json.loads(cached)
        except Exception:
            pass

    # 2. PostgreSQL award_cache
    award_data = None
    try:
        award_data = await _query_pg()
    except Exception as e:
        logger.warning(f"PG query error: {e}")

    # 3. JSON fallback (from CSVs)
    source = "seed"
    commercial = SEED_COM
    deals = SEED_DEALS
    if award_data:
        source = "award_cache"
        logger.info(f"From PG: {len(award_data)} routes")
    else:
        json_data = _load_json()
        if json_data and json_data.get("award"):
            award_data_raw = json_data["award"]
            commercial = json_data.get("commercial", {})
            deals = json_data.get("deals", [])
            source = "seats_aero_csv"
            # Normalizar pro formato do frontend
            award_data = {}
            for route, cabins in award_data_raw.items():
                award_data[route] = {}
                for ck in ["economy", "premium_economy", "business", "first_class"]:
                    entries = cabins.get(ck, [])
                    normalized = []
                    for e in entries:
                        normalized.append({
                            "program": _normalize(e.get("program", e.get("programa", ""))),
                            "miles": e.get("miles", e.get("milhas", 0)),
                            "direct_miles": e.get("direct_miles", e.get("milhas_direto")),
                            "seats": e.get("seats", e.get("assentos", 0)),
                            "carriers": e.get("carriers", e.get("cia", "")),
                            "taxes": e.get("taxes", e.get("taxas", 0)),
                            "availability": _availability(
                                e.get("seats", e.get("assentos", 0)),
                                e.get("miles", e.get("milhas", 0))
                            ),
                            "date": e.get("date", e.get("data_voo", "")),
                            "source": e.get("source", e.get("fonte", "")),
                        })
                    normalized.sort(key=lambda x: x["miles"] or 999999999)
                    award_data[route][ck] = normalized
            logger.info(f"From JSON: {len(award_data)} routes")
        else:
            award_data = SEED_AWARD
            logger.warning("Using seed data")

    data = {
        "award": award_data,
        "commercial": commercial,
        "deals": deals,
        "source": source,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    # Cache in Redis (300s)
    if redis_client:
        try:
            await redis_client.setex(cache_key, 300, json.dumps(data, default=str))
        except Exception as e:
            logger.warning(f"Redis cache error: {e}")

    return data

def format_for_frontend(data: dict, origin: str, dest: str, cabin: str = "economy") -> dict:
    """Formata dados pro frontend."""
    key = f"{origin}-{dest}"
    rev = f"{dest}-{origin}"
    award = data.get("award", {}).get(key) or data.get("award", {}).get(rev)
    result = {"award": None, "deals": data.get("deals", [])}
    if award:
        cab = award.get(cabin) or award.get("economy", [])
        result["award"] = sorted(cab, key=lambda x: x.get("miles", 999999999) or 999999999)
    return result
