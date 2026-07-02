"""Alert checker — lê emails do Gmail via Composio a cada 2h."""
import asyncio
import os
import re
import logging
from datetime import datetime

logger = logging.getLogger("alert_checker")

COMPOSIO_API_KEY = os.getenv("COMPOSIO_API_KEY", "")
ALERTA_INTERVAL = int(os.getenv("ALERTA_INTERVAL_HORAS", "2"))

try:
    from composio import Composio
    from composio.client.enums import Action
    HAS_COMPOSIO = True
except ImportError:
    HAS_COMPOSIO = False
    print("⚠️ composio-core not installed")

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
        logger.info("✅ alertas table ready")
    except Exception as e:
        logger.warning(f"⚠️ Could not create alertas table: {e}")


def parse_alert_email(subject: str, body: str) -> list[dict]:
    """Extrai alertas do assunto/corpo do email.

    Formatos esperados:
      - Assunto: "Alerta: GRU→JFK ≤ 30000 mi - AAdvantage"
      - Corpo: "Origem: GRU | Destino: JFK | Milhas: 30000 | Programa: AAdvantage"
    """
    alerts = []
    text = f"{subject} {body}"

    # Regex: captura rotas como GRU→JFK, GIG→MIA
    route_match = re.search(
        r'(?P<orig>[A-Z]{3})\s*[→➡>-]\s*(?P<dest>[A-Z]{3})', text
    )
    if not route_match:
        return alerts

    orig = route_match.group("orig")
    dest = route_match.group("dest")

    # Milhas máxima
    miles_match = re.search(r'(?:≤|<=|ate|até|max[.:]?)\s*(\d[\d.]*)\s*(?:mi|milhas)', text, re.I)
    miles = int(re.sub(r'[^0-9]', '', miles_match.group(1))) if miles_match else None

    # Programa
    prog_match = re.search(r'(?:programa|prog)[:\s]+([A-Za-zÀ-ÿ&.\s]+?)(?:\d|$|\n|\(|$)', text, re.I)
    prog = prog_match.group(1).strip() if prog_match else None

    # Classe
    cls_match = re.search(r'(?:classe|class|cabin)[:\s]+(\w+)', text, re.I)
    cls = cls_match.group(1).lower() if cls_match else "economy"

    # Data da viagem
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
                if len(parts[2]) == 4:
                    data_viagem = datetime.strptime(raw, "%d/%m/%Y").date()
                else:
                    data_viagem = datetime.strptime(raw, "%d/%m/%y").date()
        except ValueError:
            pass

    alerts.append({
        "origem": orig,
        "destino": dest,
        "milhas_max": miles,
        "programa": prog,
        "classe": cls,
        "data_viagem": str(data_viagem) if data_viagem else None,
        "email_origem": "alert@seats.aero",
    })
    return alerts


async def check_alerts(redis_client, engine):
    """Main function: fetch unread Gmail emails, parse, store."""
    if not HAS_COMPOSIO or not COMPOSIO_API_KEY:
        print("ℹ️ Composio not configured — skipping alert check")
        return

    try:
        composio_client = Composio(api_key=COMPOSIO_API_KEY)

        print("📧 Checking Gmail for alert emails...")
        result = composio_client.actions.execute(
            action=Action("GMAIL_FETCH_EMAILS"),
            params={
                "user_id": "me",
                "max_results": 10,
            },
            entity_id="default",
        )

        messages = result.get("data", {}).get("messages", [])
        if not messages:
            print("ℹ️ No new emails found")
            return

        print(f"📧 Found {len(messages)} email(s) to process")
        from sqlalchemy import text
        new_alerts = 0
        for msg in messages:
            subject = msg.get("subject", "") or ""
            body = msg.get("body_plain", "") or msg.get("snippet", "") or ""
            msg_id = msg.get("id", "")

            print(f"  → Subject: {subject[:80]}")
            alerts = parse_alert_email(subject, body)
            if not alerts:
                print(f"     ⏭️  No alert data found in this email")
                continue

            # Check if already processed
            with engine.connect() as conn:
                existing = conn.execute(
                    text("SELECT id FROM alertas WHERE email_origem = :e AND criado_em > NOW() - INTERVAL '24 hours' LIMIT 1"),
                    {"e": f"gmail_{msg_id}"}
                ).fetchone()

            if existing:
                print(f"     ⏭️  Already processed")
                continue

            for alert in alerts:
                with engine.begin() as conn:
                    conn.execute(
                        text("""
                            INSERT INTO alertas (origem, destino, milhas_max, programa, classe, data_viagem, email_origem)
                            VALUES (:o, :d, :m, :p, :c, :dv, :e)
                        """),
                        {
                            "o": alert["origem"],
                            "d": alert["destino"],
                            "m": alert["milhas_max"],
                            "p": alert["programa"],
                            "c": alert["classe"],
                            "dv": alert["data_viagem"],
                            "e": f"gmail_{msg_id}",
                        }
                    )
                new_alerts += 1
                print(f"     ✅ Saved: {alert['origem']}→{alert['destino']} {alert['milhas_max']}mi")

            # Mark as read
            try:
                composio_client.actions.execute(
                    action=Action("GMAIL_MODIFY_MESSAGE"),
                    params={
                        "user_id": "me",
                        "id": msg_id,
                        "remove_labels": ["UNREAD"],
                    },
                    entity_id="default",
                )
                print(f"     📭 Marked as read")
            except Exception as e:
                print(f"     ⚠️ Could not mark as read: {e}")

        print(f"✅ Alert check complete — {new_alerts} new alerts stored")

    except Exception as e:
        print(f"⚠️ Alert check failed: {e}")


async def alert_loop(redis_client, engine):
    """Background loop — runs check_alerts every ALERTA_INTERVAL hours."""
    while True:
        await check_alerts(redis_client, engine)
        await asyncio.sleep(ALERTA_INTERVAL * 3600)


def start_alert_checker(app, redis_client, engine):
    """Start the background alert checker task on app startup."""
    @app.on_event("startup")
    async def _start_checker():
        await init_alertas_table(engine)
        asyncio.create_task(alert_loop(redis_client, engine))
