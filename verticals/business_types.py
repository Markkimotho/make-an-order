"""Business types: the one table of what each vertical is called and offers.

One entry per tenant vertical (``models.VERTICALS``), in display order:
grouped, with the specialised types first in their group. Templates (landing,
signup, onboarding) and code (seeding, settings) all read ``BUSINESS_CONFIGS``.

Keys of every entry:

- ``label``, ``icon`` (Font Awesome name), ``color`` (hex)
- ``group``: one of ``GROUPS``
- ``tagline``: short line under the label
- ``pitch``: one plain sentence for the landing card
- ``examples``: kinds of business that should pick this type
- ``description``: longer text for onboarding
- ``includes``: what the owner gets. Only features the type really has
  (``verticals/features.py``); ``tests/test_business_types.py`` checks it.
- ``seed``: starter catalogue name in ``services/seed_service.py``
- ``specialised``: True when the type has screens beyond the general shop set
- ``features``: the type's feature set, from ``VERTICAL_FEATURES``
"""
from models.base import VERTICALS
from verticals.features import VERTICAL_FEATURES

GROUPS = ("Health", "Retail", "Food & services", "Trade & institutions")

# Shared lines for every counter business (general shop feature set).
_TILL = 'Till with M-Pesa, bank and cash; M-Pesa confirmed before the receipt prints'
_ONLINE = 'Online shop and WhatsApp orders paid by M-Pesa'
_STOCK = 'Stock counts, low-stock alerts and purchase orders'
_BATCHES = 'Batch numbers and expiry dates on stock'
_MONEY = 'Markup pricing, daily sales, profit and cash reconciliation'
_STAFF = 'Staff roles, performance and activity log'
_CREDIT = 'Customer records and credit accounts with limits'


def _general(label, icon, color, group, tagline, pitch, examples, includes, seed):
    return {
        'label': label, 'icon': icon, 'color': color, 'group': group,
        'tagline': tagline, 'pitch': pitch, 'examples': examples,
        'description': f'{pitch} Sell at the counter and online, with stock, '
                       'pricing and money in one place.',
        'includes': includes, 'seed': seed, 'specialised': False,
    }


BUSINESS_CONFIGS = {
    # ── Health ────────────────────────────────────────────────────────────
    'pharmacy': {
        'label':       'Pharmacy',
        'icon':        'fa-prescription-bottle-medical',
        'color':       '#198754',
        'group':       'Health',
        'tagline':     'Batches & expiry · Patients · Controlled drugs · Profit',
        'pitch':       'Dispense, track every batch and keep the PPB register up to date.',
        'examples':    'Chemists, retail pharmacies, clinic dispensaries',
        'description': 'For chemists and pharmacies. Till with discounts and M-Pesa or '
                       'bank confirmation, batch and expiry tracking, recalls, patient '
                       'medication history, the controlled-drug register, purchase '
                       'orders, daily profit and cashier reconciliation.',
        'includes':    ['Point of sale with M-Pesa, bank and cash',
                        'Batch, expiry and recall tracking',
                        'Patient medication history and refills',
                        'Controlled-drug register (PPB)',
                        'Purchase orders and markup pricing',
                        'Daily sales, profit and cash reconciliation',
                        'Staff roles, performance and activity log'],
        'seed':        'pharmacy',
        'specialised': True,
    },
    'agrovet': _general(
        'Agrovet', 'fa-seedling', '#2e7d32', 'Health',
        'Seeds · Feeds · Vet drugs · Expiry',
        'Sell seed, feed, fertiliser and vet drugs with every batch and expiry date tracked.',
        'Agrovets, farm input shops, vet supply stores',
        [_TILL,
         'Batch and expiry tracking for vet drugs, seed and chemicals',
         'Sell by the kg or by the pack',
         _ONLINE, _STOCK, _CREDIT, _MONEY, _STAFF],
        'agrovet'),

    # ── Retail ────────────────────────────────────────────────────────────
    'supermarket': _general(
        'Supermarket / Minimart', 'fa-cart-shopping', '#0d6efd', 'Retail',
        'Groceries · FMCG · Expiry',
        'Fast barcode checkout for groceries, with expiry dates on perishables.',
        'Supermarkets, minimarts, groceries, kiosks',
        [_TILL,
         'Barcode scanning at the till',
         'Batch and expiry tracking on dairy and packaged food',
         _ONLINE, _STOCK, _MONEY, _STAFF],
        'supermarket'),
    'hardware': _general(
        'Hardware', 'fa-screwdriver-wrench', '#fd7e14', 'Retail',
        'Building · Tools · Trade accounts',
        'Cement, steel and tools at the counter, with fundi and contractor accounts on credit.',
        'Hardware stores, building suppliers, paint and electrical shops',
        [_TILL,
         'Contractor accounts on credit with limits',
         'Sell by the metre, bag or piece',
         _ONLINE, _STOCK, _MONEY, _STAFF],
        'hardware'),
    'electronics': _general(
        'Electronics / Phones', 'fa-mobile-screen', '#20c997', 'Retail',
        'Phones · Accessories · Repairs',
        'Phones, accessories and repair jobs, with every item counted and every sale paid.',
        'Phone shops, accessory stalls, electronics and repair shops',
        [_TILL,
         'Repairs and screen fitting sold as service items',
         'Barcode scanning for boxed stock',
         _ONLINE, _STOCK, _CREDIT, _MONEY, _STAFF],
        'electronics'),
    'cosmetics': _general(
        'Cosmetics / Beauty shop', 'fa-spray-can-sparkles', '#e83e8c', 'Retail',
        'Beauty · Skincare · Fragrance',
        'Skincare, hair products and fragrance with expiry dates and repeat customers on file.',
        'Beauty shops, cosmetics stores, perfume shops',
        [_TILL,
         'Batch and expiry tracking on skincare and hair products',
         'Customer records for repeat buyers',
         _ONLINE, _STOCK, _MONEY, _STAFF],
        'cosmetics'),
    'shop': _general(
        'Any other shop', 'fa-store', '#6c757d', 'Retail',
        'Any retail business',
        'For any business that sells goods or services over a counter.',
        'Boutiques, bookshops, stationers, butcheries, gas dealers, any duka',
        [_TILL, _ONLINE, _STOCK, _BATCHES, _CREDIT, _MONEY, _STAFF],
        'general'),

    # ── Food & services ───────────────────────────────────────────────────
    'restaurant': _general(
        'Restaurant / Café', 'fa-utensils', '#dc3545', 'Food & services',
        'Menu · Takeaway · Delivery',
        'Your menu on the till as tap-to-sell tiles, with takeaway orders over WhatsApp.',
        'Restaurants, cafés, hotels, food kiosks, bakeries',
        [_TILL,
         'Menu items on tap-to-sell tiles for a quick sale',
         'Takeaway orders through the online shop and WhatsApp',
         'Ingredients bought on purchase orders, expenses for net profit',
         _MONEY, _STAFF],
        'restaurant'),
    'salon': _general(
        'Salon / Barber / Spa', 'fa-scissors', '#d63384', 'Food & services',
        'Services · Products · Stylists',
        'Charge for haircuts and treatments and sell products on the same receipt.',
        'Salons, barbershops, spas, nail bars',
        [_TILL,
         'Services such as haircuts and braiding sold as items',
         'Hair and beauty products in stock on the same till',
         'Sales by staff member to see who brings in what',
         _ONLINE, _CREDIT, _MONEY, 'Staff roles and activity log'],
        'salon'),

    # ── Trade & institutions ──────────────────────────────────────────────
    'distributor': {
        'label':       'Distributor / Wholesaler',
        'icon':        'fa-truck',
        'color':       '#0d6efd',
        'group':       'Trade & institutions',
        'tagline':     'Trade credit · Route orders · Collections',
        'pitch':       'Sell to dukas on credit, take their orders by link and collect by M-Pesa.',
        'examples':    'FMCG, gas, drinks and hardware distributors, wholesalers',
        'description': 'For FMCG, gas and hardware distributors selling to dukas on '
                       'credit, with M-Pesa collections and credit limits.',
        'includes':    ['Point of sale with M-Pesa, bank and cash',
                        'Dukas on credit with limits and deni aging',
                        'Duka order links and price tiers',
                        'Purchase orders and markup pricing',
                        'Daily sales, profit and cash reconciliation',
                        'Staff roles, performance and activity log'],
        'seed':        'supermarket',
        'specialised': True,
    },
    'institution': {
        'label':       'School / Estate / Chama',
        'icon':        'fa-school',
        'color':       '#6f42c1',
        'group':       'Trade & institutions',
        'tagline':     'Pre-orders · M-Pesa collection · Pickup',
        'pitch':       'Collect uniform, levy and bundle payments by M-Pesa against each member.',
        'examples':    'Schools, gated estates, chamas, churches, clubs',
        'description': 'For schools, gated estates and chamas collecting payments '
                       'for uniforms, bundles and levies against a student or house number.',
        'includes':    ['Campaign links parents pay through M-Pesa',
                        'Instalments matched to admission or house numbers',
                        'Pickup codes and packing lists',
                        'Purchasing, expenses and collection reports',
                        'Staff roles and activity log'],
        'seed':        'general',
        'specialised': True,
    },
}

for _vertical, _config in BUSINESS_CONFIGS.items():
    _config['features'] = VERTICAL_FEATURES[_vertical]
assert set(BUSINESS_CONFIGS) == set(VERTICALS)
assert all(c['group'] in GROUPS for c in BUSINESS_CONFIGS.values())


def grouped():
    """``[(group, [(slug, config), ...]), ...]`` in ``GROUPS`` order, each
    group in table order. Empty groups are left out."""
    out = []
    for group in GROUPS:
        members = [(s, c) for s, c in BUSINESS_CONFIGS.items() if c['group'] == group]
        if members:
            out.append((group, members))
    return out


def specialised():
    """Slugs of the types with tools beyond the general shop set."""
    return [s for s, c in BUSINESS_CONFIGS.items() if c['specialised']]
