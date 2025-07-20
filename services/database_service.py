import MySQLdb # type: ignore
from config import Config
import os
import logging

logger = logging.getLogger(__name__)

def create_database():
    """
    Connects to the MySQL server and creates the database if it doesn't exist.
    """
    if os.environ.get("JAWSDB_URL"):
        logger.info("Skipping database creation on JawsDB (detected JAWSDB_URL).")
        return

    try:
        connection = MySQLdb.connect(
            host=Config.MYSQL_HOST,
            user=Config.MYSQL_USER,
            passwd=Config.MYSQL_PASSWORD
            # db parameter is omitted initially to allow creation
        )
        cursor = connection.cursor()
        logger.info("Connected to MySQL server.")

        # Check if DB exists and create if not
        cursor.execute(f"CREATE DATABASE IF NOT EXISTS `{Config.MYSQL_DB}`")
        logger.info(f"Database '{Config.MYSQL_DB}' created (or already exists).")

        connection.close()
    except Exception as e:
        logger.error(f"Error connecting to MySQL or creating database: {e}", exc_info=True)
        # In a real app, raise this error or handle it more gracefully
        # to prevent the app from starting if the DB isn't available.