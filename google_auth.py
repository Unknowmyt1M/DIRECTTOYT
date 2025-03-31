# Use this Flask blueprint for Google authentication

import json
import os
import logging

import requests
from flask import Blueprint, redirect, request, url_for, session, jsonify
from flask_login import login_user, logout_user, login_required
from oauthlib.oauth2 import WebApplicationClient
from models import db, User

logger = logging.getLogger(__name__)

# Get Google OAuth credentials from environment variables
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")
GOOGLE_DISCOVERY_URL = "https://accounts.google.com/.well-known/openid-configuration"

# Get the Replit domain for the redirect URI
# Try different environment variables that Replit might set
REPLIT_DOMAIN = (os.environ.get("REPLIT_DEV_DOMAIN") or 
                os.environ.get("REPL_ID") or 
                os.environ.get("REPL_SLUG"))

# If we have a domain directly
if os.environ.get("REPLIT_DEV_DOMAIN"):
    REDIRECT_URI = f"https://{os.environ.get('REPLIT_DEV_DOMAIN')}/google_login/callback"
    logger.info(f"Replit domain found: {os.environ.get('REPLIT_DEV_DOMAIN')}")
# Try to construct from REPL_ID and REPL_SLUG if available
elif os.environ.get("REPL_ID") and os.environ.get("REPL_OWNER"):
    constructed_domain = f"{os.environ.get('REPL_ID')}-00-{os.environ.get('REPL_OWNER')}.pike.replit.dev"
    REDIRECT_URI = f"https://{constructed_domain}/google_login/callback"
    logger.info(f"Constructed Replit domain: {constructed_domain}")
# Fixed domain for this specific repl
elif "6760832c-25c3-4887-a0ac-2fd04f8cc424" in os.environ.get("REPL_ID", ""):
    REDIRECT_URI = "https://6760832c-25c3-4887-a0ac-2fd04f8cc424-00-rfvyv0lk9fdr.pike.replit.dev/google_login/callback"
    logger.info(f"Using fixed Replit domain from REPL_ID")
else:
    # Hardcoded domain as fallback - get this from the error message or browser URL
    REDIRECT_URI = "https://6760832c-25c3-4887-a0ac-2fd04f8cc424-00-rfvyv0lk9fdr.pike.replit.dev/google_login/callback"
    logger.warning("No Replit domain variables found. Using hardcoded domain.")

logger.info(f"Using redirect URI: {REDIRECT_URI}")

# Client configuration
client = None
if GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET:
    client = WebApplicationClient(GOOGLE_CLIENT_ID)
    # ALWAYS display setup instructions to the user
    logger.info(f"""Google Auth is configured. 
    Make sure to add this redirect URI to your Google Cloud Console:
    {REDIRECT_URI or "https://YOUR_REPLIT_DOMAIN/google_login/callback"}""")
else:
    logger.warning("Google OAuth credentials are not set. Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in environment variables.")

google_auth = Blueprint("google_auth", __name__)

# Define the OAuth scopes needed
SCOPES = [
    'https://www.googleapis.com/auth/drive',  # Full Drive access to see all folders
    'https://www.googleapis.com/auth/drive.file',  # Access to files created by app
    'https://www.googleapis.com/auth/youtube',  # Full access to YouTube on behalf of user
    'https://www.googleapis.com/auth/youtube.upload',  # Upload videos to YouTube
    'openid', 
    'email', 
    'profile'
]

@google_auth.route("/google_login")
def login():
    """Start the OAuth process for Google login"""
    if not client:
        return redirect(url_for('index', error='Google OAuth credentials are not configured'))
        
    # Get Google's OAuth 2.0 endpoints from discovery document
    try:
        google_provider_cfg = requests.get(GOOGLE_DISCOVERY_URL).json()
        authorization_endpoint = google_provider_cfg["authorization_endpoint"]

        # Always use the hardcoded redirect URI to match Google Cloud Console settings
        redirect_uri = "https://6760832c-25c3-4887-a0ac-2fd04f8cc424-00-rfvyv0lk9fdr.pike.replit.dev/google_login/callback"
        
        logger.info(f"Using redirect URI for auth: {redirect_uri}")

        # Use library to build the request for Google login
        request_uri = client.prepare_request_uri(
            authorization_endpoint,
            redirect_uri=redirect_uri,
            scope=SCOPES,
        )
        return redirect(request_uri)
    except Exception as e:
        logger.error(f"Error starting Google authentication: {e}")
        return redirect(url_for('index', error=str(e)))

@google_auth.route("/google_login/callback")
def callback():
    """Handle the Google OAuth callback"""
    if not client:
        return redirect(url_for('index'))
        
    try:
        # Get authorization code sent by Google
        code = request.args.get("code")
        google_provider_cfg = requests.get(GOOGLE_DISCOVERY_URL).json()
        token_endpoint = google_provider_cfg["token_endpoint"]

        # Always use the hardcoded redirect URI to match Google Cloud Console settings
        redirect_uri = "https://6760832c-25c3-4887-a0ac-2fd04f8cc424-00-rfvyv0lk9fdr.pike.replit.dev/google_login/callback"
        
        logger.info(f"Using redirect URI for token: {redirect_uri}")

        # Prepare token request
        token_url, headers, body = client.prepare_token_request(
            token_endpoint,
            authorization_response=request.url.replace("http://", "https://"),
            redirect_url=redirect_uri,
            code=code,
        )
        
        # Exchange code for tokens
        token_response = requests.post(
            token_url,
            headers=headers,
            data=body,
            auth=(GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET) if GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET else None,
        )

        # Parse the tokens
        client.parse_request_body_response(json.dumps(token_response.json()))
        
        # Now get user information from Google
        userinfo_endpoint = google_provider_cfg["userinfo_endpoint"]
        uri, headers, body = client.add_token(userinfo_endpoint)
        userinfo_response = requests.get(uri, headers=headers, data=body)
        
        # Get user info
        userinfo = userinfo_response.json()
        
        # Get profile information
        if userinfo.get("email_verified"):
            user_email = userinfo["email"]
            user_name = userinfo.get("name") or userinfo.get("given_name", "User")
        else:
            return redirect(url_for('index', error="User email not verified by Google"))
        
        # Look up user or create one
        user = User.query.filter_by(email=user_email).first()
        if not user:
            user = User(username=user_name, email=user_email)
            db.session.add(user)
            db.session.commit()
            
        # Log in the user
        login_user(user)
        
        # Get tokens from response
        tokens = token_response.json()
        
        # Add missing fields required by google.oauth2.credentials.Credentials
        creds_data = {
            'token': tokens.get('access_token'),
            'refresh_token': tokens.get('refresh_token'),
            'token_uri': 'https://oauth2.googleapis.com/token',
            'client_id': GOOGLE_CLIENT_ID,
            'client_secret': GOOGLE_CLIENT_SECRET,
            'scopes': SCOPES
        }
        
        # Store credentials in session
        session['credentials'] = json.dumps(creds_data)
        
        return redirect(url_for('index', auth_success=True))
    except Exception as e:
        logger.error(f"Error in Google callback: {e}")
        return redirect(url_for('index', auth_error=str(e)))

@google_auth.route("/logout")
@login_required
def logout():
    """Log out the user by clearing session data"""
    session.pop('credentials', None)
    logout_user()
    return redirect(url_for('index'))