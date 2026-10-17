import io

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

from sheets_service import Transaction  # noqa: E402


def _bar_panel(ax, series: pd.Series, title: str, currency: str) -> None:
    bars = ax.bar(series.index, series.values, color="#4C78A8")
    ax.set_title(title)
    ax.set_ylabel(currency)
    ax.tick_params(axis="x", rotation=30)
    for bar, value in zip(bars, series.values):
        ax.annotate(f"{value:,.2f}", (bar.get_x() + bar.get_width() / 2, bar.get_height()),
                    ha="center", va="bottom", fontsize=8)


def render_dashboard(transactions: list[Transaction], month_label: str) -> bytes | None:
    """Return PNG bytes for spending by category and by card, or None if there is no data."""
    if not transactions:
        return None
    df = pd.DataFrame([
        {"category": t.category or "Other", "card": t.card or "Unknown",
         "amount": t.amount, "currency": t.currency}
        for t in transactions
    ])
    currencies = [t.currency for t in transactions if t.currency]
    currency = max(set(currencies), key=currencies.count) if currencies else ""
    by_category = df.groupby("category")["amount"].sum().sort_values(ascending=False)
    by_card = df.groupby("card")["amount"].sum().sort_values(ascending=False)

    fig, (ax_cat, ax_card) = plt.subplots(1, 2, figsize=(13, 5.5))
    _bar_panel(ax_cat, by_category, "Spending by category", currency)
    _bar_panel(ax_card, by_card, "Spending by card", currency)
    fig.suptitle(f"Expenses for {month_label}: total {df['amount'].sum():,.2f} {currency}".strip())
    fig.tight_layout()

    buffer = io.BytesIO()
    fig.savefig(buffer, format="png", dpi=120)
    plt.close(fig)
    return buffer.getvalue()
