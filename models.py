from flask_sqlalchemy import SQLAlchemy # type: ignore

db = SQLAlchemy()

class Customer(db.Model):
    """
    Customer: Model to represent a customer in the database
    -----------
    Paramaters:
    db.Model - A parameter for initializing models in flask's SQLAlchemy

    -----------
    Attributes/Column Names:
    -----------
    id(int): Unique identifier for a customer; a PRIMARY KEY
    name(str): Name of the customer
    phone_number(str): Unique phone number of the customer
    code(str): Unique code assciated with customer for secondary identification
    """
    __tablename__ = 'customers'
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name = db.Column(db.String(255), nullable=False)
    phone_number = db.Column(db.String(15), unique=True, nullable=False)
    code = db.Column(db.String(50), unique=True, nullable=False)

    # Relationship to Order
    orders = db.relationship('Order', backref='customer', lazy=True,  cascade='all, delete')

    def __repr__(self):
        return f"<Customer(id={self.id}, name={self.name}, phone_number={self.phone_number}, code={self.code})>"

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "phone_number": self.phone_number,
            "code": self.code
        }

class Order(db.Model):
    """
    Order: Model to represent an order in the database
    -----------
    Paramaters:
    db.Model - A parameter for initializing models in flask's SQLAlchemy

    -----------
    Attributes/Column Names:
    -----------
    id(int): Unique identifier for an order; a PRIMARY KEY
    customer_id(int): Key used to link the order to the customer who made it: a FOREIGN KEY
    item(str): Name of the item ordered by the customer
    amount(decimal): Amount of money cost for the order
    time(datetieme): Timestamp of when the order was made.
    """
    __tablename__ = 'orders'
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customers.id'), nullable=False)
    item = db.Column(db.String(255), nullable=False)
    amount = db.Column(db.Numeric(10, 2), nullable=False)
    time = db.Column(db.DateTime, server_default=db.func.now())

    def __repr__(self):
        return f"<Order(id={self.id}, customer_id={self.customer_id}, item={self.item}, amount={self.amount}, time={self.time})>"

    def to_dict(self):
        return {
            "id": self.id,
            "customer_id": self.customer_id,
            "item": self.item,
            "amount": float(self.amount), # Ensure it's float for JSON serialization
            "time": self.time.isoformat() if self.time else None # ISO format for datetime
        }