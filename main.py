import threading
import asyncio
import json
import time
import socket
import subprocess
import customtkinter as ctk
import pygetwindow as gw
import mss
import numpy as np
from PIL import Image

from aiohttp import web
from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
from av import VideoFrame
from zeroconf import ServiceInfo, Zeroconf

# Глобальные переменные состояния
selected_window_title = None
is_streaming = False

# HTML + JavaScript для WebRTC клиента
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>WebRTC Stream</title>
    <style>
        body { margin: 0; background-color: #000; display: flex; justify-content: center; align-items: center; height: 100vh; overflow: hidden; }
        video { max-width: 100%; max-height: 100vh; object-fit: contain; }
        #status { position: absolute; top: 10px; left: 10px; color: #00ff00; font-family: monospace; background: rgba(0,0,0,0.5); padding: 5px; }
    </style>
</head>
<body>
    <div id="status">Connecting WebRTC...</div>
    <video id="video" autoplay playsinline></video>
    <script>
        async function start() {
            const pc = new RTCPeerConnection();

            pc.addEventListener('track', function(evt) {
                if (evt.track.kind === 'video') {
                    document.getElementById('video').srcObject = evt.streams[0];
                    document.getElementById('status').innerText = 'WebRTC Connected (Live)';
                }
            });

            pc.addTransceiver('video', {direction: 'recvonly'});

            const offer = await pc.createOffer();
            await pc.setLocalDescription(offer);

            const response = await fetch('/offer', {
                body: JSON.stringify({
                    sdp: pc.localDescription.sdp,
                    type: pc.localDescription.type
                }),
                headers: {'Content-Type': 'application/json'},
                method: 'POST'
            });

            const answer = await response.json();
            await pc.setRemoteDescription(answer);
        }
        start();
    </script>
</body>
</html>
"""


class WindowCaptureTrack(VideoStreamTrack):
    """Кастомный видео-трек для WebRTC, захватывающий окно"""
    kind = "video"

    def __init__(self):
        super().__init__()
        self.sct = mss.mss()
        self.last_frame = None
        self.target_size = None  # Фиксированное разрешение стрима

    async def recv(self):
        global selected_window_title, is_streaming

        while True:
            # 1. Если стрим на паузе или окно не выбрано
            if not is_streaming or not selected_window_title:
                if self.last_frame is not None:
                    # Отдаем последний успешный кадр (замораживаем картинку)
                    pts, time_base = await self.next_timestamp()
                    frame = self.last_frame
                    frame.pts = pts
                    frame.time_base = time_base
                    await asyncio.sleep(0.1)  # Снижаем нагрузку (10 FPS в простое)
                    return frame
                else:
                    # Если стрим еще ни разу не запускался — просто ждем
                    await asyncio.sleep(0.1)
                    continue

            # 2. Если трансляция активна
            try:
                windows = gw.getWindowsWithTitle(selected_window_title)
                if not windows or windows[0].width <= 0 or windows[0].height <= 0:
                    raise Exception("Окно не найдено или свернуто")

                win = windows[0]

                # КРИТИЧЕСКИ ВАЖНО: Делаем ширину и высоту четными (требование H.264)
                w = win.width - (win.width % 2)
                h = win.height - (win.height % 2)

                if w <= 0 or h <= 0:
                    raise Exception("Некорректный размер окна")

                # При первом успешном кадре жестко фиксируем разрешение трансляции
                if self.target_size is None:
                    self.target_size = (w, h)

                monitor = {"top": win.top, "left": win.left, "width": w, "height": h}

                # Асинхронный захват (чтобы не блокировать внутренние таймеры WebRTC)
                loop = asyncio.get_event_loop()
                sct_img = await loop.run_in_executor(None, self.sct.grab, monitor)

                # Переводим в PIL (это сразу удалит проблемный Альфа-канал / прозрачность)
                img = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")

                # Если пользователь растянул/уменьшил окно в процессе — подгоняем под изначальный размер,
                # чтобы не сломать кодек изменением разрешения.
                if img.size != self.target_size:
                    img = img.resize(self.target_size)

                img_np = np.array(img)

                # Формируем готовый видеокадр
                frame = VideoFrame.from_ndarray(img_np, format="rgb24")

                pts, time_base = await self.next_timestamp()
                frame.pts = pts
                frame.time_base = time_base
                self.last_frame = frame

                return frame

            except Exception as e:
                # Если окно случайно закрыли или свернули — спасаемся последним кадром
                if self.last_frame is not None:
                    pts, time_base = await self.next_timestamp()
                    frame = self.last_frame
                    frame.pts = pts
                    frame.time_base = time_base
                    await asyncio.sleep(0.1)
                    return frame
                else:
                    await asyncio.sleep(0.1)
                    continue


# --- AIOHTTP Server ---
pcs = set()


async def index(request):
    return web.Response(content_type='text/html', text=HTML_TEMPLATE)


async def offer(request):
    params = await request.json()
    offer_sdp = RTCSessionDescription(sdp=params["sdp"], type=params["type"])

    pc = RTCPeerConnection()
    pcs.add(pc)

    @pc.on("connectionstatechange")
    async def on_connectionstatechange():
        if pc.connectionState in ["failed", "closed"]:
            pcs.discard(pc)

    # Добавляем наш кастомный трек с захватом экрана
    pc.addTrack(WindowCaptureTrack())

    # Устанавливаем соединение
    await pc.setRemoteDescription(offer_sdp)
    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)

    return web.json_response({
        "sdp": pc.localDescription.sdp,
        "type": pc.localDescription.type
    })


async def on_shutdown(app):
    coros = [pc.close() for pc in pcs]
    await asyncio.gather(*coros)
    pcs.clear()


def run_server():
    # Настраиваем асинхронный цикл событий для отдельного потока
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    app = web.Application()
    app.on_shutdown.append(on_shutdown)
    app.router.add_get('/', index)
    app.router.add_post('/offer', offer)

    # handle_signals=False крайне важен, так как сервер запускается не в главном потоке
    web.run_app(app, host='0.0.0.0', port=5000, handle_signals=False)


# --- GUI ---
def get_all_ip_addresses():
    ip_list = []
    try:
        hostname = socket.gethostname()
        addresses = socket.getaddrinfo(hostname, None)
        for addr in addresses:
            ip = addr[4][0]
            if ":" not in ip and not ip.startswith("127.") and ip not in ip_list:
                ip_list.append(ip)
    except Exception:
        pass
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


class StreamApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("WebRTC Zero-Latency Streamer")
        self.geometry("450x450")
        ctk.set_appearance_mode("dark")
        self.grid_columnconfigure(0, weight=1)

        self.label = ctk.CTkLabel(self, text="Захват окна (WebRTC):", font=("Arial", 15, "bold"))
        self.label.grid(row=0, column=0, pady=(15, 5))

        self.window_combobox = ctk.CTkComboBox(self, values=self.get_window_list(), width=340)
        self.window_combobox.grid(row=1, column=0, pady=5)

        self.refresh_btn = ctk.CTkButton(self, text="Обновить список окон", command=self.refresh_windows, width=200)
        self.refresh_btn.grid(row=2, column=0, pady=5)

        self.stream_btn = ctk.CTkButton(self, text="Запустить трансляцию", fg_color="green", font=("Arial", 14, "bold"),
                                        height=40, command=self.toggle_stream)
        self.stream_btn.grid(row=3, column=0, pady=(20, 15))

        self.info_text = ctk.CTkTextbox(self, width=380, height=140, font=("Consolas", 12))
        self.info_text.grid(row=4, column=0, pady=10)

        self.update_ip_info()

    def get_window_list(self):
        titles = gw.getAllTitles()
        return [t for t in titles if t.strip() != ""]

    def refresh_windows(self):
        self.window_combobox.configure(values=self.get_window_list())

    def try_adb_reverse(self):
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
        if self.try_adb_reverse():
            text += "[USB ADB Подключено!]\n-> http://localhost:5000\n\n"
        ips = get_all_ip_addresses()
        if ips:
            text += "[USB-Модем / Wi-Fi / LAN]:\n"
            for ip in ips:
                text += f"-> http://{ip}:5000\n"
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
    # Запуск асинхронного WebRTC сервера в фоновом потоке
    threading.Thread(target=run_server, daemon=True).start()

    app_gui = StreamApp()
    app_gui.mainloop()