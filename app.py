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
from azure.storage.blob import BlobServiceClient
from datetime import datetime, time
from dotenv import load_dotenv
import struct

load_dotenv()

# Konfiguracja strony
st.set_page_config(page_title="Mapa zdjęć", layout="wide", page_icon="🌍")

# Niestandardowy CSS
st.markdown("""
<style>
    /* Lepszy styl dla kart zdjęć w galerii */
    div[data-testid="stVerticalBlock"] div[data-testid="stVerticalBlock"] div[data-testid="stVerticalBlock"] {
        border: 1px solid #ddd;
        border-radius: 8px;
        padding: 15px;
        box-shadow: 0 2px 4px rgba(0,0,0,0.1);
    }
    .photo-info {
        margin-top: 10px;
    }
</style>
""", unsafe_allow_html=True)

st.title("🗺️ Mapa zdjęć")


# --- Funkcje pomocnicze (bez zmian) ---
def convert_to_degrees(value, ref):
    degrees = value[0] + value[1] / 60 + value[2] / 3600
    if ref in ['S', 'W']:
        degrees = -degrees
    return degrees

def get_exif_data(uploaded_file):
    try:
        img = ExifImage(uploaded_file)
        exif_data = {'date_taken': None, 'coordinates': None}
        if hasattr(img, 'datetime_original'):
            try:
                date_str = img.datetime_original
                exif_data['date_taken'] = datetime.strptime(date_str, '%Y:%m:%d %H:%M:%S')
            except (ValueError, TypeError): pass
        if hasattr(img, 'gps_latitude') and hasattr(img, 'gps_longitude'):
            if img.gps_latitude and img.gps_longitude:
                lat = convert_to_degrees(img.gps_latitude, img.gps_latitude_ref)
                lon = convert_to_degrees(img.gps_longitude, img.gps_longitude_ref)
                exif_data['coordinates'] = (lat, lon)
        return exif_data
    except Exception:
        return {'date_taken': None, 'coordinates': None}

def upload_photo_to_blob(file_bytes, filename):
    container_name = os.getenv("CONTAINER_NAME", "photos")
    connect_str = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
    if not connect_str:
        st.error("Brak AZURE_STORAGE_CONNECTION_STRING")
        return None
    try:
        blob_service_client = BlobServiceClient.from_connection_string(connect_str)
        blob_client = blob_service_client.get_blob_client(container=container_name, blob=filename)
        blob_client.upload_blob(file_bytes, overwrite=True)
        return blob_client.url
    except Exception as e:
        st.error(f"Błąd Blob Storage: {e}")
        return None

def get_sql_connection():
    server = os.getenv("SQL_SERVER")
    database = os.getenv("SQL_DATABASE")
    driver = '{ODBC Driver 18 for SQL Server}'
    if not all([server, database]):
        st.error("Brak SQL_SERVER lub SQL_DATABASE.")
        return None
    try:
        credential = DefaultAzureCredential(exclude_interactive_browser_credential=False)
        token_object = credential.get_token("https://database.windows.net/.default")
        token_bytes = token_object.token.encode("UTF-16-LE")
        token_struct = struct.pack(f"=I{len(token_bytes)}s", len(token_bytes), token_bytes)
        conn_str = f"DRIVER={driver};SERVER={server};DATABASE={database};Encrypt=yes;TrustServerCertificate=no;Connection Timeout=30;"
        conn = pyodbc.connect(conn_str, attrs_before={1256: token_struct})
        return conn
    except Exception as e:
        st.error(f"Błąd połączenia z bazą: {e}")
        return None

def initialize_database():
    conn = get_sql_connection()
    if conn:
        with conn:
            cursor = conn.cursor()
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
        with conn:
            cursor = conn.cursor()
            cursor.execute("INSERT INTO dbo.photos (filename, latitude, longitude, blob_url, date_taken) VALUES (?, ?, ?, ?, ?);", filename, latitude, longitude, blob_url, date_taken)
            conn.commit()
            return True
    return False

def execute_sql_query(query, params=None):
    conn = get_sql_connection()
    if conn:
        with conn:
            try:
                df = pd.read_sql(query, conn, params=params)
                return df
            except pyodbc.Error as e:
                st.error(f"Błąd zapytania SQL: {e}")
                return pd.DataFrame()
    return pd.DataFrame()

initialize_database()

# --- Stan sesji ---
if "current_page" not in st.session_state:
    st.session_state.current_page = "map"
if "clicked_location" not in st.session_state:
    st.session_state.clicked_location = None
if "selected_photo_id" not in st.session_state:
    st.session_state.selected_photo_id = None

# Nawigacja
col1, col2, col3 = st.columns(3)
if col1.button("📤 Prześlij zdjęcie", use_container_width=True):
    st.session_state.current_page = "upload"
    st.rerun()
if col2.button("🗺️ Zobacz mapę", use_container_width=True):
    st.session_state.current_page = "map"
    st.rerun()
if col3.button("📋 Galeria zdjęć", use_container_width=True):
    st.session_state.current_page = "list"
    st.rerun()

# --- Strona Przesyłania (bez większych zmian) ---
if st.session_state.current_page == "upload":
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
                        st.error("Nie udało się zapisać metadanych zdjęcia w bazie.")
        else:
            st.info("Oczekuję na wybór lokalizacji na mapie...")

# --- Strona Mapy (duże zmiany) ---
elif st.session_state.current_page == "map":
    st.subheader("Mapa zdjęć")
    df = execute_sql_query("SELECT id, filename, latitude, longitude, blob_url, date_taken, upload_time FROM dbo.photos ORDER BY upload_time DESC")

    if not df.empty:
        map_col, preview_col = st.columns([2, 1])

        with map_col:
            # Utwórz mapę tylko raz i przechowaj w stanie sesji, aby uniknąć przeładowań
            if 'map' not in st.session_state:
                 st.session_state.map = folium.Map(location=[df['latitude'].mean(), df['longitude'].mean()], zoom_start=6)
                 for _, row in df.iterrows():
                    # <<< NAPRAWA 1: Uproszczony popup i dodana ikona kamery
                    folium.Marker(
                        [row['latitude'], row['longitude']],
                        tooltip=row['filename'],  # Tooltip (podpowiedź) jest lepszy niż popup
                        icon=folium.Icon(color="blue", icon="camera", prefix="fa")
                    ).add_to(st.session_state.map)

            # Wyświetl mapę i przechwyć kliknięcia
            map_data = st_folium(
                st.session_state.map,
                height=600,
                use_container_width=True,
                returned_objects=["last_object_clicked_tooltip"]
            )

            # <<< NAPRAWA 2: Logika zapobiegająca migotaniu (pętli rerun)
            if map_data and map_data.get("last_object_clicked_tooltip"):
                clicked_filename = map_data["last_object_clicked_tooltip"]
                # Znajdź ID klikniętego zdjęcia
                selected_row = df[df['filename'] == clicked_filename].iloc[0]
                newly_selected_id = int(selected_row['id'])

                # Uruchom ponownie tylko, jeśli wybrano inne zdjęcie
                if st.session_state.get('selected_photo_id') != newly_selected_id:
                    st.session_state.selected_photo_id = newly_selected_id
                    st.rerun()

        # <<< NAPRAWA 3: Panel podglądu zdjęcia działa teraz poprawnie
        with preview_col:
            st.subheader("Podgląd zdjęcia")
            if st.session_state.selected_photo_id is not None:
                # Pobierz dane wybranego zdjęcia z DataFrame
                photo_data = df[df['id'] == st.session_state.selected_photo_id].iloc[0]
                st.image(photo_data['blob_url'], caption=photo_data['filename'], use_column_width=True)
                st.write(f"**Nazwa pliku:** {photo_data['filename']}")
                date_str = photo_data['date_taken'].strftime('%Y-%m-%d %H:%M:%S') if pd.notnull(photo_data['date_taken']) else 'Brak danych'
                st.write(f"**Data wykonania:** {date_str}")
                st.write(f"**Współrzędne:** {photo_data['latitude']:.6f}, {photo_data['longitude']:.6f}")
                st.markdown(f"[Otwórz w nowej karcie]({photo_data['blob_url']})", unsafe_allow_html=True)
            else:
                st.info("Kliknij na znacznik (kamerę) na mapie, aby zobaczyć szczegóły zdjęcia.")
    else:
        st.info("Brak zdjęć w bazie danych. Prześlij pierwsze zdjęcie!")

# --- Strona Galerii (duże zmiany) ---
elif st.session_state.current_page == "list":
    st.subheader("Galeria zdjęć z filtrowaniem")

    # <<< NAPRAWA 4: Interaktywne filtry wykonujące zapytania do SQL
    st.write("Użyj filtrów, aby zawęzić wyniki. Wyniki są sortowane od najnowszych.")
    
    # Utwórz kolumny dla filtrów
    filter_col1, filter_col2 = st.columns(2)
    with filter_col1:
        search_term = st.text_input("Szukaj po nazwie pliku:")
    with filter_col2:
        # Użyj krotki do przekazania zakresu dat
        date_range = st.date_input("Filtruj po dacie wykonania:", value=())

    # Dynamiczne budowanie zapytania SQL
    base_query = "SELECT filename, latitude, longitude, blob_url, date_taken FROM dbo.photos WHERE 1=1"
    params = []

    if search_term:
        base_query += " AND filename LIKE ?"
        params.append(f"%{search_term}%")
    
    if len(date_range) == 2:
        start_date, end_date = date_range
        # Dołącz czas, aby objąć cały dzień
        start_datetime = datetime.combine(start_date, time.min)
        end_datetime = datetime.combine(end_date, time.max)
        base_query += " AND date_taken BETWEEN ? AND ?"
        params.append(start_datetime)
        params.append(end_datetime)

    base_query += " ORDER BY date_taken DESC"
    
    # Wykonaj zbudowane zapytanie
    df = execute_sql_query(base_query, params=params)

    if not df.empty:
        # <<< NAPRAWA 5: Wyświetlanie zdjęć w siatce za pomocą kolumn Streamlit
        st.write(f"Znaleziono: {len(df)} zdjęć.")
        
        # Twórz siatkę po 3 zdjęcia w rzędzie
        for i in range(0, len(df), 3):
            cols = st.columns(3)
            for j in range(3):
                if i + j < len(df):
                    with cols[j]:
                        row = df.iloc[i + j]
                        with st.container(): # Użyj kontenera dla lepszego stylu
                            st.image(row['blob_url'], caption=f"Lat: {row['latitude']:.2f}, Lon: {row['longitude']:.2f}", use_column_width=True)
                            st.write(f"**{row['filename']}**")
                            date_str = row['date_taken'].strftime('%Y-%m-%d') if pd.notnull(row['date_taken']) else 'Brak daty'
                            st.caption(f"Data: {date_str}")
    else:
        st.warning("Nie znaleziono zdjęć spełniających podane kryteria.")
