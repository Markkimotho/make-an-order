from dotenv import dotenv_values

# Loading environment variables to a dict
config = dotenv_values(".env")

class Config:
    SECRET_KEY = config["APP_SECRET_KEY"]
    SESSION_TYPE = "filesystem"
    SQLALCHEMY_DATABASE_URI = f"mysql://{config['MYSQL_USER']}:{config['MYSQL_PASSWORD']}@{config['MYSQL_HOST']}/{config['MYSQL_DB']}"
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    GOOGLE_CLIENT_ID = config["GOOGLE_CLIENT_ID"]
    GOOGLE_CLIENT_SECRET = config["GOOGLE_CLIENT_SECRET"]