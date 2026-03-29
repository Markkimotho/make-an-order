from models import db, InventoryItem, Customer

INVENTORY_SEED = [
    # ── Electronics ──────────────────────────────────────────────────────────
    dict(name='Laptop Pro 15"',      description='16 GB RAM · 512 GB SSD · Intel Core i7',      price=1299.99, category='Electronics',    stock=15,  icon='fa-laptop'),
    dict(name='Smartphone X12',      description='6.7" OLED · 256 GB · 5G · triple camera',     price= 899.00, category='Electronics',    stock=30,  icon='fa-mobile-alt'),
    dict(name='Wireless Headphones', description='Active noise-cancelling · 30 hr battery',      price= 249.99, category='Electronics',    stock=50,  icon='fa-headphones'),
    dict(name='Tablet Air 10"',      description='10" IPS · 64 GB · Wi-Fi · stylus support',    price= 599.99, category='Electronics',    stock=20,  icon='fa-tablet-alt'),
    dict(name='Mechanical Keyboard', description='Tactile switches · TKL · RGB backlit',         price= 129.99, category='Electronics',    stock=40,  icon='fa-keyboard'),
    dict(name='USB-C Hub 7-in-1',    description='HDMI · 3×USB-A · SD · 65 W PD · Ethernet',   price=  49.99, category='Electronics',    stock=75,  icon='fa-plug'),
    # ── Office ───────────────────────────────────────────────────────────────
    dict(name='Ergonomic Chair',     description='Lumbar support · mesh back · adjustable arms', price= 449.00, category='Office',         stock=10,  icon='fa-chair'),
    dict(name='Height-Adjust Desk',  description='Electric motor · 120×60 cm top · memory',     price= 799.00, category='Office',         stock= 8,  icon='fa-table'),
    dict(name='27" 4K Monitor',      description='IPS · USB-C 65 W PD · 99 % sRGB',            price= 399.99, category='Office',         stock=22,  icon='fa-desktop'),
    dict(name='Paper Ream A4',       description='80 gsm · 500 sheets · bright white',           price=   8.99, category='Office',         stock=200, icon='fa-file-alt'),
    dict(name='Ballpoint Pens 12pk', description='Smooth medium tip · blue ink',                 price=   4.99, category='Office',         stock=300, icon='fa-pen'),
    # ── Food & Beverage ──────────────────────────────────────────────────────
    dict(name='Premium Coffee 500g', description='Single-origin Arabica · medium roast',         price=  22.99, category='Food & Beverage', stock=80,  icon='fa-mug-hot'),
    dict(name='Energy Drinks 24pk',  description='Sugar-free · mixed tropical flavours',         price=  36.00, category='Food & Beverage', stock=60,  icon='fa-tint'),
    dict(name='Mixed Snack Box',     description='20 assorted healthy snacks',                   price=  29.99, category='Food & Beverage', stock=45,  icon='fa-cookie'),
    dict(name='Still Water 12×500ml',description='Natural spring water',                         price=   9.99, category='Food & Beverage', stock=150, icon='fa-glass'),
    # ── Accessories ──────────────────────────────────────────────────────────
    dict(name='Wireless Mouse',      description='Ergonomic · 2.4 GHz · 1600 DPI',              price=  34.99, category='Accessories',    stock=90,  icon='fa-mouse'),
    dict(name='Adjustable Phone Stand', description='Aluminium · foldable · universal fit',     price=  19.99, category='Accessories',    stock=110, icon='fa-mobile'),
    dict(name='Power Bank 20000mAh', description='Fast charge · 2×USB-A + USB-C',               price=  59.99, category='Accessories',    stock=55,  icon='fa-battery-full'),
    dict(name='Webcam HD 1080p',     description='Built-in mic · 90° FOV · plug and play',      price=  79.99, category='Accessories',    stock=35,  icon='fa-video'),
    dict(name='Screen Cleaning Kit', description='Microfibre cloth + 100 ml spray solution',    price=  12.99, category='Accessories',    stock=200, icon='fa-spray-can'),
]


CUSTOMER_SEED = [
    dict(name='Alice Muthoni',    phone_number='+254712345678', code='CUST001'),
    dict(name='Brian Ochieng',    phone_number='+254723456789', code='CUST002'),
    dict(name='Catherine Wanjiku', phone_number='+254734567890', code='CUST003'),
    dict(name='Daniel Kiprop',    phone_number='+254745678901', code='CUST004'),
    dict(name='Esther Akinyi',    phone_number='+254756789012', code='CUST005'),
    dict(name='Francis Mwangi',   phone_number='+254767890123', code='CUST006'),
    dict(name='Grace Chebet',     phone_number='+254778901234', code='CUST007'),
    dict(name='Henry Otieno',     phone_number='+254789012345', code='CUST008'),
    dict(name='Irene Njeri',      phone_number='+254790123456', code='CUST009'),
    dict(name='James Kamau',      phone_number='+254701234567', code='CUST010'),
]


def seed_inventory():
    """Insert mock inventory rows if the table is empty."""
    if InventoryItem.query.count() == 0:
        for data in INVENTORY_SEED:
            db.session.add(InventoryItem(**data))
        db.session.commit()


def seed_customers():
    """Insert mock customer rows if the table is empty."""
    if Customer.query.count() == 0:
        for data in CUSTOMER_SEED:
            db.session.add(Customer(**data))
        db.session.commit()
