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

load_dotenv()

# Konfiguracja strony
st.set_page_config(page_title="Mapa zdjęć", layout="wide", page_icon="🌍")

# Niestandardowy CSS (twój oryginalny styl)
st.markdown("""
<style>
    .nav-button {
        font-family: 'Arial', sans-serif;
        font-size: 16px;
        font-weight: bold;
        margin: 5px;
        padding: 10px 20px;
        border-radius: 5px;
        background-color: #f0f2f6;
        color: #262730;
    }
    .nav-button:hover {
        background-color: #d0d2d6;
    }
    .photo-card {
        border: 1px solid #ddd;
        border-radius: 8px;
        padding: 15px;
        margin-bottom: 20px;
        box-shadow: 0 2px 4px rgba(0,0,0,0.1);
    }
    .photo-info {
        margin-top: 10px;
    }
    .photo-preview {
        max-width: 100%;
        border-radius: 8px;
        margin-top: 10px;
    }
    .map-container {
        margin-bottom: 20px;
    }
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
            except:
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
        raise ValueError("Brak ustawionej zmiennej środowiskowej AZURE_STORAGE_CONNECTION_STRING")

    blob_service_client = BlobServiceClient.from_connection_string(connect_str)
    container_client = blob_service_client.get_container_client(container_name)
    blob_client = container_client.get_blob_client(filename)
    blob_client.upload_blob(file_bytes, overwrite=True)
    return blob_client.url


# --- Poprawiona funkcja z połączeniem Managed Identity ---
def save_photo_metadata(filename, latitude, longitude, blob_url, date_taken=None):
    server = os.getenv("SQL_SERVER")
    database = os.getenv("SQL_DATABASE")
    driver = '{ODBC Driver 18 for SQL Server}'

    credential = DefaultAzureCredential()
    token = credential.get_token("https://database.windows.net/.default").token

    conn_str = f"DRIVER={driver};SERVER={server};DATABASE={database};Authentication=ActiveDirectoryAccessToken;Encrypt=yes;TrustServerCertificate=no;Connection Timeout=30;"

    with pyodbc.connect(conn_str, attrs_before={1256: bytes(token, "utf-8")}) as conn:
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


def execute_sql_query(query):
    server = os.getenv("SQL_SERVER")
    database = os.getenv("SQL_DATABASE")
    driver = '{ODBC Driver 18 for SQL Server}'

    credential = DefaultAzureCredential()
    token = credential.get_token("https://database.windows.net/.default").token

    conn_str = f"DRIVER={driver};SERVER={server};DATABASE={database};Authentication=ActiveDirectoryAccessToken;Encrypt=yes;TrustServerCertificate=no;Connection Timeout=30;"

    try:
        with pyodbc.connect(conn_str, attrs_before={1256: bytes(token, "utf-8")}) as conn:
            df = pd.read_sql(query, conn)
        return df
    except Exception as e:
        st.error(f"Błąd wykonania zapytania: {str(e)}")
        return None


# --- Stan sesji ---
if "photo_data" not in st.session_state:
    st.session_state.photo_data = []
if "clicked_location" not in st.session_state:
    st.session_state.clicked_location = None
if "selected_photo" not in st.session_state:
    st.session_state.selected_photo = None

col1, col2, col3 = st.columns(3)
with col1:
    upload_btn = st.button("📤 Prześlij zdjęcie", key="upload_btn", help="Prześlij nowe zdjęcie z lokalizacją")
with col2:
    map_btn = st.button("🗺️ Zobacz mapę", key="map_btn", help="Zobacz wszystkie zdjęcia na mapie")
with col3:
    list_btn = st.button("📋 Galeria zdjęć", key="list_btn", help="Przeglądaj galerię zdjęć")

if upload_btn:
    st.session_state.current_page = "upload"
elif map_btn:
    st.session_state.current_page = "map"
elif list_btn:
    st.session_state.current_page = "list"

if "current_page" not in st.session_state:
    st.session_state.current_page = "upload"

if st.session_state.current_page == "upload":
    uploaded_file = st.file_uploader("Wybierz zdjęcie (jpg/jpeg)", type=["jpg", "jpeg"])

    if uploaded_file:
        st.image(uploaded_file, caption="Podgląd zdjęcia", use_container_width=True)
        exif_data = get_exif_data(uploaded_file)
        lat, lon = exif_data['coordinates'] if exif_data['coordinates'] else (None, None)
        date_taken = exif_data['date_taken']

        if date_taken:
            st.info(f"Data wykonania zdjęcia: {date_taken.strftime('%Y-%m-%d %H:%M:%S')}")
        else:
            st.info("Nie znaleziono daty wykonania w metadanych EXIF. Zostanie użyta data przesłania.")

        if lat and lon:
            st.success(f"Odczytano lokalizację z EXIF: {lat:.6f}, {lon:.6f}")
            st.session_state.clicked_location = {"lat": lat, "lng": lon}
        else:
            st.warning("Wybierz lokalizację klikając na mapie.")
            m = folium.Map(location=[52, 19], zoom_start=5)

            if st.session_state.clicked_location:
                folium.Marker(
                    [st.session_state.clicked_location["lat"], st.session_state.clicked_location["lng"]],
                    popup="Wybrana lokalizacja"
                ).add_to(m)

            output = st_folium(m, height=400, width=700, returned_objects=["last_clicked"])
            if output["last_clicked"]:
                st.session_state.clicked_location = output["last_clicked"]
                st.experimental_rerun()

            if st.session_state.clicked_location:
                lat = st.session_state.clicked_location["lat"]
                lon = st.session_state.clicked_location["lng"]
                st.success(f"Wybrano lokalizację: {lat:.6f}, {lon:.6f}")
            else:
                lat, lon = None, None

        if lat and lon:
            if st.button("Zapisz zdjęcie", key="save_btn"):
                file_bytes = uploaded_file.read()
                blob_url = upload_photo_to_blob(file_bytes, uploaded_file.name)
                save_photo_metadata(uploaded_file.name, lat, lon, blob_url, date_taken)

                st.session_state.photo_data.append({
                    "Nazwa": uploaded_file.name,
                    "Szerokość": lat,
                    "Długość": lon,
                    "URL": blob_url,
                    "Data wykonania": date_taken if date_taken else datetime.now()
                })
                st.success(f"Zdjęcie zapisane! URL: {blob_url}")

elif st.session_state.current_page == "map":
    try:
        df = execute_sql_query("""
            SELECT filename, latitude, longitude, blob_url, date_taken, upload_time 
            FROM photos
            ORDER BY upload_time DESC
        """)

        if df is not None and not df.empty:
            # Kontener na mapę i podgląd zdjęcia
            map_col, preview_col = st.columns([2, 1])

            with map_col:
                st.subheader("Mapa zdjęć")
                # Utwórz mapę wycentrowaną na średniej pozycji zdjęć
                m = folium.Map(
                    location=[df['latitude'].mean(), df['longitude'].mean()],
                    zoom_start=6,
                    control_scale=True
                )

                # Dodaj warstwy mapy
                folium.TileLayer('OpenStreetMap').add_to(m)
                folium.TileLayer('Stamen Terrain').add_to(m)
                folium.LayerControl().add_to(m)

                # Dodaj znaczniki ze zdjęciami
                for _, row in df.iterrows():
                    # HTML dla popupu
                    popup_html = f"""
                    <div style="width: 250px;">
                        <h4 style="margin: 5px 0; font-size: 16px;">{row['filename']}</h4>
                        <img src="{row['blob_url']}" style="width: 100%; max-height: 150px; object-fit: contain; margin: 5px 0;">
                        <div style="margin: 5px 0; font-size: 12px;">
                            <p><strong>Data:</strong> {row['date_taken'].strftime('%Y-%m-%d') if pd.notnull(row['date_taken']) else 'Nieznana'}</p>
                            <p><strong>Współrzędne:</strong> {row['latitude']:.6f}, {row['longitude']:.6f}</p>
                            <a href='{row['blob_url']}' target='_blank' style="color: #1a73e8;">Otwórz zdjęcie</a>
                        </div>
                    </div>
                    """

                    # Dodaj znacznik
                    folium.Marker(
                        [row['latitude'], row['longitude']],
                        popup=folium.Popup(popup_html, max_width=300),
                        icon=folium.Icon(color='blue', icon='camera', prefix='fa')
                    ).add_to(m)

                # Wyświetl mapę i przechwyć interakcje
                map_data = st_folium(
                    m,
                    height=600,
                    width=800,
                    returned_objects=["last_object_clicked"]
                )

                # Obsłuż kliknięcie na znaczniku
                if map_data.get("last_object_clicked"):
                    clicked = map_data["last_object_clicked"]
                    for _, row in df.iterrows():
                        if (abs(row['latitude'] - clicked['lat']) < 0.0001 and
                                abs(row['longitude'] - clicked['lng']) < 0.0001):
                            st.session_state.selected_photo = row
                            st.experimental_rerun()

            with preview_col:
                st.subheader("Podgląd zdjęcia")
                if st.session_state.selected_photo is not None:
                    photo = st.session_state.selected_photo
                    st.image(photo['blob_url'], use_column_width=True)
                    st.write(f"**Nazwa:** {photo['filename']}")
                    st.write(
                        f"**Data wykonania:** {photo['date_taken'].strftime('%Y-%m-%d %H:%M:%S') if pd.notnull(photo['date_taken']) else 'Nieznana'}")
                    st.write(f"**Data przesłania:** {photo['upload_time'].strftime('%Y-%m-%d %H:%M:%S')}")
                    st.write(f"**Współrzędne:** {photo['latitude']:.6f}, {photo['longitude']:.6f}")
                    st.markdown(f"[Otwórz oryginalne zdjęcie]({photo['blob_url']})", unsafe_allow_html=True)
                else:
                    st.info("Kliknij na znacznik, aby zobaczyć zdjęcie")
        else:
            st.info("Brak zdjęć w bazie danych.")
    except Exception as e:
        st.error(f"Błąd podczas ładowania mapy: {e}")

elif st.session_state.current_page == "list":
    df = execute_sql_query("""
        SELECT filename, latitude, longitude, blob_url, date_taken FROM photos
    """)
    if df is not None and not df.empty:
        for _, row in df.iterrows():
            st.markdown(f"""
            <div class="photo-card">
                <img src="{row['blob_url']}" alt="{row['filename']}" style="max-width:300px; max-height:200px;">
                <div class="photo-info">
                    <b>{row['filename']}</b><br>
                    Data wykonania: {row['date_taken']}<br>
                    Lokalizacja: {row['latitude']:.6f}, {row['longitude']:.6f}
                </div>
            </div>
            """, unsafe_allow_html=True)
    else:
        st.info("Brak zdjęć do wyświetlenia.")

        #streamlit run app.py
