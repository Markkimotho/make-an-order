"""seed_service.py — business-type-aware inventory seed data.

Call ``seed_inventory(business_type)`` at app startup.  Rows are only
inserted when the inventory table is empty, so the function is safe to
call on every boot.
"""
from models import db, InventoryItem
from services import stock

# ---------------------------------------------------------------------------
# Seed catalogues — one list per business type
# ---------------------------------------------------------------------------

_PHARMACY = [
    # OTC analgesics / anti-pyretics
    dict(name='Paracetamol 500mg Tabs 24s', description='Analgesic / antipyretic',
         price=55.00,  cost_price=30.00,  category='Analgesics',
         stock=200, reorder_level=50, unit='pack',
         icon='fa-pills', is_drug=True, requires_prescription=False),
    dict(name='Ibuprofen 400mg Tabs 24s',   description='NSAID anti-inflammatory',
         price=75.00,  cost_price=40.00,  category='Analgesics',
         stock=150, reorder_level=40, unit='pack',
         icon='fa-pills', is_drug=True, requires_prescription=False),
    dict(name='Aspirin 300mg Tabs 30s',     description='Analgesic / antiplatelet',
         price=45.00,  cost_price=22.00,  category='Analgesics',
         stock=120, reorder_level=30, unit='pack',
         icon='fa-pills', is_drug=True, requires_prescription=False),
    # Antibiotics (prescription)
    dict(name='Amoxicillin 250mg Caps 21s', description='Broad-spectrum antibiotic',
         price=180.00, cost_price=95.00,  category='Antibiotics',
         stock=80,  reorder_level=20, unit='pack',
         icon='fa-capsules', is_drug=True, requires_prescription=True),
    dict(name='Azithromycin 250mg Tabs 6s', description='Macrolide antibiotic',
         price=290.00, cost_price=140.00, category='Antibiotics',
         stock=60,  reorder_level=15, unit='pack',
         icon='fa-capsules', is_drug=True, requires_prescription=True),
    dict(name='Metronidazole 400mg Tabs 21s', description='Anti-protozoal / antibiotic',
         price=120.00, cost_price=55.00,  category='Antibiotics',
         stock=100, reorder_level=25, unit='pack',
         icon='fa-pills', is_drug=True, requires_prescription=True),
    # Anti-hypertensives (prescription)
    dict(name='Amlodipine 5mg Tabs 30s',    description='Calcium channel blocker',
         price=210.00, cost_price=110.00, category='Cardiovascular',
         stock=70,  reorder_level=15, unit='pack',
         icon='fa-heart-pulse', is_drug=True, requires_prescription=True),
    dict(name='Lisinopril 10mg Tabs 30s',   description='ACE inhibitor',
         price=195.00, cost_price=100.00, category='Cardiovascular',
         stock=65,  reorder_level=15, unit='pack',
         icon='fa-heart-pulse', is_drug=True, requires_prescription=True),
    # Vitamins / supplements (OTC)
    dict(name='Vitamin C 1000mg Tabs 30s',  description='Immune support supplement',
         price=140.00, cost_price=65.00,  category='Vitamins',
         stock=180, reorder_level=40, unit='pack',
         icon='fa-tablet-alt', is_drug=False, requires_prescription=False),
    dict(name='Zinc 50mg Tabs 30s',         description='Micronutrient supplement',
         price=95.00,  cost_price=45.00,  category='Vitamins',
         stock=120, reorder_level=30, unit='pack',
         icon='fa-tablet-alt', is_drug=False, requires_prescription=False),
    dict(name='Multivitamin Tabs 60s',      description='Daily essential vitamins',
         price=175.00, cost_price=85.00,  category='Vitamins',
         stock=100, reorder_level=25, unit='pack',
         icon='fa-tablet-alt', is_drug=False, requires_prescription=False),
    # Cough / cold
    dict(name='Cough Syrup 100ml',          description='Dextromethorphan based',
         price=130.00, cost_price=60.00,  category='Cough & Cold',
         stock=90,  reorder_level=20, unit='bottle',
         icon='fa-prescription-bottle', is_drug=True, requires_prescription=False),
    dict(name='Antihistamine 10mg Tabs 20s', description='Loratadine — allergy relief',
         price=100.00, cost_price=50.00,  category='Cough & Cold',
         stock=110, reorder_level=25, unit='pack',
         icon='fa-pills', is_drug=True, requires_prescription=False),
    # Topical
    dict(name='Antiseptic Cream 25g',       description='Chlorhexidine topical cream',
         price=85.00,  cost_price=40.00,  category='First Aid',
         stock=75,  reorder_level=20, unit='tube',
         icon='fa-first-aid', is_drug=True, requires_prescription=False),
    dict(name='Oral Rehydration Sachets 6s', description='ORS electrolyte powder',
         price=60.00,  cost_price=25.00,  category='First Aid',
         stock=200, reorder_level=50, unit='pack',
         icon='fa-glass-water', is_drug=False, requires_prescription=False),
    # Controlled drug (prescription + special tracking)
    dict(name='Diazepam 5mg Tabs 30s',      description='Benzodiazepine — controlled',
         price=350.00, cost_price=180.00, category='Controlled',
         stock=20,  reorder_level=5,  unit='pack',
         icon='fa-lock', is_drug=True, requires_prescription=True),
]

_SUPERMARKET = [
    # Cereals / grains
    dict(name='Unga Pembe Flour 2kg',        description='Fortified wheat flour',
         price=145.00, cost_price=110.00, category='Grains & Flour',
         stock=120, reorder_level=30, unit='pack', icon='fa-wheat-awn'),
    dict(name='White Rice 5kg',              description='Long-grain polished rice',
         price=380.00, cost_price=295.00, category='Grains & Flour',
         stock=80,  reorder_level=20, unit='bag',  icon='fa-seedling'),
    dict(name='Rolled Oats 500g',            description='Quick-cook oats',
         price=130.00, cost_price=95.00,  category='Cereals',
         stock=60,  reorder_level=15, unit='pack', icon='fa-bowl-food'),
    # Cooking oils / condiments
    dict(name='Cooking Oil 2L',              description='Refined sunflower oil',
         price=320.00, cost_price=245.00, category='Oils & Condiments',
         stock=90,  reorder_level=20, unit='bottle', icon='fa-bottle-droplet'),
    dict(name='Tomato Paste 400g',           description='Concentrated tomato paste',
         price=95.00,  cost_price=65.00,  category='Oils & Condiments',
         stock=110, reorder_level=25, unit='can',    icon='fa-jar'),
    # Dairy
    dict(name='Fresh Milk 1L',               description='Pasteurised full-cream milk',
         price=75.00,  cost_price=58.00,  category='Dairy',
         stock=150, reorder_level=50, unit='litre',  icon='fa-cow'),
    dict(name='Yoghurt 500ml',               description='Plain probiotic yoghurt',
         price=110.00, cost_price=80.00,  category='Dairy',
         stock=60,  reorder_level=15, unit='bottle', icon='fa-jar'),
    dict(name='Cheddar Cheese 200g',         description='Mild sliced cheese',
         price=185.00, cost_price=140.00, category='Dairy',
         stock=40,  reorder_level=10, unit='pack',   icon='fa-cheese'),
    # Beverages
    dict(name='Black Tea 100 Bags',          description='Premium Kenyan tea bags',
         price=120.00, cost_price=85.00,  category='Beverages',
         stock=200, reorder_level=50, unit='pack', icon='fa-mug-hot'),
    dict(name='Instant Coffee 200g',         description='Freeze-dried coffee',
         price=215.00, cost_price=160.00, category='Beverages',
         stock=80,  reorder_level=20, unit='jar',  icon='fa-mug-hot'),
    dict(name='Mineral Water 1.5L',          description='Natural spring water',
         price=60.00,  cost_price=38.00,  category='Beverages',
         stock=300, reorder_level=80, unit='bottle', icon='fa-bottle-water'),
    # Household cleaning
    dict(name='Dishwashing Liquid 500ml',    description='Lemon-scented degreaser',
         price=85.00,  cost_price=55.00,  category='Household',
         stock=120, reorder_level=30, unit='bottle', icon='fa-soap'),
    dict(name='Laundry Detergent 2kg',       description='Biological washing powder',
         price=270.00, cost_price=200.00, category='Household',
         stock=70,  reorder_level=15, unit='pack', icon='fa-shirt'),
    # Snacks
    dict(name='Biscuits 200g',               description='Plain sweet cream biscuits',
         price=55.00,  cost_price=35.00,  category='Snacks & Confectionery',
         stock=200, reorder_level=50, unit='pack', icon='fa-cookie'),
    dict(name='Crisps 100g',                 description='Salted potato crisps',
         price=45.00,  cost_price=28.00,  category='Snacks & Confectionery',
         stock=250, reorder_level=60, unit='pack', icon='fa-cookie-bite'),
]

_HARDWARE = [
    # Power tools
    dict(name='Cordless Drill 18V',          description='2-speed gearbox, 2 batteries included',
         price=6500.00, cost_price=4500.00, category='Power Tools',
         stock=15, reorder_level=5, unit='pcs', icon='fa-drill'),
    dict(name='Angle Grinder 115mm',         description='850 W, disc included',
         price=3200.00, cost_price=2100.00, category='Power Tools',
         stock=20, reorder_level=5, unit='pcs', icon='fa-circle-notch'),
    dict(name='Jigsaw 500W',                 description='Orbital action, variable speed',
         price=4800.00, cost_price=3200.00, category='Power Tools',
         stock=10, reorder_level=3, unit='pcs', icon='fa-scissors'),
    # Hand tools
    dict(name='Claw Hammer 500g',            description='Fibreglass handle, nail puller',
         price=450.00, cost_price=280.00, category='Hand Tools',
         stock=50, reorder_level=10, unit='pcs', icon='fa-hammer'),
    dict(name='Screwdriver Set 12pc',        description='Flat & Phillips, ergonomic grip',
         price=750.00, cost_price=480.00, category='Hand Tools',
         stock=40, reorder_level=10, unit='set', icon='fa-screwdriver'),
    dict(name='Adjustable Wrench 10"',       description='Chrome-vanadium, smooth jaw',
         price=380.00, cost_price=230.00, category='Hand Tools',
         stock=35, reorder_level=8, unit='pcs', icon='fa-wrench'),
    dict(name='Tape Measure 5m',             description='Auto-lock, magnetic tip',
         price=250.00, cost_price=150.00, category='Measuring',
         stock=60, reorder_level=15, unit='pcs', icon='fa-ruler'),
    # Building materials
    dict(name='PVC Pipe 110mm × 3m',         description='Pressure-rated drainage pipe',
         price=480.00, cost_price=320.00, category='Plumbing',
         stock=80, reorder_level=20, unit='length', icon='fa-pipe'),
    dict(name='Cement 50kg (Bamburi)',        description='OPC Portland cement',
         price=1050.00, cost_price=820.00, category='Building Materials',
         stock=200, reorder_level=50, unit='bag', icon='fa-square'),
    dict(name='Steel Rod 12mm × 12m Y16',    description='High-yield deformed bar',
         price=1450.00, cost_price=1100.00, category='Building Materials',
         stock=100, reorder_level=25, unit='rod', icon='fa-minus'),
    # Electrical
    dict(name='2.5mm² Cable 100m Roll',      description='Twin & earth copper cable',
         price=8500.00, cost_price=6200.00, category='Electrical',
         stock=20, reorder_level=5, unit='roll', icon='fa-plug'),
    dict(name='MCB 20A Single Pole',         description='DIN rail miniature circuit breaker',
         price=420.00, cost_price=260.00, category='Electrical',
         stock=80, reorder_level=20, unit='pcs', icon='fa-bolt'),
    # Safety
    dict(name='Safety Helmet (White)',        description='ANSI Z89.1 type I approved',
         price=650.00, cost_price=400.00, category='Safety',
         stock=30, reorder_level=10, unit='pcs', icon='fa-hard-hat'),
    dict(name='Nitrile Gloves L (100pk)',     description='Powder-free disposable gloves',
         price=850.00, cost_price=580.00, category='Safety',
         stock=50, reorder_level=15, unit='box', icon='fa-hand-paper'),
]

_SALON = [
    # Hair care
    dict(name='Shampoo 500ml (Professional)', description='Sulphate-free moisturising shampoo',
         price=650.00, cost_price=400.00, category='Hair Care',
         stock=40, reorder_level=10, unit='bottle', icon='fa-pump-soap'),
    dict(name='Conditioner 500ml',            description='Deep moisture conditioner',
         price=600.00, cost_price=370.00, category='Hair Care',
         stock=35, reorder_level=8, unit='bottle', icon='fa-pump-soap'),
    dict(name='Hair Relaxer Kit',             description='No-lye cream relaxer, medium strength',
         price=1200.00, cost_price=750.00, category='Hair Care',
         stock=25, reorder_level=6, unit='kit', icon='fa-spa'),
    dict(name='Edge Control Gel 100g',        description='Long-lasting edge control',
         price=350.00, cost_price=200.00, category='Hair Styling',
         stock=50, reorder_level=12, unit='jar', icon='fa-jar'),
    dict(name='Hair Colour (Various)',         description='Permanent ammonia-based hair dye',
         price=550.00, cost_price=320.00, category='Hair Colour',
         stock=60, reorder_level=15, unit='tube', icon='fa-palette'),
    dict(name='Brazilian Keratin Treatment',   description='Formaldehyde-free smoothing treatment',
         price=3500.00, cost_price=2200.00, category='Treatments',
         stock=10, reorder_level=2, unit='kit', icon='fa-star'),
    # Nail
    dict(name='Nail Polish (Assorted)',        description='Quick-dry gel-like finish',
         price=280.00, cost_price=160.00, category='Nail Care',
         stock=100, reorder_level=20, unit='bottle', icon='fa-hand-sparkles'),
    dict(name='Acrylic Nail Kit',              description='Full acrylic extension system',
         price=1800.00, cost_price=1100.00, category='Nail Care',
         stock=15, reorder_level=4, unit='kit', icon='fa-star-of-life'),
    # Skin care
    dict(name='Face Moisturiser SPF30',        description='Daily hydrating cream with sun protection',
         price=850.00, cost_price=520.00, category='Skin Care',
         stock=30, reorder_level=8, unit='tube', icon='fa-face-smile'),
    dict(name='Exfoliating Scrub 200ml',       description='Gentle micro-bead face and body scrub',
         price=480.00, cost_price=280.00, category='Skin Care',
         stock=25, reorder_level=6, unit='bottle', icon='fa-droplet'),
    # Services (treated as sellable items on the POS)
    dict(name='Basic Haircut (Service)',       description='Shampoo, cut and style',
         price=500.00, cost_price=0.00,   category='Services',
         stock=999, reorder_level=0, unit='service', icon='fa-scissors'),
    dict(name='Hair Braiding (Service)',       description='Simple box braids',
         price=1500.00, cost_price=0.00,  category='Services',
         stock=999, reorder_level=0, unit='service', icon='fa-scissors'),
    dict(name='Manicure (Service)',            description='File, buff, polish',
         price=700.00, cost_price=0.00,   category='Services',
         stock=999, reorder_level=0, unit='service', icon='fa-hand-sparkles'),
    dict(name='Pedicure (Service)',            description='Soak, scrub, polish',
         price=900.00, cost_price=0.00,   category='Services',
         stock=999, reorder_level=0, unit='service', icon='fa-spa'),
]

_RESTAURANT = [
    # Breakfast
    dict(name='Full English Breakfast',        description='Eggs, bacon, sausage, toast, beans',
         price=550.00, cost_price=250.00, category='Breakfast',
         stock=999, reorder_level=0, unit='plate', icon='fa-egg'),
    dict(name='Pancakes & Syrup',              description='Stack of 3 fluffy pancakes',
         price=350.00, cost_price=140.00, category='Breakfast',
         stock=999, reorder_level=0, unit='plate', icon='fa-circle'),
    # Mains
    dict(name='Grilled Chicken & Chips',       description='Half grilled chicken, fries, coleslaw',
         price=750.00, cost_price=350.00, category='Mains',
         stock=999, reorder_level=0, unit='plate', icon='fa-drumstick-bite'),
    dict(name='Beef Burger & Fries',           description='180g beef patty, cheese, lettuce, tomato',
         price=680.00, cost_price=310.00, category='Mains',
         stock=999, reorder_level=0, unit='plate', icon='fa-burger'),
    dict(name='Pilau Rice (Goat)',             description='Spiced Kenyan pilau with goat',
         price=480.00, cost_price=220.00, category='Mains',
         stock=999, reorder_level=0, unit='plate', icon='fa-bowl-food'),
    dict(name='Beef Stew & Ugali',             description='Slow-cooked beef stew with ugali',
         price=420.00, cost_price=190.00, category='Mains',
         stock=999, reorder_level=0, unit='plate', icon='fa-bowl-food'),
    # Light meals
    dict(name='Club Sandwich',                 description='Triple-decker with fries',
         price=500.00, cost_price=220.00, category='Light Meals',
         stock=999, reorder_level=0, unit='plate', icon='fa-bread-slice'),
    dict(name='Caesar Salad',                  description='Romaine, croutons, parmesan, dressing',
         price=380.00, cost_price=160.00, category='Salads',
         stock=999, reorder_level=0, unit='plate', icon='fa-leaf'),
    # Beverages
    dict(name='Fresh Juice (Passion)',          description='Freshly squeezed 300ml',
         price=180.00, cost_price=70.00,  category='Beverages',
         stock=999, reorder_level=0, unit='glass', icon='fa-glass-water'),
    dict(name='Masala Chai',                   description='Spiced tea with milk',
         price=100.00, cost_price=30.00,  category='Beverages',
         stock=999, reorder_level=0, unit='cup', icon='fa-mug-hot'),
    dict(name='Soft Drink 300ml',              description='Coke, Fanta or Sprite',
         price=100.00, cost_price=55.00,  category='Beverages',
         stock=200, reorder_level=50, unit='bottle', icon='fa-bottle-water'),
    # Desserts
    dict(name='Chocolate Lava Cake',           description='Warm cake with ice cream',
         price=320.00, cost_price=130.00, category='Desserts',
         stock=999, reorder_level=0, unit='plate', icon='fa-cake-candles'),
]

_GENERAL = [
    # Mixed retail — a broadly applicable starter catalogue
    dict(name='Wireless Earbuds',             description='Bluetooth 5.3, 24hr battery',
         price=1800.00, cost_price=1100.00, category='Electronics',
         stock=40, reorder_level=8, unit='pcs', icon='fa-headphones'),
    dict(name='USB-C Cable 1m',               description='Fast-charge braided cable',
         price=350.00, cost_price=190.00, category='Electronics',
         stock=100, reorder_level=25, unit='pcs', icon='fa-plug'),
    dict(name='Extension Cord 4-way 3m',      description='Surge-protected 4-socket strip',
         price=850.00, cost_price=550.00, category='Electronics',
         stock=30, reorder_level=8, unit='pcs', icon='fa-bolt'),
    dict(name='Backpack 20L',                 description='Laptop compartment, waterproof base',
         price=2200.00, cost_price=1400.00, category='Bags',
         stock=20, reorder_level=5, unit='pcs', icon='fa-bag-shopping'),
    dict(name='Notebook A5 (Hardcover)',      description='200 pages, dotted pages',
         price=280.00, cost_price=160.00, category='Stationery',
         stock=80, reorder_level=20, unit='pcs', icon='fa-book'),
    dict(name='Ballpoint Pens 10-Pack',       description='Black, medium, smooth tip',
         price=120.00, cost_price=65.00,  category='Stationery',
         stock=200, reorder_level=50, unit='pack', icon='fa-pen'),
    dict(name='Desk Organiser',               description='5-slot bamboo organiser',
         price=750.00, cost_price=460.00, category='Office',
         stock=25, reorder_level=6, unit='pcs', icon='fa-box'),
    dict(name='Reusable Water Bottle 750ml',  description='BPA-free stainless steel',
         price=950.00, cost_price=580.00, category='Kitchen',
         stock=35, reorder_level=8, unit='pcs', icon='fa-bottle-water'),
    dict(name='Scented Candle Set (3)',       description='Vanilla, lavender & citrus',
         price=650.00, cost_price=380.00, category='Home',
         stock=50, reorder_level=12, unit='set', icon='fa-fire'),
    dict(name='Sunscreen SPF50 150ml',        description='Broad-spectrum daily sunscreen',
         price=780.00, cost_price=480.00, category='Personal Care',
         stock=40, reorder_level=10, unit='bottle', icon='fa-sun'),
    dict(name='Hand Sanitiser 500ml',         description='70% isopropyl alcohol gel',
         price=350.00, cost_price=200.00, category='Personal Care',
         stock=80, reorder_level=20, unit='bottle', icon='fa-pump-soap'),
    dict(name='Gift Wrapping Paper Roll',     description='Assorted colourful patterns',
         price=150.00, cost_price=80.00,  category='Gifts & Packaging',
         stock=100, reorder_level=25, unit='roll', icon='fa-gift'),
]

_SEEDS = {
    'pharmacy':    _PHARMACY,
    'supermarket': _SUPERMARKET,
    'hardware':    _HARDWARE,
    'salon':       _SALON,
    'restaurant':  _RESTAURANT,
    'general':     _GENERAL,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def seed_inventory(business_type: str = 'general') -> None:
    """Insert seed inventory rows for *business_type* if the table is empty.

    This is idempotent — it never modifies existing rows, so it is safe to
    call on every application boot.
    """
    if InventoryItem.query.count() > 0:
        return  # Never overwrite an existing catalogue

    catalogue = _SEEDS.get(business_type, _GENERAL)
    for data in catalogue:
        data = dict(data)
        qty = int(data.pop('stock', 0) or 0)
        item = InventoryItem(**data)
        db.session.add(item)
        db.session.flush()
        if qty > 0:
            stock.receive(item, qty, unit_cost=data.get('cost_price'), reason='initial_stock')
    db.session.commit()
