import threading
import io
import time
import socket
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

# HTML шаблон для браузера
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Stream</title>
    <style>
        body { margin: 0; background-color: #121212; display: flex; justify-content: center; align-items: center; height: 100vh; overflow: hidden; }
        img { max-width: 100%; max-height: 100vh; object-fit: contain; }
    </style>
</head>
<body>
    <img src="/video_feed" alt="Screen Stream">
</body>
</html>
"""

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

def generate_frames():
    global selected_window_title, is_streaming
    with mss.mss() as sct:
        while True:
            if not is_streaming or not selected_window_title:
                time.sleep(0.1)
                continue
            
            try:
                # Находим окно по заголовку
                window = gw.getWindowsWithTitle(selected_window_title)
                if not window:
                    time.sleep(0.5)
                    continue
                
                win = window[0]
                # Определяем область захвата (координаты окна)
                monitor = {"top": win.top, "left": win.left, "width": win.width, "height": win.height}
                
                # Захват и конвертация в JPEG
                sct_img = sct.grab(monitor)
                img = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")
                
                img_byte_arr = io.BytesIO()
                img.save(img_byte_arr, format='JPEG', quality=70) # Quality 70 для баланса скорости/качества
                frame = img_byte_arr.getvalue()
                
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + frame + b'\r\n')
            except Exception as e:
                print(f"Ошибка захвата: {e}")
                time.sleep(0.5)

@app.route('/video_feed')
def video_feed():
    return Response(generate_frames(), mimetype='multipart/x-mixed-replace; boundary=frame')

def run_flask():
    # Запуск сервера на всех интерфейсах
    app.run(host='0.0.0.0', port=5000, threaded=True, use_reloader=False)

class StreamApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("Local Streamer")
        self.geometry("400x350")
        ctk.set_appearance_mode("dark")
        
        self.grid_columnconfigure(0, weight=1)

        # UI Элементы
        self.label = ctk.CTkLabel(self, text="Выберите окно для стрима:", font=("Arial", 16, "bold"))
        self.label.grid(row=0, column=0, pady=(20, 10))

        self.window_combobox = ctk.CTkComboBox(self, values=self.get_window_list(), width=300)
        self.window_combobox.grid(row=1, column=0, pady=10)

        self.refresh_btn = ctk.CTkButton(self, text="Обновить список окон", command=self.refresh_windows)
        self.refresh_btn.grid(row=2, column=0, pady=10)

        self.stream_btn = ctk.CTkButton(self, text="Запустить стрим", fg_color="green", command=self.toggle_stream)
        self.stream_btn.grid(row=3, column=0, pady=(20, 10))

        # Получаем локальный IP
        local_ip = socket.gethostbyname(socket.gethostname())
        self.info_label = ctk.CTkLabel(self, text=f"Доступно по адресу:\nhttp://{local_ip}:5000\nили http://stream.local:5000", text_color="gray")
        self.info_label.grid(row=4, column=0, pady=10)

        # Регистрация mDNS (zeroconf)
        self.zeroconf = Zeroconf()
        info = ServiceInfo(
            "_http._tcp.local.",
            "stream._http._tcp.local.",
            addresses=[socket.inet_aton(local_ip)],
            port=5000,
            server="stream.local.",
        )
        self.zeroconf.register_service(info)

    def get_window_list(self):
        titles = gw.getAllTitles()
        return [t for t in titles if t.strip() != ""]

    def refresh_windows(self):
        self.window_combobox.configure(values=self.get_window_list())

    def toggle_stream(self):
        global is_streaming, selected_window_title
        if not is_streaming:
            selected_window_title = self.window_combobox.get()
            is_streaming = True
            self.stream_btn.configure(text="Остановить стрим", fg_color="red")
        else:
            is_streaming = False
            self.stream_btn.configure(text="Запустить стрим", fg_color="green")

    def destroy(self):
        self.zeroconf.close()
        super().destroy()

if __name__ == "__main__":
    # Запускаем веб-сервер в отдельном потоке, чтобы не блокировать GUI
    threading.Thread(target=run_flask, daemon=True).start()
    
    # Запускаем GUI
    app_gui = StreamApp()
    app_gui.mainloop()