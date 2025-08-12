# main.py
# -*- coding: utf-8 -*-
import os
import re
import time
import logging
from logging.handlers import RotatingFileHandler
import threading
from dataclasses import dataclass
from typing import List
import subprocess
import shutil
from pathlib import Path

# ── .env загружаем из той же папки, где лежит main.py ──
from dotenv import load_dotenv
load_dotenv(dotenv_path=Path(__file__).with_name(".env"))

# ── Тишина в консоли: весь мусор в лог ──
try:
    import sys
    sys.stdout = open(os.devnull, "w", buffering=1)
    sys.stderr = open(os.devnull, "w", buffering=1)
except Exception:
    pass

from bs4 import BeautifulSoup

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager

from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton, ParseMode, BotCommand
)
from telegram.ext import (
    Updater, CallbackContext, CommandHandler, MessageHandler, Filters,
    CallbackQueryHandler, ConversationHandler
)

# ── Настройки ──
BOT_TOKEN = os.getenv("BOT_TOKEN")
LOGIN_URL = "https://mlkm.netbynet.ru/loginTemp"

HARD_WAIT_AFTER_LOGIN_SEC = int(os.getenv("HARD_WAIT", "12"))
HEADLESS = os.getenv("HEADLESS", "1") not in ("0", "false", "False")
SELENIUM_TIMEOUT = 30

LOG_FILE = os.getenv("LOG_FILE", "bot.log")
LOG_MAX_BYTES = int(os.getenv("LOG_MAX_BYTES", str(5 * 1024 * 1024)))
LOG_BACKUPS = int(os.getenv("LOG_BACKUPS", "2"))

# ── Логирование ──
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    handlers=[RotatingFileHandler(LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS)],
    force=True,
)
log = logging.getLogger("mlkm-bot")
for noisy in ["apscheduler", "urllib3", "WDM", "selenium", "telegram"]:
    logging.getLogger(noisy).setLevel(logging.WARNING)

# ── Диалоговые состояния ──
OPERATOR, LOGIN, PASS = range(3)

# ── Модели ──
@dataclass
class ServiceRow:
    product: str
    tariff: str

@dataclass
class MainPageData:
    request_number: str = "—"
    account_number: str = "—"
    address: str = "—"
    temp_password: str = "—"
    services: List[ServiceRow] = None

@dataclass
class ClientData:
    abonent_number: str = "—"
    contact_mobile: str = "—"
    client_mobile: str = "—"
    lastname: str = "—"
    firstname: str = "—"
    middlename: str = "—"

@dataclass
class PppoeData:
    login: str = "—"
    password: str = "—"

@dataclass
class Collected:
    main: MainPageData
    client: ClientData
    pppoe: PppoeData

# ── Selenium helpers ──
def _find_chrome_binary() -> str:
    """Ищем установленный Chrome/Chromium (Ubuntu, snap и т.п.). Можно задать CHROME_BIN в .env."""
    env_bin = os.getenv("CHROME_BIN")
    if env_bin and os.path.exists(env_bin):
        return env_bin
    candidates = [
        shutil.which("google-chrome"),
        shutil.which("google-chrome-stable"),
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/snap/bin/chromium",
        "/opt/google/chrome/chrome",
    ]
    for p in candidates:
        if p and os.path.exists(p):
            return p
    return ""

def build_driver(headless: bool = True) -> webdriver.Chrome:
    chrome_options = Options()
    if headless:
        chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.add_argument("--lang=ru-RU")
    chrome_options.add_argument("--log-level=3")
    chrome_options.add_argument("--disable-logging")
    chrome_options.add_experimental_option("excludeSwitches", ["enable-automation", "enable-logging"])
    chrome_options.add_experimental_option("useAutomationExtension", False)

    bin_path = _find_chrome_binary()
    if bin_path:
        chrome_options.binary_location = bin_path
    else:
        log.warning("Chrome/Chromium не найден. Установите браузер (google-chrome-stable или chromium).")

    try:
        service = Service(ChromeDriverManager().install(), log_output=subprocess.DEVNULL)
    except TypeError:
        service = Service(ChromeDriverManager().install())

    driver = webdriver.Chrome(service=service, options=chrome_options)
    driver.set_page_load_timeout(60)
    return driver

def _wait(driver, timeout=SELENIUM_TIMEOUT):
    return WebDriverWait(driver, timeout)

def _safe_text(el) -> str:
    if not el:
        return "—"
    try:
        t = (el.text or "").strip()
        if t:
            return re.sub(r"\s+", " ", t)
        v = el.get_attribute("value")
        return (v or "—").strip()
    except Exception:
        return "—"

BAD_SINGLE_TOKENS = {
    "Обновить", "Распечатать заявку", "Последние заявки клиента",
    "Показать удаленные подключения", "Скрыть удаленные подключения"
}

def _clean(v: str) -> str:
    v = re.sub(r"\s+", " ", (v or "")).strip(" \u200b\t\r\n:;–—")
    for t in BAD_SINGLE_TOKENS:
        v = re.sub(rf"\b{re.escape(t)}\b", "", v, flags=re.I)
    v = re.sub(r"\s{2,}", " ", v).strip(" \u200b\t\r\n:;–—")
    return v or "—"

def _strip_label(label: str, value: str) -> str:
    if not value:
        return "—"
    lab = label.strip().replace("ё","е").replace("Ё","Е")
    val = value.strip().replace("ё","е").replace("Ё","Е")
    val = re.sub(rf"(?i)^{re.escape(lab)}\s*[:\-–—]?\s*", "", val).strip()
    return val or value

def _save_dump(filename: str, html: str):
    try:
        with open(filename, "w", encoding="utf-8") as f:
            f.write(html)
    except Exception:
        pass

def get_active_tab_panel(driver):
    try:
        return driver.find_element(By.XPATH, "//*[contains(@class,'tab-pane') and contains(@class,'active')]")
    except Exception:
        return driver.find_element(By.TAG_NAME, "body")

def click_tab(driver, title):
    _wait(driver).until(EC.element_to_be_clickable(
        (By.XPATH, f"//*[self::a or self::button][contains(normalize-space(.), '{title}')]")
    )).click()
    time.sleep(1.0)
    _wait(driver).until(lambda d: get_active_tab_panel(d))

# ── Главная: берём значение из той же строки таблицы ──
def _main_field(driver, label: str) -> str:
    xp1 = f"//tr[./td[normalize-space()='{label}']]/td[2]"
    xp2 = f"//td[normalize-space()='{label}']/following-sibling::td[1]"
    for xp in (xp1, xp2):
        try:
            td = driver.find_element(By.XPATH, xp)
            val = _clean(td.get_attribute("textContent") or td.text)
            if val and val != "—":
                return _strip_label(label, val)
        except Exception:
            pass
    # Не таблица: соседний div/span
    xp3 = f"//*[self::div or self::span][normalize-space()='{label}']/following-sibling::*[1]"
    try:
        sib = driver.find_element(By.XPATH, xp3)
        val = _clean(sib.get_attribute("textContent") or sib.text)
        if val and val != "—":
            return _strip_label(label, val)
    except Exception:
        pass
    # План Б: bs4
    try:
        soup = BeautifulSoup(driver.page_source, "html.parser")
        nodes = soup.find_all(string=lambda s: s and s.strip() == label)
        for n in nodes:
            p = n.find_parent()
            if not p:
                continue
            sib = p.find_next_sibling()
            if sib:
                v = _clean(sib.get_text(" ", strip=True))
                if v and v != "—":
                    return _strip_label(label, v)
    except Exception:
        pass
    return "—"

# ── Вспомогательные для вкладок ──
def _closest_group(node):
    for xp in ["ancestor::tr[1]",
               "ancestor::*[contains(@class,'form-group')][1]",
               "ancestor::*[contains(@class,'row')][1]",
               "parent::*"]:
        try:
            g = node.find_element(By.XPATH, xp)
            if g:
                return g
        except Exception:
            continue
    return None

def _value_from_same_row(group, label_node):
    try:
        tr = group if group.tag_name.lower() == "tr" else label_node.find_element(By.XPATH, "ancestor::tr[1]")
        cells = tr.find_elements(By.XPATH, "./*")
        label_idx = None
        for i, c in enumerate(cells):
            try:
                if label_node == c or label_node in c.find_elements(By.XPATH, ".//*"):
                    label_idx = i
                    break
            except Exception:
                pass
        if label_idx is not None:
            for c in cells[label_idx+1:]:
                txt = _clean(c.get_attribute("textContent") or c.text)
                if txt and txt != "—" and txt not in BAD_SINGLE_TOKENS:
                    return txt
    except Exception:
        pass
    return "—"

def _value_in_panel(panel, label: str) -> str:
    """
    Устойчиво вытаскивает значение по лейблу внутри активной вкладки:
    - label starts-with (учёт ё/е)
    - label[for] → control
    - input/select/textarea в том же блоке
    - значение из соседнего td (табличный случай)
    - план Б: bs4 в пределах группы
    """
    if panel is None:
        return "—"
    norm = label.replace("ё","е").replace("Ё","Е")
    lbl_xp = (
        f".//*[self::label or self::td or self::th or self::div or self::span]"
        f"[starts-with(translate(normalize-space(), 'Ёё','Ее'), '{norm}')]"
    )
    try:
        lbl = panel.find_element(By.XPATH, lbl_xp)
    except Exception:
        return "—"

    # 1) label[for]
    try:
        for_attr = lbl.get_attribute("for")
        if for_attr:
            ctrl = panel.find_element(By.ID, for_attr)
            if ctrl.tag_name.lower() == "select":
                try:
                    return _clean(Select(ctrl).first_selected_option.text)
                except Exception:
                    pass
            return _clean(ctrl.get_attribute("value") or ctrl.text)
    except Exception:
        pass

    group = _closest_group(lbl) or panel

    # 2) input/select/textarea рядом
    for xp in [
        ".//following-sibling::*[self::input or self::textarea or self::select][1]",
        ".//following::*[self::input or self::textarea or self::select][1]",
        ".//input | .//textarea | .//select",
    ]:
        try:
            ctrl = lbl.find_element(By.XPATH, xp)
            if ctrl.tag_name.lower() == "select":
                try:
                    return _clean(Select(ctrl).first_selected_option.text)
                except Exception:
                    pass
            return _clean(ctrl.get_attribute("value") or ctrl.text)
        except Exception:
            pass

    # 3) Табличная строка
    val = _value_from_same_row(group, lbl)
    if val != "—":
        return val

    # 4) bs4
    try:
        html = (group if group != panel else panel).get_attribute("innerHTML") or ""
        soup = BeautifulSoup(html, "html.parser")
        nodes = soup.find_all(string=lambda s: s and s.strip().lower().replace("ё","е").startswith(norm.lower()))
        for n in nodes:
            sib = n.find_parent()
            while sib:
                sib = sib.find_next_sibling()
                if not sib:
                    break
                v = _clean(sib.get_text(" ", strip=True))
                if v and v != "—":
                    return v
    except Exception:
        pass
    return "—"

# ── Услуги ──
def table_services(driver) -> List[ServiceRow]:
    out: List[ServiceRow] = []
    try:
        table = _wait(driver).until(EC.presence_of_element_located(
            (By.XPATH, "//table[.//th[contains(., 'Продукт')] and .//th[contains(., 'Тариф')]]")
        ))
        headers = table.find_elements(By.XPATH, ".//th")
        idx = {h.text.strip(): i for i, h in enumerate(headers)}
        def col(needle):
            for k,i in idx.items():
                if needle.lower() in k.lower(): return i
        pi, ti = col("Продукт"), col("Тариф")
        for r in table.find_elements(By.XPATH, ".//tbody/tr[td]"):
            tds = r.find_elements(By.XPATH, "./td")
            prod = (tds[pi].text.strip() if pi is not None and pi < len(tds) else "")
            tarf = (tds[ti].text.strip() if ti is not None and ti < len(tds) else "")
            if not prod or "итого" in (tarf or "").lower(): continue
            out.append(ServiceRow(prod or "—", tarf or "—"))
    except Exception as e:
        log.warning("Не удалось распарсить таблицу услуг: %s", e)
    return out

# ── PPPoE ──
def _pppoe_read(panel) -> PppoeData:
    p = PppoeData()
    try:
        inp = panel.find_element(By.XPATH, ".//*[contains(normalize-space(.), 'Логин PPPoE')]/following::input[1]")
        p.login = _clean(inp.get_attribute("value") or inp.text)
    except Exception:
        try:
            sel = panel.find_element(By.XPATH, ".//*[contains(normalize-space(.), 'Логин PPPoE')]/following::select[1]")
            try:
                p.login = _clean(Select(sel).first_selected_option.text)
            except Exception:
                p.login = _clean(sel.get_attribute("value") or sel.text)
        except Exception:
            pass
    try:
        inp = panel.find_element(By.XPATH, ".//*[contains(normalize-space(.), 'Пароль PPPoE')]/following::input[1]")
        p.password = _clean(inp.get_attribute("value") or inp.text)
    except Exception:
        pass
    return p

# ── Основной сбор ──
def collect_megafon(login: str, password: str) -> Collected:
    driver = build_driver(HEADLESS)
    try:
        driver.get(LOGIN_URL)

        login_input = _wait(driver).until(EC.presence_of_element_located(
            (By.XPATH, "//label[contains(., 'Логин')]/following::input[1]")))
        pass_input = _wait(driver).until(EC.presence_of_element_located(
            (By.XPATH, "//label[contains(., 'Пароль')]/following::input[1]")))
        login_input.clear(); login_input.send_keys(login)
        pass_input.clear(); pass_input.send_keys(password)

        _wait(driver).until(EC.element_to_be_clickable((By.XPATH, "//button[contains(., 'Войти')]"))).click()

        time.sleep(HARD_WAIT_AFTER_LOGIN_SEC)
        _wait(driver).until(EC.presence_of_element_located((By.XPATH, "//*[contains(., 'Детализация заявки')]")))

        # —— Главная ——
        main = MainPageData()
        main.request_number = _main_field(driver, "Номер заявки")
        main.account_number = _main_field(driver, "Лицевой счет")
        main.address = _main_field(driver, "Адрес подключения")
        main.temp_password = _main_field(driver, "Временный пароль")
        # маски
        m = re.search(r"\b(?:Req\d{6,}|\d{6,})\b", main.request_number or "")
        if m: main.request_number = m.group(0)
        m = re.search(r"\b\d{4,}\b", main.account_number or "")
        if m: main.account_number = m.group(0)
        if main.temp_password and main.temp_password != "—":
            m = re.search(r"[A-Za-z0-9]{4,64}", main.temp_password)
            if m: main.temp_password = m.group(0)

        main.services = table_services(driver)

        # —— Данные клиента ——
        click_tab(driver, "Данные клиента")
        _wait(driver).until(EC.presence_of_element_located((By.XPATH, "//*[contains(., 'Абонентский номер')]")))
        panel = get_active_tab_panel(driver)

        client = ClientData()
        client.abonent_number = _value_in_panel(panel, "Абонентский номер")
        client.contact_mobile = _value_in_panel(panel, "Контактный мобильный телефон")
        client.client_mobile = _value_in_panel(panel, "Мобильный телефон клиента")
        client.lastname = _value_in_panel(panel, "Фамилия")
        client.firstname = _value_in_panel(panel, "Имя")
        client.middlename = _value_in_panel(panel, "Отчество")

        # —— PPPoE ——
        click_tab(driver, "Настройки и активация услуг")
        _wait(driver, 40).until(EC.presence_of_element_located((By.XPATH, "//*[contains(., 'Логин PPPoE')]")))
        ppp_panel = get_active_tab_panel(driver)
        p = _pppoe_read(ppp_panel)

        return Collected(main=main, client=client, pppoe=p)

    finally:
        try:
            driver.quit()
        except Exception:
            pass

# ── Форматирование ──
def format_collected(data: Collected) -> str:
    services_lines = []
    if data.main.services:
        for i, s in enumerate(data.main.services, 1):
            services_lines.append(f"{i}) Продукт — {s.product}; Тарифный план — {s.tariff}")
    else:
        services_lines.append("—")
    text = (
        "📌 <b>Детализация заявки</b>\n"
        f"• Номер заявки: <b>{data.main.request_number}</b>\n"
        f"• Лицевой счёт: <b>{data.main.account_number}</b>\n"
        f"• Адрес подключения: <b>{data.main.address}</b>\n"
        f"• Временный пароль: <b>{data.main.temp_password}</b>\n"
        f"• Услуги:\n" + "\n".join(services_lines) + "\n\n"
        "👤 <b>Данные клиента</b>\n"
        f"• Абонентский номер: <b>{data.client.abonent_number}</b>\n"
        f"• Контактный мобильный телефон: <b>{data.client.contact_mobile}</b>\n"
        f"• Мобильный телефон клиента: <b>{data.client.client_mobile}</b>\n"
        f"• Фамилия: <b>{data.client.lastname}</b>\n"
        f"• Имя: <b>{data.client.firstname}</b>\n"
        f"• Отчество: <b>{data.client.middlename}</b>\n\n"
        "🌐 <b>PPPoE</b>\n"
        f"• Логин PPPoE: <b>{data.pppoe.login}</b>\n"
        f"• Пароль PPPoE: <b>{data.pppoe.password}</b>"
    )
    return text

# ── Telegram ──
def start(update: Update, context: CallbackContext) -> int:
    user = update.effective_user
    name = user.first_name or user.username or "друг"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🟢 Мегафон", callback_data="op_megafon"),
         InlineKeyboardButton("🔴 МТС", callback_data="op_mts")],
        [InlineKeyboardButton("🆘 Помощь /help", callback_data="op_help")]
    ])
    (update.message or update.callback_query.message).reply_text(
        f"Привет👋, {name}, какой оператор тебе нужен?",
        reply_markup=kb
    )
    return OPERATOR

def help_cmd(update: Update, context: CallbackContext):
    (update.message or update.callback_query.message).reply_text(
        "Подсказки:\n"
        "1️⃣ Нажмите на нужного оператора.\n"
        "2️⃣ Введите логин и пароль, когда бот попросит.\n"
        "3️⃣ Подождите ~10–15 секунд — бот соберёт данные и пришлёт ответ.\n"
        "Команды: /start — начать заново, /cancel — отмена."
    )

def cancel(update: Update, context: CallbackContext) -> int:
    (update.message or update.callback_query.message).reply_text("Ок, отменил. Наберите /start, чтобы начать заново.")
    return ConversationHandler.END

def operator_choice(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    query.answer()
    data = query.data
    if data == "op_help":
        help_cmd(update, context)
        return ConversationHandler.END
    if data == "op_mts":
        query.edit_message_text("Профиль МТС пока в разработке.")
        return ConversationHandler.END
    context.user_data["operator"] = "megafon"
    query.edit_message_text("Введите логин для входа.")
    return LOGIN

def get_login(update: Update, context: CallbackContext) -> int:
    context.user_data["login"] = update.message.text.strip()
    update.message.reply_text("Теперь введите временный пароль.")
    return PASS

def scrape_worker(chat_id: int, login: str, pwd: str, context: CallbackContext):
    try:
        context.bot.send_message(chat_id, "Принято. Захожу в систему… Подождите некоторое время... ⏳")
        data = collect_megafon(login, pwd)
        text = format_collected(data)
        context.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    except Exception as e:
        log.exception("Ошибка при сборе данных: %s", e)
        context.bot.send_message(
            chat_id,
            "Не удалось собрать данные. Возможные причины: сайт недоступен или неверные логин/пароль. Попробуйте ещё раз (/start)."
        )

def get_pass_and_run(update: Update, context: CallbackContext) -> int:
    pwd = update.message.text.strip()
    login = context.user_data.get("login")
    if not login:
        update.message.reply_text("Не вижу логина. Давайте заново: /start")
        return ConversationHandler.END
    t = threading.Thread(target=scrape_worker, args=(update.effective_chat.id, login, pwd, context), daemon=True)
    t.start()
    return ConversationHandler.END

def main():
    if not BOT_TOKEN:
        log.error("BOT_TOKEN не найден. Проверьте файл .env рядом с main.py")
        raise SystemExit(1)

    updater = Updater(BOT_TOKEN, use_context=True)
    dp = updater.dispatcher

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            OPERATOR: [CallbackQueryHandler(operator_choice)],
            LOGIN: [MessageHandler(Filters.text & ~Filters.command, get_login)],
            PASS: [MessageHandler(Filters.text & ~Filters.command, get_pass_and_run)],
        },
        fallbacks=[CommandHandler("cancel", cancel), CommandHandler("help", help_cmd)],
        conversation_timeout=300
    )
    dp.add_handler(conv)
    dp.add_handler(CommandHandler("help", help_cmd))
    dp.add_handler(CommandHandler("cancel", cancel))

    # Меню команд для кнопки "Menu"
    updater.bot.set_my_commands([
        BotCommand("start", "Начать заново / выбор оператора"),
        BotCommand("help",  "Подсказки по работе с ботом"),
        BotCommand("cancel","Отменить текущий шаг"),
    ])

    log.info("Бот запущен.")
    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    main()
