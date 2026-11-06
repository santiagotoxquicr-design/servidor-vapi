"""
Servidor ligero Vapi <-> Google Calendar / Google Sheets (reemplazo de N8N + CRM).

Endpoints
---------
POST /vapi/tools      Tool calls de Vapi: agendar_cita, consultar_disponibilidad
POST /vapi/post-call  Evento end-of-call-report -> nueva fila en Google Sheets
GET  /healthz         Health check (útil para Render/Railway y para mantenerlo "despierto")

Reglas de Vapi que este servidor respeta:
  * Siempre responde HTTP 200 a las tool calls (aunque haya error de negocio).
  * Formato: {"results": [{"toolCallId": "...", "result": "<string en UNA línea>"}]}
  * El toolCallId se devuelve tal cual llegó.
"""
import base64
import hmac
import json
import logging
import os
import threading
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from typing import Any, Callable
from zoneinfo import ZoneInfo

import gspread
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("vapi-server")


# --------------------------------------------------------------------------- #
# Configuración (variables de entorno)
# --------------------------------------------------------------------------- #
def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Falta la variable de entorno obligatoria: {name}")
    return value


VAPI_SECRET = require_env("VAPI_SECRET")
CALENDAR_ID = require_env("GOOGLE_CALENDAR_ID")
SHEET_ID = require_env("GOOGLE_SHEET_ID")
SHEET_TAB = os.getenv("GOOGLE_SHEET_TAB", "Leads")

TZ = ZoneInfo(os.getenv("TIMEZONE", "America/Mexico_City"))
BUSINESS_START_HOUR = int(os.getenv("BUSINESS_START_HOUR", "9"))
BUSINESS_END_HOUR = int(os.getenv("BUSINESS_END_HOUR", "18"))
WORKDAYS = {int(d) for d in os.getenv("WORKDAYS", "0,1,2,3,4").split(",")}  # 0=lunes ... 6=domingo
DEFAULT_DURATION_MIN = int(os.getenv("DEFAULT_DURATION_MINUTES", "30"))
SLOT_STEP_MIN = int(os.getenv("SLOT_STEP_MINUTES", "30"))
MIN_NOTICE_MIN = int(os.getenv("MIN_NOTICE_MINUTES", "30"))
MAX_SLOTS_TO_OFFER = 8

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/spreadsheets",
]

DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
         "agosto", "septiembre", "octubre", "noviembre", "diciembre"]

_booking_lock = threading.Lock()  # evita doble reserva simultánea (una sola instancia)


class UserError(Exception):
    """Error 'de negocio': el mensaje se le dice tal cual al cliente por voz."""


# --------------------------------------------------------------------------- #
# Credenciales de Google (service account)
# --------------------------------------------------------------------------- #
@lru_cache(maxsize=1)
def get_credentials() -> service_account.Credentials:
    """Acepta el JSON completo en GOOGLE_SERVICE_ACCOUNT_JSON o ese mismo JSON en base64."""
    raw = require_env("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw.lstrip().startswith("{"):
        raw = base64.b64decode(raw).decode("utf-8")
    info = json.loads(raw)
    return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)


def calendar_service():
    # Un cliente por petición: los clientes de googleapiclient no son thread-safe.
    return build("calendar", "v3", credentials=get_credentials(), cache_discovery=False)


# --------------------------------------------------------------------------- #
# Utilidades
# --------------------------------------------------------------------------- #
def humanize(dt: datetime) -> str:
    dt = dt.astimezone(TZ)
    return f"{DIAS[dt.weekday()]} {dt.day} de {MESES[dt.month - 1]} a las {dt:%H:%M}"


def clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def parse_day(value: Any) -> date:
    try:
        return date.fromisoformat(clean(value)[:10])
    except ValueError:
        raise UserError("La fecha no es válida. Necesito el formato año-mes-día, por ejemplo 2026-11-10.")


def parse_datetime(value: Any) -> datetime:
    try:
        dt = datetime.fromisoformat(clean(value).replace("Z", "+00:00"))
    except ValueError:
        raise UserError("La fecha y hora no son válidas. Necesito formato ISO, por ejemplo 2026-11-10T15:00:00.")
    return dt.replace(tzinfo=TZ) if dt.tzinfo is None else dt.astimezone(TZ)


def parse_duration(value: Any) -> int:
    try:
        minutes = int(value) if value not in (None, "") else DEFAULT_DURATION_MIN
    except (TypeError, ValueError):
        minutes = DEFAULT_DURATION_MIN
    return min(max(minutes, 10), 240)


# --------------------------------------------------------------------------- #
# Google Calendar
# --------------------------------------------------------------------------- #
def busy_intervals(start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    body = {
        "timeMin": start.isoformat(),
        "timeMax": end.isoformat(),
        "timeZone": TZ.key,
        "items": [{"id": CALENDAR_ID}],
    }
    resp = calendar_service().freebusy().query(body=body).execute()
    cal = next(iter(resp.get("calendars", {}).values()), {})
    if cal.get("errors"):
        raise PermissionError(f"Sin acceso al calendario: {cal['errors']}")
    return [
        (
            datetime.fromisoformat(b["start"].replace("Z", "+00:00")),
            datetime.fromisoformat(b["end"].replace("Z", "+00:00")),
        )
        for b in cal.get("busy", [])
    ]


def free_slots(day: date, duration_min: int) -> list[datetime]:
    if day.weekday() not in WORKDAYS:
        return []
    open_dt = datetime.combine(day, time(BUSINESS_START_HOUR), TZ)
    close_dt = datetime.combine(day, time(BUSINESS_END_HOUR), TZ)
    earliest = datetime.now(TZ) + timedelta(minutes=MIN_NOTICE_MIN)
    busy = busy_intervals(open_dt, close_dt)

    dur, step = timedelta(minutes=duration_min), timedelta(minutes=SLOT_STEP_MIN)
    slots, cur = [], open_dt
    while cur + dur <= close_dt:
        end = cur + dur
        if cur >= earliest and not any(cur < b_end and end > b_start for b_start, b_end in busy):
            slots.append(cur)
        cur += step
    return slots


def sample_evenly(items: list, n: int) -> list:
    if len(items) <= n:
        return items
    return [items[int(i * len(items) / n)] for i in range(n)]


def validate_slot(start: datetime, end: datetime) -> None:
    if start < datetime.now(TZ) + timedelta(minutes=MIN_NOTICE_MIN):
        raise UserError("Esa fecha y hora ya pasó o es demasiado pronto. Pídele al cliente otro horario.")
    if start.weekday() not in WORKDAYS:
        raise UserError(f"No agendamos los {DIAS[start.weekday()]}. Ofrece otro día.")
    open_dt = datetime.combine(start.date(), time(BUSINESS_START_HOUR), TZ)
    close_dt = datetime.combine(start.date(), time(BUSINESS_END_HOUR), TZ)
    if start < open_dt or end > close_dt:
        raise UserError(
            f"Ese horario está fuera de servicio. Atendemos de {BUSINESS_START_HOUR}:00 a {BUSINESS_END_HOUR}:00."
        )


# --------------------------------------------------------------------------- #
# Herramientas (tools) que Vapi puede invocar
# --------------------------------------------------------------------------- #
def tool_consultar_disponibilidad(args: dict, ctx: dict) -> dict:
    day = parse_day(args.get("fecha"))
    duration = parse_duration(args.get("duracion_minutos"))

    if day < datetime.now(TZ).date():
        raise UserError("Esa fecha ya pasó. Pídele al cliente una fecha futura.")

    slots = free_slots(day, duration)
    if slots:
        offered = sample_evenly(slots, MAX_SLOTS_TO_OFFER)
        return {
            "exito": True,
            "fecha": day.isoformat(),
            "dia": DIAS[day.weekday()],
            "horarios_disponibles": [s.strftime("%H:%M") for s in offered],
            "mensaje": f"Hay {len(slots)} horarios libres el {DIAS[day.weekday()]} {day.day} de {MESES[day.month - 1]}.",
        }

    # Sin cupo ese día: busca el siguiente día con disponibilidad (máx. 7 días).
    for offset in range(1, 8):
        nxt = day + timedelta(days=offset)
        nxt_slots = free_slots(nxt, duration)
        if nxt_slots:
            return {
                "exito": True,
                "fecha": day.isoformat(),
                "horarios_disponibles": [],
                "proximo_dia_disponible": nxt.isoformat(),
                "proximos_horarios": [s.strftime("%H:%M") for s in sample_evenly(nxt_slots, 4)],
                "mensaje": f"Ese día no hay horarios. El siguiente día con espacio es el {DIAS[nxt.weekday()]} {nxt.day} de {MESES[nxt.month - 1]}.",
            }
    return {"exito": True, "fecha": day.isoformat(), "horarios_disponibles": [],
            "mensaje": "No hay horarios disponibles en los próximos 7 días."}


def tool_agendar_cita(args: dict, ctx: dict) -> dict:
    nombre = clean(args.get("nombre"))
    telefono = clean(args.get("telefono")) or clean(ctx.get("customer_number"))
    motivo = clean(args.get("motivo"))
    if not nombre:
        raise UserError("Falta el nombre del cliente.")
    if not telefono:
        raise UserError("Falta el teléfono del cliente.")

    start = parse_datetime(args.get("fecha_hora"))
    end = start + timedelta(minutes=parse_duration(args.get("duracion_minutos")))
    validate_slot(start, end)

    with _booking_lock:
        if busy_intervals(start, end):
            alternatives = [s.strftime("%H:%M") for s in sample_evenly(free_slots(start.date(), (end - start).seconds // 60), 5)]
            return {
                "exito": False,
                "mensaje": "Ese horario ya está ocupado. Ofrece otro al cliente.",
                "horarios_alternativos": alternatives,
            }
        event = {
            "summary": f"Cita - {nombre}",
            "description": "\n".join(filter(None, [
                f"Cliente: {nombre}",
                f"Teléfono: {telefono}",
                f"Motivo: {motivo}" if motivo else "",
                f"Agendado por Vapi (call {ctx.get('call_id') or 'n/d'})",
            ])),
            "start": {"dateTime": start.isoformat(), "timeZone": TZ.key},
            "end": {"dateTime": end.isoformat(), "timeZone": TZ.key},
        }
        created = calendar_service().events().insert(calendarId=CALENDAR_ID, body=event).execute()

    log.info("Cita creada %s para %s", created.get("id"), nombre)
    return {
        "exito": True,
        "mensaje": f"Cita confirmada para {nombre} el {humanize(start)}.",
        "fecha_hora": start.isoformat(),
        "event_id": created.get("id"),
    }


TOOLS: dict[str, Callable[[dict, dict], dict]] = {
    "agendar_cita": tool_agendar_cita,
    "consultar_disponibilidad": tool_consultar_disponibilidad,
}


def extract_tool_calls(message: dict) -> list[tuple[str | None, str | None, dict]]:
    """Soporta el formato actual (toolCallList) y el anterior (toolCalls -> function)."""
    calls = []
    for tc in message.get("toolCallList") or message.get("toolCalls") or []:
        fn = tc.get("function") or {}
        name = tc.get("name") or fn.get("name")
        args = tc.get("arguments") or fn.get("arguments") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {}
        calls.append((tc.get("id"), name, args if isinstance(args, dict) else {}))
    return calls


def run_tool(name: str | None, args: dict, ctx: dict) -> dict:
    handler = TOOLS.get(name or "")
    if not handler:
        return {"exito": False, "mensaje": f"Herramienta desconocida: {name}"}
    try:
        return handler(args, ctx)
    except UserError as e:
        return {"exito": False, "mensaje": str(e)}
    except (PermissionError, HttpError) as e:
        log.exception("Error de acceso/API de Google en %s: %s", name, e)
        return {"exito": False, "mensaje": "No pude acceder al calendario en este momento. Ofrece que un asesor le devuelva la llamada."}
    except Exception:
        log.exception("Error inesperado en %s", name)
        return {"exito": False, "mensaje": "Ocurrió un error interno. Ofrece que un asesor le devuelva la llamada."}


# --------------------------------------------------------------------------- #
# Google Sheets (CRM)
# --------------------------------------------------------------------------- #
SHEET_HEADERS = [
    "Fecha registro", "Call ID", "Nombre", "Teléfono", "Fecha agendada",
    "Resumen", "Motivo de fin", "Duración (s)", "Costo (USD)", "Grabación",
]


def extract_structured(message: dict) -> dict:
    """analysis.structuredData (analysisPlan) o artifact.structuredOutputs (Structured Outputs)."""
    data = dict((message.get("analysis") or {}).get("structuredData") or {})
    outputs = (message.get("artifact") or {}).get("structuredOutputs") or {}
    for item in outputs.values():
        result = item.get("result") if isinstance(item, dict) else None
        if isinstance(result, dict):
            for k, v in result.items():
                data.setdefault(k, v)
    return data


def build_lead_row(message: dict) -> list[Any]:
    call = message.get("call") or {}
    analysis = message.get("analysis") or {}
    artifact = message.get("artifact") or {}
    data = extract_structured(message)
    customer_number = (call.get("customer") or {}).get("number") or ""
    cost = message.get("cost")
    return [
        datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"),
        call.get("id") or "",
        data.get("nombre") or data.get("name") or "",
        data.get("telefono") or data.get("phone") or customer_number,
        data.get("fecha_agendada") or data.get("fecha_cita") or "",
        analysis.get("summary") or message.get("summary") or artifact.get("summary") or "",
        message.get("endedReason") or "",
        round(message["durationSeconds"]) if isinstance(message.get("durationSeconds"), (int, float)) else "",
        round(cost, 4) if isinstance(cost, (int, float)) else "",
        artifact.get("recordingUrl") or message.get("recordingUrl") or "",
    ]


def append_lead(message: dict) -> str:
    row = build_lead_row(message)
    call_id = row[1]

    spreadsheet = gspread.authorize(get_credentials()).open_by_key(SHEET_ID)
    try:
        ws = spreadsheet.worksheet(SHEET_TAB)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(SHEET_TAB, rows=1000, cols=len(SHEET_HEADERS))

    existing_call_ids = ws.col_values(2)
    if not existing_call_ids:
        ws.append_row(SHEET_HEADERS, value_input_option="RAW", table_range="A1")
    elif call_id and call_id in existing_call_ids:
        return "duplicate"  # Vapi reintentó el webhook: no duplicar la fila

    # RAW => conserva "+52..." como texto y neutraliza cualquier "=fórmula"
    ws.append_row(row, value_input_option="RAW", table_range="A1")
    return "inserted"


# --------------------------------------------------------------------------- #
# FastAPI
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def lifespan(_: FastAPI):
    get_credentials()  # falla rápido si el JSON de la service account está mal
    log.info("Servidor listo. Zona horaria: %s", TZ.key)
    yield


app = FastAPI(title="Vapi Backend", lifespan=lifespan)


async def verify_secret(
    x_vapi_secret: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> None:
    """Acepta `X-Vapi-Secret: <secreto>` o `Authorization: Bearer <secreto>`."""
    provided = x_vapi_secret or ""
    if not provided and authorization and authorization.lower().startswith("bearer "):
        provided = authorization[7:].strip()
    if not provided or not hmac.compare_digest(provided.encode(), VAPI_SECRET.encode()):
        raise HTTPException(status_code=401, detail="No autorizado")


@app.get("/")
@app.get("/healthz")
def health() -> dict:
    return {"status": "ok"}


@app.post("/vapi/tools", dependencies=[Depends(verify_secret)])
async def vapi_tools(request: Request) -> dict:
    body = await request.json()
    message = body.get("message", body)

    if message.get("type") not in (None, "tool-calls"):
        return {}  # otros eventos que no son tool calls: se ignoran con 200

    call = message.get("call") or {}
    ctx = {"call_id": call.get("id"), "customer_number": (call.get("customer") or {}).get("number")}

    results = []
    for tool_call_id, name, args in extract_tool_calls(message):
        log.info("Tool call %s args=%s", name, args)
        payload = await run_in_threadpool(run_tool, name, args, ctx)
        results.append({
            "toolCallId": tool_call_id,
            # string en una sola línea (json.dumps sin indent no genera saltos de línea)
            "result": json.dumps(payload, ensure_ascii=False),
        })
    return {"results": results}


@app.post("/vapi/post-call", dependencies=[Depends(verify_secret)])
async def vapi_post_call(request: Request) -> dict:
    body = await request.json()
    message = body.get("message", body)

    if message.get("type") != "end-of-call-report":
        return {"ok": True, "ignored": message.get("type")}

    status = await run_in_threadpool(append_lead, message)
    log.info("Lead %s (call %s)", status, (message.get("call") or {}).get("id"))
    return {"ok": True, "status": status}
