"""Models package. ``from models import X`` works for every model and constant.

models.base   db, naming convention, Tenant, TenantMixin
models.core   the tenant-owned business models
models.billing invoices, payments, allocations, STK intents
models.types  custom column types (EncryptedString)

Importing this package installs the tenant-scoping session hooks
(auth/tenancy.py), so no code path can use the models without them.
"""
from models.base import (  # noqa: F401
    DARAJA_ENVS, NAMING_CONVENTION, SHORTCODE_TYPES, SUBSCRIPTION_STATUSES,
    TRIAL_DAYS, VERTICALS, Tenant, TenantMixin, db,
)
from models.core import (  # noqa: F401
    ALL_ROLES, PAY_CARD, PAY_CASH, PAY_CREDIT, PAY_MPESA, PAY_SPLIT,
    PAYMENT_METHODS, PHARMACY_ROLES, PO_CANCELLED, PO_DRAFT, PO_PARTIAL,
    PO_RECEIVED, PO_SENT, PO_STATUSES, POS_ROLES, ROLE_ADMIN, ROLE_CASHIER,
    ROLE_PHARMACIST, ROLE_STOCK_CLERK, ROLE_SUPERUSER, ROLE_USER, SHIFT_CLOSED,
    SHIFT_OPEN, STOCK_ROLES, TXN_COMPLETED, TXN_OPEN, TXN_REFUNDED,
    TXN_STATUSES, TXN_VOIDED, AuditLog, CashMovement, Category, Customer,
    InventoryItem, LoyaltyTransaction, Order, OrderItem, Prescription,
    PrescriptionItem, PurchaseOrder, PurchaseOrderItem, Shift, StockMovement,
    Supplier, Transaction, TransactionItem, User,
)
from models.billing import (  # noqa: F401
    ADJUSTMENT_REASONS, INTENT_STATUSES, INVOICE_KINDS, INVOICE_STATUSES,
    MATCH_STATUSES, MPESA_METHODS, PAYMENT_STATUSES, Invoice, InvoiceAdjustment,
    Payment, PaymentAllocation, PaymentIntent,
)
from models.billing import PAYMENT_METHODS as BILLING_PAYMENT_METHODS  # noqa: F401
from models.stock import Batch, ItemVariant, LineBatch  # noqa: F401
from models.types import EncryptedString, SecretsKeyMissing  # noqa: F401

import auth.tenancy  # noqa: E402,F401  installs the session hooks
