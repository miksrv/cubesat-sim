import time
import math
from smbus2 import SMBus as smbus
from typing import Dict, Tuple, Optional

# Константы (из твоего примера)
I2C_ADD_QMI8658 = 0x6B
I2C_ADD_AK09918 = 0x0C

# QMI8658 регистры
QMI_CTRL1    = 0x02
QMI_CTRL2    = 0x03
QMI_CTRL3    = 0x04
QMI_CTRL5    = 0x06
QMI_CTRL7    = 0x08
QMI_AX_L     = 0x35
QMI_TEMP_L   = 0x33

QMI_ACC_2G   = 0x00 << 4
QMI_ACC_1000HZ = 0x03
QMI_GYR_512DPS = 5 << 4
QMI_GYR_500HZ  = 0x04
QMI_ACC_ENABLE = 0x01
QMI_GYR_ENABLE = 0x02

# AK09918 регистры
AK_WIA2       = 0x01
AK_ST1        = 0x10
AK_HXL        = 0x11
AK_CNTL2      = 0x31
AK_CNTL3      = 0x32
AK_SRST       = 0x01
AK_CONT_20HZ  = 0x04

class IMU:
    def __init__(self):
        self.bus = smbus(1)
        self.exInt = 0.0
        self.eyInt = 0.0
        self.ezInt = 0.0
        self.gyro_offset = [0, 0, 0]
        self._init_sensors()
        self._calibrate_gyro()

    def _init_sensors(self):
        # QMI8658
        whoami = self.bus.read_byte_data(I2C_ADD_QMI8658, 0x00)
        if whoami != 0x05:
            raise RuntimeError(f"QMI8658 WHO_AM_I = 0x{whoami:02X} (ожидается 0x05)")

        self.bus.write_byte_data(I2C_ADD_QMI8658, QMI_CTRL1, 0x60)  # I2C mode
        self.bus.write_byte_data(I2C_ADD_QMI8658, QMI_CTRL2, QMI_ACC_2G | QMI_ACC_1000HZ)
        self.bus.write_byte_data(I2C_ADD_QMI8658, QMI_CTRL3, QMI_GYR_512DPS | QMI_GYR_500HZ)
        self.bus.write_byte_data(I2C_ADD_QMI8658, QMI_CTRL5, 0x00)
        self.bus.write_byte_data(I2C_ADD_QMI8658, QMI_CTRL7, QMI_ACC_ENABLE | QMI_GYR_ENABLE | 0x80)

        # AK09918
        if self.bus.read_byte_data(I2C_ADD_AK09918, AK_WIA2) != 0x0C:
            raise RuntimeError("AK09918 init failed")

        self.bus.write_byte_data(I2C_ADD_AK09918, AK_CNTL3, AK_SRST)
        time.sleep(0.01)
        self.bus.write_byte_data(I2C_ADD_AK09918, AK_CNTL2, AK_CONT_20HZ)

    def _calibrate_gyro(self, samples: int = 128):
        gx_sum = gy_sum = gz_sum = 0
        for _ in range(samples):
            ax, ay, az, gx, gy, gz = self.read_accel_gyro_raw()
            gx_sum += gx
            gy_sum += gy
            gz_sum += gz
            time.sleep(0.01)
        self.gyro_offset = [gx_sum // samples, gy_sum // samples, gz_sum // samples]

    def read_accel_gyro_raw(self) -> Tuple[int, int, int, int, int, int]:
        data = self.bus.read_i2c_block_data(I2C_ADD_QMI8658, QMI_AX_L, 12)
        ax = (data[1] << 8) | data[0]
        ay = (data[3] << 8) | data[2]
        az = (data[5] << 8) | data[4]
        gx = ((data[7] << 8) | data[6]) - self.gyro_offset[0]
        gy = ((data[9] << 8) | data[8]) - self.gyro_offset[1]
        gz = ((data[11] << 8) | data[10]) - self.gyro_offset[2]

        # Приведение к signed 16-bit
        for v in (ax, ay, az, gx, gy, gz):
            if v >= 32768: v -= 65536

        return ax, ay, az, gx, gy, gz

    def read_magnetometer_raw(self) -> Tuple[int, int, int]:
        for _ in range(20):
            st1 = self.bus.read_byte_data(I2C_ADD_AK09918, AK_ST1)
            if st1 & 0x01:
                break
            time.sleep(0.005)
        else:
            return 0, 0, 0  # timeout

        data = self.bus.read_i2c_block_data(I2C_ADD_AK09918, AK_HXL, 6)
        mx = (data[1] << 8) | data[0]
        my = (data[3] << 8) | data[2]
        mz = (data[5] << 8) | data[4]

        for v in (mx, my, mz):
            if v >= 32768: v -= 65536

        return mx, my, mz

    def read_imu_temp(self) -> float:
        data = self.bus.read_i2c_block_data(I2C_ADD_QMI8658, QMI_TEMP_L, 2)
        raw = (data[1] << 8) | data[0]
        return raw / 256.0

    # AHRS (Madgwick-like) из твоего примера — упрощённый вариант
    # Глобальные переменные quaternion (в реальности лучше в экземпляре)
    q0 = 1.0
    q1 = q2 = q3 = 0.0
    exInt = eyInt = ezInt = 0.0
    Kp = 1.0  #4.5
    Ki = 0.2  #1.0
    halfT = 0.05  # ~20 Hz, подстрой под реальный цикл

    def update_ahrs(self, gx: float, gy: float, gz: float,
                    ax: float, ay: float, az: float,
                    mx: float, my: float, mz: float):
        norm = 0.0
        vx, vy, vz = 0.0, 0.0, 0.0
        ex, ey, ez = 0.0, 0.0, 0.0

        # Нормализация акселерометра
        norm = math.sqrt(ax*ax + ay*ay + az*az)
        if norm > 0.001:
            ax /= norm
            ay /= norm
            az /= norm

        # Нормализация магнитометра
        norm = math.sqrt(mx*mx + my*my + mz*mz)
        if norm > 0.001:
            mx /= norm
            my /= norm
            mz /= norm

        # Оценка направления гравитации в теле
        vx = 2*(self.q1*self.q3 - self.q0*self.q2)
        vy = 2*(self.q0*self.q1 + self.q2*self.q3)
        vz = self.q0*self.q0 - self.q1*self.q1 - self.q2*self.q2 + self.q3*self.q3

        # Ошибка между измеренной и ожидаемой гравитацией
        ex = (ay * vz - az * vy)
        ey = (az * vx - ax * vz)
        ez = (ax * vy - ay * vx)

        self.exInt += ex * self.Ki * self.halfT
        self.eyInt += ey * self.Ki * self.halfT
        self.ezInt += ez * self.Ki * self.halfT

        gx += self.Kp * ex + self.exInt
        gy += self.Kp * ey + self.eyInt
        gz += self.Kp * ez + self.ezInt

        # Интеграция quaternion
        self.q0 += (-self.q1*gx - self.q2*gy - self.q3*gz) * self.halfT
        self.q1 += ( self.q0*gx + self.q2*gz - self.q3*gy) * self.halfT
        self.q2 += ( self.q0*gy - self.q1*gz + self.q3*gx) * self.halfT
        self.q3 += ( self.q0*gz + self.q1*gy - self.q2*gx) * self.halfT

        norm = math.sqrt(self.q0**2 + self.q1**2 + self.q2**2 + self.q3**2)
        if norm > 0:
            self.q0 /= norm
            self.q1 /= norm
            self.q2 /= norm
            self.q3 /= norm

    def get_orientation_deg(self) -> Dict[str, float]:
        """Returns roll, pitch, yaw in degrees, plus accel_g and gyro_dps dicts"""
        # Get scaled values
        gx_rad, gy_rad, gz_rad, ax_g, ay_g, az_g, mx, my, mz = self._get_scaled_motion()
        # Convert gyro from rad/s to dps
        gx_dps = gx_rad * 57.2958
        gy_dps = gy_rad * 57.2958
        gz_dps = gz_rad * 57.2958

        # Update AHRS
        self.update_ahrs(gx_rad, gy_rad, gz_rad, ax_g, ay_g, az_g, mx, my, mz)

        pitch = math.asin(-2 * self.q1 * self.q3 + 2 * self.q0 * self.q2) * 57.2958
        roll  = math.atan2(2 * self.q2 * self.q3 + 2 * self.q0 * self.q1,
                           -2 * self.q1**2 - 2 * self.q2**2 + 1) * 57.2958
        yaw   = math.atan2(-2 * self.q1 * self.q2 - 2 * self.q0 * self.q3,
                           2 * self.q2**2 + 2 * self.q3**2 - 1) * 57.2958

        return {
            "roll": round(roll, 2),
            "pitch": round(pitch, 2),
            "yaw": round(yaw, 2),
            "accel_g": {
                "x": round(ax_g, 2),
                "y": round(ay_g, 2),
                "z": round(az_g, 2)
            },
            "gyro_dps": {
                "x": round(gx_dps, 2),
                "y": round(gy_dps, 2),
                "z": round(gz_dps, 2)
            }
        }

    def _get_scaled_motion(self) -> Tuple[float, ...]:
        ax, ay, az, gx, gy, gz = self.read_accel_gyro_raw()
        mx, my, mz = self.read_magnetometer_raw()

        # Масштабирование (примерные коэффициенты — подстрой!)
        ax_g = ax / 16384.0   # ±2g → правильно
        ay_g = ay / 16384.0
        az_g = az / 16384.0

        # Гироскоп: ±512 dps → 64 LSB/(°/s)
        gx_dps = gx / 64.0    # ← ИСПРАВИТЬ ЗДЕСЬ! Было 32.8 → стало 64.0
        gy_dps = gy / 64.0
        gz_dps = gz / 64.0

        # Магнитометр в условных единицах (можно нормализовать позже)
        return (
            gx_dps * 0.0174533, gy_dps * 0.0174533, gz_dps * 0.0174533,  # rad/s
            ax_g, ay_g, az_g,
            mx, my, mz
        )