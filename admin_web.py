import streamlit as st
import pandas as pd
from supabase import create_client
import os
from dotenv import load_dotenv
from datetime import datetime
import time

# --- CONFIGURACIÓN DE CONEXIÓN ---
load_dotenv()
url = os.getenv("SUPABASE_URL")
key = os.getenv("SUPABASE_KEY")
supabase = create_client(url, key)

st.set_page_config(page_title="Registro Laboral Control Panel", layout="wide", page_icon="🏢")

# --- FUNCIONES DE UTILIDAD ---
def fetch_all(table):
    try:
        res = supabase.table(table).select("*").execute()
        return pd.DataFrame(res.data)
    except Exception:
        return pd.DataFrame()

def calcular_horas_hhmm(h1, h2):
    try:
        if not h1 or not h2: return "00:00"
        fmt = '%H:%M'
        t1 = datetime.strptime(h1, fmt)
        t2 = datetime.strptime(h2, fmt)
        diff = t2 - t1
        segundos = int(diff.total_seconds())
        if segundos < 0: return "00:00"
        horas = segundos // 3600
        minutos = (segundos % 3600) // 60
        return f"{horas:02d}:{minutos:02d}"
    except:
        return "00:00"

# --- CARGA DE DATOS PARA CRUCES ---
df_trab_raw = fetch_all("trabajadores")
df_emp_raw = fetch_all("empresas")

map_nombre_trab = {t['id']: t['nombre'] for _, t in df_trab_raw.iterrows()} if not df_trab_raw.empty else {}
map_dni_trab = {t['id']: t['dni'] for _, t in df_trab_raw.iterrows()} if not df_trab_raw.empty else {}
map_nombre_emp = {e['id']: e['nombre'] for _, e in df_emp_raw.iterrows()} if not df_emp_raw.empty else {}
map_empresa_de_trab = {t['id']: t['empresa_id'] for _, t in df_trab_raw.iterrows()} if not df_trab_raw.empty else {}

# --- MENÚ LATERAL ---
st.sidebar.title("🛠️ Registro Laboral Admin")
menu = ["📊 Resumen Mensual", "🏢 Empresas", "👥 Trabajadores", "📅 Jornadas", "⚠️ Incidencias", "🕵️ Auditoría", "📂 Bucket PDFs"]
choice = st.sidebar.radio("Navegación", menu)

# --- 1. RESUMEN DE HORAS ---
if choice == "📊 Resumen Mensual":
    st.title("📊 Horas Totales por Mes")
    df_j = fetch_all("jornadas")
    if not df_j.empty and 'horas_totales' in df_j.columns:
        df_j['fecha_dt'] = pd.to_datetime(df_j['fecha'])
        meses = sorted(df_j['fecha_dt'].dt.strftime('%Y-%m').unique(), reverse=True)
        mes_sel = st.selectbox("Seleccionar Mes", meses)
        df_mes = df_j[df_j['fecha_dt'].dt.strftime('%Y-%m') == mes_sel].copy()
        
        def h_to_min(h):
            if not h or ':' not in str(h): return 0
            p = str(h).split(':')
            return int(p[0]) * 60 + int(p[1])

        df_mes['minutos'] = df_mes['horas_totales'].apply(h_to_min)
        res = df_mes.groupby('trabajador_id')['minutos'].sum().reset_index()
        res['Nombre'] = res['trabajador_id'].map(map_nombre_trab)
        res['DNI'] = res['trabajador_id'].map(map_dni_trab)
        res['Total Horas'] = res['minutos'].apply(lambda x: f"{int(x//60):02d}:{int(x%60):02d}")
        st.table(res[['Nombre', 'DNI', 'Total Horas']])
        st.bar_chart(res.set_index('Nombre')['minutos'])
    else:
        st.info("No hay datos de jornadas.")

# --- 2. EMPRESAS ---
elif choice == "🏢 Empresas":
    st.title("🏢 Gestión de Empresas")
    with st.form("f_emp"):
        c1, c2 = st.columns(2); n = c1.text_input("Nombre Empresa"); c = c2.text_input("CIF")
        e = c1.text_input("Email"); p = c2.text_input("Password", type="password")
        if st.form_submit_button("Registrar Empresa"):
            supabase.table("empresas").insert({"nombre": n, "cif": c, "email": e, "password": p}).execute()
            st.rerun()
    if not df_emp_raw.empty:
        st.dataframe(df_emp_raw.reindex(columns=['nombre', 'cif', 'email', 'password', 'id']), use_container_width=True)

# --- 3. TRABAJADORES ---
elif choice == "👥 Trabajadores":
    st.title("👥 Gestión de Trabajadores")
    with st.form("f_trab"):
        nom = st.text_input("Nombre Completo"); dni = st.text_input("DNI")
        emp = st.selectbox("Empresa", options=list(map_nombre_emp.keys()), format_func=lambda x: map_nombre_emp[x])
        rol = st.selectbox("Rol", ["trabajador", "admin"]); tel = st.text_input("ID Telegram")
        if st.form_submit_button("Crear"):
            supabase.table("trabajadores").insert({"nombre": nom, "dni": dni.upper(), "empresa_id": emp, "rol": rol, "telegram_id": tel}).execute()
            st.rerun()
    if not df_trab_raw.empty:
        df_t = df_trab_raw.copy(); df_t['empresa_nombre'] = df_t['empresa_id'].map(map_nombre_emp)
        st.dataframe(df_t.reindex(columns=['nombre', 'dni', 'empresa_nombre', 'empresa_id', 'telegram_id', 'id']), use_container_width=True)

# --- 4. JORNADAS ---
elif choice == "📅 Jornadas":
    st.title("📅 Historial de Jornadas")
    df_j = fetch_all("jornadas")
    if not df_j.empty:
        df_j['Nombre'] = df_j['trabajador_id'].map(map_nombre_trab)
        df_j['DNI'] = df_j['trabajador_id'].map(map_dni_trab)
        for col in ['pausa_inicio', 'pausa_fin', 'estado', 'modificado_manualmente']:
            if col not in df_j.columns: df_j[col] = None
        cols = ['Nombre', 'DNI', 'fecha', 'entrada', 'pausa_inicio', 'pausa_fin', 'salida', 'horas_totales', 'estado', 'modificado_manualmente', 'id', 'trabajador_id']
        st.dataframe(df_j.reindex(columns=cols).sort_values("fecha", ascending=False), use_container_width=True)

# --- 5. INCIDENCIAS ---
elif choice == "⚠️ Incidencias":
    st.title("⚠️ Panel de Incidencias")
    inc_list = supabase.table("incidencias").select("*").eq("estado", "pendiente").execute().data
    for i in inc_list:
        t_nom = map_nombre_trab.get(i['trabajador_id'], "N/A")
        t_dni = map_dni_trab.get(i['trabajador_id'], "N/A")
        e_nom = map_nombre_emp.get(map_empresa_de_trab.get(i['trabajador_id']), "N/A")
        with st.expander(f"Solicitud: {t_nom} - {i['fecha']}"):
            st.write(f"**DNI:** {t_dni} | **Empresa:** {e_nom} | **Hora Real:** {i['hora_real']}")
            st.write(f"**Motivo:** {i['motivo']} | **Creado:** {i['created_at']}")
            c1, c2 = st.columns(2)
            if c1.button("✅ Aprobar", key=f"ok_{i['id']}"):
                j_ex = supabase.table("jornadas").select("*").eq("trabajador_id", i['trabajador_id']).eq("fecha", i['fecha']).execute().data
                upd = {"trabajador_id": i['trabajador_id'], "fecha": i['fecha'], "modificado_manualmente": True, "estado": "completado"}
                if i['tipo'] == "entrada": upd["entrada"] = i['hora_real']
                else:
                    upd["salida"] = i['hora_real']
                    if j_ex and j_ex[0]['entrada']: upd["horas_totales"] = calcular_horas_hhmm(j_ex[0]['entrada'], i['hora_real'])
                if j_ex: supabase.table("jornadas").update(upd).eq("id", j_ex[0]['id']).execute()
                else: supabase.table("jornadas").insert(upd).execute()
                supabase.table("incidencias").update({"estado": "aprobada"}).eq("id", i['id']).execute()
                st.success("Aprobado!"); time.sleep(1); st.rerun()
            if c2.button("❌ Rechazar", key=f"no_{i['id']}"):
                supabase.table("incidencias").update({"estado": "rechazada"}).eq("id", i['id']).execute()
                st.rerun()

# --- 6. AUDITORÍA ---
elif choice == "🕵️ Auditoría":
    st.title("🕵️ Log de Auditoría")
    df_aud = fetch_all("auditoria")
    if not df_aud.empty:
        df_aud['Trabajador'] = df_aud['trabajador_id'].map(map_nombre_trab)
        df_aud['DNI'] = df_aud['trabajador_id'].map(map_dni_trab)
        cols = ['fecha', 'Trabajador', 'DNI', 'accion', 'detalles', 'id', 'trabajador_id']
        st.dataframe(df_aud.reindex(columns=cols).sort_values("fecha", ascending=False), use_container_width=True)

# --- 7. BUCKET PDFs ---
elif choice == "📂 Bucket PDFs":
    st.title("📂 Histórico de PDFs")
    try:
        files = supabase.storage.from_("registro_laboral").list()
        for f in files:
            col1, col2 = st.columns([4, 1])
            col1.write(f"📄 {f['name']}")
            url_desc = supabase.storage.from_("registro_laboral").get_public_url(f['name'])
            col2.markdown(f"[Descargar]({url_desc})")
    except Exception as e:
        st.error(f"Error bucket: {e}")