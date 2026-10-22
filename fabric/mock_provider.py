from __future__ import annotations

"""Mock Fabric & Power BI data provider for local development without Azure credentials."""

from dataclasses import dataclass
from typing import Any

DEMO_SCHEMA_TABLES: dict[str, list[tuple[str, str]]] = {
    "DimCustomer": [
        ("CustomerKey", "int"),
        ("FirstName", "string"),
        ("LastName", "string"),
        ("EmailAddress", "string"),
        ("Gender", "string"),
        ("YearlyIncome", "decimal"),
        ("TotalChildren", "int"),
    ],
    "DimProduct": [
        ("ProductKey", "int"),
        ("ProductAlternateKey", "string"),
        ("EnglishProductName", "string"),
        ("StandardCost", "decimal"),
        ("Color", "string"),
        ("ModelName", "string"),
    ],
    "FactInternetSales": [
        ("ProductKey", "int"),
        ("CustomerKey", "int"),
        ("SalesOrderNumber", "string"),
        ("SalesOrderLineNumber", "int"),
        ("OrderQuantity", "int"),
        ("UnitPrice", "decimal"),
        ("ExtendedAmount", "decimal"),
        ("TotalProductCost", "decimal"),
        ("SalesAmount", "decimal"),
        ("TaxAmt", "decimal"),
        ("OrderDate", "datetime"),
    ],
}

DEMO_REQUIREMENTS = """
# Retail Analytics Dashboard Requirements
1. Track Total Sales Amount across regions and product categories.
2. Calculate Gross Margin ($ and %) = Sales Amount - Total Product Cost.
3. Customer Lifetime Value and purchase frequency distribution.
4. Month-over-month (MoM) sales growth percentage.
"""


def get_mock_workspace() -> dict[str, Any]:
    """Return mock workspace metadata for zero-cloud local testing."""
    return {
        "id": "mock-workspace-001",
        "name": "AdventureWorks-Local-Demo",
        "models": ["AdventureWorks_Sales"],
        "tables": list(DEMO_SCHEMA_TABLES.keys()),
        "description": "Pre-seeded local mock workspace for open-source testing.",
    }
