import streamlit as st
from streamlit_folium import st_folium
import folium
import os
import pandas as pd
from PIL import Image
from exif import Image as ExifImage
from azure.storage.blob import BlobServiceClient
from azure.identity import DefaultAzureCredential
import pyodbc
from datetime import datetime
import io

# Konfiguracja Azure Storage
CONTAINER_NAME = os.getenv("CONTAINER_NAME")
STORAGE_CONNECTION_STRING = os.getenv("AZURE_STORAGE_CONNECTION_STRING")

# Funkcja do przesyłania zdjęcia do Blob Storage
def upload_to_blob(photo_bytes, filename):
    blob_service_client = BlobServiceClient.from_connection_string(STORAGE_CONNECTION_STRING)
    blob_client = blob_service_client.get_blob_client(container=CONTAINER_NAME, blob=filename)
    blob_client.upload_blob(photo_bytes, overwrite=True)
    return blob_client.url

# Pobierz lokalizację z EXIF (jeśli dostępna)
def get_exif_location(image):
    try:
        exif_img = ExifImage(image)
        if exif_img.has_exif:
            if exif_img.gps_latitude and exif_img.gps_longitude:
                lat = convert_to_degrees(exif_img.gps_latitude, exif_img.gps_latitude_ref)
                lon = convert_to_degrees(exif_img.gps_longitude, exif_img.gps_longitude_ref)
                return lat, lon
    except Exception:
        return None
    return None

def convert_to_degrees(value, ref):
    degrees = value[0] + value[1] / 60 + value[2] / 3600
    if ref in ["S", "W"]:
        degrees = -degrees
    return degrees

# Odczytaj datę wykonania ze zdjęcia (jeśli dostępna)
def get_photo_taken_date(image):
    try:
        exif_img = ExifImage(image)
        if hasattr(exif_img, "datetime_original"):
            return datetime.strptime(exif_img.datetime_original, "%Y:%m:%d %H:%M:%S")
    except Exception:
        return None
    return None

# Połączenie z bazą danych SQL za pomocą Managed Identity
def get_sql_connection():
    server = os.getenv("SQL_SERVER")
    database = os.getenv("SQL_DATABASE")
    driver = '{ODBC Driver 18 for SQL Server}'

    credential = DefaultAzureCredential(exclude_interactive_browser_credential=True)
    token = credential.get_token("https://database.windows.net/.default").token
    access_token = bytes(token, 'utf-8')

    conn_str = f"DRIVER={driver};SERVER={server};DATABASE={database};Encrypt=yes;TrustServerCertificate=no;Authentication=ActiveDirectoryAccessToken;"

    return pyodbc.connect(conn_str, attrs_before={1256: access_token})

# Zapisz metadane do bazy
def save_photo_metadata(filename, latitude, longitude, blob_url, date_taken=None):
    with get_sql_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            IF NOT EXISTS (
                SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = 'photos'
            )
            CREATE TABLE photos (
                id UNIQUEIDENTIFIER DEFAULT NEWID() PRIMARY KEY,
                filename NVARCHAR(255),
                latitude FLOAT,
                longitude FLOAT,
                blob_url NVARCHAR(2083),
                date_taken DATETIME,
                upload_time DATETIME DEFAULT GETDATE()
            );

            INSERT INTO photos (filename, latitude, longitude, blob_url, date_taken)
            VALUES (?, ?, ?, ?, ?);
        """, filename, latitude, longitude, blob_url, date_taken)
        conn.commit()

# Wykonaj zapytanie SQL i zwróć DataFrame
def execute_sql_query(query):
    try:
        with get_sql_connection() as conn:
            df = pd.read_sql(query, conn)
        return df
    except Exception as e:
        st.error(f"Błąd wykonania zapytania: {str(e)}")
        return None

# UI aplikacji
st.set_page_config(page_title="Mapa zdjęć", layout="wide")
st.title("🗺️ Mapa zdjęć")

# Sekcja uploadu zdjęcia
with st.expander("📤 Prześlij zdjęcie", expanded=True):
    uploaded_file = st.file_uploader("Wybierz zdjęcie", type=["jpg", "jpeg"])
    if uploaded_file:
        image_bytes = uploaded_file.read()
        filename = uploaded_file.name
        location = get_exif_location(io.BytesIO(image_bytes))
        date_taken = get_photo_taken_date(io.BytesIO(image_bytes))

        st.image(image_bytes, caption="Podgląd zdjęcia", use_column_width=True)

        if location:
            lat, lon = location
            st.success(f"📍 Wykryto lokalizację: ({lat:.5f}, {lon:.5f})")
        else:
            st.warning("Nie wykryto lokalizacji w metadanych EXIF. Wybierz ręcznie.")
            lat = st.number_input("Szerokość (latitude)", format="%.6f")
            lon = st.number_input("Długość (longitude)", format="%.6f")

        if st.button("Zapisz zdjęcie"):
            if lat and lon:
                blob_url = upload_to_blob(image_bytes, filename)
                save_photo_metadata(filename, lat, lon, blob_url, date_taken)
                st.success("✅ Zdjęcie zostało zapisane!")
            else:
                st.error("❌ Lokalizacja jest wymagana.")

# Wyświetlenie mapy ze zdjęciami
st.subheader("🌍 Galeria zdjęć na mapie")

photo_df = execute_sql_query("SELECT filename, latitude, longitude, blob_url, date_taken FROM photos")

if photo_df is not None and not photo_df.empty:
    m = folium.Map(location=[photo_df.latitude.mean(), photo_df.longitude.mean()], zoom_start=4)
    for _, row in photo_df.iterrows():
        popup_content = f"<b>{row['filename']}</b><br><a href='{row['blob_url']}' target='_blank'>Zobacz zdjęcie</a>"
        if pd.notnull(row['date_taken']):
            popup_content += f"<br>📅 Data: {row['date_taken']}"
        folium.Marker(
            location=[row["latitude"], row["longitude"]],
            popup=popup_content,
            icon=folium.Icon(color="blue", icon="camera", prefix="fa")
        ).add_to(m)
    st_folium(m, width=1000, height=600)
else:
    st.info("Brak zdjęć do wyświetlenia.")
