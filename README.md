# Servidor Vapi → Google Calendar + Google Sheets

Reemplazo ligero de N8N + CRM de pago. FastAPI, un solo archivo (`main.py`).

| Endpoint | Qué hace |
|---|---|
| `POST /vapi/tools` | Recibe tool calls `consultar_disponibilidad` y `agendar_cita`, consulta/crea eventos en Google Calendar y responde en el formato de Vapi. |
| `POST /vapi/post-call` | Recibe `end-of-call-report` y agrega una fila a Google Sheets (con deduplicación por `call.id`). |
| `GET /healthz` | Health check. |

---

## 1. Configurar Google (una sola vez, ~10 min)

1. Entra a <https://console.cloud.google.com> y crea un proyecto.
2. **APIs y servicios → Biblioteca**: habilita **Google Calendar API** y **Google Sheets API**.
3. **IAM y administración → Cuentas de servicio → Crear cuenta de servicio** (sin roles). 
4. Entra a la cuenta creada → pestaña **Claves → Agregar clave → JSON**. Se descarga un archivo `.json`: es tu credencial. **No lo subas a Git.**
5. Copia el correo de la cuenta de servicio (`algo@proyecto.iam.gserviceaccount.com`).
6. **Calendario**: en Google Calendar → Configuración del calendario → *Compartir con personas* → agrega ese correo con permiso **"Hacer cambios en los eventos"**. Copia el **ID del calendario** (sección *Integrar el calendario*).
7. **Sheet**: crea una hoja de cálculo, compártela con ese mismo correo como **Editor**. Copia el ID de la URL (`/spreadsheets/d/<ID>/edit`). El servidor crea la pestaña `Leads` y los encabezados solo.

> Limitación de Google: una cuenta de servicio **no puede invitar asistentes** a un evento sin Domain-Wide Delegation (solo Workspace). Por eso el nombre, teléfono y motivo se guardan en el título y la descripción del evento.

---

## 2. Probar en local

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env     # llena los valores
export $(grep -v '^#' .env | xargs)   # o usa python-dotenv si prefieres
uvicorn main:app --reload --port 8000
```

Para `GOOGLE_SERVICE_ACCOUNT_JSON` usa el JSON completo en una línea o en base64 (`base64 -w0 key.json`).

```bash
curl -X POST localhost:8000/vapi/tools \
  -H "Content-Type: application/json" -H "X-Vapi-Secret: $VAPI_SECRET" \
  -d '{"message":{"type":"tool-calls","call":{"id":"test"},"toolCallList":[
        {"id":"call_1","name":"consultar_disponibilidad","arguments":{"fecha":"2026-11-10"}}]}}'
```

Respuesta esperada (nota: `result` es un **string** en una sola línea, como exige Vapi):

```json
{"results":[{"toolCallId":"call_1","result":"{\"exito\": true, \"fecha\": \"2026-11-10\", \"horarios_disponibles\": [\"09:00\", \"10:00\"], ...}"}]}
```

Para probar desde Vapi sin desplegar: `ngrok http 8000` y usa esa URL temporalmente.

---

## 3. Desplegar en Render (opción gratis recomendada)

1. Sube la carpeta a un repositorio de GitHub (el `.gitignore` ya excluye `.env` y claves `.json`).
2. En <https://render.com> → **New → Web Service** → conecta el repo.
3. Configura:
   - **Runtime**: Python 3
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `uvicorn main:app --host 0.0.0.0 --port $PORT`
   - **Instance Type**: **Free**
   - (Alternativa: **New → Blueprint** y Render lee `render.yaml`.)
4. En **Environment** agrega las variables de `.env.example`: `VAPI_SECRET`, `GOOGLE_CALENDAR_ID`, `GOOGLE_SHEET_ID`, `GOOGLE_SERVICE_ACCOUNT_JSON` (pega el JSON completo o el base64), y opcionalmente `TIMEZONE`, horarios, etc. Agrega también `PYTHON_VERSION=3.12.7`.
5. **Create Web Service**. Cuando termine, abre `https://TU-APP.onrender.com/healthz`: debe responder `{"status":"ok"}`.

### ⚠️ Importante: el plan gratis de Render "se duerme"

Un servicio gratis se apaga tras **15 minutos sin tráfico** y tarda cerca de **1 minuto** en despertar. En una llamada de voz eso significa silencio o una herramienta que falla por timeout. Soluciones:

- **Gratis**: crea un monitor en [UptimeRobot](https://uptimerobot.com) o [cron-job.org](https://cron-job.org) que haga `GET https://TU-APP.onrender.com/healthz` cada **5–10 minutos**. Un servicio activo 24/7 consume ~720 de las 750 horas gratis mensuales, así que cabe (si tienes otros servicios gratis en el mismo workspace, comparten esas horas).
- **De pago**: pasar a una instancia de pago (≈ 7 USD/mes) elimina el spin-down.

## 3b. Alternativa: Railway

Railway **ya no tiene un plan gratis permanente**: ofrece una prueba de 30 días con 5 USD de crédito y después un plan Free con 1 USD de crédito mensual (muy justo para un servicio 24/7); lo práctico es el plan Hobby (5 USD/mes con 5 USD de crédito de uso incluidos). Verifica precios vigentes en <https://railway.com/pricing>.

1. <https://railway.com> → **New Project → Deploy from GitHub repo**.
2. En **Variables** agrega las mismas variables de entorno.
3. En **Settings → Networking** pulsa **Generate Domain**.
4. Railway detecta Python por `requirements.txt`; si no, define Start Command: `uvicorn main:app --host 0.0.0.0 --port $PORT`.

---

## 4. Configurar Vapi

Todos los JSON están en **`vapi_tools.json`**. Reemplaza `TU-APP` por tu dominio y `TU_VAPI_SECRET` por el valor de `VAPI_SECRET`.

### Tools (`/vapi/tools`)
1. Dashboard → **Tools → Create Tool → Function** (o pega el JSON vía `POST https://api.vapi.ai/tool`). Crea `consultar_disponibilidad` y `agendar_cita`.
2. Verifica que **Server URL** sea `https://TU-APP.onrender.com/vapi/tools` y que el secreto coincida (Vapi lo envía como header `X-Vapi-Secret`; si tu panel solo ofrece una credencial *Bearer*, el servidor también acepta `Authorization: Bearer <secreto>`).
3. En tu asistente → **Tools** → agrega ambas herramientas.

### Post-call (`/vapi/post-call`)
1. Asistente → **Advanced → Messaging → Server URL**: `https://TU-APP.onrender.com/vapi/post-call` (y el mismo secreto).
2. En **Server Messages** deja activado únicamente `end-of-call-report`.
3. Asistente → **Analysis**: activa *Summary* y *Structured Data* con el esquema de `assistant_post_call.analysisPlan` en `vapi_tools.json` (campos `nombre`, `telefono`, `fecha_agendada`).
   - Si en tu cuenta usas **Structured Outputs** en lugar de `analysisPlan`, el servidor también lee `artifact.structuredOutputs` mientras los campos se llamen igual.
   - Si la llamada no produce datos estructurados, igual se guarda la fila con el número del llamante y el resumen.

### Prompt del asistente (importante)
Añade la fecha actual para que el modelo calcule "mañana" o "el lunes" correctamente, y el orden de uso de las herramientas:

```
Fecha y hora actual: {{now}}
Primero llama a consultar_disponibilidad con la fecha pedida y ofrece solo horarios que devuelva.
Cuando el cliente confirme nombre, teléfono, fecha y hora, llama a agendar_cita.
Si agendar_cita devuelve exito=false, di el mensaje al cliente y ofrece otro horario.
```

---

## 5. Cómo se comporta

- **Siempre HTTP 200** en tool calls (Vapi ignora respuestas con otros códigos). Los errores de negocio viajan en el `result` con `"exito": false` y un `mensaje` que el asistente lee al cliente.
- **Horario laboral**: configurable con `BUSINESS_START_HOUR`, `BUSINESS_END_HOUR`, `WORKDAYS` (0=lunes … 6=domingo). Rechaza fechas pasadas, fines de semana y horas fuera de servicio.
- **Doble reserva**: se verifica con `freebusy` y un candado en memoria antes de insertar (válido para una sola instancia, que es lo que da el plan gratis).
- **CRM**: cada llamada agrega una fila (`Fecha registro, Call ID, Nombre, Teléfono, Fecha agendada, Resumen, Motivo de fin, Duración, Costo, Grabación`). Si Vapi reintenta el webhook, no se duplica.
- **Seguridad**: cualquier petición sin el secreto correcto recibe 401.

## 6. Problemas comunes

| Síntoma | Causa probable |
|---|---|
| El asistente dice "no pude acceder al calendario" | No compartiste el calendario con el correo de la service account, o falta el permiso "Hacer cambios en los eventos". Revisa los logs de Render. |
| `404 Not Found` al escribir en Sheets | El Sheet no está compartido con la service account, o el `GOOGLE_SHEET_ID` es incorrecto. |
| Herramienta con timeout en la primera llamada del día | El servicio gratis estaba dormido: configura el ping cada 5–10 min. |
| Vapi dice "No result returned" | Revisa **Logs → Webhooks** en Vapi: casi siempre es URL mal escrita, secreto distinto (401) o servidor dormido. |
| `RuntimeError: Falta la variable...` al arrancar | Falta alguna variable obligatoria en Render. |
