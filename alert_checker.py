"""Alert checker — lê emails do Gmail via Composio."""
import asyncio
import os
import re
from datetime import datetime

USER_ID = os.getenv("GMAIL_ENTITY", "pg-test-0b63b0ba-9503-4607-8921-3f03394aa1ed")
ALERTA_INTERVAL = int(os.getenv("ALERTA_INTERVAL_HORAS", "2"))

HAS_COMPOSIO = False
try:
    from composio import Composio
    HAS_COMPOSIO = bool(os.getenv("COMPOSIO_API_KEY", ""))
except ImportError:
    print("⚠️ composio not installed")

SQL_CREATE_ALERTAS = """
CREATE TABLE IF NOT EXISTS alertas (
    id SERIAL PRIMARY KEY,
    usuario_id INTEGER REFERENCES usuarios(id) ON DELETE CASCADE,
    origem VARCHAR(3),
    destino VARCHAR(3),
    milhas_max INTEGER,
    programa VARCHAR(100),
    classe VARCHAR(20),
    email_origem VARCHAR(255),
    data_viagem DATE,
    criado_em TIMESTAMPTZ DEFAULT NOW(),
    processado BOOLEAN DEFAULT FALSE
);
"""


async def init_alertas_table(engine):
    """Ensure alertas table exists."""
    try:
        from sqlalchemy import text
        with engine.begin() as conn:
            conn.execute(text(SQL_CREATE_ALERTAS))
        print("✅ alertas table ready")
    except Exception as e:
        print(f"⚠️ Could not create alertas table: {e}")


def parse_alert_email(subject: str, body: str) -> list[dict]:
    """Extrai alertas do assunto/corpo do email."""
    alerts = []
    text = f"{subject} {body}"
    route_match = re.search(
        r'(?:(?:from\s+)?(?P<orig>[A-Z]{3})\s*(?:[→➡>\-]|\s+to\s+)\s*(?P<dest>[A-Z]{3}))', text, re.I
    )
    if not route_match:
        return alerts
    orig = route_match.group("orig")
    dest = route_match.group("dest")
    miles_match = re.search(
        r'(?:≤|<=|ate|até|max[.:]?)\s*(\d[\d.]*)\s*(?:mi|milhas)', text, re.I
    )
    miles = int(re.sub(r'[^0-9]', '', miles_match.group(1))) if miles_match else None
    prog_match = re.search(
        r'(?:programa|prog)[:\s]+([A-Za-zÀ-ÿ&.\s]+?)(?:\d|$|\n|\(|$)', text, re.I
    )
    prog = prog_match.group(1).strip() if prog_match else None
    cls_match = re.search(r'(?:classe|class|cabin)[:\s]+(\w+)', text, re.I)
    cls = cls_match.group(1).lower() if cls_match else "economy"
    date_match = re.search(
        r'(\d{2}[/-]\d{2}[/-]\d{2,4}|\d{4}-\d{2}-\d{2})', text
    )
    data_viagem = None
    if date_match:
        try:
            raw = date_match.group(1)
            if "-" in raw:
                data_viagem = datetime.strptime(raw, "%Y-%m-%d").date()
            elif "/" in raw:
                parts = raw.split("/")
                y = parts[2]
                data_viagem = datetime.strptime(raw, "%d/%m/%Y" if len(y) == 4 else "%d/%m/%y").date()
        except ValueError:
            pass
    alerts.append({
        "origem": orig, "destino": dest, "milhas_max": miles,
        "programa": prog, "classe": cls,
        "data_viagem": str(data_viagem) if data_viagem else None,
        "email_origem": "alert@seats.aero",
    })
    return alerts


async def check_alerts(redis_client, engine):
    """Fetch unread Gmail emails, parse, store."""
    if not HAS_COMPOSIO:
        print("ℹ️ Composio not configured — skipping alert check")
        return

    try:
        composio = Composio()
        print("📧 Checking Gmail for alert emails...")

        # Busca emails não lidos — execute direto, sem create()
        result = composio.tools.execute(
            slug="GMAIL_FETCH_EMAILS",
            arguments={"user_id": "me", "max_results": 10},
            user_id=USER_ID,
            dangerously_skip_version_check=True,
        )

        if isinstance(result, dict):
            messages = result.get("data", {}).get("messages", [])
        else:
            print(f"⚠️ Unexpected response type: {type(result)}")
            return

        if not messages:
            print("ℹ️ No new emails found")
            return

        print(f"📧 Found {len(messages)} email(s) to process")
        from sqlalchemy import text
        new_alerts = 0

        for msg in messages:
            subject = msg.get("subject", "") or ""
            body = msg.get("messageText", "") or msg.get("snippet", "") or ""
            msg_id = msg.get("messageId", "") or msg.get("id", "")
            print(f"  → Subject: {subject[:80]}")

            alerts = parse_alert_email(subject, body)
            if not alerts:
                print("     ⏭️  No alert data found")
                continue

            # Check if already processed (last 24h)
            with engine.connect() as conn:
                existing = conn.execute(
                    text("SELECT id FROM alertas WHERE email_origem = :e AND criado_em > NOW() - INTERVAL '24 hours' LIMIT 1"),
                    {"e": f"gmail_{msg_id}"}
                ).fetchone()

            if existing:
                print("     ⏭️  Already processed")
                continue

            for alert in alerts:
                with engine.begin() as conn:
                    conn.execute(
                        text("""INSERT INTO alertas (origem, destino, milhas_max, programa, classe, data_viagem, email_origem)
                                VALUES (:o, :d, :m, :p, :c, :dv, :e)"""),
                        {"o": alert["origem"], "d": alert["destino"],
                         "m": alert["milhas_max"], "p": alert["programa"],
                         "c": alert["classe"], "dv": alert["data_viagem"],
                         "e": f"gmail_{msg_id}"}
                    )
                new_alerts += 1
                print(f"     ✅ Saved: {alert['origem']}→{alert['destino']} {alert['milhas_max']}mi")

            # Mark as read
            try:
                composio.tools.execute(
                    slug="GMAIL_MODIFY_MESSAGE",
                    arguments={"user_id": "me", "id": msg_id, "remove_labels": ["UNREAD"]},
                    user_id=USER_ID,
                    dangerously_skip_version_check=True,
                )
                print("     📭 Marked as read")
            except Exception as e:
                print(f"     ⚠️ Could not mark as read: {e}")

        print(f"✅ Alert check complete — {new_alerts} new alerts stored")

    except Exception as e:
        print(f"⚠️ Alert check failed: {e}")


async def alert_loop(redis_client, engine):
    """Background loop every ALERTA_INTERVAL hours."""
    while True:
        await check_alerts(redis_client, engine)
        await asyncio.sleep(ALERTA_INTERVAL * 3600)
