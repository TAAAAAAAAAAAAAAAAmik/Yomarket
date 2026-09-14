"""Минимальная сборка: как всё соединяется.

Запускать не нужно — адаптер площадки не написан. Это образец проводки:
видно, кто кого создаёт и что зовётся в цикле.

    python3 example_bot.py     # честно скажет, что адаптера нет
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "code"))

from catalog import Card, Denomination            # noqa: E402
from delivery import DeliveryEngine               # noqa: E402
from marketplace import PlayerokMarketplace       # noqa: E402
from store import JsonStore                       # noqa: E402
from supplier import ApprouteSupplier             # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")

# Период опроса заказов. Выясняется у площадки, а не угадывается: слишком
# часто — просьба сбавить темп, слишком редко — покупатель ждёт.
PERIOD = 60.0

# Какие товары умеем выдавать. Слова — по которым узнаём заказ в названии.
CARDS = [
    Card(slug="robux", title="Roblox", emoji="🎮",
         keywords=("robux", "робукс", "роблокс"),
         measure="робуксов",
         activation="Активируйте код на roblox.com/redeem.",
         services={"GL": "СЮДА-ID-УСЛУГИ-ГЛОБАЛ",
                   "RU": "СЮДА-ID-УСЛУГИ-РОССИЯ"}),
]


class Catalog:
    """Каталог поставщика с кешем.

    Кеш не для скорости, а по необходимости: `GET /services` разрешён
    **2 раза в минуту**. Без кеша два оплаченных заказа подряд означают два
    тяжёлых чтения, и всё это время опрос заказов стоит.
    """

    TTL = 120.0

    def __init__(self, supplier: ApprouteSupplier):
        self.supplier = supplier
        self._at = 0.0
        self._rows: dict[tuple[str, str], list[Denomination]] = {}

    def __call__(self, card: Card, region: str) -> list[Denomination]:
        import time
        key = (card.slug, region.upper())
        if time.time() - self._at < self.TTL and key in self._rows:
            return self._rows[key]

        service_id = card.services.get(region.upper(), "")
        if not service_id:
            return []

        # TODO: здесь читается каталог поставщика и приводится к Denomination.
        #       Остаток и цену берём оттуда же — но перед покупкой движок
        #       перечитает номинал отдельно, потому что кеш может устареть.
        rows: list[Denomination] = []
        self._rows[key] = rows
        self._at = time.time()
        return rows


async def notify(text: str) -> None:
    """Сообщить продавцу. В боевом боте — отправка в Telegram."""
    print("[продавцу]", text)


async def main() -> None:
    market = PlayerokMarketplace(token=os.environ.get("PLAYEROK_TOKEN", ""))
    supplier = ApprouteSupplier(
        api_key=os.environ["APPROUTE_KEY"],
        # Прокси с ПОСТОЯННЫМ адресом: у поставщика белый список IP, а адрес
        # сервера меняется при каждом выкате.
        proxy=os.environ.get("APPROUTE_PROXY", ""),
    )
    store = JsonStore("state/seller-1.json")
    engine = DeliveryEngine(
        market=market, supplier=supplier, store=store, cards=CARDS,
        notify=notify, catalog_of=Catalog(supplier),
        # Префикс ссылки покупки. У каждой площадки СВОЙ: ссылка уникальна в
        # пределах кабинета поставщика, а кабинет один на все площадки.
        reference_prefix="pk",
    )

    seen: set[str] = set()
    while True:
        try:
            for order in await market.paid_orders():
                if order.id in seen:
                    continue
                seen.add(order.id)
                await engine.on_paid_order(order)

            # Каждый проход, а не только при старте: обрыв случается чаще
            # всего от обычного выката, и без этого вызова деньги останутся
            # потраченными, а покупатель — без кода.
            await engine.resume_unfinished()
        except Exception as e:                       # noqa: BLE001
            # Исключение не убивает цикл: один упавший заказ не должен
            # уносить с собой остальные.
            logging.error("проход не удался: %s", e)
        await asyncio.sleep(PERIOD)


if __name__ == "__main__":
    asyncio.run(main())
