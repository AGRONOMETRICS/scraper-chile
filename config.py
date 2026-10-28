from dotenv import dotenv_values, find_dotenv, load_dotenv
import logging
import os


load_dotenv()

# Read straight from the file: USERNAME is also an OS variable on Windows, and
# load_dotenv never overrides one that is already set.
_dotenv = dotenv_values(find_dotenv())


class Settings:
    # True keeps every row the server returns, even if two rows share the same
    # unique_together key. False drops the duplicates (keep first).
    ALLOW_DUPLICATES = True

    # True for testing mode (no actual API calls, read json files from data folder)
    TEST_RESPONSE = False

    # True for creating JSON files from API responses (needed by TEST_RESPONSE)
    CREATE_JSON_FILES = False

    # True for debug mode (detailed logging + post-save DB/server count check)
    DEBUG = True

    # Seconds to wait between page requests so the endpoint does not throttle us.
    REQUEST_DELAY = 1

    # Seconds to wait for a response before giving up on a request.
    REQUEST_TIMEOUT = 120

    # Number of extra attempts per request when the endpoint times out or 5xx's.
    REQUEST_RETRIES = 2

    # Rows per page. The API caps it at 500.
    PAGE_SIZE = 500

    # Global Reefers API. The DecoFrut FCM report is the one dataset this
    # scraper reads: projected fruit shipments by season, market, species and
    # exporter. It only accepts HTTP Basic auth (the Bearer token from
    # /api/token/ is rejected there).
    URL_BASE = os.getenv('URL', 'https://api.globalreefers.app/').rstrip('/')
    REPORT_URL = f'{URL_BASE}/api/reportes/decofrut/'

    API_USERNAME = _dotenv.get('USERNAME') or os.getenv('GLOBALREEFERS_USERNAME')
    API_PASSWORD = (_dotenv.get('PASSWORD') or _dotenv.get('PASSWORSD')
                    or os.getenv('GLOBALREEFERS_PASSWORD'))

    MAILCHIMP_API_KEY = os.getenv('MAILCHIMP_API_KEY')
    FROM_NAME = os.getenv('FROM_NAME', 'Agronometrics Chile Scraper')
    FROM_EMAIL = os.getenv('FROM_EMAIL', 'info@agronometrics.com')


settings = Settings()


def get_logger(name=None, log_filename=None):
    if not name or not log_filename:
        raise ValueError("[get_logger] Both name and log_filename must be provided.")
    log_level_str = os.getenv('LOG_LEVEL', 'INFO').upper()
    level = getattr(logging, log_level_str, logging.INFO)

    logger = logging.getLogger(name)
    logger.setLevel(level)

    if logger.hasHandlers():
        logger.handlers.clear()

    formatter = logging.Formatter(
        '%(asctime)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.DEBUG)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # Create logs directory in the same directory as this script (project root)
    base_dir = os.path.dirname(os.path.abspath(__file__))
    log_dir = os.path.join(base_dir, "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, log_filename)

    file_handler = logging.FileHandler(log_path, mode='a', encoding='utf-8')
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger
