from picamera2 import Picamera2
from picamera2.encoders import JpegEncoder
from picamera2.outputs import FileOutput
import time
import os
import logging
from threading import Thread, Event

logger = logging.getLogger(__name__)

class PayloadCamera:
    PHOTO_DIR = "/home/mik/cubesat-sim/data/photos"  # или из конфига
    def __init__(self):
        os.makedirs(self.PHOTO_DIR, exist_ok=True)
        self.picam2 = Picamera2()
        self.timelapse_running = False
        self.timelapse_thread = None
        self.stop_event = Event()

        # Конфигурация камеры (можно настраивать)
        config = self.picam2.create_still_configuration(
            main={"size": (1920, 1080)},  # или (4056, 3040) для полного разрешения
            lores={"size": (640, 480), "format": "RGB888"}
        )
        self.picam2.configure(config)
        self.picam2.start()

    def take_photo(self, overlay=False):
        """Делает одно фото и возвращает путь к файлу"""
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        filename = f"photo_{timestamp}.jpg"
        path = os.path.join(self.PHOTO_DIR, filename)

        try:
            # Простой захват
            request = self.picam2.capture_request()
            request.save("main", path)
            request.release()

            logger.info(f"Фото сохранено: {path}")

            # Если overlay=True — здесь можно добавить логику (см. предыдущий ответ)
            # Пока просто возвращаем путь
            return path

        except Exception as e:
            logger.error(f"Ошибка съёмки фото: {e}")
            return None

    def start_timelapse(self, interval_sec=60):
        if self.timelapse_running:
            logger.warning("Timelapse уже запущен")
            return

        self.timelapse_running = True
        self.stop_event.clear()
        self.timelapse_thread = Thread(target=self._timelapse_loop, args=(interval_sec,))
        self.timelapse_thread.daemon = True
        self.timelapse_thread.start()
        logger.info(f"Timelapse запущен с интервалом {interval_sec} сек")

    def _timelapse_loop(self, interval):
        while not self.stop_event.is_set():
            self.take_photo()  # можно добавить overlay
            time.sleep(interval)

    def stop_timelapse(self):
        if not self.timelapse_running:
            return
        self.stop_event.set()
        if self.timelapse_thread:
            self.timelapse_thread.join(timeout=5)
        self.timelapse_running = False
        logger.info("Timelapse остановлен")

    def cleanup(self):
        self.stop_timelapse()
        self.picam2.stop()
        self.picam2.close()
        logger.info("Камера остановлена")