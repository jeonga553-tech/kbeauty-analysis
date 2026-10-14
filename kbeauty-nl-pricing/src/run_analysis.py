"""Run the full pricing analysis and write tables, charts and an Excel file.

Usage (from the project folder):  python src/run_analysis.py
"""
from pathlib import Path
import sys

import matplotlib
import pandas as pd

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent))
from model import (DATA, OUT, batch_economics, channel_economics, classify,  # noqa: E402
                   landed_costs, load_assumptions, market_prices)


def sensitivity(quote, comp, a, param, values):
    """Total blended profit for one MOQ batch of every product,
    for different values of one assumption."""
    rows = []
    for v in values:
        a2 = dict(a, **{param: v})
        d = channel_economics(landed_costs(quote, a2).merge(market_prices(comp), on="comparable_group"), a2)
        rows.append({param: v, "total_profit_all_batches": (d["blended_margin"] * d["moq"]).sum()})
    return pd.DataFrame(rows)


def main() -> None:
    OUT.mkdir(exist_ok=True)
    a = load_assumptions()
    quote = pd.read_csv(DATA / "supplier_quote.csv")
    comp = pd.read_csv(DATA / "competitors.csv")

    d = landed_costs(quote, a).merge(market_prices(comp), on="comparable_group", how="left")
    d = channel_economics(d, a)
    d["verdict"] = d.apply(classify, axis=1)
    b = batch_economics(d, a)

    # ---------- Tables ----------
    summary = d[["product_name_en", "fob_usd", "moq", "landed_cost", "market_min",
                 "market_median", "target_price", "bol_breakeven_price",
                 "bol_margin", "bol_margin_pct", "web_margin", "web_margin_pct",
                 "blended_margin_pct", "repeat_blended_margin_pct",
                 "fob_share_of_net_price", "max_fob_usd_for_25pct", "verdict"]].round(2)
    summary.to_csv(OUT / "product_summary.csv", index=False)

    cost_cols = ["fob_eur", "fx_cost", "freight", "duty", "label", "shipment_fixed", "compliance"]
    d[["product_name_en"] + cost_cols + ["landed_cost"]].round(2).to_csv(OUT / "landed_cost_breakdown.csv", index=False)
    b.round(2).to_csv(OUT / "batch_economics.csv", index=False)

    one_off = a["label_design_fixed"] + a["kvk_registration"] + a["photo_content_fixed"]
    platform = a["web_platform_monthly"] * a["months_to_sell_batch"]
    totals = {
        "cash_for_all_MOQ_stock_eur": b["cash_for_stock"].sum(),
        "other_one_off_costs_eur": one_off,
        "webshop_fees_over_horizon_eur": platform,
        "total_cash_needed_eur": b["cash_for_stock"].sum() + one_off + platform,
        "profit_if_everything_sells_eur": b["profit_if_sold_out"].sum() - one_off - platform,
        "units_total": int(b["moq"].sum()),
    }
    pd.Series(totals).round(0).to_csv(OUT / "totals.csv", header=["value"])

    # ---------- Optimistic scenario ----------
    scen = pd.read_csv(DATA / "scenarios.csv")
    a_opt = dict(a, **dict(zip(scen["parameter"], scen["value"].astype(float))))
    d_opt = channel_economics(landed_costs(quote, a_opt).merge(market_prices(comp), on="comparable_group"), a_opt)
    d_opt["verdict"] = d_opt.apply(classify, axis=1)
    scen_table = pd.DataFrame({
        "product_name_en": d["product_name_en"],
        "base_blended_margin_pct": d["blended_margin_pct"].round(2),
        "optimistic_target_price": d_opt["target_price"].round(2),
        "optimistic_blended_margin_pct": d_opt["blended_margin_pct"].round(2),
        "optimistic_verdict": d_opt["verdict"],
        "max_fob_usd_for_25pct_optimistic": d_opt["max_fob_usd_for_25pct"].round(2),
        "quoted_fob_usd": d["fob_usd"],
    })
    scen_table["fob_cut_needed_pct"] = (1 - scen_table["max_fob_usd_for_25pct_optimistic"] / scen_table["quoted_fob_usd"]).clip(lower=0).round(2)
    scen_table.to_csv(OUT / "scenario_comparison.csv", index=False)
    opt_profit = (d_opt["blended_margin"] * d_opt["moq"]).sum()

    sens_cac = sensitivity(quote, comp, a, "web_cac_per_order", [6, 8, 10, 12, 15, 20])
    sens_disc = sensitivity(quote, comp, a, "price_discount_vs_market", [0.7, 0.75, 0.8, 0.85, 0.9, 1.0])
    sens_freight = sensitivity(quote, comp, a, "freight_per_kg", [3, 5, 7, 9, 12])

    with pd.ExcelWriter(OUT / "kbeauty_nl_pricing.xlsx") as xw:
        summary.to_excel(xw, sheet_name="Summary", index=False)
        d[["product_name_en"] + cost_cols + ["landed_cost"]].round(2).to_excel(xw, sheet_name="Landed cost", index=False)
        b.round(2).to_excel(xw, sheet_name="Batch economics", index=False)
        scen_table.to_excel(xw, sheet_name="Scenario base vs optimistic", index=False)
        comp.to_excel(xw, sheet_name="Competitors (bol.com)", index=False)
        pd.read_csv(DATA / "assumptions.csv").to_excel(xw, sheet_name="Assumptions", index=False)
        sens_cac.round({"total_profit_all_batches": 0}).to_excel(xw, sheet_name="Sensitivity CAC", index=False)
        sens_disc.round({"total_profit_all_batches": 0}).to_excel(xw, sheet_name="Sensitivity price", index=False)
        sens_freight.round({"total_profit_all_batches": 0}).to_excel(xw, sheet_name="Sensitivity freight", index=False)

    # ---------- Charts ----------
    s = d.sort_values("blended_margin_pct")
    fig, ax = plt.subplots(figsize=(9, 4.8))
    colors = ["tab:green" if v >= 0.25 else "tab:orange" if v >= 0.10 else "tab:red"
              for v in s["blended_margin_pct"]]
    ax.barh(s["product_name_en"], s["blended_margin_pct"] * 100, color=colors)
    ax.axvline(0, color="black", lw=0.8)
    ax.axvline(25, color="grey", ls="--", lw=0.8)
    ax.set(title="Blended contribution margin per product (70% bol.com / 30% webshop)",
           xlabel="% of net revenue (excl. VAT)")
    ax.grid(alpha=0.3, axis="x")
    fig.tight_layout()
    fig.savefig(OUT / "margin_by_product.png", dpi=150)

    fig, ax = plt.subplots(figsize=(9, 4.8))
    s = d.sort_values("market_median")
    y = range(len(s))
    ax.hlines(y, s["market_min"], s["market_max"], color="lightgrey", lw=6, label="bol.com price range (known brands)")
    ax.scatter(s["market_median"], y, color="grey", zorder=3, label="Market median")
    ax.scatter(s["target_price"], y, color="tab:blue", zorder=4, label="Our target price")
    ax.scatter(s["bol_breakeven_price"], y, color="tab:red", marker="|", s=250, zorder=5, label="bol.com break-even price")
    ax.set_yticks(list(y))
    ax.set_yticklabels(s["product_name_en"])
    ax.set(title="Where our price sits vs Korean brands on bol.com", xlabel="EUR incl. VAT")
    ax.legend(fontsize=8, loc="upper center", bbox_to_anchor=(0.5, -0.14), ncol=4, frameon=False)
    ax.grid(alpha=0.3, axis="x")
    fig.tight_layout()
    fig.savefig(OUT / "price_positioning.png", dpi=150, bbox_inches="tight")

    fig, ax = plt.subplots(figsize=(9, 4.8))
    s = d.sort_values("landed_cost")
    left = pd.Series(0.0, index=s.index)
    labels = {"fob_eur": "Supplier price", "fx_cost": "FX", "freight": "Air freight", "duty": "Duty",
              "label": "Dutch label", "shipment_fixed": "Customs & handling", "compliance": "RP / safety report"}
    for c in cost_cols:
        ax.barh(s["product_name_en"], s[c], left=left, label=labels[c])
        left += s[c]
    ax.set(title="Landed cost per unit (excl. VAT)", xlabel="EUR")
    ax.legend(fontsize=8, loc="lower right")
    ax.grid(alpha=0.3, axis="x")
    fig.tight_layout()
    fig.savefig(OUT / "landed_cost_breakdown.png", dpi=150)

    # ---------- Console ----------
    pd.set_option("display.width", 200)
    print(summary.to_string(index=False))
    print("\nTotals:", {k: round(v) for k, v in totals.items()})
    print("\nBase vs optimistic scenario:\n", scen_table.to_string(index=False))
    print(f"\nOptimistic scenario, total contribution for all MOQ batches: {opt_profit:,.0f} EUR")
    print("\nSensitivity to webshop ad cost per order:\n", sens_cac.round({"total_profit_all_batches": 0}).to_string(index=False))
    print("\nSensitivity to price vs market:\n", sens_disc.round({"total_profit_all_batches": 0}).to_string(index=False))
    print("\nSensitivity to freight per kg:\n", sens_freight.round({"total_profit_all_batches": 0}).to_string(index=False))


if __name__ == "__main__":
    main()
