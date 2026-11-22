"""Bounded factual inquiries and evidence-checked interpretation of supplier replies."""
from __future__ import annotations

import re

from core import rules
from core.chase_evidence import day, quantity


# Render supplier mail directly from the PO so previews and live mail are identical.
WORDING = {
    "en": (
        "Could you confirm the following for delivery to {site}?",
        "Thank you.",
        "A quick follow-up on the {site} delivery below.",
        "For line {line} ({name}), please confirm how many of the {qty} units will arrive and when, including any split deliveries. "
        "The requested delivery date on our order is {date}.",
        "Please also confirm the artwork or drawing revision.",
    ),
    "de": (
        "Können Sie uns die folgenden Angaben zur Lieferung nach {site} bestätigen?",
        "Vielen Dank.",
        "Eine kurze Nachfrage zur Lieferung nach {site}.",
        "Bitte bestätigen Sie für Position {line} ({name}), wie viele der {qty} Stück wann eintreffen, auch bei Teillieferungen. "
        "Der gewünschte Liefertermin laut Bestellung ist der {date}.",
        "Bitte bestätigen Sie auch den Stand der Druckdatei oder Zeichnung.",
    ),
    "pl": (
        "Czy mogą Państwo potwierdzić poniższe informacje o dostawie do {site}?",
        "Dziękujemy.",
        "Wracamy z krótkim pytaniem o dostawę do {site}.",
        "Dla pozycji {line} ({name}) prosimy potwierdzić, ile z {qty} sztuk dotrze i kiedy, również przy dostawach częściowych. "
        "Termin dostawy w zamówieniu to {date}.",
        "Prosimy również o potwierdzenie wersji grafiki lub rysunku.",
    ),
    "zh": (
        "请确认以下发往{site}的交货信息。",
        "谢谢。",
        "我们想跟进以下{site}交货信息。",
        "请确认第 {line} 行（{name}）的 {qty} 件货物何时送达，每批各有多少件。订单要求的交货日期是 {date}。",
        "也请确认印刷文件或图纸的版本。",
    ),
    "ko": (
        "다음 {site} 납품 정보를 확인해 주시겠습니까?",
        "감사합니다.",
        "아래 {site} 납품 건을 다시 문의드립니다.",
        "{line}번 항목({name})의 {qty}개에 대해 도착 일정과 수량을 확인 부탁드립니다. "
        "분할 납품 시 각 납품의 수량과 일정을 알려 주세요. 주문서의 요청 납품일은 {date}입니다.",
        "인쇄 파일 또는 도면의 버전도 확인해 주세요.",
    ),
}


def _body(language, lines, reminder):
    opening, closing, follow_up, question, revision = WORDING[language]
    site = rules.cfg("localized_site_names").get(language, rules.cfg("site_name"))
    paragraphs = [(follow_up if reminder else opening).format(site=site)]
    for line, kinds in lines:
        text = question.format(line=line.key[2], name=line.name, qty=f"{line.qty:g}", date=line.need.isoformat())
        if line.revision or "contradicting_revision" in kinds:
            text += " " + revision
        if "amended_confirmation" in kinds:
            text += " " + {
                "en": "Please also confirm which acknowledgment is current and what changed.",
                "de": "Bitte bestätigen Sie auch, welche Auftragsbestätigung gilt und was geändert wurde.",
                "pl": "Prosimy również potwierdzić, które potwierdzenie zamówienia obowiązuje i co się zmieniło.",
                "zh": "也请确认以哪份订单确认为准，以及变更内容。",
                "ko": "어떤 주문 확인서가 유효한지와 변경 사항도 알려 주세요.",
            }[language]
        paragraphs.append(text)
    paragraphs.extend([closing, f"Dockside | {rules.cfg('customer_name')}"])
    return "\n\n".join(paragraphs)


SUBJECTS = {
    "en": "Purchase order {po} — {site} delivery",
    "de": "Bestellung {po} — Lieferung nach {site}",
    "pl": "Zamówienie {po} — dostawa do {site}",
    "zh": "采购订单 {po} — {site}交货",
    "ko": "발주서 {po} — {site} 납품",
}


def draft(lines, *, reminder=False, address=None, subject=None):
    line = lines[0][0]
    supplier = line.supplier
    language = (supplier.get("language") or "en").strip().casefold()
    if language not in WORDING:
        language = "en"
    site = rules.cfg("localized_site_names").get(language, rules.cfg("site_name"))
    return {"channel": "email", "to_name": supplier.get("contact_name") or supplier.get("name") or line.key[0],
            "to_address": address or supplier["contact_email"], "language": language,
            "subject": subject or SUBJECTS[language].format(po=line.key[1], site=site),
            "body": _body(language, lines, reminder),
            "body_en": _body("en", lines, reminder) if language != "en" else None,
            "po_numbers": [line.key[1]]}


DATE_IN_QUOTE = re.compile(
    r"(?<!\d)(?:\d{4}-\d{2}-\d{2}|\d{1,2}\.\d{1,2}\.\d{4}|"
    r"\d{4}\s*[年년]\s*\d{1,2}\s*[月월]\s*\d{1,2}\s*[日일])(?!\d)"
)


def _quoted_date(token):
    if "-" in token:
        return day(token)
    first, second, third = (int(number) for number in re.findall(r"\d+", token))
    year, month, day_of_month = (third, second, first) if "." in token else (first, second, third)
    return day(f"{year:04d}-{month:02d}-{day_of_month:02d}")


# Auto-resolution requires a clear commitment to arrival at the configured site. Estimates,
# dispatches, split shipments and commitments conditional on anything go to review.
AFFIRMATIVE = re.compile(r"\b(?:we confirm|wir bestätigen|potwierdzamy)\b|我们确认|我方确认|확인합니다", re.I)
ARRIVAL = re.compile(r"\b(?:arriv\w*|deliver\w*|eintreff\w*|liefer\w*|dotr\w*|dostaw\w*)\b|送达|到达|납품|도착", re.I)
HEDGING = re.compile(
    r"\b(?:no|not|cannot|can't|won't|unable|if|estimate\w*|expect\w*|plan\w*|partial|split|balance|remaining|subject to|"
    r"nicht|voraussichtlich|geplant|sofern|falls|nie|szacuj\w*|planuj\w*|jeżeli|warunk\w*)\b"
    r"|预计|计划|无法|如果|部分|有条件|예정|예상|불가|조건|나머지", re.I,
)
LINE_REFERENCE = (
    r"\b(?:line|item|position|pozycj[ai])\s*#?\s*0*{number}\b|"
    r"第\s*0*{number}\s*行|(?<!\d)0*{number}\s*번\s*항목"
)
AMOUNT = re.compile(r"(?<![0-9A-Za-z.,])\d+(?:[,. \u00a0\u202f]\d+)*(?!\d)")


def interpret(reply, lines):
    body = reply["body"]
    claims = []
    if not HEDGING.search(body):
        for line in lines:
            for paragraph in re.split(r"\n\s*\n", body):
                claim = _confirmation(paragraph.strip(), reply, line, len(lines) > 1)
                if claim:
                    claims.append(claim)
    return {"lines": claims, "summary": (
        "Supplier made an explicit delivery confirmation." if claims
        else "The reply does not establish a complete arrival commitment for this PO line."
    )}


def _confirmation(quote, reply, line, multiple):
    site_names = [re.escape(alias) for alias in rules.cfg("site_aliases")]
    site_pattern = r"(?<![A-Za-z0-9])(?:" + "|".join(site_names) + r")(?![A-Za-z0-9])"
    if "?" in quote or "？" in quote or not AFFIRMATIVE.search(quote) or not re.search(site_pattern, quote, re.I):
        return None
    arrival = ARRIVAL.search(quote)
    dates = list(DATE_IN_QUOTE.finditer(quote))
    if not arrival or len(dates) != 1 or dates[0].start() < arrival.start():
        return None
    date = _quoted_date(dates[0].group())
    if not date:
        return None
    revision = None
    if line.revision:
        match = re.search(r"\b(?:revision|rev\.?|version)\s*([A-Za-z0-9-]+)\b|(?:版本|버전)\s*([A-Za-z0-9-]+)", quote, re.I)
        if match:
            revision = match.group(1) or match.group(2)
    claim = {"line": line.key[2], "intent": "confirmed", "qty": line.qty, "revision": revision,
             "arrival_date": date.isoformat(), "quote": quote}
    checked = verified_claim(claim, reply, line, multiple)
    return claim if checked and checked[0] is not None else None


def verified_claim(claim, reply, line, multiple):
    quote = (claim.get("quote") or "").strip()
    if not quote or quote not in reply["body"]:
        return None
    if multiple and not (
        re.search(LINE_REFERENCE.format(number=line.key[2]), quote, re.I)
        or line.name.casefold() in quote.casefold() or line.key[3].casefold() in quote.casefold()
    ):
        return None
    qty = quantity(claim.get("qty"))
    arrival = day(claim.get("arrival_date"))
    revision = (claim.get("revision") or "").strip().upper()
    # Strip ISO dates before checking quantity so a day of month cannot substantiate a quantity.
    amount_text = DATE_IN_QUOTE.sub("", quote)
    amount_text = re.sub(LINE_REFERENCE.format(number=line.key[2]), "", amount_text, flags=re.I)
    amount_text = re.sub(r"(?<!\w)" + re.escape(line.key[1]) + r"(?!\w)", "", amount_text)
    if revision:
        amount_text = re.sub(r"(?<!\w)" + re.escape(revision) + r"(?!\w)", "", amount_text, flags=re.I)
    amounts = {quantity(token) for token in AMOUNT.findall(amount_text)}
    if amounts != {qty}:
        qty = None
    if arrival and arrival not in {_quoted_date(token) for token in DATE_IN_QUOTE.findall(quote)}:
        arrival = None
    if revision and not re.search(r"(?<!\w)" + re.escape(revision) + r"(?!\w)", quote, re.I):
        revision = None
    return qty, arrival, revision, quote
