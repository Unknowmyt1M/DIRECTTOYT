import os
from app import app, store_api_credentials

def init_google_credentials():
    """Initialize Google API credentials from environment variables"""
    with app.app_context():
        client_id = os.environ.get('GOOGLE_CLIENT_ID')
        client_secret = os.environ.get('GOOGLE_CLIENT_SECRET')
        
        if client_id and client_secret:
            success = store_api_credentials('google', client_id, client_secret)
            if success:
                print('Google API credentials stored successfully.')
            else:
                print('Failed to store Google API credentials.')
        else:
            print('Google API credentials not found in environment variables.')

if __name__ == '__main__':
    init_google_credentials()