from dotenv import load_dotenv
import os

# Load environment variables from .env file
load_dotenv()
class Config:
    SECRET_KEY = os.environ.get("APP_SECRET_KEY")
    SESSION_TYPE = "filesystem"
    SQLALCHEMY_DATABASE_URI = os.environ.get("JAWSDB_URL") or \
                              f"mysql+pymysql://{os.environ.get('MYSQL_USER')}:{os.environ.get('MYSQL_PASSWORD')}@{os.environ.get('MYSQL_HOST')}/{os.environ.get('MYSQL_DB')}"
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Base URL for generating absolute URLs (e.g., for OAuth redirects)
    APP_URL = os.environ.get("APP_URL", "http://localhost:5001")

    # Number of reverse-proxy hops to trust for X-Forwarded-For / -Proto.
    # Heroku's router is 1. Add one per CDN/WAF placed in front of it.
    PROXY_FIX_X_FOR   = int(os.environ.get("PROXY_FIX_X_FOR", "1"))
    PROXY_FIX_X_PROTO = int(os.environ.get("PROXY_FIX_X_PROTO", "1"))

    # Multi-tenancy
    # Fernet key for tenant secrets (Daraja keys, eTIMS token). Generate with
    # python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    TENANT_SECRETS_KEY = os.environ.get("TENANT_SECRETS_KEY")
    # Comma-separated emails that get platform-admin rights at login.
    PLATFORM_ADMIN_EMAILS = os.environ.get("PLATFORM_ADMIN_EMAILS", "")

    GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
    GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")

    # Africa's Talking credentials
    AT_USERNAME = os.environ.get("AT_USERNAME")
    AT_API_KEY = os.environ.get("AT_API_KEY")
    AT_SENDER_ID = os.environ.get("AT_SENDER_ID")
    # Optional secret on the delivery-report URL: /delivery-reports?token=...
    AT_DELIVERY_TOKEN = os.environ.get("AT_DELIVERY_TOKEN")

    # DB Credentials (for local, non-Prod setup)
    MYSQL_HOST=os.environ.get("MYSQL_HOST")
    MYSQL_USER=os.environ.get("MYSQL_USER")
    MYSQL_PASSWORD=os.environ.get("MYSQL_PASSWORD")
    MYSQL_DB=os.environ.get("MYSQL_DB")

    # ------------------------------------------------------------------
    # POS extensions
    # ------------------------------------------------------------------

    # M-Pesa Daraja (Safaricom)
    MPESA_CONSUMER_KEY        = os.environ.get("MPESA_CONSUMER_KEY")
    MPESA_CONSUMER_SECRET     = os.environ.get("MPESA_CONSUMER_SECRET")
    MPESA_BUSINESS_SHORT_CODE = os.environ.get("MPESA_BUSINESS_SHORT_CODE")
    MPESA_PASSKEY             = os.environ.get("MPESA_PASSKEY")
    MPESA_ENV                 = os.environ.get("MPESA_ENV", "sandbox")  # 'sandbox' | 'production'

    # Flutterwave (cards). Off by default: card money settles to the platform's
    # account, not the business's. Set CARD_PAYMENTS_ENABLED=1 to turn on.
    CARD_PAYMENTS_ENABLED = os.environ.get("CARD_PAYMENTS_ENABLED", "") in ("1", "true", "yes")

    # Optional: only accept Daraja webhooks from these IPs (comma-separated).
    # Needs PROXY_FIX_X_FOR set right behind a proxy, or every webhook is refused.
    DARAJA_ALLOWED_IPS = os.environ.get("DARAJA_ALLOWED_IPS", "")
    FLW_SECRET_KEY    = os.environ.get("FLW_SECRET_KEY")
    FLW_PUBLIC_KEY    = os.environ.get("FLW_PUBLIC_KEY")
    FLW_WEBHOOK_SECRET = os.environ.get("FLW_WEBHOOK_SECRET")

    # Business / POS settings
    BUSINESS_NAME        = os.environ.get("BUSINESS_NAME", "Make-An-Order POS")
    LOYALTY_POINTS_RATIO = int(os.environ.get("LOYALTY_POINTS_RATIO", "10"))   # KES per point
    TAX_RATE             = float(os.environ.get("TAX_RATE", "0.16"))           # 16% VAT
    LOW_STOCK_DAYS       = int(os.environ.get("LOW_STOCK_DAYS", "30"))         # default expiring window
    MANAGER_OTP_TTL      = int(os.environ.get("MANAGER_OTP_TTL", "300"))       # seconds

    # Redis (for OTPs / future caching). Falls back to in-memory if unreachable.
    REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    # Rate limits on the public pages: Redis when configured (shared by all
    # workers), else per-process memory.
    RATELIMIT_STORAGE_URI = (os.environ.get("RATELIMIT_STORAGE_URI")
                             or os.environ.get("REDIS_URL") or "memory://")
    # Absolute base for links sent by SMS (pay links), e.g. https://app.example.co.ke
    PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL")

    # Backups (services/backup.py, `flask backup ...`, Settings > Backup)
    BACKUP_DIR = os.environ.get("BACKUP_DIR", "backups")
    # Optional rclone remote for an offsite copy of every backup, e.g. gdrive:mao-backups
    BACKUP_RCLONE_REMOTE = os.environ.get("BACKUP_RCLONE_REMOTE", "")
    # Optional MySQL server URL (no database) where `flask backup verify` may
    # create and drop a scratch database, e.g. mysql://root:pw@db:3306
    BACKUP_VERIFY_MYSQL_URL = os.environ.get("BACKUP_VERIFY_MYSQL_URL", "")
    # true on a one-shop install: the owner may download backups. On a shared
    # server a dump holds every business, so only platform admins may.
    SINGLE_BUSINESS_INSTALL = os.environ.get("SINGLE_BUSINESS_INSTALL", "").lower() in ("1", "true", "yes")
