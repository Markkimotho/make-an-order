# services/database_services.py
import MySQLdb # type: ignore
from config import Config
import os

def create_database():
    """
    Connects to the MySQL server and creates the database if it doesn't exist.
    """

    if os.environ.get("JAWSDB_URL"):
        print("Skipping database creation on JawsDB")
        return
    
    connection = MySQLdb.connect(
        host=Config.MYSQL_HOST,
        user=Config.MYSQL_USER,
        passwd=Config.MYSQL_PASSWORD,
        db=Config.MYSQL_DB
    )
    cursor = connection.cursor()
    print("Connected to MySQL")

    cursor.execute(f"CREATE DATABASE IF NOT EXISTS {Config.MYSQL_DB}")

    print("Database created (or already exists)")
    
    connection.close()
