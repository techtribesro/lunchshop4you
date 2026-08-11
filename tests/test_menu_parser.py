from app.services.menu_parser import parse_menu_email

SAMPLE_EMAIL = """
PONDĚLÍ 11.8.2026

Polévka
Kuřecí vývar s nudlemi (1,3,9)
25 Kč

Hlavní jídlo 1
Kuřecí prsa na grilu
s bramborovou kaší (1,7)
185 Kč

Hlavní jídlo 2
Hovězí guláš s houskovým knedlíkem (1,3,7)
175 Kč

Vege. jídlo
Zeleninové rizoto s balkánským sýrem (7)
160 Kč

ÚTERÝ 12.8.2026

Polévka: Zelňačka se smetanou (7) 25 Kč

Hlavní jídlo 1
Vepřová panenka na hořčici, dušená rýže (1,10)
195 Kč
"""


def test_parses_items_grouped_by_day_and_category():
    items = parse_menu_email(SAMPLE_EMAIL)
    assert len(items) == 6

    monday_soup = items[0]
    assert monday_soup.day == "Monday"
    assert monday_soup.category == "Polévka"
    assert monday_soup.item_name == "Kuřecí vývar s nudlemi"
    assert monday_soup.price_czk == 25


def test_multiline_description_is_joined():
    items = parse_menu_email(SAMPLE_EMAIL)
    main1 = next(i for i in items if i.category == "Hlavní jídlo 1" and i.day == "Monday")
    assert main1.item_name == "Kuřecí prsa na grilu"
    assert "bramborovou kaší" in main1.description
    assert main1.price_czk == 185


def test_allergen_codes_are_stripped():
    items = parse_menu_email(SAMPLE_EMAIL)
    for item in items:
        assert "(" not in item.item_name
        assert "(" not in item.description


def test_single_line_category_with_colon():
    items = parse_menu_email(SAMPLE_EMAIL)
    tuesday_soup = next(i for i in items if i.day == "Tuesday" and i.category == "Polévka")
    assert tuesday_soup.item_name == "Zelňačka se smetanou"
    assert tuesday_soup.price_czk == 25


def test_second_day_is_parsed():
    items = parse_menu_email(SAMPLE_EMAIL)
    tuesday_items = [i for i in items if i.day == "Tuesday"]
    assert len(tuesday_items) == 2
