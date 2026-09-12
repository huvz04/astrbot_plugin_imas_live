"""Conservative parsers for the three documented official page shapes.

The parser never fills in an unknown year/time.  It emits review notes instead of
turning ambiguous text into a deadline, which is important for notifications.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime, timedelta
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from .models import CastAppearance, Evidence, ParsedPage, Performance, TicketRound

JST = "+09:00"
SPACE = re.compile(r"\s+")
DATE = re.compile(
    r"(?:(?P<year>20\d{2})[年./])?(?P<month>\d{1,2})[月./](?P<day>\d{1,2})日?"
    r"(?:\([^)]*\))?\s*(?P<hour>\d{1,2})[:：](?P<minute>\d{2})"
)
DAY_DATE = re.compile(r"(?:(?P<year>20\d{2})[年./])?(?P<month>\d{1,2})[月./](?P<day>\d{1,2})日?")
YEAR = re.compile(r"(20\d{2})[年./]")


def clean(value: str) -> str:
    return SPACE.sub(" ", unicodedata.normalize("NFKC", value)).strip()


def stable(*values: str) -> str:
    return hashlib.sha256("\x1f".join(values).encode()).hexdigest()[:24]


def japan_datetime(value: str, inherited_year: int | None = None) -> str | None:
    """Return an ISO JST timestamp only when a date and clock time are explicit."""
    match = DATE.search(clean(value))
    if not match:
        return None
    year = int(match.group("year") or inherited_year or 0)
    if not year:
        return None
    try:
        hour = int(match.group('hour'))
        minute = int(match.group('minute'))
        if hour == 24 and minute != 0:
            return None
        stamp = datetime(
            year, int(match.group("month")), int(match.group("day")),
            0 if hour == 24 else hour, minute)
        return (stamp + timedelta(days=hour == 24)).isoformat(timespec="minutes") + JST
    except ValueError:
        return None


def date_only(value: str, inherited_year: int | None = None) -> str | None:
    match = DAY_DATE.search(clean(value))
    if not match:
        return None
    year = int(match.group("year") or inherited_year or 0)
    if not year:
        return None
    try:
        return datetime(year, int(match.group('month')), int(match.group('day'))).date().isoformat()
    except ValueError:
        return None


def _pairs(node: Tag) -> dict[str, str]:
    """Read semantic dt/dd rows, including modern div-based c-dl rows."""
    result: dict[str, str] = {}
    for dt in node.select("dt"):
        dd = dt.find_next_sibling("dd")
        if dd:
            result[clean(dt.get_text(" "))] = clean(dd.get_text(" "))
    for row in node.select(".c-dl__row, .p-ticket-detail__row"):
        dt, dd = row.find("dt"), row.find("dd")
        if dt and dd:
            result[clean(dt.get_text(" "))] = clean(dd.get_text(" "))
    return result


def _links(node: Tag, base_url: str) -> list[str]:
    return [urljoin(base_url, a["href"]) for a in node.select("a[href]") if a["href"].startswith(("http", "/", "."))]


def _nearest_title(node: Tag) -> str:
    accordion = node.find_parent('dl', class_=lambda value: value in {'accordionList', 'accordion'})
    if accordion:
        heading = accordion.find("dt", recursive=False)
        if heading:
            return clean(heading.get_text(" "))
    details = node.find_parent("details")
    if details:
        heading = details.find("summary")
        if heading:
            return clean(heading.get_text(" "))
    if node.find_parent('div', class_='ticketCol'):
        heading = node.find_previous('h2')
        if heading:
            return clean(heading.get_text(' '))
    ancestor = node
    while ancestor:
        if ancestor.name in {"section", "details", "dl"}:
            title = ancestor.find(["h2", "h3", "h4", "summary", "dt"], recursive=False)
            if title:
                return clean(title.get_text(" "))
        ancestor = ancestor.parent if isinstance(ancestor.parent, Tag) else None
    previous = node.find_previous(["h2", "h3", "h4"])
    return clean(previous.get_text(" ")) if previous else "未命名受理"


def _scope(text: str) -> str:
    if any(key in text for key in ("配信", "視聴", "streaming")):
        return "streaming"
    if any(key in text for key in ("物販", "グッズ", "整理券", "CD・映像")):
        return "goods"
    return "onsite"


def _method(text: str) -> str:
    if "リセール" in text:
        return "resale"
    if "先着" in text or "一般販売" in text:
        return "first_come"
    if any(key in text for key in ("抽選", "先行")):
        return "lottery"
    return "unknown"


def _field(pairs: dict[str, str], *labels: str) -> str | None:
    for label, value in pairs.items():
        if any(term in label for term in labels):
            return value
    return None


def _range(value: str | None) -> tuple[str | None, str | None]:
    if not value:
        return None, None
    normalized = clean(value)
    first = japan_datetime(normalized)
    inherited = int(first[:4]) if first else None
    # The second half commonly omits its year; only inherit an explicit first year.
    parts = re.split(r"(?:~|〜|～|\u301c)", normalized, maxsplit=1)
    second = japan_datetime(parts[1], inherited) if len(parts) == 2 else None
    if first and second and second < first:
        # An omitted year may roll into January; other reversed ranges are unsafe.
        if not YEAR.search(parts[1]) and first[5:7] == '12' and second[5:7] == '01':
            second = japan_datetime(parts[1], inherited + 1)
        else:
            return first, None
    return first, second


def parse_venue(html: str) -> str | None:
    """Extract an event venue only from explicit official venue fields."""
    soup = BeautifulSoup(html, "html.parser")

    def value_of(node: Tag) -> str:
        return clean(re.sub(r"https?://\S+", "", node.get_text(" ")))

    for dt in soup.select("dt"):
        label = clean(dt.get_text(" "))
        if label not in {"開催場所", "会場", "場所"}:
            continue
        dd = dt.find_next_sibling("dd")
        if dd:
            value = value_of(dd)
            if value:
                return value
    for row in soup.select(".c-dl__row, .p-ticket-detail__row"):
        dt, dd = row.find("dt"), row.find("dd")
        if dt and dd and clean(dt.get_text(" ")) in {"開催場所", "会場"}:
            value = value_of(dd)
            if value:
                return value
    for heading in soup.find_all(["h1", "h2", "h3", "h4"]):
        if clean(heading.get_text(" ")) not in {"開催場所", "会場"}:
            continue
        detail = heading.find_next_sibling()
        if detail:
            value = value_of(detail)
            if value:
                return value
    return None


def parse_ticket_page(html: str, source_url: str) -> ParsedPage:
    """Extract ticket rows from Gakuen, SideM and Shiny's known layouts."""
    soup = BeautifulSoup(html, "html.parser")
    parsed = ParsedPage()
    seen: set[str] = set()
    # These are the field-owning units in the three documented page designs.
    # Do not traverse broad page sections: repeated dt labels would overwrite one another.
    containers = soup.select("section.p-ticket__group, dl.ticketList, dl.c-dl, .ticketCol > dl")
    # Small Gakuen sections and SideM accordions carry fields in one container.
    for node in containers:
        pairs = _pairs(node)
        period = _field(pairs, "受付期間", "申込期間", "販売期間")
        ticket_url = _field(pairs, "受付URL", "販売URL")
        links = _links(node, source_url)
        if not period and not ticket_url and not links:
            continue
        title = re.sub(r'20\d{2}[./]\d{1,2}[./]\d{1,2}\s*Update!?|受付(?:は)?終了しました|受付終了|終了しました', '', _nearest_title(node), flags=re.I).strip()
        whole = clean(node.get_text(" "))
        if "中止" in title or "取消" in title:
            parsed.review_notes.append(f"已发现取消/中止标题，未生成提醒：{title}")
            continue
        scope = _scope(title)
        # Ignore generic pages / other semantic sections with unrelated outgoing links.
        if not period and not ticket_url:
            continue
        start, end = _range(period)
        result = japan_datetime(_field(pairs, "当落発表", "結果発表") or "")
        pay_start, pay_end = _range(_field(pairs, "入金期間", "支払期間"))
        url = next((item for item in links if "ticket" in item or "asobi" in item or "eplus" in item or "l-tike" in item), None)
        evidence = Evidence(source_url, whole[:400], "ticket-html")
        # Dates and update badges must never change a reception's identity.
        key = stable(url) if url else stable(source_url, title, _field(pairs, "対象席種", "券種") or '')
        if key in seen:
            continue
        seen.add(key)
        if period and (start is None or end is None):
            parsed.review_notes.append(f"票务期限无法完整解析：{title}｜{period}")
        parsed.ticket_rounds.append(TicketRound(
            stable_key=key, name=title, ticket_scope=scope,
            sale_method=_method(title + " " + whole[:300]),
            application_start=start, application_end=end, result_at=result,
            payment_start=pay_start, payment_end=pay_end, url=url,
            seats=_field(pairs, "対象席種", "券種", "チケット料金"),
            eligibility=_field(pairs, "対象者", "対象会員", "資格"), evidence=evidence,
        ))
    if not parsed.ticket_rounds:
        parsed.review_notes.append("未找到可验证的票务字段；页面已保留为待核验来源。")
    return parsed


def schedule_performances(text: str, source_url: str, venue: str | None = None,
                          directory: bool = False) -> list[Performance]:
    """Read explicit dates only; continuous ranges are never expanded into sessions."""
    normalized = unicodedata.normalize('NFKC', text or '')
    if re.search(r'\d日?\s*(?:\([^)]*\))?\s*[~〜～－–]', normalized):
        return []
    # Resolve same-month enumerations, e.g. 9月12日(土)・13日(日).
    last_year = last_month = None
    dated = re.compile(r'(?:(20\d{2})[年./])?(?:(\d{1,2})[月./])?(\d{1,2})日(?:\([^)]*\))?|(?:(20\d{2})[./])(\d{1,2})[./](\d{1,2})')
    found = []
    for match in dated.finditer(normalized):
        year, month, day, y2, m2, d2 = match.groups()
        if not (month or m2) and (match.start() == 0 or not re.search(r'[・、,]\s*$', normalized[:match.start()])):
            continue
        last_year = int(year or y2 or last_year or 0)
        last_month = int(month or m2 or last_month or 0)
        try:
            day_value = datetime(last_year, last_month, int(day or d2)).date().isoformat()
        except (ValueError, TypeError):
            continue
        found.append((match, day_value))
    rows = []
    seen_sessions: set[str] = set()
    for index, (match, day_value) in enumerate(found):
        tail = normalized[match.end():found[index+1][0].start() if index+1 < len(found) else len(normalized)]
        clocks = re.findall(r'(?:開演\s*[:：]?\s*(\d{1,2}:\d{2})|(\d{1,2}:\d{2})\s*開演)', tail)
        sessions = [a or b for a, b in clocks] or ['']
        for clock in sessions:
            label = f'开演 {clock} JST' if clock else '演出日（场次待细分）'
            # The time, rather than an ordinal local to one date match, is the
            # stable session identity.  An official page can repeat the same
            # date in separate blocks (PC/SP markup) and can announce an
            # afternoon and evening performance on that date.
            key = stable(source_url, day_value, clock or 'time-unpublished')
            if key in seen_sessions:
                continue
            seen_sessions.add(key)
            rows.append(Performance(key, day_value, label, venue,
                                    status='directory' if directory else 'announced',
                                    evidence=Evidence(source_url, clean(match.group() + tail)[:300], 'schedule-html')))
    return rows


def parse_information(html: str, source_url: str) -> list[Performance]:
    soup = BeautifulSoup(html, 'html.parser')
    labels = {'公演日時', '開催日時', '日程', '公演日程', '日時', '開催日'}
    for node in soup.find_all(['dt', 'h2', 'h3', 'h4']):
        label = re.sub(r'20\d{2}[./]\d{1,2}[./]\d{1,2}\s*Update', '', clean(node.get_text(' '))).strip()
        if label not in labels:
            continue
        blocks = []
        if node.name == 'dt':
            block = node.find_next_sibling('dd')
            if block:
                blocks.append(block)
        else:
            for block in node.find_next_siblings():
                if block.name == 'dt' or (block.name in {'h1', 'h2', 'h3', 'h4'} and int(block.name[1]) <= int(node.name[1])):
                    break
                blocks.append(block)
        if blocks:
            rows = schedule_performances('\n'.join(block.get_text('\n', strip=True) for block in blocks), source_url, parse_venue(html))
            if rows:
                return rows
    return []


def parse_shiny_information(html: str, source_url: str) -> ParsedPage:
    """Parse Master ShowPiece's two date-specific announced cast lists."""
    soup = BeautifulSoup(html, "html.parser")
    parsed = ParsedPage()
    schedule = next((dd for dt in soup.find_all("dt") if clean(dt.get_text(" ")).startswith("日程") if (dd := dt.find_next_sibling("dd"))), None)
    year_match = YEAR.search(clean(schedule.get_text(" ")) if schedule else "")
    event_year = int(year_match.group(1)) if year_match else None
    current_date: str | None = None
    performance_key: str | None = None
    seen_performances: set[str] = set()
    for heading in soup.find_all(["h2", "h3", "h4"]):
        text = clean(heading.get_text(" "))
        image = heading.find("img", alt=True)
        text = clean(f"{text} {image['alt'] if image else ''}")
        candidate = date_only(text, event_year)
        if candidate and ("DAY" in text.upper() or "CAST" in text.upper()):
            current_date = candidate
            performance_key = stable(source_url, candidate, text)
            if performance_key not in seen_performances:
                seen_performances.add(performance_key)
                parsed.performances.append(Performance(
                    performance_key, candidate, text, None,
                    evidence=Evidence(source_url, text, "shiny-cast-html"),
                ))
        if not current_date:
            continue
        block = heading.find_next_sibling("div")
        if block and "p-information__cast" not in (block.get("class") or []):
            block = None
        if not block:
            continue
        for caption in block.select("figcaption.p-information__castText"):
            for raw_line in caption.get_text("\n").splitlines():
                line = clean(raw_line)
                match = re.match(r"(.+?)\s*[（(]([^()（）]+)[)）]$", line)
                if not match:
                    continue
                parsed.cast.append(CastAppearance(
                    clean(match.group(1)), clean(match.group(2)), performance_key,
                    evidence=Evidence(source_url, line, "shiny-cast-html"),
                ))
    if not parsed.performances:
        parsed.review_notes.append("闪彩页面未定位到带年份的日程与 CAST 区块；未猜测演出年份。")
    return parsed


def parse_day_cast(html: str, source_url: str) -> ParsedPage:
    """Read explicitly labelled DAY cast text without inferring CV appearances.

    This deliberately accepts only text in a DAY/CAST section.  Decorative title
    images and character-profile images are not a cast roster.
    """
    soup = BeautifulSoup(html, "html.parser")
    parsed = ParsedPage()
    year_match = YEAR.search(clean(soup.get_text(" ")))
    year = int(year_match.group(1)) if year_match else None
    seen_performances: set[str] = set()
    for heading in soup.find_all(["h2", "h3", "h4"]):
        heading_text = clean(heading.get_text(" "))
        if "DAY" not in heading_text.upper() or "CAST" not in heading_text.upper():
            continue
        day = date_only(heading_text, year)
        if not day:
            continue
        key = stable(source_url, day, heading_text)
        if key not in seen_performances:
            seen_performances.add(key)
            parsed.performances.append(Performance(key, day, heading_text, None,
                evidence=Evidence(source_url, heading_text, "day-cast-html")))
        chunks: list[str] = []
        for sibling in heading.find_next_siblings():
            if sibling.name in {"h2", "h3", "h4"}:
                break
            chunks.extend(clean(line) for line in sibling.get_text("\n").splitlines() if clean(line))
        for line in dict.fromkeys(chunks):
            # Official pages commonly write 団体名 / 声优名（角色名）.  Preserve
            # the entire left part as the published performer/group label.
            match = re.match(r"(.{2,80}?)\s*[（(]([^()（）]{1,80})[)）]$", line)
            if not match or "http" in line:
                continue
            name, role = clean(match.group(1)), clean(match.group(2))
            if name and role:
                parsed.cast.append(CastAppearance(name, role, key,
                    evidence=Evidence(source_url, line, "day-cast-html")))
    if not parsed.cast:
        parsed.review_notes.append("未找到分日官方文字出演名单；未把装饰图或角色资料当作出演。")
    return parsed


def official_roster_image_urls(html: str, source_url: str) -> list[str]:
    """Return only complete, semantically-labelled official roster artwork.

    The known Million 14th page places exactly bnr_day1.webp/bnr_day2.webp
    below its 出演者 heading.  Title banners such as bnr_day1_title01.webp are
    intentionally excluded, rather than guessed to be a cast image.
    """
    soup = BeautifulSoup(html, "html.parser")
    heading = next((node for node in soup.find_all(["h2", "h3"]) if "出演者" in clean(node.get_text(" "))), None)
    if not heading:
        return []
    container = heading.parent
    values = []
    for image in container.select("img[src]") if container else []:
        src = image.get("src", "")
        if re.search(r"/bnr_day\d+\.webp(?:[?#].*)?$", src):
            values.append(urljoin(source_url, src))
    return list(dict.fromkeys(values))
