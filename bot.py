import datetime
import io
import os
import threading
import logging
import functools
import calendar

from dotenv import load_dotenv
from supabase import create_client
from telegram import (
    Update, ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, 
    CallbackQueryHandler, ContextTypes, filters
)
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib import colors
from reportlab.lib.utils import simpleSplit
from apscheduler.schedulers.background import BackgroundScheduler

# ==========================================
# CONFIGURACIÓN Y LOGGING
# ==========================================
load_dotenv()

TOKEN = os.getenv("TELEGRAM_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

logging.basicConfig(
    level=logging.INFO, 
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("AsistenteProgramacion")

def safe_execute(func):
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        try:
            return await func(update, context, *args, **kwargs)
        except Exception as e:
            logger.error(f"Error en {func.__name__}: {e}", exc_info=True)
            if update.callback_query:
                await update.callback_query.answer("⚠️ Error al procesar la solicitud.")
            elif update.message:
                await update.message.reply_text("⚠️ Ha ocurrido un error interno, pero sigo operativo.")
    return wrapper

try:
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
except Exception as e:
    logger.critical(f"No se pudo conectar a Supabase: {e}")

# ESTADOS DE CONVERSACIÓN
esperando_dni = {}
esperando_dni_admin = {} 
esperando_revision_mes = {}
esperando_nueva_jornada = {} 
esperando_nuevo_trabajador = {} 
estado_incidencia = {} 

TEXTO_LEGAL = (
    "Este documento constituye el registro diario de jornada obligatorio según lo previsto en el artículo 34.9 "
    "del Estatuto de los Trabajadores. La empresa garantiza la conservación de estos registros durante cuatro años, "
    "permaneciendo a disposición de las personas trabajadoras y de la Inspección de Trabajo."
)

# ==========================================
# FUNCIONES DE APOYO
# ==========================================

def buscar_trabajador(telegram_id):
    try:
        r = supabase.table("trabajadores").select("*").eq("telegram_id", str(telegram_id)).execute()
        return r.data[0] if r.data else None
    except Exception as e:
        logger.error(f"Error en buscar_trabajador: {e}")
        return None

def registrar_dni(dni, telegram_id):
    r = supabase.table("trabajadores").select("*").eq("dni", dni.upper()).execute()
    if r.data:
        trabajador = r.data[0]
        supabase.table("trabajadores").update({"telegram_id": str(telegram_id)}).eq("id", trabajador["id"]).execute()
        return trabajador
    return None

def redondear_15_minutos(hora):
    if not hora: return hora
    try:
        h, m = map(int, hora.split(":"))
        resto = m % 15
        if resto < 8: m -= resto
        else: m += (15 - resto)
        if m == 60: h += 1; m = 0
        return f"{h:02d}:{m:02d}"
    except: return hora

def minutos(h):
    if not h: return 0
    try:
        parts = h.split(":")
        return int(parts[0]) * 60 + int(parts[1])
    except: return 0

def calcular_horas(e, pi, pf, s):
    if not e or not s: return "00:00"
    p = (minutos(pf) - minutos(pi)) if (pi and pf) else 0
    t = (minutos(s) - minutos(e)) - p
    h, m = divmod(max(0, t), 60)
    return f"{h:02d}:{m:02d}"

def sumar_horas_mensuales(jornadas):
    total_minutos = sum(minutos(j.get("horas_totales", "00:00")) for j in jornadas)
    h, m = divmod(total_minutos, 60)
    return f"{h:02d}:{m:02d}"

# ==========================================
# GENERACIÓN DE PDF
# ==========================================

def dibujar_cabecera(c, empresa, trabajador, titulo, y_start):
    width, height = A4
    y = y_start
    c.setFont("Helvetica-Bold", 16)
    c.drawString(50, y, empresa.get('nombre', 'EMPRESA').upper())
    y -= 20
    c.setFont("Helvetica", 10)
    c.drawString(50, y, f"CIF: {empresa.get('cif', '---')}")
    y -= 10
    c.setStrokeColor(colors.lightgrey)
    c.line(50, y, width - 50, y)
    y -= 25
    c.setFont("Helvetica-Bold", 11)
    c.drawString(50, y, "DATOS DEL TRABAJADOR")
    y -= 15
    c.setFont("Helvetica", 10)
    c.drawString(50, y, f"Nombre: {trabajador['nombre']}")
    c.drawString(300, y, f"DNI/NIE: {trabajador['dni']}")
    y -= 30
    c.setFont("Helvetica-Bold", 12)
    c.drawCentredString(width/2, y, titulo)
    return y - 30

def dibujar_pie_y_legal(c, y, empresa):
    width, height = A4
    if y < 150: c.showPage(); y = height - 50
    y -= 40
    c.setFont("Helvetica", 8); c.setFillColor(colors.darkslategray)
    lines = simpleSplit(TEXTO_LEGAL, "Helvetica", 8, width - 100)
    for line in lines:
        c.drawString(50, y, line)
        y -= 10
    c.setFont("Helvetica-Bold", 9); c.setFillColor(colors.black)
    c.line(50, 65, width - 50, 65)
    c.drawString(50, 50, empresa.get('nombre', 'EMPRESA').upper())
    c.drawRightString(width - 50, 50, f"Generado: {datetime.datetime.now().strftime('%d/%m/%Y %H:%M:%S')}")

def generar_pdf_jornada(empresa, trabajador, jornada):
    try:
        buffer = io.BytesIO()
        c = canvas.Canvas(buffer, pagesize=A4)
        width, height = A4
        y = dibujar_cabecera(c, empresa, trabajador, f"REGISTRO DIARIO - FECHA: {jornada['fecha']}", height - 50)
        c.setFillColor(colors.whitesmoke); c.rect(50, y - 5, width - 100, 25, fill=1)
        c.setFillColor(colors.black); c.setFont("Helvetica-Bold", 9)
        x_p = [55, 155, 255, 355, 455]
        for txt, x in zip(["ENTRADA", "P. INICIO", "P. FIN", "SALIDA", "TOTAL"], x_p): c.drawString(x, y + 2, txt)
        y -= 20; c.setFont("Helvetica", 10)
        vals = [str(jornada.get('entrada') or '--'), str(jornada.get('pausa_inicio') or '--'), str(jornada.get('pausa_fin') or '--'), str(jornada.get('salida') or '--'), str(jornada.get('horas_totales') or '00:00')]
        for v, x in zip(vals, x_p): c.drawString(x, y, v)
        y -= 50; c.setFont("Helvetica-Bold", 11); c.drawString(50, y, "AUDITORÍA Y CONTROL DE JORNADA"); y -= 15
        c.setFont("Helvetica-Oblique", 9)
        auds = supabase.table("auditoria").select("*").eq("trabajador_id", trabajador["id"]).gte("fecha", f"{jornada['fecha']}T00:00:00").lte("fecha", f"{jornada['fecha']}T23:59:59").execute().data
        eventos = [a for a in auds if any(k in str(a.get('accion','')) for k in ["Aprobación", "Denegación", "Cambio manual", "Inserción manual"])]
        if eventos:
            for a in eventos:
                h_audit = str(a.get('fecha', ''))[11:16]
                c.drawString(60, y, f"[{h_audit}] {a.get('accion','')} (Validado por: {a.get('admin','Admin')})"); y -= 12
        else: c.drawString(60, y, "Sin incidencias registradas.")
        dibujar_pie_y_legal(c, y, empresa); c.save(); return buffer.getvalue()
    except Exception as e: logger.error(f"Error PDF: {e}"); return None

def generar_pdf_mensual(empresa, trabajador, anio, mes):
    try:
        buffer = io.BytesIO()
        c = canvas.Canvas(buffer, pagesize=A4)
        width, height = A4
        y = dibujar_cabecera(c, empresa, trabajador, f"REPORTE MENSUAL: {mes:02d}/{anio}", height - 50)
        
        # Tabla de registros
        c.setFont("Helvetica-Bold", 8); x_p = [55, 120, 180, 240, 300, 360]
        for txt, x in zip(["FECHA", "ENT.", "P. INI", "P. FIN", "SAL.", "TOTAL"], x_p): c.drawString(x, y, txt)
        y -= 10; c.line(50, y+2, width-50, y+2)
        
        f_ini, f_fin = f"{anio}-{mes:02d}-01", f"{anio}-{mes:02d}-{calendar.monthrange(anio, mes)[1]}"
        jornadas = supabase.table("jornadas").select("*").eq("trabajador_id", trabajador["id"]).gte("fecha", f_ini).lte("fecha", f_fin).order("fecha").execute().data
        
        c.setFont("Helvetica", 9)
        for j in jornadas:
            if y < 150: 
                c.showPage(); y = height - 50; c.setFont("Helvetica", 9)
            vals = [str(j.get('fecha', '--')), str(j.get('entrada') or '--'), str(j.get('pausa_inicio') or '--'), str(j.get('pausa_fin') or '--'), str(j.get('salida') or '--'), str(j.get('horas_totales') or '--')]
            for v, x in zip(vals, x_p): c.drawString(x, y, v)
            y -= 15
            
        y -= 10; c.setFont("Helvetica-Bold", 10)
        c.drawRightString(width - 50, y, f"SUMA TOTAL MES: {sumar_horas_mensuales(jornadas)}")
        
        # --- AUDITORÍAS DEL MES ---
        y -= 30
        if y < 150: c.showPage(); y = height - 50
        
        c.setFont("Helvetica-Bold", 11); c.drawString(50, y, "AUDITORÍA Y CONTROL DE JORNADA (MENSUAL)")
        y -= 15; c.setFont("Helvetica-Oblique", 8)
        
        auds_mes = supabase.table("auditoria").select("*").eq("trabajador_id", trabajador["id"]).gte("fecha", f"{f_ini}T00:00:00").lte("fecha", f"{f_fin}T23:59:59").order("fecha").execute().data
        eventos = [a for a in auds_mes if any(k in str(a.get('accion','')) for k in ["Aprobación", "Denegación", "manual", "Inserción"])]
        
        if eventos:
            for a in eventos:
                if y < 80: c.showPage(); y = height - 50; c.setFont("Helvetica-Oblique", 8)
                fecha_aud = str(a.get('fecha', ''))[:10]
                hora_aud = str(a.get('fecha', ''))[11:16]
                c.drawString(60, y, f"[{fecha_aud} {hora_aud}] {a.get('accion','')} (Admin: {a.get('admin','Admin')})")
                y -= 10
        else:
            c.drawString(60, y, "No se registran incidencias manuales en este periodo.")
            
        dibujar_pie_y_legal(c, y - 40, empresa)
        c.save()
        return buffer.getvalue()
    except Exception as e: logger.error(f"Error PDF Mensual: {e}"); return None

# ==========================================
# TAREAS AUTOMÁTICAS
# ==========================================

def ejecutar_revision_rango(fecha_inicio, fecha_fin):
    try:
        # 1. Obtener jornadas en el rango
        jornadas = supabase.table("jornadas").select("*").gte("fecha", fecha_inicio).lte("fecha", fecha_fin).execute().data
        for j in jornadas:
            try:
                # Buscar trabajador y su empresa específica
                t_res = supabase.table("trabajadores").select("*").eq("id", j["trabajador_id"]).execute().data
                if not t_res: continue
                t = t_res[0]
                
                emp_res = supabase.table("empresas").select("*").eq("id", t["empresa_id"]).execute().data
                if not emp_res: continue
                emp = emp_res[0]

                pdf = generar_pdf_jornada(emp, t, j)
                if pdf:
                    f_obj = datetime.datetime.strptime(j['fecha'], '%Y-%m-%d')
                    # Ruta organizada por Empresa ID / Año / Mes / Trabajador
                    ruta = f"{emp['id']}/{f_obj.year}/{f_obj.month:02d}/{t['nombre']}/{j['fecha']}_{t['dni']}.pdf"
                    supabase.storage.from_("registro_laboral").upload(
                        path=ruta, 
                        file=pdf, 
                        file_options={"content-type": "application/pdf", "upsert": "true"}
                    )
            except Exception as e:
                logger.error(f"Error procesando jornada {j.get('id')}: {e}")

        # 2. Generar resúmenes mensuales para TODOS los trabajadores involucrados
        f_ini_obj = datetime.datetime.strptime(fecha_inicio, '%Y-%m-%d')
        trabajadores = supabase.table("trabajadores").select("*").execute().data
        for tr in trabajadores:
            try:
                emp_res = supabase.table("empresas").select("*").eq("id", tr["empresa_id"]).execute().data
                if not emp_res: continue
                emp = emp_res[0]
                
                pdf_m = generar_pdf_mensual(emp, tr, f_ini_obj.year, f_ini_obj.month)
                if pdf_m:
                    ruta_m = f"{emp['id']}/{f_ini_obj.year}/{f_ini_obj.month:02d}/{tr['nombre']}/RESUMEN_{f_ini_obj.month:02d}_{tr['dni']}.pdf"
                    supabase.storage.from_("registro_laboral").upload(
                        path=ruta_m, 
                        file=pdf_m, 
                        file_options={"content-type": "application/pdf", "upsert": "true"}
                    )
            except Exception as e:
                logger.error(f"Error en resumen mensual para {tr.get('nombre')}: {e}")
    except Exception as e: 
        logger.error(f"Error crítico en Revision: {e}")

def tarea_pdf_diario():
    hoy = datetime.date.today().isoformat()
    ejecutar_revision_rango(hoy, hoy)

# ==========================================
# MENÚS Y TECLADOS
# ==========================================

def menu_trabajador():
    return ReplyKeyboardMarkup([["🟢 Entrada"], ["⏸ Pausa", "▶️ Fin pausa"], ["🔴 Salida"], ["⚠️ Incidencia", "📊 Mis horas"]], resize_keyboard=True)

def menu_admin():
    return ReplyKeyboardMarkup([["🟢 Entrada"], ["⏸ Pausa", "▶️ Fin pausa"], ["🔴 Salida"], ["⚠️ Incidencia", "📊 Mis horas"], ["📋 Incidencias trabajadores"]], resize_keyboard=True)

def teclado_dia_incidencia():
    return ReplyKeyboardMarkup([["📅 Hoy", "📅 Otro día"]], resize_keyboard=True)

def teclado_tipo_incidencia():
    return ReplyKeyboardMarkup([
        ["❌ 🟢 No registré entrada"], ["❌ ⏸ No registré pausa"],
        ["❌ ▶️ No registré fin pausa"], ["❌ 🔴 No registré salida"]
    ], resize_keyboard=True)

# ==========================================
# LÓGICA DE MENSAJES
# ==========================================

@safe_execute
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = buscar_trabajador(update.message.from_user.id)
    if t:
        await update.message.reply_text(f"Hola {t['nombre']}", reply_markup=menu_admin() if t['rol']=='admin' else menu_trabajador())
    else: 
        esperando_dni[update.message.from_user.id] = True
        await update.message.reply_text("Introduce tu DNI para vincular tu cuenta:")

@safe_execute
async def manejar_mensajes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text
    chat_id = update.message.chat_id
    u_id = update.message.from_user.id
    hoy = datetime.date.today().isoformat()
    hora = redondear_15_minutos(datetime.datetime.now().strftime("%H:%M"))

    if u_id in esperando_dni:
        if registrar_dni(texto, u_id):
            esperando_dni.pop(u_id)
            await start(update, context)
        else: await update.message.reply_text("DNI no válido.")
        return

    if u_id in estado_incidencia:
        d = estado_incidencia[u_id]
        if d.get('paso') == 1:
            if texto == "📅 Hoy": d['fecha'] = hoy; d['paso'] = 3; await update.message.reply_text("¿Qué registro olvidaste?", reply_markup=teclado_tipo_incidencia())
            else: d['paso'] = 2; await update.message.reply_text("Indica la fecha (AAAA-MM-DD):")
        elif d.get('paso') == 2:
            d['fecha'] = texto; d['paso'] = 3; await update.message.reply_text("¿Qué registro olvidaste?", reply_markup=teclado_tipo_incidencia())
        elif d.get('paso') == 3:
            m = {"❌ 🟢 No registré entrada": "entrada", "❌ ⏸ No registré pausa": "pausa_inicio", "❌ ▶️ No registré fin pausa": "pausa_fin", "❌ 🔴 No registré salida": "salida"}
            d['tipo'] = m.get(texto); d['paso'] = 4; await update.message.reply_text("Hora correcta (HH:MM):")
        elif d.get('paso') == 4:
            d['hora'] = texto; d['paso'] = 5; await update.message.reply_text("Escribe el motivo brevemente:")
        elif d.get('paso') == 5:
            t = buscar_trabajador(u_id)
            supabase.table("incidencias").insert({"empresa_id": t["empresa_id"], "trabajador_id": t["id"], "fecha": d['fecha'], "tipo": d['tipo'], "hora_real": d['hora'], "motivo": texto, "estado": "pendiente"}).execute()
            estado_incidencia.pop(u_id)
            await update.message.reply_text("✅ Incidencia enviada.", reply_markup=menu_admin() if t['rol']=='admin' else menu_trabajador())
        return

    if chat_id in esperando_nueva_jornada:
        datos = esperando_nueva_jornada[chat_id]
        if len(datos) == 0:
            tr = supabase.table("trabajadores").select("*").eq("dni", texto.upper()).execute().data
            if tr: datos.append(tr[0]); await update.message.reply_text(f"Trabajador: {tr[0]['nombre']}\nFecha (AAAA-MM-DD):")
            else: await update.message.reply_text("DNI no encontrado.")
        elif len(datos) == 1:
            datos.append(texto); await update.message.reply_text("Entrada (HH:MM):")
        elif len(datos) == 2:
            datos.append(texto); await update.message.reply_text("Salida (HH:MM):")
        elif len(datos) == 3:
            tr, fecha, ent, sal = datos[0], datos[1], datos[2], texto
            h_tot = calcular_horas(ent, None, None, sal)
            supabase.table("jornadas").insert({"empresa_id": tr["empresa_id"], "trabajador_id": tr["id"], "fecha": fecha, "entrada": ent, "salida": sal, "horas_totales": h_tot, "estado": "cerrada"}).execute()
            supabase.table("auditoria").insert({"trabajador_id": tr["id"], "empresa_id": tr["empresa_id"], "accion": f"Inserción manual jornada ({ent}-{sal})", "admin": "Admin"}).execute()
            esperando_nueva_jornada.pop(chat_id)
            threading.Thread(target=ejecutar_revision_rango, args=(fecha, fecha)).start()
            await update.message.reply_text(f"✅ Jornada creada para {tr['nombre']}.")
        return

    if chat_id in esperando_nuevo_trabajador:
        datos = esperando_nuevo_trabajador[chat_id]
        if len(datos) == 0:
            datos.append(texto); await update.message.reply_text("DNI del trabajador:")
        elif len(datos) == 1:
            nom, dni = datos[0], texto.upper()
            t_adm = buscar_trabajador(u_id)
            supabase.table("trabajadores").insert({"nombre": nom, "dni": dni, "empresa_id": t_adm["empresa_id"], "rol": "trabajador"}).execute()
            esperando_nuevo_trabajador.pop(chat_id)
            await update.message.reply_text(f"✅ {nom} registrado correctamente.")
        return

    if chat_id in esperando_revision_mes:
        esperando_revision_mes.pop(chat_id)
        try:
            anio, mes = map(int, texto.split("-"))
            f_ini = f"{anio}-{mes:02d}-01"; f_fin = f"{anio}-{mes:02d}-{calendar.monthrange(anio, mes)[1]}"
            await update.message.reply_text("🚀 Iniciando revisión..."); threading.Thread(target=ejecutar_revision_rango, args=(f_ini, f_fin)).start()
            await update.message.reply_text("✅ Proceso lanzado.")
        except: await update.message.reply_text("Formato AAAA-MM.")
        return

    if chat_id in esperando_dni_admin:
        tipo_pdf = esperando_dni_admin.pop(chat_id)
        query = supabase.table("trabajadores").select("*")
        if texto.lower() != "todos": query = query.eq("dni", texto.upper())
        trabs = query.execute().data
        if not trabs: await update.message.reply_text("No encontrado."); return
        for tr in trabs:
            emp = supabase.table("empresas").select("*").eq("id", tr["empresa_id"]).execute().data[0]
            if tipo_pdf == "pdf_dia":
                j_res = supabase.table("jornadas").select("*").eq("trabajador_id", tr["id"]).eq("fecha", hoy).execute().data
                if j_res:
                    pdf = generar_pdf_jornada(emp, tr, j_res[0])
                    if pdf: await update.message.reply_document(io.BytesIO(pdf), filename=f"Diario_{tr['dni']}.pdf")
            else:
                pdf = generar_pdf_mensual(emp, tr, datetime.date.today().year, datetime.date.today().month)
                if pdf: await update.message.reply_document(io.BytesIO(pdf), filename=f"Mensual_{tr['dni']}.pdf")
        return

    t = buscar_trabajador(u_id)
    if not t: return

    if texto == "⚠️ Incidencia":
        estado_incidencia[u_id] = {'paso': 1}
        await update.message.reply_text("¿De qué día es la incidencia?", reply_markup=teclado_dia_incidencia())
    elif texto == "🟢 Entrada":
        j_res = supabase.table("jornadas").select("*").eq("trabajador_id", t["id"]).eq("fecha", hoy).execute().data
        if not j_res:
            supabase.table("jornadas").insert({"empresa_id": t["empresa_id"], "trabajador_id": t["id"], "fecha": hoy, "entrada": hora}).execute()
            await update.message.reply_text(f"Entrada: {hora}")
    elif texto == "🔴 Salida":
        j_res = supabase.table("jornadas").select("*").eq("trabajador_id", t["id"]).eq("fecha", hoy).execute().data
        if j_res:
            j = j_res[0]; h_tot = calcular_horas(j["entrada"], j.get("pausa_inicio"), j.get("pausa_fin"), hora)
            supabase.table("jornadas").update({"salida": hora, "horas_totales": h_tot, "estado": "cerrada"}).eq("id", j["id"]).execute()
            threading.Thread(target=tarea_pdf_diario).start()
            await update.message.reply_text(f"Salida: {hora}. Total: {h_tot}")
    elif texto == "⏸ Pausa":
        j_res = supabase.table("jornadas").select("*").eq("trabajador_id", t["id"]).eq("fecha", hoy).execute().data
        if j_res: supabase.table("jornadas").update({"pausa_inicio": hora}).eq("id", j_res[0]["id"]).execute(); await update.message.reply_text(f"Pausa: {hora}")
    elif texto == "▶️ Fin pausa":
        j_res = supabase.table("jornadas").select("*").eq("trabajador_id", t["id"]).eq("fecha", hoy).execute().data
        if j_res: supabase.table("jornadas").update({"pausa_fin": hora}).eq("id", j_res[0]["id"]).execute(); await update.message.reply_text(f"Fin pausa: {hora}")
    elif texto == "📊 Mis horas":
        emp = supabase.table("empresas").select("*").eq("id", t["empresa_id"]).execute().data[0]
        pdf = generar_pdf_mensual(emp, t, datetime.date.today().year, datetime.date.today().month)
        if pdf: await update.message.reply_document(io.BytesIO(pdf), filename="Mis_Horas.pdf")
    elif texto == "📋 Incidencias trabajadores" and t["rol"] == "admin":
        r = supabase.table("incidencias").select("*").eq("estado", "pendiente").execute().data
        if not r: await update.message.reply_text("No hay incidencias."); return
        for i in r:
            t_nom = supabase.table("trabajadores").select("nombre").eq("id", i["trabajador_id"]).execute().data[0]
            ic = {"entrada": "🟢", "pausa_inicio": "⏸", "pausa_fin": "▶️", "salida": "🔴"}.get(i['tipo'], "❓")
            btns = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Aprobar", callback_data=f"apr_{i['id']}"), InlineKeyboardButton("❌ Rechazar", callback_data=f"rej_{i['id']}") ]])
            await update.message.reply_text(f"👤 {t_nom['nombre']}\n📅 {i['fecha']}\n{ic} {i['tipo'].title()}: {i['hora_real']}\n📝 {i['motivo']}", reply_markup=btns)

# ==========================================
# CALLBACKS Y COMANDOS ADMIN
# ==========================================

@safe_execute
async def botones_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer(); accion, inc_id = query.data.split("_")
    inc = supabase.table("incidencias").select("*").eq("id", inc_id).execute().data[0]
    adm = buscar_trabajador(query.from_user.id)
    log = f"{'Aprobación' if accion == 'apr' else 'Denegación'} {inc['tipo']} ({inc['hora_real']})"
    supabase.table("auditoria").insert({"trabajador_id": inc["trabajador_id"], "empresa_id": inc["empresa_id"], "accion": log, "admin": adm["nombre"] if adm else "Admin"}).execute()
    if accion == "apr":
        res = supabase.table("jornadas").select("*").eq("trabajador_id", inc["trabajador_id"]).eq("fecha", inc["fecha"]).execute().data
        if res:
            j = res[0]; campos = {inc["tipo"]: inc["hora_real"]}
            e, s, pi, pf = j.get("entrada"), j.get("salida"), j.get("pausa_inicio"), j.get("pausa_fin")
            if inc["tipo"] == "entrada": e = inc["hora_real"]
            elif inc["tipo"] == "salida": s = inc["hora_real"]
            elif inc["tipo"] == "pausa_inicio": pi = inc["hora_real"]
            elif inc["tipo"] == "pausa_fin": pf = inc["hora_real"]
            campos["horas_totales"] = calcular_horas(e, pi, pf, s)
            supabase.table("jornadas").update(campos).eq("id", j["id"]).execute()
        else:
            supabase.table("jornadas").insert({"trabajador_id": inc["trabajador_id"], "empresa_id": inc["empresa_id"], "fecha": inc["fecha"], inc["tipo"]: inc["hora_real"]}).execute()
        supabase.table("incidencias").update({"estado": "aprobada"}).eq("id", inc_id).execute()
        threading.Thread(target=ejecutar_revision_rango, args=(inc['fecha'], inc['fecha'])).start()
        await query.edit_message_text(f"✅ {log}")
    else:
        supabase.table("incidencias").update({"estado": "rechazada"}).eq("id", inc_id).execute()
        await query.edit_message_text(f"❌ {log}")

@safe_execute
async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = buscar_trabajador(update.message.from_user.id)
    if not t or t['rol'] != 'admin': return
    cmd = update.message.text.split()[0][1:]
    if cmd == "crear_jornada": esperando_nueva_jornada[update.message.chat_id] = []; await update.message.reply_text("DNI del trabajador:")
    elif cmd == "registrar_trabajador": esperando_nuevo_trabajador[update.message.chat_id] = []; await update.message.reply_text("Nombre completo:")
    elif cmd == "revisar_mes": esperando_revision_mes[update.message.chat_id] = True; await update.message.reply_text("Mes (AAAA-MM):")
    else:
        esperando_dni_admin[update.message.chat_id] = cmd; await update.message.reply_text("DNI o 'todos':")

if __name__ == "__main__":
    scheduler = BackgroundScheduler()
    scheduler.add_job(tarea_pdf_diario, "cron", hour=23, minute=0)
    scheduler.start()
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler(["pdf_dia", "pdf_mes", "revisar_mes", "crear_jornada", "registrar_trabajador"], cmd_admin))
    app.add_handler(CallbackQueryHandler(botones_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, manejar_mensajes))
    app.run_polling()