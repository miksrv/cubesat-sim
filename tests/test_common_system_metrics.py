from src.common.system_metrics import SystemMetricsCollector


class TestSocTemperature:
    def test_reads_thermal_zone_file(self, monkeypatch, tmp_path):
        thermal_file = tmp_path / "temp"
        thermal_file.write_text("45678\n")

        real_open = open

        def fake_open(path, *args, **kwargs):
            if path == "/sys/class/thermal/thermal_zone0/temp":
                return real_open(thermal_file, *args, **kwargs)
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr("builtins.open", fake_open)
        assert SystemMetricsCollector.get_soc_temperature() == 45.7

    def test_falls_back_to_vcgencmd(self, monkeypatch):
        def fake_open(*_args, **_kwargs):
            raise FileNotFoundError

        monkeypatch.setattr("builtins.open", fake_open)
        monkeypatch.setattr(
            "os.popen",
            lambda _cmd: type("R", (), {"readline": lambda self: "temp=52.3'C\n"})(),
        )
        assert SystemMetricsCollector.get_soc_temperature() == 52.3

    def test_returns_none_when_all_sources_fail(self, monkeypatch):
        def fake_open(*_args, **_kwargs):
            raise FileNotFoundError

        monkeypatch.setattr("builtins.open", fake_open)
        monkeypatch.setattr(
            "os.popen",
            lambda _cmd: type("R", (), {"readline": lambda self: "garbage\n"})(),
        )
        assert SystemMetricsCollector.get_soc_temperature() is None

    def test_returns_none_when_vcgencmd_itself_raises(self, monkeypatch):
        def fake_open(*_args, **_kwargs):
            raise FileNotFoundError

        def boom(_cmd):
            raise OSError("vcgencmd not available")

        monkeypatch.setattr("builtins.open", fake_open)
        monkeypatch.setattr("os.popen", boom)
        assert SystemMetricsCollector.get_soc_temperature() is None


class TestResourceUsage:
    def test_cpu_usage_returns_psutil_value(self, monkeypatch):
        monkeypatch.setattr(
            "src.common.system_metrics.psutil.cpu_percent", lambda interval: 12.5
        )
        assert SystemMetricsCollector.get_cpu_usage(interval=0.1) == 12.5

    def test_cpu_usage_returns_zero_on_error(self, monkeypatch):
        def boom(interval):
            raise OSError

        monkeypatch.setattr("src.common.system_metrics.psutil.cpu_percent", boom)
        assert SystemMetricsCollector.get_cpu_usage() == 0.0

    def test_ram_usage(self, monkeypatch):
        monkeypatch.setattr(
            "src.common.system_metrics.psutil.virtual_memory",
            lambda: type("M", (), {"percent": 33.3})(),
        )
        assert SystemMetricsCollector.get_ram_usage() == 33.3

    def test_ram_usage_returns_zero_on_error(self, monkeypatch):
        def boom():
            raise OSError

        monkeypatch.setattr("src.common.system_metrics.psutil.virtual_memory", boom)
        assert SystemMetricsCollector.get_ram_usage() == 0.0

    def test_swap_usage(self, monkeypatch):
        monkeypatch.setattr(
            "src.common.system_metrics.psutil.swap_memory",
            lambda: type("M", (), {"percent": 5.0})(),
        )
        assert SystemMetricsCollector.get_swap_usage() == 5.0

    def test_swap_usage_returns_zero_on_error(self, monkeypatch):
        def boom():
            raise OSError

        monkeypatch.setattr("src.common.system_metrics.psutil.swap_memory", boom)
        assert SystemMetricsCollector.get_swap_usage() == 0.0

    def test_sd_usage(self, monkeypatch):
        monkeypatch.setattr(
            "src.common.system_metrics.psutil.disk_usage",
            lambda _path: type("D", (), {"percent": 70.1})(),
        )
        assert SystemMetricsCollector.get_sd_usage() == 70.1

    def test_sd_usage_returns_zero_on_error(self, monkeypatch):
        def boom(_path):
            raise OSError

        monkeypatch.setattr("src.common.system_metrics.psutil.disk_usage", boom)
        assert SystemMetricsCollector.get_sd_usage() == 0.0

    def test_uptime_seconds(self, monkeypatch):
        monkeypatch.setattr("src.common.system_metrics.time.time", lambda: 1000.0)
        monkeypatch.setattr("src.common.system_metrics.psutil.boot_time", lambda: 400.0)
        assert SystemMetricsCollector.get_uptime_seconds() == 600

    def test_uptime_seconds_returns_zero_on_error(self, monkeypatch):
        def boom():
            raise OSError

        monkeypatch.setattr("src.common.system_metrics.psutil.boot_time", boom)
        assert SystemMetricsCollector.get_uptime_seconds() == 0


class TestCollect:
    def test_collect_aggregates_all_metrics(self, monkeypatch):
        monkeypatch.setattr(SystemMetricsCollector, "get_soc_temperature", staticmethod(lambda: 40.0))
        monkeypatch.setattr(SystemMetricsCollector, "get_cpu_usage", staticmethod(lambda interval=0.8: 10.0))
        monkeypatch.setattr(SystemMetricsCollector, "get_ram_usage", staticmethod(lambda: 20.0))
        monkeypatch.setattr(SystemMetricsCollector, "get_swap_usage", staticmethod(lambda: 1.0))
        monkeypatch.setattr(SystemMetricsCollector, "get_sd_usage", staticmethod(lambda: 30.0))
        monkeypatch.setattr("src.common.system_metrics.time.time", lambda: 1000.0)
        monkeypatch.setattr("src.common.system_metrics.psutil.boot_time", lambda: 100.0)

        result = SystemMetricsCollector.collect(with_interval=0.1)

        assert result == {
            "cpu_percent": 10.0,
            "ram_percent": 20.0,
            "swap_percent": 1.0,
            "disk_percent": 30.0,
            "uptime_seconds": 900.0,
            "cpu_temperature": 40.0,
        }
