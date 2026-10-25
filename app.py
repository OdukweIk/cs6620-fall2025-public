import os
import re
import csv
from io import StringIO
from flask import Flask, render_template, request, jsonify, send_from_directory, send_file
from flask_cors import CORS
from pydub import AudioSegment
import tempfile
from datetime import datetime


app = Flask(__name__)
CORS(app)

# Version: 2.0
# Built with GitHub Actions CI/CD + AWS SSM


# Global variables for playlist management
current_directory = None
current_playlist = [] # Stores full paths on server
audio_file_map = {}  # Maps filename to full path for nested directories
SUPPORTED_AUDIO_EXTENSIONS = ('.mp3', '.wav', '.ogg')

# Global variable to store parsed log data
parsed_transcription_data = {}

# Global variables for CSV error labeling
csv_error_data = []
csv_file_loaded = False

def time_to_seconds(time_str):
    """Convert HH:MM:SS format to seconds"""
    try:
        if ':' in time_str:
            parts = time_str.split(':')
            if len(parts) == 3:
                hours, minutes, seconds = map(float, parts)
                return hours * 3600 + minutes * 60 + seconds
            elif len(parts) == 2:
                minutes, seconds = map(float, parts)
                return minutes * 60 + seconds
        return float(time_str)  # Try direct conversion as fallback
    except (ValueError, AttributeError):
        return 0.0

def parse_log_content(log_content):
    """
    Parses the log content to extract error segments for each audio file.
    Only supports the new CSV/TSV format as in sample_log.txt.
    Returns a dict mapping audio file basenames to a list of error segments.
    """
    data = {}
    # Auto-detect delimiter: use tab if more tabs than commas, else comma
    tab_count = log_content.count('\t')
    comma_count = log_content.count(',')
    delimiter = '\t' if tab_count > comma_count else ','
    reader = csv.reader(StringIO(log_content), delimiter=delimiter)
    header = next(reader, None)
    # Find relevant column indices
    def col(name):
        try:
            return header.index(name)
        except ValueError:
            return None
    col_audio = col('transcriptFile')
    col_long_start = col('longFormStart')
    col_long_end = col('longFormEnd')
    col_long_error = col('longFormError')
    col_short_error = col('shortFormError')
    col_short_start = col('shortFormStart')
    col_short_end = col('shortFormEnd')
    for row in reader:
        if not row or len(row) <= max(filter(None, [col_audio, col_long_start, col_long_end, col_long_error, col_short_error, col_short_start, col_short_end])):
            continue
        audio_path = row[col_audio]
        filename = os.path.basename(audio_path)
        try:
            long_start = float(row[col_long_start]) if col_long_start is not None else None
            long_end = float(row[col_long_end]) if col_long_end is not None else None
            short_start = float(row[col_short_start]) if col_short_start is not None and row[col_short_start] else None
            short_end = float(row[col_short_end]) if col_short_end is not None and row[col_short_end] else None
        except (ValueError, IndexError):
            long_start = long_end = short_start = short_end = None
        segment = {
            'longFormStart': long_start,
            'longFormEnd': long_end,
            'longFormError': row[col_long_error] if col_long_error is not None else '',
            'shortFormError': row[col_short_error] if col_short_error is not None else '',
            'shortFormStart': short_start,
            'shortFormEnd': short_end
        }
        if filename not in data:
            data[filename] = []
        data[filename].append(segment)
    return data

@app.route('/audio_files/<path:filename>')
def serve_audio_file(filename):
    """
    Serves audio files directly to the client's web browser.
    Now supports nested directories using the audio_file_map.
    """
    # First check if we have a mapping for this filename (from nested directories)
    if filename in audio_file_map:
        file_path = audio_file_map[filename]
        directory = os.path.dirname(file_path)
        file_name = os.path.basename(file_path)
        return send_from_directory(directory, file_name)
    
    # Fallback to old behavior for flat directory structure
    elif current_directory and os.path.exists(os.path.join(current_directory, filename)):
        return send_from_directory(current_directory, filename)
    else:
        return "File not found", 404

@app.route('/audio_segment')
def serve_audio_segment():
    """
    Serves a segment of an audio file, given filename, start, and end times (in seconds).
    The segment is [start-5, end+5] seconds, clipped to file boundaries.
    """
    filename = request.args.get('file')
    start = request.args.get('start', type=float)
    end = request.args.get('end', type=float)
    if not (filename and start is not None and end is not None):
        return "Missing parameters", 400
    if not current_directory:
        return "No directory selected", 400
    audio_path = os.path.join(current_directory, filename)
    if not os.path.exists(audio_path):
        return "Audio file not found", 404
    try:
        audio = AudioSegment.from_file(audio_path)
        duration = len(audio) / 1000.0  # in seconds
        seg_start = max(0, start - 5)
        seg_end = min(duration, end + 5)
        seg_audio = audio[int(seg_start * 1000):int(seg_end * 1000)]
        # Write to a temp file
        with tempfile.NamedTemporaryFile(delete=False, suffix='.wav') as tmpf:
            seg_audio.export(tmpf.name, format='wav')
            tmpf.flush()
            return send_file(tmpf.name, mimetype='audio/wav', as_attachment=False, download_name=f'{filename}_segment.wav')
    except Exception as e:
        app.logger.error(f"Error extracting audio segment: {e}")
        return f"Error extracting audio segment: {str(e)}", 500

@app.route('/')
def index():
    """
    Renders the main HTML page for verification indicators required by the assignment.
    """
    return f'''
    <h1>Hello from Automated CI/CD Pipeline!</h1>
    <p><strong>Version:</strong> 2.0 - Automated Deployment</p>
    <p><strong>Deployed via:</strong> GitHub Actions + AWS SSM</p>
    <p><strong>Build Date:</strong> {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}</p>
    <p><strong>Assignment:</strong> Automated EC2 Deployment</p>
    '''

@app.route('/health')
def health():
    """
    Returns automated deployment validation metadata.
    """
    return {
        'status': 'healthy',
        'version': '2.0',
        'deployment_method': 'automated',
        'timestamp': datetime.now().isoformat()
    }

@app.route('/select_directory', methods=['POST'])
def select_directory():
    """
    Handles the selection of an audio directory and populates the playlist.
    Now also includes transcription data if a log file has been uploaded/loaded.
    """
    global current_directory, current_playlist, audio_file_map

    directory_path = request.form.get('directory_path')

    if not directory_path:
        return jsonify({"success": False, "message": "Please enter a directory path."})

    if not os.path.isdir(directory_path):
        return jsonify({"success": False, "message": "Invalid directory path."})

    current_directory = directory_path
    current_playlist = []
    audio_file_map = {}
    
    # Recursively load all audio files from the directory and subdirectories
    for root, _, files in os.walk(directory_path):
        for filename in files:
            if filename.lower().endswith(SUPPORTED_AUDIO_EXTENSIONS):
                full_path = os.path.join(root, filename)
                current_playlist.append(full_path)
                # Map just the filename to the full path for CSV lookups
                audio_file_map[filename] = full_path
                # Also map filename without extension for CSV compatibility
                name_without_ext = os.path.splitext(filename)[0]
                audio_file_map[name_without_ext] = full_path
    
    current_playlist.sort()
    
    files_with_transcription_info = []

    for f_path in current_playlist:
        f_name = os.path.basename(f_path)
        file_url = f'/audio_files/{f_name}'
        error_segments = parsed_transcription_data.get(f_name, [])
        files_with_transcription_info.append({
            "name": f_name,
            "url": file_url,
            "error_segments": error_segments
        })

    return jsonify({
        "success": True,
        "message": f"Directory selected: {directory_path}",
        "files_with_info": files_with_transcription_info
    })
