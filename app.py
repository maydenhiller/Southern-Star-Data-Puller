import io
import zipfile
import re
from typing import Dict, Optional, List, Tuple

import pandas as pd
import streamlit as st
import xml.etree.ElementTree as ET

# Constants
KML_NS = {"kml": "http://www.opengis.net/kml/2.2"}

EARTHPOINT_ICON_URL = "http://www.earthpoint.us/Dots/GoogleEarth/pal3/icon62.png"

VALVE_ICON_URL = "http://maps.google.com/mapfiles/kml/shapes/triangle.png"
VALVE_ICON_COLOR = "purple"

LETTER_DASH_ICON_URL = "http://maps.google.com/mapfiles/kml/shapes/flag.png"
LETTER_DASH_ICON_COLOR = "red"

# blu-circle icon corresponds to blue
DEFAULT_AGM_ICON_URL = "http://maps.google.com/mapfiles/kml/paddle/blu-circle.png"
DEFAULT_AGM_ICON_COLOR = "blue"


def read_kml_from_upload(uploaded_file) -> str:
    """
    Read KML text from an uploaded .kml or .kmz Streamlit UploadedFile.
    """
    filename = uploaded_file.name.lower()

    uploaded_file.seek(0)
    data = uploaded_file.read()

    if filename.endswith(".kml"):
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return data.decode("utf-8", errors="ignore")

    if filename.endswith(".kmz"):
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            # Find the first .kml file inside the KMZ
            kml_name = None
            for name in zf.namelist():
                if name.lower().endswith(".kml"):
                    kml_name = name
                    break
            if not kml_name:
                raise ValueError("No .kml file found inside the KMZ archive.")
            raw = zf.read(kml_name)
            try:
                return raw.decode("utf-8")
            except UnicodeDecodeError:
                return raw.decode("utf-8", errors="ignore")

    raise ValueError("Unsupported file type. Please upload a .kml or .kmz file.")


def build_style_maps(root: ET.Element) -> Tuple[Dict[str, Dict], Dict[str, str]]:
    """
    Build:
      - styles: id -> {'icon_href': str or None, 'line_color': str or None}
      - stylemap_to_style: styleMap id -> concrete style id (normal)
    """
    styles: Dict[str, Dict[str, Optional[str]]] = {}
    stylemap_to_style: Dict[str, str] = {}

    # Parse <Style> elements
    for style in root.findall(".//kml:Style", KML_NS):
        sid = style.get("id")
        if not sid:
            continue

        icon_href = None
        icon_el = style.find(".//kml:IconStyle/kml:Icon/kml:href", KML_NS)
        if icon_el is not None and icon_el.text:
            icon_href = icon_el.text.strip()

        line_color = None
        color_el = style.find(".//kml:LineStyle/kml:color", KML_NS)
        if color_el is not None and color_el.text:
            line_color = color_el.text.strip().lower()

        styles[sid] = {
            "icon_href": icon_href,
            "line_color": line_color,
        }

    # Parse <StyleMap> elements to link them to concrete styles
    for sm in root.findall(".//kml:StyleMap", KML_NS):
        smid = sm.get("id")
        if not smid:
            continue

        normal_style_id = None
        for pair in sm.findall("kml:Pair", KML_NS):
            key_el = pair.find("kml:key", KML_NS)
            if key_el is not None and key_el.text and key_el.text.strip() == "normal":
                style_url_el = pair.find("kml:styleUrl", KML_NS)
                if style_url_el is not None and style_url_el.text:
                    normal_style_id = style_url_el.text.strip().lstrip("#")
                    break
        if normal_style_id:
            stylemap_to_style[smid] = normal_style_id

    return styles, stylemap_to_style


def resolve_style(style_url: Optional[str],
                  styles: Dict[str, Dict],
                  stylemap_to_style: Dict[str, str]) -> Dict:
    """
    Given a styleUrl text (may be '#id' or full URL), return a style dict:
    {'icon_href': str or None, 'line_color': str or None}
    """
    if not style_url:
        return {"icon_href": None, "line_color": None}

    if style_url.startswith("#"):
        style_id = style_url[1:]
    else:
        if "#" in style_url:
            style_id = style_url.split("#", 1)[1]
        else:
            return {"icon_href": None, "line_color": None}

    if style_id in stylemap_to_style:
        style_id = stylemap_to_style[style_id]

    return styles.get(style_id, {"icon_href": None, "line_color": None})


def parse_coordinates(coord_text: str) -> List[Tuple[float, float]]:
    """
    Parse a KML <coordinates> string into a list of (lat, lon) tuples.
    """
    coords = []
    if not coord_text:
        return coords

    for part in coord_text.strip().split():
        bits = part.split(",")
        if len(bits) < 2:
            continue
        try:
            lon = float(bits[0])
            lat = float(bits[1])
        except ValueError:
            continue
        coords.append((lat, lon))
    return coords


def classify_agm(name: str) -> Tuple[str, str, str]:
    """
    Determine AGM icon URL, icon color, and text symbol based on the name.
    Rules:
      - Contains 'valve' or 'mlv' (case-insensitive): purple triangle
      - Starts with letter(s) + '-' : red flag
      - Otherwise (has any word/letter): blue dot
    Returns (icon_url, icon_color, symbol_text).
    """
    lower_name = name.lower()

    # Valve / MLV rule
    if "valve" in lower_name or "mlv" in lower_name:
        return VALVE_ICON_URL, VALVE_ICON_COLOR, "purple triangle"

    # Letter-dash prefix rule, e.g., "A-123", "AB-01", etc.
    if re.match(r"^[A-Za-z]+-", name.strip()):
        return LETTER_DASH_ICON_URL, LETTER_DASH_ICON_COLOR, "red flag"

    # Default: any name that has a letter is treated as "word in the name"
    if re.search(r"[A-Za-z]", name):
        return DEFAULT_AGM_ICON_URL, DEFAULT_AGM_ICON_COLOR, "blue dot"

    # Fallback
    return DEFAULT_AGM_ICON_URL, DEFAULT_AGM_ICON_COLOR, "blue dot"


def extract_data(kml_text: str):
    """
    Extract:
      - Map Notes
      - SS provided access (LineStrings)
      - SS provided AGMs
    Returns dicts with data for text/CSV generation.
    """
    root = ET.fromstring(kml_text)

    styles, stylemap_to_style = build_style_maps(root)

    # Storage for outputs
    map_notes_txt_rows = []  # Latitude, Longitude, note
    map_notes_csv_rows = []  # Latitude, Longitude, Name, Icon, HideNameUntilMouseOver

    ss_access_csv_rows = []  # Latitude, Longitude, icon, linestring color
    ss_access_txt_lines: List[Tuple[str, str]] = []  # (latitude or label, longitude)

    agm_csv_rows = []  # Latitude, Longitude, Name, Icon, IconColor
    agm_txt_rows = []  # Latitude, Longitude, Name, Symbol

    # --- Process Placemarks ---
    for pm in root.findall(".//kml:Placemark", KML_NS):
        name_el = pm.find("kml:name", KML_NS)
        name = name_el.text.strip() if name_el is not None and name_el.text else ""

        style_url_el = pm.find("kml:styleUrl", KML_NS)
        style_url = style_url_el.text.strip() if style_url_el is not None and style_url_el.text else None
        style_info = resolve_style(style_url, styles, stylemap_to_style)

        # Try to resolve icon from a local <Style> inside the Placemark as well
        local_icon_href = None
        local_icon_el = pm.find(".//kml:Style/kml:IconStyle/kml:Icon/kml:href", KML_NS)
        if local_icon_el is not None and local_icon_el.text:
            local_icon_href = local_icon_el.text.strip()
        icon_href = local_icon_href or style_info.get("icon_href")

        # --- First: check for Point (Map Notes or AGMs) ---
        point_el = pm.find(".//kml:Point", KML_NS)
        if point_el is not None and name:
            coords_el = point_el.find("kml:coordinates", KML_NS)
            coords = parse_coordinates(coords_el.text if coords_el is not None and coords_el.text else "")
            if coords:
                lat, lon = coords[0]

                # Map Notes placemark
                if icon_href == EARTHPOINT_ICON_URL:
                    desc_el = pm.find("kml:description", KML_NS)
                    if desc_el is not None and desc_el.text and desc_el.text.strip():
                        note_text = desc_el.text.strip()
                    else:
                        note_text = name

                    map_notes_txt_rows.append({
                        "Latitude": lat,
                        "Longitude": lon,
                        "note": note_text,
                    })
                    map_notes_csv_rows.append({
                        "Latitude": lat,
                        "Longitude": lon,
                        "Name": name,
                        "Icon": "40",
                        "HideNameUntilMouseOver": "TRUE",
                    })
                else:
                    # AGM placemark
                    icon_url, icon_color, symbol_text = classify_agm(name)
                    agm_csv_rows.append({
                        "Latitude": lat,
                        "Longitude": lon,
                        "Name": name,
                        "Icon": icon_url,
                        "IconColor": icon_color,
                    })
                    agm_txt_rows.append({
                        "Latitude": lat,
                        "Longitude": lon,
                        "Name": name,
                        "Symbol": symbol_text,
                    })

        # --- Second: check for LineStrings (SS provided access) ---
        line_el = pm.find(".//kml:LineString", KML_NS)
        if line_el is not None:
            coords_el = line_el.find("kml:coordinates", KML_NS)
            coords = parse_coordinates(coords_el.text if coords_el is not None and coords_el.text else "")
            if not coords:
                continue

            # TXT: begin line marker
            ss_access_txt_lines.append(("begin line", ""))

            for lat, lon in coords:
                # CSV row: icon always "none", linestring color always "blue"
                ss_access_csv_rows.append({
                    "Latitude": lat,
                    "Longitude": lon,
                    "icon": "none",
                    "linestring color": "blue",
                })
                # TXT row
                ss_access_txt_lines.append((f"{lat}", f"{lon}"))

            # TXT: END marker
            ss_access_txt_lines.append(("END", ""))

            # CSV: blank line separator between multiple LineStrings
            ss_access_csv_rows.append({
                "Latitude": "",
                "Longitude": "",
                "icon": "",
                "linestring color": "",
            })

    return {
        "map_notes_txt": map_notes_txt_rows,
        "map_notes_csv": map_notes_csv_rows,
        "ss_access_csv": ss_access_csv_rows,
        "ss_access_txt": ss_access_txt_lines,
        "agm_csv": agm_csv_rows,
        "agm_txt": agm_txt_rows,
    }


def dataframe_to_csv_bytes(df: pd.DataFrame) -> bytes:
    buf = io.StringIO()
    df.to_csv(buf, index=False)
    return buf.getvalue().encode("utf-8")


def rows_to_txt_bytes(headers: List[str], rows: List[Dict[str, object]]) -> bytes:
    """
    Create a tab-separated .txt file from a list of dict rows.
    """
    buf = io.StringIO()
    buf.write("\t".join(headers) + "\n")
    for row in rows:
        values = [str(row.get(h, "")) for h in headers]
        buf.write("\t".join(values) + "\n")
    return buf.getvalue().encode("utf-8")


def ss_access_txt_to_bytes(lines: List[Tuple[str, str]]) -> bytes:
    """
    SS provided access TXT format:
      - two columns: latitude, longitude
      - 'begin line' and 'END' markers in the first column
    """
    buf = io.StringIO()
    buf.write("latitude\tlongitude\n")
    for lat_str, lon_str in lines:
        buf.write(f"{lat_str}\t{lon_str}\n")
    return buf.getvalue().encode("utf-8")


def build_output_files(extracted: Dict) -> Dict[str, bytes]:
    """
    Turn extracted data into file-name -> bytes mapping.
    """
    files: Dict[str, bytes] = {}

    # --- Map Notes ---
    if extracted["map_notes_txt"]:
        txt_bytes = rows_to_txt_bytes(
            ["Latitude", "Longitude", "note"],
            extracted["map_notes_txt"],
        )
        csv_df = pd.DataFrame(extracted["map_notes_csv"])
        csv_bytes = dataframe_to_csv_bytes(csv_df)
        files["Map Notes.txt"] = txt_bytes
        files["Map Notes.csv"] = csv_bytes

    # --- SS provided access (LineStrings) ---
    if extracted["ss_access_csv"]:
        csv_df = pd.DataFrame(extracted["ss_access_csv"])
        csv_bytes = dataframe_to_csv_bytes(csv_df)
        txt_bytes = ss_access_txt_to_bytes(extracted["ss_access_txt"])
        files["SS provided access.csv"] = csv_bytes
        files["SS provided access.txt"] = txt_bytes

    # --- SS provided AGMs ---
    if extracted["agm_csv"]:
        csv_df = pd.DataFrame(extracted["agm_csv"])
        csv_bytes = dataframe_to_csv_bytes(csv_df)
        txt_bytes = rows_to_txt_bytes(
            ["Latitude", "Longitude", "Name", "Symbol"],
            extracted["agm_txt"],
        )
        files["SS provided AGMs.csv"] = csv_bytes
        files["SS provided AGMs.txt"] = txt_bytes

    return files


def build_zip(files: Dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


def main():
    st.set_page_config(page_title="Southern Star Data Puller", layout="wide")

    st.title("Southern Star Data Puller")

    st.markdown(
        """
        Upload a `.kml` or `.kmz` file and this app will:

        - **Map Notes**: extract named placemarks with icon
          `http://www.earthpoint.us/Dots/GoogleEarth/pal3/icon62.png`
          into `Map Notes.txt` and `Map Notes.csv`.
        - **SS provided access**: extract LineStrings into
          `SS provided access.txt` and `SS provided access.csv`
          (icon = `none`, linestring color = `blue`).
        - **SS provided AGMs**: extract other named placemarks into
          `SS provided AGMs.txt` and `SS provided AGMs.csv`
          using your icon and color rules.
        """
    )

    uploaded_file = st.file_uploader("Upload a .kml or .kmz file", type=["kml", "kmz"])

    if not uploaded_file:
        st.info("Please upload a KML or KMZ file to begin.")
        return

    if st.button("Process file"):
        try:
            kml_text = read_kml_from_upload(uploaded_file)
            extracted = extract_data(kml_text)
            files = build_output_files(extracted)

            if not files:
                st.warning("No matching placemarks or LineStrings were found in the file.")
                return

            st.success("Processing complete. Download your files below.")

            # Individual download buttons
            for name, content in files.items():
                st.download_button(
                    label=f"Download {name}",
                    data=content,
                    file_name=name,
                    mime="text/plain" if name.lower().endswith(".txt") else "text/csv",
                )

            # ZIP download
            zip_bytes = build_zip(files)
            st.download_button(
                label="Download all as ZIP",
                data=zip_bytes,
                file_name="southern_star_data_puller_outputs.zip",
                mime="application/zip",
            )

        except Exception as e:
            st.error(f"An error occurred while processing the file: {e}")


if __name__ == "__main__":
    main()
