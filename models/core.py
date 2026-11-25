"""Tenant-owned models: every class here carries TenantMixin."""
from decimal import Decimal

from sqlalchemy.orm import validates
from werkzeug.security import generate_password_hash, check_password_hash

from models.base import TenantMixin, db
from services.phone import normalize_ke_phone

# ---- Role constants ---------------------------------------------------------
ROLE_SUPERUSER   = 'superuser'
ROLE_ADMIN       = 'admin'
ROLE_USER        = 'user'
ROLE_CASHIER     = 'cashier'
ROLE_PHARMACIST  = 'pharmacist'
ROLE_STOCK_CLERK = 'stock_clerk'

ALL_ROLES = (
    ROLE_SUPERUSER, ROLE_ADMIN, ROLE_USER,
    ROLE_CASHIER, ROLE_PHARMACIST, ROLE_STOCK_CLERK,
)

# Roles that may operate the POS checkout
POS_ROLES        = (ROLE_CASHIER, ROLE_PHARMACIST, ROLE_ADMIN, ROLE_SUPERUSER)
PHARMACY_ROLES   = (ROLE_PHARMACIST, ROLE_ADMIN, ROLE_SUPERUSER)
STOCK_ROLES      = (ROLE_STOCK_CLERK, ROLE_ADMIN, ROLE_SUPERUSER)


class User(TenantMixin, db.Model):
    __tablename__ = 'users'
    __table_args__ = (
        db.UniqueConstraint('tenant_id', 'phone_number', name='uq_users_tenant_phone_number'),
    )
    # NULL only for platform admins (enforced in auth/tenancy.py before_flush).
    tenant_id     = db.Column(db.Integer, db.ForeignKey('tenants.id', ondelete='CASCADE'),
                              nullable=True, index=True)
    id            = db.Column(db.Integer, primary_key=True, autoincrement=True)
    email         = db.Column(db.String(255), unique=True, nullable=False)
    name          = db.Column(db.String(255), nullable=True)
    phone_number  = db.Column(db.String(20), nullable=True)
    role          = db.Column(db.String(20), default='user', nullable=False)
    # 'superuser' | 'admin' | 'user' | 'cashier' | 'pharmacist' | 'stock_clerk'
    status        = db.Column(db.String(20), default='pending', nullable=False) # 'pending'|'approved'|'rejected'
    password_hash = db.Column(db.String(255), nullable=True)                   # null for OAuth-only users
    is_platform_admin = db.Column(db.Boolean, nullable=False, default=False,
                                  server_default=db.false())
    created_at    = db.Column(db.DateTime, server_default=db.func.now())

    # POS relationships
    shifts        = db.relationship('Shift', backref='cashier', lazy=True,
                                    foreign_keys='Shift.cashier_id')

    @validates('phone_number')
    def _normalize_phone(self, key, value):
        if value is None or str(value).strip() == '':
            return None
        return normalize_ke_phone(value)

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
            'tenant_id':    self.tenant_id,
            'is_platform_admin': bool(self.is_platform_admin),
            'created_at':   self.created_at.isoformat() if self.created_at else None,
        }


class Category(TenantMixin, db.Model):
    """Inventory category lookup. Replaces the legacy free-text `category` field."""
    __tablename__ = 'categories'
    __table_args__ = (
        db.UniqueConstraint('tenant_id', 'name', name='uq_categories_tenant_name'),
    )
    id          = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name        = db.Column(db.String(100), nullable=False)
    description = db.Column(db.String(255), nullable=True)
    created_at  = db.Column(db.DateTime, server_default=db.func.now())

    items = db.relationship('InventoryItem', backref='category_ref', lazy=True)

    def to_dict(self):
        return {'id': self.id, 'name': self.name, 'description': self.description}


class Supplier(TenantMixin, db.Model):
    __tablename__ = 'suppliers'
    id             = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name           = db.Column(db.String(255), nullable=False)
    contact_name   = db.Column(db.String(255), nullable=True)
    phone          = db.Column(db.String(20), nullable=True)
    email          = db.Column(db.String(255), nullable=True)
    address        = db.Column(db.String(500), nullable=True)
    payment_terms  = db.Column(db.String(100), nullable=True)  # e.g. "Net 30"
    created_at     = db.Column(db.DateTime, server_default=db.func.now())

    items           = db.relationship('InventoryItem', backref='supplier', lazy=True)
    purchase_orders = db.relationship('PurchaseOrder', backref='supplier', lazy=True)

    def to_dict(self):
        return {
            'id':            self.id,
            'name':          self.name,
            'contact_name':  self.contact_name,
            'phone':         self.phone,
            'email':         self.email,
            'address':       self.address,
            'payment_terms': self.payment_terms,
            'created_at':    self.created_at.isoformat() if self.created_at else None,
        }


class InventoryItem(TenantMixin, db.Model):
    __tablename__ = 'inventory_items'
    __table_args__ = (
        db.UniqueConstraint('tenant_id', 'sku', name='uq_inventory_items_tenant_sku'),
        db.UniqueConstraint('tenant_id', 'barcode', name='uq_inventory_items_tenant_barcode'),
    )
    id          = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name        = db.Column(db.String(255), nullable=False)
    description = db.Column(db.String(500), nullable=True)
    price       = db.Column(db.Numeric(12, 2), nullable=False)              # selling price
    cost_price  = db.Column(db.Numeric(12, 2), nullable=True)               # for margin reporting
    # Legacy free-text category kept for backward compat. New code should use category_id.
    category    = db.Column(db.String(100), nullable=True)
    category_id = db.Column(db.Integer, db.ForeignKey('categories.id', ondelete='SET NULL'), nullable=True)
    stock       = db.Column(db.Integer, default=0, nullable=False)
    icon        = db.Column(db.String(80), nullable=True)   # FA icon class
    sku         = db.Column(db.String(50), nullable=True)
    barcode     = db.Column(db.String(64), nullable=True)
    reorder_level         = db.Column(db.Integer, default=5, nullable=False)
    # Stock is held in batches (each with its own expiry) or, for sized
    # goods, in variants; ``stock`` is their total, kept by services/stock.py.
    has_variants          = db.Column(db.Boolean, default=False, nullable=False,
                                      server_default=db.false())
    supplier_id           = db.Column(db.Integer, db.ForeignKey('suppliers.id', ondelete='SET NULL'), nullable=True)
    unit                  = db.Column(db.String(20), nullable=True, default='pcs')
    is_drug               = db.Column(db.Boolean, default=False, nullable=False)
    requires_prescription = db.Column(db.Boolean, default=False, nullable=False)

    @staticmethod
    def soonest_batches(item_ids):
        """{item_id: the stocked batch sold next} in one query, for lists."""
        from models.stock import Batch
        ids = list(item_ids)
        if not ids:
            return {}
        rows = (Batch.query.filter(Batch.inventory_item_id.in_(ids), Batch.quantity_on_hand > 0)
                .order_by(Batch.inventory_item_id, Batch.expiry_date.is_(None),
                          Batch.expiry_date.asc(), Batch.id.asc()).all())
        out = {}
        for batch in rows:
            out.setdefault(batch.inventory_item_id, batch)
        return out

    def _soonest_batch(self):
        return self.soonest_batches([self.id]).get(self.id)

    @property
    def expiry_date(self):
        """Expiry of the batch that will be sold next (read-only)."""
        batch = self._soonest_batch()
        return batch.expiry_date if batch else None

    @property
    def batch_number(self):
        """Number of the batch that will be sold next (read-only)."""
        batch = self._soonest_batch()
        return batch.batch_number if batch else None

    def to_dict(self, soonest=None):
        """``soonest``: a map from :meth:`soonest_batches`, so a list of
        items costs one batch query instead of one per item."""
        batch = soonest.get(self.id) if soonest is not None else self._soonest_batch()
        return {
            'id':          self.id,
            'name':        self.name,
            'description': self.description,
            'price':       float(self.price),
            'cost_price':  float(self.cost_price) if self.cost_price is not None else None,
            'category':    self.category_ref.name if self.category_ref else self.category,
            'category_id': self.category_id,
            'stock':       self.stock,
            'icon':        self.icon,
            'sku':         self.sku,
            'barcode':     self.barcode,
            'reorder_level': self.reorder_level,
            'expiry_date': batch.expiry_date.isoformat() if batch and batch.expiry_date else None,
            'batch_number': batch.batch_number if batch else None,
            'supplier_id': self.supplier_id,
            'unit':        self.unit,
            'has_variants': bool(self.has_variants),
            'is_drug':     self.is_drug,
            'requires_prescription': self.requires_prescription,
        }


class Customer(TenantMixin, db.Model):
    __tablename__ = 'customers'
    __table_args__ = (
        db.UniqueConstraint('tenant_id', 'phone_number', name='uq_customers_tenant_phone_number'),
        db.UniqueConstraint('tenant_id', 'code', name='uq_customers_tenant_code'),
    )
    id           = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name         = db.Column(db.String(255), nullable=False)
    email        = db.Column(db.String(255), nullable=True)
    phone_number = db.Column(db.String(15), nullable=False)
    code         = db.Column(db.String(50), nullable=False)

    # POS extensions
    loyalty_points = db.Column(db.Integer, default=0, nullable=False)
    credit_limit   = db.Column(db.Numeric(12, 2), default=0, nullable=False)
    # What the customer owes is the sum of their open invoices (services/
    # billing), not a stored running balance, so it cannot drift.
    credit_term_days = db.Column(db.Integer, default=30, nullable=False,
                                 server_default='30')
    customer_type  = db.Column(db.String(20), default='retail', nullable=False)
    # 'retail' | 'wholesale' | 'vip'
    created_at     = db.Column(db.DateTime, server_default=db.func.now())

    orders        = db.relationship('Order', backref='customer', lazy=True, cascade='all, delete')
    transactions  = db.relationship('Transaction', backref='customer', lazy=True)
    prescriptions = db.relationship('Prescription', backref='customer', lazy=True)
    loyalty_log   = db.relationship('LoyaltyTransaction', backref='customer', lazy=True)

    @validates('phone_number')
    def _normalize_phone(self, key, value):
        return normalize_ke_phone(value)

    def __repr__(self):
        return f"<Customer(id={self.id}, name={self.name}, phone_number={self.phone_number}, code={self.code})>"

    def to_dict(self):
        return {
            'id':           self.id,
            'name':         self.name,
            'email':        self.email,
            'phone_number': self.phone_number,
            'code':         self.code,
            'loyalty_points': self.loyalty_points,
            'credit_limit':   float(self.credit_limit or 0),
            'credit_term_days': self.credit_term_days,
            'customer_type':  self.customer_type,
        }


ORDER_CHANNELS = ('staff', 'public_link')
FULFILLMENT_STATUSES = ('pending', 'ready', 'dispatched', 'collected', 'released',
                        'restock_needed', 'cancelled')


class Order(TenantMixin, db.Model):
    """A remote order (public link or staff). Created with its invoice by
    services/orders.py; walk-in counter sales are Transactions."""
    __tablename__ = 'orders'
    __table_args__ = (
        db.UniqueConstraint('tenant_id', 'order_number', name='uq_orders_tenant_order_number'),
    )
    id                = db.Column(db.Integer, primary_key=True, autoincrement=True)
    # NULL for campaign orders placed for a member (student, house).
    customer_id       = db.Column(db.Integer, db.ForeignKey('customers.id'), nullable=True)
    # item/amount kept for backward compat and as a human-readable summary
    item              = db.Column(db.String(255), nullable=False)
    amount            = db.Column(db.Numeric(10, 2), nullable=False)
    status            = db.Column(db.String(20), default='pending', nullable=False)
    # 'pending' | 'confirmed' | 'completed' | 'cancelled' | 'refunded'
    payment_method    = db.Column(db.String(20), nullable=True)   # 'cash' | 'mpesa' | 'card'
    payment_reference = db.Column(db.String(100), nullable=True)  # e.g. M-Pesa transaction code
    time              = db.Column(db.DateTime, server_default=db.func.now())
    # Assigned by services/orders.py (ORD000123, per business). NULL only on
    # rows written by the old order screens before they move onto it.
    order_number       = db.Column(db.String(16), nullable=True)
    channel            = db.Column(db.String(16), nullable=False, default='staff',
                                   server_default='staff')
    fulfillment_status = db.Column(db.String(16), nullable=False, default='pending',
                                   server_default='pending')
    delivery_address   = db.Column(db.String(255), nullable=True)
    # Pay-now orders hold their stock until this time; the sweep releases
    # unpaid holds. NULL: not held (paid, credit, or released).
    reserved_until     = db.Column(db.DateTime, nullable=True, index=True)
    prescription_id    = db.Column(db.Integer, db.ForeignKey('prescriptions.id', ondelete='SET NULL'),
                                   nullable=True)
    pickup_code_hash   = db.Column(db.String(64), nullable=True)
    picked_up_at       = db.Column(db.DateTime, nullable=True)
    picked_up_by       = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'),
                                   nullable=True)

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
            'order_number':      self.order_number,
            'channel':           self.channel,
            'fulfillment_status': self.fulfillment_status,
            'delivery_address':  self.delivery_address,
            'reserved_until':    self.reserved_until.isoformat() if self.reserved_until else None,
            'picked_up_at':      self.picked_up_at.isoformat() if self.picked_up_at else None,
            'items':             [oi.to_dict() for oi in self.order_items],
        }


class StockMovement(TenantMixin, db.Model):
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


class OrderItem(TenantMixin, db.Model):
    __tablename__ = 'order_items'
    id                = db.Column(db.Integer, primary_key=True, autoincrement=True)
    order_id          = db.Column(db.Integer, db.ForeignKey('orders.id', ondelete='CASCADE'), nullable=False)
    inventory_item_id = db.Column(db.Integer, db.ForeignKey('inventory_items.id', ondelete='SET NULL'), nullable=True)
    item_name         = db.Column(db.String(255), nullable=False)  # snapshot at time of order
    quantity          = db.Column(db.Integer, nullable=False, default=1)
    unit_price        = db.Column(db.Numeric(10, 2), nullable=False)
    subtotal          = db.Column(db.Numeric(10, 2), nullable=False)
    variant_id        = db.Column(db.Integer, db.ForeignKey('item_variants.id', ondelete='SET NULL'),
                                  nullable=True)

    def to_dict(self):
        return {
            'id':                self.id,
            'inventory_item_id': self.inventory_item_id,
            'variant_id':        self.variant_id,
            'item_name':         self.item_name,
            'quantity':          self.quantity,
            'unit_price':        float(self.unit_price),
            'subtotal':          float(self.subtotal),
        }


# =============================================================================
# POS / Pharmacy / Procurement models
# =============================================================================

# --- Status & enum-like string constants ---
TXN_OPEN       = 'open'
TXN_COMPLETED  = 'completed'
TXN_VOIDED     = 'voided'
TXN_REFUNDED   = 'refunded'
TXN_STATUSES   = (TXN_OPEN, TXN_COMPLETED, TXN_VOIDED, TXN_REFUNDED)

PAY_CASH   = 'cash'
PAY_MPESA  = 'mpesa'
PAY_CARD   = 'card'
PAY_CREDIT = 'credit'
PAY_SPLIT  = 'split'
PAYMENT_METHODS = (PAY_CASH, PAY_MPESA, PAY_CARD, PAY_CREDIT, PAY_SPLIT)

SHIFT_OPEN   = 'open'
SHIFT_CLOSED = 'closed'

PO_DRAFT     = 'draft'
PO_SENT      = 'sent'
PO_PARTIAL   = 'partial'
PO_RECEIVED  = 'received'
PO_CANCELLED = 'cancelled'
PO_STATUSES  = (PO_DRAFT, PO_SENT, PO_PARTIAL, PO_RECEIVED, PO_CANCELLED)


class Shift(TenantMixin, db.Model):
    __tablename__ = 'shifts'
    id             = db.Column(db.Integer, primary_key=True, autoincrement=True)
    cashier_id     = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
    branch_id      = db.Column(db.Integer, nullable=True)  # FK left loose for single-branch deployments
    opened_at      = db.Column(db.DateTime, server_default=db.func.now(), nullable=False)
    closed_at      = db.Column(db.DateTime, nullable=True)
    opening_float  = db.Column(db.Numeric(12, 2), default=0, nullable=False)
    closing_cash   = db.Column(db.Numeric(12, 2), nullable=True)
    expected_cash  = db.Column(db.Numeric(12, 2), nullable=True)
    cash_variance  = db.Column(db.Numeric(12, 2), nullable=True)
    status         = db.Column(db.String(20), default=SHIFT_OPEN, nullable=False)

    transactions    = db.relationship('Transaction', backref='shift', lazy=True)
    cash_movements  = db.relationship('CashMovement', backref='shift', lazy=True,
                                      cascade='all, delete-orphan')

    def to_dict(self):
        return {
            'id':            self.id,
            'cashier_id':    self.cashier_id,
            'branch_id':     self.branch_id,
            'opened_at':     self.opened_at.isoformat() if self.opened_at else None,
            'closed_at':     self.closed_at.isoformat() if self.closed_at else None,
            'opening_float': float(self.opening_float or 0),
            'closing_cash':  float(self.closing_cash) if self.closing_cash is not None else None,
            'expected_cash': float(self.expected_cash) if self.expected_cash is not None else None,
            'cash_variance': float(self.cash_variance) if self.cash_variance is not None else None,
            'status':        self.status,
        }


class CashMovement(TenantMixin, db.Model):
    __tablename__ = 'cash_movements'
    id            = db.Column(db.Integer, primary_key=True, autoincrement=True)
    shift_id      = db.Column(db.Integer, db.ForeignKey('shifts.id', ondelete='CASCADE'), nullable=False)
    user_id       = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
    movement_type = db.Column(db.String(10), nullable=False)  # 'cash_in' | 'cash_out'
    amount        = db.Column(db.Numeric(12, 2), nullable=False)
    reason        = db.Column(db.String(255), nullable=True)
    created_at    = db.Column(db.DateTime, server_default=db.func.now(), nullable=False)

    def to_dict(self):
        return {
            'id':            self.id,
            'shift_id':      self.shift_id,
            'user_id':       self.user_id,
            'movement_type': self.movement_type,
            'amount':        float(self.amount),
            'reason':        self.reason,
            'created_at':    self.created_at.isoformat() if self.created_at else None,
        }


class Transaction(TenantMixin, db.Model):
    """POS sale. Distinct from the legacy `Order` model which is preserved for back-compat."""
    __tablename__ = 'transactions'
    __table_args__ = (
        db.UniqueConstraint('tenant_id', 'transaction_ref', name='uq_transactions_tenant_transaction_ref'),
    )
    id              = db.Column(db.Integer, primary_key=True, autoincrement=True)
    transaction_ref = db.Column(db.String(40), nullable=False)
    customer_id     = db.Column(db.Integer, db.ForeignKey('customers.id', ondelete='SET NULL'), nullable=True)
    cashier_id      = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
    shift_id        = db.Column(db.Integer, db.ForeignKey('shifts.id', ondelete='SET NULL'), nullable=True)
    status          = db.Column(db.String(20), default=TXN_OPEN, nullable=False)

    subtotal        = db.Column(db.Numeric(12, 2), default=0, nullable=False)
    discount_amount = db.Column(db.Numeric(12, 2), default=0, nullable=False)
    tax_amount      = db.Column(db.Numeric(12, 2), default=0, nullable=False)
    total           = db.Column(db.Numeric(12, 2), default=0, nullable=False)

    payment_method  = db.Column(db.String(20), nullable=True)
    mpesa_ref       = db.Column(db.String(64), nullable=True)
    card_ref        = db.Column(db.String(64), nullable=True)
    checkout_request_id = db.Column(db.String(80), nullable=True)  # Daraja STK Push correlator
    notes           = db.Column(db.Text, nullable=True)

    created_at      = db.Column(db.DateTime, server_default=db.func.now(), nullable=False)
    updated_at      = db.Column(db.DateTime, server_default=db.func.now(),
                                onupdate=db.func.now(), nullable=False)

    items                  = db.relationship('TransactionItem', backref='transaction',
                                             lazy=True, cascade='all, delete-orphan')
    loyalty_movements      = db.relationship('LoyaltyTransaction', backref='transaction', lazy=True)
    prescriptions_dispensed = db.relationship('Prescription', backref='transaction', lazy=True)

    def to_dict(self):
        return {
            'id':              self.id,
            'transaction_ref': self.transaction_ref,
            'customer_id':     self.customer_id,
            'cashier_id':      self.cashier_id,
            'shift_id':        self.shift_id,
            'status':          self.status,
            'subtotal':        float(self.subtotal or 0),
            'discount_amount': float(self.discount_amount or 0),
            'tax_amount':      float(self.tax_amount or 0),
            'total':           float(self.total or 0),
            'payment_method':  self.payment_method,
            'mpesa_ref':       self.mpesa_ref,
            'card_ref':        self.card_ref,
            'notes':           self.notes,
            'created_at':      self.created_at.isoformat() if self.created_at else None,
            'updated_at':      self.updated_at.isoformat() if self.updated_at else None,
            'items':           [i.to_dict() for i in self.items],
        }


class TransactionItem(TenantMixin, db.Model):
    __tablename__ = 'transaction_items'
    id                = db.Column(db.Integer, primary_key=True, autoincrement=True)
    transaction_id    = db.Column(db.Integer, db.ForeignKey('transactions.id', ondelete='CASCADE'), nullable=False)
    inventory_item_id = db.Column(db.Integer, db.ForeignKey('inventory_items.id', ondelete='SET NULL'), nullable=True)
    item_name         = db.Column(db.String(255), nullable=False)  # snapshot
    quantity          = db.Column(db.Numeric(12, 3), nullable=False)  # supports fractional units
    unit_price        = db.Column(db.Numeric(12, 2), nullable=False)
    discount          = db.Column(db.Numeric(12, 2), default=0, nullable=False)
    line_total        = db.Column(db.Numeric(12, 2), nullable=False)
    batch_number      = db.Column(db.String(64), nullable=True)
    # Units already returned; refunds cannot return more than were sold.
    refunded_quantity = db.Column(db.Numeric(12, 3), nullable=False, default=0,
                                  server_default='0')

    def to_dict(self):
        return {
            'id':                self.id,
            'transaction_id':    self.transaction_id,
            'inventory_item_id': self.inventory_item_id,
            'item_name':         self.item_name,
            'quantity':          float(self.quantity),
            'unit_price':        float(self.unit_price),
            'discount':          float(self.discount or 0),
            'line_total':        float(self.line_total),
            'refunded_quantity': float(self.refunded_quantity or 0),
            'batch_number':      self.batch_number,
        }


class PurchaseOrder(TenantMixin, db.Model):
    __tablename__ = 'purchase_orders'
    __table_args__ = (
        db.UniqueConstraint('tenant_id', 'po_number', name='uq_purchase_orders_tenant_po_number'),
    )
    id            = db.Column(db.Integer, primary_key=True, autoincrement=True)
    po_number     = db.Column(db.String(40), nullable=False)
    supplier_id   = db.Column(db.Integer, db.ForeignKey('suppliers.id', ondelete='SET NULL'), nullable=True)
    status        = db.Column(db.String(20), default=PO_DRAFT, nullable=False)
    ordered_by    = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
    expected_date = db.Column(db.Date, nullable=True)
    notes         = db.Column(db.Text, nullable=True)
    created_at    = db.Column(db.DateTime, server_default=db.func.now(), nullable=False)
    updated_at    = db.Column(db.DateTime, server_default=db.func.now(),
                              onupdate=db.func.now(), nullable=False)

    items = db.relationship('PurchaseOrderItem', backref='purchase_order',
                            lazy=True, cascade='all, delete-orphan')

    def to_dict(self):
        return {
            'id':            self.id,
            'po_number':     self.po_number,
            'supplier_id':   self.supplier_id,
            'status':        self.status,
            'ordered_by':    self.ordered_by,
            'expected_date': self.expected_date.isoformat() if self.expected_date else None,
            'expected_delivery_date': self.expected_date.isoformat() if self.expected_date else None,
            'supplier_name': self.supplier.name if self.supplier_id and self.supplier else None,
            'total':         float(sum((Decimal(str(i.quantity_ordered)) * Decimal(str(i.unit_cost))
                                        for i in self.items), Decimal('0'))),
            'notes':         self.notes,
            'created_at':    self.created_at.isoformat() if self.created_at else None,
            'updated_at':    self.updated_at.isoformat() if self.updated_at else None,
            'items':         [i.to_dict() for i in self.items],
        }


class PurchaseOrderItem(TenantMixin, db.Model):
    __tablename__ = 'purchase_order_items'
    id                 = db.Column(db.Integer, primary_key=True, autoincrement=True)
    po_id              = db.Column(db.Integer, db.ForeignKey('purchase_orders.id', ondelete='CASCADE'), nullable=False)
    inventory_item_id  = db.Column(db.Integer, db.ForeignKey('inventory_items.id', ondelete='SET NULL'), nullable=True)
    quantity_ordered   = db.Column(db.Numeric(12, 3), nullable=False)
    quantity_received  = db.Column(db.Numeric(12, 3), default=0, nullable=False)
    unit_cost          = db.Column(db.Numeric(12, 2), nullable=False)
    batch_number       = db.Column(db.String(64), nullable=True)
    expiry_date        = db.Column(db.Date, nullable=True)

    def to_dict(self):
        return {
            'id':                self.id,
            'po_id':             self.po_id,
            'inventory_item_id': self.inventory_item_id,
            'quantity_ordered':  float(self.quantity_ordered),
            'quantity_received': float(self.quantity_received or 0),
            'unit_cost':         float(self.unit_cost),
            'batch_number':      self.batch_number,
            'expiry_date':       self.expiry_date.isoformat() if self.expiry_date else None,
        }


class Prescription(TenantMixin, db.Model):
    __tablename__ = 'prescriptions'
    id                = db.Column(db.Integer, primary_key=True, autoincrement=True)
    customer_id       = db.Column(db.Integer, db.ForeignKey('customers.id', ondelete='CASCADE'), nullable=False)
    pharmacist_id     = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
    doctor_name       = db.Column(db.String(255), nullable=True)
    doctor_phone      = db.Column(db.String(20), nullable=True)
    prescription_date = db.Column(db.Date, nullable=True)
    notes             = db.Column(db.Text, nullable=True)
    transaction_id    = db.Column(db.Integer, db.ForeignKey('transactions.id', ondelete='SET NULL'), nullable=True)
    created_at        = db.Column(db.DateTime, server_default=db.func.now(), nullable=False)

    items = db.relationship('PrescriptionItem', backref='prescription',
                            lazy=True, cascade='all, delete-orphan')

    def to_dict(self):
        return {
            'id':                self.id,
            'customer_id':       self.customer_id,
            'pharmacist_id':     self.pharmacist_id,
            'doctor_name':       self.doctor_name,
            'doctor_phone':      self.doctor_phone,
            'prescription_date': self.prescription_date.isoformat() if self.prescription_date else None,
            'notes':             self.notes,
            'transaction_id':    self.transaction_id,
            'created_at':        self.created_at.isoformat() if self.created_at else None,
            'items':             [i.to_dict() for i in self.items],
        }


class PrescriptionItem(TenantMixin, db.Model):
    __tablename__ = 'prescription_items'
    id                  = db.Column(db.Integer, primary_key=True, autoincrement=True)
    prescription_id     = db.Column(db.Integer, db.ForeignKey('prescriptions.id', ondelete='CASCADE'), nullable=False)
    inventory_item_id   = db.Column(db.Integer, db.ForeignKey('inventory_items.id', ondelete='SET NULL'), nullable=True)
    dosage_instructions = db.Column(db.String(255), nullable=True)
    quantity_prescribed = db.Column(db.Numeric(12, 3), nullable=False)
    quantity_dispensed  = db.Column(db.Numeric(12, 3), default=0, nullable=False)
    is_dispensed        = db.Column(db.Boolean, default=False, nullable=False)

    def to_dict(self):
        return {
            'id':                  self.id,
            'prescription_id':     self.prescription_id,
            'inventory_item_id':   self.inventory_item_id,
            'dosage_instructions': self.dosage_instructions,
            'quantity_prescribed': float(self.quantity_prescribed),
            'quantity_dispensed':  float(self.quantity_dispensed or 0),
            'is_dispensed':        self.is_dispensed,
        }


class LoyaltyTransaction(TenantMixin, db.Model):
    __tablename__ = 'loyalty_transactions'
    id              = db.Column(db.Integer, primary_key=True, autoincrement=True)
    customer_id     = db.Column(db.Integer, db.ForeignKey('customers.id', ondelete='CASCADE'), nullable=False)
    transaction_id  = db.Column(db.Integer, db.ForeignKey('transactions.id', ondelete='SET NULL'), nullable=True)
    points_earned   = db.Column(db.Integer, default=0, nullable=False)
    points_redeemed = db.Column(db.Integer, default=0, nullable=False)
    balance_after   = db.Column(db.Integer, default=0, nullable=False)
    note            = db.Column(db.String(255), nullable=True)
    created_at      = db.Column(db.DateTime, server_default=db.func.now(), nullable=False)

    def to_dict(self):
        return {
            'id':              self.id,
            'customer_id':     self.customer_id,
            'transaction_id':  self.transaction_id,
            'points_earned':   self.points_earned,
            'points_redeemed': self.points_redeemed,
            'balance_after':   self.balance_after,
            'note':            self.note,
            'created_at':      self.created_at.isoformat() if self.created_at else None,
        }


class AuditLog(TenantMixin, db.Model):
    """Append-only log of destructive / sensitive actions."""
    __tablename__ = 'audit_logs'
    # NULL only for platform events written inside unscoped().
    tenant_id   = db.Column(db.Integer, db.ForeignKey('tenants.id', ondelete='CASCADE'),
                            nullable=True, index=True)
    id          = db.Column(db.Integer, primary_key=True, autoincrement=True)
    user_id     = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
    action      = db.Column(db.String(64), nullable=False)
    entity_type = db.Column(db.String(64), nullable=True)
    entity_id   = db.Column(db.Integer, nullable=True)
    old_value   = db.Column(db.JSON, nullable=True)
    new_value   = db.Column(db.JSON, nullable=True)
    ip_address  = db.Column(db.String(64), nullable=True)
    created_at  = db.Column(db.DateTime, server_default=db.func.now(), nullable=False)

    def to_dict(self):
        return {
            'id':          self.id,
            'user_id':     self.user_id,
            'action':      self.action,
            'entity_type': self.entity_type,
            'entity_id':   self.entity_id,
            'old_value':   self.old_value,
            'new_value':   self.new_value,
            'ip_address':  self.ip_address,
            'created_at':  self.created_at.isoformat() if self.created_at else None,
        }
