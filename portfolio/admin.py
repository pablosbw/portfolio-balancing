from django.contrib import admin

from .models import Order, Portfolio, Stock


@admin.register(Stock)
class StockAdmin(admin.ModelAdmin):
    list_display = ("symbol", "market", "company_name", "currency", "is_divisible", "current_price")
    search_fields = ("symbol", "company_name")


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = (
        "order_id", "asset", "side", "status",
        "proposed_quantity", "proposed_price", "executed_quantity", "executed_price", "created_at",
    )
    list_filter = ("side", "status", "user_id")


@admin.register(Portfolio)
class PortfolioAdmin(admin.ModelAdmin):
    list_display = ("portfolio_id", "user_id")
