from main import app
import logging

logger = logging.getLogger(__name__)

if __name__ == '__main__':
    logger.info("Starting Flask application")
    app.run(host='0.0.0.0', port=5000, debug=True)