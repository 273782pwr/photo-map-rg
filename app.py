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
from datetime import datetime
from dotenv import load_dotenv
import struct  # <<< KROK 1: Zaimportuj moduł struct

load_dotenv()

# Konfiguracja strony
st.set_page_config(page_title="Mapa zdjęć", layout="wide", page_icon="🌍")

# Niestandardowy CSS
st.markdown("""
<style>
    /* Twoje style CSS bez zmian */
    .nav-button { font-family: 'Arial', sans-serif; font-size: 16px; font-weight: bold; margin: 5px; padding: 10px 20px; border-radius: 5px; background-color: #f0f2f6; color: #262730; }
    .nav-button:hover { background-color: #d0d2d6; }
    .photo-card { border: 1px solid #ddd; border-radius: 8px; padding: 15px; margin-bottom: 20px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
    .photo-info { margin-top: 10px; }
    .photo-preview { max-width: 100%; border-radius: 8px; margin-top: 10px; }
    .map-container { margin-bottom: 20px; }
</style>
""", unsafe_allow_html=True)

st.title("🗺️ Mapa zdjęć")


# --- Funkcje pomocnicze ---
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
            except (ValueError, TypeError):
                pass
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
        st.error("Brak ustawionej zmiennej środowiskowej AZURE_STORAGE_CONNECTION_STRING")
        return None
    try:
        blob_service_client = BlobServiceClient.from_connection_string(connect_str)
        blob_client = blob_service_client.get_blob_client(container=container_name, blob=filename)
        blob_client.upload_blob(file_bytes, overwrite=True)
        return blob_client.url
    except Exception as e:
        st.error(f"Błąd podczas przesyłania do Blob Storage: {e}")
        return None

# <<< KROK 2: Stwórz funkcję do nawiązywania połączenia z SQL DB, aby uniknąć powtórzeń
def get_sql_connection():
    """Nawiązuje połączenie z Azure SQL DB używając Managed Identity."""
    server = os.getenv("SQL_SERVER")
    database = os.getenv("SQL_DATABASE")
    driver = '{ODBC Driver 18 for SQL Server}'
    
    if not all([server, database]):
        st.error("Brak skonfigurowanych zmiennych SQL_SERVER lub SQL_DATABASE.")
        return None

    try:
        credential = DefaultAzureCredential(exclude_interactive_browser_credential=False)
        token_object = credential.get_token("https://database.windows.net/.default")
        token_bytes = token_object.token.encode("UTF-16-LE")
        
        # <<< KROK 3: Prawidłowo spakuj token do struktury binarnej
        token_struct = struct.pack(f"=I{len(token_bytes)}s", len(token_bytes), token_bytes)

        conn_str = (
            f"DRIVER={driver};"
            f"SERVER={server};"
            f"DATABASE={database};"
            "Encrypt=yes;TrustServerCertificate=no;Connection Timeout=30;"
        )
        
        conn = pyodbc.connect(conn_str, attrs_before={1256: token_struct})
        return conn
    except Exception as e:
        st.error(f"Błąd połączenia z bazą danych: {e}")
        # Wypisz bardziej szczegółowe informacje w konsoli dla dewelopera
        print(f"Szczegóły błędu połączenia z bazą: {e}")
        return None

def save_photo_metadata(filename, latitude, longitude, blob_url, date_taken=None):
    conn = get_sql_connection()
    if conn:
        with conn:
            cursor = conn.cursor()
            # Użyj jednego zapytania do sprawdzenia i wstawienia danych
            # Ta składnia z IF NOT EXISTS... CREATE TABLE działa w SQL Server
            cursor.execute("""
              IF OBJECT_ID('dbo.photos', 'U') IS NULL
              BEGIN
                CREATE TABLE photos (
                  id INT IDENTITY(1,1) PRIMARY KEY,
                  filename NVARCHAR(255),
                  latitude FLOAT,
                  longitude FLOAT,
                  blob_url NVARCHAR(MAX),
                  date_taken DATETIME,
                  upload_time DATETIME DEFAULT GETDATE()
                );
              END
            """)
            cursor.execute("""
              INSERT INTO photos(filename, latitude, longitude, blob_url, date_taken)
              VALUES (?, ?, ?, ?, ?);
            """, filename, latitude, longitude, blob_url, date_taken)
            conn.commit()
            return True
    return False

def execute_sql_query(query, params=None):
    conn = get_sql_connection()
    if conn:
        with conn:
            df = pd.read_sql(query, conn, params=params)
            return df
    return pd.DataFrame() # Zwróć pusty DataFrame w przypadku błędu


# --- Stan sesji ---
if "current_page" not in st.session_state:
    st.session_state.current_page = "map" # Domyślnie mapa
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

# --- Strony aplikacji ---

if st.session_state.current_page == "upload":
    st.subheader("Prześlij nowe zdjęcie")
    uploaded_file = st.file_uploader("Wybierz zdjęcie (jpg/jpeg)", type=["jpg", "jpeg"])

    if uploaded_file:
        st.image(uploaded_file, caption="Podgląd zdjęcia", use_container_width=True)
        file_bytes = uploaded_file.getvalue() # Odczytaj bajty raz
        exif_data = get_exif_data(file_bytes)
        lat, lon = exif_data['coordinates'] if exif_data['coordinates'] else (None, None)
        date_taken = exif_data['date_taken']

        if date_taken:
            st.info(f"Data wykonania zdjęcia (z EXIF): {date_taken.strftime('%Y-%m-%d %H:%M:%S')}")
        
        if lat and lon:
            st.success(f"Odczytano lokalizację z EXIF: {lat:.6f}, {lon:.6f}")
            st.session_state.clicked_location = {"lat": lat, "lng": lon}
        else:
            st.warning("Nie znaleziono danych GPS w EXIF. Wybierz lokalizację klikając na mapie.")
            m = folium.Map(location=[52, 19], zoom_start=6)
            
            # Dodaj marker, jeśli lokalizacja została już kliknięta
            if st.session_state.clicked_location:
                folium.Marker(
                    [st.session_state.clicked_location["lat"], st.session_state.clicked_location["lng"]],
                    popup="Wybrana lokalizacja",
                    icon=folium.Icon(color="green")
                ).add_to(m)

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
                    if blob_url:
                        if save_photo_metadata(uploaded_file.name, lat, lon, blob_url, date_taken):
                            st.success(f"Zdjęcie zostało pomyślnie zapisane!")
                            st.session_state.clicked_location = None # Resetuj kliknięcie
                        else:
                            st.error("Nie udało się zapisać metadanych zdjęcia w bazie.")
        else:
            st.info("Oczekuję na wybór lokalizacji na mapie...")


elif st.session_state.current_page == "map":
    df = execute_sql_query("SELECT id, filename, latitude, longitude, blob_url, date_taken, upload_time FROM photos ORDER BY upload_time DESC")

    if not df.empty:
        map_col, preview_col = st.columns([2, 1])

        with map_col:
            st.subheader("Mapa zdjęć")
            m = folium.Map(location=[df['latitude'].mean(), df['longitude'].mean()], zoom_start=6)
            
            for _, row in df.iterrows():
                popup_html = f"""
                <div style="width: 200px;">
                    <h5 style="margin:0;">{row['filename']}</h5>
                    <img src="{row['blob_url']}" style="width:100%;">
                </div>
                """
                folium.Marker(
                    [row['latitude'], row['longitude']],
                    popup=folium.Popup(popup_html, max_width=250),
                    tooltip=row['filename'],
                    # Przekazanie ID w obiekcie, niestety st_folium tego nie zwraca
                ).add_to(m)

            map_data = st_folium(m, height=600, use_container_width=True, returned_objects=["last_object_clicked_tooltip"])
            
            if map_data and map_data.get("last_object_clicked_tooltip"):
                clicked_filename = map_data["last_object_clicked_tooltip"]
                selected_row = df[df['filename'] == clicked_filename].iloc[0]
                st.session_state.selected_photo_id = selected_row['id']
                st.rerun()

        with preview_col:
            st.subheader("Podgląd zdjęcia")
            if st.session_state.selected_photo_id is not None:
                photo = df[df['id'] == st.session_state.selected_photo_id].iloc[0]
                st.image(photo['blob_url'], use_column_width=True)
                st.write(f"**Nazwa:** {photo['filename']}")
                date_str = photo['date_taken'].strftime('%Y-%m-%d %H:%M:%S') if pd.notnull(photo['date_taken']) else 'Brak danych'
                st.write(f"**Data wykonania:** {date_str}")
                st.write(f"**Współrzędne:** {photo['latitude']:.6f}, {photo['longitude']:.6f}")
                st.markdown(f"[Otwórz w nowej karcie]({photo['blob_url']})")
            else:
                st.info("Kliknij na znacznik na mapie, aby zobaczyć szczegóły.")
    else:
        st.info("Brak zdjęć w bazie danych. Prześlij pierwsze zdjęcie!")

elif st.session_state.current_page == "list":
    st.subheader("Galeria zdjęć")
    df = execute_sql_query("SELECT filename, latitude, longitude, blob_url, date_taken FROM photos ORDER BY date_taken DESC")
    if not df.empty:
        for _, row in df.iterrows():
            st.markdown(f"""
            <div class="photo-card">
                <img src="{row['blob_url']}" alt="{row['filename']}" class="photo-preview">
                <div class="photo-info">
                    <b>{row['filename']}</b><br>
                    Data wykonania: {row['date_taken'].strftime('%Y-%m-%d') if pd.notnull(row['date_taken']) else 'Brak'}<br>
                    Lokalizacja: {row['latitude']:.4f}, {row['longitude']:.4f}
                </div>
            </div>
            """, unsafe_allow_html=True)
    else:
        st.info("Brak zdjęć do wyświetlenia.")
