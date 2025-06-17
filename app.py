# -*- coding: utf-8 -*-
import streamlit as st
import folium
from streamlit_folium import st_folium
import pandas as pd
from exif import Image as ExifImage
from PIL import Image
from io import BytesIO
import os
import pyodbc
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient, generate_blob_sas, UserDelegationKey
from datetime import datetime, time, timedelta
from dotenv import load_dotenv
import struct

load_dotenv()

# Konfiguracja strony
st.set_page_config(page_title="Mapa zdjęć", layout="wide", page_icon="🌍")

# Niestandardowy CSS
st.markdown("""
<style>
    div[data-testid="stVerticalBlock"] div[data-testid="stVerticalBlock"] div[data-testid="stVerticalBlock"] {
        border: 1px solid #ddd; border-radius: 8px; padding: 15px; box-shadow: 0 2px 4px rgba(0,0,0,0.1);
    }
    .photo-info { margin-top: 10px; }
</style>
""", unsafe_allow_html=True)

st.title("🗺️ Mapa zdjęć")

# <<< SEKCJA 1: UPROSZCZONE I BEZPIECZNE FUNKCJE DOSTĘPU DO AZURE >>>

@st.cache_resource
def get_azure_credential():
    """Pobiera i cache'uje obiekt poświadczeń Azure."""
    return DefaultAzureCredential(exclude_interactive_browser_credential=False)

@st.cache_resource
def get_blob_service_client():
    """Tworzy i cache'uje klienta Blob Service Client używając Entra ID."""
    account_url = os.getenv("AZURE_STORAGE_ACCOUNT_URL")
    if not account_url:
        st.error("Brak zmiennej środowiskowej AZURE_STORAGE_ACCOUNT_URL.")
        return None
    credential = get_azure_credential()
    return BlobServiceClient(account_url=account_url, credential=credential)

def upload_photo_to_blob(file_bytes, filename):
    """Przesyła plik do Blob Storage używając Entra ID."""
    container_name = os.getenv("CONTAINER_NAME", "photos")
    blob_service_client = get_blob_service_client()
    if not blob_service_client:
        return None
    try:
        blob_client = blob_service_client.get_blob_client(container=container_name, blob=filename)
        blob_client.upload_blob(file_bytes, overwrite=True)
        return blob_client.url
    except Exception as e:
        st.error(f"Błąd podczas przesyłania do Blob Storage: {e}")
        return None

@st.cache_data(ttl=3540) # Cache na 59 minut (klucz delegowania jest ważny 60 min)
def get_blob_with_user_delegation_sas(blob_url: str) -> str:
    """Generuje URL z tokenem User Delegation SAS dla prywatnego bloba."""
    if not blob_url:
        return None
        
    blob_service_client = get_blob_service_client()
    if not blob_service_client:
        return None

    try:
        # 1. Uzyskaj klucz delegowania użytkownika
        delegation_key_start_time = datetime.utcnow()
        delegation_key_expiry_time = delegation_key_start_time + timedelta(hours=1)
        user_delegation_key = blob_service_client.get_user_delegation_key(
            key_start_time=delegation_key_start_time,
            key_expiry_time=delegation_key_expiry_time
        )

        # 2. Wyodrębnij nazwę kontenera i bloba z URL
        path_parts = blob_url.split(f"{blob_service_client.account_name}.blob.core.windows.net/")[1].split('/', 1)
        container_name = path_parts[0]
        blob_name = path_parts[1]

        # 3. Wygeneruj token SAS używając klucza delegowania
        sas_token = generate_blob_sas(
            account_name=blob_service_client.account_name,
            container_name=container_name,
            blob_name=blob_name,
            user_delegation_key=user_delegation_key,
            permission="r", # Tylko odczyt (read)
            expiry=delegation_key_expiry_time
        )
        
        return f"{blob_url}?{sas_token}"
    except Exception as e:
        st.error(f"Błąd generowania User Delegation SAS: {e}. Upewnij się, że tożsamość ma rolę 'Storage Blob Delegator'.")
        return None

def get_sql_connection():
    """Nawiązuje połączenie z Azure SQL DB używając wyłącznie Entra ID."""
    server = os.getenv("SQL_SERVER")
    database = os.getenv("SQL_DATABASE")
    driver = '{ODBC Driver 18 for SQL Server}'
    
    if not all([server, database]):
        st.error("Brak zdefiniowanych zmiennych SQL_SERVER lub SQL_DATABASE.")
        return None
    try:
        credential = get_azure_credential()
        token_object = credential.get_token("https://database.windows.net/.default")
        token_bytes = token_object.token.encode("UTF-16-LE")
        token_struct = struct.pack(f"=I{len(token_bytes)}s", len(token_bytes), token_bytes)
        
        conn_str = f"DRIVER={driver};SERVER={server};DATABASE={database};Encrypt=yes;TrustServerCertificate=no;Connection Timeout=30;"
        conn = pyodbc.connect(conn_str, attrs_before={1256: token_struct})
        return conn
    except Exception as e:
        st.error(f"Błąd połączenia z bazą danych (Entra ID): {e}")
        return None

# --- Pozostałe funkcje pomocnicze (bez zmian) ---
def convert_to_degrees(value, ref):
    degrees = value[0] + value[1] / 60 + value[2] / 3600
    if ref in ['S', 'W']: degrees = -degrees
    return degrees

def get_exif_data(uploaded_file):
    try:
        img = ExifImage(uploaded_file)
        exif_data = {'date_taken': None, 'coordinates': None}
        if hasattr(img, 'datetime_original'):
            try:
                exif_data['date_taken'] = datetime.strptime(img.datetime_original, '%Y:%m:%d %H:%M:%S')
            except (ValueError, TypeError): pass
        if hasattr(img, 'gps_latitude') and hasattr(img, 'gps_longitude') and img.gps_latitude:
            lat = convert_to_degrees(img.gps_latitude, img.gps_latitude_ref)
            lon = convert_to_degrees(img.gps_longitude, img.gps_longitude_ref)
            exif_data['coordinates'] = (lat, lon)
        return exif_data
    except Exception:
        return {'date_taken': None, 'coordinates': None}

def initialize_database():
    conn = get_sql_connection()
    if conn:
        with conn.cursor() as cursor:
            cursor.execute("""
              IF OBJECT_ID('dbo.photos', 'U') IS NULL
              BEGIN
                CREATE TABLE dbo.photos (id INT IDENTITY(1,1) PRIMARY KEY, filename NVARCHAR(255), latitude FLOAT, longitude FLOAT, blob_url NVARCHAR(MAX), date_taken DATETIME, upload_time DATETIME DEFAULT GETDATE());
              END
            """)
            conn.commit()

def save_photo_metadata(filename, latitude, longitude, blob_url, date_taken=None):
    conn = get_sql_connection()
    if conn:
        with conn.cursor() as cursor:
            cursor.execute("INSERT INTO dbo.photos (filename, latitude, longitude, blob_url, date_taken) VALUES (?, ?, ?, ?, ?);", filename, latitude, longitude, blob_url, date_taken)
            conn.commit()
            return True
    return False

def execute_sql_query(query, params=None):
    conn = get_sql_connection()
    if conn:
        try:
            return pd.read_sql(query, conn, params=params)
        except pyodbc.Error as e:
            st.error(f"Błąd zapytania SQL: {e}")
    return pd.DataFrame()

# --- Inicjalizacja i logika aplikacji ---
initialize_database()

if "current_page" not in st.session_state: st.session_state.current_page = "map"
if "clicked_location" not in st.session_state: st.session_state.clicked_location = None
if "selected_photo_from_map" not in st.session_state: st.session_state.selected_photo_from_map = None

col1, col2, col3 = st.columns(3)
if col1.button("📤 Prześlij zdjęcie", use_container_width=True): st.session_state.current_page = "upload"; st.rerun()
if col2.button("🗺️ Zobacz mapę", use_container_width=True): st.session_state.current_page = "map"; st.rerun()
if col3.button("📋 Galeria zdjęć", use_container_width=True): st.session_state.current_page = "list"; st.rerun()

# --- Strona Przesyłania ---
if st.session_state.current_page == "upload":
    # (reszta kodu tej strony jest identyczna, nie ma potrzeby jej zmieniać)
    st.subheader("Prześlij nowe zdjęcie")
    uploaded_file = st.file_uploader("Wybierz zdjęcie (jpg/jpeg)", type=["jpg", "jpeg"])
    if uploaded_file:
        st.image(uploaded_file, caption="Podgląd zdjęcia", use_container_width=True)
        file_bytes = uploaded_file.getvalue()
        exif_data = get_exif_data(file_bytes)
        lat, lon = exif_data['coordinates'] if exif_data['coordinates'] else (None, None)
        date_taken = exif_data['date_taken']
        if date_taken: st.info(f"Data z EXIF: {date_taken.strftime('%Y-%m-%d %H:%M:%S')}")
        if lat and lon:
            st.success(f"Lokalizacja z EXIF: {lat:.6f}, {lon:.6f}")
            st.session_state.clicked_location = {"lat": lat, "lng": lon}
        else:
            st.warning("Brak GPS w EXIF. Wybierz lokalizację na mapie.")
            m = folium.Map(location=[52, 19], zoom_start=6)
            if st.session_state.clicked_location:
                folium.Marker([st.session_state.clicked_location["lat"], st.session_state.clicked_location["lng"]], popup="Wybrana lokalizacja", icon=folium.Icon(color="green")).add_to(m)
            output = st_folium(m, height=400, width=700, returned_objects=["last_clicked"])
            if output and output["last_clicked"]:
                st.session_state.clicked_location = output["last_clicked"]
                st.rerun()
        if st.session_state.clicked_location:
            lat = st.session_state.clicked_location["lat"]
            lon = st.session_state.clicked_location["lng"]
            st.success(f"Używana lokalizacja: {lat:.6f}, {lon:.6f}")
            if st.button("Zapisz zdjęcie", key="save_btn"):
                with st.spinner("Przesyłanie i zapisywanie danych..."):
                    blob_url = upload_photo_to_blob(file_bytes, uploaded_file.name)
                    if blob_url and save_photo_metadata(uploaded_file.name, lat, lon, blob_url, date_taken):
                        st.success("Zdjęcie zostało pomyślnie zapisane!")
                        st.session_state.clicked_location = None
                    else:
                        st.error("Nie udało się zapisać metadanych zdjęcia.")
        else:
            st.info("Oczekuję na wybór lokalizacji na mapie...")


# --- Strona Mapy ---
elif st.session_state.current_page == "map":
    st.subheader("Mapa zdjęć")
    df = execute_sql_query("SELECT id, filename, latitude, longitude, blob_url, date_taken, upload_time FROM dbo.photos ORDER BY upload_time DESC")
    if not df.empty:
        st.session_state.map_df = df
        map_col, preview_col = st.columns([2, 1])
        with map_col:
            m = folium.Map(location=[df['latitude'].mean(), df['longitude'].mean()], zoom_start=6)
            for _, row in df.iterrows():
                folium.Marker([row['latitude'], row['longitude']], tooltip=row['filename'], icon=folium.Icon(color="blue", icon="camera", prefix="fa")).add_to(m)
            map_data = st_folium(m, height=600, use_container_width=True, returned_objects=["last_object_clicked_tooltip"])
            if map_data and map_data.get("last_object_clicked_tooltip"):
                clicked_filename = map_data["last_object_clicked_tooltip"]
                selected_row = df[df['filename'] == clicked_filename]
                if not selected_row.empty:
                    st.session_state.selected_photo_from_map = selected_row.iloc[0].to_dict()
        with preview_col:
            st.subheader("Podgląd zdjęcia")
            if st.session_state.selected_photo_from_map:
                photo_data = st.session_state.selected_photo_from_map
                photo_url = str(photo_data.get('blob_url', ''))
                if photo_url:
                    # <<< UŻYCIE NOWEJ FUNKCJI SAS >>>
                    sas_url = get_blob_with_user_delegation_sas(photo_url)
                    if sas_url:
                        st.image(sas_url, caption=photo_data.get('filename'), use_container_width=True)
                        st.write(f"**Nazwa pliku:** {photo_data.get('filename')}")
                        date_str = photo_data.get('date_taken').strftime('%Y-%m-%d %H:%M:%S') if pd.notnull(photo_data.get('date_taken')) else 'Brak danych'
                        st.write(f"**Data wykonania:** {date_str}")
                        st.write(f"**Współrzędne:** {photo_data.get('latitude'):.6f}, {photo_data.get('longitude'):.6f}")
                        st.markdown(f"[Otwórz w nowej karcie]({sas_url})", unsafe_allow_html=True)
                    else:
                        st.error("Nie udało się pobrać bezpiecznego linku do zdjęcia.")
                else:
                    st.warning("Brak URL zdjęcia w bazie danych.")
            else:
                st.info("Kliknij na znacznik na mapie, aby zobaczyć szczegóły.")
    else:
        st.info("Brak zdjęć w bazie. Prześlij pierwsze!")

# --- Strona Galerii ---
elif st.session_state.current_page == "list":
    st.subheader("Galeria zdjęć z filtrowaniem")
    filter_col1, filter_col2 = st.columns(2)
    with filter_col1: search_term = st.text_input("Szukaj po nazwie pliku:")
    with filter_col2: date_range = st.date_input("Filtruj po dacie wykonania:", value=())
    
    query = "SELECT filename, latitude, longitude, blob_url, date_taken FROM dbo.photos WHERE 1=1"
    params = []
    if search_term:
        query += " AND filename LIKE ?"
        params.append(f"%{search_term}%")
    if len(date_range) == 2:
        start_datetime = datetime.combine(date_range[0], time.min)
        end_datetime = datetime.combine(date_range[1], time.max)
        query += " AND date_taken BETWEEN ? AND ?"
        params.extend([start_datetime, end_datetime])
    query += " ORDER BY date_taken DESC"
    
    df = execute_sql_query(query, params=params)

    if not df.empty:
        st.write(f"Znaleziono: {len(df)} zdjęć.")
        for i in range(0, len(df), 3):
            cols = st.columns(3)
            for j in range(3):
                if i + j < len(df):
                    with cols[j]:
                        row = df.iloc[i + j]
                        with st.container():
                            # <<< UŻYCIE NOWEJ FUNKCJI SAS >>>
                            sas_url = get_blob_with_user_delegation_sas(str(row['blob_url']))
                            if sas_url:
                                st.image(sas_url, caption=f"Lat: {row['latitude']:.2f}, Lon: {row['longitude']:.2f}", use_container_width=True)
                            else:
                                st.warning("Brak linku do zdjęcia")
                            st.write(f"**{row['filename']}**")
                            date_str = row['date_taken'].strftime('%Y-%m-%d') if pd.notnull(row['date_taken']) else 'Brak daty'
                            st.caption(f"Data: {date_str}")
    else:
        st.warning("Nie znaleziono zdjęć spełniających kryteria.")
