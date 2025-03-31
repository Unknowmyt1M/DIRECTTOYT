import os
import logging
import tempfile
import json
import time
import subprocess
from datetime import datetime
from urllib.parse import urlparse

def format_duration(seconds):
    """Format duration from seconds to HH:MM:SS"""
    if not seconds:
        return "00:00"
    
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"
from flask import Flask, render_template, request, jsonify, flash, session, redirect, send_file
import yt_dlp
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import Flow
import requests
from config import Config
from models import db, User, Video, ApiCredential
from urllib.parse import urlparse, parse_qs
from flask_login import LoginManager, current_user, login_required

def is_valid_youtube_url(url):
    try:
        parsed_url = urlparse(url)
        if parsed_url.netloc in ['youtube.com', 'www.youtube.com', 'youtu.be']:
            if parsed_url.netloc == 'youtu.be':
                return bool(parsed_url.path[1:])
            if parsed_url.path == '/watch':
                return bool(parse_qs(parsed_url.query).get('v'))
            return False
        return False
    except:
        return False

# Configure logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

app = Flask(__name__)
# Use SESSION_SECRET environment variable if available, otherwise use a secure random key
app.secret_key = os.environ.get("SESSION_SECRET") or os.urandom(24).hex()
app.config.from_object(Config)

# Configure database
database_url = os.environ.get("DATABASE_URL")
if not database_url:
    # Fallback to SQLite if DATABASE_URL is not set
    logger.warning("DATABASE_URL not set. Falling back to SQLite.")
    database_url = "sqlite:///app.db"

app.config["SQLALCHEMY_DATABASE_URI"] = database_url
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
    "pool_recycle": 300,
    "pool_pre_ping": True,
    "pool_timeout": 30,  # Shorter connection timeout
}
# Initialize the database
db.init_app(app)

# Create tables
with app.app_context():
    db.create_all()

# Set up Flask-Login
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'google_auth.login'

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# Temporary directory for storing downloads
temp_dir = tempfile.gettempdir()

# Google Drive API scopes
SCOPES = ['https://www.googleapis.com/auth/drive.file']

# Import and register Google Auth blueprint
from google_auth import google_auth
app.register_blueprint(google_auth)

def store_api_credentials(service_name, client_id, client_secret):
    """Store API credentials in the database"""
    try:
        # Check if credentials for this service already exist
        existing_creds = ApiCredential.query.filter_by(service_name=service_name).first()
        
        if existing_creds:
            # Update existing credentials
            existing_creds.client_id = client_id
            existing_creds.client_secret = client_secret
            existing_creds.updated_at = datetime.utcnow()
        else:
            # Create new credentials
            new_creds = ApiCredential(
                service_name=service_name,
                client_id=client_id,
                client_secret=client_secret
            )
            db.session.add(new_creds)
        
        db.session.commit()
        logger.info(f"API credentials for {service_name} stored successfully")
        return True
    except Exception as e:
        logger.error(f"Error storing API credentials: {e}")
        db.session.rollback()
        return False

def get_api_credentials(service_name):
    """Retrieve API credentials from the database"""
    try:
        creds = ApiCredential.query.filter_by(service_name=service_name).first()
        if creds:
            return {
                'client_id': creds.client_id,
                'client_secret': creds.client_secret
            }
        return None
    except Exception as e:
        logger.error(f"Error retrieving API credentials: {e}")
        return None

def get_authenticated_service():
    """Build and return a Drive service object"""
    try:
        if 'credentials' not in session:
            logger.debug("No credentials in session")
            return None
        
        # Parse credentials from session
        creds_data = json.loads(session['credentials'])
        logger.debug(f"Credentials structure: {list(creds_data.keys())}")
        
        # Ensure all required fields are present
        required_fields = ['token', 'refresh_token', 'token_uri', 'client_id', 'client_secret', 'scopes']
        missing_fields = [field for field in required_fields if field not in creds_data]
        
        if missing_fields:
            logger.error(f"Credentials missing required fields: {missing_fields}")
            flash(f"Authentication issue: Missing {', '.join(missing_fields)}")
            return None
        
        # Create credentials object
        credentials = Credentials(
            token=creds_data['token'],
            refresh_token=creds_data['refresh_token'],
            token_uri=creds_data['token_uri'],
            client_id=creds_data['client_id'],
            client_secret=creds_data['client_secret'],
            scopes=creds_data['scopes']
        )
        
        # Refresh if expired
        if credentials and credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
            
            # Update session with refreshed credentials
            updated_creds = {
                'token': credentials.token,
                'refresh_token': credentials.refresh_token,
                'token_uri': credentials.token_uri,
                'client_id': credentials.client_id,
                'client_secret': credentials.client_secret,
                'scopes': credentials.scopes
            }
            session['credentials'] = json.dumps(updated_creds)
        
        return build('drive', 'v3', credentials=credentials)
    except Exception as e:
        logger.error(f"Error getting authenticated service: {e}")
        return None

@app.route('/')
def index():
    """Render the main page with options"""
    return render_template('index.html')

@app.route('/download')
def download_page():
    """Render the download page"""
    return render_template('download.html')

@app.route('/metadata')
def metadata_page():
    """Render the metadata extraction page"""
    return render_template('metadata.html')

@app.route('/get_metadata', methods=['POST'])
def get_metadata():
    """Extract metadata from YouTube URL"""
    try:
        data = request.get_json()
        url = data.get('url', '')
        
        if not is_valid_youtube_url(url):
            return jsonify({'error': 'Invalid YouTube URL'}), 400
        
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'extract_flat': True
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            
        return jsonify({
            'title': info.get('title', 'Unknown title'),
            'description': info.get('description', 'No description available'),
            'channel': info.get('uploader', 'Unknown channel'),
            'duration': format_duration(info.get('duration', 0)),
            'tags': info.get('tags', [])
        })
    except Exception as e:
        logger.error(f"Error extracting metadata: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/history_page')
def history_page():
    """Render the history page"""
    return render_template('history.html')

@app.route('/auth')
def auth():
    """Start the OAuth flow - redirects to the Google Auth blueprint"""
    return redirect('/google_login')

@app.route('/api/credentials', methods=['POST'])
def add_api_credentials():
    """Store API credentials in the database"""
    try:
        data = request.get_json()
        service_name = data.get('service_name')
        client_id = data.get('client_id')
        client_secret = data.get('client_secret')
        
        # Validate inputs
        if not all([service_name, client_id, client_secret]):
            return jsonify({
                'error': 'Missing required parameters'
            }), 400
        
        # Store credentials in the database
        success = store_api_credentials(service_name, client_id, client_secret)
        
        if success:
            return jsonify({
                'status': 'success',
                'message': f'API credentials for {service_name} stored successfully'
            })
        else:
            return jsonify({
                'error': 'Failed to store API credentials'
            }), 500
    except Exception as e:
        logger.error(f"Error storing API credentials: {e}")
        return jsonify({
            'error': str(e)
        }), 500

@app.route('/api/credentials/<service_name>', methods=['GET'])
def get_service_credentials(service_name):
    """Get API credentials for a specific service"""
    try:
        credentials = get_api_credentials(service_name)
        
        if credentials:
            # Don't return the actual secret, just confirm it exists
            return jsonify({
                'status': 'success',
                'service_name': service_name,
                'has_credentials': True
            })
        else:
            return jsonify({
                'status': 'success',
                'service_name': service_name,
                'has_credentials': False
            })
    except Exception as e:
        logger.error(f"Error getting API credentials: {e}")
        return jsonify({
            'error': str(e)
        }), 500

@app.route('/get_video_info', methods=['POST'])
def get_video_info():
    """Get video information from YouTube URL"""
    try:
        if not request.is_json:
            logger.error(f"Request is not JSON: {request.data}")
            return jsonify({'error': 'Request must be JSON'}), 400
            
        data = request.get_json()
        url = data.get('url', '')
        
        logger.info(f"Processing YouTube URL: {url}")
        
        # Validate YouTube URL
        if not is_valid_youtube_url(url):
            logger.warning(f"Invalid YouTube URL: {url}")
            return jsonify({'error': 'Invalid YouTube URL'}), 400
        
        # Enhanced options for yt-dlp
        ydl_opts = {
            'format': 'best[height<=720]',  # Limit to 720p to avoid extremely large files
            'quiet': True,
            'no_warnings': True,
            'skip_download': True,
            'extract_flat': False,
            'no_playlist': True,
            'nocheckcertificate': True,  # Skip HTTPS certificate validation
            'ignoreerrors': True,        # Skip unavailable videos
        }
        
        logger.info("Extracting video info with yt-dlp...")
        
        # Try the primary extraction method
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                
                # Get the best thumbnail available
                thumbnails = info.get('thumbnails', [])
                best_thumbnail = ''
                
                if thumbnails:
                    # Try to get the highest quality thumbnail
                    for quality in ['maxres', 'high', 'medium', 'default', 'standard']:
                        for thumb in thumbnails:
                            if isinstance(thumb, dict) and thumb.get('id') == quality:
                                best_thumbnail = thumb.get('url', '')
                                break
                        if best_thumbnail:
                            break
                
                # Fallback to the basic thumbnail if no better one was found
                if not best_thumbnail:
                    best_thumbnail = info.get('thumbnail', '')
                
                response_data = {
                    'title': info.get('title', 'Unknown title'),
                    'duration': info.get('duration', 0),
                    'thumbnail': best_thumbnail,
                    'uploader': info.get('uploader', 'Unknown uploader'),
                }
                
                logger.info(f"Successfully extracted video info: {response_data['title']}")
                return jsonify(response_data)
                
        except Exception as ydl_error:
            logger.error(f"yt-dlp extraction failed: {ydl_error}")
            
            # Fallback to simpler extraction method
            try:
                # Try with even more basic options
                simple_opts = {
                    'quiet': True,
                    'no_warnings': True,
                    'skip_download': True,
                    'extract_flat': True,
                    'no_color': True,
                    'nocheckcertificate': True,  # Skip HTTPS certificate validation
                    'ignoreerrors': True,        # Skip unavailable videos
                    'geo_bypass': True,          # Try to bypass geo-restrictions
                    'no_playlist': True          # Skip playlist info
                }
                
                logger.info("Trying fallback extraction method...")
                with yt_dlp.YoutubeDL(simple_opts) as ydl:
                    info = ydl.extract_info(url, download=False)
                    
                    # Get the best thumbnail from fallback method
                    thumbnails = info.get('thumbnails', [])
                    best_thumbnail = ''
                    
                    if thumbnails:
                        # Try to get the highest quality thumbnail
                        for quality in ['maxres', 'high', 'medium', 'default', 'standard']:
                            for thumb in thumbnails:
                                if isinstance(thumb, dict) and thumb.get('id') == quality:
                                    best_thumbnail = thumb.get('url', '')
                                    break
                            if best_thumbnail:
                                break
                    
                    # Fallback to the basic thumbnail if no better one was found
                    if not best_thumbnail:
                        best_thumbnail = info.get('thumbnail', '')
                    
                    response_data = {
                        'title': info.get('title', 'Unknown title'),
                        'duration': info.get('duration', 0),
                        'thumbnail': best_thumbnail,
                        'uploader': info.get('uploader', 'Unknown uploader'),
                    }
                    
                    logger.info(f"Fallback extraction succeeded: {response_data['title']}")
                    return jsonify(response_data)
                    
            except Exception as fallback_error:
                logger.error(f"Fallback extraction also failed: {fallback_error}")
                raise fallback_error
            
    except Exception as e:
        logger.error(f"Error getting video info: {e}")
        return jsonify({'error': f"Failed to extract video info: {str(e)}"}), 500

@app.route('/download', methods=['POST'])
def download_video():
    """Download video from YouTube URL"""
    data = request.get_json()
    logger.info(f"Download data received: {data}")
    
    url = data.get('url', '')
    
    # Create a unique filename
    timestamp = int(time.time())
    temp_file = os.path.join(temp_dir, f"yt_video_{timestamp}")
    temp_file_mp4 = temp_file + '.mp4'  # Default to mp4 for backup methods
    logger.info(f"Temp file path: {temp_file}")
    
    # For testing, use the simplest command possible
    try:
        subprocess.run(['pip', 'list'], capture_output=True, text=True)
        
        # Simplified approach - use a direct system command with 360p format 
        # Adding more options to handle various restrictions
        cmd = f"yt-dlp -f 'bestvideo[height<=360]+bestaudio/best[height<=360]' --no-check-certificates --geo-bypass --ignore-errors -o '{temp_file_mp4}' '{url}'"
        logger.info(f"Running direct command: {cmd}")
        subprocess.run(cmd, shell=True, check=True)
        
        if os.path.exists(temp_file_mp4):
            logger.info(f"File downloaded successfully: {temp_file_mp4}")
            
            # Get metadata
            try:
                with yt_dlp.YoutubeDL({'quiet': True, 'skip_download': True, 'nocheckcertificate': True, 'ignoreerrors': True}) as ydl:
                    info = ydl.extract_info(url, download=False)
                
                # Get the best thumbnail available
                thumbnails = info.get('thumbnails', [])
                best_thumbnail = ''
                
                if thumbnails:
                    # Try to get the highest quality thumbnail
                    for quality in ['maxres', 'high', 'medium', 'default', 'standard']:
                        for thumb in thumbnails:
                            if isinstance(thumb, dict) and thumb.get('id') == quality:
                                best_thumbnail = thumb.get('url', '')
                                break
                        if best_thumbnail:
                            break
                
                # Fallback to the basic thumbnail if no better one was found
                if not best_thumbnail:
                    best_thumbnail = info.get('thumbnail', '')
                
                video_info = {
                    'youtube_id': info.get('id', ''),
                    'title': info.get('title', 'Unknown'),
                    'duration': info.get('duration', 0),
                    'thumbnail_url': best_thumbnail,
                    'uploader': info.get('uploader', 'Unknown uploader'),
                    'filename': temp_file_mp4
                }
            except Exception as e:
                logger.warning(f"Could not get metadata: {e}")
                # Fallback metadata
                video_info = {
                    'youtube_id': url.split('v=')[-1] if 'v=' in url else url.split('/')[-1],
                    'title': os.path.basename(temp_file_mp4),
                    'duration': 0,
                    'thumbnail_url': '',
                    'uploader': 'Unknown',
                    'filename': temp_file_mp4
                }
                
            return process_downloaded_video(url, video_info)
        
    except Exception as e:
        logger.error(f"Download error: {e}")
        
        # Last attempt - basic pytube with additional error handling
        try:
            logger.info("Trying basic pytube...")
            from pytube import YouTube
            from pytube.exceptions import RegexMatchError, VideoUnavailable
            
            try:
                yt = YouTube(url, use_oauth=False, allow_oauth_cache=False)
            except RegexMatchError:
                logger.error("PyTube couldn't parse the URL")
                raise
            except VideoUnavailable:
                logger.error("PyTube reports video unavailable")
                raise
                
            # Try to get the highest quality stream with specific format if possible
            logger.info("Getting available streams...")
            highest_res_stream = None
            
            try:
                # Try to get a progressive MP4 stream first
                highest_res_stream = yt.streams.filter(progressive=True, file_extension='mp4').order_by('resolution').desc().first()
                logger.info(f"Found progressive stream: {highest_res_stream}")
            except Exception as stream_error:
                logger.warning(f"Error getting progressive stream: {stream_error}")
                
            # Fallback to any available stream if no progressive stream found
            if not highest_res_stream:
                try:
                    highest_res_stream = yt.streams.get_highest_resolution()
                    logger.info(f"Falling back to highest resolution: {highest_res_stream}")
                except Exception as fallback_error:
                    logger.warning(f"Error getting highest resolution: {fallback_error}")
            
            if highest_res_stream:
                logger.info(f"Downloading with stream: {highest_res_stream}")
                download_path = highest_res_stream.download(output_path=temp_dir, filename=f"yt_video_{timestamp}.mp4")
                logger.info(f"Downloaded with pytube: {download_path}")
                
                if os.path.exists(download_path):
                    video_info = {
                        'youtube_id': yt.video_id,
                        'title': yt.title,
                        'duration': yt.length,
                        'thumbnail_url': yt.thumbnail_url,
                        'uploader': yt.author,
                        'filename': download_path
                    }
                    return process_downloaded_video(url, video_info)
                else:
                    logger.error(f"PyTube reported success but file doesn't exist at {download_path}")
            else:
                logger.error("No suitable stream found for download")
        except Exception as pytube_error:
            logger.error(f"Basic pytube error: {pytube_error}")
    
    # If all methods fail
    return jsonify({'error': 'All download methods failed'}), 500

def process_downloaded_video(url, video_info):
    """Process a successfully downloaded video and create database entry"""
    try:
        filename = video_info['filename']
        # Get file size
        file_size = os.path.getsize(filename)
        logger.info(f"File size: {file_size} bytes")
        
        # Create database record
        video = Video(
            youtube_id=video_info['youtube_id'],
            title=video_info['title'],
            url=url,
            duration=video_info['duration'],
            thumbnail_url=video_info['thumbnail_url'],
            uploader=video_info['uploader'],
            file_size=file_size,
            download_success=True,
            uploaded_to_youtube=False,
            youtube_upload_id=None
        )
        
        db.session.add(video)
        db.session.commit()
        logger.info(f"Video record created with ID: {video.id}")
        
        # Add video ID to response for later reference
        response_data = {
            'status': 'success',
            'message': 'Video downloaded successfully',
            'filename': filename,
            'title': video_info['title'],
            'video_id': video.id
        }
        logger.info(f"Sending response with data: {response_data}")
        return jsonify(response_data)
    except Exception as e:
        logger.error(f"Error processing downloaded video: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/upload_to_drive', methods=['POST'])
def upload_to_drive():
    """Upload video to Google Drive"""
    try:
        data = request.get_json()
        filename = data.get('filename', '')
        folder_id = data.get('folder_id', None)
        video_id = data.get('video_id', None)
        
        if not os.path.exists(filename):
            return jsonify({'error': 'File not found'}), 404
        
        drive_service = get_authenticated_service()
        if not drive_service:
            return jsonify({'error': 'Not authenticated with Google Drive'}), 401
        
        # Create file metadata
        file_metadata = {
            'name': os.path.basename(filename),
        }
        
        if folder_id:
            file_metadata['parents'] = [folder_id]
        
        # Upload file
        media = MediaFileUpload(
            filename, 
            resumable=True
        )
        
        file = drive_service.files().create(
            body=file_metadata,
            media_body=media,
            fields='id'
        ).execute()
        
        # Update database record if video_id was provided
        if video_id:
            video = Video.query.get(video_id)
            if video:
                video.uploaded_to_drive = True
                video.drive_file_id = file.get('id')
                video.drive_folder_id = folder_id
                db.session.commit()
        
        # Don't delete the file if it might be needed for YouTube upload
        # We'll let the user explicitly request deletion when both uploads are complete
        
        return jsonify({
            'status': 'success',
            'message': 'Video uploaded to Google Drive',
            'file_id': file.get('id')
        })
    except Exception as e:
        logger.error(f"Upload error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/get_drive_folders', methods=['GET'])
def get_drive_folders():
    """Get list of Google Drive folders"""
    try:
        drive_service = get_authenticated_service()
        if not drive_service:
            return jsonify({'error': 'Not authenticated with Google Drive'}), 401
        
        # Query all folders including those in shared drives and shared with the user
        results = drive_service.files().list(
            q="mimeType='application/vnd.google-apps.folder'",
            spaces='drive',
            fields='files(id, name, parents)',
            pageSize=1000,  # Get maximum number of folders
            includeItemsFromAllDrives=True,
            supportsAllDrives=True
        ).execute()
        
        folders = results.get('files', [])
        logger.info(f"Found {len(folders)} folders")
        
        # Sort folders by name for better display
        folders.sort(key=lambda x: x.get('name', '').lower())
        
        return jsonify({'folders': folders})
    except Exception as e:
        logger.error(f"Error getting folders: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/upload_to_yt', methods=['POST'])
def upload_to_yt():
    """Upload video to YouTube with original metadata"""
    try:
        data = request.get_json()
        logger.info(f"YouTube upload with original metadata data received: {data}")
        
        filename = data.get('filename', '')
        video_id = data.get('video_id', None)
        privacy_status = data.get('privacy_status', 'private')
        
        if not os.path.exists(filename):
            return jsonify({'error': 'File not found'}), 404
        
        # Get the original video's metadata
        if video_id:
            video = Video.query.get(video_id)
            if not video:
                return jsonify({'error': 'Video record not found'}), 404
            
            youtube_id = video.youtube_id
            
            # Get metadata using yt-dlp
            try:
                logger.info(f"Fetching metadata for video ID: {youtube_id}")
                
                with yt_dlp.YoutubeDL({'quiet': True, 'skip_download': True}) as ydl:
                    info = ydl.extract_info(f"https://www.youtube.com/watch?v={youtube_id}", download=False)
                
                title = info.get('title', 'Unknown title')
                description = info.get('description', '')
                tags = info.get('tags', [])
                category_id = info.get('categories', ['22'])[0] if info.get('categories') else '22'
                
                logger.info(f"Original metadata fetched: title='{title}', tags={tags}")
            except Exception as e:
                logger.error(f"Error fetching original metadata: {e}")
                return jsonify({'error': f'Could not fetch original metadata: {str(e)}'}), 500
        else:
            return jsonify({'error': 'Video ID is required for upload with original metadata'}), 400
        
        # Now upload to YouTube with the original metadata
        # Google OAuth credentials from session
        if 'credentials' not in session:
            return jsonify({'error': 'Not authenticated with Google, please login first'}), 401
        
        # Build the YouTube API service object
        creds_data = json.loads(session['credentials'])
        credentials = Credentials(
            token=creds_data['token'],
            refresh_token=creds_data['refresh_token'],
            token_uri=creds_data['token_uri'],
            client_id=creds_data['client_id'],
            client_secret=creds_data['client_secret'],
            scopes=creds_data['scopes']
        )
        
        # Check if YouTube scope is present
        if 'https://www.googleapis.com/auth/youtube.upload' not in credentials.scopes:
            return jsonify({'error': 'YouTube upload permission not granted', 'action_required': 'reauth'}), 403
        
        # Build YouTube API client
        youtube = build('youtube', 'v3', credentials=credentials)
        
        # Prepare video upload metadata
        body = {
            'snippet': {
                'title': title,
                'description': description,
                'tags': tags,
                'categoryId': '22'  # Using default category 22 (People & Blogs) as safe default
            },
            'status': {
                'privacyStatus': privacy_status,
                'embeddable': True
            }
        }
        
        # Upload the video
        media = MediaFileUpload(
            filename,
            resumable=True
        )
        
        # Execute the upload request
        insert_request = youtube.videos().insert(
            part=','.join(body.keys()),
            body=body,
            media_body=media
        )
        
        # This is a simplified version without a resumable upload
        response = insert_request.execute()
        
        logger.info(f"YouTube upload successful: {response}")
        
        # Update database
        if video_id:
            video = Video.query.get(video_id)
            if video:
                video.uploaded_to_youtube = True
                video.youtube_upload_id = response.get('id')
                db.session.commit()
        
        # Return success response with the YouTube video ID
        return jsonify({
            'status': 'success',
            'message': 'Video uploaded to YouTube with original metadata',
            'youtube_video_id': response.get('id')
        })
        
    except Exception as e:
        logger.error(f"Error uploading to YouTube with original metadata: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/upload_to_youtube', methods=['POST'])
def upload_to_youtube():
    """Upload video to YouTube"""
    try:
        data = request.get_json()
        logger.info(f"YouTube upload data received: {data}")
        
        filename = data.get('filename', '')
        video_id = data.get('video_id', None)
        title = data.get('title', os.path.basename(filename))
        description = data.get('description', 'Uploaded via YouTube Downloader App')
        tags = data.get('tags', '').split(',') if data.get('tags') else []
        category_id = data.get('category_id', '22')  # Default: "People & Blogs"
        privacy_status = data.get('privacy_status', 'private')  # Default: private
        
        logger.info(f"Checking file: {filename}, exists: {os.path.exists(filename) if filename else 'No filename provided'}")
        
        if not filename:
            return jsonify({'error': 'No video file available for upload'}), 400
            
        if not os.path.exists(filename):
            return jsonify({'error': 'File not found'}), 404
            
        # Get authenticated service
        credentials_data = json.loads(session.get('credentials', '{}'))
        if not credentials_data:
            return jsonify({'error': 'Not authenticated with Google'}), 401
            
        credentials = Credentials(
            token=credentials_data.get('token'),
            refresh_token=credentials_data.get('refresh_token'),
            token_uri=credentials_data.get('token_uri'),
            client_id=credentials_data.get('client_id'),
            client_secret=credentials_data.get('client_secret'),
            scopes=credentials_data.get('scopes')
        )
        
        if credentials.expired:
            request_obj = Request()
            credentials.refresh(request_obj)
            # Update stored credentials
            credentials_data['token'] = credentials.token
            session['credentials'] = json.dumps(credentials_data)
            
        youtube_service = build('youtube', 'v3', credentials=credentials)
        
        # Create video metadata
        body = {
            'snippet': {
                'title': title,
                'description': description,
                'tags': tags,
                'categoryId': '22'  # Using default category 22 (People & Blogs) as safe default
            },
            'status': {
                'privacyStatus': privacy_status
            }
        }
        
        # Create media upload with progress reporting
        media = MediaFileUpload(
            filename, 
            mimetype='video/mp4',
            resumable=True,
            chunksize=1024*1024
        )
        
        # Upload video
        request_obj = youtube_service.videos().insert(
            part=','.join(body.keys()),
            body=body,
            media_body=media
        )
        
        response = None
        last_progress = 0
        
        while response is None:
            status, response = request_obj.next_chunk()
            if status:
                progress = int(status.progress() * 100)
                # Only log if progress has changed significantly
                if progress - last_progress >= 5:
                    logger.info(f"YouTube upload progress: {progress}%")
                    last_progress = progress
        
        youtube_video_id = response.get('id')
        logger.info(f"Video uploaded successfully to YouTube. ID: {youtube_video_id}")
        
        # Update database record
        if video_id:
            video = Video.query.get(video_id)
            if video:
                video.uploaded_to_youtube = True
                video.youtube_upload_id = youtube_video_id
                db.session.commit()
        
        # Now that YouTube upload is complete, we can safely clean up if Drive upload also happened
        if video_id:
            video = Video.query.get(video_id)
            if video and video.uploaded_to_drive and os.path.exists(filename):
                logger.info(f"Cleaning up file after successful uploads: {filename}")
                try:
                    os.remove(filename)
                except Exception as e:
                    logger.error(f"Error removing temporary file: {e}")
                
        return jsonify({
            'status': 'success',
            'message': 'Video uploaded successfully to YouTube',
            'youtube_video_id': youtube_video_id,
            'watch_url': f'https://www.youtube.com/watch?v={youtube_video_id}'
        })
    except Exception as e:
        error_message = str(e)
        logger.error(f"YouTube upload error: {error_message}")
        
        # Check for permission error
        if "insufficientPermissions" in error_message or "insufficient authentication scopes" in error_message:
            return jsonify({
                'error': 'YouTube API permissions are missing. Please log out and log back in to grant all required permissions.',
                'action_required': 'reauth',
                'details': error_message
            }), 403
            
        return jsonify({'error': error_message}), 500

@app.route('/download_file/<path:filename>')
def download_file(filename):
    """Download a file to user's device"""
    try:
        # Ensure the file exists and is within the temp directory
        if not os.path.exists(filename) or not filename.startswith(temp_dir):
            return jsonify({'error': 'File not found'}), 404
            
        return send_file(
            filename,
            as_attachment=True,
            download_name=os.path.basename(filename)
        )
    except Exception as e:
        logger.error(f"Error downloading file: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/history', methods=['GET'])
def get_history():
    """Get download/upload history"""
    try:
        # Get all videos from database, ordered by download date (newest first)
        videos = Video.query.order_by(Video.download_date.desc()).all()
        
        # Convert to list of dictionaries
        video_list = [video.to_dict() for video in videos]
        
        return jsonify({
            'status': 'success',
            'videos': video_list
        })
    except Exception as e:
        logger.error(f"Error getting history: {e}")
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(host="0.0.0.0", port=5000, debug=True)
