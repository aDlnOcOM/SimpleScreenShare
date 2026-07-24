import threading
import io
import time
import socket
import subprocess
import customtkinter as ctk
import pygetwindow as gw
import mss
from PIL import Image
from flask import Flask, Response, render_template_string
from zeroconf import ServiceInfo, Zeroconf

# Настройка Flask
app = Flask(__name__)
selected_window_title = None
is_streaming = False

# Настройки качества и FPS по умолчанию
STREAM_QUALITY = 65
TARGET_FPS = 30

# HTML шаблон с отключением кэширования для минимальной задержки
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>USB Stream</title>
    <style>
        body { margin: 0; background-color: #000; display: flex; justify-content: center; align-items: center; height: 100vh; overflow: hidden; }
        img { max-width: 100%; max-height: 100vh; object-fit: contain; }
    </style>
</head>
<body>
    <img src="/video_feed" alt="Screen Stream">
</body>
</html>
"""


def get_all_ip_addresses():
    """Получение всех локальных IPv4 адресов (включая USB Tethering)"""
    ip_list = []
    try:
        # Получаем имя хоста и все связанные IP
        hostname = socket.gethostname()
        addresses = socket.getaddrinfo(hostname, None)
        for addr in addresses:
            ip = addr[4][0]
            # Отфильтровываем IPv6 и loopback
            if ":" not in ip and not ip.startswith("127."):
                if ip not in ip_list:
                    ip_list.append(ip)
    except Exception:
        pass

    # Резервный способ получения IP через подключение к внешнему сокету
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        main_ip = s.getsockname()[0]
        s.close()
        if main_ip not in ip_list:
            ip_list.append(main_ip)
    except Exception:
        pass

    return ip_list


@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)


PLACEHOLDER_FRAME = None


def get_placeholder():
    """Создает статичный темный кадр-заглушку, чтобы браузер не зависал в ожидании"""
    global PLACEHOLDER_FRAME
    if PLACEHOLDER_FRAME is None:
        # Создаем простой темно-серый фон
        img = Image.new('RGB', (640, 480), color=(30, 30, 30))
        img_byte_arr = io.BytesIO()
        img.save(img_byte_arr, format='JPEG', quality=30)
        PLACEHOLDER_FRAME = img_byte_arr.getvalue()
    return PLACEHOLDER_FRAME


def generate_frames():
    global selected_window_title, is_streaming, STREAM_QUALITY, TARGET_FPS

    frame_duration = 1.0 / TARGET_FPS

    with mss.mss() as sct:
        while True:
            start_time = time.time()

            # 1. Если стрим на паузе — отдаем заглушку
            if not is_streaming or not selected_window_title:
                frame = get_placeholder()
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + frame + b'\r\n')
                time.sleep(0.5)  # Обновляем заглушку редко (2 кадра в секунду)
                continue

            try:
                window = gw.getWindowsWithTitle(selected_window_title)

                # 2. Если окно пропало или закрыто — тоже отдаем заглушку
                if not window or window[0].width <= 0 or window[0].height <= 0:
                    frame = get_placeholder()
                    yield (b'--frame\r\n'
                           b'Content-Type: image/jpeg\r\n\r\n' + frame + b'\r\n')
                    time.sleep(0.5)
                    continue

                win = window[0]
                monitor = {"top": win.top, "left": win.left, "width": win.width, "height": win.height}

                # Основной захват
                sct_img = sct.grab(monitor)
                img = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")

                img_byte_arr = io.BytesIO()
                img.save(img_byte_arr, format='JPEG', quality=STREAM_QUALITY, optimize=False)
                frame = img_byte_arr.getvalue()

                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + frame + b'\r\n')

                # Контроль FPS
                elapsed = time.time() - start_time
                sleep_time = frame_duration - elapsed
                if sleep_time > 0:
                    time.sleep(sleep_time)

            except Exception as e:
                # В случае сбоя захвата спасаемся заглушкой
                frame = get_placeholder()
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + frame + b'\r\n')
                time.sleep(0.5)

def generate_frames():
    global selected_window_title, is_streaming, STREAM_QUALITY, TARGET_FPS

    frame_duration = 1.0 / TARGET_FPS

    with mss.mss() as sct:
        while True:
            start_time = time.time()

            if not is_streaming or not selected_window_title:
                time.sleep(0.05)
                continue

            try:
                window = gw.getWindowsWithTitle(selected_window_title)
                if not window:
                    time.sleep(0.2)
                    continue

                win = window[0]
                if win.width <= 0 or win.height <= 0:
                    time.sleep(0.2)
                    continue

                monitor = {"top": win.top, "left": win.left, "width": win.width, "height": win.height}

                sct_img = sct.grab(monitor)
                img = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")

                img_byte_arr = io.BytesIO()
                # Сохраняем в JPEG с оптимизацией
                img.save(img_byte_arr, format='JPEG', quality=STREAM_QUALITY, optimize=False)
                frame = img_byte_arr.getvalue()

                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + frame + b'\r\n')

                # Контроль FPS
                elapsed = time.time() - start_time
                sleep_time = frame_duration - elapsed
                if sleep_time > 0:
                    time.sleep(sleep_time)

            except Exception as e:
                time.sleep(0.1)


@app.route('/video_feed')
def video_feed():
    response = Response(generate_frames(), mimetype='multipart/x-mixed-replace; boundary=frame')
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response


def run_flask():
    app.run(host='0.0.0.0', port=5000, threaded=True, use_reloader=False)


class StreamApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("USB & LAN Screen Streamer")
        self.geometry("450x520")
        ctk.set_appearance_mode("dark")

        self.grid_columnconfigure(0, weight=1)

        # UI Элементы
        self.label = ctk.CTkLabel(self, text="Захват окна:", font=("Arial", 15, "bold"))
        self.label.grid(row=0, column=0, pady=(15, 5))

        self.window_combobox = ctk.CTkComboBox(self, values=self.get_window_list(), width=340)
        self.window_combobox.grid(row=1, column=0, pady=5)

        self.refresh_btn = ctk.CTkButton(self, text="Обновить список окон", command=self.refresh_windows, width=200)
        self.refresh_btn.grid(row=2, column=0, pady=5)

        # Настройки качества
        self.quality_label = ctk.CTkLabel(self, text=f"Качество JPEG: {STREAM_QUALITY}%")
        self.quality_label.grid(row=3, column=0, pady=(10, 0))

        self.quality_slider = ctk.CTkSlider(self, from_=30, to=95, number_of_steps=13, command=self.update_quality)
        self.quality_slider.set(STREAM_QUALITY)
        self.quality_slider.grid(row=4, column=0, pady=5)

        # Кнопка управления
        self.stream_btn = ctk.CTkButton(self, text="Запустить трансляцию", fg_color="green", font=("Arial", 14, "bold"),
                                        height=40, command=self.toggle_stream)
        self.stream_btn.grid(row=5, column=0, pady=15)

        # Текстовое поле с адресами подключения
        self.info_text = ctk.CTkTextbox(self, width=380, height=140, font=("Consolas", 12))
        self.info_text.grid(row=6, column=0, pady=10)

        self.update_ip_info()

    def get_window_list(self):
        titles = gw.getAllTitles()
        return [t for t in titles if t.strip() != ""]

    def refresh_windows(self):
        self.window_combobox.configure(values=self.get_window_list())

    def update_quality(self, value):
        global STREAM_QUALITY
        STREAM_QUALITY = int(value)
        self.quality_label.configure(text=f"Качество JPEG: {STREAM_QUALITY}%")

    def try_adb_reverse(self):
        """Попытка выполнения ADB reverse для трансляции по USB через localhost"""
        try:
            result = subprocess.run(["adb", "reverse", "tcp:5000", "tcp:5000"], capture_output=True, text=True,
                                    timeout=2)
            if result.returncode == 0:
                return True
        except Exception:
            pass
        return False

    def update_ip_info(self):
        self.info_text.configure(state="normal")
        self.info_text.delete("1.0", "end")

        text = "--- ССЫЛКИ ДЛЯ ПОДКЛЮЧЕНИЯ ---\n\n"

        # Проверка ADB
        if self.try_adb_reverse():
            text += "[USB ADB Подключено!]\n-> http://localhost:5000\n\n"

        # Поиск остальных IP (включая USB Tethering)
        ips = get_all_ip_addresses()
        if ips:
            text += "[USB-Модем / Wi-Fi / LAN]:\n"
            for ip in ips:
                text += f"-> http://{ip}:5000\n"

        text += "\n[mDNS]: http://stream.local:5000"

        self.info_text.insert("1.0", text)
        self.info_text.configure(state="disabled")

    def toggle_stream(self):
        global is_streaming, selected_window_title
        if not is_streaming:
            selected_window_title = self.window_combobox.get()
            is_streaming = True
            self.stream_btn.configure(text="Остановить трансляцию", fg_color="red")
            self.update_ip_info()
        else:
            is_streaming = False
            self.stream_btn.configure(text="Запустить трансляцию", fg_color="green")


if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    app_gui = StreamApp()
    app_gui.mainloop()