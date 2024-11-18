# services/database_services.py
import MySQLdb
from config import Config



def create_database():
    """
    Connects to the MySQL server and creates the database if it doesn't exist.
    """
    connection = MySQLdb.connect(
        host=Config.MYSQL_HOST,
        user=Config.MYSQL_USER,
        passwd=Config.MYSQL_PASSWORD  
    )
    cursor = connection.cursor()
    print("Connected to MySQL")
    cursor.execute(f"CREATE DATABASE IF NOT EXISTS {config['MYSQL_DB']}")
    print("Database created (or already exists)")
    connection.close()
