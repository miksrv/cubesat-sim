import pytest

import src.common.imu_qmi8658_ak09918 as imu_mod
from tests.fakes import FakeI2CBus

QMI = imu_mod.I2C_ADD_QMI8658
AK = imu_mod.I2C_ADD_AK09918


def make_imu(monkeypatch, byte_responses=None, block_responses=None):
    responses = {(QMI, 0x00): 0x05, (AK, imu_mod.AK_WIA2): 0x0C}
    responses.update(byte_responses or {})
    bus = FakeI2CBus(byte_responses=responses, block_responses=block_responses or {})
    monkeypatch.setattr(imu_mod, "smbus", lambda _bus_num: bus)
    return imu_mod.IMU(), bus


class TestInitSensors:
    def test_raises_when_qmi8658_whoami_mismatch(self, monkeypatch):
        with pytest.raises(RuntimeError, match="QMI8658"):
            make_imu(monkeypatch, byte_responses={(QMI, 0x00): 0x99})

    def test_raises_when_ak09918_whoami_mismatch(self, monkeypatch):
        with pytest.raises(RuntimeError, match="AK09918"):
            make_imu(monkeypatch, byte_responses={(AK, imu_mod.AK_WIA2): 0x00})

    def test_succeeds_with_correct_whoami_values(self, monkeypatch):
        imu, _bus = make_imu(monkeypatch)
        assert imu.gyro_offset == [0, 0, 0]


class TestCalibrateGyro:
    def test_averages_constant_raw_samples(self, monkeypatch):
        # gz = 300 encoded little-endian: lo=0x2C, hi=0x01
        block = {(QMI, imu_mod.QMI_AX_L): [0, 0, 0, 0, 0, 0, 100, 0, 200, 0, 0x2C, 0x01]}
        imu, _bus = make_imu(monkeypatch, block_responses=block)
        assert imu.gyro_offset == [100, 200, 300]


class TestReadAccelGyroRaw:
    def test_subtracts_calibrated_gyro_offset(self, monkeypatch):
        block = {(QMI, imu_mod.QMI_AX_L): [0, 0, 0, 0, 0, 0, 100, 0, 200, 0, 0x2C, 0x01]}
        imu, bus = make_imu(monkeypatch, block_responses=block)
        # After calibration, offset == the constant raw values above -> reads back as 0.
        assert imu.read_accel_gyro_raw() == (0, 0, 0, 0, 0, 0)

    def test_handles_negative_values(self, monkeypatch):
        imu, bus = make_imu(monkeypatch)
        # -1 as unsigned 16-bit little-endian is 0xFF, 0xFF
        bus.block_responses[(QMI, imu_mod.QMI_AX_L)] = [
            0xFF, 0xFF,  # ax = -1
            0x00, 0x00,  # ay = 0
            0x00, 0x00,  # az = 0
            0x00, 0x00,  # gx = 0 (offset 0)
            0x00, 0x00,  # gy = 0
            0x00, 0x00,  # gz = 0
        ]
        ax, ay, az, gx, gy, gz = imu.read_accel_gyro_raw()
        assert (ax, ay, az) == (-1, 0, 0)
        assert (gx, gy, gz) == (0, 0, 0)


class TestReadMagnetometerRaw:
    def test_returns_zeros_on_timeout(self, monkeypatch):
        imu, _bus = make_imu(monkeypatch)  # ST1 register never sets the ready bit
        assert imu.read_magnetometer_raw() == (0, 0, 0)

    def test_reads_values_once_ready(self, monkeypatch):
        imu, bus = make_imu(
            monkeypatch,
            byte_responses={(AK, imu_mod.AK_ST1): 0x01},
            block_responses={(AK, imu_mod.AK_HXL): [10, 0, 20, 0, 30, 0]},
        )
        assert imu.read_magnetometer_raw() == (10, 20, 30)


class TestReadImuTemp:
    def test_converts_raw_to_celsius(self, monkeypatch):
        # raw = 0x1400 = 5120 -> 5120 / 256.0 == 20.0
        block = {(QMI, imu_mod.QMI_TEMP_L): [0x00, 0x14]}
        imu, _bus = make_imu(monkeypatch, block_responses=block)
        assert imu.read_imu_temp() == 20.0


class TestGetOrientationDeg:
    def test_structure_and_values_at_rest(self, monkeypatch):
        # Same constant raw gyro values during calibration and afterwards ->
        # offset-corrected gyro reads as zero; accel/mag all zero too.
        block = {(QMI, imu_mod.QMI_AX_L): [0, 0, 0, 0, 0, 0, 100, 0, 200, 0, 0x2C, 0x01]}
        imu, _bus = make_imu(monkeypatch, block_responses=block)

        result = imu.get_orientation_deg()

        assert set(result.keys()) == {"roll", "pitch", "yaw", "accel_g", "gyro_dps"}
        assert result["roll"] == 0.0
        assert result["pitch"] == 0.0
        assert result["yaw"] == pytest.approx(-180.0)
        assert result["accel_g"] == {"x": 0.0, "y": 0.0, "z": 0.0}
        assert result["gyro_dps"] == {"x": 0.0, "y": 0.0, "z": 0.0}

    def test_quaternion_stays_normalized_after_multiple_updates(self, monkeypatch):
        imu, bus = make_imu(monkeypatch)
        bus.block_responses[(QMI, imu_mod.QMI_AX_L)] = [
            0, 0x40,  # ax raw = 0x4000 = 16384 -> 1g
            0, 0,
            0, 0,
            50, 0,   # small gyro rate
            0, 0,
            0, 0,
        ]
        bus.byte_responses[(AK, imu_mod.AK_ST1)] = 0x01
        bus.block_responses[(AK, imu_mod.AK_HXL)] = [100, 0, 0, 0, 0, 0]

        for _ in range(10):
            imu.get_orientation_deg()

        norm = (imu.q0 ** 2 + imu.q1 ** 2 + imu.q2 ** 2 + imu.q3 ** 2) ** 0.5
        assert norm == pytest.approx(1.0, abs=1e-6)
