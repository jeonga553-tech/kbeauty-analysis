"""Price competitiveness and unit economics for importing Korean skincare
into the Netherlands.

For each product in the supplier quote we calculate:
1. Landed cost per unit: supplier price + FX + freight + duty + labelling
   + a share of fixed import and compliance costs.
2. A target retail price: the median price of comparable Korean products on
   bol.com, discounted because the brand is unknown in the Netherlands.
3. Contribution margin per unit in two channels:
   - bol.com (marketplace commission, shipping, sponsored ads)
   - own webshop (payment fees, shipping, Instagram/Google ad cost per order)
4. Batch economics at the minimum order quantity (MOQ): cash needed,
   profit if the batch sells out, and the break-even sell-through.

All inputs live in data/*.csv so assumptions can be changed without
touching the code. Run from the project folder:  python src/model.py
"""
from pathlib import Path

import pandas as pd

DATA = Path("data")
OUT = Path("outputs")


def load_assumptions() -> dict:
    a = pd.read_csv(DATA / "assumptions.csv")
    return dict(zip(a["parameter"], a["value"].astype(float)))


def landed_costs(quote: pd.DataFrame, a: dict) -> pd.DataFrame:
    """Cost per unit to get one product onto a Dutch shelf (excl. VAT).

    Import VAT (21%) is left out on purpose: a VAT-registered business
    reclaims it, so it is a cash-flow timing issue, not a cost.
    """
    q = quote.copy()
    q["fob_eur"] = q["fob_usd"] * a["usd_eur"]
    q["fx_cost"] = q["fob_eur"] * a["fx_spread"]

    # Freight: charged on weight, so heavy jars cost more to ship than pouches
    q["ship_kg"] = q["est_unit_weight_kg"] * (1 + a["packaging_weight_uplift"])
    q["freight"] = q["ship_kg"] * a["freight_per_kg"]

    # Customs duty is charged on the value including freight (CIF).
    # With an EU-Korea FTA origin statement the rate is 0%.
    duty_rate = q["mfn_duty_rate"] * (0 if a["use_fta_origin_proof"] else 1)
    q["duty"] = (q["fob_eur"] + q["freight"]) * duty_rate

    q["label"] = a["dutch_label_per_unit"]

    # Fixed costs per shipment are shared across all units in the order,
    # in proportion to each product's order value.
    order_value = q["fob_eur"] * q["moq"]
    share = order_value / order_value.sum()
    shipment_fixed = a["customs_clearance_fixed"] + a["wire_fee"]
    q["shipment_fixed"] = shipment_fixed * share / q["moq"]

    # One-off compliance (Responsible Person, safety report) is per SKU,
    # spread over the first batch.
    q["compliance"] = a["compliance_per_sku"] / q["moq"]

    cost_cols = ["fob_eur", "fx_cost", "freight", "duty", "label",
                 "shipment_fixed", "compliance"]
    q["landed_cost"] = q[cost_cols].sum(axis=1)
    return q


def market_prices(comp: pd.DataFrame) -> pd.DataFrame:
    """Median, min and max bol.com price per comparable group (incl. VAT)."""
    return (comp.groupby("comparable_group")["price_eur"]
                .agg(market_min="min", market_median="median",
                     market_max="max", n_competitors="count")
                .reset_index())


def channel_economics(df: pd.DataFrame, a: dict) -> pd.DataFrame:
    """Contribution margin per unit for bol.com and an own webshop."""
    d = df.copy()
    d["target_price"] = (d["market_median"] * a["price_discount_vs_market"]).round(1) - 0.01
    d["net_revenue"] = d["target_price"] / (1 + a["vat_rate"])
    returns = d["net_revenue"] * a["returns_damage_rate"]

    # bol.com: commission is charged on the price incl. VAT
    bol_units = a["bol_units_per_order"]
    d["bol_commission"] = d["target_price"] * a["bol_commission_pct"] + a["bol_commission_fixed"]
    d["bol_shipping"] = (a["bol_shipping_per_order"] + a["packaging_per_order"]) / bol_units
    d["bol_ads"] = d["target_price"] * a["bol_ad_rate"]
    d["bol_margin"] = (d["net_revenue"] - d["landed_cost"] - d["bol_commission"]
                       - d["bol_shipping"] - d["bol_ads"] - returns)

    # Own webshop: customers come from Instagram / Google ads
    web_units = a["web_units_per_order"]
    d["web_payment"] = d["target_price"] * a["web_payment_pct"] + a["web_payment_fixed"] / web_units
    d["web_shipping"] = (a["web_shipping_per_order"] + a["packaging_per_order"]) / web_units
    d["web_marketing"] = a["web_cac_per_order"] / web_units
    d["web_margin"] = (d["net_revenue"] - d["landed_cost"] - d["web_payment"]
                       - d["web_shipping"] - d["web_marketing"] - returns)

    for ch in ("bol", "web"):
        d[f"{ch}_margin_pct"] = d[f"{ch}_margin"] / d["net_revenue"]

    # Blended margin using the assumed channel mix
    mix = a["channel_mix_bol"]
    d["blended_margin"] = mix * d["bol_margin"] + (1 - mix) * d["web_margin"]
    d["blended_margin_pct"] = d["blended_margin"] / d["net_revenue"]

    # Lowest price (incl. VAT) at which a bol.com sale breaks even
    var_pct = (1 / (1 + a["vat_rate"])) * (1 - a["returns_damage_rate"]) - a["bol_commission_pct"] - a["bol_ad_rate"]
    fixed = d["landed_cost"] + a["bol_commission_fixed"] + d["bol_shipping"]
    d["bol_breakeven_price"] = fixed / var_pct
    d["price_vs_market_low"] = d["target_price"] / d["market_min"]

    # Repeat orders: the one-off compliance cost is already paid
    d["repeat_landed_cost"] = d["landed_cost"] - d["compliance"]
    d["repeat_blended_margin"] = d["blended_margin"] + d["compliance"]
    d["repeat_blended_margin_pct"] = d["repeat_blended_margin"] / d["net_revenue"]

    # Supplier price as a share of what we actually receive per unit
    d["fob_share_of_net_price"] = d["fob_eur"] / d["net_revenue"]

    # Negotiation target: the highest supplier price (USD) that still gives
    # a 25% blended margin on repeat orders at our target retail price.
    # Margin moves 1:1 with the supplier cost (incl. FX spread), so:
    #   max cost = current cost + (current margin - 25% of net revenue)
    gap = d["repeat_blended_margin"] - 0.25 * d["net_revenue"]
    max_fob_eur = d["fob_eur"] + gap / (1 + a["fx_spread"])
    d["max_fob_usd_for_25pct"] = (max_fob_eur / a["usd_eur"]).clip(lower=0)
    return d


def batch_economics(d: pd.DataFrame, a: dict) -> pd.DataFrame:
    """Cash needed to buy one MOQ batch, and what it returns if it sells out."""
    b = d[["product_key", "product_name_en", "moq", "landed_cost", "blended_margin"]].copy()
    b["cash_for_stock"] = b["moq"] * b["landed_cost"]
    b["profit_if_sold_out"] = b["moq"] * b["blended_margin"]
    revenue_net = d["net_revenue"]
    # Cash returned per unit sold, after channel costs = landed cost + margin.
    # If that is zero or negative, the batch never pays back (NaN).
    cash_back_per_unit = b["landed_cost"] + d["blended_margin"]
    b["sell_through_to_break_even"] = (b["cash_for_stock"] / (b["moq"] * cash_back_per_unit)).where(cash_back_per_unit > 0)
    b["sell_through_to_break_even"] = b["sell_through_to_break_even"].where(b["sell_through_to_break_even"] <= 1)
    b["retail_to_fob_multiple"] = d["market_median"] / d["fob_eur"]
    b["net_revenue_if_sold_out"] = b["moq"] * revenue_net
    return b


def classify(row) -> str:
    if row["blended_margin_pct"] >= 0.25 and row["bol_margin"] > 0:
        return "GO: healthy margin"
    if row["blended_margin_pct"] >= 0.10:
        return "MAYBE: thin margin, test small"
    if row["blended_margin_pct"] >= 0:
        return "WEAK: barely covers costs"
    return "NO: loses money at market price"
