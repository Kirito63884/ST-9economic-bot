"""Classic 40-space Atlantic City board and paraphrased Russian card effects.

Amounts are integer in-game dollars, independent of site Bishopcoin wallets.
The modern fixed-tax edition is used: income tax 200, luxury tax 100.
"""

COLORS = {
    "brown": "#a87453",
    "blue": "#8bd3ed",
    "pink": "#de8bca",
    "orange": "#efa75c",
    "red": "#e57679",
    "yellow": "#e6d167",
    "green": "#7ebd91",
    "navy": "#8a9fda",
}


def street(index, name, price, group, cost, rents):
    return {
        "id": index,
        "name": name,
        "kind": "street",
        "price": price,
        "group": group,
        "color": COLORS[group],
        "build_cost": cost,
        "rents": rents,
        "mortgage": price // 2,
        "interest": (price // 2 + 9) // 10,
    }


BOARD = [
    {"id": i, "name": str(i), "kind": "special", "price": 0, "color": "#34475a"}
    for i in range(40)
]
for data in (
    (1, "Медитеррейниан-авеню", 60, "brown", 50, [2, 10, 30, 90, 160, 250]),
    (3, "Балтик-авеню", 60, "brown", 50, [4, 20, 60, 180, 320, 450]),
    (6, "Ориентал-авеню", 100, "blue", 50, [6, 30, 90, 270, 400, 550]),
    (8, "Вермонт-авеню", 100, "blue", 50, [6, 30, 90, 270, 400, 550]),
    (9, "Коннектикут-авеню", 120, "blue", 50, [8, 40, 100, 300, 450, 600]),
    (11, "Сент-Чарльз-плейс", 140, "pink", 100, [10, 50, 150, 450, 625, 750]),
    (13, "Стейтс-авеню", 140, "pink", 100, [10, 50, 150, 450, 625, 750]),
    (14, "Вирджиния-авеню", 160, "pink", 100, [12, 60, 180, 500, 700, 900]),
    (16, "Сент-Джеймс-плейс", 180, "orange", 100, [14, 70, 200, 550, 750, 950]),
    (18, "Теннесси-авеню", 180, "orange", 100, [14, 70, 200, 550, 750, 950]),
    (19, "Нью-Йорк-авеню", 200, "orange", 100, [16, 80, 220, 600, 800, 1000]),
    (21, "Кентукки-авеню", 220, "red", 150, [18, 90, 250, 700, 875, 1050]),
    (23, "Индиана-авеню", 220, "red", 150, [18, 90, 250, 700, 875, 1050]),
    (24, "Иллинойс-авеню", 240, "red", 150, [20, 100, 300, 750, 925, 1100]),
    (26, "Атлантик-авеню", 260, "yellow", 150, [22, 110, 330, 800, 975, 1150]),
    (27, "Вентнор-авеню", 260, "yellow", 150, [22, 110, 330, 800, 975, 1150]),
    (29, "Марвин-гарденс", 280, "yellow", 150, [24, 120, 360, 850, 1025, 1200]),
    (31, "Пасифик-авеню", 300, "green", 200, [26, 130, 390, 900, 1100, 1275]),
    (32, "Северная Каролина", 300, "green", 200, [26, 130, 390, 900, 1100, 1275]),
    (34, "Пенсильвания-авеню", 320, "green", 200, [28, 150, 450, 1000, 1200, 1400]),
    (37, "Парк-плейс", 350, "navy", 200, [35, 175, 500, 1100, 1300, 1500]),
    (39, "Бордуок", 400, "navy", 200, [50, 200, 600, 1400, 1700, 2000]),
):
    BOARD[data[0]] = street(*data)
for index, name in (
    (5, "Ридингский вокзал"),
    (15, "Пенсильванский вокзал"),
    (25, "Вокзал B&O"),
    (35, "Вокзал Шорт-Лайн"),
):
    BOARD[index] = {
        "id": index,
        "name": name,
        "kind": "rail",
        "price": 200,
        "mortgage": 100,
        "interest": 10,
        "color": "#a1b7c2",
    }
for index, name in ((12, "Электрическая компания"), (28, "Водопроводная компания")):
    BOARD[index] = {
        "id": index,
        "name": name,
        "kind": "utility",
        "price": 150,
        "mortgage": 75,
        "interest": 8,
        "color": "#b5c2cf",
    }
for index, name, kind in (
    (0, "Старт · +200", "go"),
    (2, "Общественная казна", "chest"),
    (4, "Подоходный налог", "tax"),
    (7, "Шанс", "chance"),
    (10, "Тюрьма / В гостях", "jail"),
    (17, "Общественная казна", "chest"),
    (20, "Бесплатная стоянка", "parking"),
    (22, "Шанс", "chance"),
    (30, "Отправляйтесь в тюрьму", "go_jail"),
    (33, "Общественная казна", "chest"),
    (36, "Шанс", "chance"),
    (38, "Налог на роскошь", "tax"),
):
    BOARD[index].update(name=name, kind=kind)
BOARD[4]["tax"] = 200
BOARD[38]["tax"] = 100
PROPERTIES = {cell["id"]: cell for cell in BOARD if cell["price"]}
GROUPS = {
    group: [c["id"] for c in BOARD if c.get("group") == group] for group in COLORS
}

# Effect names are private engine data. Text is shown only after a card is drawn.
CHANCE = [
    {"text": "Перейдите на Старт и получите 200.", "effect": "go"},
    {"text": "Перейдите на Бордуок.", "effect": "advance", "to": 39},
    {
        "text": "Перейдите на Иллинойс-авеню. При пересечении Старта получите 200.",
        "effect": "advance",
        "to": 24,
    },
    {
        "text": "Перейдите на Сент-Чарльз-плейс. При пересечении Старта получите 200.",
        "effect": "advance",
        "to": 11,
    },
    {
        "text": "Ближайший вокзал: при чужом владении заплатите двойную ренту.",
        "effect": "rail",
    },
    {
        "text": "Отправляйтесь на ближайший вокзал. Рента чужому владельцу удваивается.",
        "effect": "rail",
    },
    {
        "text": "Ближайшая коммунальная компания: чужому владельцу — десятикратная сумма нового броска.",
        "effect": "utility",
    },
    {
        "text": "Банк выплачивает дивиденды: получите 50.",
        "effect": "cash",
        "amount": 50,
    },
    {
        "text": "Бесплатный выход из тюрьмы. Сохраните карточку или передайте по сделке.",
        "effect": "jail_card",
    },
    {"text": "Отступите на три клетки.", "effect": "back", "steps": 3},
    {"text": "В тюрьму, без премии за Старт.", "effect": "jail"},
    {
        "text": "Ремонт: 25 за каждый дом и 100 за каждый отель.",
        "effect": "repairs",
        "house": 25,
        "hotel": 100,
    },
    {
        "text": "Штраф за превышение скорости: заплатите 15.",
        "effect": "cash",
        "amount": -15,
    },
    {
        "text": "Поездка на Ридингский вокзал. За пересечение Старта — 200.",
        "effect": "advance",
        "to": 5,
    },
    {
        "text": "Вас избрали председателем: заплатите каждому другому игроку 50.",
        "effect": "each",
        "amount": -50,
    },
    {
        "text": "Погашение строительного займа: получите 150.",
        "effect": "cash",
        "amount": 150,
    },
]
CHEST = [
    {"text": "Перейдите на Старт и получите 200.", "effect": "go"},
    {
        "text": "Ошибка банка в вашу пользу: получите 200.",
        "effect": "cash",
        "amount": 200,
    },
    {"text": "Услуги врача: заплатите 50.", "effect": "cash", "amount": -50},
    {"text": "Продажа акций: получите 50.", "effect": "cash", "amount": 50},
    {
        "text": "Бесплатный выход из тюрьмы. Карточку можно сохранить или передать по сделке.",
        "effect": "jail_card",
    },
    {"text": "В тюрьму, без прохождения Старта и премии.", "effect": "jail"},
    {
        "text": "Выплата отпускного фонда: получите 100.",
        "effect": "cash",
        "amount": 100,
    },
    {"text": "Возврат налога: получите 20.", "effect": "cash", "amount": 20},
    {
        "text": "День рождения: получите по 10 от каждого другого игрока.",
        "effect": "each",
        "amount": 10,
    },
    {"text": "Страховая выплата: получите 100.", "effect": "cash", "amount": 100},
    {"text": "Расходы на больницу: заплатите 100.", "effect": "cash", "amount": -100},
    {"text": "Обучение: заплатите 50.", "effect": "cash", "amount": -50},
    {"text": "Консультационные услуги: получите 25.", "effect": "cash", "amount": 25},
    {
        "text": "Ремонт улиц: 40 за дом, 115 за отель.",
        "effect": "repairs",
        "house": 40,
        "hotel": 115,
    },
    {"text": "Приз конкурса: получите 10.", "effect": "cash", "amount": 10},
    {"text": "Наследство: получите 100.", "effect": "cash", "amount": 100},
]
DECKS = {"chance": CHANCE, "chest": CHEST}
JAIL_CARDS = {
    key: next(i for i, c in enumerate(cards) if c["effect"] == "jail_card")
    for key, cards in DECKS.items()
}
