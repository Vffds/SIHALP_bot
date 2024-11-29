import requests
import re
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    filters,
    ContextTypes,
)
from bs4 import BeautifulSoup
from datetime import datetime
import unidecode
import configparser

# Чтение конфигурации
config = configparser.ConfigParser()
config.read("auth.cfg")
TOKEN_TELEGRAM = config.get("TOKENS", "TOKEN_TELEGRAM")
TOKEN_24TV = config.get("TOKENS", "TOKEN_24TV")

# Заголовки для авторизации
AUTH_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:132.0) Gecko/20100101 Firefox/132.0",
    "Content-Type": "application/x-www-form-urlencoded",
}
FS_DHCP_URL = "http://ip.iformula.ru/mac.php"


# Поиск MAC-адресов в тексте
def find_mac(text):
    text = unidecode.unidecode(text)
    mac_regex = r'(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}'
    matches = re.findall(mac_regex, text)
    return matches


# Нормализация MAC-адреса
def normalize_mac(mac):
    mac = mac.lower().replace(":", "").replace("-", "")
    mac = mac.replace("o", "0")
    return ":".join(mac[i:i + 2] for i in range(0, len(mac), 2))


# Инкремент MAC-адреса
def increment_mac(mac):
    mac_clean = mac.replace(":", "").replace("-", "")
    mac_int = int(mac_clean, 16) + 1
    mac_new = f"{mac_int:012X}"
    return ":".join(mac_new[i:i + 2] for i in range(0, 12, 2))


# Парсинг данных IP из HTML
def parse_ip_from_html(html):
    soup = BeautifulSoup(html, 'html.parser')
    ip_links = soup.find_all('a')
    latest_ip = None
    latest_time = None
    switch_ip = None
    port = None

    for link in ip_links:
        text = link.text.strip()

        if " - " in text:
            ip, time_str = text.split(" - ", 1)
            try:
                time_obj = datetime.strptime(time_str.strip(), "%Y-%m-%d %H:%M:%S")
                if latest_time is None or time_obj > latest_time:
                    latest_ip = ip.strip()
                    latest_time = time_obj
                    switch_port_matches = re.findall(r'\[([^]]+)]', link.find_parent().text)
                    if switch_port_matches:
                        for match in switch_port_matches:
                            switch_ip, port = match.split(":")
            except ValueError:
                continue

    return latest_ip, latest_time, switch_ip, port


# Получение информации по MAC-адресу
def get_ip_from_mac(mac):
    try:
        response = requests.post(
            FS_DHCP_URL,
            headers=AUTH_HEADERS,
            data={"mac": mac}
        )
        if response.status_code == 200:
            return parse_ip_from_html(response.text)
        else:
            return None, None, None, None
    except Exception as e:
        print("Ошибка при запросе:", e)
        return None, None, None, None


# Получение информации о приставке с 24ТВ
def get_info_from_24tv(mac):
    url = f"https://zt.platform24.tv/v2/devices?format=json&token={TOKEN_24TV}&interface_mac={mac}"
    response = requests.get(url)

    login_at = ''

    if response.status_code == 200:
        data = response.json()

        if data:
            try:
                data.sort(key=lambda x: datetime.strptime(x.get('login_at', '1900-01-01T00:00:00.000000Z'),
                                                          "%Y-%m-%dT%H:%M:%S.%fZ"), reverse=True)

                latest_device = data[0]

                provider_uid = latest_device.get('user', {}).get('provider_uid', 'Не найдено')
                login_at_raw = latest_device.get('login_at', 'Не найдено')

                if login_at_raw != "Не найдено":
                    try:
                        parsed_date = datetime.strptime(login_at_raw, "%Y-%m-%dT%H:%M:%S.%fZ")
                        login_at = parsed_date.strftime("%d.%m.%Y %H:%M")
                    except ValueError:
                        login_at = "Неверный формат даты"
            except Exception as e:
                print(f"Ошибка при обработке данных: {e}")
                provider_uid, login_at = "Ошибка", "Ошибка"
        else:
            provider_uid, login_at = "Не найдено", "Не найдено"
    else:
        print(f"Ошибка запроса 24ТВ: {response.status_code}")
        provider_uid, login_at = "Ошибка", "Ошибка"

    return provider_uid, login_at


# Формирование строки с информацией о роутере
async def build_ip_response(ip, time, switch_ip, port, mac, is_incremented, only_ip=False):
    response = ''
    if ip:
        mac_display = mac if not is_incremented else mac.lower()  # Если MAC инкрементирован, используем оригинальный
        response += f"Роутер"
        if not only_ip:  # Если нужно выводить MAC, если он не дублируется
            response += f" {mac_display}"
        response += f" получал IP <pre>{ip}</pre>{time.strftime('%Y-%m-%d %H:%M:%S')} co свитча: <pre>{switch_ip}</pre> и порта: {port}\n"
    return response

# Формирование информации о приставке для 24ТВ
async def build_provider_info(provider_uid, login_at, mac, is_incremented, only_ip=False):
    response = ''
    if login_at and login_at != "Не найдено":
        response += "Приставка"
        if not only_ip:  # Если нужно выводить MAC, если он не дублируется
            mac_display = mac.lower() if not is_incremented else mac.lower()
            response += f" {mac_display}"
        response += f" последний раз подключалась к 24ТВ\n{login_at} под учёткой "
        response += f'<a href="https://fs.groupw.ru/user_selected?id_user={provider_uid}">{provider_uid}</a>.\n'
    return response

# Обработчик сообщений
async def handle_mac_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    mac_addresses = find_mac(text)

    response = ''
    only_ip = len(mac_addresses) == 1  # Если найдено только одно устройство, не показываем MAC

    for idx, mac in enumerate(mac_addresses):
        normalized_mac = normalize_mac(mac)

        # Получаем данные для оригинального и инкрементированного MAC-адреса
        original_ip, original_time, switch_ip, port = get_ip_from_mac(normalized_mac)
        incremented_mac = increment_mac(normalized_mac)
        incremented_ip, incremented_time, incremented_switch_ip, incremented_port = get_ip_from_mac(incremented_mac)

        mac_found = False

        # Проверяем, если найден роутер для оригинального MAC
        if original_ip:
            response += await build_ip_response(original_ip, original_time, switch_ip, port, normalized_mac, False, only_ip)
            mac_found = True

        # Проверяем, если найден роутер для инкрементированного MAC
        elif incremented_ip:
            # Если инкрементированный MAC-адрес найден, выводим оригинальный MAC
            response += await build_ip_response(incremented_ip, incremented_time, incremented_switch_ip,
                                                incremented_port, normalized_mac, True, only_ip)
            mac_found = True

        # Если роутер не найден, ищем приставку
        if not mac_found:
            provider_uid, login_at = get_info_from_24tv(normalized_mac)
            if login_at != "Не найдено":
                response += await build_provider_info(provider_uid, login_at, normalized_mac, False, only_ip)
                mac_found = True
            else:
                # Если приставка не найдена, игнорируем этот MAC-адрес
                if only_ip:  # Если только один MAC-адрес
                    response += "Не найдено ни роутеров, ни приставок.\n"
                else:
                    response += f"По MAC-адресу {normalized_mac} не найдено ни роутеров, ни приставок.\n"

        # Добавляем пустую строку, если есть больше одного устройства и не нашли информации
        if idx < len(mac_addresses) - 1:
            response += "\n"

    if not response:  # Если ничего не нашли для всех MAC-адресов
        response = 'Роутер и приставка не найдены.\n'

    await update.message.reply_text(response.strip(), parse_mode="HTML")


# Главная функция
def main():
    app = ApplicationBuilder().token(TOKEN_TELEGRAM).build()
    app.add_handler(
        MessageHandler(filters.TEXT & filters.Regex(r'(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}'), handle_mac_message))
    app.run_polling()


# Запуск приложения
if __name__ == "__main__":
    main()
