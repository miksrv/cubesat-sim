# src/obc/state_machine.py
from transitions import Machine
import logging
import json

logger = logging.getLogger(__name__)

class CubeSatStateMachine:
    """Конечный автомат состояний спутника (OBC)"""

    states = [
        'BOOT',       # Инициализация после включения
        'DEPLOY',     # Развёртывание (антенны, калибровка)
        'NOMINAL',    # Штатный режим
        'SCIENCE',    # Научный режим (интенсивная работа payload)
        'LOW_POWER',  # Энергосбережение
        'SAFE'        # Аварийный минимальный режим
    ]

    transitions = [
        # Автоматический переход после успешной загрузки
        {'trigger': 'auto_deploy', 'source': 'BOOT', 'dest': 'DEPLOY'},

        # Успешное развёртывание → штатный режим
        {'trigger': 'deployment_complete', 'source': 'DEPLOY', 'dest': 'NOMINAL'},

        # Команды / нормальные переходы
        {'trigger': 'start_science', 'source': 'NOMINAL', 'dest': 'SCIENCE'},
        {'trigger': 'end_science',   'source': 'SCIENCE', 'dest': 'NOMINAL'},

        # Энергетические переходы (обычно вызываются из EPS)
        {'trigger': 'enter_low_power', 'source': ['NOMINAL', 'SCIENCE', 'DEPLOY'], 'dest': 'LOW_POWER'},
        {'trigger': 'enter_safe_mode', 'source': '*', 'dest': 'SAFE'},

        # Восстановление
        {'trigger': 'recover', 'source': ['LOW_POWER', 'SAFE'], 'dest': 'NOMINAL'},
    ]

    def __init__(self, obc):
        self.obc = obc  # ссылка на родительский OBC для вызова методов
        self.machine = Machine(
            model=self,
            states=CubeSatStateMachine.states,
            transitions=CubeSatStateMachine.transitions,
            initial='BOOT'
        )

    # ────────────── Коллбеки входа в состояния ──────────────
    def on_enter_BOOT(self):
        logger.info("OBC → BOOT: запуск самотестирования...")
        self.publish_state({"step": "self_test_started"})
        # Здесь можно добавить проверки оборудования
        # После тестов:
        self.auto_deploy()

    def on_enter_DEPLOY(self):
        logger.info("OBC → DEPLOY: развёртывание систем...")
        self.publish_state({"step": "antenna_deploying"})
        # Симуляция: 5–15 секунд
        # В реальности: раскрытие антенны, калибровка ADCS и т.д.
        # После завершения:
        self.deployment_complete()

    def on_enter_NOMINAL(self):
        logger.info("OBC → NOMINAL: штатный режим")
        self.publish_state()
        # self.obc.publish_control("telegram/start", "")   # если нужно
        # self.obc.publish_control("wifi/on", "")
        # self.obc.publish_control("payload/on", "")

    def on_enter_SCIENCE(self):
        logger.info("OBC → SCIENCE: научный режим")
        self.publish_state()

    def on_enter_LOW_POWER(self):
        logger.warning("OBC → LOW_POWER: режим энергосбережения")
        self.publish_state()
        # self.obc.publish_control("telegram/stop", "")
        # self.obc.publish_control("wifi/off", "")
        # self.obc.publish_control("payload/off", "")
        # self.obc.publish_control("adcs/reduce_frequency", "60")  # пример

    def on_enter_SAFE(self):
        logger.critical("OBC → SAFE: аварийный режим!")
        self.publish_state()
        # self.obc.publish_control("all/non_critical/off", "")
        # Только EPS и редкая телеметрия остаются активны

    def publish_state(self, extra=None):
        """
        Publishes the current state to MQTT.
        """
        payload = {"state": self.state}
        if extra:
            payload.update(extra)
        self.obc.publish("cubesat/obc/status", json.dumps(payload), retain=True)
