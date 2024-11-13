import MySQLdb
from dotenv import dotenv_values

# Load environment variables
config = dotenv_values(".env")

def create_database():
    """
    Connects to the MySQL server and creates the database if it doesn't exist.
    """
    connection = MySQLdb.connect(
        host=config['MYSQL_HOST'],
        user=config['MYSQL_USER'],
        passwd=config['MYSQL_PASSWORD']
    )
    cursor = connection.cursor()
    print("Connected to MySQL")
    cursor.execute(f"CREATE DATABASE IF NOT EXISTS {config['MYSQL_DB']}")
    print("Database created (or already exists)")
    connection.close()
