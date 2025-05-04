import os
import re
import io
import time
import threading
import subprocess
from typing import Optional, Dict, List
import shlex
import socket
import random

import requests
from PIL import Image, ImageDraw, ImageFont
from fastapi import FastAPI, Request, Form, HTTPException, Response
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
import uvicorn

app = FastAPI()
templates = Jinja2Templates(directory="templates")

# Create directories if they don't exist
os.makedirs("static", exist_ok=True)
os.makedirs("output", exist_ok=True)
os.makedirs("templates", exist_ok=True)

# Serve static files
app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/output", StaticFiles(directory="output"), name="output")

# Data structure to store stream information
streams: Dict[str, Dict] = {}
stop_event = threading.Event()

def get_icy_metadata(url):
    """Fetch ICY metadata from a stream URL"""
    headers = {"Icy-MetaData": "1", "User-Agent": "Winamp"}
    try:
        r = requests.get(url, headers=headers, stream=True, timeout=5)
        print(f"Headers received: {r.headers}")  # Debug: print all headers
        metaint = int(r.headers.get("icy-metaint", 0))
        if metaint:
            r.raw.read(metaint)
            meta_length = int.from_bytes(r.raw.read(1), byteorder='big') * 16
            if meta_length > 0:
                metadata = r.raw.read(meta_length).decode('utf-8', errors='ignore')
                return metadata
        else:
            print("No metaint value found in headers")
    except Exception as e:
        print(f"Error fetching ICY metadata: {e}")
    return ""

def download_image(url):
    """Download and open an image from a URL"""
    try:
        response = requests.get(url, timeout=5)
        return Image.open(io.BytesIO(response.content))
    except Exception as e:
        print(f"Error downloading image: {e}")
        return None

def generate_overlay_image(stream_id: str, station_name: str, song_title: str, artwork_url: Optional[str]):
    """Generate an overlay image with station name and song title"""
    width, height = 1280, 720
    background = Image.new("RGB", (width, height), color=(25, 25, 35))
    draw = ImageDraw.Draw(background)
    
    # Try to use a nice font, fallback to default if unavailable
    try:
        title_font = ImageFont.truetype("arial.ttf", size=48)
        song_font = ImageFont.truetype("arial.ttf", size=36)
    except IOError:
        title_font = ImageFont.load_default()
        song_font = ImageFont.load_default()
    
    # Draw station name at the top
    station_bbox = draw.textbbox((0, 0), station_name, font=title_font)
    station_text_width = station_bbox[2] - station_bbox[0]
    draw.text(((width - station_text_width) / 2, 50), station_name, fill="white", font=title_font)
    
    # Draw song title at the bottom
    song_bbox = draw.textbbox((0, 0), song_title, font=song_font)
    song_text_width = song_bbox[2] - song_bbox[0]
    song_text_height = song_bbox[3] - song_bbox[1]
    draw.text(((width - song_text_width) / 2, height - song_text_height - 50), song_title, fill="white", font=song_font)
    
    # Add album artwork if available
    if artwork_url:
        image = download_image(artwork_url)
        if image:
            image = image.resize((400, 400))
            background.paste(image, ((width - 400) // 2, 160))
    
    # Save the overlay image
    overlay_path = f"static/overlay_{stream_id}.png"
    background.save(overlay_path)
    return overlay_path

def find_free_port():
    """Find a free port to use for the FFmpeg stream"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('', 0))
        s.listen(1)
        port = s.getsockname()[1]
        return port

def metadata_updater(stream_id: str, stream_url: str, station_name: str):
    """Thread to update metadata periodically"""
    while not stop_event.is_set():
        try:
            if stream_id not in streams:
                print(f"Stream {stream_id} no longer exists, stopping metadata updater")
                break
                
            metadata = get_icy_metadata(stream_url)
            song_title = None
            artwork_url = None
            
            # Parse StreamTitle
            title_match = re.search(r"StreamTitle='([^']+)';", metadata)
            if title_match:
                song_title = title_match.group(1)
            
            # Parse StreamUrl (optional)
            url_match = re.search(r"StreamUrl='([^']+)';", metadata)
            if url_match:
                artwork_url = url_match.group(1)
            
            # If no title found, default to station name
            if not song_title:
                song_title = station_name
            
            # Clean up the song title
            song_title = re.sub(r'\s+[^\w\s-]\s+', ' - ', song_title)
            
            # Update stream info
            streams[stream_id]["current_song"] = song_title
            
            # Use fallback icon if no artwork URL
            if not artwork_url and streams[stream_id].get("icon"):
                artwork_url = streams[stream_id]["icon"]
            
            # Generate new overlay
            overlay_path = generate_overlay_image(stream_id, station_name, song_title, artwork_url)
            
            # Restart FFmpeg if needed
            if streams[stream_id].get("ffmpeg_needs_update", False):
                restart_ffmpeg_stream(stream_id)
                streams[stream_id]["ffmpeg_needs_update"] = False
            
            print(f"[{station_name}] Now Playing: {song_title}")
            
            # Sleep for a reasonable interval
            time.sleep(30)
        except Exception as e:
            print(f"Error in metadata updater for {station_name}: {e}")
            time.sleep(10)

def start_ffmpeg_stream(stream_id: str):
    """Start a new FFmpeg process for a stream"""
    stream_info = streams.get(stream_id)
    if not stream_info:
        return False
    
    stream_url = stream_info["url"]
    station_name = stream_info["name"]
    overlay_image = f"static/overlay_{stream_id}.png"
    log_file = f"output/ffmpeg_{stream_id}.log"
    
    # Create output directory if it doesn't exist
    os.makedirs("output", exist_ok=True)
    
    # Ensure we have an overlay image
    if not os.path.exists(overlay_image):
        generate_overlay_image(stream_id, station_name, station_name, stream_info.get("icon"))
    
    # Get a free port for this stream
    stream_port = find_free_port()
    stream_info["port"] = stream_port
    
    # Build the FFmpeg command for streaming with HTTP server
    ffmpeg_command = [
        "ffmpeg",
        "-v", "verbose",   # Verbose output
        "-y",              # Overwrite without asking
        "-re",             # Read input at native frame rate
        "-i", stream_url,  # Input stream URL
        "-loop", "1",      # Loop the image
        "-i", overlay_image,
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-tune", "stillimage",
        "-c:a", "aac",
        "-b:a", "128k",
        "-vf", "scale=1280:720",
        "-f", "matroska",    # Use Matroska format for streaming
        "-listen", "1",      # Enable HTTP server in FFmpeg
        f"http://0.0.0.0:{stream_port}/live.mkv"  # Changed to 0.0.0.0 to allow external access
    ]
    
    try:
        # Kill existing process if it exists
        if stream_info.get("process"):
            try:
                print(f"Terminating existing FFmpeg process for {station_name}")
                stream_info["process"].terminate()
                stream_info["process"].wait(timeout=2)
            except:
                try:
                    print(f"Force killing FFmpeg process for {station_name}")
                    stream_info["process"].kill()
                except:
                    print(f"Failed to kill FFmpeg process for {station_name}")
        
        # Close existing log file handle if open
        if stream_info.get("log_fd"):
            try:
                stream_info["log_fd"].close()
            except:
                pass
        
        # Open log file
        log_fd = open(log_file, "w")
        
        print(f"Starting FFmpeg for {station_name} with command: {' '.join(ffmpeg_command)}")
        
        # Start new process with output redirected to log file
        stream_info["process"] = subprocess.Popen(
            ffmpeg_command, 
            stdout=log_fd,
            stderr=log_fd,
            universal_newlines=True
        )
        
        # Store log file handle and path
        stream_info["log_fd"] = log_fd
        stream_info["log_file"] = log_file
        stream_info["stream_url"] = f"http://0.0.0.0:{stream_port}/live.mkv"
        
        # Set last update time
        stream_info["last_update"] = time.time()
        
        print(f"Started FFmpeg for {station_name}, streaming at http://0.0.0.0:{stream_port}/live.mkv")
        
        # Wait a moment for FFmpeg to start up
        time.sleep(2)
        
        return True
    except Exception as e:
        print(f"Error starting FFmpeg: {e}")
        return False

def restart_ffmpeg_stream(stream_id: str):
    """Restart the FFmpeg process for a stream"""
    print(f"Restarting FFmpeg stream for {stream_id}")
    return start_ffmpeg_stream(stream_id)

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Main page with form to add streams and list of existing streams"""
    # Get the host from the request to build proper URLs
    host = request.headers.get('host', 'localhost:8000')
    
    # If accessed via IP, use that in the URLs
    host_ip = host.split(':')[0]
    app_port = host.split(':')[1] if ':' in host else '8000'
    
    # Build stream URLs dynamically based on how the page was accessed
    for stream_id, stream in streams.items():
        if 'port' in stream:
            stream['direct_ffmpeg_url'] = f"http://{host_ip}:{stream['port']}/live.mkv"
            stream['redirect_url'] = f"http://{host}/redirect/{stream_id}"
    
    return templates.TemplateResponse(
        "index.html", 
        {
            "request": request, 
            "streams": streams
        }
    )

@app.post("/add_stream", response_class=HTMLResponse)
async def add_stream(
    request: Request, 
    station_name: str = Form(...), 
    stream_url: str = Form(...),
    station_icon: Optional[str] = Form(None)
):
    """Add a new stream and start it"""
    # Generate a unique ID for the stream
    stream_id = re.sub(r'[^a-z0-9]', '', station_name.lower())
    if not stream_id:
        stream_id = f"stream_{len(streams) + 1}"
    
    # First check if ffmpeg is installed
    try:
        proc = subprocess.run(["ffmpeg", "-version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        ffmpeg_version = proc.stdout.decode().split('\n')[0]
        print(f"Using FFmpeg: {ffmpeg_version}")
    except (subprocess.SubprocessError, FileNotFoundError) as e:
        print(f"FFmpeg check failed: {e}")
        return templates.TemplateResponse(
            "index.html", 
            {
                "request": request, 
                "streams": streams,
                "message": f"Error: FFmpeg not found or not working. Error: {e}"
            }
        )
    
    # Store stream information
    streams[stream_id] = {
        "id": stream_id,
        "name": station_name,
        "url": stream_url,
        "icon": station_icon if station_icon else None,
        "current_song": station_name,  # Default to station name
        "process": None,
        "last_update": time.time(),
        "ffmpeg_needs_update": False
    }
    
    # Start the FFmpeg process
    if not start_ffmpeg_stream(stream_id):
        return templates.TemplateResponse(
            "index.html", 
            {
                "request": request, 
                "streams": streams,
                "message": f"Error: Failed to start FFmpeg for {station_name}"
            }
        )
    
    # Start the metadata updater thread
    metadata_thread = threading.Thread(
        target=metadata_updater,
        args=(stream_id, stream_url, station_name),
        daemon=True
    )
    metadata_thread.start()
    
    return RedirectResponse(url="/", status_code=303)

@app.get("/redirect/{stream_id}")
async def redirect_to_stream(request: Request, stream_id: str):
    """Redirect to the direct FFmpeg stream with proper host"""
    if stream_id not in streams:
        raise HTTPException(status_code=404, detail="Stream not found")
    
    # Get the stream port
    port = streams[stream_id].get("port")
    if not port:
        raise HTTPException(status_code=500, detail="Stream port not found")
    
    # Get the host from the request
    host = request.headers.get('host', 'localhost').split(':')[0]
    
    # Return a redirect to the direct FFmpeg URL using the same host
    return RedirectResponse(url=f"http://{host}:{port}/live.mkv", status_code=301)

@app.get("/refresh/{stream_id}")
async def refresh_stream(stream_id: str):
    """Force refresh a stream"""
    if stream_id not in streams:
        raise HTTPException(status_code=404, detail="Stream not found")
    
    print(f"Refreshing stream: {stream_id}")
    
    # Kill existing process ONLY for this stream
    if streams[stream_id].get("process"):
        try:
            print(f"Terminating process for stream {stream_id}")
            streams[stream_id]["process"].terminate()
            streams[stream_id]["process"].wait(timeout=2)
        except:
            # If it doesn't terminate nicely, force kill
            try:
                streams[stream_id]["process"].kill()
                print(f"Force killed process for stream {stream_id}")
            except:
                print(f"Failed to kill process for stream {stream_id}")
                pass
    
    # Start a new FFmpeg process
    if start_ffmpeg_stream(stream_id):
        print(f"Successfully restarted stream {stream_id}")
    else:
        print(f"Failed to restart stream {stream_id}")
    
    # Redirect back to the index page
    return RedirectResponse(url="/")

@app.get("/remove/{stream_id}")
async def remove_stream(stream_id: str):
    """Remove a stream"""
    if stream_id not in streams:
        raise HTTPException(status_code=404, detail="Stream not found")
    
    # Stop the FFmpeg process
    if streams[stream_id].get("process"):
        try:
            streams[stream_id]["process"].terminate()
            streams[stream_id]["process"].wait(timeout=5)
        except:
            pass
    
    # Close log file handle if open
    if streams[stream_id].get("log_fd"):
        try:
            streams[stream_id]["log_fd"].close()
        except:
            pass
    
    # Remove the overlay image
    overlay_file = f"static/overlay_{stream_id}.png"
    if os.path.exists(overlay_file):
        try:
            os.remove(overlay_file)
        except:
            pass
    
    # Remove the stream
    del streams[stream_id]
    
    # Remove the log file
    log_file = f"output/ffmpeg_{stream_id}.log"
    if os.path.exists(log_file):
        try:
            os.remove(log_file)
        except:
            pass
    
    return RedirectResponse(url="/")

@app.get("/logs/{stream_id}")
async def get_stream_logs(stream_id: str):
    """Return the FFmpeg logs for a stream"""
    if stream_id not in streams:
        raise HTTPException(status_code=404, detail="Stream not found")
    
    log_file = streams[stream_id].get("log_file")
    if not log_file or not os.path.exists(log_file):
        raise HTTPException(status_code=404, detail="Log file not found")
    
    try:
        with open(log_file, "r") as f:
            log_content = f.read()
        
        return HTMLResponse(f"""
        <html>
        <head>
            <title>FFmpeg Logs for {streams[stream_id]['name']}</title>
            <style>
                body {{ font-family: monospace; background-color: #000; color: #0f0; padding: 20px; }}
                h1 {{ color: #fff; }}
                pre {{ background-color: #111; padding: 15px; overflow-x: auto; white-space: pre-wrap; }}
                .refresh {{ display: inline-block; margin: 10px 0; padding: 8px 16px; background: #333; 
                            color: #fff; text-decoration: none; border-radius: 4px; }}
                .cmd {{ color: #ff9; margin-bottom: 10px; }}
            </style>
            <meta http-equiv="refresh" content="5">
        </head>
        <body>
            <h1>FFmpeg Logs for {streams[stream_id]['name']}</h1>
            <a href="/" class="refresh">Back to Index</a>
            <pre class="cmd">Command: {' '.join(streams[stream_id]['process'].args if streams[stream_id].get('process') else ['No process running'])}</pre>
            <pre>{log_content}</pre>
        </body>
        </html>
        """)
    except Exception as e:
        return HTMLResponse(f"<html><body>Error reading logs: {e}</body></html>")

if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)))  # Change to the directory of this script
    uvicorn.run(app, host="127.0.0.1", port=8000)