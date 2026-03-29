from flask_sqlalchemy import SQLAlchemy # type: ignore
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()

SUPERUSER_EMAIL = 'kimothomark93@gmail.com'

class User(db.Model):
    __tablename__ = 'users'
    id            = db.Column(db.Integer, primary_key=True, autoincrement=True)
    email         = db.Column(db.String(255), unique=True, nullable=False)
    name          = db.Column(db.String(255), nullable=True)
    phone_number  = db.Column(db.String(20), nullable=True)
    role          = db.Column(db.String(20), default='user', nullable=False)   # 'superuser'|'admin'|'user'
    status        = db.Column(db.String(20), default='pending', nullable=False) # 'pending'|'approved'|'rejected'
    password_hash = db.Column(db.String(255), nullable=True)                   # null for OAuth-only users
    created_at    = db.Column(db.DateTime, server_default=db.func.now())

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        if not self.password_hash:
            return False
        return check_password_hash(self.password_hash, password)

    def to_dict(self):
        return {
            'id':           self.id,
            'email':        self.email,
            'name':         self.name,
            'phone_number': self.phone_number,
            'role':         self.role,
            'status':       self.status,
            'created_at':   self.created_at.isoformat() if self.created_at else None,
        }


class InventoryItem(db.Model):
    __tablename__ = 'inventory_items'
    id          = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name        = db.Column(db.String(255), nullable=False)
    description = db.Column(db.String(500), nullable=True)
    price       = db.Column(db.Numeric(10, 2), nullable=False)
    category    = db.Column(db.String(100), nullable=False)
    stock       = db.Column(db.Integer, default=0, nullable=False)
    icon        = db.Column(db.String(80), nullable=True)   # FA icon class
    sku         = db.Column(db.String(50), unique=True, nullable=True)  # Barcode/SKU

    def to_dict(self):
        return {
            'id':          self.id,
            'name':        self.name,
            'description': self.description,
            'price':       float(self.price),
            'category':    self.category,
            'stock':       self.stock,
            'icon':        self.icon,
            'sku':         self.sku,
        }


class Customer(db.Model):
    __tablename__ = 'customers'
    id           = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name         = db.Column(db.String(255), nullable=False)
    email        = db.Column(db.String(255), nullable=True)
    phone_number = db.Column(db.String(15), unique=True, nullable=False)
    code         = db.Column(db.String(50), unique=True, nullable=False)

    orders = db.relationship('Order', backref='customer', lazy=True, cascade='all, delete')

    def __repr__(self):
        return f"<Customer(id={self.id}, name={self.name}, phone_number={self.phone_number}, code={self.code})>"

    def to_dict(self):
        return {
            'id':           self.id,
            'name':         self.name,
            'email':        self.email,
            'phone_number': self.phone_number,
            'code':         self.code,
        }


class Order(db.Model):
    __tablename__ = 'orders'
    id                = db.Column(db.Integer, primary_key=True, autoincrement=True)
    customer_id       = db.Column(db.Integer, db.ForeignKey('customers.id'), nullable=False)
    # item/amount kept for backward compat and as a human-readable summary
    item              = db.Column(db.String(255), nullable=False)
    amount            = db.Column(db.Numeric(10, 2), nullable=False)
    status            = db.Column(db.String(20), default='pending', nullable=False)
    # 'pending' | 'confirmed' | 'completed' | 'cancelled' | 'refunded'
    payment_method    = db.Column(db.String(20), nullable=True)   # 'cash' | 'mpesa' | 'card'
    payment_reference = db.Column(db.String(100), nullable=True)  # e.g. M-Pesa transaction code
    time              = db.Column(db.DateTime, server_default=db.func.now())

    order_items = db.relationship('OrderItem', backref='order', lazy=True, cascade='all, delete-orphan')

    def __repr__(self):
        return f"<Order(id={self.id}, customer_id={self.customer_id}, item={self.item}, amount={self.amount})>"

    def to_dict(self):
        return {
            'id':                self.id,
            'customer_id':       self.customer_id,
            'item':              self.item,
            'amount':            float(self.amount),
            'status':            self.status,
            'payment_method':    self.payment_method,
            'payment_reference': self.payment_reference,
            'time':              self.time.isoformat() if self.time else None,
            'items':             [oi.to_dict() for oi in self.order_items],
        }


class StockMovement(db.Model):
    """Audit log for every stock level change."""
    __tablename__ = 'stock_movements'
    id         = db.Column(db.Integer, primary_key=True, autoincrement=True)
    item_id    = db.Column(db.Integer, db.ForeignKey('inventory_items.id', ondelete='CASCADE'), nullable=False)
    item_name  = db.Column(db.String(255), nullable=False)   # name snapshot at time of movement
    delta      = db.Column(db.Integer, nullable=False)        # +N = stock in, -N = stock out
    reason     = db.Column(db.String(50), nullable=False)
    # 'sale' | 'restock' | 'cancel' | 'refund' | 'initial_stock' | 'manual'
    user_id    = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
    note       = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, server_default=db.func.now())

    def to_dict(self):
        return {
            'id':         self.id,
            'item_id':    self.item_id,
            'item_name':  self.item_name,
            'delta':      self.delta,
            'reason':     self.reason,
            'user_id':    self.user_id,
            'note':       self.note,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


class OrderItem(db.Model):
    __tablename__ = 'order_items'
    id                = db.Column(db.Integer, primary_key=True, autoincrement=True)
    order_id          = db.Column(db.Integer, db.ForeignKey('orders.id', ondelete='CASCADE'), nullable=False)
    inventory_item_id = db.Column(db.Integer, db.ForeignKey('inventory_items.id', ondelete='SET NULL'), nullable=True)
    item_name         = db.Column(db.String(255), nullable=False)  # snapshot at time of order
    quantity          = db.Column(db.Integer, nullable=False, default=1)
    unit_price        = db.Column(db.Numeric(10, 2), nullable=False)
    subtotal          = db.Column(db.Numeric(10, 2), nullable=False)

    def to_dict(self):
        return {
            'id':                self.id,
            'inventory_item_id': self.inventory_item_id,
            'item_name':         self.item_name,
            'quantity':          self.quantity,
            'unit_price':        float(self.unit_price),
            'subtotal':          float(self.subtotal),
        }
