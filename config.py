from dotenv import load_dotenv
import os

# Load environment variables from .env file
load_dotenv()
class Config:
    SECRET_KEY = os.environ.get("APP_SECRET_KEY")
    SESSION_TYPE = "filesystem"
    SQLALCHEMY_DATABASE_URI = os.environ.get("JAWSDB_URL") or f"mysql://{os.environ.get('MYSQL_USER')}:{os.environ.get('MYSQL_PASSWORD')}@{os.environ.get('MYSQL_HOST')}/{os.environ.get('MYSQL_DB')}"
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
    GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")

    # Africa's Talking credentials
    AT_USERNAME = os.environ.get("AT_USERNAME")
    AT_API_KEY = os.environ.get("AT_API_KEY")
    AT_SENDER_ID = os.environ.get("AT_SENDER_ID")

    # DB Credentials
    MYSQL_HOST=os.environ.get("MYSQL_HOST")
    MYSQL_USER=os.environ.get("MYSQL_USER")
    MYSQL_PASSWORD=os.environ.get("MYSQL_PASSWORD")
    MYSQL_DB=os.environ.get("MYSQL_DB")

