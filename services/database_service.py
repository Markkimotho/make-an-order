# import MySQLdb # No longer needed if not explicitly using MySQLdb for local
import os
import logging
import psycopg2 # Import psycopg2 for potential local PostgreSQL creation, though not strictly used by this function if DATABASE_URL exists
from config import Config # Import Config to access DATABASE_URL
import pymysql


logger = logging.getLogger(__name__)

def create_database():
    """
    Connects to the MySQL/PostgreSQL server and creates the database if it doesn't exist.
    This function is primarily for local development setup.
    It will be skipped if a DATABASE_URL (indicating a hosted DB) is present.
    """
    # If DATABASE_URL is set (e.g., on Render), assume the DB is already provisioned
    if os.environ.get("DATABASE_URL"):
        logger.info("DATABASE_URL detected. Skipping local database creation (assuming hosted DB is used).")
        return

    # Fallback to local MySQL creation logic (as before)
    # This block will only execute if DATABASE_URL is NOT set, meaning local dev.
    try:
        # Use PyMySQL for local MySQL connection
        connection = pymysql.connect(
            host=Config.MYSQL_HOST,
            user=Config.MYSQL_USER,
            passwd=Config.MYSQL_PASSWORD
        )
        cursor = connection.cursor()
        logger.info("Connected to local MySQL server for creation check.")

        # Check if DB exists and create if not
        cursor.execute(f"CREATE DATABASE IF NOT EXISTS `{Config.MYSQL_DB}`")
        logger.info(f"Local MySQL Database '{Config.MYSQL_DB}' created (or already exists).")

        connection.close()
    except Exception as e:
        logger.error(f"Error connecting to local MySQL or creating database: {e}", exc_info=True)
        # In a real app, raise this error to prevent startup
        # if the local DB isn't available for development.