"""Money reports for the owner: daily sales and profit, and cashier
reconciliation. Read-only; nothing here writes the database except
``reconciliation.alert_variance`` (the SMS outbox).

profit          revenue, COGS, gross and net profit per Nairobi day
reconciliation  per shift and per day: cash counted vs expected, M-Pesa
                and bank checks
"""
from services.reports import profit, reconciliation  # noqa: F401
