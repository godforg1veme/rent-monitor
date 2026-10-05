import html


def escaped_parts(text, max_encoded=3000):
    parts, chars, size = [], [], 0
    for char in str(text):
        encoded = html.escape(char)
        if size + len(encoded) > max_encoded:
            parts.append("".join(chars))
            chars, size = [], 0
        chars.append(encoded)
        size += len(encoded)
    if chars:
        parts.append("".join(chars))
    return parts


def format_messages(candidate, details, event):
    """Keep public fields intact; don't invent seller stats or publication time."""
    prefix = {
        "delivery-test": "Проверка доставки: актуальное объявление, не сигнал о новой публикации.",
        "new": "Новое в наблюдаемой выдаче.",
        "changed": "Изменилось объявление в наблюдаемой выдаче.",
    }[event]
    blocks = [f"<i>{prefix}</i>"]

    def code(text):
        blocks.extend(f"<pre>{part}</pre>" for part in escaped_parts(text))

    code(candidate.title or "Объявление Авито")
    basic = []
    if candidate.price_rub is not None:
        basic.append(f"Цена: {candidate.price_rub:,} ₽/мес.".replace(",", " "))
    if candidate.address:
        basic.append(f"Адрес: {candidate.address}")
    if candidate.metro:
        basic.append(f"Метро: {candidate.metro}")
    if details.get("published_label"):
        basic.append(f"Опубликовано: {details['published_label']}")
    code("\n".join(basic))
    blocks.extend(f"<b>Описание:</b>\n{part}" for part in escaped_parts(details["description"]))
    if details.get("characteristics"):
        code("\n".join(f"{key}: {value}" for key, value in details["characteristics"].items()))
    seller = []
    for key, label in [
        ("seller_name", "Продавец"),
        ("seller_type_label", "Тип"),
        ("seller_rating", "Рейтинг"),
        ("seller_reviews", "Отзывы"),
    ]:
        if details.get(key):
            seller.append(f"{label}: {details[key]}")
    if not seller and details.get("seller_info"):
        seller.append(details["seller_info"])
    if seller:
        code("\n".join(seller))
    code(f"Поиск: квартиры\nОбъявление: #{candidate.source_id}")
    messages, current = [], ""
    for block in blocks:
        if len(current) + len(block) + 2 > 3800:
            messages.append(current)
            current = ""
        current += ("\n\n" if current else "") + block
    if current:
        messages.append(current)
    return messages
