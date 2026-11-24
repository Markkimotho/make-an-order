"""Tests for new POS models (Transaction, Shift, PurchaseOrder, etc.).

Each test gets a fresh SQLite database from the shared ``client`` fixture
in ``tests/conftest.py``.
"""
import pytest
from decimal import Decimal

from models import (
    db,
    Customer, InventoryItem, Supplier, Category, Shift, CashMovement,
    Transaction, TransactionItem, PurchaseOrder, PurchaseOrderItem,
    Prescription, PrescriptionItem, LoyaltyTransaction,
    SHIFT_OPEN, SHIFT_CLOSED, TXN_OPEN, TXN_COMPLETED,
    PAY_CASH, PAY_MPESA, PO_DRAFT, PO_RECEIVED,
)



# --------------------------------------------------------------------------
# Shift
# --------------------------------------------------------------------------

class TestShift:
    def test_open_shift_defaults(self, client):
        s = Shift(opening_float=Decimal('1000'))
        db.session.add(s); db.session.commit()
        assert s.id is not None
        assert s.status == SHIFT_OPEN
        assert s.closed_at is None
        assert float(s.opening_float) == 1000.0

    def test_close_shift(self, client):
        s = Shift(opening_float=Decimal('500'))
        db.session.add(s); db.session.commit()
        from datetime import datetime
        s.status = SHIFT_CLOSED
        s.closed_at = datetime.utcnow()
        s.closing_cash = Decimal('1500')
        s.expected_cash = Decimal('1450')
        s.cash_variance = Decimal('50')
        db.session.commit()
        d = s.to_dict()
        assert d['status'] == SHIFT_CLOSED
        assert d['closing_cash'] == 1500.0
        assert d['cash_variance'] == 50.0

    def test_cash_movement_cascades(self, client):
        s = Shift(opening_float=Decimal('0'))
        db.session.add(s); db.session.commit()
        cm = CashMovement(shift_id=s.id, movement_type='cash_in',
                          amount=Decimal('200'), reason='change')
        db.session.add(cm); db.session.commit()
        assert len(s.cash_movements) == 1
        # Cascade delete
        db.session.delete(s); db.session.commit()
        assert CashMovement.query.count() == 0


# --------------------------------------------------------------------------
# Transaction
# --------------------------------------------------------------------------

class TestTransaction:
    def test_create_with_items(self, client):
        item = InventoryItem(name='Pen', stock=100, price=Decimal('10.00'))
        db.session.add(item); db.session.commit()

        txn = Transaction(transaction_ref='TXN-20260502-0001',
                          subtotal=Decimal('20'), tax_amount=Decimal('3.20'),
                          total=Decimal('23.20'))
        db.session.add(txn); db.session.flush()
        ti = TransactionItem(transaction_id=txn.id, inventory_item_id=item.id,
                             item_name='Pen', quantity=Decimal('2'),
                             unit_price=Decimal('10.00'), line_total=Decimal('20'))
        db.session.add(ti); db.session.commit()

        assert txn.status == TXN_OPEN
        assert len(txn.items) == 1
        assert float(txn.total) == 23.20
        d = txn.to_dict()
        assert d['transaction_ref'] == 'TXN-20260502-0001'
        assert len(d['items']) == 1

    def test_complete_transaction(self, client):
        txn = Transaction(transaction_ref='TXN-X', total=Decimal('100'),
                          status=TXN_OPEN)
        db.session.add(txn); db.session.commit()
        txn.status = TXN_COMPLETED
        txn.payment_method = PAY_CASH
        db.session.commit()
        assert txn.status == TXN_COMPLETED
        assert txn.payment_method == PAY_CASH

    def test_unique_transaction_ref(self, client):
        a = Transaction(transaction_ref='TXN-DUP', total=Decimal('1'))
        b = Transaction(transaction_ref='TXN-DUP', total=Decimal('2'))
        db.session.add_all([a, b])
        with pytest.raises(Exception):
            db.session.commit()
        db.session.rollback()

    def test_checkout_request_id_field(self, client):
        txn = Transaction(transaction_ref='TXN-MPESA',
                          payment_method=PAY_MPESA,
                          checkout_request_id='ws_CO_ABC123',
                          total=Decimal('500'))
        db.session.add(txn); db.session.commit()
        found = Transaction.query.filter_by(checkout_request_id='ws_CO_ABC123').first()
        assert found is not None
        assert found.id == txn.id

    def test_items_cascade_delete(self, client):
        txn = Transaction(transaction_ref='TXN-CAS', total=Decimal('5'))
        db.session.add(txn); db.session.flush()
        db.session.add(TransactionItem(
            transaction_id=txn.id, item_name='X',
            quantity=Decimal('1'), unit_price=Decimal('5'),
            line_total=Decimal('5')))
        db.session.commit()
        db.session.delete(txn); db.session.commit()
        assert TransactionItem.query.count() == 0


# --------------------------------------------------------------------------
# Supplier / PurchaseOrder
# --------------------------------------------------------------------------

class TestPurchaseOrder:
    def test_supplier_crud(self, client):
        s = Supplier(name='Acme Distributors', phone='+254700000000')
        db.session.add(s); db.session.commit()
        assert s.id is not None
        assert s.to_dict()['name'] == 'Acme Distributors'

    def test_create_po_with_items(self, client):
        sup = Supplier(name='Vendor')
        item = InventoryItem(name='Box', stock=0, price=Decimal('5'))
        db.session.add_all([sup, item]); db.session.commit()

        po = PurchaseOrder(po_number='PO-20260502-0001', supplier_id=sup.id,
                           status=PO_DRAFT)
        db.session.add(po); db.session.flush()
        db.session.add(PurchaseOrderItem(
            po_id=po.id, inventory_item_id=item.id,
            quantity_ordered=Decimal('20'), unit_cost=Decimal('5')))
        db.session.commit()

        assert po.status == PO_DRAFT
        assert len(po.items) == 1
        assert float(po.items[0].quantity_ordered) == 20.0

    def test_po_status_transition(self, client):
        sup = Supplier(name='V')
        db.session.add(sup); db.session.commit()
        po = PurchaseOrder(po_number='PO-X', supplier_id=sup.id, status=PO_DRAFT)
        db.session.add(po); db.session.commit()
        po.status = PO_RECEIVED
        db.session.commit()
        assert po.status == PO_RECEIVED


# --------------------------------------------------------------------------
# Prescription
# --------------------------------------------------------------------------

class TestPrescription:
    def test_create_prescription(self, client):
        cust = Customer(name='Pat', phone_number='+254712345678', code='C1')
        drug = InventoryItem(name='Amoxicillin', stock=50, price=Decimal('20'),
                             is_drug=True, requires_prescription=True)
        db.session.add_all([cust, drug]); db.session.commit()

        rx = Prescription(customer_id=cust.id, doctor_name='Dr Smith')
        db.session.add(rx); db.session.flush()
        db.session.add(PrescriptionItem(
            prescription_id=rx.id, inventory_item_id=drug.id,
            quantity_prescribed=Decimal('10'), quantity_dispensed=Decimal('0'),
            dosage_instructions='1 cap TID'))
        db.session.commit()

        assert len(rx.items) == 1
        assert float(rx.items[0].quantity_prescribed) == 10.0
        assert rx.items[0].dosage_instructions == '1 cap TID'


# --------------------------------------------------------------------------
# LoyaltyTransaction
# --------------------------------------------------------------------------

class TestLoyaltyTransaction:
    def test_award_log(self, client):
        cust = Customer(name='Loyal', phone_number='+254712345678',
                        code='LOY', loyalty_points=0)
        db.session.add(cust); db.session.commit()
        lt = LoyaltyTransaction(customer_id=cust.id, points_earned=50,
                                points_redeemed=0, balance_after=50,
                                note='earned')
        db.session.add(lt); db.session.commit()
        assert lt.id is not None
        assert lt.points_earned == 50

    def test_redeem_log(self, client):
        cust = Customer(name='X', phone_number='+254712345678', code='Y',
                        loyalty_points=100)
        db.session.add(cust); db.session.commit()
        lt = LoyaltyTransaction(customer_id=cust.id, points_earned=0,
                                points_redeemed=30, balance_after=70,
                                note='redeemed')
        db.session.add(lt); db.session.commit()
        assert lt.points_redeemed == 30


# --------------------------------------------------------------------------
# Customer POS extensions
# --------------------------------------------------------------------------

class TestCustomerPosFields:
    def test_defaults(self, client):
        c = Customer(name='New', phone_number='+254712345678', code='Z')
        db.session.add(c); db.session.commit()
        assert c.loyalty_points == 0
        assert c.credit_term_days == 30
        assert c.customer_type == 'retail'

    def test_credit_limit_and_term(self, client):
        c = Customer(name='Credit', phone_number='+254712345678', code='CR',
                     credit_limit=Decimal('10000'), credit_term_days=14)
        db.session.add(c); db.session.commit()
        assert float(c.credit_limit) == 10000.0
        assert c.credit_term_days == 14


# --------------------------------------------------------------------------
# InventoryItem POS extensions
# --------------------------------------------------------------------------

class TestInventoryItemPosFields:
    def test_drug_flags(self, client):
        cat = Category(name='Antibiotics')
        sup = Supplier(name='S')
        db.session.add_all([cat, sup]); db.session.commit()
        item = InventoryItem(
            name='Test Drug', stock=100, price=Decimal('50'),
            cost_price=Decimal('30'), reorder_level=10,
            barcode='1234567', category_id=cat.id, supplier_id=sup.id,
            is_drug=True, requires_prescription=True, unit='caps',
        )
        db.session.add(item); db.session.commit()
        assert item.is_drug is True
        assert item.requires_prescription is True
        assert float(item.cost_price) == 30.0
        assert item.reorder_level == 10


# --------------------------------------------------------------------------
# Supplier API (found by the isolation sweep: create always raised TypeError)
# --------------------------------------------------------------------------

def test_supplier_create_and_update_api(login_as):
    client = login_as('admin')
    r = client.post('/inventory/suppliers', json={
        'name': 'Phillips Pharma', 'contact_person': 'Wambui', 'phone': '0722000000',
        'payment_terms': 'Net 30'})
    assert r.status_code == 201, r.get_json()
    sup = r.get_json()['supplier']
    assert (sup['contact_name'], sup['payment_terms']) == ('Wambui', 'Net 30')
    r = client.put(f"/inventory/suppliers/{sup['id']}", json={'contact_name': 'Otieno'})
    assert r.status_code == 200
    assert r.get_json()['supplier']['contact_name'] == 'Otieno'
    assert client.put(f"/inventory/suppliers/{sup['id']}", json={'name': ''}).status_code == 400


def test_item_references_must_belong_to_the_business(login_as, other_tenant):
    """Found by the isolation sweep: another business's category id made
    the item update fail with a 500 instead of a clear 400."""
    from auth.tenancy import tenant_scope
    with tenant_scope(other_tenant):
        foreign = Category(name='Theirs')
        db.session.add(foreign)
        db.session.commit()
        foreign_id = foreign.id
    client = login_as('admin')
    r = client.post('/inventory/items', json={'name': 'Gauze', 'price': 10,
                                              'category': 'Dressings',
                                              'category_id': foreign_id})
    assert r.status_code == 400 and r.get_json() == {'error': 'Unknown category'}
    item = InventoryItem(name='Gauze', price=Decimal('10'))
    db.session.add(item); db.session.commit()
    r = client.put(f'/inventory/items/{item.id}', json={'category_id': foreign_id})
    assert r.status_code == 400
    assert client.put(f'/inventory/items/{item.id}', json={'supplier_id': 'x'}).status_code == 400
