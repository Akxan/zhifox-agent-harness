from __future__ import annotations

from . import budget
from .app_types import AppPart
from .endpoints import EndpointStore
from .pricing import price_of as builtin_price
from .providers import find
from .settings import Settings


def merged_prices(settings: Settings, endpoints: EndpointStore) -> dict[str, dict]:
    """算钱用的价格表：你在设置里给内置模型填的（按模型名），加上自定义接口上每个模型的（按"接口/模型"，见 pricing.usage_key）。
    两边的名字不会撞上：自定义接口的带着 custom- 的前缀。"""
    return {**settings.prices, **endpoints.prices()}


class AppPrices(AppPart):
    def prices(self) -> dict[str, dict]:
        """算钱用的价格表（见 merged_prices）。"""
        return merged_prices(self.settings, self.endpoints)

    def price_of(self, provider_id: str, model: str, offpeak: bool = False) -> dict[str, float] | None:
        """一个模型的价格：自定义接口上的看 endpoints.json（本机和内网的默认不花钱），内置的看你填的和内置的价格表。"""
        provider = find(provider_id)
        if provider is not None and provider.endpoint is not None:
            return provider.endpoint.price_of(model)
        return builtin_price(model, self.settings.prices, offpeak=offpeak)

    def set_price(self, provider_id: str, model: str, price: dict | None) -> None:
        """设置里"价格"一节：自定义接口上的模型记进 endpoints.json，内置的记进 settings.json。"""
        provider = find(provider_id)
        if provider is not None and provider.endpoint is not None:
            self.endpoints.set_price(provider.id, model, price)
        else:
            self.settings.set_price(model, price)

    def spending(self) -> dict:
        """今天、本月花了多少，预算是多少、超了没有（见 ledger.py、budget.py）。

        这个月的花费刚越过预算时，顺便记下"已经提醒过了"，所以同一个预算只提醒一次。记不下来（磁盘写不进去）
        也不要紧，只是下一次还会再提醒。
        """
        today = self.agent.clock().date()
        month = budget.month_of(today)
        spent = self.ledger.month_total(month)
        amount = self.settings.budget
        if budget.should_alert(spent, amount, self.settings.budget_alert, month):
            try:
                self.settings.set_budget_alert({"month": month, "amount": amount})
            except OSError:
                pass
        return {
            "day": today.isoformat(),
            "today": self.ledger.day_total(today),
            "month": spent,
            "budget": amount,
            "over": spent >= amount if amount else False,
            "alert": budget.marker(self.settings.budget_alert, month),
            "error": self.ledger.error,
        }
