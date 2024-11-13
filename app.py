from flask import Flask # type: ignore
from config import Config
from models import db
from api.customers import customers_bp
from api.orders import orders_bp
import MySQLdb
from dotenv import dotenv_values

# Load environment variables
config = dotenv_values(".env")

def create_database():
    """
    Connects to the MySQL server and creates the database if it doesn't exist
    """
    connection = MySQLdb.connect(
        host=config['MYSQL_HOST'],
        user=config['MYSQL_USER'],
        passwd=config['MYSQL_PASSWORD']
    )
    cursor = connection.cursor()
    print("Connected to MySQL")
    cursor.execute(f"CREATE DATABASE IF NOT EXISTS {config['MYSQL_DB']}")
    print("Database created")
    connection.close()

app = Flask(__name__)
app.config.from_object(Config)

# Create the database if it doesn't exist
create_database()

# Initialize the database
db.init_app(app)
with app.app_context():
    db.create_all()

# Register the endpoint blueprints
app.register_blueprint(customers_bp, url_prefix='/customers')
app.register_blueprint(orders_bp, url_prefix='/orders')

if __name__ == '__main__':
    app.run(debug=True, port=5001)
