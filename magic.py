import requests
import re
import time
from PIL import Image, ImageDraw, ImageFont
import io

stream_url = "https://stream.streambase.ch/argovia/mp3-192/radiobrowser/"

def get_icy_metadata(url):
    headers = {"Icy-MetaData": "1", "User-Agent": "Winamp"}
    r = requests.get(url, headers=headers, stream=True)
    metaint = int(r.headers.get("icy-metaint", 0))
    if metaint:
        r.raw.read(metaint)
        meta_length = int.from_bytes(r.raw.read(1), byteorder='big') * 16
        if meta_length > 0:
            metadata = r.raw.read(meta_length).decode('utf-8', errors='ignore')
            return metadata
    return ""

def download_image(url):
    try:
        response = requests.get(url)
        return Image.open(io.BytesIO(response.content))
    except Exception as e:
        print(f"Error downloading image: {e}")
        return None
metadata = get_icy_metadata(stream_url)
match = re.search(r"StreamTitle='([^']+)';.*StreamUrl='([^']+)'", metadata)
if match:
    song_title = match.group(1)
    artwork_url = match.group(2)
else:
    song_title = "Unknown Song"
    artwork_url = None
    
song_title = re.sub(r'\s+[^\w\s-]\s+', ' - ', song_title)
print(f"Now Playing: {song_title}")
image = download_image(artwork_url) if artwork_url else None
width, height = 1280, 720
background = Image.new("RGB", (width, height), color="black")
draw = ImageDraw.Draw(background)
font = ImageFont.load_default().font_variant(size=36)
bbox = draw.textbbox((0, 0), song_title, font=font)
text_width = bbox[2] - bbox[0]
text_height = bbox[3] - bbox[1]
draw.text(((width - text_width) / 2, height - text_height - 50), song_title, fill="white", font=font)

if image:
    image = image.resize((400, 400))
    background.paste(image, ((width - 400) // 2, 100))

background.save("overlay_image.png")